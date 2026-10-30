"""Components: Codex app-server, statusline snapshot and scripts, host metrics, systemd, collector."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pulse import collector as collector_module
from pulse.collector import Collector
from pulse.integrations import codex_binary, codex_limits, read_claude_snapshot
from pulse.metrics import HostMetrics

ROOT = Path(__file__).resolve().parents[1]
# Stand-in for `codex app-server`: answers the two RPC calls Pulse makes, mode set by env.
FAKE_CODEX = '''#!/usr/bin/env python3
import json, os, sys

mode = os.environ.get('FAKE_CODEX_MODE', 'ok')
if mode == 'exit':
    sys.exit(0)


def send(message):
    sys.stdout.write(json.dumps(message) + '\\n')
    sys.stdout.flush()


for line in sys.stdin:
    request = json.loads(line)
    if mode == 'silent':
        continue
    if request.get('id') == 1:
        sys.stdout.write('not json\\n')
        send({'method': 'notification'})
        send({'id': 1, 'result': {}})
    elif request.get('id') == 2:
        if mode == 'error':
            send({'id': 2, 'error': {'message': 'denied'}})
        elif mode == 'empty':
            send({'id': 2, 'result': {}})
        else:
            send({'id': 2, 'result': {'rateLimitsByLimitId': {'codex': {
                'primary': {'usedPercent': 40, 'windowDurationMins': 300, 'resetsAt': 1800000000},
                'secondary': {'usedPercent': 5, 'windowDurationMins': 10080, 'resetsAt': 1800500000},
            }}}})
'''


class CodexAppServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.binary = Path(self.temp.name) / 'codex'
        self.binary.write_text(FAKE_CODEX)
        self.binary.chmod(stat.S_IRWXU)

    def tearDown(self):
        self.temp.cleanup()

    def run_mode(self, mode, timeout=10):
        with mock.patch.dict(os.environ, {'FAKE_CODEX_MODE': mode}):
            return codex_limits(str(self.binary), timeout=timeout)

    def test_reads_limits_and_skips_noise(self):
        windows = self.run_mode('ok')
        self.assertEqual(
            windows['primary'],
            {'used_percent': 40, 'window_minutes': 300, 'resets_at': 1800000000.0},
        )
        self.assertEqual(windows['secondary']['window_minutes'], 10080)

    def test_errors(self):
        for mode, error in (
            ('error', RuntimeError),
            ('empty', RuntimeError),
            ('exit', RuntimeError),
        ):
            with self.subTest(mode=mode), self.assertRaises(error):
                self.run_mode(mode)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.run_mode('silent', timeout=1)
        self.assertLess(time.monotonic() - started, 6)

    def test_binary_selection(self):
        with mock.patch.dict(os.environ, {'PULSE_CODEX_BIN': 'disabled'}):
            self.assertIsNone(codex_binary())
        with mock.patch.dict(os.environ, {'PULSE_CODEX_BIN': '/opt/codex'}):
            self.assertEqual(codex_binary(), '/opt/codex')


class ClaudeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'claude-limits.json'

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_future_and_broken_snapshots(self):
        now = time.time()
        self.path.write_text(
            json.dumps(
                {
                    'observed_at': now,
                    'primary': {'used_percent': 30, 'window_minutes': 300, 'resets_at': now + 60},
                    'secondary': None,
                }
            )
        )
        observed, windows = read_claude_snapshot(self.path)
        self.assertEqual(observed, now)
        self.assertEqual(windows['primary']['used_percent'], 30)
        self.path.write_text(json.dumps({'observed_at': now + 3600}))
        with self.assertRaises(ValueError):
            read_claude_snapshot(self.path)
        self.path.write_text('{broken')
        with self.assertRaises(ValueError):
            read_claude_snapshot(self.path)


class HostMetricsTests(unittest.TestCase):
    def test_two_samples_give_bounded_values(self):
        host = HostMetrics()
        first = host.read(Path('/'))
        # CPU load needs two samples.
        self.assertIsNone(first['cpu_percent'])
        sum(range(200000))
        time.sleep(0.05)
        second = host.read(Path('/'))
        if second['cpu_percent'] is not None:
            self.assertTrue(0 <= second['cpu_percent'] <= 100)
        for key in ('memory', 'disk'):
            self.assertTrue(0 <= second[key]['percent'] <= 100)
            self.assertLessEqual(second[key]['used'], second[key]['total'])
        self.assertGreater(second['uptime_seconds'], 0)

    def test_missing_disk_path(self):
        self.assertIsNone(HostMetrics().read(Path('/definitely/absent'))['disk'])


class StatuslineScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / 'data' / 'claude-limits.json'

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, stdin):
        return subprocess.run(
            [sys.executable, str(ROOT / 'scripts/claude_statusline.py'), '--output', str(self.out)],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_writes_only_quota_and_prints_line(self):
        payload = {
            'session_id': 'SECRET_SESSION',
            'transcript_path': '/private/path',
            'rate_limits': {
                'five_hour': {'used_percentage': 25, 'resets_at': 1800000000},
                'seven_day': {'used_percentage': 60, 'resets_at': 1800500000},
            },
        }
        result = self.run_script(json.dumps(payload))
        self.assertEqual(result.stdout.strip(), 'Claude · 5h: 75% осталось · 7d: 40% осталось')
        saved = self.out.read_text()
        self.assertNotIn('SECRET_SESSION', saved)
        self.assertNotIn('/private/path', saved)
        data = json.loads(saved)
        self.assertEqual(data['secondary']['window_minutes'], 10080)
        # No temporary files are left behind.
        self.assertEqual([p.name for p in self.out.parent.iterdir()], ['claude-limits.json'])

    def test_without_limits_and_invalid_input(self):
        self.assertEqual(self.run_script('{}').stdout.strip(), 'Claude · Pulse подключён')
        self.out.unlink()
        result = self.run_script('not json')
        self.assertEqual(result.stdout.strip(), 'Claude · Pulse: нет данных')
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.out.exists())


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / '.claude').mkdir()
        self.settings = self.home / '.claude/settings.json'

    def tearDown(self):
        self.temp.cleanup()

    def install(self):
        return subprocess.run(
            [sys.executable, str(ROOT / 'scripts/install_claude_statusline.py')],
            env={**os.environ, 'HOME': str(self.home)},
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_preserves_settings_backs_up_and_is_idempotent(self):
        self.settings.write_text(
            json.dumps({'model': 'opus', 'permissions': {'allow': ['Bash(ls)']}})
        )
        self.assertEqual(self.install().returncode, 0)
        data = json.loads(self.settings.read_text())
        self.assertEqual(data['model'], 'opus')
        self.assertEqual(data['permissions'], {'allow': ['Bash(ls)']})
        self.assertIn('claude_statusline.py', data['statusLine']['command'])
        self.assertEqual(len(list(self.settings.parent.glob('settings.pulse-backup-*.json'))), 1)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(json.loads(self.settings.read_text()), data)
        self.assertEqual(len(list(self.settings.parent.glob('settings.pulse-backup-*.json'))), 1)

    def test_refuses_to_replace_foreign_statusline(self):
        original = {'statusLine': {'type': 'command', 'command': 'my-own-line'}}
        self.settings.write_text(json.dumps(original))
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(json.loads(self.settings.read_text()), original)

    def test_creates_settings_when_missing(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertIn('statusLine', json.loads(self.settings.read_text()))


class CollectorCodexTests(unittest.TestCase):
    def run_once(self, limits):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            c = Collector(
                data,
                {'codex': data / 'none', 'claude': data / 'none'},
                'UTC',
                interval=5,
            )
            with (
                mock.patch.object(collector_module, 'codex_binary', return_value='/fake/codex'),
                mock.patch.object(collector_module, 'codex_limits', side_effect=limits),
                mock.patch.object(collector_module, 'claude_credentials', return_value=None),
            ):
                c.start()
                self.assertTrue(c.ready.wait(10))
                c.stop.set()
                c.thread.join(10)
            report = json.loads(c.get(7)[0])
            self.assertFalse(c.get(7)[1])
            self.assertEqual(
                sorted((json.loads(c.get(d)[0])['period']['days'] for d in (1, 7, 14, 30))),
                [1, 7, 14, 30],
            )
            return report

    def test_codex_success_and_failure(self):
        r = self.run_once(
            lambda binary: {
                'primary': {
                    'used_percent': 10,
                    'window_minutes': 300,
                    'resets_at': time.time() + 60,
                },
                'secondary': None,
            }
        )
        self.assertEqual(r['providers']['codex']['integration']['status'], 'ok')
        self.assertEqual(r['providers']['codex']['limits']['source'], 'app_server')
        self.assertEqual(r['providers']['claude']['integration']['status'], 'awaiting_statusline')
        self.assertEqual(r['providers']['codex']['source']['status'], 'missing')

        def broken(binary):
            raise TimeoutError
        r = self.run_once(broken)
        self.assertEqual(r['providers']['codex']['integration']['status'], 'unavailable')
        self.assertIsNone(r['providers']['codex']['limits'])


if __name__ == '__main__':
    unittest.main()


class SystemdNotifyTests(unittest.TestCase):
    def test_ready_and_watchdog_messages(self):
        import socket
        from pulse.systemd import notify
        with tempfile.TemporaryDirectory() as temp:
            address = str(Path(temp) / 'notify')
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(address)
            try:
                with mock.patch.dict(os.environ, {'NOTIFY_SOCKET': address}):
                    self.assertTrue(notify('READY=1'))
                    self.assertTrue(notify('WATCHDOG=1'))
                self.assertEqual(sock.recv(64), b'READY=1')
                self.assertEqual(sock.recv(64), b'WATCHDOG=1')
            finally:
                sock.close()
            with mock.patch.dict(os.environ, {'NOTIFY_SOCKET': address}):
                self.assertFalse(notify('WATCHDOG=1'))  # receiver gone: no crash
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(notify('READY=1'))

    def test_collector_pings_during_import_and_after_each_cycle(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            (data / 'logs').mkdir()
            for i in range(3):
                (data / 'logs' / f'{i}.jsonl').write_text('{}\n')
            c = Collector(
                data,
                {'codex': data / 'logs', 'claude': data / 'none'},
                'UTC',
                interval=5,
            )
            pings = []
            with (
                mock.patch.object(collector_module, 'notify', side_effect=pings.append),
                mock.patch.object(collector_module, 'codex_binary', return_value=None),
                mock.patch.object(collector_module, 'claude_credentials', return_value=None),
            ):
                c.start()
                self.assertTrue(c.ready.wait(10))
                c.stop.set()
                c.thread.join(10)
            # One ping per log file plus one per finished cycle.
            self.assertEqual(pings, ['WATCHDOG=1'] * 4)
