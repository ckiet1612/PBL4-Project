"""B16: worker value objects accept every registered adapter and reject crossed families."""

from dataclasses import replace

import pytest

from nexa.worker.models import (
    AllocationIdentity,
    CpuWorkloadSpec,
    InputMount,
    StartExecution,
)
from tests.worker.test_models import CHECKSUM, UUIDS, context


def _ml_context(template_id="pytorch-cifar10-cnn", adapter_id="pytorch.cifar10", **changes):
    return replace(
        context(),
        template_id=template_id,
        adapter_id=adapter_id,
        framework=changes.pop("framework", "PYTORCH"),
        framework_version=changes.pop("framework_version", "2.8.0"),
        **changes,
    )


@pytest.mark.parametrize(
    ("template_id", "adapter_id"),
    [("pytorch-cifar10-cnn", "pytorch.cifar10"), ("batch-inference", "batch.inference")],
)
def test_registered_pytorch_context_is_accepted(template_id, adapter_id):
    built = _ml_context(template_id, adapter_id)
    assert (built.template_id, built.adapter_id, built.framework) == (
        template_id,
        adapter_id,
        "PYTORCH",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"template_id": "cpu-iterative"},
        {"adapter_id": "shell.exec"},
        {"adapter_version": "2.0.0"},
        {"framework": "NEXA_CPU"},
    ],
)
def test_crossed_or_unregistered_context_is_rejected(changes):
    with pytest.raises(ValueError, match="template_id is not supported"):
        _ml_context(**changes)


def test_cpu_workload_spec_requires_the_cpu_adapter():
    ml = _ml_context()
    with pytest.raises(ValueError, match="CPU iterative adapter"):
        StartExecution(
            context=ml,
            allocation=AllocationIdentity(
                allocation_id=UUIDS["allocation"],
                attempt_id=UUIDS["attempt"],
                resources=ml.resources,
            ),
            startup_nonce=UUIDS["nonce"],
            operation_sequence=1,
            scratch_bytes=64 * 1024 * 1024,
            log_bytes=1024 * 1024,
            runtime_limit_seconds=30,
            input_mounts=(
                InputMount(
                    artifact_id=UUIDS["job"],
                    source_path="/var/lib/nexa/input",
                    target_path="/input/input.json",
                    content_checksum=CHECKSUM,
                    size_bytes=2,
                ),
            ),
            cpu_workload=CpuWorkloadSpec(iterations=10, seed=1, modulus=97, spec_checksum=CHECKSUM),
        )
