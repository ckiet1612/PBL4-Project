import hashlib
import json
from dataclasses import replace

import pytest

from nexa.worker.docker_client import DockerCommandBackend
from nexa.worker.errors import ExecutorError
from nexa.worker.executor import DockerExecutor
from nexa.worker.journal import ExecutionJournal, JournalWriteError
from nexa.worker.models import CpuWorkloadSpec, InputMount
from tests.worker.test_docker_config import start


class FakeDocker(DockerCommandBackend):
    def __init__(self) -> None:
        self.created = 0
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.execs: list[tuple[str, ...]] = []
        self.container_id = "a" * 64

    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        if argv[1] == "create":
            self.created += 1
            return 0, (self.container_id + "\n").encode(), b""
        if argv[1] == "start":
            self.started.append(argv[-1])
            return 0, b"", b""
        if argv[1] == "exec":
            self.execs.append(argv)
            return 0, b"", b""
        if argv[1] == "inspect":
            if self.container_id in self.removed:
                return 1, b"", f"Error: No such object: {self.container_id}".encode()
            payload = {
                "Id": self.container_id,
                "Config": {
                    "Labels": {
                        "nexa.managed": "true",
                        "nexa.attempt_id": start().context.authority.attempt_id,
                        "nexa.allocation_id": start().context.authority.allocation_id,
                        "nexa.startup_nonce": start().startup_nonce,
                    }
                },
                "Image": "sha256:" + "a" * 64,
                "State": {
                    "Running": bool(self.started) and self.container_id not in self.stopped,
                    "Status": "running"
                    if self.started and self.container_id not in self.stopped
                    else "created",
                    "ExitCode": 0,
                    "OOMKilled": False,
                },
                "HostConfig": {
                    "ReadonlyRootfs": True,
                    "NetworkMode": "none",
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "PidsLimit": 128,
                    "Memory": 256 * 1024 * 1024,
                    "MemorySwap": 256 * 1024 * 1024,
                    "NanoCpus": 1_000_000_000,
                    "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=67108864"},
                    "LogConfig": {"Type": "json-file", "Config": {"max-file": "1"}},
                    "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                    "OomKillDisable": None,
                },
            }
            return 0, json.dumps([payload]).encode(), b""
        if argv[1] == "ps":
            return 0, b"", b""
        if argv[1] == "stop":
            self.stopped.append(argv[-1])
            return 0, b"", b""
        if argv[1] == "rm":
            self.removed.append(argv[-1])
            return 0, b"", b""
        return 0, b"", b""


def test_prepare_start_is_idempotent_for_exact_identity(tmp_path) -> None:
    docker = FakeDocker()
    executor = DockerExecutor(ExecutionJournal(tmp_path), docker, image_ref="registry.invalid/cpu")
    request = start()
    prepared = executor.prepare(request)
    identity = executor.start(prepared)
    replay = executor.start(executor.prepare(request))
    assert identity == replay
    assert docker.created == 1
    assert docker.started == ["a" * 64]


def test_start_execs_non_root_supervisor_by_exact_container_id(tmp_path) -> None:
    docker = FakeDocker()
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "input.json"
    payload = b'{"initial_value":17}'
    source.write_bytes(payload)
    checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
    request = replace(
        start(),
        context=replace(start().context, input_checksum=checksum),
        input_mounts=(
            InputMount(
                artifact_id="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
                source_path=str(source),
                target_path="/input/input.json",
                content_checksum=checksum,
                size_bytes=len(payload),
            ),
        ),
        cpu_workload=CpuWorkloadSpec(
            iterations=1,
            seed=7,
            modulus=101,
            spec_checksum="sha256:" + "f" * 64,
        ),
    )
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "journal"),
        docker,
        image_ref="registry.invalid/cpu",
        staging_root=staging,
    )

    identity = executor.start(executor.prepare(request))

    assert len(docker.execs) == 1
    command = docker.execs[0]
    assert command[:7] == (
        "docker",
        "exec",
        "--detach",
        "--user",
        "1001:1000",
        identity.container_id,
        "python",
    )
    assert command[7:14] == (
        "-m",
        "nexa.workloads.workload_supervisor",
        "--registration-socket",
        "/run/nexa/supervisor-register.sock",
        "--log-limit",
        str(request.log_bytes),
        "--",
    )
    assert command[14:17] == ("python", "-m", "nexa.workloads.cpu_entrypoint")


def test_changed_context_is_rejected(tmp_path) -> None:
    docker = FakeDocker()
    executor = DockerExecutor(ExecutionJournal(tmp_path), docker, image_ref="registry.invalid/cpu")
    request = start()
    executor.prepare(request)
    with pytest.raises(Exception, match="mismatch"):
        changed_context = replace(request.context, input_checksum="sha256:" + "c" * 64)
        executor.prepare(replace(request, context=changed_context))


def test_cleanup_matches_exact_identity(tmp_path) -> None:
    docker = FakeDocker()
    executor = DockerExecutor(ExecutionJournal(tmp_path), docker, image_ref="registry.invalid/cpu")
    identity = executor.start(executor.prepare(start()))
    proof = executor.cleanup(identity)
    assert proof.proof_type == "CONTAINER_STOPPED"
    assert proof.container == identity
    assert docker.removed == [identity.container_id]
    replay = executor.cleanup(identity)
    assert replay.proof_type == "CONTAINER_STOPPED"
    assert replay.container == identity


