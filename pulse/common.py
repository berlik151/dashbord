"""Small validators shared by log parsing and quota sources."""
import hashlib
import math
from datetime import datetime, timezone

# Year 3000: anything later is a broken value, not a real time.
MAX_TIMESTAMP = 32503680000


def timestamp(value):
    """Unix time from a number or an ISO 8601 string (UTC if no zone); None if invalid."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result = float(value)
        else:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result = parsed.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None
    return result if math.isfinite(result) and 0 < result < MAX_TIMESTAMP else None


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def number(value):
    """A non-negative counter, or 0 for anything else."""
    return value if is_number(value) and value >= 0 else 0


def identity(*parts):
    """Stable hashed identifier: raw IDs and paths are never stored."""
    return hashlib.sha256('\0'.join(str(part) for part in parts).encode()).hexdigest()


def limits_window(value, percent='used_percent', minutes='window_minutes', reset='resets_at',
                  default_minutes=None):
    """Normalise one quota window from any source; None if the percentage is not 0..100."""
    if not isinstance(value, dict):
        return None
    used = value.get(percent)
    if not is_number(used) or not 0 <= used <= 100:
        return None
    return {
        'used_percent': used,
        'window_minutes': number(value.get(minutes)) or default_minutes,
        'resets_at': timestamp(value.get(reset)),
    }
