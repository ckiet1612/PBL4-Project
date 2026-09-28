"""B16 static WorkloadAdapter registry: one declarative table replaces adapter hardcodes."""

import ast
import pathlib

import pytest

from nexa.domain import workload_adapters as registry


def test_three_executable_adapters_are_registered_exactly():
    assert {(d.adapter_id, d.adapter_version) for d in registry.DESCRIPTORS} == {
        ("cpu.iterative", "1.0.0"),
        ("pytorch.cifar10", "1.0.0"),
        ("batch.inference", "1.0.0"),
    }
    cpu = registry.descriptor("cpu.iterative", "1.0.0")
    assert cpu.template_id == "cpu-iterative"
    assert cpu.framework == "NEXA_CPU"
    assert cpu.manifest_framework == "PYTHON"
    assert cpu.framework_version == "1.0.0"
    assert cpu.checkpoint_format == "cpu-state-v1"
    assert cpu.state_components == ("ACCUMULATOR",)
    assert not cpu.chunked
    training = registry.descriptor("pytorch.cifar10", "1.0.0")
    assert training.template_id == "pytorch-cifar10-cnn"
    assert training.framework == "PYTORCH"
    assert training.manifest_framework == "PYTORCH"
    assert training.state_components == (
        "MODEL",
        "OPTIMIZER",
        "RNG_PYTHON",
        "RNG_NUMPY",
        "RNG_TORCH_CPU",
        "SAMPLER",
    )
    inference = registry.descriptor("batch.inference", "1.0.0")
    assert inference.template_id == "batch-inference"
    assert inference.state_components == ("INFERENCE_CURSOR",)
    assert inference.chunked


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        (("cpu.iterative", "1.0.0"), ("INPUT", "application/vnd.nexa.cpu-iterative-input+json")),
        (("pytorch.cifar10", "1.0.0"), ("DATASET", "application/vnd.apache.arrow.file")),
        (("batch.inference", "1.0.0"), ("DATASET", "application/vnd.apache.arrow.file")),
    ],
)
def test_input_requirements_keep_the_b06_media_types(adapter, expected):
    descriptor = registry.descriptor(*adapter)
    assert (descriptor.input_kind, descriptor.input_media_type) == expected


def test_model_requirement_only_for_inference():
    assert registry.descriptor("cpu.iterative", "1.0.0").model_kind is None
    assert registry.descriptor("pytorch.cifar10", "1.0.0").model_kind is None
    inference = registry.descriptor("batch.inference", "1.0.0")
    assert (inference.model_kind, inference.model_media_type) == (
        "MODEL",
        "application/octet-stream",
    )


def test_unknown_or_mismatched_adapter_is_rejected():
    assert registry.descriptor("cpu.iterative", "1.0.1") is None
    assert registry.descriptor("shell.exec", "1.0.0") is None
    assert (
        registry.template_descriptor(
            {
                "template_id": "cpu-iterative",
                "adapter_id": "pytorch.cifar10",
                "adapter_version": "1.0.0",
            }
        )
        is None
    )
    assert registry.template_descriptor(
        {"template_id": "cpu-iterative", "adapter_id": "cpu.iterative", "adapter_version": "1.0.0"}
    ) is registry.descriptor("cpu.iterative", "1.0.0")


def test_image_labels_must_match_one_descriptor_exactly():
    labels = {
        "io.nexa.runner": "trusted-runner-v1",
        "io.nexa.adapter.id": "pytorch.cifar10",
        "io.nexa.adapter.version": "1.0.0",
        "io.nexa.framework": "PYTORCH",
        "io.nexa.framework.version": "2.8.0",
        "io.nexa.runner.checkpoint": "pytorch-cifar10-state-v1",
    }
    assert registry.descriptor_for_labels(labels) is registry.descriptor("pytorch.cifar10", "1.0.0")
    for key, bad in [
        ("io.nexa.framework", "NEXA_CPU"),
        ("io.nexa.runner.checkpoint", "cpu-state-v1"),
        ("io.nexa.framework.version", "latest"),
        ("io.nexa.adapter.version", "2.0.0"),
    ]:
        assert registry.descriptor_for_labels({**labels, key: bad}) is None
    cpu_labels = {
        "io.nexa.runner": "trusted-runner-v1",
        "io.nexa.adapter.id": "cpu.iterative",
        "io.nexa.adapter.version": "1.0.0",
        "io.nexa.framework": "NEXA_CPU",
        "io.nexa.framework.version": "1.0.0",
    }
    # B09-B13 CPU images predate the checkpoint label and still advertise the adapter.
    assert registry.descriptor_for_labels(cpu_labels) is registry.descriptor(
        "cpu.iterative", "1.0.0"
    )
    assert (
        registry.descriptor_for_labels({**cpu_labels, "io.nexa.framework.version": "1.0.1"}) is None
    )


def test_upload_media_allowlist_is_per_adapter_and_never_widens_cpu():
    cpu = registry.descriptor("cpu.iterative", "1.0.0")
    assert cpu.upload_media_types["RESULT_FILE"] == frozenset(
        {"application/vnd.nexa.cpu-iterative-result+json", "application/json"}
    )
    assert cpu.upload_media_types["CHECKPOINT_FILE"] == frozenset({"application/json"})
    assert "CHUNK_OUTPUT_MANIFEST" not in cpu.upload_media_types
    inference = registry.descriptor("batch.inference", "1.0.0")
    assert inference.upload_media_types["RESULT_FILE"] >= {
        "application/x-ndjson",
        "application/vnd.apache.parquet",
    }
    assert inference.upload_media_types["CHUNK_OUTPUT_MANIFEST"] == frozenset({"application/json"})


def test_registry_is_stdlib_only():
    tree = ast.parse(pathlib.Path(registry.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "dataclasses", "re", "types", "collections"}
