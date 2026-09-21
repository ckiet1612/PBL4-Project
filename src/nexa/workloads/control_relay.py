"""Bounded byte relay from Docker exec stdio to the runner control socket."""

import argparse
import os
import socket
import sys
import threading
import time
from contextlib import suppress

_CHUNK_BYTES = 64 * 1024


def relay(socket_path: str, *, connect_timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + connect_timeout_seconds
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        while True:
            try:
                connection.connect(socket_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("trusted runner control socket was not ready") from None
                time.sleep(0.02)

        def forward_input() -> None:
            while True:
                payload = os.read(sys.stdin.fileno(), _CHUNK_BYTES)
                if not payload:
                    with suppress(OSError):
                        connection.shutdown(socket.SHUT_WR)
                    return
                connection.sendall(payload)

        input_thread = threading.Thread(target=forward_input, daemon=True)
        input_thread.start()
        while True:
            payload = connection.recv(_CHUNK_BYTES)
            if not payload:
                break
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
        input_thread.join(timeout=1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Nexa trusted-runner control relay")
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    try:
        relay(args.socket)
    except (OSError, TimeoutError):
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
