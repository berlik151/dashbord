#!/usr/bin/env python3
"""Claude Code calls this with statusline JSON on stdin. Only quota windows are kept."""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pulse.common import limits_window  # noqa: E402

MAX_INPUT = 2 * 1024 * 1024


def snapshot(raw):
    rate = raw.get('rate_limits') or {}
    return {
        'observed_at': time.time(),
        'primary': limits_window(rate.get('five_hour'), percent='used_percentage', default_minutes=300),
        'secondary': limits_window(rate.get('seven_day'), percent='used_percentage', default_minutes=10080),
    }


def write_atomically(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temp = stream.name
        json.dump(data, stream)
    try:
        os.replace(temp, path)
    except OSError:
        os.unlink(temp)
        raise


def status_text(data):
    parts = [f"{title}: {100 - data[key]['used_percent']:.0f}% осталось"
             for key, title in (('primary', '5h'), ('secondary', '7d')) if data[key]]
    return 'Claude · ' + (' · '.join(parts) if parts else 'Pulse подключён')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        data = snapshot(json.loads(sys.stdin.read(MAX_INPUT)))
        write_atomically(args.output, data)
        print(status_text(data))
    except (OSError, ValueError, TypeError, AttributeError):
        print('Claude · Pulse: нет данных')


if __name__ == '__main__':
    main()
