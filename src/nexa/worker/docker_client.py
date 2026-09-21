"""Small, injectable Docker CLI boundary.

The worker is the only production caller. Commands are argv tuples so client
data cannot become shell syntax; callers must pass already validated values.
"""

import json
import os
import re
import select
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
_CONTAINER_ID_LENGTH = 64
_CONTROL_SOCKET_PATH = "/run/nexa/control.sock"


class DockerContainerNotFound(RuntimeError):
    pass


class DockerCommandBackend(Protocol):
    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]: ...


class SubprocessDockerBackend:
    def __init__(self, *, max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES) -> None:
        if max_output_bytes < 1 or max_output_bytes > MAX_COMMAND_OUTPUT_BYTES:
            raise ValueError("max_output_bytes is outside the worker command bound")
        self.max_output_bytes = max_output_bytes

    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdout is not None
        assert process.stderr is not None
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        overflow = threading.Event()

        def drain(stream, key: str) -> None:  # type: ignore[no-untyped-def]
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = self.max_output_bytes - len(buffers[key])
                if len(chunk) > remaining:
                    buffers[key].extend(chunk[:remaining])
                    overflow.set()
                    with suppress(ProcessLookupError):
                        process.kill()
                    return
                buffers[key].extend(chunk)

        readers = [
            threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            for reader in readers:
                reader.join()
            raise TimeoutError("Docker command timed out") from exc
        for reader in readers:
            reader.join()
        if overflow.is_set():
            raise RuntimeError("Docker command output exceeded the bounded capture limit")
        return returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])


class DockerControlChannel:
    """Byte stream from the worker to the runner's private in-container socket."""

    def __init__(
        self,
        container_id: str,
        *,
        process_factory=subprocess.Popen,
    ) -> None:  # type: ignore[no-untyped-def]
        if len(container_id) != _CONTAINER_ID_LENGTH or any(
            char not in "0123456789abcdef" for char in container_id
        ):
            raise ValueError("control channel requires a full container ID")
        argv = (
            "docker",
            "exec",
            "--interactive",
            "--user",
            "1000:1000",
            container_id,
            "python",
            "-m",
            "nexa.workloads.control_relay",
            "--socket",
            _CONTROL_SOCKET_PATH,
        )
        self.process = process_factory(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        if self.process.stdin is None or self.process.stdout is None or self.process.stderr is None:
            raise RuntimeError("Docker control relay pipes were not created")
        self._timeout: float | None = None

    def __enter__(self) -> "DockerControlChannel":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def settimeout(self, timeout: float | None) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        self._timeout = timeout

    def sendall(self, payload: bytes) -> None:
        self._raise_if_exited()
        self.process.stdin.write(payload)
        self.process.stdin.flush()

    def recv(self, maximum_bytes: int) -> bytes:
        if maximum_bytes < 1:
            raise ValueError("maximum_bytes must be positive")
        ready, _, _ = select.select([self.process.stdout], [], [], self._timeout)
        if not ready:
            raise TimeoutError("timed out waiting for the trusted runner")
        payload = os.read(self.process.stdout.fileno(), maximum_bytes)
        if not payload:
            self._raise_if_exited()
            raise RuntimeError("Docker control relay closed unexpectedly")
        return payload

    def close(self) -> None:
        with suppress(OSError, ValueError):
            self.process.stdin.close()
        if self.process.poll() is None:
            with suppress(ProcessLookupError):
                self.process.terminate()
        with suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=1)
        for stream in (self.process.stdout, self.process.stderr):
            with suppress(OSError, ValueError):
                stream.close()

    def _raise_if_exited(self) -> None:
        code = self.process.poll()
        if code is None:
            return
        error = self.process.stderr.read(MAX_COMMAND_OUTPUT_BYTES).decode("utf-8", "replace")
        raise RuntimeError(f"Docker control relay exited with code {code}: {error.strip()}")


@dataclass(frozen=True, slots=True)
class DockerCreateResult:
    container_id: str


@dataclass(frozen=True, slots=True)
class DockerInspection:
    container_id: str
    payload: dict[str, object]


