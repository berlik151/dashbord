#!/usr/bin/env python3
"""Pulse: read-only localhost dashboard with a persistent background collector."""
import argparse
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pulse.collector import Collector
from pulse.systemd import notify

ROOT = Path(__file__).resolve().parent
PERIODS = (1, 7, 14, 30)
# The only files the server ever reads from disk.
STATIC_FILES = {
    '/': ('index.html', 'text/html'),
    '/app.js': ('app.js', 'text/javascript'),
    '/style.css': ('style.css', 'text/css'),
}
SECURITY_HEADERS = {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY',
    'Content-Security-Policy': "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                               "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
}
JSON = 'application/json; charset=utf-8'


def make_handler(collector):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path == '/api/usage':
                response = self.usage(url.query)
            elif url.path == '/healthz':
                response = self.health()
            elif url.path in STATIC_FILES:
                name, mime = STATIC_FILES[url.path]
                response = (200, (ROOT / 'static' / name).read_bytes(), mime + '; charset=utf-8')
            else:
                self.send_error(404)
                return
            if response:  # None: an error was already sent
                self.reply(*response)

        def usage(self, query):
            try:
                days = int(parse_qs(query).get('days', ['7'])[0])
            except ValueError:
                days = 0
            if days not in PERIODS:
                self.send_error(400, 'Choose days=1,7,14,30')
                return None
            data, failed = collector.get(days)
            if data is None:
                return 503, b'{"error":"collecting"}', JSON
            if failed:
                # A failed poll must not look like a successful fresh response.
                report = json.loads(data)
                report['collector_error'] = True
                data = json.dumps(report).encode()
            return 200, data, JSON

        def health(self):
            data, failed = collector.get(7)
            healthy = data is not None and not failed
            if healthy:
                age = time.time() - json.loads(data)['generated_at']
                healthy = age < max(90, collector.interval * 4)
            return (200 if healthy else 503), json.dumps({'ok': healthy}).encode(), 'application/json'

        def reply(self, status, data, mime):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt, *args):
            pass  # request lines are not logged

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8765')))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    os.umask(0o077)

    data = Path(os.environ.get('PULSE_DATA_DIR', str(ROOT / 'data'))).expanduser()
    data.mkdir(parents=True, exist_ok=True)
    timezone = os.environ.get('PULSE_TIMEZONE', 'Asia/Kamchatka')
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        parser.error('Unknown PULSE_TIMEZONE')
    interval = max(5, int(os.environ.get('PULSE_INTERVAL', '15')))
    roots = {
        'codex': Path(os.environ.get('PULSE_CODEX_SESSIONS', str(Path.home() / '.codex/sessions'))),
        'claude': Path(os.environ.get('PULSE_CLAUDE_PROJECTS', str(Path.home() / '.claude/projects'))),
    }
    collector = Collector(data, roots, timezone, interval)

    # Loopback is intentional: remote access is through SSH or an authenticated reverse proxy.
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(collector))
    server.daemon_threads = True
    collector.start()
    notify('READY=1')
    logging.info('Pulse http://127.0.0.1:%s', args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        collector.stop.set()
        collector.thread.join(timeout=20)


if __name__ == '__main__':
    main()
