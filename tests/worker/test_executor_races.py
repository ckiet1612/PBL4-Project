import hashlib
from dataclasses import replace

import pytest

from nexa.worker.docker_client import DockerCommandBackend
from nexa.worker.errors import ExecutorError
from nexa.worker.executor import DockerExecutor
from nexa.worker.journal import ExecutionJournal
from nexa.worker.models import CpuWorkloadSpec, InputMount
from tests.worker.test_docker_config import start
from tests.worker.test_executor import FakeDocker


class TimeoutAfterCreateDocker(DockerCommandBackend):
    def __init__(self) -> None:
        self.create_calls = 0

    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        if argv[1] == "create":
            self.create_calls += 1
            raise TimeoutError("late Docker response")
        return 1, b"", b"not found"


class NoContainerDocker(DockerCommandBackend):
    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        if argv[1] == "ps":
            return 0, b"", b""
        return 1, b"", b"not found"


def test_create_timeout_keeps_inflight_and_does_not_retry_blindly(tmp_path) -> None:
    docker = TimeoutAfterCreateDocker()
    executor = DockerExecutor(ExecutionJournal(tmp_path), docker, image_ref="registry.invalid/cpu")
    prepared = executor.prepare(start())
    with pytest.raises(ExecutorError) as first:
        executor.start(prepared)
    assert first.value.code == "CREATE_OUTCOME_UNKNOWN"
    with pytest.raises(ExecutorError) as second:
        executor.start(prepared)
    assert second.value.code == "CREATE_OUTCOME_UNKNOWN"
    assert docker.create_calls == 1


def test_precreate_tombstone_returns_no_container_proof(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path)
    executor = DockerExecutor(journal, NoContainerDocker(), image_ref="registry.invalid/cpu")
    request = start()
    executor.prepare(request)
    proof = executor.tombstone_unclaimed(request, reason="PRE_CREATE_STOP")
    assert proof.proof_type == "NO_CONTAINER"
    assert proof.tombstone_sequence >= 1
    with pytest.raises(ExecutorError, match="tombstoned"):
        executor.start(executor.prepare(request))


def test_tombstone_rejects_request_identity_mismatch_without_mutation(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path)
    executor = DockerExecutor(journal, NoContainerDocker(), image_ref="registry.invalid/cpu")
    original = start()
    executor.prepare(original)
    changed_nonce = "018f0d60-7b6a-7a29-9d82-1aa39c4f30b7"
    changed = replace(
        original,
        startup_nonce=changed_nonce,
        context=replace(original.context, startup_nonce=changed_nonce),
    )

    with pytest.raises(ExecutorError) as error:
        executor.tombstone_unclaimed(changed, reason="PRE_CREATE_STOP")

    assert error.value.code == "IDENTITY_MISMATCH"
    record = journal.load(original.context.authority.attempt_id)
    assert record.state == "PREPARED"
    assert record.startup_nonce == original.startup_nonce


def test_tombstone_does_not_claim_no_container_while_create_is_inflight(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path)
    executor = DockerExecutor(journal, TimeoutAfterCreateDocker(), image_ref="registry.invalid/cpu")
    request = start()
    executor.prepare(request)
    record = journal.load(request.context.authority.attempt_id)
    journal.begin_create(
        request.context.authority.attempt_id, expected_sequence=record.operation_sequence
    )
    with pytest.raises(ExecutorError) as error:
        executor.tombstone_unclaimed(request, reason="PRE_CREATE_STOP")
    assert error.value.code == "CREATE_OUTCOME_UNKNOWN"


def test_unclaimed_without_local_record_creates_sequence_one_tombstone(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path)
    executor = DockerExecutor(journal, NoContainerDocker(), image_ref="registry.invalid/cpu")
    request = start()
    proof = executor.tombstone_unclaimed(request, reason="SERVER_CONFIRMED_UNCLAIMED")
    assert proof.tombstone_sequence == 1
    assert journal.load(request.context.authority.attempt_id).state == "TOMBSTONED"


class UnavailableDocker(NoContainerDocker):
    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        raise RuntimeError("daemon unavailable")


