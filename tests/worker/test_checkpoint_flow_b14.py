"""B14 worker checkpoint cycle, restore launch and adoption reconcile without Docker.

The runner is the real trusted runner and the worker is the production agent loop;
only Docker and the HTTP API are faked. The API fake replays committed answers by
callback/upload key like the server and checks manifests with the server validator.
"""

import hashlib
import io
import json
import logging
import tarfile
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import rfc8785
from hypothesis import given, settings
from hypothesis import strategies as st

from nexa.application.checkpoint_validation import (
    checkpoint_provenance,
    expected_compatibility,
    validate_checkpoint_manifest,
    validate_cpu_state,
)
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.worker import dispatch
from nexa.worker.agent import WorkerAgent
from nexa.worker.checkpoint_flow import CheckpointFlow, reconcile_adoption
from nexa.worker.client import WorkerApiClient, WorkerApiError
from nexa.worker.docker_client import DockerCli
from nexa.worker.errors import ExecutorError, ExecutorErrorCode
from nexa.worker.executor import DockerExecutor, _execution_binding, _supervisor_command
from nexa.worker.journal import ExecutionJournal
from nexa.worker.models import RESTORE_STATE_PATH, Authority, CpuCheckpointLaunch
from nexa.worker.protocol import FrameDecoder, encode_frame
from nexa.worker.result_flow import checksum
from nexa.worker.state import PendingOperationStore
from nexa.workloads.cpu_state import CpuState, encode_state
from nexa.workloads.trusted_runner import RunnerState, RunnerSupervisor
from tests.worker.test_docker_config import start
from tests.worker.test_executor import FakeDocker

INPUT = b'{"initial_value":17}'
INPUT_ARTIFACT = "018f0d60-7b6a-7a40-9d82-1aa39c4f30b7"
ITERATIONS = 50
MODULUS = 1_000_003
ARCH = "linux/amd64"
LABELS = {"io.nexa.runner.checkpoint": "cpu-state-v1"}
REQUIREMENT = {
    "architectures": [ARCH],
    "device": "CPU",
    "framework": "NEXA_CPU",
    "framework_version": "1.0.0",
    "cuda_runtime_min": None,
    "driver_min": None,
    "compute_capability_min": None,
}
SECOND = 1_000_000_000


def claim_context(
    *, checkpointable=True, restart_safe=False, restore=None, interval=5, requirement=None
):
    requested = start()
    artifact = {
        "artifact_id": INPUT_ARTIFACT,
        "tenant_id": requested.context.tenant_id,
        "kind": "INPUT",
        "checksum": "sha256:" + hashlib.sha256(INPUT).hexdigest(),
        "size_bytes": len(INPUT),
        "media_type": "application/vnd.nexa.cpu-iterative-input+json",
        "state": "COMMITTED",
    }
    return {
        "authority": asdict(requested.context.authority),
        "job_id": requested.context.job_id,
        "logical_session_id": requested.context.logical_session_id,
        "spec": {
            "template_id": "cpu-iterative",
            "template_version": 1,
            "input_artifact_id": INPUT_ARTIFACT,
            "parameters": {"iterations": ITERATIONS, "seed": 1, "modulus": MODULUS},
            "runtime_limit_seconds": 300,
            "checkpoint_interval_seconds": interval,
        },
        "execution_intent": "RUN",
        "restore_checkpoint": restore,
        "template_snapshot": {
            "checkpointable": checkpointable,
            "restart_safe": restart_safe,
            "capability_requirement": requirement or REQUIREMENT,
        },
        "adapter_id": "cpu.iterative",
        "adapter_version": "1.0.0",
        "image_digest": requested.context.image_digest,
        "startup_nonce": requested.startup_nonce,
        "allocation": {"resources": asdict(requested.context.resources)},
        "input_artifacts": [artifact],
    }


def state_bytes(context, step, accumulator):
    return encode_state(
        CpuState(
            step=step,
            accumulator=accumulator,
            input_checksum=context["input_artifacts"][0]["checksum"],
            spec_checksum=checksum(context["spec"]),
        )
    )


def server_provenance(context, attempt_id, fence):
    spec = context["spec"]
    return checkpoint_provenance(
        job={"tenant_id": context["input_artifacts"][0]["tenant_id"], "job_id": context["job_id"]},
        spec={**spec, "spec_checksum": checksum(spec)},
        template={
            "adapter_id": context["adapter_id"],
            "adapter_version": context["adapter_version"],
            "image_digest": context["image_digest"],
        },
        session_id=context["logical_session_id"],
        input_checksum=context["input_artifacts"][0]["checksum"],
        attempt_id=attempt_id,
        fence=fence,
    )


def server_compatibility(context):
    snapshot = context["template_snapshot"]
    return expected_compatibility(
        {
            "capability_requirements": snapshot["capability_requirement"],
            "checkpointable": snapshot["checkpointable"],
            "restart_safe": snapshot["restart_safe"],
        },
        ARCH,
    )


def sealed_restore(context, *, step=20, accumulator=555, sequence=1, raw=None):
    """A committed checkpoint exactly as the claim transaction freezes it."""
    raw = state_bytes(context, step, accumulator) if raw is None else raw
    tenant = context["input_artifacts"][0]["tenant_id"]
    state_file = {
        "artifact_id": str(new_uuid7()),
        "tenant_id": tenant,
        "kind": "CHECKPOINT_FILE",
        "media_type": "application/json",
        "size_bytes": len(raw),
        "checksum": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "state": "COMMITTED",
    }
    checkpoint_id = str(new_uuid7())
    body = {
        "kind": "CHECKPOINT",
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "checkpoint_sequence": sequence,
        "created_at": "2026-09-26T00:00:00.000Z",
        "provenance": server_provenance(context, str(new_uuid7()), 1),
        "compatibility": server_compatibility(context),
        "cursor": {"step": step, "epoch": 0, "item_cursor": step, "accumulator": accumulator},
        "state_components": ["ACCUMULATOR"],
        "files": [
            {
                "logical_name": "state.json",
                "media_type": "application/json",
                "size_bytes": len(raw),
                "checksum": state_file["checksum"],
                "artifact_id": state_file["artifact_id"],
            }
        ],
    }
    manifest = {**body, "manifest_checksum": checksum(body)}
    record = {
        "checkpoint_id": checkpoint_id,
        "job_id": context["job_id"],
        "attempt_id": body["provenance"]["attempt_id"],
        "sequence": sequence,
        "manifest_artifact_id": str(new_uuid7()),
        "manifest_checksum": "sha256:" + hashlib.sha256(rfc8785.dumps(manifest)).hexdigest(),
        "state": "COMMITTED",
        "created_at": "2026-09-26T00:00:00.000Z",
    }
    return {"record": record, "manifest": manifest, "files": [state_file]}, raw


class Lost(TimeoutError):
    """The server committed the effect but the response never arrived."""


