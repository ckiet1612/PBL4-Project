"""B16 real-Docker harness: PyTorch training, chunked inference and sweep on Linux (VPS1).

Extends the B14 harness (in-process API and coordinator, worker container, workload
containers from the frozen images). Differences: the API listens on 127.0.0.1 only and
the worker container shares the host network namespace; templates are registered from
``deploy/templates`` with the image digests under test; the dataset and model fixtures
come from ``NEXA_B16_DATA_DIR`` and must match the frozen fixture checksums. Faults are
injected only by the tests (SIGKILL of a workload container, rewriting or deleting a
committed blob on the test storage root).

Evidence rows hold identifiers, states, sequences, checksums, metrics and sizes only; no
credential, dataset, model, checkpoint or chunk content is recorded.
"""

import hashlib
import json
import math
import os
import socket
import stat
import subprocess
import threading
from pathlib import Path
from uuid import UUID

import pytest
import uvicorn
from sqlalchemy import select, update

from nexa.api.app import create_app
from nexa.application.template_registry import load_definition, register_template
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.database import create_session_factory
from nexa.worker.credentials import CredentialStore
from tests.docker.test_b11_vertical import _check, _check_output
from tests.docker.test_b14_checkpoint_restore import _Harness, _wait
from tests.integration.test_worker_api_b10 import (
    FINGERPRINT,
    INSTALLATION_ID,
    WORKER_ID,
    _bootstrap_worker,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "workloads"
TEMPLATES = ROOT / "deploy" / "templates"
USERNAME = "b16-docker@example.test"
PASSWORD = "correct-horse-battery-staple"


def fixture(name):
    return json.loads((FIXTURES / name / "fixture.json").read_text())


def tolerance():
    raw = (FIXTURES / "pytorch-cifar10-v1" / "tolerance.json").read_bytes()
    return json.loads(raw), "sha256:" + hashlib.sha256(raw).hexdigest()


def environment():
    """Images and data of the opt-in run, or a skip with the missing variable."""
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in B16 Docker evidence (NEXA_RUN_DOCKER=1)")
    names = (
        "NEXA_B16_PYTORCH_IMAGE_REF",
        "NEXA_B16_INFERENCE_IMAGE_REF",
        "NEXA_B09_IMAGE_REF",
        "NEXA_B11_WORKER_IMAGE",
        "NEXA_B16_DATA_DIR",
    )
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.skip("B16 Docker evidence needs " + ", ".join(missing))
    values = {name: os.environ[name] for name in names}
    for name in names[:3]:
        assert "@sha256:" in values[name], f"{name} must be digest pinned"
    return values


def inspect_image(ref, template):
    return subprocess.check_output(
        ["docker", "image", "inspect", "--format", template, ref], text=True, timeout=10
    ).strip()


def register_templates(engine, training_image, inference_image):
    factory = create_session_factory(engine)
    for name, image in (
        ("pytorch-cifar10-cnn.v1.json", training_image),
        ("batch-inference.v1.json", inference_image),
    ):
        definition = load_definition((TEMPLATES / name).read_bytes())
        register_template(factory, definition, image.rsplit("@", 1)[1])


# B16-R26 real OOM run only: version 2 of each AI template with a 256 MiB memory floor.
# It is registered in the Docker test database, never in deploy/templates.
OOM_TEMPLATE_VERSION = 2
OOM_MEMORY_BYTES = 256 * 1024**2


def register_oom_templates(engine, training_image, inference_image):
    factory = create_session_factory(engine)
    for name, image in (
        ("pytorch-cifar10-cnn.v1.json", training_image),
        ("batch-inference.v1.json", inference_image),
    ):
        raw = json.loads((TEMPLATES / name).read_bytes())
        raw["version"] = OOM_TEMPLATE_VERSION
        raw["description"] = "B16-R26 test-only: small memory floor for the real OOM run."
        raw["resource_bounds"]["resources"]["memory_bytes"]["minimum"] = OOM_MEMORY_BYTES
        definition = load_definition(json.dumps(raw).encode())
        register_template(factory, definition, image.rsplit("@", 1)[1])


def data_file(data_dir, entry):
    """A fixture file under NEXA_B16_DATA_DIR whose bytes match the frozen checksum."""
    path = Path(data_dir) / entry["file"]
    content = path.read_bytes()
    assert len(content) == entry["size_bytes"], f"{entry['file']} size differs from the fixture"
    assert "sha256:" + hashlib.sha256(content).hexdigest() == entry["checksum"], (
        f"{entry['file']} checksum differs from the fixture"
    )
    return content


class MemorySampler:
    """Peak cgroup v2 memory of every workload container of this installation.

    ``memory.peak`` of the container scope is monotonic while the container lives and the
    scope disappears on exit, so it is sampled every 0.25 s and the largest value is kept.
    """

    def __init__(self):
        self.peaks = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)

    def _run(self):
        while not self.stop.is_set():
            try:
                listing = subprocess.check_output(
                    [
                        "docker",
                        "ps",
                        "--no-trunc",
                        "--filter",
                        f"label=nexa.installation_id={INSTALLATION_ID}",
                        "--format",
                        '{{.ID}} {{.Label "nexa.attempt_id"}}',
                    ],
                    text=True,
                    timeout=5,
                )
            except subprocess.SubprocessError:
                listing = ""
            for line in listing.splitlines():
                container_id, _, attempt_id = line.partition(" ")
                scope = Path(f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope")
                try:
                    peak = int((scope / "memory.peak").read_text())
                except (OSError, ValueError):
                    continue
                row = self.peaks.setdefault(container_id, {"attempt_id": attempt_id, "peak": 0})
                row["peak"] = max(row["peak"], peak)
            self.stop.wait(0.25)

    def attempt_peak(self, attempt_id):
        values = [row["peak"] for row in self.peaks.values() if row["attempt_id"] == attempt_id]
        return max(values) if values else None


def _proc_status(pid):
    fields = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        fields[key] = value.strip()
    return fields


def hardening(container_id):
    """docker inspect + /proc of a running workload container (hardening oracle)."""
    payload = json.loads(
        subprocess.check_output(["docker", "inspect", container_id], text=True, timeout=10)
    )[0]
    config, host = payload["Config"], payload["HostConfig"]
    # Every process of the container (runner PID 1 and the UID 1001 workload started by
    # docker exec) as listed by its cgroup v2 scope.
    scope = Path(f"/sys/fs/cgroup/system.slice/docker-{payload['Id']}.scope")
    processes = []
    for pid in (scope / "cgroup.procs").read_text().split():
        try:
            status = _proc_status(pid)
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1]
        except OSError:
            continue
        processes.append(
            {
                "comm": comm,
                "argv": [arg.decode(errors="replace") for arg in argv[:2]],
                "uid": status["Uid"].split()[0],
                "seccomp": status["Seccomp"],
                "no_new_privs": status["NoNewPrivs"],
                "cap_eff": status["CapEff"],
                "cap_bnd": status["CapBnd"],
            }
        )
    mounts = [
        {
            "type": mount["Type"],
            "destination": mount["Destination"],
            "read_only": not mount["RW"],
        }
        for mount in payload["Mounts"]
    ]
    return {
        "image": config["Image"],
        "user": config["User"],
        "network_mode": host["NetworkMode"],
        "readonly_rootfs": host["ReadonlyRootfs"],
        "privileged": host["Privileged"],
        "cap_drop": host.get("CapDrop") or [],
        "cap_add": host.get("CapAdd") or [],
        "security_opt": host.get("SecurityOpt") or [],
        "processes": processes,
        "pids_limit": host["PidsLimit"],
        "memory": host["Memory"],
        "memory_swap": host["MemorySwap"],
        "nano_cpus": host["NanoCpus"],
        "restart_policy": host["RestartPolicy"]["Name"] or "no",
        "log_config": host["LogConfig"],
        "tmpfs": sorted((host.get("Tmpfs") or {}).keys()),
        "mounts": mounts,
    }