def test_unclaimed_does_not_emit_proof_when_docker_is_unavailable(tmp_path) -> None:
    executor = DockerExecutor(
        ExecutionJournal(tmp_path), UnavailableDocker(), image_ref="registry.invalid/cpu"
    )
    with pytest.raises(ExecutorError) as error:
        executor.tombstone_unclaimed(start(), reason="SERVER_CONFIRMED_UNCLAIMED")
    assert error.value.code == "INSPECTION_UNAVAILABLE"


class AdvancingClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class BudgetDocker(NoContainerDocker):
    def __init__(self, clock: AdvancingClock) -> None:
        self.clock = clock
        self.timeouts: list[float] = []
        self.container_id = "a" * 64

    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        import json

        self.timeouts.append(timeout_seconds)
        self.clock.value += 16.0
        if argv[1] == "create":
            return 0, (self.container_id + "\n").encode(), b""
        if argv[1] == "inspect":
            payload = {
                "Id": self.container_id,
                "Image": "sha256:" + "a" * 64,
                "Config": {
                    "User": "1000:1000",
                    "Labels": {
                        "nexa.managed": "true",
                        "nexa.attempt_id": start().context.authority.attempt_id,
                        "nexa.allocation_id": start().context.authority.allocation_id,
                        "nexa.startup_nonce": start().startup_nonce,
                    },
                },
                "HostConfig": {},
                "State": {
                    "Running": False,
                    "Status": "created",
                    "ExitCode": 0,
                    "OOMKilled": False,
                },
            }
            return 0, json.dumps([payload]).encode(), b""
        raise AssertionError(argv)


def test_startup_commands_share_one_thirty_second_budget(tmp_path) -> None:
    clock = AdvancingClock()
    docker = BudgetDocker(clock)
    executor = DockerExecutor(
        ExecutionJournal(tmp_path), docker, image_ref="registry.invalid/cpu", monotonic=clock
    )
    with pytest.raises(ExecutorError) as error:
        executor.start(executor.prepare(start()))
    assert error.value.code == "START_TIMEOUT"
    assert len(docker.timeouts) == 2
    assert docker.timeouts[1] < docker.timeouts[0]


class TimeoutAfterAppliedStartDocker(FakeDocker):
    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        if argv[1] == "start":
            self.started.append(argv[-1])
            raise TimeoutError("start response lost")
        return super().run(argv, timeout_seconds)


def test_cleanup_reconciles_exact_container_after_start_timeout(tmp_path) -> None:
    clock = AdvancingClock()
    docker = TimeoutAfterAppliedStartDocker()
    journal = ExecutionJournal(tmp_path)
    executor = DockerExecutor(
        journal,
        docker,
        image_ref="registry.invalid/cpu",
        monotonic=clock,
        clock_domain="boot-a",
    )
    prepared = executor.prepare(start())
    with pytest.raises(ExecutorError) as error:
        executor.start(prepared)
    assert error.value.code == "START_OUTCOME_UNKNOWN"
    record = journal.load(start().context.authority.attempt_id)
    assert record.state == "START_IN_FLIGHT"
    assert record.container is not None
    clock.value = 31.0

    proof = executor.cleanup(record.container)

    assert proof.proof_type == "CONTAINER_STOPPED"
    assert docker.stopped == [record.container.container_id]
    assert docker.removed == [record.container.container_id]


class TimeoutAfterAppliedSupervisorExecDocker(FakeDocker):
    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        if argv[1] == "exec":
            self.execs.append(argv)
            if len(self.execs) == 1:
                raise TimeoutError("supervisor exec response lost")
            return 0, b"", b""
        return super().run(argv, timeout_seconds)


def test_running_inflight_container_never_reexecs_unknown_supervisor_start(tmp_path) -> None:
    docker = TimeoutAfterAppliedSupervisorExecDocker()
    journal = ExecutionJournal(tmp_path)
    source = tmp_path / "input.json"
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
        journal,
        docker,
        image_ref="registry.invalid/cpu",
        staging_root=tmp_path,
        monotonic=lambda: 1.0,
        clock_domain="boot-a",
    )
    prepared = executor.prepare(request)
    with pytest.raises(ExecutorError) as initial:
        executor.start(prepared)
    assert initial.value.code == "START_OUTCOME_UNKNOWN"
    assert journal.load(request.context.authority.attempt_id).state == "START_IN_FLIGHT"
    assert len(docker.execs) == 1

    with pytest.raises(ExecutorError) as replay:
        executor.start(prepared)

    assert replay.value.code == "START_OUTCOME_UNKNOWN"
    assert len(docker.execs) == 1