class Api:
    """HTTP API fake: callback replays and upload keys return the committed answer."""

    def __init__(self, context, *, blobs=None):
        self.context = context
        self.blobs = dict(blobs or {})
        self.artifacts = {}
        self.reservations = {}
        self.uploads = {}
        self.published = {}
        self.sequence = 0
        self.calls = []
        self.lose = set()
        self.reserve_status = None
        self.publish_status = None
        self.results = []
        self.failures = []
        self.cleanups = []

    def _maybe_lose(self, name):
        if name in self.lose:
            self.lose.discard(name)
            raise Lost(f"{name} response lost after commit")

    def claim(self, attempt, callback, body):
        return {"accepted": True, "callback_id": callback, "execution_context": self.context}

    def download_execution(self, authority, artifact, path):
        path.write_bytes(
            INPUT
            if artifact["artifact_id"] == INPUT_ARTIFACT
            else self.blobs[artifact["artifact_id"]]
        )
        self.calls.append(("download", artifact["artifact_id"]))

    def start(self, attempt, callback, body):
        return {
            "accepted": True,
            "callback_id": callback,
            "lease_duration_seconds": 45,
            "safety_margin_seconds": 5,
        }

    def fail(self, attempt, callback, body):
        self.failures.append(body)
        return {"callback_id": callback, "accepted": True}

    def cleanup(self, attempt, callback, body):
        self.cleanups.append(body)
        return {"callback_id": callback, "verified": True, "allocation_state": "RELEASED"}

    def reserve_checkpoint(self, attempt, callback, body):
        self.calls.append(("reserve_checkpoint", callback))
        if callback not in self.reservations:
            if self.reserve_status is not None:
                raise WorkerApiError(self.reserve_status, "state_conflict")
            self.sequence += 1
            self.reservations[callback] = {
                "callback_id": callback,
                "checkpoint_id": str(new_uuid7()),
                "job_id": self.context["job_id"],
                "attempt_id": attempt,
                "sequence": self.sequence,
                "reserved_at": "2026-09-26T00:00:00.000Z",
            }
        self._maybe_lose("reserve")
        return self.reservations[callback]

    def upload_artifact(self, authority, descriptor, key, content):
        assert len(content) == descriptor["size_bytes"]
        assert "sha256:" + hashlib.sha256(content).hexdigest() == descriptor["checksum"]
        self.calls.append(("upload", descriptor["kind"]))
        if key not in self.uploads:
            artifact = {
                "artifact_id": str(new_uuid7()),
                "tenant_id": self.context["input_artifacts"][0]["tenant_id"],
                "kind": descriptor["kind"],
                "media_type": descriptor["media_type"],
                "size_bytes": descriptor["size_bytes"],
                "checksum": descriptor["checksum"],
                "state": "COMMITTED",
            }
            self.uploads[key] = artifact
            self.artifacts[artifact["artifact_id"]] = artifact
            self.blobs[artifact["artifact_id"]] = content
        self._maybe_lose(f"upload:{descriptor['kind']}")
        return self.uploads[key]

    def publish_checkpoint(self, attempt, callback, body):
        self.calls.append(("publish_checkpoint", callback))
        if callback not in self.published:
            if self.publish_status is not None:
                raise WorkerApiError(self.publish_status, "checkpoint_rejected")
            (reservation,) = [
                item
                for item in self.reservations.values()
                if item["checkpoint_id"] == body["manifest"]["checkpoint_id"]
            ]
            manifest_id = body["manifest_artifact_id"]
            files = validate_checkpoint_manifest(
                body["manifest"],
                raw=self.blobs[manifest_id],
                artifact=self.artifacts[manifest_id],
                checkpoint_id=UUID(reservation["checkpoint_id"]),
                sequence=reservation["sequence"],
                provenance=server_provenance(self.context, attempt, body["authority"]["job_fence"]),
                compatibility=server_compatibility(self.context),
                parameters=self.context["spec"]["parameters"],
            )
            validate_cpu_state(self.blobs[files[0]["artifact_id"]], manifest=body["manifest"])
            self.published[callback] = {
                "checkpoint_id": reservation["checkpoint_id"],
                "job_id": self.context["job_id"],
                "attempt_id": attempt,
                "sequence": reservation["sequence"],
                "manifest_artifact_id": manifest_id,
                "manifest_checksum": self.artifacts[manifest_id]["checksum"],
                "state": "COMMITTED",
                "created_at": "2026-09-26T00:00:01.000Z",
                "manifest": body["manifest"],
            }
        self._maybe_lose("publish")
        return {key: value for key, value in self.published[callback].items() if key != "manifest"}

    def reserve_result(self, attempt, callback, body):
        self.calls.append(("reserve_result", callback))
        return {
            "callback_id": callback,
            "result_id": str(UUID(int=0x018F0D607B6A7A509D821AA39C4F30B7)),
            "attempt_id": attempt,
        }

    def complete(self, attempt, callback, body):
        self.calls.append(("complete", callback))
        self.results.append(body)
        return {"accepted": True}


class Backend(FakeDocker):
    """Docker fake whose container /output is a host directory the runner writes."""

    def __init__(self, output: Path, labels=None, inspect_code=0):
        super().__init__()
        self.output = output
        self.labels = LABELS if labels is None else labels
        self.inspect_code = inspect_code
        self.image_inspects = []
        self.create_argv = None
        self.supervisor_argv = None

    def run(self, argv, timeout_seconds):
        if argv[1:3] == ("image", "inspect"):
            self.image_inspects.append(argv[3])
            if self.inspect_code:
                return self.inspect_code, b"", b"inspect failed"
            return 0, json.dumps([{"Config": {"Labels": self.labels}}]).encode(), b""
        if argv[1] == "exec" and argv[5] == "tar":
            source = self.output / argv[11]
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                archive.add(source, arcname=source.name)
            return 0, buffer.getvalue(), b""
        if argv[1] == "create":
            self.create_argv = argv
        if argv[1:3] == ("exec", "--detach"):
            self.supervisor_argv = argv
        result = super().run(argv, timeout_seconds)
        if argv[1] == "inspect" and result[0] == 0:
            payload = json.loads(result[1])
            payload[0]["Config"]["Labels"]["nexa.installation_id"] = "test"
            return 0, json.dumps(payload).encode(), b""
        return result


