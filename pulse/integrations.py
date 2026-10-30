"""Read-only quota sources: Codex app-server, Claude account usage and the Claude statusline snapshot."""
import json
import mmap
import os
import re
import selectors
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import limits_window, timestamp

WINDOW_KEYS = ('primary', 'secondary')

# =============================================================================
# Codex
# =============================================================================

RPC_MAX_BUFFER = 4 * 1024 * 1024


def codex_binary():
    """Codex CLI to run app-server with: PULSE_CODEX_BIN, PATH, or the VS Code extension."""
    configured = os.environ.get('PULSE_CODEX_BIN')
    if configured == 'disabled':
        return None
    if configured:
        return configured
    executable = shutil.which('codex')
    if executable:
        return executable
    extensions = Path.home() / '.vscode-server/extensions'
    matches = list(extensions.glob('openai.chatgpt-*/bin/linux-x86_64/codex'))
    return str(max(matches, key=lambda path: path.stat().st_mtime)) if matches else None


class _AppServer:
    """JSON-RPC over stdio with a Codex app-server process, bounded by one deadline."""

    def __init__(self, binary, timeout):
        self.process = subprocess.Popen([binary, 'app-server', '--listen', 'stdio://'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b''
        self.deadline = time.monotonic() + timeout

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        self.process.stdin.flush()

    def receive(self, wanted_id):
        """Result of the response with `wanted_id`; notifications and noise are skipped."""
        while time.monotonic() < self.deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get('id') == wanted_id:
                    if 'error' in message:
                        raise RuntimeError('RPC error')
                    return message.get('result') or {}
            if not self.selector.select(max(0, self.deadline - time.monotonic())):
                break
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('RPC process ended')
            self.buffer += chunk
            if len(self.buffer) > RPC_MAX_BUFFER:
                raise RuntimeError('RPC response too large')
        raise TimeoutError('RPC timeout')

    def close(self):
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def codex_limits(binary, timeout=15):
    """Account quota windows from the official Codex app-server. No model calls are made."""
    server = _AppServer(binary, timeout)
    try:
        server.send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'pulse_dashboard', 'title': 'Pulse', 'version': '1.0.0'},
            'capabilities': {'experimentalApi': True},
        }})
        server.receive(1)
        server.send({'method': 'initialized'})
        server.send({'id': 2, 'method': 'account/rateLimits/read'})
        result = server.receive(2)
    finally:
        server.close()

    by_id = result.get('rateLimitsByLimitId') or {}
    raw = by_id.get('codex') or result.get('rateLimits') or {}
    windows = {key: limits_window(raw.get(key), 'usedPercent', 'windowDurationMins', 'resetsAt')
               for key in WINDOW_KEYS}
    if not any(windows.values()):
        raise RuntimeError('No account limits returned')
    return windows


# =============================================================================
# Claude: statusline snapshot (written by scripts/claude_statusline.py)
# =============================================================================

def read_claude_snapshot(path):
    with path.open() as stream:
        raw = json.load(stream)
    observed = timestamp(raw.get('observed_at'))
    if not observed or observed > time.time() + 60:
        raise ValueError('Invalid timestamp')
    return observed, {key: limits_window(raw.get(key)) for key in WINDOW_KEYS}


# =============================================================================
# Claude: account usage API
# =============================================================================

CLAUDE_API_BASE = 'https://api.anthropic.com'
CLAUDE_USAGE_PATH = '/api/oauth/usage'
CLAUDE_USAGE_URL = CLAUDE_API_BASE + CLAUDE_USAGE_PATH
# Only paths of this shape on CLAUDE_API_BASE may ever receive the token.
SAFE_PATH = re.compile(r'/api/oauth/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*')
# Strings that sit next to the usage address inside Claude Code.
USAGE_MARKERS = (b'fetchUtilization', b'five_hour', b'seven_day')
MAX_RESPONSE_BYTES = 1024 * 1024
# HTTP codes meaning the address itself is gone, not a temporary failure.
MOVED_STATUS_CODES = (404, 405, 410)


class ClaudeAuthError(RuntimeError):
    """The Claude Code login is missing or expired; Claude Code itself must renew it."""


class ClaudeEndpointError(RuntimeError):
    """The usage address no longer answers with quota windows: it may have moved."""


def claude_credentials():
    configured = os.environ.get('PULSE_CLAUDE_CREDENTIALS')
    if configured == 'disabled':
        return None
    return Path(configured) if configured else Path.home() / '.claude/.credentials.json'


def is_safe_usage_url(url):
    prefix = CLAUDE_API_BASE + '/'
    return url.startswith(prefix) and bool(SAFE_PATH.fullmatch(url[len(CLAUDE_API_BASE):]))


def read_access_token(credentials):
    """Token Claude Code keeps.

    Never refreshed here: refreshing rotates the login and could sign Claude Code out.
    """
    oauth = json.loads(credentials.read_text()).get('claudeAiOauth') or {}
    token = oauth.get('accessToken')
    expires_ms = oauth.get('expiresAt')
    if not isinstance(token, str) or not token:
        raise ClaudeAuthError('No login')
    if isinstance(expires_ms, (int, float)) and expires_ms / 1000 <= time.time() + 60:
        raise ClaudeAuthError('Login expired')
    return token