def test_cleanup_rejects_identity_not_bound_in_journal(tmp_path) -> None:
    docker = FakeDocker()
    executor = DockerExecutor(ExecutionJournal(tmp_path), docker, image_ref="registry.invalid/cpu")
    identity = executor.start(executor.prepare(start()))
    wrong = replace(identity, container_id="b" * 64)
    with pytest.raises(ExecutorError, match="identity"):
        executor.cleanup(wrong)
    assert docker.stopped == []
    assert docker.removed == []


def test_stop_rejects_identity_not_bound_in_journal(tmp_path) -> None:
    docker = FakeDocker()
    executor = DockerExecutor(ExecutionJournal(tmp_path), docker, image_ref="registry.invalid/cpu")
    identity = executor.start(executor.prepare(start()))
    wrong = replace(identity, container_id="b" * 64)
    with pytest.raises(ExecutorError, match="identity"):
        executor.stop(wrong)
    assert docker.stopped == []


class StartFailsOnceDocker(FakeDocker):
    def __init__(self) -> None:
        super().__init__()
        self.fail_start = True

    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        if argv[1] == "start" and self.fail_start:
            self.fail_start = False
            return 1, b"", b"start failed"
        return super().run(argv, timeout_seconds)


def test_start_failure_replay_does_not_report_bound_identity_as_started(tmp_path) -> None:
    docker = StartFailsOnceDocker()
    executor = DockerExecutor(ExecutionJournal(tmp_path), docker, image_ref="registry.invalid/cpu")
    prepared = executor.prepare(start())
    with pytest.raises(ExecutorError, match="start"):
        executor.start(prepared)
    identity = executor.start(prepared)
    assert identity.container_id == docker.container_id
    assert docker.started == [docker.container_id]
    assert executor.journal.load(identity.attempt_id).state == "STARTED"


def test_runtime_identity_ignores_mutable_docker_inspect_fields(tmp_path) -> None:
    docker = FakeDocker()
    executor = DockerExecutor(ExecutionJournal(tmp_path), docker, image_ref="registry.invalid/cpu")
    identity = executor.start(executor.prepare(start()))
    assert executor.inspect(identity).identity == identity


def test_input_descriptor_is_materialized_before_start(tmp_path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "input.json"
    expected = b'{"initial_value":17}'
    source.write_bytes(expected)
    checksum = "sha256:" + hashlib.sha256(expected).hexdigest()
    mount = InputMount(
        artifact_id="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
        source_path=str(source),
        target_path="/input/input.json",
        content_checksum=checksum,
        size_bytes=len(expected),
    )
    request = start()
    request = replace(
        request,
        context=replace(request.context, input_checksum=checksum),
        input_mounts=(mount,),
    )
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "journal"),
        FakeDocker(),
        image_ref="registry.invalid/cpu",
        staging_root=staging,
    )
    prepared = executor.prepare(request)
    pinned = prepared.request.input_mounts[0].source_path
    assert pinned != str(source)
    source.write_bytes(b"substituted")
    executor.start(prepared)
    with open(pinned, "rb") as handle:
        assert handle.read() == expected


@pytest.mark.parametrize("mode", ["symlink", "missing", "checksum"])
def test_input_verification_fails_closed(tmp_path, mode) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    real = staging / "real.json"
    real.write_bytes(b"x")
    source = staging / "input.json"
    checksum = "sha256:" + hashlib.sha256(b"x").hexdigest()
    if mode == "symlink":
        source.symlink_to(real)
    elif mode == "missing":
        source = staging / "missing.json"
    else:
        source = real
        checksum = "sha256:" + "f" * 64
    mount = InputMount(
        artifact_id="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
        source_path=str(source),
        target_path="/input/input.json",
        content_checksum=checksum,
        size_bytes=1,
    )
    request = start()
    request = replace(
        request,
        context=replace(request.context, input_checksum=checksum),
        input_mounts=(mount,),
    )
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "journal"),
        FakeDocker(),
        image_ref="registry.invalid/cpu",
        staging_root=staging,
    )
    with pytest.raises(ExecutorError, match="input"):
        executor.prepare(request)


def test_input_verification_rejects_ancestor_symlink_escape(tmp_path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = b"outside"
    (outside / "input.json").write_bytes(payload)
    (staging / "link").symlink_to(outside, target_is_directory=True)
    checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
    mount = InputMount(
        artifact_id="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
        source_path=str(staging / "link" / "input.json"),
        target_path="/input/input.json",
        content_checksum=checksum,
        size_bytes=len(payload),
    )
    request = replace(
        start(),
        context=replace(start().context, input_checksum=checksum),
        input_mounts=(mount,),
    )
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "journal"),
        FakeDocker(),
        image_ref="registry.invalid/cpu",
        staging_root=staging,
    )

    with pytest.raises(ExecutorError, match="input"):
        executor.prepare(request)


