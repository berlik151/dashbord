"""Claude subscription limits from the account API, and finding its address after it moves."""
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from pulse import collector as collector_module
from pulse.collector import Collector
from pulse.integrations import (CLAUDE_API_BASE, CLAUDE_USAGE_URL, ClaudeAuthError, ClaudeEndpointError,
                                ClaudeUsageEndpoint, claude_limits, usage_path_candidates)

TOKEN = 'sk-ant-oat-TEST_TOKEN_MARKER'
USAGE = {
    'five_hour': {'utilization': 42.5, 'resets_at': '2026-10-30T05:00:00Z'},
    'seven_day': {'utilization': 10, 'resets_at': '2026-11-02T00:00:00+00:00'},
    'seven_day_opus': None,
}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Opener:
    def __init__(self, body=USAGE, error=None):
        self.body = body
        self.error = error
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise urllib.error.HTTPError(request.full_url, self.error, 'x', {}, None)
        return Response(
            self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        )


class ClaudeLimitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / '.credentials.json'
        self.credentials(time.time() + 3600)

    def tearDown(self):
        self.temp.cleanup()

    def credentials(self, expires, token=TOKEN):
        self.path.write_text(
            json.dumps(
                {
                    'claudeAiOauth': {
                        'accessToken': token,
                        'refreshToken': 'REFRESH_MARKER',
                        'expiresAt': int(expires * 1000),
                    }
                }
            )
        )

    def test_parses_windows_and_sends_token_only_to_usage_endpoint(self):
        opener = Opener()
        windows = claude_limits(self.path, opener=opener)
        self.assertEqual(
            windows['primary'],
            {'used_percent': 42.5, 'window_minutes': 300, 'resets_at': 1793336400.0},
        )
        self.assertEqual(windows['secondary']['window_minutes'], 10080)
        self.assertEqual(windows['secondary']['used_percent'], 10)
        request = opener.requests[0]
        self.assertEqual(request.full_url, CLAUDE_USAGE_URL)
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
        self.assertNotIn(TOKEN, json.dumps(windows))

    def test_expired_login_is_not_used_or_refreshed(self):
        self.credentials(time.time() - 10)
        opener = Opener()
        with self.assertRaises(ClaudeAuthError):
            claude_limits(self.path, opener=opener)
        self.assertEqual(opener.requests, [])
        self.assertIn('REFRESH_MARKER', self.path.read_text())  # credentials file untouched

    def test_missing_token_and_file(self):
        self.credentials(time.time() + 3600, token='')
        with self.assertRaises(ClaudeAuthError):
            claude_limits(self.path, opener=Opener())
        with self.assertRaises(FileNotFoundError):
            claude_limits(Path(self.temp.name) / 'absent', opener=Opener())

    def test_rejected_login_and_server_errors(self):
        for code in (401, 403):
            with self.assertRaises(ClaudeAuthError):
                claude_limits(self.path, opener=Opener(error=code))
        with self.assertRaises(RuntimeError) as ctx:
            claude_limits(self.path, opener=Opener(error=500))
        self.assertNotIsInstance(ctx.exception, ClaudeAuthError)

    def test_invalid_or_empty_responses(self):
        for body in ({}, {'five_hour': {'utilization': None}}, {'five_hour': {'utilization': 150}}):
            with self.assertRaises(ClaudeEndpointError):
                claude_limits(self.path, opener=Opener(body))
        for body in (b'not json', b'[]'):
            with self.assertRaises(ClaudeEndpointError):
                claude_limits(self.path, opener=Opener(body))
        with self.assertRaises(RuntimeError):
            claude_limits(self.path, opener=Opener(b' ' * (1024 * 1024 + 5)))

    def test_moved_endpoint_errors(self):
        for code in (404, 405, 410):
            with self.assertRaises(ClaudeEndpointError):
                claude_limits(self.path, opener=Opener(error=code))

    def test_token_never_leaves_the_usage_api(self):
        for url in (
            'https://evil.example/api/oauth/usage',
            'https://api.anthropic.com.evil.example/api/oauth/usage',
            'http://api.anthropic.com/api/oauth/usage',
            'https://api.anthropic.com/v1/messages',
            'https://api.anthropic.com/api/oauth/../v1/messages',
            'https://api.anthropic.com/api/oauth/usage?x=1',
        ):
            opener = Opener()
            with self.subTest(url=url), self.assertRaises(ValueError):
                claude_limits(self.path, url=url, opener=opener)
            self.assertEqual(opener.requests, [])


WINDOWS = {
    'primary': {'used_percent': 1, 'window_minutes': 300, 'resets_at': None},
    'secondary': None,
}


# A fake Claude Code binary: unrelated oauth paths, a foreign host, and the usage
# address near `fetchUtilization`, as in the real program.
FAKE_PROGRAM = (
    b'\x00junk/api/oauth/profile\x00' + b'x' * 5000
    + b'/api/oauth/claude_cli/create_api_key\x00' + b'y' * 3000
    + b'https://evil.example/api/oauth/usage_v2\x00fetchUtilization: \x00five_hour\x00' + b'z' * 20000
    + b'/api/oauth/rate_limits\x00/api/oauth/usage\x00'
)


class EndpointDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.program = root / 'claude'
        self.program.write_bytes(FAKE_PROGRAM)
        self.cache = root / 'claude-endpoint.json'
        self.calls = []
        self.working = '/api/oauth/usage_v2'

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, url):
        self.calls.append(url)
        self.assertTrue(url.startswith(CLAUDE_API_BASE + '/api/oauth/'))
        if url == CLAUDE_API_BASE + self.working:
            return WINDOWS
        raise ClaudeEndpointError('gone')

    def endpoint(self):
        return ClaudeUsageEndpoint(self.cache, programs=lambda: [self.program])

    def test_candidates_are_usage_like_paths_nearest_to_markers(self):
        self.assertEqual(
            usage_path_candidates(self.program),
            ['/api/oauth/usage_v2', '/api/oauth/rate_limits', '/api/oauth/usage'],
        )

    def test_finds_saves_and_reuses_new_address(self):
        endpoint = self.endpoint()
        self.assertEqual(endpoint.limits(None, self.fetch), WINDOWS)
        self.assertEqual(self.calls, [CLAUDE_USAGE_URL, CLAUDE_API_BASE + '/api/oauth/usage_v2'])
        restored = self.endpoint()
        self.assertEqual(restored.path, '/api/oauth/usage_v2')
        self.calls.clear()
        self.assertEqual(restored.limits(None, self.fetch), WINDOWS)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('evil', self.cache.read_text())

    def test_searches_once_per_claude_code_version(self):
        self.working = '/api/oauth/nowhere'
        endpoint = self.endpoint()
        with self.assertRaises(ClaudeEndpointError):
            endpoint.limits(None, self.fetch)
        tried = len(self.calls)
        # The current address plus two other candidates; the default one is not retried.
        self.assertEqual(tried, 3)
        with self.assertRaises(ClaudeEndpointError):
            self.endpoint().limits(None, self.fetch)
        # Same Claude Code version: no new search, even after a restart.
        self.assertEqual(len(self.calls), tried + 1)
        self.working = '/api/oauth/rate_limits'
        time.sleep(0.01)
        self.program.write_bytes(FAKE_PROGRAM)
        st = self.program.stat()
        os.utime(self.program, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        self.assertEqual(self.endpoint().limits(None, self.fetch), WINDOWS)
        self.assertEqual(self.endpoint().path, '/api/oauth/rate_limits')

    def test_returns_to_default_address_when_it_works_again(self):
        self.cache.write_text(json.dumps({'path': '/api/oauth/old_moved'}))
        self.working = '/api/oauth/usage'
        endpoint = self.endpoint()
        self.assertEqual(endpoint.limits(None, self.fetch), WINDOWS)
        self.assertEqual(endpoint.path, '/api/oauth/usage')

    def test_bad_cache_and_no_program(self):
        for content in (
            '{broken',
            '[]',
            json.dumps({'path': 'https://evil.example/x'}),
            json.dumps({'path': '/v1/messages'}),
        ):
            self.cache.write_text(content)
            self.assertEqual(self.endpoint().url, CLAUDE_USAGE_URL)
        self.cache.unlink()
        endpoint = ClaudeUsageEndpoint(self.cache, programs=lambda: [])
        with self.assertRaises(ClaudeEndpointError):
            endpoint.limits(None, self.fetch)
        self.assertEqual(len(self.calls), 1)

    def test_other_errors_do_not_trigger_search(self):

        def offline(url):
            self.calls.append(url)
            raise urllib.error.URLError('down')
        with self.assertRaises(urllib.error.URLError):
            self.endpoint().limits(None, offline)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.cache.exists())


class CollectorClaudeLimitsTests(unittest.TestCase):
    def run_once(self, limits):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            roots = {'codex': data / 'none', 'claude': data / 'none'}
            c = Collector(data, roots, 'UTC', interval=5)
            with (
                mock.patch.object(collector_module, 'codex_binary', return_value=None),
                mock.patch.object(
                    collector_module,
                    'claude_credentials',
                    return_value=data / 'creds.json',
                ),
                mock.patch.object(collector_module, 'claude_limits', side_effect=limits),
                mock.patch('pulse.integrations.claude_programs', return_value=[]),
            ):
                c.start()
                self.assertTrue(c.ready.wait(10))
                c.stop.set()
                c.thread.join(10)
            report = json.loads(c.get(7)[0])['providers']['claude']
            self.assertNotIn(TOKEN, json.dumps(report))
            return report

    def test_successful_poll_is_saved_and_reported(self):
        p = self.run_once(
            lambda *args: {
                'primary': {
                    'used_percent': 30,
                    'window_minutes': 300,
                    'resets_at': time.time() + 3600,
                },
                'secondary': None,
            }
        )
        self.assertEqual(p['integration']['status'], 'ok')
        self.assertEqual(p['limits']['source'], 'oauth_usage')
        self.assertEqual(p['limits']['primary']['remaining_percent'], 70)
        self.assertFalse(p['limits']['stale'])

    def test_failures_are_reported_without_limits(self):

        def expired(*args):
            raise ClaudeAuthError('x')

        def offline(*args):
            raise urllib.error.URLError('down')

        def missing(*args):
            raise FileNotFoundError

        def moved(*args):
            raise ClaudeEndpointError('x')
        for side, status in (
            (moved, 'endpoint_changed'),
            (expired, 'auth_expired'),
            (offline, 'unavailable'),
            (missing, 'no_credentials'),
        ):
            p = self.run_once(side)
            self.assertEqual(p['integration']['status'], status)
            self.assertIsNone(p['limits'])


if __name__ == '__main__':
    unittest.main()