class Harness:
    """Production dispatch + IPC + result loops against the real trusted runner."""

    def __init__(self, tmp_path: Path, context, *, api=None, backend=None):
        self.root = tmp_path
        self.fs = tmp_path / "fs"
        (self.fs / "output").mkdir(parents=True)
        (self.fs / "input").mkdir(parents=True)
        self.context = context
        self.api = api or Api(context)
        self.backend = backend or Backend(self.fs / "output")
        self.journal = ExecutionJournal(tmp_path / "journal")
        self.executor = DockerExecutor(
            self.journal,
            self.backend,
            image_ref="registry.invalid/cpu",
            staging_root=tmp_path / "staging",
            installation_id="test",
        )
        self.clock = [time.monotonic_ns()]
        self.runner = None
        self.fail_send = set()
        self.authority = start().context.authority
        self.agent = self._agent()

    def _agent(self):
        return WorkerAgent(
            worker_id=self.authority.worker_id,
            incarnation_id=self.authority.worker_incarnation_id,
            installation_id="test",
            client=self.api,
            journal=self.journal,
            state=PendingOperationStore(self.root / "pending", boot_id="test"),
            docker=DockerCli(self.backend),
            executor=self.executor,
            provider=SimpleNamespace(discover=lambda: SimpleNamespace(architecture=ARCH)),
            channel_factory=lambda _: self._channel(),
            monotonic_ns=lambda: self.clock[0],
        )

    @property
    def attempt_id(self):
        return self.authority.attempt_id

    def launch_spec(self):
        path = self.journal.root / "control" / self.attempt_id / "launch-spec.json"
        return json.loads(path.read_bytes())

    def _channel(self):
        harness = self
        if self.runner is None:
            self.runner = RunnerSupervisor(
                startup_limit_seconds=30,
                runtime_limit_seconds=300,
                stop_grace_seconds=5,
                state_path=self.root / "run" / "runner-state.json",
                launch_spec=self.launch_spec(),
                fs_root=self.fs,
            )

        class Channel:
            def __init__(self):
                self.acks = []

            def settimeout(self, value):
                pass

            def sendall(self, raw):
                for frame in FrameDecoder().feed(raw):
                    if "ack_sequence" in frame:
                        harness.runner.acknowledge_message(frame)
                        continue
                    if frame["type"] in harness.fail_send:
                        harness.fail_send.discard(frame["type"])
                        raise OSError("control channel dropped before delivery")
                    code = harness.runner.apply_control(frame)
                    self.acks.append(
                        {
                            "schema_version": 1,
                            "ack_sequence": frame["control_sequence"],
                            "accepted": code in {"ACCEPTED", "DUPLICATE"},
                            "code": code,
                        }
                    )

            def recv(self, maximum):
                frames = [*self.acks, *harness.runner.pending_messages]
                self.acks = []
                if not frames:
                    raise TimeoutError()
                return b"".join(encode_frame(frame) for frame in frames)

        return Channel()

    def dispatch(self):
        self.agent._dispatch_offer({"authority": asdict(self.authority)})

    def run_workload(self, step, accumulator):
        """The workload has started and rewrites its live state snapshot."""
        self.runner.mark_workload_started()
        self.runner.emit_progress(fraction=0.0, step=0)
        self.write_state(step, accumulator)

    def write_state(self, step, accumulator):
        (self.fs / "output" / "state.json").write_bytes(
            state_bytes(self.context, step, accumulator)
        )

    def cycle(self, rounds=8, *, tolerate=()):
        for _ in range(rounds):
            try:
                self.agent._ipc_once()
                self.agent._result_once()
            except tolerate:
                continue

    def advance(self, seconds):
        self.clock[0] += seconds * SECOND

    def flow_state(self):
        return self.journal.load(self.attempt_id).runner_state

    def stage_result(self, accumulator):
        spec = self.launch_spec()
        source = self.fs / "output" / "result.json"
        source.write_bytes(json.dumps({"accumulator": accumulator}).encode())
        self.runner.stage_result(
            source,
            completion_token=spec["startup_nonce"],
            logical_name="result.json",
            media_type="application/vnd.nexa.cpu-iterative-result+json",
            provenance=spec["provenance"],
        )


def started(tmp_path, **kwargs):
    harness = Harness(tmp_path, claim_context(**kwargs))
    harness.dispatch()
    harness.run_workload(20, 555)
    harness.cycle(2)
    return harness


def calls(api, name):
    return [call for call in api.calls if call[0] == name]


# --- launch selection -----------------------------------------------------------


@pytest.mark.parametrize(
    ("checkpointable", "capable", "requirement", "expected"),
    [
        (True, True, REQUIREMENT, True),
        (False, True, REQUIREMENT, False),
        (True, False, REQUIREMENT, False),
        (True, True, {**REQUIREMENT, "framework": "PYTORCH"}, False),
        (True, True, {**REQUIREMENT, "device": "CUDA"}, False),
        (True, True, {**REQUIREMENT, "cuda_runtime_min": "12.1"}, False),
    ],
)
def test_checkpoint_launch_needs_checkpointable_cpu_template_and_capable_image(
    checkpointable, capable, requirement, expected
):
    context = claim_context(checkpointable=checkpointable, requirement=requirement)
    launch = dispatch.checkpoint_launch(context, image_capable=capable)
    assert (launch is not None) is expected
    if expected:
        assert launch == CpuCheckpointLaunch(
            framework_version="1.0.0", restart_safe=False, interval_seconds=5
        )


@pytest.mark.parametrize(
    ("interval", "expected"), [(1, 5), (5, 5), (17, 17), (600, 60), (None, 30)]
)
def test_checkpoint_interval_is_bounded_to_the_contract_range(interval, expected):
    launch = dispatch.checkpoint_launch(claim_context(interval=interval), image_capable=True)
    assert launch.interval_seconds == expected


@pytest.mark.parametrize("capable", [True, False])
def test_claimed_restore_never_falls_back_to_a_from_zero_launch(capable):
    restore, _ = sealed_restore(claim_context())
    context = claim_context(checkpointable=capable, restore=restore)
    with pytest.raises(dispatch.RestoreUnavailable):
        dispatch.checkpoint_launch(context, image_capable=False)


def test_executor_reads_the_pinned_image_label_once_and_fails_closed(tmp_path):
    backend = Backend(tmp_path)
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "journal"), backend, image_ref="registry.invalid/cpu"
    )
    digest = start().context.image_digest
    assert executor.checkpoint_supported(digest) is True
    assert executor.checkpoint_supported(digest) is True
    assert backend.image_inspects == [f"registry.invalid/cpu@{digest}"]

    old = DockerExecutor(
        ExecutionJournal(tmp_path / "old"), Backend(tmp_path, labels={}), image_ref="r/cpu"
    )
    assert old.checkpoint_supported(digest) is False
    wrong = DockerExecutor(
        ExecutionJournal(tmp_path / "wrong"),
        Backend(tmp_path, labels={"io.nexa.runner.checkpoint": "cpu-state-v0"}),
        image_ref="r/cpu",
    )
    assert wrong.checkpoint_supported(digest) is False

    broken = DockerExecutor(
        ExecutionJournal(tmp_path / "broken"), Backend(tmp_path, inspect_code=1), image_ref="r/cpu"
    )
    with pytest.raises(ExecutorError) as raised:
        broken.checkpoint_supported(digest)
    assert raised.value.code == ExecutorErrorCode.INSPECTION_UNAVAILABLE
    with pytest.raises(ValueError, match="digest-pinned"):
        DockerCli(backend).image_labels("registry.invalid/cpu:latest", timeout_seconds=1)


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("checkpoint-1-state.json", True),
        ("checkpoint-42-manifest.json", True),
        ("checkpoint-0-state.json", False),
        ("checkpoint-1-other.json", False),
        ("../checkpoint-1-state.json", False),
        ("state.json", False),
    ],
)
def test_output_reader_accepts_only_closed_checkpoint_names(name, allowed):
    body = b"{}"

    class Tar:
        def run(self, argv, timeout_seconds):
            assert argv[-1] == name
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                entry = tarfile.TarInfo(name)
                entry.size = len(body)
                archive.addfile(entry, io.BytesIO(body))
            return 0, buffer.getvalue(), b""

    descriptor = {
        "staging_name": name,
        "size_bytes": len(body),
        "checksum": "sha256:" + hashlib.sha256(body).hexdigest(),
    }
    if allowed:
        assert DockerCli(Tar()).read_output("a" * 64, descriptor) == body
    else:
        with pytest.raises(ValueError):
            DockerCli(Tar()).read_output("a" * 64, descriptor)


def test_launch_spec_v2_and_binding_exist_only_for_a_checkpoint_launch(tmp_path):
    source = tmp_path / "input.json"
    plain = dispatch.execution_request(claim_context(), source, ARCH)
    assert "checkpoint" not in _execution_binding(plain)
    assert "--state-output" not in _supervisor_command(plain)

    launch = dispatch.checkpoint_launch(claim_context(), image_capable=True)
    request = dispatch.execution_request(claim_context(), source, ARCH, checkpoint=launch)
    assert _execution_binding(request)["checkpoint"]["interval_seconds"] == 5
    command = _supervisor_command(request)
    assert command[command.index("--state-output") + 1] == "/output/state.json"
    assert "--resume-state" not in command