def assert_hardened(row, resources):
    assert row["user"] == "1000:1000"
    uids = {process["uid"] for process in row["processes"]}
    # Inspected after a committed checkpoint, so the UID 1001 workload is running.
    assert uids == {"1000", "1001"}, row["processes"]
    assert row["network_mode"] == "none"
    assert row["readonly_rootfs"] is True and row["privileged"] is False
    assert row["cap_drop"] == ["ALL"] and row["cap_add"] == []
    assert "no-new-privileges:true" in row["security_opt"]
    assert not any("unconfined" in option for option in row["security_opt"])
    for process in row["processes"]:
        assert process["no_new_privs"] == "1", process
        assert process["seccomp"] == "2", process
        assert int(process["cap_eff"], 16) == 0 and int(process["cap_bnd"], 16) == 0, process
    assert row["pids_limit"] == 512  # contract default (B19-R15)
    assert row["memory"] == row["memory_swap"] == resources["memory_bytes"]
    assert row["nano_cpus"] == resources["cpu_millis"] * 1_000_000
    assert row["restart_policy"] == "no"
    assert row["tmpfs"] == ["/output", "/run/nexa", "/tmp"]
    assert all(mount["read_only"] for mount in row["mounts"]), row["mounts"]
    assert not any("docker.sock" in mount["destination"] for mount in row["mounts"])


