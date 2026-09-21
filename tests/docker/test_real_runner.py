import hashlib
import json
import os
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from nexa.worker.docker_client import DockerControlChannel, SubprocessDockerBackend
from nexa.worker.executor import DockerExecutor
from nexa.worker.journal import ExecutionJournal
from nexa.worker.models import ContainerIdentity, CpuWorkloadSpec, InputMount, ResourceVector
from nexa.worker.protocol import FrameDecoder, encode_frame
from tests.worker.test_docker_config import start

CALLBACK = "018f0d60-7b6a-7a31-9d82-1aa39c4f30b7"
RESULT = "018f0d60-7b6a-7a32-9d82-1aa39c4f30b7"
ARTIFACT = "018f0d60-7b6a-7a33-9d82-1aa39c4f30b7"


def _evidence(scenario: str, **observations: object) -> None:
    if os.environ.get("NEXA_B09_EVIDENCE") == "1":
        print(
            json.dumps(
                {"evidence": "B09", "scenario": scenario, "status": "pass", **observations},
                sort_keys=True,
            ),
            flush=True,
        )


class Peer:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.decoder = FrameDecoder()
        self.pending: list[dict[str, object]] = []

    def send(self, payload: dict[str, object]) -> None:
        self.connection.sendall(encode_frame(payload))

    def receive(self, *, timeout: float = 10) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pending:
                return self.pending.pop(0)
            self.connection.settimeout(max(0.01, deadline - time.monotonic()))
            data = self.connection.recv(64 * 1024)
            if not data:
                raise AssertionError("runner disconnected before the expected frame")
            self.pending.extend(self.decoder.feed(data))
        raise AssertionError("timed out waiting for a runner frame")

    def receive_type(self, message_type: str, *, timeout: float = 15) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        deferred: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            frame = self.receive(timeout=deadline - time.monotonic())
            if frame.get("type") == message_type:
                self.pending = deferred + self.pending
                return frame
            deferred.append(frame)
        raise AssertionError(f"timed out waiting for {message_type}")

    def receive_ack(self, sequence: int, *, timeout: float = 10) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        deferred: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            frame = self.receive(timeout=deadline - time.monotonic())
            if frame.get("ack_sequence") == sequence:
                self.pending = deferred + self.pending
                return frame
            deferred.append(frame)
        raise AssertionError(f"timed out waiting for acknowledgment {sequence}")


class SupervisorExecBarrierBackend:
    def __init__(self, *, release_timeout_seconds: float = 10) -> None:
        self.backend = SubprocessDockerBackend()
        self.supervisor_exec_blocked = threading.Event()
        self.release_supervisor_exec = threading.Event()
        self.container_id: str | None = None
        self.supervisor_exec_count = 0
        self.release_timeout_seconds = release_timeout_seconds

    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        if argv[:2] == ("docker", "create"):
            result = self.backend.run(argv, timeout_seconds)
            if result[0] == 0:
                self.container_id = result[1].decode("ascii").strip()
            return result
        if (
            argv[:4] == ("docker", "exec", "--detach", "--user")
            and "nexa.workloads.workload_supervisor" in argv
        ):
            self.supervisor_exec_count += 1
            self.supervisor_exec_blocked.set()
            if not self.release_supervisor_exec.wait(timeout=self.release_timeout_seconds):
                raise TimeoutError("test barrier timed out before supervisor exec")
        return self.backend.run(argv, timeout_seconds)


def _wait_for_isolated_processes(container_id: str, *, timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    next_readiness_probe = 0.0
    last_result: subprocess.CompletedProcess[str] | None = None
    last_ready: subprocess.CompletedProcess[str] | None = None
    while time.monotonic() < deadline:
        last_result = subprocess.run(
            ["docker", "top", container_id, "-eo", "pid,user,args"],
            capture_output=True,
            text=True,
        )
        if last_result.returncode == 0:
            output = last_result.stdout
            process_lines = output.lower().splitlines()[1:]
            now = time.monotonic()
            if (
                ("nexa-runner" in output or "1000" in output)
                and ("nexa-workload" in output or "1001" in output)
                and all(" root " not in f" {line} " for line in process_lines)
                and now >= next_readiness_probe
            ):
                last_ready = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "--user",
                        "1000:1000",
                        container_id,
                        "test",
                        "-S",
                        "/run/nexa/control.sock",
                    ],
                    capture_output=True,
                    text=True,
                )
                if last_ready.returncode == 0:
                    return output
                next_readiness_probe = time.monotonic() + 1.0
        time.sleep(0.1)
    inspection = subprocess.run(
        ["docker", "inspect", container_id],
        capture_output=True,
        text=True,
    )
    logs = subprocess.run(
        ["docker", "logs", container_id],
        capture_output=True,
        text=True,
    )
    top_error = "not-run" if last_result is None else last_result.stderr
    top_output = "not-run" if last_result is None else last_result.stdout
    ready_error = "not-run" if last_ready is None else last_ready.stderr
    raise AssertionError(
        f"runner/workload UID isolation was not ready: {top_error}\n"
        f"top={top_output}\nready={ready_error}\n"
        f"inspect={inspection.stdout}{inspection.stderr}\n"
        f"logs={logs.stdout}{logs.stderr}"
    )