# --- restore verification --------------------------------------------------------


def _restore_mutations():
    def provenance(key, value):
        def mutate(restore):
            restore["manifest"]["provenance"][key] = value

        return mutate

    def compatibility(key, value):
        def mutate(restore):
            restore["manifest"]["compatibility"][key] = value

        return mutate

    def file_field(key, value):
        def mutate(restore):
            restore["files"][0][key] = value

        return mutate

    def record_field(key, value):
        def mutate(restore):
            restore["record"][key] = value

        return mutate

    return {
        "image": provenance("image_digest", "sha256:" + "0" * 64),
        "input": provenance("input_checksum", "sha256:" + "0" * 64),
        "session": provenance("session_id", str(new_uuid7())),
        "spec": provenance("spec_checksum", "sha256:" + "0" * 64),
        "adapter": provenance("adapter_version", "2.0.0"),
        "architecture": compatibility("architecture", "linux/arm64"),
        "framework": compatibility("framework_version", "2.0.0"),
        "restart_safe": compatibility("restart_safe", True),
        "file_checksum": file_field("checksum", "sha256:" + "0" * 64),
        "file_kind": file_field("kind", "RESULT_FILE"),
        "file_tenant": file_field("tenant_id", str(new_uuid7())),
        "record_corrupt": record_field("state", "CORRUPT"),
        "record_job": record_field("job_id", str(new_uuid7())),
        "record_sequence": record_field("sequence", 9),
    }


@pytest.mark.parametrize("mutation", sorted(_restore_mutations()))
def test_restore_manifest_mismatch_is_unavailable(mutation):
    restore, _ = sealed_restore(claim_context())
    _restore_mutations()[mutation](restore)
    # Keep the manifest self-consistent so only the named fact differs.
    body = {k: v for k, v in restore["manifest"].items() if k != "manifest_checksum"}
    restore["manifest"]["manifest_checksum"] = checksum(body)
    restore["record"]["manifest_checksum"] = (
        "sha256:" + hashlib.sha256(rfc8785.dumps(restore["manifest"])).hexdigest()
    )
    context = claim_context(restore=restore)
    launch = dispatch.checkpoint_launch(context, image_capable=True)
    with pytest.raises(dispatch.RestoreUnavailable):
        dispatch.verify_restore_manifest(context, ARCH, launch)


def test_restore_manifest_bytes_must_match_the_committed_checksum():
    restore, _ = sealed_restore(claim_context())
    restore["record"]["manifest_checksum"] = "sha256:" + "0" * 64
    context = claim_context(restore=restore)
    launch = dispatch.checkpoint_launch(context, image_capable=True)
    with pytest.raises(dispatch.RestoreUnavailable):
        dispatch.verify_restore_manifest(context, ARCH, launch)


@pytest.mark.parametrize("defect", ["cursor", "bytes", "input", "json"])
def test_restore_state_must_be_the_manifest_cursor(defect):
    context = claim_context()
    restore, raw = sealed_restore(context, step=20, accumulator=555)
    context = claim_context(restore=restore)
    launch = dispatch.checkpoint_launch(context, image_capable=True)
    _, cursor = dispatch.verify_restore_manifest(context, ARCH, launch)
    dispatch.verify_restore_state(raw, context=context, cursor=cursor)
    if defect == "cursor":
        raw = state_bytes(context, 21, 555)
    elif defect == "bytes":
        raw = raw.replace(b"555", b"556")
    elif defect == "input":
        raw = encode_state(
            CpuState(
                step=20,
                accumulator=555,
                input_checksum="sha256:" + "0" * 64,
                spec_checksum=checksum(context["spec"]),
            )
        )
    else:
        raw = b"not json"
    with pytest.raises(dispatch.RestoreUnavailable):
        dispatch.verify_restore_state(raw, context=context, cursor=cursor)


@pytest.mark.parametrize("defect", ["manifest", "bytes", "image"])
def test_unavailable_restore_fails_incompatible_without_a_container(tmp_path, defect):
    base = claim_context()
    restore, raw = sealed_restore(base)
    if defect == "manifest":
        restore["manifest"]["provenance"]["image_digest"] = "sha256:" + "0" * 64
    blobs = {restore["files"][0]["artifact_id"]: raw if defect != "bytes" else raw[:-1] + b" "}
    context = claim_context(restore=restore)
    labels = {} if defect == "image" else None
    api = Api(context, blobs=blobs)
    harness = Harness(
        tmp_path, context, api=api, backend=Backend(tmp_path / "fs" / "output", labels=labels)
    )
    harness.dispatch()
    assert [(f["failure_class"], f["reason_code"]) for f in api.failures] == [
        ("INCOMPATIBLE", "CHECKPOINT_RESTORE_UNAVAILABLE")
    ]
    assert api.failures[0]["observation"]["observation_type"] == "NO_CONTAINER"
    assert [c["proof"]["proof_type"] for c in api.cleanups] == ["NO_CONTAINER"]
    assert harness.backend.created == 0
    assert harness.attempt_id not in harness.agent._adopted


def test_verified_restore_mounts_state_and_resumes_the_runner(tmp_path):
    base = claim_context()
    restore, raw = sealed_restore(base, step=20, accumulator=555)
    context = claim_context(restore=restore)
    api = Api(context, blobs={restore["files"][0]["artifact_id"]: raw})
    harness = Harness(tmp_path, context, api=api)
    (harness.fs / "input" / "restore-state.json").write_bytes(raw)
    harness.dispatch()
    assert api.failures == []
    assert ("download", restore["files"][0]["artifact_id"]) in api.calls
    spec = harness.launch_spec()
    assert spec["schema_version"] == 2
    assert spec["restore"] == {
        "path": RESTORE_STATE_PATH,
        "checkpoint_id": restore["record"]["checkpoint_id"],
        "checkpoint_sequence": 1,
        "step": 20,
        "accumulator": 555,
        "state_checksum": restore["files"][0]["checksum"],
    }
    assert any(f"dst={RESTORE_STATE_PATH},readonly" in item for item in harness.backend.create_argv)
    argv = harness.backend.supervisor_argv
    assert argv[argv.index("--resume-state") + 1] == RESTORE_STATE_PATH
    assert argv[argv.index("--state-output") + 1] == "/output/state.json"
    binding = harness.journal.load(harness.attempt_id).execution_binding
    assert binding["checkpoint"]["restore"]["step"] == 20
    # The real runner accepts the mounted bytes before compute starts.
    assert harness.runner._restore_state_is_valid() is True
    (harness.fs / "input" / "restore-state.json").write_bytes(state_bytes(context, 19, 555))
    assert harness.runner._restore_state_is_valid() is False