class B16Harness(_Harness):
    """B14 harness with loopback API, multi-image worker and B16 fixture uploads."""

    def __init__(self, engine, tmp_path, client, env):
        super().__init__(
            engine, tmp_path, client, env["NEXA_B09_IMAGE_REF"], env["NEXA_B11_WORKER_IMAGE"]
        )
        self.env = env
        self.artifacts = {}

    # -- setup -----------------------------------------------------------------
    def bootstrap(self, *, outstanding_limit=None):
        client = self.client
        self.root.mkdir(mode=0o700)
        credential = _bootstrap_worker(client)
        CredentialStore(self.root / "credential.json").save(
            worker_id=WORKER_ID, installation_id=INSTALLATION_ID, credential=credential
        )
        (self.root / "bootstrap-secret").write_bytes(b"b" * 32)
        login = _check(
            client.post(
                "/v1/internal/admin-bootstrap",
                headers={
                    "X-Nexa-Bootstrap-Secret": "b" * 32,
                    "Idempotency-Key": "b16-docker-admin-bootstrap",
                },
                json={"username": USERNAME, "display_name": "B16 Docker", "password": PASSWORD},
            ),
            201,
        )
        write = {"Origin": "https://nexa.test", "X-CSRF-Token": self._login()}
        tenant = _check(
            client.post(
                "/v1/admin/tenants",
                headers={**write, "Idempotency-Key": "b16-docker-tenant"},
                json={"slug": "b16-docker", "display_name": "B16 Docker"},
            ),
            201,
        )
        self.tenant_id = tenant["tenant_id"]
        _check(
            client.post(
                f"/v1/admin/tenants/{self.tenant_id}/memberships",
                headers={**write, "If-Match": '"v1"', "Idempotency-Key": "b16-docker-member"},
                json={"user_id": login["user_id"], "role": "MEMBER"},
            ),
            200,
        )
        self.policy(cpu_limit_millis=6000, memory_limit_bytes=12 * 1024**3)
        if outstanding_limit is not None:
            self.policy(outstanding_limit=outstanding_limit)
        self.write = {
            "Origin": "https://nexa.test",
            "X-CSRF-Token": self._login(),
            "X-Nexa-Tenant-Id": self.tenant_id,
        }

    def policy(self, **values):
        with self.engine.begin() as connection:
            connection.execute(
                update(s.tenant_policies)
                .where(
                    s.tenant_policies.c.tenant_id == UUID(self.tenant_id),
                    s.tenant_policies.c.is_current.is_(True),
                )
                .values(**values)
            )

    def _login(self):
        return _check(
            self.client.post(
                "/v1/auth/login",
                headers={"Origin": "https://nexa.test"},
                json={"username": USERNAME, "password": PASSWORD},
            ),
            200,
        )["csrf_token"]

    def upload(self, entry, content):
        artifact = _check(
            self.client.post(
                "/v1/artifacts",
                content=content,
                headers={
                    **self.write,
                    "Idempotency-Key": "b16-docker-" + entry["checksum"][7:39],
                    "X-Artifact-Checksum": entry["checksum"],
                    "X-Artifact-Size": str(len(content)),
                    "X-Artifact-Kind": entry["kind"],
                    "X-Artifact-Media-Type": entry["media_type"],
                    "Content-Type": "application/octet-stream",
                },
            ),
            201,
        )
        self.artifacts[entry["file"]] = artifact["artifact_id"]
        return artifact["artifact_id"]

    def start_api(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # Loopback only: nothing of the test listens beyond 127.0.0.1 on the host.
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_app(self.client.app.state.services.settings, engine=self.engine),
                host="127.0.0.1",
                port=port,
                log_level="warning",
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        _wait(lambda: self.server.started, "API readiness", timeout=10)
        refs = ",".join(
            (self.env["NEXA_B16_PYTORCH_IMAGE_REF"], self.env["NEXA_B16_INFERENCE_IMAGE_REF"])
        )
        self.worker_command = [
            "docker",
            "run",
            "--detach",
            "--network",
            "host",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount",
            f"type=bind,src={self.root},dst={self.root}",
            "--env",
            f"NEXA_WORKER_API_URL=http://127.0.0.1:{port}",
            "--env",
            f"NEXA_WORKER_STATE_ROOT={self.root}",
            "--env",
            f"NEXA_INSTALLATION_ID={INSTALLATION_ID}",
            "--env",
            f"NEXA_LOCAL_WORKER_ID={WORKER_ID}",
            "--env",
            f"NEXA_LOCAL_WORKER_FINGERPRINT={FINGERPRINT}",
            "--env",
            f"NEXA_BOOTSTRAP_SECRET_FILE={self.root / 'bootstrap-secret'}",
            "--env",
            f"NEXA_CPU_IMAGE_REF={self.image}",
            "--env",
            f"NEXA_WORKLOAD_IMAGE_REFS={refs}",
            self.worker_image,
            "nexa-worker",
        ]

    # -- operations ------------------------------------------------------------
    def submit(self, key, spec):
        accepted = _check(
            self.client.post(
                "/v1/jobs", headers={**self.write, "Idempotency-Key": key}, json={"spec": spec}
            ),
            202,
        )
        self.submitted[accepted["job_id"]] = key
        return accepted["job_id"]

    def result_files(self, job_id):
        """logical_name -> (checksum, bytes) of every RESULT_FILE, checked against its row."""
        result = _check(
            self.client.get(
                f"/v1/jobs/{job_id}/result", headers={"X-Nexa-Tenant-Id": self.tenant_id}
            ),
            200,
        )
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    s.artifact_references.c.logical_name,
                    s.artifacts.c.artifact_id,
                    s.artifacts.c.checksum,
                )
                .join(
                    s.artifacts,
                    s.artifacts.c.artifact_id == s.artifact_references.c.artifact_id,
                )
                .where(
                    s.artifact_references.c.owner_type == "RESULT",
                    s.artifact_references.c.owner_id == UUID(result["result_id"]),
                    s.artifact_references.c.purpose == "RESULT_FILE",
                )
            ).all()
        files = {}
        for name, artifact_id, checksum in rows:
            data = _check_output(self.client, self.tenant_id, str(artifact_id))
            assert "sha256:" + hashlib.sha256(data).hexdigest() == checksum
            files[name] = (checksum, data)
        return result["result_id"], files

    def _blob_path(self, artifact_id):
        with self.engine.connect() as connection:
            key = connection.execute(
                select(s.artifacts.c.blob_key).where(s.artifacts.c.artifact_id == artifact_id)
            ).scalar_one()
        return self.client.app.state.services.artifact.store._path_for_key(key)

    def checkpoint_file(self, checkpoint_id, logical_name):
        with self.engine.connect() as connection:
            return connection.execute(
                select(s.artifact_references.c.artifact_id).where(
                    s.artifact_references.c.owner_type == "CHECKPOINT",
                    s.artifact_references.c.owner_id == checkpoint_id,
                    s.artifact_references.c.purpose == "CHECKPOINT_FILE",
                    s.artifact_references.c.logical_name == logical_name,
                )
            ).scalar_one()

    def corrupt_blob(self, artifact_id):
        """Fault injection on the test storage root only: flip the first byte.

        The blob may be shared with a later job's artifact (per-tenant content dedup),
        so the bytes and mode are returned for :meth:`repair_blob`.
        """
        path = self._blob_path(artifact_id)
        original = path.read_bytes()
        saved = (original, stat.S_IMODE(path.stat().st_mode))
        os.chmod(path, 0o600)
        path.write_bytes(bytes([original[0] ^ 0x01]) + original[1:])
        return saved

    def repair_blob(self, artifact_id, saved):
        raw, mode = saved
        path = self._blob_path(artifact_id)
        descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(path, mode)
        assert path.read_bytes() == raw

    def delete_blob(self, artifact_id):
        """Fault injection on the test storage root only: the blob file disappears."""
        self._blob_path(artifact_id).unlink()

    def take_blob(self, artifact_id):
        """Delete a blob but keep its bytes and mode for :meth:`put_back_blob`.

        Committed artifacts are deduplicated per tenant by content, so a deterministic
        chunk file of one job is the same blob as that chunk of every later job: a
        scenario that deletes it must put it back before the next scenario runs.
        """
        path = self._blob_path(artifact_id)
        saved = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        path.unlink()
        return saved

    def blob_intact(self, artifact_id, checksum):
        path = self._blob_path(artifact_id)
        return path.is_file() and "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() == (
            checksum
        )

    def put_back_blob(self, artifact_id, saved):
        raw, mode = saved
        path = self._blob_path(artifact_id)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def chunks(self, job_id):
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(
                        s.recognized_chunks.c.chunk_id,
                        s.recognized_chunks.c.range_start,
                        s.recognized_chunks.c.range_end,
                        s.recognized_chunks.c.artifact_id,
                        s.recognized_chunks.c.checksum,
                        s.recognized_chunks.c.source_attempt_id,
                        s.recognized_chunks.c.source_job_fence,
                        s.recognized_chunks.c.session_id,
                    )
                    .where(s.recognized_chunks.c.job_id == UUID(job_id))
                    .order_by(s.recognized_chunks.c.range_start)
                ).mappings()
            ]

    def blob_json(self, artifact_id):
        """A committed JSON manifest read from the test storage root (checksum verified)."""
        with self.engine.connect() as connection:
            checksum = connection.execute(
                select(s.artifacts.c.checksum).where(s.artifacts.c.artifact_id == artifact_id)
            ).scalar_one()
        raw = self._blob_path(artifact_id).read_bytes()
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == checksum
        return json.loads(raw)

    def checkpoint_manifest(self, checkpoint_id):
        with self.engine.connect() as connection:
            artifact_id = connection.execute(
                select(s.checkpoints.c.manifest_artifact_id).where(
                    s.checkpoints.c.checkpoint_id == checkpoint_id
                )
            ).scalar_one()
        return self.blob_json(artifact_id)

    def result_manifest(self, job_id):
        with self.engine.connect() as connection:
            artifact_id = connection.execute(
                select(s.results.c.manifest_artifact_id).where(s.results.c.job_id == UUID(job_id))
            ).scalar_one()
        return self.blob_json(artifact_id)

    def execution_identity(self, job_id):
        context = s.attempts.c.execution_context
        with self.engine.connect() as connection:
            return [
                {
                    "adapter_id": adapter,
                    "adapter_version": version,
                    "image_digest": digest,
                }
                for adapter, version, digest in connection.execute(
                    select(
                        context["adapter_id"].astext,
                        context["adapter_version"].astext,
                        context["image_digest"].astext,
                    )
                    .where(s.attempts.c.job_id == UUID(job_id))
                    .order_by(s.attempts.c.attempt_number)
                )
            ]

    def progress(self, job_id):
        """Last runtime progress each attempt reported (information, not an oracle)."""
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    select(s.attempts.c.progress_snapshot)
                    .where(s.attempts.c.job_id == UUID(job_id))
                    .order_by(s.attempts.c.attempt_number)
                ).scalars()
            )

    def wait_state(self, job_id, states, timeout):
        return _wait(
            lambda: (job := self.job(job_id))["state"] in states and job,
            f"job {job_id} in {sorted(states)}",
            timeout=timeout,
            pause=0.5,
        )