def claude_limits(credentials, url=CLAUDE_USAGE_URL, timeout=15, opener=urllib.request.urlopen):
    """5-hour and 7-day windows of the Claude subscription. No model calls are made."""
    if not is_safe_usage_url(url):
        raise ValueError('Refusing to send the Claude login outside the usage API')
    token = read_access_token(credentials)
    request = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token,
        'anthropic-beta': 'oauth-2025-04-20',
        'Accept': 'application/json',
        'User-Agent': 'pulse-dashboard/1.0',
    })
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise ClaudeAuthError('Login rejected') from None
        if error.code in MOVED_STATUS_CODES:
            raise ClaudeEndpointError(f'HTTP {error.code}') from None
        raise RuntimeError(f'HTTP {error.code}') from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError('Response too large')

    try:
        data = json.loads(body)
    except ValueError:
        raise ClaudeEndpointError('Not JSON') from None
    if not isinstance(data, dict):
        raise ClaudeEndpointError('Unexpected response')
    windows = {
        'primary': limits_window(data.get('five_hour'), 'utilization', default_minutes=300),
        'secondary': limits_window(data.get('seven_day'), 'utilization', default_minutes=10080),
    }
    if not any(windows.values()):
        raise ClaudeEndpointError('No account limits returned')
    return windows


# =============================================================================
# Claude: finding the usage address after it moves
# =============================================================================

def claude_programs():
    """Installed Claude Code executables, newest first: the usage address is compiled into them."""
    configured = os.environ.get('PULSE_CLAUDE_PROGRAM')
    found = [Path(configured)] if configured else []
    extensions = Path.home() / '.vscode-server/extensions'
    found += extensions.glob('anthropic.claude-code-*/resources/native-binary/claude')
    for candidate in (shutil.which('claude'), Path.home() / '.local/bin/claude'):
        if candidate:
            found.append(Path(candidate))

    modified = {}
    for program in found:
        try:
            real = program.resolve(strict=True)
            if real.is_file():
                modified[real] = real.stat().st_mtime
        except OSError:
            pass
    return sorted(modified, key=modified.get, reverse=True)


def usage_path_candidates(program, limit=3):
    """oauth paths that look like the usage address, nearest to its known neighbours first."""
    with program.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        anchors = [match.start() for marker in USAGE_MARKERS
                   for match in re.finditer(re.escape(marker), data)]
        distance = {}
        for match in re.finditer(rb'/api/oauth/[A-Za-z0-9_\-/]{1,80}', data):
            path = match.group().decode().rstrip('/')
            if not SAFE_PATH.fullmatch(path) or not re.search('usage|utiliz|limit|quota', path, re.I):
                continue
            nearest = min((abs(match.start() - anchor) for anchor in anchors), default=10**12)
            distance[path] = min(distance.get(path, nearest), nearest)
    return sorted(distance, key=lambda path: (distance[path], path))[:limit]


class ClaudeUsageEndpoint:
    """Current usage address; when it stops working, look for the new one in updated Claude Code.

    State is cached in a JSON file: the working `path`, when it was `found_at`,
    and which Claude Code version was already `scanned`.
    """

    def __init__(self, cache, programs=None):
        self.cache = cache
        self.programs = programs or claude_programs
        try:
            self.state = json.loads(cache.read_text())
        except (OSError, ValueError):
            self.state = {}
        if not isinstance(self.state, dict):
            self.state = {}
        if not SAFE_PATH.fullmatch(str(self.state.get('path', CLAUDE_USAGE_PATH))):
            self.state = {}  # never trust a cached address outside the usage API

    @property
    def path(self):
        return self.state.get('path', CLAUDE_USAGE_PATH)

    @property
    def url(self):
        return CLAUDE_API_BASE + self.path

    def limits(self, credentials, fetch=None):
        """Fetch windows from the current address, searching for a new one if it has moved."""
        fetch = fetch or (lambda url: claude_limits(credentials, url))
        try:
            return fetch(self.url)
        except ClaudeEndpointError:
            windows = self.discover(fetch)
            if windows is None:
                raise
            return windows

    def discover(self, fetch):
        """Try candidates from the newest Claude Code once per its version; return windows or None."""
        programs = self.programs()
        if not programs:
            return None
        program = programs[0]
        version = f'{program}:{program.stat().st_mtime_ns}'
        if self.state.get('scanned') == version:
            return None  # this Claude Code version was already searched
        self.state['scanned'] = version
        self._save()

        candidates = dict.fromkeys([CLAUDE_USAGE_PATH] + usage_path_candidates(program))
        for path in candidates:
            if path == self.path:
                continue
            try:
                windows = fetch(CLAUDE_API_BASE + path)
            except ClaudeEndpointError:
                continue
            self.state.update(path=path, found_at=time.time())
            self._save()
            return windows
        return None

    def _save(self):
        temp = self.cache.with_name(self.cache.name + '.tmp')
        temp.write_text(json.dumps(self.state))
        os.replace(temp, self.cache)
