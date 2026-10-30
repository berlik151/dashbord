"""Log import (pulse/store.py) and reports built from it (pulse/report.py)."""
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path

from pulse.common import limits_window
from pulse.report import build_report, union_seconds
from pulse.store import Store
from scripts.claude_statusline import snapshot

T = '2026-10-29T12:00:00Z'
NO_STATUS = {'codex': {}, 'claude': {}}


def utc(hour, minute=0, day=29):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=dt.timezone.utc).timestamp()


def codex(i, o=10, t=T):
    return {
        'type': 'event_msg',
        'timestamp': t,
        'payload': {
            'type': 'token_count',
            'info': {
                'total_token_usage': {
                    'input_tokens': i,
                    'output_tokens': o,
                    'cached_input_tokens': i // 2,
                }
            },
        },
    }


def at(m, s=0):
    return f'2026-10-29T12:{m:02}:{s:02}Z'


def prompt(pid, t, **extra):
    return {
        'type': 'user',
        'sessionId': 'c1',
        'promptId': pid,
        'timestamp': t,
        'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'PRIVATE_PROMPT'}]},
        **extra,
    }


def tool_result(pid, t):
    return {
        'type': 'user',
        'sessionId': 'c1',
        'promptId': pid,
        'timestamp': t,
        'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'x'}]},
    }


def reply(ident, stop, t, **extra):
    return {
        'type': 'assistant',
        'sessionId': 'c1',
        'timestamp': t,
        'message': {
            'id': ident,
            'stop_reason': stop,
            'usage': {'input_tokens': 1, 'output_tokens': 1},
        },
        **extra,
    }


