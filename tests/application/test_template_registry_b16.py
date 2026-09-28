"""B16 template definition files: strict schema_version 1, registry-owned families, CPU-only."""

import copy
import json
from pathlib import Path

import pytest

from nexa.application.template_registry import (
    TemplateDefinitionError,
    load_definition,
    validate_definition,
    version_values,
)

TEMPLATES = Path(__file__).resolve().parents[2] / "deploy" / "templates"
DIGEST = "sha256:" + "e" * 64


def _definition(name="pytorch-cifar10-cnn.v1.json"):
    return json.loads((TEMPLATES / name).read_text())


def test_repository_ships_exactly_three_executable_template_files():
    names = sorted(path.name for path in TEMPLATES.glob("*.json"))
    assert names == [
        "batch-inference.v1.json",
        "cpu-iterative.v1.json",
        "pytorch-cifar10-cnn.v1.json",
    ]
    for name in names:
        definition = load_definition((TEMPLATES / name).read_bytes())
        row = version_values(definition, DIGEST)
        assert row["capability_requirements"]["image_digest"] == DIGEST
        assert len(row["capability_requirements"]) == 10
        assert row["resource_bounds"]["resources"]["gpu_count"] == 0


def test_ml_templates_admit_a_one_gib_job():
    for name in ("pytorch-cifar10-cnn.v1.json", "batch-inference.v1.json"):
        memory = _definition(name)["resource_bounds"]["resources"]["memory_bytes"]
        assert memory["minimum"] <= 1024**3 <= memory["maximum"]


def test_cpu_template_keeps_the_b08_parameter_contract():
    definition = _definition("cpu-iterative.v1.json")
    assert [p["name"] for p in definition["parameter_schema"]] == [
        "iterations",
        "seed",
        "modulus",
    ]
    assert definition["capability_requirement"]["framework"] == "NEXA_CPU"


def _mutated(path, value, name="pytorch-cifar10-cnn.v1.json"):
    definition = copy.deepcopy(_definition(name))
    target = definition
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return definition


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("adapter_id",), "shell.exec"),
        (("adapter_id",), "cpu.iterative"),
        (("template_id",), "batch-inference"),
        (("allowed_devices",), ["CPU", "CUDA"]),
        (("resource_bounds", "resources", "gpu_count"), 1),
        (("resource_bounds", "resources", "gpu_count"), {"minimum": 0, "maximum": 1}),
        (("resource_bounds", "runtime_limit_seconds"), 301),
        (("capability_requirement", "framework"), "NEXA_CPU"),
        (("capability_requirement", "framework_version"), "latest"),
        (("capability_requirement", "device"), "CUDA"),
        (("capability_requirement", "cuda_runtime_min"), "12.1.0"),
        (("capability_requirement", "architectures"), ["linux/riscv64"]),
        (("artifact_requirements", "input", "media_type"), "application/json"),
        (("schema_version",), 2),
    ],
)
def test_invalid_definitions_are_rejected(path, value):
    with pytest.raises(TemplateDefinitionError):
        validate_definition(_mutated(path, value))


def test_unknown_members_and_image_digest_in_file_are_rejected():
    extra = {**_definition(), "image_digest": DIGEST}
    with pytest.raises(TemplateDefinitionError):
        validate_definition(extra)
    capability = _mutated(("capability_requirement", "image_digest"), DIGEST)
    with pytest.raises(TemplateDefinitionError):
        validate_definition(capability)


@pytest.mark.parametrize("digest", ["sha256:" + "E" * 64, "sha256:abc", "latest", None])
def test_registration_digest_must_be_a_sha256_digest(digest):
    with pytest.raises(TemplateDefinitionError):
        version_values(_definition(), digest)


def test_duplicate_or_malformed_json_is_rejected():
    raw = (TEMPLATES / "cpu-iterative.v1.json").read_bytes()
    with pytest.raises(TemplateDefinitionError):
        load_definition(raw[:-3])
    with pytest.raises(TemplateDefinitionError):
        load_definition(b'{"schema_version": 1, "schema_version": 1}')
