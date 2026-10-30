#!/usr/bin/env python3
"""Install only our statusline; preserve all settings and refuse to replace another command."""
import json
import os
import shlex
import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    settings = Path.home() / '.claude/settings.json'
    data = json.loads(settings.read_text()) if settings.exists() else {}
    script = shlex.quote(str(ROOT / 'scripts/claude_statusline.py'))
    output = shlex.quote(str(ROOT / 'data/claude-limits.json'))
    command = f'/usr/bin/python3 {script} --output {output}'

    existing = data.get('statusLine')
    if existing and existing.get('command') != command:
        raise SystemExit('An existing status line is configured; leave it unchanged and integrate manually.')
    if not existing:
        if settings.exists():
            shutil.copy2(settings, settings.with_name(f'settings.pulse-backup-{time.time_ns()}.json'))
        data['statusLine'] = {'type': 'command', 'command': command}
        with tempfile.NamedTemporaryFile(mode='w', dir=settings.parent, delete=False) as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            temp = stream.name
        os.replace(temp, settings)
    print('Pulse status line configured; other settings preserved.')


if __name__ == '__main__':
    main()