def test_partial_restore_download_left_by_a_crash_is_fetched_again(tmp_path):
    base = claim_context()
    restore, raw = sealed_restore(base, step=20, accumulator=555)
    context = claim_context(restore=restore)
    api = Api(context, blobs={restore["files"][0]["artifact_id"]: raw})
    harness = Harness(tmp_path, context, api=api)
    (harness.fs / "input" / "restore-state.json").write_bytes(raw)
    # A worker killed mid-download leaves a truncated private file behind.
    partial = tmp_path / "staging" / "downloads" / harness.attempt_id / "restore-state.json"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(raw[:7])
    harness.dispatch()
    assert api.failures == []
    assert ("download", restore["files"][0]["artifact_id"]) in api.calls
    assert partial.read_bytes() == raw
    assert harness.launch_spec()["restore"]["step"] == 20


def test_configured_image_digest_mismatch_fails_the_attempt_without_a_container(tmp_path):
    harness = Harness(tmp_path, claim_context())
    harness.executor.image_ref = "registry.invalid/cpu@sha256:" + "0" * 64
    with pytest.raises(ValueError, match="digest"):
        harness.dispatch()
    assert [(f["failure_class"], f["reason_code"]) for f in harness.api.failures] == [
        ("INTERNAL", "INVALID_RESULT")
    ]
    assert harness.api.failures[0]["observation"]["observation_type"] == "NO_CONTAINER"
    assert [c["proof"]["proof_type"] for c in harness.api.cleanups] == ["NO_CONTAINER"]
    assert harness.backend.created == 0


def test_old_image_without_label_runs_the_b11_launch_unchanged(tmp_path):
    context = claim_context()
    harness = Harness(tmp_path, context, backend=Backend(tmp_path / "fs" / "output", labels={}))
    harness.dispatch()
    assert harness.launch_spec()["schema_version"] == 1
    assert "checkpoint" not in harness.journal.load(harness.attempt_id).execution_binding
    assert "--state-output" not in harness.backend.supervisor_argv
    harness.run_workload(20, 555)
    harness.cycle(3)
    harness.advance(120)
    harness.cycle(3)
    assert calls(harness.api, "reserve_checkpoint") == []


# --- worker checkpoint cycle ------------------------------------------------------


def test_interval_cycle_publishes_a_server_valid_checkpoint_then_the_result(tmp_path):
    harness = started(tmp_path)
    assert calls(harness.api, "reserve_checkpoint") == []
    harness.advance(4)
    harness.cycle(2)
    assert calls(harness.api, "reserve_checkpoint") == []
    harness.advance(2)
    harness.cycle(6)
    (published,) = harness.api.published.values()
    assert published["sequence"] == 1
    assert published["manifest"]["cursor"] == {
        "step": 20,
        "epoch": 0,
        "item_cursor": 20,
        "accumulator": 555,
    }
    state = harness.flow_state()
    assert state["checkpoint_flow"]["cycle"] is None
    assert state["checkpoint_flow"]["last_outcome"]["outcome"] == "COMMITTED"
    assert state["active_reservations"]["checkpoint"] is None
    assert harness.runner.state is RunnerState.RUNNING
    assert [kind for name, kind in harness.api.calls if name == "upload"] == [
        "CHECKPOINT_FILE",
        "CHECKPOINT_MANIFEST",
    ]

    # The next interval checkpoints a newer cursor under a new sequence.
    harness.write_state(40, 777)
    harness.advance(6)
    harness.cycle(6)
    assert sorted(p["sequence"] for p in harness.api.published.values()) == [1, 2]

    harness.write_state(ITERATIONS, 999)
    harness.stage_result(999)
    harness.cycle(8)
    assert len(harness.api.results) == 1
    assert [c["proof"]["container"]["container_id"] for c in harness.api.cleanups] == [
        harness.backend.container_id
    ]