def test_startup_timestamp_reused_only_in_same_clock_domain(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path)
    request = start()
    journal.prepare(
        attempt_id=request.context.authority.attempt_id,
        allocation_id=request.allocation.allocation_id,
        startup_nonce=request.startup_nonce,
        authority=request.context.authority,
        resources=request.context.resources,
        image_digest=request.context.image_digest,
        input_checksum=request.context.input_checksum,
        execution_binding={"request": "fixed"},
    )
    prepared = journal.load(request.context.authority.attempt_id)
    recorded = journal.begin_create(
        request.context.authority.attempt_id,
        expected_sequence=prepared.operation_sequence,
        started_monotonic_ns=100_000_000_000,
        clock_domain="boot-a",
    )
    same = DockerExecutor(
        journal,
        NoContainerDocker(),
        image_ref="registry.invalid/cpu",
        monotonic=lambda: 110.0,
        clock_domain="boot-a",
    )
    changed = DockerExecutor(
        journal,
        NoContainerDocker(),
        image_ref="registry.invalid/cpu",
        monotonic=lambda: 0.0,
        clock_domain="boot-b",
    )

    assert same._remaining(recorded, request) == 20.0
    assert changed._remaining(recorded, request) == 30.0


class TimeoutBeforeAppliedStartDocker(FakeDocker):
    def __init__(self) -> None:
        super().__init__()
        self.start_attempts = 0

    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        if argv[1] == "start":
            self.start_attempts += 1
            raise TimeoutError("start outcome unknown")
        return super().run(argv, timeout_seconds)


def test_clock_domain_change_inspects_but_does_not_restart_inflight_container(tmp_path) -> None:
    docker = TimeoutBeforeAppliedStartDocker()
    journal = ExecutionJournal(tmp_path)
    first = DockerExecutor(
        journal,
        docker,
        image_ref="registry.invalid/cpu",
        monotonic=lambda: 100.0,
        clock_domain="boot-a",
    )
    prepared = first.prepare(start())
    with pytest.raises(ExecutorError) as initial:
        first.start(prepared)
    assert initial.value.code == "START_OUTCOME_UNKNOWN"
    assert docker.start_attempts == 1

    restarted = DockerExecutor(
        journal,
        docker,
        image_ref="registry.invalid/cpu",
        monotonic=lambda: 0.0,
        clock_domain="boot-b",
    )
    with pytest.raises(ExecutorError) as replay:
        restarted.start(prepared)

    assert replay.value.code == "START_OUTCOME_UNKNOWN"
    assert docker.start_attempts == 1


class FailStartThenRecoverDocker(FakeDocker):
    def __init__(self) -> None:
        super().__init__()
        self.fail_start = True
        self.inspect_calls = 0

    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        if argv[1] == "inspect":
            self.inspect_calls += 1
        if argv[1] == "start" and self.fail_start:
            self.fail_start = False
            return 1, b"", b"start failed"
        return super().run(argv, timeout_seconds)


def test_clock_domain_change_does_not_restart_created_container(tmp_path) -> None:
    docker = FailStartThenRecoverDocker()
    journal = ExecutionJournal(tmp_path)
    first = DockerExecutor(
        journal,
        docker,
        image_ref="registry.invalid/cpu",
        monotonic=lambda: 100.0,
        clock_domain="boot-a",
    )
    prepared = first.prepare(start())
    with pytest.raises(ExecutorError) as initial:
        first.start(prepared)
    assert initial.value.code == "RUNTIME_ERROR"
    record = journal.load(start().context.authority.attempt_id)
    assert record.state == "CREATED"
    assert record.container is not None
    inspections_before_replay = docker.inspect_calls

    restarted = DockerExecutor(
        journal,
        docker,
        image_ref="registry.invalid/cpu",
        monotonic=lambda: 0.0,
        clock_domain="boot-b",
    )
    with pytest.raises(ExecutorError) as replay:
        restarted.start(prepared)

    assert replay.value.code == "START_OUTCOME_UNKNOWN"
    assert docker.inspect_calls == inspections_before_replay
    assert docker.started == []
    assert docker.execs == []
    assert restarted.inspect(record.container).identity == record.container
    assert restarted.cleanup(record.container).proof_type == "CONTAINER_STOPPED"
