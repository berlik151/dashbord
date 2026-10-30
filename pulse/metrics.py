"""Host metrics from /proc: CPU load between samples, memory, disk and uptime."""
import os
import shutil
import time
from pathlib import Path


class HostMetrics:
    def __init__(self):
        # CPU load is a difference between two /proc/stat samples.
        self.previous = None
        self.previous_time = None

    def read(self, disk_path):
        now = time.time()
        return {
            'sampled_at': now,
            'cpu_count': os.cpu_count(),
            **self._cpu(now),
            'memory': memory(),
            'disk': disk(disk_path),
            'uptime_seconds': uptime(),
        }

    def _cpu(self, now):
        result = {'cpu_percent': None, 'cpu_sample_seconds': None}
        try:
            values = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
        except (OSError, ValueError, IndexError):
            return result
        total = sum(values)
        idle = values[3] + values[4]  # idle + iowait
        if self.previous:
            previous_total, previous_idle = self.previous
            elapsed = total - previous_total
            if elapsed > 0:
                busy = 100 * (1 - (idle - previous_idle) / elapsed)
                result['cpu_percent'] = round(max(0, min(100, busy)), 1)
                result['cpu_sample_seconds'] = round(now - self.previous_time, 1)
        self.previous = (total, idle)
        self.previous_time = now
        return result


def memory():
    try:
        info = {line.split(':')[0]: int(line.split()[1]) * 1024
                for line in Path('/proc/meminfo').read_text().splitlines()}
        total = info['MemTotal']
        used = total - info['MemAvailable']
    except (OSError, ValueError, KeyError):
        return None
    return {'total': total, 'used': used, 'percent': round(used / total * 100, 1)}


def disk(path):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {'total': usage.total, 'used': usage.used, 'free': usage.free,
            'percent': round(usage.used / usage.total * 100, 1)}


def uptime():
    try:
        return float(Path('/proc/uptime').read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