def test_checkpoint_cycle_logs_no_state_content(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    harness = started(tmp_path)
    harness.write_state(30, 987_654)
    harness.advance(6)
    harness.cycle(6)

    (published,) = harness.api.published.values()
    assert published["manifest"]["cursor"]["accumulator"] == 987_654
    assert "987654" not in caplog.text
    assert "manifest_checksum" not in caplog.text


def test_result_pending_before_a_due_tick_wins_without_a_checkpoint(tmp_path):
    harness = started(tmp_path)
    harness.write_state(ITERATIONS, 999)
    harness.stage_result(999)
    harness.advance(6)
    harness.cycle(8)
    assert calls(harness.api, "reserve_checkpoint") == []
    assert len(harness.api.results) == 1
    assert harness.flow_state()["active_reservations"]["checkpoint"] is None


@pytest.mark.parametrize("order", ["runner_defers", "worker_defers"])
def test_result_staged_during_a_cycle_publishes_the_checkpoint_first(tmp_path, order):
    harness = started(tmp_path)
    harness.write_state(ITERATIONS, 999)
    harness.advance(6)
    if order == "runner_defers":
        # REQUEST_CHECKPOINT is accepted first; the runner holds RESULT_PREPARE back.
        harness.agent._result_once()
        harness.stage_result(999)
    else:
        # RESULT_PREPARE is already in flight when the worker opens the cycle.
        harness.stage_result(999)
        harness.agent._result_once()
        harness.agent._ipc_once()
        harness.agent._result_once()
        assert harness.flow_state()["deferred_result_prepare"]["type"] == "RESULT_PREPARE"
    assert calls(harness.api, "reserve_checkpoint")
    harness.cycle(10)
    names = [name for name, _ in harness.api.calls]
    assert names.index("publish_checkpoint") < names.index("reserve_result")
    (published,) = harness.api.published.values()
    assert published["manifest"]["cursor"]["step"] == ITERATIONS
    assert len(harness.api.results) == 1
    assert harness.api.failures == []
    assert harness.flow_state().get("deferred_result_prepare") is None


CRASH_POINTS = [
    "reserve",
    "request",
    "state_upload",
    "bind",
    "finalize",
    "manifest_upload",
    "publish",
    "close",
]


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_every_cycle_step_replays_the_same_checkpoint_identity(tmp_path, point, monkeypatch):
    harness = started(tmp_path)
    api = harness.api
    if point == "reserve":
        api.lose.add("reserve")
    elif point == "state_upload":
        api.lose.add("upload:CHECKPOINT_FILE")
    elif point == "manifest_upload":
        api.lose.add("upload:CHECKPOINT_MANIFEST")
    elif point == "publish":
        api.lose.add("publish")
    elif point == "request":
        harness.fail_send.add("REQUEST_CHECKPOINT")
    elif point == "bind":
        harness.fail_send.add("BIND_ARTIFACT_BATCH")
    elif point == "finalize":
        harness.fail_send.add("FINALIZE_CHECKPOINT_MANIFEST")
    else:
        original = CheckpointFlow._close
        crashed = []

        def close_once(self, *args, **kwargs):
            if not crashed:
                crashed.append(True)
                raise OSError("worker died before the journal recorded COMMITTED")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(CheckpointFlow, "_close", close_once)
    harness.advance(6)
    harness.cycle(10, tolerate=(OSError, TimeoutError))
    # A restarted process has no in-memory schedule; the journal alone resumes.
    harness.agent._checkpoint_due.clear()
    harness.cycle(4)
    assert api.sequence == 1
    assert len(api.reservations) == 1
    assert len({call[1] for call in calls(api, "publish_checkpoint")}) == 1
    (published,) = api.published.values()
    assert published["sequence"] == 1
    assert sorted(a["kind"] for a in api.uploads.values()) == [
        "CHECKPOINT_FILE",
        "CHECKPOINT_MANIFEST",
    ]
    state = harness.flow_state()
    assert state["checkpoint_flow"]["cycle"] is None
    assert state["checkpoint_flow"]["last_outcome"]["checkpoint_id"] == published["checkpoint_id"]
    assert state["active_reservations"]["checkpoint"] is None
    assert harness.runner.state is RunnerState.RUNNING
    assert api.failures == []


def test_reserve_conflict_drops_the_cycle_and_waits_a_full_interval(tmp_path):
    harness = started(tmp_path)
    harness.api.reserve_status = 409
    harness.advance(6)
    harness.cycle(2)
    assert harness.flow_state()["checkpoint_flow"]["cycle"] is None
    assert len(calls(harness.api, "reserve_checkpoint")) == 1
    harness.api.reserve_status = None
    harness.advance(4)
    harness.cycle(2)
    assert len(calls(harness.api, "reserve_checkpoint")) == 1
    harness.advance(2)
    harness.cycle(6)
    assert len(harness.api.published) == 1
    assert harness.runner.state is RunnerState.RUNNING


def test_publish_rejection_disables_further_cycles_but_not_the_workload(tmp_path):
    harness = started(tmp_path)
    harness.api.publish_status = 422
    harness.advance(6)
    harness.cycle(6)
    flow = harness.flow_state()["checkpoint_flow"]
    assert flow["disabled"] is True
    assert flow["last_outcome"]["outcome"] == "REJECTED"
    assert harness.flow_state()["active_reservations"]["checkpoint"] is None
    harness.advance(60)
    harness.cycle(4)
    assert len(calls(harness.api, "reserve_checkpoint")) == 1
    assert harness.runner.state is RunnerState.RUNNING
    assert harness.api.failures == []


@pytest.mark.parametrize("status", [409, 503])
def test_publish_conflict_keeps_the_reservation_and_retries_the_same_callback(tmp_path, status):
    # 503 covers a restarted incarnation whose inventory the server has not seen yet.
    harness = started(tmp_path)
    harness.api.publish_status = status
    harness.advance(6)
    harness.cycle(4, tolerate=(WorkerApiError,))
    first = calls(harness.api, "publish_checkpoint")
    assert first and harness.flow_state()["checkpoint_flow"]["cycle"] is not None
    harness.api.publish_status = None
    harness.cycle(2)
    assert {call[1] for call in calls(harness.api, "publish_checkpoint")} == {first[0][1]}
    assert len(harness.api.published) == 1
    assert harness.api.failures == []
    assert harness.flow_state()["checkpoint_flow"]["last_outcome"]["outcome"] == "COMMITTED"


@pytest.mark.parametrize(
    "runner_state",
    [
        {"result_flow": {"stage": "RESERVED"}},
        {"deferred_result_prepare": {"message_sequence": 9}},
        {"failure_resolution": {"callback_id": "x"}},
        {"latest_progress": None},
        {"latest_progress": {"fraction": 1.0}},
        {"checkpoint_flow": {"disabled": True}},
    ],
)
def test_tick_opens_no_cycle_when_the_attempt_is_finishing(tmp_path, runner_state):
    harness = started(tmp_path)
    harness.journal.update_runner_state(harness.attempt_id, lambda local: {**local, **runner_state})
    flow = CheckpointFlow(
        harness.journal,
        harness.api,
        None,
        None,
        monotonic_ns=lambda: harness.clock[0],
        next_due={harness.attempt_id: 0},
    )
    flow.tick(harness.attempt_id)
    assert calls(harness.api, "reserve_checkpoint") == []


@pytest.mark.parametrize(
    "tamper",
    ["identity", "sequence", "batch", "descriptor", "bytes", "manifest_early", "unknown_last"],
)
def test_protocol_defect_fails_the_attempt_closed(tmp_path, tamper):
    harness = started(tmp_path)
    harness.advance(6)
    harness.agent._result_once()
    harness.agent._ipc_once()
    state = harness.flow_state()
    envelope = json.loads(json.dumps(state["pending_execution_message"]))
    assert envelope["type"] == "CHECKPOINT_FILES_READY"
    payload = envelope["payload"]
    if tamper == "identity":
        payload["checkpoint_id"] = str(new_uuid7())
    elif tamper == "sequence":
        payload["checkpoint_sequence"] = 2
    elif tamper == "batch":
        payload["batch_count"] = 2
    elif tamper == "descriptor":
        payload["artifacts"][0]["staging_name"] = "checkpoint-9-state.json"
    elif tamper == "bytes":
        payload["artifacts"][0]["checksum"] = "sha256:" + "0" * 64
    elif tamper == "manifest_early":
        envelope["type"] = "CHECKPOINT_READY"
        payload.pop("batch_index")
        payload.pop("batch_count")
        payload["manifest"] = payload.pop("artifacts")[0]
    else:
        harness.journal.update_runner_state(
            harness.attempt_id,
            lambda local: {
                **local,
                "checkpoint_flow": {**local["checkpoint_flow"], "cycle": None},
            },
        )
    harness.journal.update_runner_state(
        harness.attempt_id, lambda local: {**local, "pending_execution_message": envelope}
    )
    harness.agent._result_once()
    assert [(f["failure_class"], f["reason_code"]) for f in harness.api.failures] == [
        ("INTERNAL", "CHECKPOINT_PROTOCOL_ERROR")
    ]
    assert len(harness.api.cleanups) == 1
    assert harness.api.published == {}


@pytest.mark.parametrize("answer", ["binding", "missing_field", "not_object", "not_json", "bad_id"])
def test_invalid_committed_upload_answer_fails_only_that_attempt(tmp_path, answer):
    """The real client's answer checks end this Attempt, never the worker loop."""
    harness = started(tmp_path)

    def respond(request):
        body = {
            "artifact_id": str(new_uuid7()),
            "tenant_id": harness.context["input_artifacts"][0]["tenant_id"],
            "kind": request.headers["X-Artifact-Kind"],
            "media_type": request.headers["X-Artifact-Media-Type"],
            "size_bytes": int(request.headers["X-Artifact-Size"]),
            "checksum": request.headers["X-Artifact-Checksum"],
            "state": "COMMITTED",
        }
        if answer == "binding":
            body["checksum"] = "sha256:" + "0" * 64
        elif answer == "missing_field":
            del body["kind"]
        elif answer == "not_object":
            return httpx.Response(201, json=[body])
        elif answer == "not_json":
            return httpx.Response(201, content=b"committed")
        else:
            body["artifact_id"] = "not-a-uuid"
        return httpx.Response(201, json=body)

    client = WorkerApiClient(
        "http://worker.invalid", "c" * 32, transport=httpx.MockTransport(respond)
    )
    harness.api.upload_artifact = client.upload_artifact
    harness.advance(6)
    harness.agent._result_once()
    harness.agent._ipc_once()
    assert harness.flow_state()["pending_execution_message"]["type"] == "CHECKPOINT_FILES_READY"
    # Another adopted Attempt after the defective one must still get its turn.
    other = str(new_uuid7())
    harness.agent._adopted[other] = harness.authority
    visited = []
    result_attempt = harness.agent._result_attempt

    def record(attempt_id, flow, checkpoints):
        visited.append(attempt_id)
        if attempt_id != other:
            result_attempt(attempt_id, flow, checkpoints)

    harness.agent._result_attempt = record
    harness.agent._result_once()
    assert visited == [harness.attempt_id, other]
    assert [(f["failure_class"], f["reason_code"]) for f in harness.api.failures] == [
        ("INTERNAL", "CHECKPOINT_PROTOCOL_ERROR")
    ]
    assert len(harness.api.cleanups) == 1
    assert harness.api.published == {}
    client.close()


def test_runner_invalid_control_answer_fails_closed(tmp_path):
    harness = started(tmp_path)
    harness.advance(6)
    # The runner can no longer take a checkpoint: its live state is gone.
    (harness.fs / "output" / "state.json").unlink()
    harness.agent._result_once()
    assert [(f["failure_class"], f["reason_code"]) for f in harness.api.failures] == [
        ("INTERNAL", "CHECKPOINT_PROTOCOL_ERROR")
    ]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"failure_class": "INCOMPATIBLE", "reason_code": "CHECKPOINT_RESTORE_UNAVAILABLE"},
            ("INCOMPATIBLE", "CHECKPOINT_RESTORE_UNAVAILABLE"),
        ),
        (
            {"failure_class": "INCOMPATIBLE", "reason_code": "SOMETHING_ELSE"},
            ("INTERNAL", "WORKLOAD_EXIT_NONZERO"),
        ),
        (
            {"failure_class": "INTERNAL", "reason_code": "CHECKPOINT_RESTORE_UNAVAILABLE"},
            ("INTERNAL", "WORKLOAD_EXIT_NONZERO"),
        ),
    ],
)
def test_only_allowlisted_runner_failures_are_forwarded(tmp_path, payload, expected):
    harness = started(tmp_path)
    envelope = {
        "schema_version": 1,
        "message_sequence": 99,
        "type": "FAILED",
        "payload": {
            **payload,
            "exit_code": None,
            "oom_killed": False,
            "runtime_limit_reached": False,
        },
    }
    harness.journal.update_runner_state(
        harness.attempt_id, lambda local: {**local, "pending_execution_message": envelope}
    )
    harness.agent._result_once()
    assert [(f["failure_class"], f["reason_code"]) for f in harness.api.failures] == [expected]


