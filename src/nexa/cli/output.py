from __future__ import annotations

import json
from typing import TextIO

from .errors import OutputMode


def emit_success(
    payload: object,
    *,
    mode: OutputMode,
    human_title: str | None = None,
    stream: TextIO | None = None,
) -> None:
    import sys

    stream = stream or sys.stdout
    if mode is OutputMode.JSON:
        stream.write(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        )
        return
    if human_title:
        stream.write(f"{human_title}\n")
    if isinstance(payload, dict):
        for key in sorted(payload):
            stream.write(f"{key}: {payload[key]}\n")
    else:
        stream.write(f"{payload}\n")
