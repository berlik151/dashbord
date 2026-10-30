"""Builds the JSON report for one period from the stored aggregates."""
import datetime as dt
import json
from zoneinfo import ZoneInfo

PROVIDERS = ('codex', 'claude')
TOKEN_FIELDS = ('input', 'output', 'cache_read', 'cache_write')
# Snapshots older than this are shown as stale.
LIMITS_STALE_SECONDS = 600
# An unfinished turn without events for this long is shown as unresolved.
OPEN_TURN_SECONDS = 120
RECENT_EVENT_SECONDS = 600


def union_seconds(intervals):
    """Total length of (start, end) intervals, counting overlaps once."""
    covered_until = None
    total = 0
    for start, stop in sorted(intervals):
        if stop <= start:
            continue
        begin = start if covered_until is None else max(start, covered_until)
        total += max(0, stop - begin)
        covered_until = stop if covered_until is None else max(covered_until, stop)
    return round(total)


def build_report(store, statuses, integrations, host, now, days, timezone):
    tz = ZoneInfo(timezone)
    today = dt.datetime.fromtimestamp(now, tz).date()
    first_day = today - dt.timedelta(days=days - 1)
    since = dt.datetime.combine(first_day, dt.time.min, tz).timestamp()
    history = {str(first_day + dt.timedelta(days=i)): {name: 0 for name in PROVIDERS} for i in range(days)}

    providers = {}
    all_intervals = []
    for name in PROVIDERS:
        provider = usage_summary(store, name, since, now, tz, history)
        provider.update(source=statuses[name], integration=integrations.get(name, {}),
                        limits=limits_summary(store, name, now))
        provider['activity'], intervals = activity_summary(store, name, since, now, provider['last'])
        all_intervals += intervals
        providers[name] = provider

    # Parallel work of both agents is counted once, like parallel Codex tasks.
    activity = {'tracked': any(p['activity']['tracked'] for p in providers.values()),
                'completed_seconds': union_seconds(all_intervals)}
    return {
        'generated_at': now,
        'period': {'days': days, 'since': since, 'timezone': timezone},
        'providers': providers,
        'activity': activity,
        'history': [{'date': date, **values} for date, values in history.items()],
        'host': host,
    }


def usage_summary(store, name, since, now, tz, history):
    """Token totals for the period; also adds each record to the daily `history`."""
    rows = store.db.execute('SELECT * FROM usage WHERE provider = ? AND ts >= ? AND ts <= ?',
                            (name, since, now)).fetchall()
    first, last, records = store.db.execute(
        'SELECT MIN(ts), MAX(ts), COUNT(*) FROM usage WHERE provider = ?', (name,)).fetchone()
    summary = {field: 0 for field in TOKEN_FIELDS}
    for row in rows:
        for field in TOKEN_FIELDS:
            summary[field] += row[field]
        day = dt.datetime.fromtimestamp(row['ts'], tz).date().isoformat()
        if day in history:
            history[day][name] += row['input'] + row['output']
    # Cache is already part of input, so it is not added again.
    summary['total'] = summary['input'] + summary['output']
    summary.update(sessions=len({row['session'] for row in rows}), first=first, last=last, records=records)
    return summary


def limits_summary(store, name, now):
    row = store.db.execute('SELECT * FROM limits WHERE provider = ?', (name,)).fetchone()
    if not row:
        return None
    limits = {'observed_at': row['observed'], 'source': row['source'],
              'stale': now - row['observed'] > LIMITS_STALE_SECONDS, **json.loads(row['windows'])}
    for key in ('primary', 'secondary'):
        window = limits.get(key)
        if window:
            # After the reset the old percentage says nothing about what is left.
            window['expired'] = window['resets_at'] is not None and window['resets_at'] <= now
            window['remaining_percent'] = None if window['expired'] else max(0, 100 - window['used_percent'])
    return limits


def activity_summary(store, name, since, now, last_usage):
    """Confirmed task time in the period. Returns the summary and the clipped intervals."""
    turns = store.db.execute('SELECT * FROM turns WHERE provider = ?', (name,)).fetchall()
    intervals = [(max(since, turn['start']), min(now, turn['end'])) for turn in turns
                 if turn['start'] is not None and turn['end'] is not None
                 and turn['end'] >= since and turn['end'] >= turn['start']]
    # Only unfinished turns with events in the selected period; older ones would never disappear.
    unfinished = [turn for turn in turns
                  if turn['end'] is None and (turn['last'] or turn['start'] or 0) >= since]
    recently_active = [turn for turn in unfinished if turn['last'] and now - turn['last'] < OPEN_TURN_SECONDS]
    summary = {
        'tracked': bool(turns),
        'completed_seconds': union_seconds(intervals),
        'completed_turns': len(intervals),
        'open_recent': len(recently_active),
        'unresolved': len(unfinished) - len(recently_active),
        'recent_event': bool(last_usage and now - last_usage < RECENT_EVENT_SECONDS),
    }
    return summary, intervals
