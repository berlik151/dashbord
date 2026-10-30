"""Incremental JSONL import into SQLite.

Only token counters, hashed identifiers and timestamps are stored: message
text, prompts, paths and keys never reach the database.
"""
import json
import os
import sqlite3
import time
from pathlib import Path

from .common import identity, limits_window, number, timestamp

SCHEMA = '''
CREATE TABLE IF NOT EXISTS usage (
    provider TEXT NOT NULL, id TEXT NOT NULL, session TEXT NOT NULL, ts REAL NOT NULL,
    input INTEGER NOT NULL, output INTEGER NOT NULL,
    cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL,
    PRIMARY KEY (provider, id));
CREATE INDEX IF NOT EXISTS usage_time ON usage(ts);
CREATE TABLE IF NOT EXISTS turns (
    provider TEXT NOT NULL, id TEXT NOT NULL, session TEXT NOT NULL,
    start REAL, end REAL, last REAL, status TEXT NOT NULL,
    PRIMARY KEY (provider, id));
CREATE TABLE IF NOT EXISTS cursors (
    id TEXT PRIMARY KEY, inode TEXT, position INTEGER, guard TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS limits (
    provider TEXT PRIMARY KEY, observed REAL, source TEXT, windows TEXT);
CREATE TABLE IF NOT EXISTS source_health (
    provider TEXT PRIMARY KEY, last_success REAL, skipped INTEGER);
'''
SCHEMA_VERSION = 1

# Bytes before the cursor that must be unchanged for an incremental read.
GUARD_BYTES = 256

SAVE_LIMITS_SQL = '''
INSERT INTO limits VALUES (?, ?, ?, ?)
ON CONFLICT (provider) DO UPDATE SET
    observed = excluded.observed, source = excluded.source, windows = excluded.windows
WHERE excluded.observed >= limits.observed'''

# Multiple Claude content blocks can repeat or update the same message's usage.
MERGE_CLAUDE_USAGE_SQL = '''
INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (provider, id) DO UPDATE SET
    input = MAX(usage.input, excluded.input),
    output = MAX(usage.output, excluded.output),
    cache_read = MAX(usage.cache_read, excluded.cache_read),
    cache_write = MAX(usage.cache_write, excluded.cache_write),
    ts = MIN(usage.ts, excluded.ts)'''

UPSERT_CODEX_TURN_SQL = '''
INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (provider, id) DO UPDATE SET
    start = COALESCE(turns.start, excluded.start),
    end = COALESCE(excluded.end, turns.end),
    last = MAX(turns.last, excluded.last),
    status = CASE WHEN turns.end IS NOT NULL AND excluded.end IS NULL
                  THEN turns.status ELSE excluded.status END'''

UPSERT_CLAUDE_TURN_SQL = '''
INSERT INTO turns VALUES (?, ?, ?, ?, NULL, ?, 'open')
ON CONFLICT (provider, id) DO UPDATE SET
    start = MIN(turns.start, excluded.start),
    last = MAX(turns.last, excluded.last)'''

TOUCH_TURN_SQL = 'UPDATE turns SET last = MAX(last, ?) WHERE provider = ? AND id = ?'

ABORT_TURN_SQL = '''
UPDATE turns SET end = last, status = 'aborted'
WHERE provider = ? AND id = ? AND end IS NULL'''

COMPLETE_TURN_SQL = '''
UPDATE turns SET end = MAX(COALESCE(end, ?), ?), last = MAX(last, ?), status = 'completed'
WHERE provider = ? AND id = ?'''