def _wait_for_runner_control(container_id: str, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "1000:1000",
                container_id,
                "test",
                "-S",
                "/run/nexa/control.sock",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        time.sleep(0.05)
    raise AssertionError("runner control socket was not ready before the test deadline")


def _wait_for_container_stop(
    container_id: str, *, origin: float | None = None, timeout: float = 8
) -> float:
    observed_from = time.monotonic() if origin is None else origin
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip() == "false":
            return time.monotonic() - observed_from
        time.sleep(0.02)
    raise AssertionError(f"container {container_id} did not stop within {timeout} seconds")


def _read_cgroup_counters(container_id: str, path: str) -> dict[str, int]:
    result = subprocess.run(
        ["docker", "exec", container_id, "cat", path],
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        name: int(value) for line in result.stdout.splitlines() for name, value in [line.split()]
    }


def _read_container_integer(container_id: str, path: str, *, timeout: float = 3) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", "--user", "1001:1000", container_id, "cat", path],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
        time.sleep(0.02)
    raise AssertionError(f"container timestamp {path} was not readable before the deadline")


@contextmanager
def _production_container(
    tmp_path: Path,
    *,
    image: str | None = None,
    iterations: int = 1_000_000_000,
    runtime_limit_seconds: int = 30,
    cpu_millis: int = 1000,
    memory_bytes: int = 256 * 1024 * 1024,
    scratch_bytes: int = 64 * 1024 * 1024,
    log_bytes: int = 1024 * 1024,
) -> Iterator[tuple[DockerExecutor, ContainerIdentity]]:
    image = image or os.environ.get("NEXA_B09_IMAGE_REF", "")
    if "@sha256:" not in image:
        pytest.fail("NEXA_B09_IMAGE_REF must be an exact digest reference")
    image_ref, image_digest = image.rsplit("@", 1)
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_dir = tmp_path / "staging"
    input_dir.mkdir()
    source = input_dir / "input.json"
    source.write_bytes(b'{"initial_value":17}')
    input_checksum = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    request = start()
    resources = ResourceVector(cpu_millis=cpu_millis, memory_bytes=memory_bytes, gpu_count=0)
    request = replace(
        request,
        context=replace(
            request.context,
            image_digest=image_digest,
            architecture="linux/arm64",
            input_checksum=input_checksum,
            resources=resources,
        ),
        allocation=replace(request.allocation, resources=resources),
        input_mounts=(
            InputMount(
                artifact_id="018f0d60-7b6a-7a34-9d82-1aa39c4f30b7",
                source_path=str(source),
                target_path="/input/input.json",
                content_checksum=input_checksum,
                size_bytes=source.stat().st_size,
            ),
        ),
        cpu_workload=CpuWorkloadSpec(
            iterations=iterations,
            seed=7,
            modulus=1_000_000_007,
            spec_checksum="sha256:" + "f" * 64,
        ),
        runtime_limit_seconds=runtime_limit_seconds,
        scratch_bytes=scratch_bytes,
        log_bytes=log_bytes,
    )
    journal = ExecutionJournal(tmp_path / "journal")
    executor = DockerExecutor(
        journal,
        SubprocessDockerBackend(),
        image_ref=image_ref,
        staging_root=input_dir,
    )
    identity = executor.start(executor.prepare(request))
    try:
        _wait_for_isolated_processes(identity.container_id)
        yield executor, identity
    finally:
        executor.cleanup(identity)


@contextmanager
def _log_pressure_image(tmp_path: Path) -> Iterator[str]:
    base = os.environ.get("NEXA_B09_IMAGE_REF", "")
    if "@sha256:" not in base:
        pytest.fail("NEXA_B09_IMAGE_REF must be an exact digest reference")
    context = tmp_path / "log-pressure-image"
    context.mkdir()
    (context / "Dockerfile").write_text(
        "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n"
        "COPY cpu_entrypoint.py /opt/nexa/nexa/workloads/cpu_entrypoint.py\n",
        encoding="ascii",
    )
    (context / "cpu_entrypoint.py").write_text(
        "import argparse,os,time\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--input');p.add_argument('--output')\n"
        "p.add_argument('--iterations');p.add_argument('--seed')\n"
        "p.add_argument('--modulus');p.add_argument('--spec-checksum')\n"
        "p.parse_args()\n"
        "open('/output/log-trigger.ns','w').write(str(time.monotonic_ns()))\n"
        "time.sleep(1.0)\n"
        "os.write(1,b'x'*(128*1024))\n"
        "time.sleep(60)\n",
        encoding="ascii",
    )
    token = f"{os.getpid()}-{abs(hash(str(tmp_path))) & 0xFFFF:x}"
    base_tag = f"nexa/cpu-log-base:{token}"
    tag = f"nexa/cpu-log-pressure:{token}"
    subprocess.run(["docker", "tag", base, base_tag], check=True, capture_output=True)
    exact_image = ""
    try:
        build = subprocess.run(
            [
                "docker",
                "build",
                "--pull=false",
                "--build-arg",
                f"BASE_IMAGE={base_tag}",
                "--tag",
                tag,
                str(context),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert build.returncode == 0, build.stderr
        inspection = subprocess.run(
            ["docker", "image", "inspect", tag, "--format", "{{index .RepoDigests 0}}"],
            check=True,
            capture_output=True,
            text=True,
        )
        exact_image = inspection.stdout.strip()
        assert "@sha256:" in exact_image
        yield exact_image
    finally:
        for image in (exact_image, tag, base_tag):
            if image:
                subprocess.run(
                    ["docker", "image", "rm", "--force", image],
                    capture_output=True,
                    text=True,
                )


@pytest.mark.docker
def test_authority_before_supervisor_registration_launches_once_as_workload_uid(
    tmp_path: Path,
) -> None:
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in real Docker suite; use scripts/b09_docker_tests.sh --require")
    image = os.environ.get("NEXA_B09_IMAGE_REF", "")
    if "@sha256:" not in image:
        pytest.fail("NEXA_B09_IMAGE_REF must be an exact digest reference")
    image_ref, image_digest = image.rsplit("@", 1)
    input_dir = tmp_path / "staging"
    input_dir.mkdir()
    source = input_dir / "input.json"
    source.write_bytes(b'{"initial_value":17}')
    input_checksum = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    request = start()
    request = replace(
        request,
        context=replace(
            request.context,
            image_digest=image_digest,
            architecture="linux/arm64",
            input_checksum=input_checksum,
        ),
        input_mounts=(
            InputMount(
                artifact_id="018f0d60-7b6a-7a34-9d82-1aa39c4f30b7",
                source_path=str(source),
                target_path="/input/input.json",
                content_checksum=input_checksum,
                size_bytes=source.stat().st_size,
            ),
        ),
        cpu_workload=CpuWorkloadSpec(
            iterations=1_000_000_000,
            seed=7,
            modulus=1_000_000_007,
            spec_checksum="sha256:" + "f" * 64,
        ),
    )
    backend = SupervisorExecBarrierBackend()
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "journal"),
        backend,
        image_ref=image_ref,
        staging_root=input_dir,
    )
    result: dict[str, ContainerIdentity] = {}
    errors: list[BaseException] = []

    def start_execution() -> None:
        try:
            result["identity"] = executor.start(executor.prepare(request))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    starter = threading.Thread(target=start_execution, name="executor-start")
    starter.start()
    identity: ContainerIdentity | None = None
    try:
        assert backend.supervisor_exec_blocked.wait(timeout=30)
        assert backend.container_id is not None
        _wait_for_runner_control(backend.container_id)
        with DockerControlChannel(backend.container_id) as connection:
            peer = Peer(connection)
            peer.send(
                {
                    "schema_version": 1,
                    "control_sequence": 1,
                    "type": "SET_AUTHORITY_DEADLINE",
                    "payload": {
                        "source_callback_id": CALLBACK,
                        "deadline_monotonic_ns": 2**63 - 1,
                    },
                }
            )
            assert peer.receive_ack(1)["accepted"] is True
            assert starter.is_alive()
            backend.release_supervisor_exec.set()
            starter.join(timeout=30)
            assert not starter.is_alive()
            assert errors == []
            identity = result["identity"]
            started = peer.receive_type("STARTED")
            workload_pid = int(started["payload"]["pid"])
            status = subprocess.run(
                [
                    "docker",
                    "exec",
                    identity.container_id,
                    "awk",
                    "/^Uid:/{print $2}",
                    f"/proc/{workload_pid}/status",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            assert status.stdout.strip() == "1001"
            peer.receive_type("PROGRESS")
            assert backend.supervisor_exec_count == 1
            _evidence(
                "authority-before-supervisor-registration",
                supervisor_exec_count=backend.supervisor_exec_count,
                workload_uid=1001,
                started_message_count=1,
            )
    finally:
        backend.release_supervisor_exec.set()
        starter.join(timeout=5)
        if identity is not None:
            executor.cleanup(identity)
        elif backend.container_id is not None:
            subprocess.run(
                ["docker", "rm", "--force", backend.container_id],
                capture_output=True,
                text=True,
            )


@pytest.mark.docker
def test_supervisor_registration_after_startup_deadline_never_launches_cpu(
    tmp_path: Path,
) -> None:
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in real Docker suite; use scripts/b09_docker_tests.sh --require")
    image = os.environ.get("NEXA_B09_IMAGE_REF", "")
    if "@sha256:" not in image:
        pytest.fail("NEXA_B09_IMAGE_REF must be an exact digest reference")
    image_ref, image_digest = image.rsplit("@", 1)
    input_dir = tmp_path / "staging"
    input_dir.mkdir()
    source = input_dir / "input.json"
    source.write_bytes(b'{"initial_value":17}')
    input_checksum = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    request = start()
    request = replace(
        request,
        context=replace(
            request.context,
            image_digest=image_digest,
            architecture="linux/arm64",
            input_checksum=input_checksum,
        ),
        input_mounts=(
            InputMount(
                artifact_id="018f0d60-7b6a-7a34-9d82-1aa39c4f30b7",
                source_path=str(source),
                target_path="/input/input.json",
                content_checksum=input_checksum,
                size_bytes=source.stat().st_size,
            ),
        ),
        cpu_workload=CpuWorkloadSpec(
            iterations=1_000_000_000,
            seed=7,
            modulus=1_000_000_007,
            spec_checksum="sha256:" + "f" * 64,
        ),
    )
    backend = SupervisorExecBarrierBackend(release_timeout_seconds=40)
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "journal"),
        backend,
        image_ref=image_ref,
        staging_root=input_dir,
    )
    errors: list[BaseException] = []

    def start_execution() -> None:
        try:
            executor.start(executor.prepare(request))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    starter = threading.Thread(target=start_execution, name="late-supervisor-start")
    starter.start()
    stopped: dict[str, object] | None = None
    ready_at = 0.0
    try:
        assert backend.supervisor_exec_blocked.wait(timeout=30)
        assert backend.container_id is not None
        _wait_for_runner_control(backend.container_id)
        ready_at = time.monotonic()
        with DockerControlChannel(backend.container_id) as connection:
            peer = Peer(connection)
            peer.send(
                {
                    "schema_version": 1,
                    "control_sequence": 1,
                    "type": "SET_AUTHORITY_DEADLINE",
                    "payload": {
                        "source_callback_id": CALLBACK,
                        "deadline_monotonic_ns": 2**63 - 1,
                    },
                }
            )
            assert peer.receive_ack(1)["accepted"] is True
            process_snapshot = subprocess.run(
                ["docker", "top", backend.container_id, "-eo", "pid,user,args"],
                check=True,
                capture_output=True,
                text=True,
            )
            cpu_process_count = sum(
                "nexa.workloads.cpu_entrypoint" in line
                for line in process_snapshot.stdout.splitlines()[1:]
            )
            assert cpu_process_count == 0
            stopped = peer.receive_type("STOPPED", timeout=35)
            assert stopped["payload"]["reason"] == "RUNTIME_LIMIT"
            assert all(frame.get("type") != "STARTED" for frame in peer.pending)
        stopped_after_ready = time.monotonic() - ready_at
        backend.release_supervisor_exec.set()
        starter.join(timeout=10)
        assert not starter.is_alive()
        assert len(errors) == 1
        inspect = subprocess.run(
            [
                "docker",
                "inspect",
                backend.container_id,
                "--format",
                "{{json .State.Running}}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert inspect.stdout.strip() == "false"
        assert backend.supervisor_exec_count == 1
        _evidence(
            "supervisor-registration-after-startup-deadline",
            bound_seconds=35,
            started_message_count=0,
            cpu_process_count=cpu_process_count,
            stop_reason="RUNTIME_LIMIT",
            stopped_after_runner_ready_seconds=round(stopped_after_ready, 6),
        )
    finally:
        backend.release_supervisor_exec.set()
        starter.join(timeout=5)
        if backend.container_id is not None:
            subprocess.run(
                ["docker", "rm", "--force", backend.container_id],
                capture_output=True,
                text=True,
            )


@pytest.mark.docker
def test_production_executor_runner_cpu_result_handshake(tmp_path: Path) -> None:
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in real Docker suite; use scripts/b09_docker_tests.sh --require")
    image = os.environ.get("NEXA_B09_IMAGE_REF", "")
    if "@sha256:" not in image:
        pytest.fail("NEXA_B09_IMAGE_REF must be an exact digest reference")
    image_ref, image_digest = image.rsplit("@", 1)
    input_dir = tmp_path / "staging"
    input_dir.mkdir()
    source = input_dir / "input.json"
    source.write_bytes(b'{"initial_value":17}')
    input_checksum = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    request = start()
    request = replace(
        request,
        context=replace(
            request.context,
            image_digest=image_digest,
            architecture="linux/arm64",
            input_checksum=input_checksum,
        ),
        input_mounts=(
            InputMount(
                artifact_id="018f0d60-7b6a-7a34-9d82-1aa39c4f30b7",
                source_path=str(source),
                target_path="/input/input.json",
                content_checksum=input_checksum,
                size_bytes=source.stat().st_size,
            ),
        ),
        cpu_workload=CpuWorkloadSpec(
            iterations=3,
            seed=7,
            modulus=1_000_000_007,
            spec_checksum="sha256:" + "f" * 64,
        ),
    )
    journal = ExecutionJournal(tmp_path / "journal")
    executor = DockerExecutor(
        journal,
        SubprocessDockerBackend(),
        image_ref=image_ref,
        staging_root=input_dir,
    )
    identity = executor.start(executor.prepare(request))
    try:
        top = _wait_for_isolated_processes(identity.container_id)
        assert "nexa-runner" in top or "1000" in top
        assert "nexa-workload" in top or "1001" in top
        assert " root " not in f" {top.lower()} "

        isolation = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "1001:1000",
                identity.container_id,
                "python",
                "-c",
                (
                    "import os,socket;"
                    "s=socket.socket(socket.AF_UNIX);"
                    "denied=[];"
                    "\ntry:s.connect('/run/nexa/control.sock')"
                    "\nexcept PermissionError:denied.append('socket')"
                    "\ntry:os.kill(1,0)"
                    "\nexcept PermissionError:denied.append('signal')"
                    "\nassert denied==['socket','signal'],denied"
                ),
            ],
            capture_output=True,
            text=True,
        )
        assert isolation.returncode == 0, isolation.stderr

        with DockerControlChannel(identity.container_id) as connection:
            peer = Peer(connection)
            peer.send(
                {
                    "schema_version": 1,
                    "control_sequence": 1,
                    "type": "SET_AUTHORITY_DEADLINE",
                    "payload": {
                        "source_callback_id": CALLBACK,
                        "deadline_monotonic_ns": 2**63 - 1,
                    },
                }
            )
            ack = peer.receive_ack(1)
            assert ack["accepted"] is True
            started = peer.receive_type("STARTED")
            assert started["payload"]["startup_nonce"] == request.startup_nonce
            assert started["payload"]["pid"] > 0
            peer.send(
                {
                    "schema_version": 1,
                    "ack_sequence": started["message_sequence"],
                    "accepted": True,
                    "code": "ACCEPTED",
                }
            )
            progress = peer.receive_type("PROGRESS")
            peer.send(
                {
                    "schema_version": 1,
                    "ack_sequence": progress["message_sequence"],
                    "accepted": True,
                    "code": "ACCEPTED",
                }
            )
            result_prepare = peer.receive_type("RESULT_PREPARE")
            completion_token = result_prepare["payload"]["completion_token"]
            peer.send(
                {
                    "schema_version": 1,
                    "control_sequence": 2,
                    "type": "PREPARE_RESULT",
                    "payload": {
                        "completion_token": completion_token,
                        "reservation_callback_id": CALLBACK,
                        "result_id": RESULT,
                    },
                }
            )
            assert peer.receive_ack(2)["accepted"] is True
            peer.send(
                {
                    "schema_version": 1,
                    "ack_sequence": result_prepare["message_sequence"],
                    "accepted": True,
                    "code": "ACCEPTED",
                }
            )
            batch = peer.receive_type("RESULT_FILE_BATCH")
            descriptor = batch["payload"]["artifacts"][0]
            binding = {**descriptor, "artifact_id": ARTIFACT}
            peer.send(
                {
                    "schema_version": 1,
                    "control_sequence": 3,
                    "type": "BIND_ARTIFACT_BATCH",
                    "payload": {
                        "purpose": "RESULT",
                        "reservation_callback_id": CALLBACK,
                        "reserved_id": RESULT,
                        "source_message_sequence": batch["message_sequence"],
                        "bindings": [binding],
                    },
                }
            )
            assert peer.receive_ack(3)["accepted"] is True
            binding_checksum = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps([binding], sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
            peer.send(
                {
                    "schema_version": 1,
                    "control_sequence": 4,
                    "type": "FINALIZE_RESULT_MANIFEST",
                    "payload": {
                        "completion_token": completion_token,
                        "reservation_callback_id": CALLBACK,
                        "result_id": RESULT,
                        "binding_set_checksum": binding_checksum,
                    },
                }
            )
            assert peer.receive_ack(4)["accepted"] is True
            ready = peer.receive_type("RESULT_READY")
            assert ready["payload"]["result_id"] == RESULT
            assert ready["payload"]["manifest"]["kind"] == "RESULT_MANIFEST"
            manifest_result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "1000:1000",
                    identity.container_id,
                    "cat",
                    "/output/result-manifest.json",
                ],
                check=True,
                capture_output=True,
            )
            manifest = json.loads(manifest_result.stdout)
            assert set(manifest) == {
                "kind",
                "schema_version",
                "result_id",
                "created_at",
                "provenance",
                "status",
                "files",
                "metrics",
                "manifest_checksum",
            }
            assert set(manifest["provenance"]) == {
                "tenant_id",
                "job_id",
                "session_id",
                "attempt_id",
                "job_fence",
                "input_checksum",
                "spec_checksum",
                "template_id",
                "template_version",
                "adapter_id",
                "adapter_version",
                "image_digest",
            }
            assert set(manifest["files"][0]) == {
                "artifact_id",
                "logical_name",
                "media_type",
                "size_bytes",
                "checksum",
            }
            assert ready["payload"]["manifest"]["checksum"] == (
                "sha256:" + hashlib.sha256(manifest_result.stdout).hexdigest()
            )

        observation = executor.inspect(identity)
        assert observation.identity == identity
        _evidence(
            "production-result-handshake",
            image=image,
            runtime_identity_digest=identity.runtime_identity_digest,
            message_types=[
                "STARTED",
                "PROGRESS",
                "RESULT_PREPARE",
                "RESULT_FILE_BATCH",
                "RESULT_READY",
            ],
            manifest_checksum=manifest["manifest_checksum"],
            manifest_artifact_checksum=ready["payload"]["manifest"]["checksum"],
            distinct_runner_uid=1000,
            distinct_workload_uid=1001,
        )
    finally:
        proof = executor.cleanup(identity)
        assert proof.proof_type == "CONTAINER_STOPPED"


