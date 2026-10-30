"""Whole dashboard: real collector and HTTP server over fixture logs of both agents."""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from pulse import collector as collector_module
from pulse.collector import Collector
from server import make_handler

NOW = time.time()


def iso(offset):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(NOW + offset))


def codex_log(path):

    def count(i, o, t):
        return {
            'type': 'event_msg',
            'timestamp': iso(t),
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

    def task(kind, t):
        return {
            'type': 'event_msg',
            'timestamp': iso(t),
            'payload': {'type': kind, 'turn_id': 't1'},
        }
    rows = [
        {'type': 'session_meta', 'timestamp': iso(-600), 'payload': {'id': 'codex-session'}},
        task('task_started', -600),
        count(1000, 100, -590),
        count(3000, 300, -500),
        task('task_complete', -480),
    ]
    path.write_text(''.join((json.dumps(r) + '\n' for r in rows)))


def claude_log(path):

    def user(t, **c):
        return {
            'type': 'user',
            'sessionId': 'claude-session',
            'promptId': 'p1',
            'timestamp': iso(t),
            'message': {'content': [{'type': 'text', 'text': 'PRIVATE_PROMPT_MARKER'}]},
            **c,
        }

    def reply(ident, stop, t):
        return {
            'type': 'assistant',
            'sessionId': 'claude-session',
            'timestamp': iso(t),
            'message': {
                'id': ident,
                'stop_reason': stop,
                'content': [{'type': 'text', 'text': 'PRIVATE_ANSWER_MARKER'}],
                'usage': {
                    'input_tokens': 10,
                    'output_tokens': 40,
                    'cache_read_input_tokens': 500,
                    'cache_creation_input_tokens': 50,
                },
            },
        }
    rows = [user(-300), reply('m1', 'tool_use', -290), reply('m2', 'end_turn', -240)]
    path.write_text(''.join((json.dumps(r) + '\n' for r in rows)))


class DashboardEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        (root / 'codex/2026').mkdir(parents=True)
        (root / 'claude/project').mkdir(parents=True)
        (root / 'data').mkdir()
        codex_log(root / 'codex/2026/rollout.jsonl')
        claude_log(root / 'claude/project/session.jsonl')
        windows = {
            'primary': {'used_percent': 5, 'window_minutes': 300, 'resets_at': NOW + 3600},
            'secondary': {'used_percent': 2, 'window_minutes': 10080, 'resets_at': NOW + 86400},
        }
        cls.patches = [
            mock.patch.object(collector_module, 'codex_binary', return_value=None),
            mock.patch.object(
                collector_module,
                'claude_credentials',
                return_value=root / 'creds.json',
            ),
            mock.patch.object(collector_module, 'claude_limits', return_value=windows),
        ]
        for p in cls.patches:
            p.start()
        cls.collector = Collector(
            root / 'data',
            {'codex': root / 'codex', 'claude': root / 'claude'},
            'UTC',
            interval=5,
        )
        cls.collector.start()
        assert cls.collector.ready.wait(15)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(cls.collector))
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.collector.stop.set()
        cls.collector.thread.join(15)
        for p in cls.patches:
            p.stop()
        cls.temp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=10) as r:
            return (r.status, r.read())

    def test_usage_for_both_agents(self):
        status, body = self.get('/api/usage?days=1')
        self.assertEqual(status, 200)
        d = json.loads(body)
        codex, claude = (d['providers']['codex'], d['providers']['claude'])
        self.assertEqual(
            (codex['total'], codex['input'], codex['output'], codex['sessions']),
            (3300, 3000, 300, 1),
        )
        self.assertEqual(
            (
                claude['total'],
                claude['input'],
                claude['output'],
                claude['cache_read'],
                claude['cache_write'],
            ),
            (1200, 1120, 80, 1000, 100),
        )
        self.assertEqual(sum((x['codex'] + x['claude'] for x in d['history'])), 4500)
        self.assertEqual(
            (codex['activity']['completed_seconds'], claude['activity']['completed_seconds']),
            (120, 60),
        )
        self.assertEqual(d['activity'], {'tracked': True, 'completed_seconds': 180})
        self.assertEqual(claude['integration']['status'], 'ok')
        self.assertEqual(claude['limits']['source'], 'oauth_usage')
        self.assertEqual(
            (
                claude['limits']['primary']['remaining_percent'],
                claude['limits']['secondary']['remaining_percent'],
            ),
            (95, 98),
        )
        self.assertEqual(codex['integration']['status'], 'not_configured')
        self.assertIsNone(codex['limits'])
        self.assertEqual((codex['source']['status'], claude['source']['status']), ('ok', 'ok'))
        self.assertIn('memory', d['host'])

    def test_no_private_content_in_api_or_database(self):
        for days in (1, 7, 14, 30):
            _, body = self.get(f'/api/usage?days={days}')
            for marker in (
                b'PRIVATE_PROMPT_MARKER',
                b'PRIVATE_ANSWER_MARKER',
                b'claude-session',
                b'codex-session',
                str(self.root).encode(),
            ):
                self.assertNotIn(marker, body)
        data = self.root / 'data'
        dump = (data / 'pulse.sqlite3').read_bytes() + (data / 'pulse.sqlite3-wal').read_bytes()
        self.assertNotIn(b'PRIVATE_PROMPT_MARKER', dump)
        self.assertNotIn(b'PRIVATE_ANSWER_MARKER', dump)

    def test_page_health_and_unknown_paths(self):
        status, body = self.get('/')
        self.assertEqual(status, 200)
        self.assertIn('ВРЕМЯ ЗАДАЧ АГЕНТОВ', body.decode())
        self.assertEqual(json.loads(self.get('/healthz')[1]), {'ok': True})
        for path in ('/data/pulse.sqlite3', '/../server.py', '/api/usage?days=2'):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get(path)
            self.assertIn(ctx.exception.code, (400, 404))


if __name__ == '__main__':
    unittest.main()