def compare_training(baseline, candidate, rules):
    """Metrics of a resumed lineage versus the uninterrupted baseline (tolerance.json)."""
    exact = {
        name: baseline[name] == candidate[name]
        for name in (
            "steps",
            "epochs",
            "epoch_sample_order_digests",
            "input_checksum",
            "spec_checksum",
            "architecture_id",
            "eval_items",
        )
    }
    absolute = {
        name: {
            "baseline": baseline[name],
            "candidate": candidate[name],
            "difference": abs(baseline[name] - candidate[name]),
            "limit": limit,
            "within": abs(baseline[name] - candidate[name]) <= limit,
        }
        for name, limit in rules["absolute"].items()
    }

    def relative(a, b, limit):
        difference = abs(a - b) / max(abs(a), math.ulp(0.0))
        return {
            "baseline": a,
            "candidate": b,
            "relative": difference,
            "within": difference <= limit,
        }

    norms = {
        name: relative(baseline[name], candidate[name], rules["relative"][name])
        for name in ("model_l2_norm", "optimizer_momentum_l2_norm")
    }
    parameters = {
        name: relative(
            value, candidate["parameter_l2_norms"][name], rules["relative"]["parameter_l2_norms.*"]
        )
        for name, value in baseline["parameter_l2_norms"].items()
    }
    same_names = set(baseline["parameter_l2_norms"]) == set(candidate["parameter_l2_norms"])
    return {
        "exact": exact,
        "absolute": absolute,
        "relative": {**norms, "parameter_l2_norms": parameters},
        "parameter_names_equal": same_names,
        "within_tolerance": all(exact.values())
        and same_names
        and all(row["within"] for row in absolute.values())
        and all(row["within"] for row in norms.values())
        and all(row["within"] for row in parameters.values()),
    }