def claude(i, o, ident='msg1', t=T):
    return {
        'type': 'assistant',
        'sessionId': 'c1',
        'timestamp': t,
        'message': {
            'id': ident,
            'usage': {
                'input_tokens': i,
                'output_tokens': o,
                'cache_read_input_tokens': 20,
                'cache_creation_input_tokens': 30,
            },
        },
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dbpath = self.root / 'db.sqlite'
        self.store = Store(self.dbpath)
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.file = self.logs / 'session.jsonl'

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def write(self, rows, mode='w'):
        with self.file.open(mode) as f:
            for r in rows:
                f.write(json.dumps(r) + '\n')

    def totals(self):
        query = 'SELECT SUM(input), SUM(output), SUM(cache_read), SUM(cache_write), COUNT(*) FROM usage'
        return tuple(self.store.db.execute(query).fetchone())

    def count(self, table):
        return self.store.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def report(self, now, days=1, timezone='UTC'):
        return build_report(self.store, NO_STATUS, {}, {}, now, days, timezone)

    def scan_and_report(self, now, days=1):
        self.store.scan('claude', self.logs)
        return self.report(now, days)

    def activity(self, now, provider='claude'):
        return self.report(now)['providers'][provider]['activity']

    def test_incremental_restart_reset_and_duplicate_copy(self):
        meta = {'type': 'session_meta', 'payload': {'id': 'stable-session'}}
        self.write([meta, codex(100), codex(150, 20, '2026-10-29T12:01:00Z')])
        self.store.scan('codex', self.logs)
        self.assertEqual(self.totals()[:2], (150, 20))
        self.store.close()
        self.store = Store(self.dbpath)
        self.store.scan('codex', self.logs)
        self.assertEqual(self.totals()[:2], (150, 20))
        self.write([codex(20, 3, '2026-10-29T12:02:00Z')], 'a')
        self.store.scan('codex', self.logs)
        self.assertEqual(self.totals()[:2], (170, 23))
        (self.logs / 'copy.jsonl').write_bytes(self.file.read_bytes())
        self.store.scan('codex', self.logs)
        self.assertEqual(self.totals()[:2], (170, 23))

    def test_copied_codex_tail_does_not_replace_delta(self):
        meta = {'type': 'session_meta', 'payload': {'id': 'stable-session'}}
        tail = codex(150, 20, '2026-10-29T12:01:00Z')
        self.write([meta, codex(100), tail])
        self.store.scan('codex', self.logs)
        (self.logs / 'tail.jsonl').write_text(json.dumps(meta) + '\n' + json.dumps(tail) + '\n')
        self.store.scan('codex', self.logs)
        self.assertEqual(self.totals()[:2], (150, 20))

    def test_claude_streaming_and_copies_deduplicated(self):
        self.write([claude(2, 5), claude(2, 15), claude(2, 10)])
        self.store.scan('claude', self.logs)
        self.assertEqual(self.totals(), (52, 15, 20, 30, 1))
        (self.logs / 'copy.jsonl').write_bytes(self.file.read_bytes())
        self.store.scan('claude', self.logs)
        self.assertEqual(self.totals(), (52, 15, 20, 30, 1))

    def test_partial_line_retained_then_imported(self):
        text = json.dumps(claude(2, 7))
        self.file.write_text(text[:-2])
        self.store.scan('claude', self.logs)
        self.assertEqual(self.totals()[-1], 0)
        with self.file.open('a') as f:
            f.write(text[-2:] + '\n')
        self.store.scan('claude', self.logs)
        self.assertEqual(self.totals()[-1], 1)

    def test_rewrite_truncation_and_removed_logs_keep_history(self):
        self.write([claude(1, 2)])
        self.store.scan('claude', self.logs)
        self.write([claude(1, 2), claude(1, 3, 'msg2')])
        self.store.scan('claude', self.logs)
        self.assertEqual(self.totals()[-1], 2)
        self.write([claude(1, 4, 'msg3')])
        self.store.scan('claude', self.logs)
        self.file.unlink()
        self.store.scan('claude', self.logs)
        self.assertEqual(self.totals()[-1], 3)

    def test_malformed_records_do_not_stop_scan(self):
        self.file.write_text('invalid\n[]\n')
        self.write([claude(2, 7)], 'a')
        status = self.store.scan('claude', self.logs)
        self.assertEqual(status['status'], 'partial')
        self.assertEqual(status['invalid_records'], 2)
        self.assertEqual(self.totals()[-1], 1)

    def test_private_content_not_persisted(self):
        r = claude(2, 7)
        r['message']['content'] = [{'text': 'PRIVATE_TEXT_MARKER'}]
        r['cwd'] = '/private-project'
        self.write([r])
        self.store.scan('claude', self.logs)
        dump = ''.join(self.store.db.iterdump())
        self.assertNotIn('PRIVATE_TEXT_MARKER', dump)
        self.assertNotIn('/private-project', dump)

    def test_time_window_timezone_and_expired_quota(self):
        self.write(
            [claude(2, 7, t='2026-10-29T14:00:00Z'), claude(4, 9, 'msg2', t='2026-10-29T10:00:00Z')]
        )
        self.store.scan('claude', self.logs)
        now = utc(15)
        self.store.save_limits(
            'codex',
            now - 1000,
            'session_log',
            {'primary': {'used_percent': 20, 'resets_at': now - 1, 'window_minutes': 300}},
        )
        report = self.report(now, timezone='Asia/Kamchatka')
        self.assertEqual(report['history'][0]['date'], '2026-10-30')
        self.assertEqual(report['providers']['claude']['total'], 59)
        w = report['providers']['codex']['limits']
        self.assertTrue(w['stale'])
        self.assertIsNone(w['primary']['remaining_percent'])

    def test_completed_turns_union_and_incomplete_turns(self):
        def event(kind, id, t):
            return {
                'type': 'event_msg',
                'timestamp': f'2026-10-29T12:{t:02}:00Z',
                'payload': {'type': kind, 'turn_id': id},
            }

        self.write(
            [
                event('task_started', 'a', 0),
                event('task_started', 'b', 1),
                event('task_complete', 'a', 3),
                event('task_complete', 'b', 4),
                event('task_started', 'c', 5),
            ]
        )
        self.store.scan('codex', self.logs)
        p = self.activity(utc(12, 10), 'codex')
        self.assertEqual(p['completed_seconds'], 240)
        self.assertEqual(p['unresolved'], 1)

    def test_claude_completed_turn_with_tools(self):
        self.write(
            [
                prompt('p1', at(0)),
                reply('m1', 'tool_use', at(0, 5)),
                tool_result('p1', at(0, 20)),
                reply('m2', 'end_turn', at(1)),
                reply('m2', 'end_turn', at(1, 30)),
            ]
        )
        r = self.scan_and_report(utc(12, 10))
        a = r['providers']['claude']['activity']
        self.assertTrue(a['tracked'])
        self.assertEqual(a['completed_seconds'], 90)
        self.assertEqual(a['completed_turns'], 1)
        self.assertEqual(a['unresolved'], 0)
        self.assertEqual(r['providers']['claude']['total'], 4)

    def test_claude_interrupted_turn_closed_by_next_prompt(self):
        self.write(
            [
                prompt('p1', at(0)),
                reply('m1', 'tool_use', at(0, 30)),
                {
                    'type': 'user',
                    'sessionId': 'c1',
                    'promptId': 'p1',
                    'timestamp': at(0, 40),
                    'message': {
                        'content': [{'type': 'text', 'text': '[Request interrupted by user]'}]
                    },
                },
                prompt('p2', at(5)),
                reply('m2', 'end_turn', at(6)),
            ]
        )
        a = self.scan_and_report(utc(12, 10))['providers']['claude']['activity']
        self.assertEqual(a['completed_seconds'], 100)
        self.assertEqual(a['completed_turns'], 2)
        self.assertEqual(a['unresolved'], 0)
        rows = self.store.db.execute("SELECT status FROM turns WHERE provider = 'claude' ORDER BY start")
        statuses = [row[0] for row in rows]
        self.assertEqual(statuses, ['aborted', 'completed'])

    def test_claude_open_turn_recent_then_unresolved(self):
        self.write([prompt('p1', at(0)), reply('m1', 'tool_use', at(0, 30))])
        self.store.scan('claude', self.logs)
        a = self.activity(utc(12, 1))
        self.assertEqual((a['open_recent'], a['unresolved'], a['completed_seconds']), (1, 0, 0))
        a = self.activity(utc(13))
        self.assertEqual((a['open_recent'], a['unresolved']), (0, 1))

    def test_claude_sidechain_meta_and_tool_results_do_not_start_turns(self):
        self.write(
            [
                prompt('p1', at(0)),
                prompt('sub', at(0, 10), isSidechain=True),
                prompt('meta', at(0, 11), isMeta=True),
                tool_result('p1', at(0, 20)),
                reply('s1', 'end_turn', at(0, 30), isSidechain=True),
                reply('m1', 'end_turn', at(2)),
            ]
        )
        a = self.scan_and_report(utc(12, 10))['providers']['claude']['activity']
        self.assertEqual((a['completed_turns'], a['completed_seconds']), (1, 120))
        self.assertEqual(self.count('turns'), 1)
        self.assertEqual(self.totals()[-1], 2)  # sidechain tokens are still usage

    def test_claude_turns_reimport_is_idempotent_and_private(self):
        self.write([prompt('p1', at(0)), reply('m1', 'end_turn', at(1))])
        self.store.scan('claude', self.logs)
        (self.logs / 'copy.jsonl').write_bytes(self.file.read_bytes())
        self.store.scan('claude', self.logs)
        self.assertEqual(self.count('turns'), 1)
        self.assertEqual(self.totals()[-1], 1)
        self.assertNotIn('PRIVATE_PROMPT', ''.join(self.store.db.iterdump()))

    def test_migration_rebuilds_claude_turns_from_imported_logs(self):
        self.write([prompt('p1', at(0)), reply('m1', 'end_turn', at(1))])
        self.store.scan('claude', self.logs)
        with self.store.db:
            self.store.db.execute('DELETE FROM turns')
            self.store.db.execute('PRAGMA user_version=0')
        self.store.close()
        self.store = Store(self.dbpath)
        self.assertEqual(self.count('cursors'), 0)
        self.store.scan('claude', self.logs)
        self.assertEqual(self.store.db.execute('SELECT status FROM turns').fetchone()[0], 'completed')
        self.assertEqual(self.totals()[-1], 1)
        self.store.close()
        self.store = Store(self.dbpath)
        self.assertEqual(self.count('cursors'), 1)

    def test_combined_activity_counts_parallel_agents_once(self):
        def event(kind, id, m):
            return {
                'type': 'event_msg',
                'timestamp': at(m),
                'payload': {'type': kind, 'turn_id': id},
            }

        self.write([event('task_started', 'a', 0), event('task_complete', 'a', 4)])
        self.store.scan('codex', self.logs)
        self.file.unlink()
        self.write([prompt('p1', at(2)), reply('m1', 'end_turn', at(6))])
        r = self.scan_and_report(utc(12, 10))
        self.assertEqual(r['providers']['codex']['activity']['completed_seconds'], 240)
        self.assertEqual(r['providers']['claude']['activity']['completed_seconds'], 240)
        self.assertEqual(r['activity'], {'tracked': True, 'completed_seconds': 360})

    def test_old_unfinished_turns_leave_the_period(self):
        self.write([prompt('p1', at(0)), reply('m1', 'tool_use', at(0, 30))])
        self.store.scan('claude', self.logs)
        self.assertEqual(self.activity(utc(13))['unresolved'], 1)
        self.assertEqual(self.activity(utc(13, day=30))['unresolved'], 0)

    def test_history_sessions_and_totals_by_day(self):
        rows = [
            claude(10, 1, 'a', t='2026-10-28T12:00:00Z'),
            claude(20, 2, 'b', t='2026-10-29T12:00:00Z'),
        ]
        self.write(rows[:1])
        (self.logs / 'second.jsonl').write_text(json.dumps({**rows[1], 'sessionId': 'c2'}) + '\n')
        self.store.scan('claude', self.logs)
        now = utc(13)
        r = self.report(now, days=7)
        self.assertEqual(len(r['history']), 7)
        self.assertEqual(r['history'][-1], {'date': '2026-10-29', 'codex': 0, 'claude': 72})
        self.assertEqual(r['history'][-2]['claude'], 61)
        c = r['providers']['claude']
        self.assertEqual(
            (c['sessions'], c['total'], c['input'], c['cache_read'], c['cache_write']),
            (2, 133, 130, 40, 60),
        )
        self.assertEqual(sum(day['claude'] for day in r['history']), c['total'])
        today = self.report(now)['providers']['claude']
        self.assertEqual((today['sessions'], today['total'], today['records']), (1, 72, 2))

    def test_fresh_limits_newer_snapshot_wins(self):
        now = time.time()
        self.store.save_limits(
            'claude',
            now - 100,
            'oauth_usage',
            {
                'primary': {'used_percent': 30, 'resets_at': now + 60, 'window_minutes': 300},
                'secondary': None,
            },
        )
        self.store.save_limits(
            'claude',
            now - 500,
            'statusline',
            {
                'primary': {'used_percent': 90, 'resets_at': now + 60, 'window_minutes': 300},
                'secondary': None,
            },
        )
        self.store.save_limits('claude', now, 'oauth_usage', {'primary': None, 'secondary': None})
        w = self.report(now)['providers']['claude']['limits']
        self.assertEqual(
            (w['source'], w['stale'], w['primary']['remaining_percent']),
            ('oauth_usage', False, 70),
        )
        self.assertIsNone(w['secondary'])

    def test_missing_directory(self):
        self.assertEqual(self.store.scan('codex', self.root / 'absent')['status'], 'missing')


class IntegrationTests(unittest.TestCase):
    def test_statusline_allowlist(self):
        data = snapshot(
            {
                'secret': 'never-store',
                'rate_limits': {'five_hour': {'used_percentage': 40, 'resets_at': 1800000000}},
            }
        )
        self.assertEqual(data['primary']['used_percent'], 40)
        self.assertIsNone(data['secondary'])
        self.assertNotIn('never-store', json.dumps(data))

    def test_invalid_quota(self):
        for v in (
            None,
            {},
            {'used_percent': -1},
            {'used_percent': 101},
            {'used_percent': float('nan')},
            {'used_percent': True},
        ):
            self.assertIsNone(limits_window(v))

    def test_union(self):
        self.assertEqual(union_seconds([(1, 5), (2, 3), (4, 8), (10, 12)]), 9)


if __name__ == '__main__':
    unittest.main()