def test_cpu_primary_mount_checksum_must_match_authorized_context(tmp_path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "input.json"
    payload = b"x"
    source.write_bytes(payload)
    checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
    mount = InputMount(
        artifact_id="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
        source_path=str(source),
        target_path="/input/input.json",
        content_checksum=checksum,
        size_bytes=1,
    )
    request = replace(
        start(),
        input_mounts=(mount,),
        cpu_workload=CpuWorkloadSpec(
            iterations=1,
            seed=1,
            modulus=101,
            spec_checksum="sha256:" + "f" * 64,
        ),
    )
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "journal"),
        FakeDocker(),
        image_ref="registry.invalid/cpu",
        staging_root=staging,
    )

    with pytest.raises(ExecutorError, match="checksum"):
        executor.prepare(request)


def test_oversized_input_stops_after_declared_size_plus_one_byte(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "input.json"
    source.write_bytes(b"x" * (256 * 1024))
    mount = InputMount(
        artifact_id="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
        source_path=str(source),
        target_path="/input/input.json",
        content_checksum="sha256:" + hashlib.sha256(b"x").hexdigest(),
        size_bytes=1,
    )
    request = replace(
        start(),
        context=replace(start().context, input_checksum=mount.content_checksum),
        input_mounts=(mount,),
    )
    real_read = __import__("os").read
    bytes_read = 0

    def tracked_read(descriptor: int, maximum: int) -> bytes:
        nonlocal bytes_read
        chunk = real_read(descriptor, maximum)
        bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr("nexa.worker.executor.os.read", tracked_read)
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "journal"),
        FakeDocker(),
        image_ref="registry.invalid/cpu",
        staging_root=staging,
    )

    with pytest.raises(ExecutorError, match="input"):
        executor.prepare(request)

    assert bytes_read <= 2


class FailFinishOnceJournal(ExecutionJournal):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.fail_finish = True

    def finish_cleanup(self, attempt_id: str, *, expected_sequence: int):
        if self.fail_finish:
            self.fail_finish = False
            raise JournalWriteError("injected tombstone write failure")
        return super().finish_cleanup(attempt_id, expected_sequence=expected_sequence)


def test_cleanup_recovers_after_remove_then_journal_write_failure(tmp_path) -> None:
    docker = FakeDocker()
    failing = FailFinishOnceJournal(tmp_path)
    executor = DockerExecutor(failing, docker, image_ref="registry.invalid/cpu")
    identity = executor.start(executor.prepare(start()))
    with pytest.raises(ExecutorError, match="injected"):
        executor.cleanup(identity)
    assert docker.removed == [identity.container_id]
    record = ExecutionJournal(tmp_path).load(identity.attempt_id)
    assert record.state == "CLEANUP_IN_FLIGHT"

    recovered = DockerExecutor(
        ExecutionJournal(tmp_path), docker, image_ref="registry.invalid/cpu"
    ).cleanup(identity)
    assert recovered.proof_type == "CONTAINER_STOPPED"
    assert ExecutionJournal(tmp_path).load(identity.attempt_id).state == "TOMBSTONED"


def test_pinned_image_reference_is_used_once_and_matches_claimed_digest(tmp_path):
    import pytest

    from nexa.worker.errors import ExecutorError
    from tests.worker.test_docker_config import start

    request = start()
    image = f"registry.invalid/cpu@{request.context.image_digest}"
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "matching"),
        object(),
        image_ref=image,
        staging_root=tmp_path / "staging",
    )
    assert executor.prepare(request).config.image == image

    changed = f"registry.invalid/cpu@sha256:{'0' * 64}"
    mismatch = DockerExecutor(
        ExecutionJournal(tmp_path / "mismatch"),
        object(),
        image_ref=changed,
        staging_root=tmp_path / "staging-mismatch",
    )
    with pytest.raises(ExecutorError, match="image digest"):
        mismatch.prepare(request)


def test_prepared_launch_spec_is_readable_by_non_root_runner(tmp_path):
    import os
    import stat
    from dataclasses import replace

    from nexa.worker.models import CpuWorkloadSpec
    from tests.worker.test_docker_config import start

    request = start()
    source = tmp_path / "staging" / "input.json"
    source.parent.mkdir()
    payload = b'{"initial_value":17}'
    source.write_bytes(payload)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    request = replace(
        request,
        context=replace(request.context, input_checksum=digest),
        input_mounts=(
            InputMount(
                artifact_id="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
                source_path=str(source),
                target_path="/input/input.json",
                content_checksum=digest,
                size_bytes=len(payload),
            ),
        ),
        cpu_workload=CpuWorkloadSpec(
            iterations=2, seed=1, modulus=101, spec_checksum="sha256:" + "f" * 64
        ),
    )
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "journal"),
        object(),
        image_ref="registry.invalid/cpu",
        staging_root=tmp_path / "staging",
    )
    prepared = executor.prepare(request)
    directory = prepared.config.control_dir
    assert directory is not None
    assert stat.S_IMODE(os.stat(directory).st_mode) & 0o001
    assert stat.S_IMODE(os.stat(directory + "/launch-spec.json").st_mode) & 0o004
