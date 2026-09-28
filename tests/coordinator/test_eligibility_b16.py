"""B16: eligibility is decided by registered adapter capability, not an adapter-ID literal."""

import pytest

from nexa.application.job_service import JobService
from nexa.coordinator.eligibility import _compatible, _possible

DIGEST = "sha256:" + "d" * 64


def _template(template_id, adapter_id, framework, *, version="2.8.0", enabled=True):
    return {
        "template_id": template_id,
        "version": 1,
        "adapter_id": adapter_id,
        "adapter_version": "1.0.0",
        "image_digest": DIGEST,
        "enabled": enabled,
        "capability_requirements": {
            "architectures": ["linux/amd64"],
            "adapter_id": adapter_id,
            "adapter_version": "1.0.0",
            "image_digest": DIGEST,
            "device": "CPU",
            "framework": framework,
            "framework_version": version,
            "cuda_runtime_min": None,
            "driver_min": None,
            "compute_capability_min": None,
        },
    }


def _inventory(adapter_id, framework, *, version="2.8.0"):
    return {
        "inventory_id": 7,
        "architecture": "linux/amd64",
        "workload_capabilities": {
            "adapters": [{"adapter_id": adapter_id, "adapter_version": "1.0.0"}],
            "images": [{"image_digest": DIGEST, "architecture": "linux/amd64", "verified": True}],
            "frameworks": [{"framework": framework, "framework_version": version, "device": "CPU"}],
        },
    }


@pytest.mark.parametrize(
    ("template_id", "adapter_id"),
    [("pytorch-cifar10-cnn", "pytorch.cifar10"), ("batch-inference", "batch.inference")],
)
def test_pytorch_template_is_compatible_by_inventory_capability(template_id, adapter_id):
    template = _template(template_id, adapter_id, "PYTORCH")
    assert _compatible(template, _inventory(adapter_id, "PYTORCH"))
    assert JobService.template_runs_on(template, _inventory(adapter_id, "PYTORCH"))
    # A different framework version, a missing adapter or a disabled template never match.
    assert not _compatible(template, _inventory(adapter_id, "PYTORCH", version="2.7.0"))
    assert not _compatible(template, _inventory("cpu.iterative", "NEXA_CPU", version="1.0.0"))
    assert not _compatible({**template, "enabled": False}, _inventory(adapter_id, "PYTORCH"))


def test_unregistered_or_mismatched_adapter_is_never_compatible():
    unknown = _template("pytorch-cifar10-cnn", "shell.exec", "PYTORCH")
    assert not _compatible(unknown, _inventory("shell.exec", "PYTORCH"))
    # The adapter must own the template family even when inventory advertises it.
    crossed = _template("cpu-iterative", "pytorch.cifar10", "PYTORCH")
    assert not _compatible(crossed, _inventory("pytorch.cifar10", "PYTORCH"))
    # Template framework must be the adapter's framework.
    wrong_framework = _template("pytorch-cifar10-cnn", "pytorch.cifar10", "NEXA_CPU")
    assert not _compatible(wrong_framework, _inventory("pytorch.cifar10", "NEXA_CPU"))


def test_gpu_request_is_never_possible_without_a_gpu_provider():
    template = _template("pytorch-cifar10-cnn", "pytorch.cifar10", "PYTORCH")
    state = {"cpu": 4000, "memory": 2**31, "inventory": _inventory("pytorch.cifar10", "PYTORCH")}
    spec = {
        "template_id": "pytorch-cifar10-cnn",
        "template_version": 1,
        "cpu_millis": 1000,
        "memory_bytes": 2**30,
        "gpu_count": 0,
    }
    templates = {("pytorch-cifar10-cnn", 1): template}
    assert _possible(spec, state, templates, {})
    assert not _possible({**spec, "gpu_count": 1}, state, templates, {})


def test_template_outside_an_adapter_family_keeps_the_b11_adapter_binding():
    # B11 templates such as cpu.iterative.<label> are bound by adapter only; a family
    # owned by one adapter (cpu-iterative, pytorch-cifar10-cnn, ...) never runs on another.
    legacy = _template("cpu.iterative.coordinator", "cpu.iterative", "NEXA_CPU", version="1.0.0")
    assert _compatible(legacy, _inventory("cpu.iterative", "NEXA_CPU", version="1.0.0"))
    crossed = _template("batch-inference", "pytorch.cifar10", "PYTORCH")
    assert not _compatible(crossed, _inventory("pytorch.cifar10", "PYTORCH"))
