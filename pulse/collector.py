"""Background collector: imports logs, polls quota sources and prepares reports."""
import json
import logging
import threading
import time

from .integrations import (CLAUDE_USAGE_PATH, ClaudeAuthError, ClaudeEndpointError, ClaudeUsageEndpoint,
                           claude_credentials, claude_limits, codex_binary, codex_limits, read_claude_snapshot)
from .metrics import HostMetrics
from .report import build_report
from .store import Store
from .systemd import notify

PERIODS = (1, 7, 14, 30)
# Quota APIs are polled less often than logs are scanned.
LIMITS_POLL_SECONDS = 300


class Collector:
    def __init__(self, data_dir, roots, timezone, interval=15):
        self.data_dir = data_dir
        self.roots = roots
        self.timezone = timezone
        self.interval = interval
        self.lock = threading.Lock()
        self.snapshots = {}
        self.failed = False
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name='pulse-collector', daemon=True)
        self.thread.start()

    def get(self, days):
        """Latest encoded report for the period and whether the last cycle failed."""
        with self.lock:
            return self.snapshots.get(days), self.failed

    def heartbeat(self):
        # systemd restarts the service when these pings stop (WatchdogSec in the unit).
        notify('WATCHDOG=1')

    def run(self):
        db = None
        try:
            db = Store(self.data_dir / 'pulse.sqlite3')
            self._prepare()
            while not self.stop.is_set():
                try:
                    self._cycle(db)
                except Exception as exc:
                    # Never log a record, path or provider response: these may contain private data.
                    logging.error('Collector failed (%s)', type(exc).__name__)
                    self._set_failed()
                self.stop.wait(self.interval)
        except Exception as exc:
            logging.error('Collector startup failed (%s)', type(exc).__name__)
            self._set_failed()
        finally:
            if db:
                db.close()

    def _prepare(self):
        self.host = HostMetrics()
        self.codex = codex_binary()
        self.credentials = claude_credentials()
        self.endpoint = ClaudeUsageEndpoint(self.data_dir / 'claude-endpoint.json')
        self.integrations = {}
        self.claude_api = None
        self.last_codex_poll = 0
        self.last_claude_poll = 0

    def _cycle(self, db):
        statuses = {name: db.scan(name, root, progress=self.heartbeat) for name, root in self.roots.items()}
        self._poll_codex(db)
        self._poll_claude(db)
        statusline = self._read_statusline(db)
        # The account API is the automatic source; the statusline remains a fallback.
        api = self.claude_api
        use_api = api and (api['status'] == 'ok' or statusline['status'] != 'ok')
        self.integrations['claude'] = api if use_api else statusline

        sample = self.host.read(self.data_dir)
        now = time.time()
        reports = {}
        for days in PERIODS:
            report = build_report(db, statuses, self.integrations, sample, now, days, self.timezone)
            reports[days] = json.dumps(report, ensure_ascii=False).encode()
        with self.lock:
            self.snapshots = reports
            self.failed = False
        self.ready.set()
        self.heartbeat()

    def _poll_codex(self, db):
        if not self.codex:
            self.integrations['codex'] = {'status': 'not_configured'}
            return
        if time.time() - self.last_codex_poll < LIMITS_POLL_SECONDS:
            return
        self.last_codex_poll = time.time()
        try:
            windows = codex_limits(self.codex)
            with db.db:
                db.save_limits('codex', time.time(), 'app_server', windows)
            status = 'ok'
        except (OSError, RuntimeError, TimeoutError, ValueError):
            status = 'unavailable'
        self.integrations['codex'] = {'status': status, 'checked_at': time.time()}

    def _poll_claude(self, db):
        if not self.credentials or time.time() - self.last_claude_poll < LIMITS_POLL_SECONDS:
            return
        self.last_claude_poll = time.time()
        credentials = self.credentials
        try:
            windows = self.endpoint.limits(credentials, lambda url: claude_limits(credentials, url))
            with db.db:
                db.save_limits('claude', time.time(), 'oauth_usage', windows)
            status = 'ok'
        except FileNotFoundError:
            status = 'no_credentials'
        except ClaudeAuthError:
            status = 'auth_expired'
        except ClaudeEndpointError:
            status = 'endpoint_changed'
        except (OSError, RuntimeError, TimeoutError, ValueError, TypeError, AttributeError):
            status = 'unavailable'
        self.claude_api = {'status': status, 'checked_at': time.time()}
        if status == 'ok' and self.endpoint.path != CLAUDE_USAGE_PATH:
            self.claude_api.update(endpoint=self.endpoint.path, found_at=self.endpoint.state.get('found_at'))

    def _read_statusline(self, db):
        try:
            observed, windows = read_claude_snapshot(self.data_dir / 'claude-limits.json')
        except FileNotFoundError:
            return {'status': 'awaiting_statusline'}
        except (OSError, ValueError, TypeError, AttributeError):
            return {'status': 'invalid_snapshot'}
        with db.db:
            db.save_limits('claude', observed, 'statusline', windows)
        return {'status': 'ok' if any(windows.values()) else 'no_limits', 'checked_at': observed}

    def _set_failed(self):
        with self.lock:
            self.failed = True