# --- adoption reconcile -----------------------------------------------------------


RESERVATION = {
    "callback_id": "018f0d60-7b6a-7a60-9d82-1aa39c4f30b7",
    "checkpoint_id": "018f0d60-7b6a-7a61-9d82-1aa39c4f30b7",
    "job_id": "018f0d60-7b6a-7a62-9d82-1aa39c4f30b7",
    "attempt_id": "018f0d60-7b6a-7a63-9d82-1aa39c4f30b7",
    "sequence": 3,
    "reserved_at": "2026-09-26T00:00:00.000Z",
}


RESULT_RESERVATION = {
    "callback_id": "018f0d60-7b6a-7a64-9d82-1aa39c4f30b7",
    "result_id": "018f0d60-7b6a-7a65-9d82-1aa39c4f30b7",
    "job_id": RESERVATION["job_id"],
    "attempt_id": RESERVATION["attempt_id"],
    "reserved_at": "2026-09-26T00:00:02.000Z",
}


def _result_flow(**changes):
    return {
        "completion_token": "018f0d60-7b6a-7a66-9d82-1aa39c4f30b7",
        "reservation_callback_id": RESULT_RESERVATION["callback_id"],
        "source_sequence": 7,
        **changes,
    }


def _cycle(**changes):
    return {
        "reserve_callback_id": RESERVATION["callback_id"],
        "reservation": None,
        "request": None,
        "files_sequence": None,
        "bindings": {},
        "cursor": None,
        "manifest_sequence": None,
        "manifest_binding": None,
        "publish_callback_id": None,
        **changes,
    }


def _local(checkpoint, cycle, result=None):
    return {
        "active_reservations": {"checkpoint": checkpoint, "result": result},
        "checkpoint_flow": {"cycle": cycle},
    }


@pytest.mark.parametrize(
    ("local", "server", "expected_cycle"),
    [
        # Nothing held on either side: an uncommitted reserve is forgotten.
        (_local(None, _cycle()), None, None),
        (_local(None, None), None, None),
        # Held on both sides: keep the cycle, the old publish callback is dropped.
        (
            _local(RESERVATION, _cycle(reservation=RESERVATION, publish_callback_id="p")),
            RESERVATION,
            _cycle(reservation=RESERVATION, publish_callback_id=None),
        ),
        # The reserve committed but its answer was lost before the journal wrote it.
        (_local(None, _cycle()), RESERVATION, _cycle(reservation=RESERVATION)),
        # Publish resolved before adoption: the identity is never reused.
        (
            _local(RESERVATION, _cycle(reservation=RESERVATION, publish_callback_id="p")),
            None,
            None,
        ),
    ],
)
def test_adoption_reconcile_explains_every_transferred_reservation(local, server, expected_cycle):
    reconciled = reconcile_adoption(local, {"checkpoint": server, "result": None})
    assert reconciled is not None
    assert reconciled["active_reservations"]["checkpoint"] == server
    assert reconciled["checkpoint_flow"]["cycle"] == expected_cycle
    if local["active_reservations"]["checkpoint"] and server is None:
        assert reconciled["checkpoint_flow"]["last_outcome"]["outcome"] == (
            "RESOLVED_BEFORE_ADOPTION"
        )


@pytest.mark.parametrize(
    ("local", "server", "result"),
    [
        # A server reservation the journal never asked for.
        (_local(None, None), RESERVATION, None),
        (_local(None, _cycle(reserve_callback_id="other")), RESERVATION, None),
        # A held reservation vanished before this worker tried to publish it.
        (_local(RESERVATION, _cycle(reservation=RESERVATION)), None, None),
        # Different identities.
        (
            _local(RESERVATION, _cycle(reservation=RESERVATION)),
            {**RESERVATION, "sequence": 4},
            None,
        ),
        # A result reservation the journal never asked for.
        (_local(None, None), None, RESULT_RESERVATION),
        (
            {**_local(None, None), "result_flow": _result_flow(reservation_callback_id="other")},
            None,
            RESULT_RESERVATION,
        ),
        # A journaled answer that differs from the server's.
        (
            {
                **_local(None, None),
                "result_flow": _result_flow(
                    reservation={**RESULT_RESERVATION, "result_id": RESERVATION["job_id"]}
                ),
            },
            None,
            RESULT_RESERVATION,
        ),
        # A held result reservation cannot vanish while the Attempt is live.
        (_local(None, None, RESULT_RESERVATION), None, None),
    ],
)
def test_adoption_reconcile_refuses_unexplained_differences(local, server, result):
    assert reconcile_adoption(local, {"checkpoint": server, "result": result}) is None


@pytest.mark.parametrize("journaled", [None, "flow_only"])
def test_adoption_reconcile_explains_a_result_reserve_committed_before_the_journal(journaled):
    # The worker died after the server committed the reserve, before (or halfway
    # through) journaling the answer; only the journaled callback id made it.
    flow = _result_flow(reservation=RESULT_RESERVATION if journaled else None)
    local = {**_local(None, None), "result_flow": flow}
    reconciled = reconcile_adoption(local, {"checkpoint": None, "result": RESULT_RESERVATION})
    assert reconciled is not None
    assert reconciled["active_reservations"] == {"checkpoint": None, "result": RESULT_RESERVATION}
    assert reconciled["result_flow"] == {**flow, "reservation": RESULT_RESERVATION}