CODEX_TURN_EVENTS = {'task_started': 'open', 'task_complete': 'completed', 'turn_aborted': 'aborted'}
CODEX_COUNTERS = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_input_tokens')
CLAUDE_FINAL_STOP_REASONS = ('end_turn', 'stop_sequence', 'max_tokens', 'refusal')


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        # Version 1 adds Claude turns. Re-reading is idempotent for usage, so
        # reset cursors to rebuild turns from logs that were already imported.
        with self.db:
            self.db.execute('DELETE FROM cursors')
            self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def close(self):
        self.db.close()

    def save_limits(self, provider, observed, source, windows):
        """Keep the newest snapshot of quota windows; older ones are ignored."""
        if not observed or not any(windows.values()):
            return
        self.db.execute(SAVE_LIMITS_SQL, (provider, observed, source, json.dumps(windows)))

    def add_usage(self, provider, key, session, ts, counts):
        if not ts:
            return
        row = (provider, key, session, ts, *counts)
        if provider == 'codex':
            # A copied tail may have no prior counter baseline. Never replace an
            # already imported delta with the full cumulative counter in that tail.
            self.db.execute('INSERT OR IGNORE INTO usage VALUES (?, ?, ?, ?, ?, ?, ?, ?)', row)
        else:
            self.db.execute(MERGE_CLAUDE_USAGE_SQL, row)

    # --- Parsing log records -------------------------------------------------

    def parse(self, provider, row, state, fallback):
        """Import one log record. Returns False when the record is unusable.

        `state` is persisted with the file cursor: current session, open turn
        and, for Codex, the previous cumulative counters.
        """
        if not isinstance(row, dict):
            return False
        ts = timestamp(row.get('timestamp'))
        kind = row.get('type')
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}

        if provider == 'codex' and kind == 'session_meta':
            state['session'] = identity(payload.get('id') or payload.get('session_id') or fallback)
        session = state.get('session') or identity(row.get('sessionId') or fallback)
        state['session'] = session

        if provider == 'codex' and kind == 'event_msg':
            return self._parse_codex_event(payload, ts, session, state)
        if provider == 'claude' and kind == 'user' and is_claude_prompt(row):
            return self._parse_claude_prompt(row, ts, session, state)
        if provider == 'claude' and kind == 'assistant':
            return self._parse_claude_reply(row, ts, session, state)
        return True

    def _parse_codex_event(self, payload, ts, session, state):
        event = payload.get('type')
        if event == 'token_count':
            return self._parse_codex_token_count(payload, ts, session, state)
        if event in CODEX_TURN_EVENTS:
            return self._parse_codex_turn(event, payload, ts, session, state)
        return True

    def _parse_codex_token_count(self, payload, ts, session, state):
        limits = payload.get('rate_limits') or {}
        if isinstance(limits, dict) and limits.get('limit_id', 'codex') in (None, 'codex'):
            windows = {key: limits_window(limits.get(key)) for key in ('primary', 'secondary')}
            self.save_limits('codex', ts, 'session_log', windows)

        info = payload.get('info') or {}
        total = info.get('total_token_usage') if isinstance(info, dict) else None
        if isinstance(total, dict):
            current = [int(number(total.get(key))) for key in CODEX_COUNTERS]
            previous = state.get('previous', [0] * 4)
            # A counter reset starts a new segment; the first value is usage, not zero.
            reset = current[0] < previous[0] or current[1] < previous[1]
            delta = current if reset else [max(0, now - before) for now, before in zip(current, previous)]
            state['previous'] = current
            if any(delta):
                if not ts:
                    return False
                key = identity(session, ts, json.dumps(current))
                self.add_usage('codex', key, session, ts, delta)

        if ts and state.get('turn'):
            self.db.execute(TOUCH_TURN_SQL, (ts, 'codex', state['turn']))
        return True

    def _parse_codex_turn(self, event, payload, ts, session, state):
        raw_id = payload.get('turn_id') or payload.get('root_turn_id')
        turn = identity(session, raw_id) if raw_id else state.get('turn')
        if not turn or not ts:
            return False
        started = event == 'task_started'
        start = timestamp(payload.get('started_at')) or (ts if started else None)
        end = timestamp(payload.get('completed_at')) or (None if started else ts)
        self.db.execute(UPSERT_CODEX_TURN_SQL,
                        ('codex', turn, session, start, end, ts, CODEX_TURN_EVENTS[event]))
        state['turn'] = turn if started else None
        return True

    def _parse_claude_prompt(self, row, ts, session, state):
        """A human prompt starts a turn; tool results inside the turn do not."""
        message = row.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list) and any(
                isinstance(item, dict) and item.get('type') == 'tool_result' for item in content):
            return True
        if not ts:
            return False
        turn = identity(session, row['promptId'])
        if state.get('turn') and state['turn'] != turn:
            # A new prompt while the previous turn never reached end_turn: interrupted.
            self.db.execute(ABORT_TURN_SQL, ('claude', state['turn']))
        self.db.execute(UPSERT_CLAUDE_TURN_SQL, ('claude', turn, session, ts, ts))
        state['turn'] = turn
        return True

    def _parse_claude_reply(self, row, ts, session, state):
        message = row.get('message') or {}
        if ts and state.get('turn') and isinstance(message, dict) and not row.get('isSidechain'):
            if message.get('stop_reason') in CLAUDE_FINAL_STOP_REASONS:
                self.db.execute(COMPLETE_TURN_SQL, (ts, ts, ts, 'claude', state['turn']))
            else:
                self.db.execute(TOUCH_TURN_SQL, (ts, 'claude', state['turn']))

        usage = message.get('usage') if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            return True
        message_id = message.get('id') or row.get('uuid')
        if not ts or not message_id:
            return False
        read = int(number(usage.get('cache_read_input_tokens')))
        write = int(number(usage.get('cache_creation_input_tokens')))
        total_input = int(number(usage.get('input_tokens'))) + read + write
        output = int(number(usage.get('output_tokens')))
        self.add_usage('claude', identity(message_id), session, ts, [total_input, output, read, write])
        return True

    # --- Reading files -------------------------------------------------------

    def ingest_file(self, provider, file):
        """Read new complete lines since the saved cursor. Returns the number of skipped records."""
        key = identity(provider, str(file))
        cursor = self.db.execute('SELECT * FROM cursors WHERE id = ?', (key,)).fetchone()
        warnings = 0
        with file.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            inode = f'{stat.st_dev}:{stat.st_ino}'
            position = cursor['position'] if cursor else 0

            # Continue only if this is the same file and the bytes before the cursor are unchanged.
            valid = cursor and cursor['inode'] == inode and stat.st_size >= position
            if valid:
                valid = read_guard(stream, position) == cursor['guard']
            if valid:
                state = json.loads(cursor['state'])
            else:
                position, state = 0, {}

            stream.seek(position)
            # Read only complete lines; leave a writer's unfinished last line for the next scan.
            while stream.tell() < stat.st_size:
                line = stream.readline()
                if not line.endswith(b'\n'):
                    break
                position = stream.tell()
                try:
                    if not self.parse(provider, json.loads(line), state, key):
                        warnings += 1
                except (ValueError, TypeError, AttributeError, OverflowError):
                    warnings += 1
            guard = read_guard(stream, position)

        self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?, ?, ?, ?, ?)',
                        (key, inode, position, guard, json.dumps(state)))
        return warnings

    def scan(self, provider, root, progress=None):
        """Import every *.jsonl under root and record the source health."""
        health = self.db.execute('SELECT * FROM source_health WHERE provider = ?', (provider,)).fetchone()
        status = {
            'status': 'ok', 'files': 0, 'read_errors': 0, 'invalid_records': 0,
            'scanned_at': time.time(),
            'last_success': health['last_success'] if health else None,
            'skipped_total': health['skipped'] if health else 0,
        }
        try:
            if not root.is_dir():
                status['status'] = 'missing'
                return status

            def on_walk_error(error):
                status['read_errors'] += 1

            for folder, _, files in os.walk(root, onerror=on_walk_error):
                for name in sorted(files):
                    path = Path(folder) / name
                    if not name.endswith('.jsonl') or path.is_symlink():
                        continue
                    status['files'] += 1
                    if progress:
                        progress()  # a long first import must not look like a hang
                    try:
                        with self.db:
                            status['invalid_records'] += self.ingest_file(provider, path)
                    except OSError:
                        status['read_errors'] += 1
            if status['read_errors'] or status['invalid_records']:
                status['status'] = 'partial'
        except OSError:
            status['status'] = 'error'
            status['read_errors'] += 1

        if status['status'] == 'ok':
            status['last_success'] = status['scanned_at']
        status['skipped_total'] += status['invalid_records']
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO source_health VALUES (?, ?, ?)',
                            (provider, status['last_success'], status['skipped_total']))
        return status


def is_claude_prompt(row):
    """A top-level human prompt (not a subagent, not an injected meta record)."""
    return bool(row.get('promptId')) and not row.get('isSidechain') and not row.get('isMeta')


def read_guard(stream, position):
    """Hash of the bytes right before `position`, used to detect rewritten files."""
    stream.seek(max(0, position - GUARD_BYTES))
    return identity(stream.read(min(position, GUARD_BYTES)))