_LINEAGE_KEYS = {"job_id", "session_id", "attempt_id", "job_fence", "result_id"}


def stable_provenance(provenance):
    """Result provenance without the per-job/attempt identity (compared exactly)."""
    return {key: value for key, value in provenance.items() if key not in _LINEAGE_KEYS}


def chunk_report(chunks, baseline, item_count, chunk_size):
    """One RecognizedChunk per chunk_id, exact coverage [0, N), checksums versus baseline."""
    ids = [row["chunk_id"] for row in chunks]
    ranges = [(row["range_start"], row["range_end"]) for row in chunks]
    expected = [
        (start, min(start + chunk_size, item_count)) for start in range(0, item_count, chunk_size)
    ]
    sources = {}
    for row in chunks:
        sources[str(row["source_attempt_id"])] = sources.get(str(row["source_attempt_id"]), 0) + 1
    mismatched = [
        row["chunk_id"] for row in chunks if baseline.get(row["chunk_id"]) != row["checksum"]
    ]
    return {
        "recognized": len(chunks),
        "distinct_chunk_ids": len(set(ids)),
        "coverage_exact": ranges == expected,
        "ids_formatted": ids == [f"chunk-{index:08d}" for index in range(len(expected))],
        "chunks_per_source_attempt": sources,
        "checksums_equal_baseline": not mismatched and len(chunks) == len(baseline),
        "mismatched_chunk_ids": mismatched,
    }


__all__ = [
    "B16Harness",
    "MemorySampler",
    "assert_hardened",
    "chunk_report",
    "compare_training",
    "data_file",
    "environment",
    "fixture",
    "hardening",
    "inspect_image",
    "register_templates",
    "stable_provenance",
    "tolerance",
]
