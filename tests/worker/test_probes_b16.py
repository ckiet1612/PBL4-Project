"""B16: the worker probes every configured image and advertises only registry matches."""

import json

import pytest

from nexa.worker import probes
from nexa.worker.probes import DockerProbeBackend, configured_image_refs

CPU = "nexa/cpu@sha256:" + "b" * 64
TRAIN = "nexa/pytorch-cifar10@sha256:" + "c" * 64
INFER = "nexa/batch-inference@sha256:" + "e" * 64

_LABELS = {
    CPU: {
        "io.nexa.adapter.id": "cpu.iterative",
        "io.nexa.adapter.version": "1.0.0",
        "io.nexa.framework": "NEXA_CPU",
        "io.nexa.framework.version": "1.0.0",
        "io.nexa.runner": "trusted-runner-v1",
        "io.nexa.runner.checkpoint": "cpu-state-v1",
    },
    TRAIN: {
        "io.nexa.adapter.id": "pytorch.cifar10",
        "io.nexa.adapter.version": "1.0.0",
        "io.nexa.framework": "PYTORCH",
        "io.nexa.framework.version": "2.8.0",
        "io.nexa.runner": "trusted-runner-v1",
        "io.nexa.runner.checkpoint": "pytorch-cifar10-state-v1",
    },
    INFER: {
        "io.nexa.adapter.id": "batch.inference",
        "io.nexa.adapter.version": "1.0.0",
        "io.nexa.framework": "PYTORCH",
        "io.nexa.framework.version": "2.8.0",
        "io.nexa.runner": "trusted-runner-v1",
        "io.nexa.runner.checkpoint": "batch-inference-state-v1",
    },
}


class FakeDocker:
    def __init__(self, labels=None):
        self.labels = labels or _LABELS
        self.inspected = []

    def run(self, argv, timeout_seconds):
        if argv[:3] == ("docker", "info", "--format"):
            info = {
                "ServerVersion": "29.5.3",
                "OSType": "linux",
                "Architecture": "x86_64",
                "NCPU": 8,
                "MemTotal": 16 * 1024**3,
                "CgroupVersion": "2",
                "KernelVersion": "6.8.0",
                "DefaultRuntime": "runc",
                "SecurityOptions": ["name=seccomp"],
            }
            return 0, json.dumps(info).encode(), b""
        ref = argv[3]
        self.inspected.append(ref)
        image = {
            "Architecture": "amd64",
            "Os": "linux",
            "RepoDigests": [ref],
            "Config": {
                "Entrypoint": ["python", "-m", "nexa.workloads.trusted_runner"],
                "Labels": self.labels[ref],
            },
        }
        return 0, json.dumps([image]).encode(), b""


def test_each_image_is_probed_and_advertises_its_own_adapter():
    docker = FakeDocker()
    probe = DockerProbeBackend(docker, image_refs=(CPU, TRAIN, INFER)).snapshot()
    assert docker.inspected == [CPU, TRAIN, INFER]
    assert [(i.image_digest, i.verified) for i in probe.images] == [
        (CPU.rsplit("@")[1], True),
        (TRAIN.rsplit("@")[1], True),
        (INFER.rsplit("@")[1], True),
    ]
    assert [(a.adapter_id, a.adapter_version) for a in probe.adapters] == [
        ("cpu.iterative", "1.0.0"),
        ("pytorch.cifar10", "1.0.0"),
        ("batch.inference", "1.0.0"),
    ]
    # Two PyTorch images with one framework/version collapse to one closed entry.
    assert [(f.framework, f.framework_version, f.device) for f in probe.frameworks] == [
        ("NEXA_CPU", "1.0.0", "CPU"),
        ("PYTORCH", "2.8.0", "CPU"),
    ]


def test_label_mismatch_keeps_image_unverified_and_adapter_unadvertised():
    labels = {**_LABELS, TRAIN: {**_LABELS[TRAIN], "io.nexa.runner.checkpoint": "cpu-state-v1"}}
    probe = DockerProbeBackend(FakeDocker(labels), image_refs=(CPU, TRAIN)).snapshot()
    assert [i.verified for i in probe.images] == [True, False]
    assert [a.adapter_id for a in probe.adapters] == ["cpu.iterative"]


def test_configured_refs_merge_legacy_cpu_ref_and_require_digests(monkeypatch):
    monkeypatch.setenv("NEXA_CPU_IMAGE_REF", CPU)
    monkeypatch.setenv("NEXA_WORKLOAD_IMAGE_REFS", f"{TRAIN},{INFER},{CPU}")
    assert configured_image_refs() == (CPU, TRAIN, INFER)
    monkeypatch.delenv("NEXA_WORKLOAD_IMAGE_REFS")
    assert configured_image_refs() == (CPU,)
    monkeypatch.setenv("NEXA_WORKLOAD_IMAGE_REFS", "nexa/pytorch:latest")
    with pytest.raises(ValueError, match="sha256"):
        configured_image_refs()
    monkeypatch.delenv("NEXA_CPU_IMAGE_REF")
    monkeypatch.delenv("NEXA_WORKLOAD_IMAGE_REFS")
    assert configured_image_refs() == ()


def test_live_provider_probes_every_configured_image(monkeypatch):
    docker = FakeDocker()

    def backend(command_backend=None, *, image_refs=()):
        return DockerProbeBackend(docker, image_refs=image_refs)

    monkeypatch.setattr(probes, "DockerProbeBackend", backend)
    monkeypatch.setenv("NEXA_CPU_IMAGE_REF", CPU)
    monkeypatch.setenv("NEXA_WORKLOAD_IMAGE_REFS", TRAIN)
    provider = probes.live_provider()
    assert docker.inspected == [CPU, TRAIN]
    assert provider is not None
