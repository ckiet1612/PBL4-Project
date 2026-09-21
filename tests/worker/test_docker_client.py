import io
import subprocess
import sys

import pytest

from nexa.worker import docker_client
from nexa.worker.docker_client import (
    DockerCli,
    DockerCommandBackend,
    DockerContainerNotFound,
    SubprocessDockerBackend,
)


class RecordingBackend(DockerCommandBackend):
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        self.calls.append(argv)
        if argv[1] == "create":
            return 0, ("a" * 64 + "\n").encode(), b""
        if argv[1] == "inspect":
            return (
                0,
                b'{"Id":"' + (b"a" * 64) + b'","Config":{"Labels":{}},"State":{"Running":false}}',
                b"",
            )
        return 0, b"", b""


def test_docker_cli_uses_argv_and_full_id() -> None:
    backend = RecordingBackend()
    client = DockerCli(backend)
    output = client.create(("docker", "create", "--network", "none"), timeout_seconds=2)
    assert output.container_id == "a" * 64
    assert backend.calls == [("docker", "create", "--network", "none")]


def test_docker_cli_execs_supervisor_detached_by_exact_id() -> None:
    backend = RecordingBackend()
    client = DockerCli(backend)
    container_id = "a" * 64
    command = (
        "python",
        "-m",
        "nexa.workloads.workload_supervisor",
        "--registration-socket",
        "/run/nexa/supervisor-register.sock",
    )

    client.exec_detached(
        container_id,
        command,
        user="1001:1000",
        timeout_seconds=2,
    )

    assert backend.calls == [
        (
            "docker",
            "exec",
            "--detach",
            "--user",
            "1001:1000",
            container_id,
            *command,
        )
    ]


def test_subprocess_backend_bounds_command_output() -> None:
    backend = SubprocessDockerBackend(max_output_bytes=32)
    with pytest.raises(RuntimeError, match="bounded capture"):
        backend.run((sys.executable, "-c", "print('x' * 1000)"), timeout_seconds=2)


def test_missing_docker_socket_is_unavailable_not_container_absence() -> None:
    class MissingSocketBackend(DockerCommandBackend):
        def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
            del argv, timeout_seconds
            return (
                1,
                b"",
                b"Cannot connect: dial unix /var/run/docker.sock: no such file or directory",
            )

    with pytest.raises(RuntimeError) as error:
        DockerCli(MissingSocketBackend()).inspect("a" * 64, timeout_seconds=2)
    assert not isinstance(error.value, DockerContainerNotFound)


def test_control_channel_uses_runner_uid_and_internal_socket() -> None:
    assert hasattr(docker_client, "DockerControlChannel")
    channel_type = docker_client.DockerControlChannel
    captured: dict[str, object] = {}

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            assert self.returncode is not None
            return self.returncode

    def process_factory(argv: tuple[str, ...], **kwargs: object) -> FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    channel = channel_type("a" * 64, process_factory=process_factory)
    try:
        assert captured["argv"] == (
            "docker",
            "exec",
            "--interactive",
            "--user",
            "1000:1000",
            "a" * 64,
            "python",
            "-m",
            "nexa.workloads.control_relay",
            "--socket",
            "/run/nexa/control.sock",
        )
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["stdin"] is subprocess.PIPE
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
    finally:
        channel.close()
