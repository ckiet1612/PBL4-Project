import secrets
import threading
import time
import uuid

_RANDOM_BITS = 74
_RANDOM_MASK = (1 << _RANDOM_BITS) - 1
_RAND_B_MASK = (1 << 62) - 1
_MAX_UNIX_MS = (1 << 48) - 1

_state_lock = threading.Lock()
_last_unix_ms = -1
_last_random = -1


def new_uuid7() -> uuid.UUID:
    """Return a process-monotonic RFC 9562 UUIDv7 using Python 3.12 APIs."""
    global _last_random, _last_unix_ms

    unix_ms = time.time_ns() // 1_000_000
    with _state_lock:
        if unix_ms > _last_unix_ms:
            random_bits = secrets.randbits(_RANDOM_BITS)
        else:
            unix_ms = _last_unix_ms
            random_bits = (_last_random + 1) & _RANDOM_MASK
            if random_bits == 0:
                unix_ms += 1
                random_bits = secrets.randbits(_RANDOM_BITS)
        if unix_ms > _MAX_UNIX_MS:
            raise OverflowError("Unix millisecond timestamp does not fit UUIDv7")
        _last_unix_ms = unix_ms
        _last_random = random_bits

    rand_a = random_bits >> 62
    rand_b = random_bits & _RAND_B_MASK
    value = (unix_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)
