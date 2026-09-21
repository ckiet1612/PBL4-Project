import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def test_control_relay_forwards_bytes_in_both_directions() -> None:
    received: list[bytes] = []

    with tempfile.TemporaryDirectory(prefix="nexa-relay-", dir="/tmp") as directory:
        socket_path = Path(directory) / "control.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)

            def exchange() -> None:
                connection, _ = server.accept()
                with connection:
                    received.append(connection.recv(4))
                    connection.sendall(b"pong")

            thread = threading.Thread(target=exchange, daemon=True)
            thread.start()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "nexa.workloads.control_relay",
                    "--socket",
                    str(socket_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(b"ping")
            process.stdin.flush()
            assert process.stdout.read(4) == b"pong"
            process.stdin.close()
            assert process.wait(timeout=5) == 0
            thread.join(timeout=5)

    assert received == [b"ping"]
