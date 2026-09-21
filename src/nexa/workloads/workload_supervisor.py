"""UID-separated workload process-group supervisor for the trusted runner."""

import argparse
import ctypes
import math
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import suppress


def _set_parent_death_signal() -> None:
    if sys.platform != "linux":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGKILL)


def _signal_group(process: subprocess.Popen[bytes], signal_number: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal_number)


def _group_exists(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_group(process: subprocess.Popen[bytes], *, grace_seconds: float | None) -> int:
    if process.poll() is not None and not _group_exists(process):
        return int(process.returncode)
    if grace_seconds is not None:
        _signal_group(process, signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while _group_exists(process) and time.monotonic() < deadline:
            process.poll()
            time.sleep(0.01)
    if _group_exists(process):
        _signal_group(process, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    return int(process.returncode if process.returncode is not None else -1)


def _term_grace(instruction: str) -> float | None:
    if instruction == "TERM":
        return 5.0
    prefix = "TERM "
    if not instruction.startswith(prefix):
        return None
    try:
        grace = float(instruction.removeprefix(prefix))
    except ValueError:
        return None
    if not math.isfinite(grace) or not 0 <= grace <= 5:
        return None
    return grace


def supervise(control_fd: int, command: tuple[str, ...]) -> int:
    control = os.fdopen(control_fd, "rb", buffering=0)
    workload: subprocess.Popen[bytes] | None = None
    buffer = bytearray()
    try:
        while True:
            if workload is not None and workload.poll() is not None:
                return _stop_group(workload, grace_seconds=None)
            readable, _, _ = select.select([control], [], [], 0.05)
            if not readable:
                continue
            chunk = control.read(4096)
            if not chunk:
                if workload is not None:
                    return _stop_group(workload, grace_seconds=None)
                return 143
            buffer.extend(chunk)
            while b"\n" in buffer:
                raw, _, remainder = buffer.partition(b"\n")
                buffer[:] = remainder
                instruction = raw.decode("ascii", "strict")
                if instruction == "START":
                    if workload is not None:
                        continue
                    workload = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        close_fds=True,
                        start_new_session=True,
                        preexec_fn=_set_parent_death_signal,
                    )
                elif (grace := _term_grace(instruction)) is not None:
                    if workload is None:
                        return 143
                    return _stop_group(workload, grace_seconds=grace)
                elif instruction == "KILL":
                    if workload is None:
                        return 137
                    return _stop_group(workload, grace_seconds=None)
                else:
                    if workload is not None:
                        _stop_group(workload, grace_seconds=None)
                    return 78
    finally:
        control.close()


def supervise_socket(
    registration_socket: str,
    command: tuple[str, ...],
    *,
    log_limit_bytes: int,
    connect_timeout_seconds: float = 30,
) -> int:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + connect_timeout_seconds
    while True:
        try:
            connection.connect(registration_socket)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() >= deadline:
                connection.close()
                return 124
            time.sleep(0.01)
    workload: subprocess.Popen[bytes] | None = None
    buffer = bytearray()
    log_bytes = 0
    log_lock = threading.Lock()
    log_overflow = threading.Event()
    log_overflow_reported = False

    def drain(stream) -> None:  # type: ignore[no-untyped-def]
        nonlocal log_bytes
        while True:
            chunk = os.read(stream.fileno(), 64 * 1024)
            if not chunk:
                return
            with log_lock:
                log_bytes += len(chunk)
                if log_bytes > log_limit_bytes:
                    log_overflow.set()

    try:
        connection.sendall(b"READY\n")
        while True:
            if log_overflow.is_set() and not log_overflow_reported:
                connection.sendall(b"LOG_OVERFLOW\n")
                log_overflow_reported = True
            if workload is not None and workload.poll() is not None:
                exit_code = _stop_group(workload, grace_seconds=None)
                connection.sendall(f"EXIT {exit_code}\n".encode("ascii"))
                return exit_code
            readable, _, _ = select.select([connection], [], [], 0.05)
            if not readable:
                continue
            chunk = connection.recv(4096)
            if not chunk:
                if workload is not None:
                    _stop_group(workload, grace_seconds=None)
                return 143
            buffer.extend(chunk)
            while b"\n" in buffer:
                raw, _, remainder = buffer.partition(b"\n")
                buffer[:] = remainder
                instruction = raw.decode("ascii", "strict")
                if instruction == "START":
                    if workload is not None:
                        continue
                    workload = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        close_fds=True,
                        start_new_session=True,
                        preexec_fn=_set_parent_death_signal,
                    )
                    for stream in (workload.stdout, workload.stderr):
                        assert stream is not None
                        threading.Thread(target=drain, args=(stream,), daemon=True).start()
                    connection.sendall(f"STARTED {workload.pid}\n".encode("ascii"))
                elif (grace := _term_grace(instruction)) is not None:
                    if workload is None:
                        connection.sendall(b"EXIT 143\n")
                        return 143
                    exit_code = _stop_group(workload, grace_seconds=grace)
                    connection.sendall(f"EXIT {exit_code}\n".encode("ascii"))
                    return exit_code
                elif instruction == "KILL":
                    if workload is None:
                        connection.sendall(b"EXIT 137\n")
                        return 137
                    exit_code = _stop_group(workload, grace_seconds=None)
                    connection.sendall(f"EXIT {exit_code}\n".encode("ascii"))
                    return exit_code
                else:
                    if workload is not None:
                        _stop_group(workload, grace_seconds=None)
                    return 78
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Nexa workload process-group supervisor")
    control = parser.add_mutually_exclusive_group(required=True)
    control.add_argument("--control-fd", type=int)
    control.add_argument("--registration-socket")
    parser.add_argument("--log-limit", type=int, default=1024 * 1024)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = tuple(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        return 78
    if args.registration_socket is not None:
        if args.log_limit < 1:
            return 78
        return supervise_socket(
            args.registration_socket,
            command,
            log_limit_bytes=args.log_limit,
        )
    assert args.control_fd is not None
    return supervise(args.control_fd, command)


if __name__ == "__main__":
    raise SystemExit(main())
