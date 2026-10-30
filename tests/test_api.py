"""HTTP handler: allowed files, headers, period validation and health."""
import io
import json
import time
import unittest

from server import make_handler


class Transport:
    def __init__(self, path):
        self.input = io.BytesIO(f'GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())
        self.output = b''

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, value):
        self.output += value


class Collector:
    interval = 15

    def __init__(self, data=b'{"generated_at":1}', failed=False):
        self.data = data
        self.failed = failed

    def get(self, days):
        return (self.data, self.failed)


class APITests(unittest.TestCase):
    def request(self, path, collector=None):
        transport = Transport(path)
        make_handler(collector or Collector())(transport, ('127.0.0.1', 1), None)
        headers, body = transport.output.split(b'\r\n\r\n', 1)
        return (headers, body)

    def test_no_arbitrary_file_serving(self):
        for path in ('/server.py', '/../server.py', '/data/pulse.sqlite3', '/api/usage?days=999'):
            headers, _ = self.request(path)
            self.assertRegex(headers.split(b'\r\n')[0], b' (400|404) ')

    def test_json_headers_and_failed_collection(self):
        headers, body = self.request('/api/usage?days=7', Collector(failed=True))
        self.assertIn(b'Cache-Control: no-store', headers)
        self.assertIn(b'X-Content-Type-Options: nosniff', headers)
        self.assertTrue(json.loads(body)['collector_error'])

    def test_startup_health_is_not_success(self):
        for path in ('/healthz', '/api/usage'):
            headers, _ = self.request(path, Collector(None))
            self.assertIn(b'503', headers)

    def test_static_files_and_security_headers(self):
        for path, mime in (
            ('/', 'text/html'),
            ('/app.js', 'text/javascript'),
            ('/style.css', 'text/css'),
        ):
            headers, body = self.request(path)
            self.assertIn(b' 200 ', headers.split(b'\r\n')[0])
            self.assertIn(f'Content-Type: {mime}'.encode(), headers)
            self.assertTrue(body)
            self.assertIn(b"frame-ancestors 'none'", headers)
            self.assertIn(b'X-Frame-Options: DENY', headers)

    def test_period_validation(self):
        # An empty value falls back to the default period.
        for days in ('1', '7', '14', '30', ''):
            headers, _ = self.request(f'/api/usage?days={days}')
            self.assertIn(b' 200 ', headers.split(b'\r\n')[0])
        for days in ('0', 'abc', '7.5', '-7', '2'):
            headers, body = self.request(f'/api/usage?days={days}')
            self.assertIn(b' 400 ', headers.split(b'\r\n')[0])
            self.assertNotIn(b'HTTP/1.', body)  # exactly one response

    def test_fresh_health(self):
        headers, body = self.request(
            '/healthz',
            Collector(json.dumps({'generated_at': time.time()}).encode()),
        )
        self.assertIn(b' 200 ', headers.split(b'\r\n')[0])
        self.assertEqual(json.loads(body), {'ok': True})
        headers, _ = self.request(
            '/healthz',
            Collector(json.dumps({'generated_at': time.time()}).encode(), failed=True),
        )
        self.assertIn(b'503', headers)

    def test_stale_health(self):
        headers, _ = self.request('/healthz')
        self.assertIn(b'503', headers)


if __name__ == '__main__':
    unittest.main()