@pytest.mark.docker
def test_production_container_denies_rootfs_input_network_and_runner_control(
    tmp_path: Path,
) -> None:
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in real Docker suite; use scripts/b09_docker_tests.sh --require")
    with _production_container(tmp_path) as (_, identity):
        probe = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "1001:1000",
                identity.container_id,
                "python",
                "-c",
                (
                    "import errno,os,socket;"
                    "denied=[];"
                    "\nfor name,path in "
                    "[('rootfs','/opt/nexa/probe'),('input','/input/input.json')]:"
                    "\n try:open(path,'wb').write(b'x')"
                    "\n except OSError as exc:"
                    "\n  assert exc.errno in {errno.EACCES,errno.EROFS},(name,exc.errno);"
                    "denied.append(name)"
                    "\ns=socket.socket();s.settimeout(0.5)"
                    "\ntry:s.connect(('1.1.1.1',53))"
                    "\nexcept OSError:denied.append('network')"
                    "\nu=socket.socket(socket.AF_UNIX)"
                    "\ntry:u.connect('/run/nexa/control.sock')"
                    "\nexcept PermissionError:denied.append('control')"
                    "\nassert not os.path.exists('/var/run/docker.sock')"
                    "\nassert denied==['rootfs','input','network','control'],denied"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert probe.returncode == 0, probe.stderr
        _evidence(
            "workload-isolation",
            denied=["rootfs-write", "input-write", "network", "control-socket", "runner-signal"],
            docker_socket_present=False,
            workload_uid=1001,
        )


@pytest.mark.docker
def test_production_container_enforces_cpu_pid_scratch_and_memory_bounds(tmp_path: Path) -> None:
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in real Docker suite; use scripts/b09_docker_tests.sh --require")
    with _production_container(
        tmp_path,
        cpu_millis=100,
        memory_bytes=128 * 1024 * 1024,
        scratch_bytes=1024 * 1024,
    ) as (_, identity):
        before_cpu = _read_cgroup_counters(identity.container_id, "/sys/fs/cgroup/cpu.stat")
        cpu_probe = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "1001:1000",
                identity.container_id,
                "python",
                "-c",
                "import time;d=time.monotonic()+1.5\nwhile time.monotonic()<d:pass",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert cpu_probe.returncode == 0, cpu_probe.stderr
        after_cpu = _read_cgroup_counters(identity.container_id, "/sys/fs/cgroup/cpu.stat")
        assert after_cpu["nr_throttled"] > before_cpu["nr_throttled"]
        assert after_cpu["throttled_usec"] > before_cpu["throttled_usec"]

        pid_probe = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "1001:1000",
                identity.container_id,
                "python",
                "-c",
                (
                    "import os,signal,time;children=[];limited=False"
                    "\ntry:"
                    "\n for _ in range(200):"
                    "\n  try:pid=os.fork()"
                    "\n  except OSError:limited=True;break"
                    "\n  if pid==0:time.sleep(5);os._exit(0)"
                    "\n  children.append(pid)"
                    "\n print(f'{limited}:{len(children)}',flush=True)"
                    "\nfinally:"
                    "\n for pid in children:"
                    "\n  try:os.kill(pid,signal.SIGKILL)"
                    "\n  except ProcessLookupError:pass"
                    "\n for pid in children:"
                    "\n  try:os.waitpid(pid,0)"
                    "\n  except ChildProcessError:pass"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert pid_probe.returncode == 0, pid_probe.stderr
        limited, count = pid_probe.stdout.strip().split(":")
        assert limited == "True"
        assert int(count) < 128

        scratch_probe = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "1001:1000",
                identity.container_id,
                "python",
                "-c",
                (
                    "import errno,os;fd=os.open('/tmp/limit-probe',os.O_CREAT|os.O_WRONLY,0o600);"
                    "hit=False"
                    "\ntry:"
                    "\n for _ in range(32):"
                    "\n  try:os.write(fd,b'x'*65536)"
                    "\n  except OSError as exc:assert exc.errno==errno.ENOSPC;hit=True;break"
                    "\nfinally:os.close(fd);os.unlink('/tmp/limit-probe')"
                    "\nassert hit"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert scratch_probe.returncode == 0, scratch_probe.stderr

        before_memory = _read_cgroup_counters(identity.container_id, "/sys/fs/cgroup/memory.events")
        memory_probe = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "1001:1000",
                identity.container_id,
                "python",
                "-c",
                "data=bytearray(256*1024*1024);data[::4096]=b'x'*(len(data)//4096)",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        after_memory = _read_cgroup_counters(identity.container_id, "/sys/fs/cgroup/memory.events")
        assert memory_probe.returncode != 0
        assert after_memory["oom_kill"] > before_memory["oom_kill"]
        _evidence(
            "resource-bounds",
            cpu_millis=100,
            cpu_nr_throttled_delta=after_cpu["nr_throttled"] - before_cpu["nr_throttled"],
            cpu_throttled_usec_delta=after_cpu["throttled_usec"] - before_cpu["throttled_usec"],
            pids_limit=128,
            child_count_before_limit=int(count),
            scratch_limit_bytes=1024 * 1024,
            scratch_result="ENOSPC",
            memory_limit_bytes=128 * 1024 * 1024,
            oom_kill_delta=after_memory["oom_kill"] - before_memory["oom_kill"],
        )


@pytest.mark.docker
def test_watchdog_stops_cpu_after_controller_disconnect_at_runtime_limit(tmp_path: Path) -> None:
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in real Docker suite; use scripts/b09_docker_tests.sh --require")
    with _production_container(tmp_path, runtime_limit_seconds=1) as (_, identity):
        connection = DockerControlChannel(identity.container_id)
        peer = Peer(connection)
        peer.send(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "SET_AUTHORITY_DEADLINE",
                "payload": {
                    "source_callback_id": CALLBACK,
                    "deadline_monotonic_ns": 2**63 - 1,
                },
            }
        )
        assert peer.receive_ack(1)["accepted"] is True
        peer.receive_type("STARTED")
        peer.receive_type("PROGRESS")
        connection.close()
        stop_observation = _wait_for_container_stop(identity.container_id)
        assert stop_observation <= 7
        _evidence(
            "controller-disconnect-runtime-watchdog",
            runtime_limit_seconds=1,
            container_stop_observation_seconds=round(stop_observation, 6),
            container_stop_observation_bound_seconds=7,
        )


@pytest.mark.docker
def test_runtime_watchdog_timeline_uses_runner_monotonic_clock(tmp_path: Path) -> None:
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in real Docker suite; use scripts/b09_docker_tests.sh --require")
    with (
        _production_container(tmp_path, runtime_limit_seconds=1) as (_, identity),
        DockerControlChannel(identity.container_id) as connection,
    ):
        peer = Peer(connection)
        peer.send(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "SET_AUTHORITY_DEADLINE",
                "payload": {
                    "source_callback_id": CALLBACK,
                    "deadline_monotonic_ns": 2**63 - 1,
                },
            }
        )
        assert peer.receive_ack(1)["accepted"] is True
        started = peer.receive_type("STARTED")
        peer.receive_type("PROGRESS")
        stopped = peer.receive_type("STOPPED", timeout=8)
        assert stopped["payload"]["reason"] == "RUNTIME_LIMIT"
        elapsed = (
            int(stopped["payload"]["stopped_monotonic_ns"])
            - int(started["payload"]["started_monotonic_ns"])
        ) / 1_000_000_000
        assert 0 <= elapsed <= 7
        _wait_for_container_stop(identity.container_id)
        _evidence(
            "runtime-watchdog-runner-clock",
            runtime_limit_seconds=1,
            stopped_after_started_seconds=round(elapsed, 6),
            clock_domain="runner-monotonic",
            bound_seconds=7,
        )


@pytest.mark.docker
def test_watchdog_survives_partial_ipc_pressure_until_authority_deadline(tmp_path: Path) -> None:
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in real Docker suite; use scripts/b09_docker_tests.sh --require")
    with _production_container(tmp_path) as (_, identity):
        guest_clock = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "1000:1000",
                identity.container_id,
                "python",
                "-c",
                "import time;print(time.monotonic_ns())",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        guest_now_ns = int(guest_clock.stdout)
        receive_delay_seconds = 2.0
        deadline_ns = guest_now_ns + 4_000_000_000
        time.sleep(receive_delay_seconds)
        with DockerControlChannel(identity.container_id) as connection:
            peer = Peer(connection)
            peer.send(
                {
                    "schema_version": 1,
                    "control_sequence": 1,
                    "type": "SET_AUTHORITY_DEADLINE",
                    "payload": {
                        "source_callback_id": CALLBACK,
                        "deadline_monotonic_ns": deadline_ns,
                    },
                }
            )
            assert peer.receive_ack(1)["accepted"] is True
            peer.receive_type("PROGRESS")
            connection.sendall(struct.pack("!I", 64 * 1024) + b"{")
            stopped = peer.receive_type("STOPPED", timeout=7)
            assert stopped["payload"]["reason"] == "LEASE_DEADLINE"
            elapsed = (
                int(stopped["payload"]["stopped_monotonic_ns"]) - deadline_ns
            ) / 1_000_000_000
            assert elapsed >= 0
            assert elapsed <= 7
            _wait_for_container_stop(identity.container_id)
            _evidence(
                "authority-deadline-under-partial-ipc",
                deadline_offset_seconds=4,
                host_receive_delay_seconds=receive_delay_seconds,
                stop_reason=stopped["payload"]["reason"],
                stopped_after_deadline_seconds=round(elapsed, 6),
                clock_domain="runner-monotonic",
                bound_seconds=7,
            )


@pytest.mark.docker
def test_watchdog_stops_workload_when_runner_log_bound_is_exceeded(tmp_path: Path) -> None:
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in real Docker suite; use scripts/b09_docker_tests.sh --require")
    with (
        _log_pressure_image(tmp_path) as image,
        _production_container(tmp_path / "run", image=image, log_bytes=64 * 1024) as (_, identity),
        DockerControlChannel(identity.container_id) as connection,
    ):
        peer = Peer(connection)
        peer.send(
            {
                "schema_version": 1,
                "control_sequence": 1,
                "type": "SET_AUTHORITY_DEADLINE",
                "payload": {
                    "source_callback_id": CALLBACK,
                    "deadline_monotonic_ns": 2**63 - 1,
                },
            }
        )
        assert peer.receive_ack(1)["accepted"] is True
        peer.receive_type("STARTED")
        trigger_ns = _read_container_integer(identity.container_id, "/output/log-trigger.ns")
        peer.receive_type("PROGRESS")
        stopped = peer.receive_type("STOPPED", timeout=7)
        assert stopped["payload"]["reason"] == "FAILURE"
        elapsed = (int(stopped["payload"]["stopped_monotonic_ns"]) - trigger_ns) / 1_000_000_000
        assert elapsed >= 0
        assert elapsed <= 7
        _wait_for_container_stop(identity.container_id)
        _evidence(
            "runner-log-bound",
            log_limit_bytes=64 * 1024,
            emitted_bytes=128 * 1024,
            stop_reason=stopped["payload"]["reason"],
            stopped_after_trigger_seconds=round(elapsed, 6),
            clock_domain="runner-monotonic",
            bound_seconds=7,
        )
