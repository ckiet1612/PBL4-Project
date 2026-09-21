import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_supervisor_kills_descendant_when_direct_parent_exits_first(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_code = (
        "import os,signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f"open({str(child_pid_path)!r},'w').write(str(os.getpid()));"
        "time.sleep(60)"
    )
    parent_code = (
        "import signal,subprocess,sys,time;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        "signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));"
        "time.sleep(60)"
    )
    read_fd, write_fd = os.pipe()
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nexa.workloads.workload_supervisor",
            "--control-fd",
            str(read_fd),
            "--",
            sys.executable,
            "-c",
            parent_code,
        ],
        pass_fds=(read_fd,),
        start_new_session=True,
    )
    os.close(read_fd)
    child_pid = None
    try:
        os.write(write_fd, b"START\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_path.exists():
            time.sleep(0.01)
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        assert _process_exists(child_pid)
        os.write(write_fd, b"TERM\n")
        supervisor.wait(timeout=7)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _process_exists(child_pid):
            time.sleep(0.01)
        assert not _process_exists(child_pid)
    finally:
        os.close(write_fd)
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=2)
        if child_pid is not None and _process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_supervisor_cleans_process_group_when_direct_child_completes(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "orphan.pid"
    child_code = (
        "import os,signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f"open({str(child_pid_path)!r},'w').write(str(os.getpid()));"
        "time.sleep(60)"
    )
    parent_code = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        "time.sleep(0.2)"
    )
    read_fd, write_fd = os.pipe()
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nexa.workloads.workload_supervisor",
            "--control-fd",
            str(read_fd),
            "--",
            sys.executable,
            "-c",
            parent_code,
        ],
        pass_fds=(read_fd,),
        start_new_session=True,
    )
    os.close(read_fd)
    child_pid = None
    try:
        os.write(write_fd, b"START\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pid_path.exists():
            time.sleep(0.01)
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        assert _process_exists(child_pid)
        assert supervisor.wait(timeout=5) == 0
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _process_exists(child_pid):
            time.sleep(0.01)
        assert not _process_exists(child_pid)
    finally:
        os.close(write_fd)
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=2)
        if child_pid is not None and _process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_supervisor_registers_then_waits_for_start(tmp_path: Path) -> None:
    socket_root = tempfile.TemporaryDirectory(prefix="nexa-supervisor-", dir="/tmp")
    socket_path = Path(socket_root.name) / "s"
    output = tmp_path / "started.txt"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nexa.workloads.workload_supervisor",
            "--registration-socket",
            str(socket_path),
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path;Path({str(output)!r}).write_text('started')",
        ],
        start_new_session=True,
    )
    connection = None
    try:
        server.settimeout(3)
        connection, _ = server.accept()
        connection.settimeout(3)
        assert connection.recv(64) == b"READY\n"
        assert not output.exists()
        connection.sendall(b"START\n")
        frames = bytearray()
        while b"EXIT 0\n" not in frames:
            frames.extend(connection.recv(128))
        assert b"STARTED " in frames
        assert output.read_text(encoding="utf-8") == "started"
        assert supervisor.wait(timeout=3) == 0
    finally:
        if connection is not None:
            connection.close()
        server.close()
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=2)
        socket_root.cleanup()


def test_registered_supervisor_reports_log_overflow(tmp_path: Path) -> None:
    socket_root = tempfile.TemporaryDirectory(prefix="nexa-supervisor-", dir="/tmp")
    socket_path = Path(socket_root.name) / "s"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nexa.workloads.workload_supervisor",
            "--registration-socket",
            str(socket_path),
            "--log-limit",
            "1024",
            "--",
            sys.executable,
            "-c",
            "import os,time;os.write(1,b'x'*2048);time.sleep(60)",
        ],
        start_new_session=True,
    )
    connection = None
    try:
        server.settimeout(3)
        connection, _ = server.accept()
        connection.settimeout(3)
        assert connection.recv(64) == b"READY\n"
        connection.sendall(b"START\n")
        frames = bytearray()
        while b"LOG_OVERFLOW\n" not in frames:
            frames.extend(connection.recv(128))
        connection.sendall(b"KILL\n")
        while b"EXIT " not in frames:
            frames.extend(connection.recv(128))
    finally:
        if connection is not None:
            connection.close()
        server.close()
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=2)
        socket_root.cleanup()


@pytest.mark.parametrize("grace_seconds", [0.0, 0.1])
def test_registered_supervisor_enforces_requested_short_grace(
    tmp_path: Path, grace_seconds: float
) -> None:
    socket_root = tempfile.TemporaryDirectory(prefix="nexa-supervisor-", dir="/tmp")
    socket_path = Path(socket_root.name) / "s"
    child_pid_path = tmp_path / f"child-{grace_seconds}.pid"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nexa.workloads.workload_supervisor",
            "--registration-socket",
            str(socket_path),
            "--",
            sys.executable,
            "-c",
            (
                "import os,signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                f"open({str(child_pid_path)!r},'w').write(str(os.getpid()));"
                "time.sleep(60)"
            ),
        ],
        start_new_session=True,
    )
    connection = None
    child_pid = None
    try:
        server.settimeout(3)
        connection, _ = server.accept()
        connection.settimeout(3)
        assert connection.recv(64) == b"READY\n"
        connection.sendall(b"START\n")
        frames = bytearray()
        while b"STARTED " not in frames:
            frames.extend(connection.recv(128))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not child_pid_path.exists():
            time.sleep(0.01)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        started = time.monotonic()
        connection.sendall(f"TERM {grace_seconds:.9f}\n".encode("ascii"))
        deadline = time.monotonic() + 3
        while b"EXIT " not in frames and time.monotonic() < deadline:
            chunk = connection.recv(128)
            assert chunk, "supervisor closed without EXIT"
            frames.extend(chunk)
        assert b"EXIT " in frames
        elapsed = time.monotonic() - started
        assert elapsed < grace_seconds + 0.75
        assert supervisor.wait(timeout=2) != 0
        assert not _process_exists(child_pid)
    finally:
        if connection is not None:
            connection.close()
        server.close()
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=2)
        if child_pid is not None and _process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)
        socket_root.cleanup()
