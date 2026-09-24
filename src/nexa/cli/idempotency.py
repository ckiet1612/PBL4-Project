from __future__ import annotations

import secrets
import string


def new_idempotency_key() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))