class DockerCli:
    def __init__(self, backend: DockerCommandBackend | None = None) -> None:
        self.backend = backend or SubprocessDockerBackend()

    def create(self, argv: tuple[str, ...], *, timeout_seconds: float) -> DockerCreateResult:
        code, stdout, stderr = self.backend.run(argv, timeout_seconds)
        if code != 0:
            raise RuntimeError(f"Docker create failed with code {code}")
        container_id = stdout.decode("utf-8", "strict").strip()
        if len(container_id) != _CONTAINER_ID_LENGTH or any(
            char not in "0123456789abcdef" for char in container_id
        ):
            raise RuntimeError("Docker create did not return a full container ID")
        return DockerCreateResult(container_id)

    def start(self, container_id: str, *, timeout_seconds: float) -> None:
        self._run(("docker", "start", container_id), timeout_seconds)

    def exec_detached(
        self,
        container_id: str,
        argv: tuple[str, ...],
        *,
        user: str,
        timeout_seconds: float,
    ) -> None:
        _validate_container_id(container_id)
        if not argv:
            raise ValueError("Docker exec command must not be empty")
        if user != "1001:1000":
            raise ValueError("workload supervisor requires the fixed non-root identity")
        self._run(
            ("docker", "exec", "--detach", "--user", user, container_id, *argv),
            timeout_seconds,
        )

    def stop(self, container_id: str, *, timeout_seconds: float) -> None:
        self._run(("docker", "stop", "--time", "5", container_id), timeout_seconds)

    def kill(self, container_id: str, *, timeout_seconds: float) -> None:
        self._run(("docker", "kill", container_id), timeout_seconds)

    def remove(self, container_id: str, *, timeout_seconds: float) -> None:
        self._run(("docker", "rm", "--force", container_id), timeout_seconds)

    def inspect(self, container_id: str, *, timeout_seconds: float) -> DockerInspection:
        code, stdout, stderr = self.backend.run(
            ("docker", "inspect", container_id), timeout_seconds
        )
        if code != 0:
            error = stderr.decode("utf-8", "replace").lower()
            if re.search(r"(?:no such (?:container|object)|container .+ not found)", error):
                raise DockerContainerNotFound("Docker container does not exist")
            raise RuntimeError(f"Docker inspect failed with code {code}")
        try:
            entries = json.loads(stdout.decode("utf-8", "strict"))
            payload = entries[0]
        except (UnicodeDecodeError, json.JSONDecodeError, IndexError, TypeError, KeyError) as exc:
            raise RuntimeError("Docker inspect returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Docker inspect returned invalid JSON")
        if payload.get("Id") != container_id:
            raise RuntimeError("Docker inspect identity mismatch")
        return DockerInspection(container_id, payload)

    def find_by_labels(self, labels: dict[str, str], *, timeout_seconds: float) -> tuple[str, ...]:
        argv = ["docker", "ps", "--all", "--no-trunc", "--quiet"]
        for key, value in sorted(labels.items()):
            argv.extend(("--filter", f"label={key}={value}"))
        code, stdout, _ = self.backend.run(tuple(argv), timeout_seconds)
        if code != 0:
            raise RuntimeError(f"Docker container query failed with code {code}")
        identifiers = tuple(line.strip() for line in stdout.decode("utf-8", "strict").splitlines())
        if any(
            len(identifier) != 64 or any(char not in "0123456789abcdef" for char in identifier)
            for identifier in identifiers
        ):
            raise RuntimeError("Docker container query returned an invalid identity")
        return identifiers

    def _run(self, argv: tuple[str, ...], timeout_seconds: float) -> None:
        code, _, _ = self.backend.run(argv, timeout_seconds)
        if code != 0:
            raise RuntimeError(f"Docker command failed with code {code}")


def _validate_container_id(container_id: str) -> None:
    if len(container_id) != _CONTAINER_ID_LENGTH or any(
        char not in "0123456789abcdef" for char in container_id
    ):
        raise ValueError("Docker operation requires a full container ID")