@settings(max_examples=200, deadline=None)
@given(
    held=st.sampled_from([None, RESERVATION, {**RESERVATION, "sequence": 4}]),
    server=st.sampled_from([None, RESERVATION, {**RESERVATION, "sequence": 4}]),
    cycle_reservation=st.sampled_from([None, RESERVATION, {**RESERVATION, "sequence": 4}]),
    has_cycle=st.booleans(),
    published=st.booleans(),
    callback=st.sampled_from([RESERVATION["callback_id"], "other"]),
)
def test_adoption_reconcile_never_invents_a_reservation(
    held, server, cycle_reservation, has_cycle, published, callback
):
    cycle = (
        _cycle(
            reserve_callback_id=callback,
            reservation=cycle_reservation,
            publish_callback_id="p" if published else None,
        )
        if has_cycle
        else None
    )
    reconciled = reconcile_adoption(_local(held, cycle), {"checkpoint": server, "result": None})
    if reconciled is None:
        return
    after = reconciled["checkpoint_flow"]["cycle"]
    # The journal adopts exactly the server's snapshot and nothing else.
    assert reconciled["active_reservations"]["checkpoint"] == server
    if server is None:
        assert after is None
    else:
        assert after is not None and after["reservation"] == server
        assert after["reserve_callback_id"] == server["callback_id"]
        # A publish callback bound to the old Authority never replays under the new one.
        assert after["publish_callback_id"] is None


def test_reconcile_is_persisted_only_while_the_prior_authority_is_journaled(tmp_path):
    harness = started(tmp_path)
    harness.advance(6)
    harness.agent._result_once()
    record = harness.journal.load(harness.attempt_id)
    reservation = record.runner_state["active_reservations"]["checkpoint"]
    assert reservation is not None
    acknowledgment = {
        "transferred_checkpoint_reservation": reservation,
        "transferred_result_reservation": None,
    }
    before = record.runner_state
    transferred = harness.agent._reconcile_reservations(
        harness.attempt_id, acknowledgment, record.authority
    )
    assert transferred == {"checkpoint": reservation, "result": None}
    assert harness.flow_state()["checkpoint_flow"]["cycle"]["reservation"] == reservation

    other = asdict(record.authority) | {"lease_id": str(new_uuid7())}
    harness.journal.update_runner_state(harness.attempt_id, lambda local: before)
    harness.agent._reconcile_reservations(
        harness.attempt_id, {"transferred_checkpoint_reservation": None}, Authority(**other)
    )
    assert harness.flow_state() == before

    with pytest.raises(RuntimeError):
        harness.agent._reconcile_reservations(
            harness.attempt_id,
            {"transferred_checkpoint_reservation": {**reservation, "sequence": 7}},
            record.authority,
        )


def test_result_reserve_crash_window_is_adopted_and_completes_once(tmp_path):
    """Server commits the result reserve; the worker dies before journaling the answer."""
    harness = started(tmp_path)
    api = harness.api
    committed = {}

    def reserve_then_die(attempt, callback, body):
        api.calls.append(("reserve_result", callback))
        committed.setdefault(
            callback,
            {
                "callback_id": callback,
                "result_id": str(new_uuid7()),
                "job_id": harness.context["job_id"],
                "attempt_id": attempt,
                "reserved_at": "2026-09-26T00:00:02.000Z",
            },
        )
        if len(calls(api, "reserve_result")) == 1:
            raise Lost("worker process died before the journal recorded the reservation")
        return committed[callback]

    api.reserve_result = reserve_then_die
    harness.write_state(ITERATIONS, 999)
    harness.stage_result(999)
    harness.agent._ipc_once()
    with pytest.raises(TimeoutError):
        harness.agent._result_once()
    record = harness.journal.load(harness.attempt_id)
    state = record.runner_state
    (reservation,) = committed.values()
    assert state["result_flow"]["reservation_callback_id"] == reservation["callback_id"]
    assert state["result_flow"].get("reservation") is None
    assert (state.get("active_reservations") or {}).get("result") is None

    # A restarted worker process adopts the running container under a new incarnation.
    incarnation = str(new_uuid7())
    adoptions = []

    def adopt(attempt, callback, body):
        adoptions.append(body)
        return {
            "callback_id": callback,
            "accepted": True,
            "authority": {**body["prior_authority"], "worker_incarnation_id": incarnation},
            "transferred_checkpoint_reservation": None,
            "transferred_result_reservation": reservation,
            "lease_duration_seconds": 45,
            "safety_margin_seconds": 5,
            "renew_interval_seconds": 5,
        }

    api.adopt = adopt
    restarted = WorkerAgent(
        worker_id=harness.authority.worker_id,
        incarnation_id=incarnation,
        installation_id="test",
        client=api,
        journal=harness.journal,
        state=PendingOperationStore(harness.root / "pending", boot_id="test"),
        docker=DockerCli(harness.backend),
        executor=harness.executor,
        provider=SimpleNamespace(discover=lambda: SimpleNamespace(architecture=ARCH)),
        channel_factory=lambda _: harness._channel(),
        monotonic_ns=lambda: harness.clock[0],
    )
    assert restarted._adopt({}, record, record.container) is True
    assert len(adoptions) == 1
    adopted = harness.journal.load(harness.attempt_id)
    assert adopted.authority.worker_incarnation_id == incarnation
    assert adopted.runner_state["result_flow"]["reservation"] == reservation
    assert adopted.runner_state["active_reservations"]["result"] == reservation

    harness.agent = restarted
    harness.cycle(8)
    # The journaled reservation is reused: no second reserve, one final result.
    assert len(calls(api, "reserve_result")) == 1
    assert len(api.results) == 1
    assert api.results[0]["manifest"]["result_id"] == reservation["result_id"]
    assert api.results[0]["authority"]["worker_incarnation_id"] == incarnation
    assert api.failures == []


# --- compatibility property -------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    architecture=st.sampled_from(["linux/amd64", "linux/arm64"]),
    version=st.tuples(st.integers(0, 20), st.integers(0, 20), st.integers(0, 20)),
    restart_safe=st.booleans(),
)
def test_worker_compatibility_is_the_server_expectation(architecture, version, restart_safe):
    framework_version = ".".join(map(str, version))
    requirement = {
        **REQUIREMENT,
        "architectures": ["linux/amd64", "linux/arm64"],
        "framework_version": framework_version,
    }
    launch = CpuCheckpointLaunch(framework_version=framework_version, restart_safe=restart_safe)
    template = {
        "capability_requirements": requirement,
        "checkpointable": True,
        "restart_safe": restart_safe,
    }
    assert launch.compatibility(architecture) == expected_compatibility(template, architecture)


def test_published_checkpoint_round_trips_into_a_verified_restore(tmp_path):
    harness = started(tmp_path / "first")
    harness.advance(6)
    harness.cycle(6)
    (published,) = harness.api.published.values()
    manifest = published["manifest"]
    record = {key: value for key, value in published.items() if key != "manifest"}
    (entry,) = manifest["files"]
    restore = {
        "record": record,
        "manifest": manifest,
        "files": [harness.api.artifacts[entry["artifact_id"]]],
    }
    context = claim_context(restore=restore)
    launch = dispatch.checkpoint_launch(context, image_capable=True)
    state_file, cursor = dispatch.verify_restore_manifest(context, ARCH, launch)
    dispatch.verify_restore_state(
        harness.api.blobs[state_file["artifact_id"]], context=context, cursor=cursor
    )
    assert (cursor.step, cursor.accumulator) == (20, 555)
