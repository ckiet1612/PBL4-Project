"""B16: the worker turns a PyTorch training claim into a closed v3 launch (no torch)."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
import rfc8785

from nexa.worker import adapter_dispatch as ad
from nexa.worker.dispatch import RestoreUnavailable
from nexa.worker.models import AdapterLaunch, CpuWorkloadSpec, InputMount
from nexa.worker.result_flow import checksum
from nexa.workloads import adapter_launch
from tests.worker import b16_claims as c
from tests.worker.test_checkpoint_flow_b14 import claim_context as cpu_claim

DIRECTORY = Path("/var/lib/nexa/work/attempt")


def _checkpoint(context):
    return ad.adapter_checkpoint(
        context, ad.adapter_of(context), image_capable=True, architecture=c.ARCH
    )


def _request(context, *, restore=True):
    checkpoint = _checkpoint(context)
    if context["restore_checkpoint"] is None or not restore:
        return ad.adapter_execution_request(context, DIRECTORY, c.ARCH, checkpoint=checkpoint)
    block, ordered = ad.verify_adapter_restore_manifest(
        context, ad.adapter_of(context), checkpoint[0]
    )
    return ad.adapter_execution_request(
        context, DIRECTORY, c.ARCH, checkpoint=checkpoint, restore=block, restore_files=ordered
    )


def _reseal(restore):
    manifest = restore["manifest"]
    body = {k: v for k, v in manifest.items() if k != "manifest_checksum"}
    manifest["manifest_checksum"] = checksum(body)
    raw = rfc8785.dumps(manifest)
    restore["record"]["manifest_checksum"] = "sha256:" + hashlib.sha256(raw).hexdigest()


def test_cpu_claim_keeps_the_cpu_dispatch() -> None:
    assert ad.adapter_of(cpu_claim()) is None


def test_training_claim_builds_the_validated_v3_launch() -> None:
    context = c.training_claim()
    request = _request(context)
    spec = request.adapter_launch.spec
    assert request.cpu_workload is None and request.checkpoint is None
    assert request.adapter_launch.interval_seconds == 5
    assert spec["schema_version"] == 3
    assert spec["threads"] == 1
    assert spec["spec_checksum"] == checksum(context["spec"])
    assert spec["parameters"] == context["spec"]["parameters"]
    assert spec["checkpoint"] == {
        "state_path": adapter_launch.TRAINING_STATE_PATH,
        "compatibility": adapter_launch.compatibility(c.ARCH, c.FRAMEWORK_VERSION, True),
    }
    assert spec["restore"] is None
    assert spec["provenance"]["attempt_id"] == context["authority"]["attempt_id"]
    assert spec["provenance"]["job_fence"] == context["authority"]["job_fence"]
    assert [(m.target_path, m.source_path) for m in request.input_mounts] == [
        ("/input/dataset.arrow", str(DIRECTORY / "dataset"))
    ]
    assert request.context.framework == "PYTORCH"
    assert request.context.framework_version == c.FRAMEWORK_VERSION
    assert adapter_launch.workload_command(spec)[:3] == (
        "python",
        "-m",
        "nexa.workloads.pytorch_cifar10",
    )


@pytest.mark.parametrize(("requested", "expected"), [(1, 5), (5, 5), (45, 45), (600, 60)])
def test_interval_is_clamped_to_the_contract(requested, expected) -> None:
    assert _checkpoint(c.training_claim(interval=requested))[1] == expected


@pytest.mark.parametrize(
    "claim",
    [
        lambda: c.training_claim(checkpointable=False),
        lambda: c.training_claim(requirement={**c.REQUIREMENT, "framework": "NEXA_CPU"}),
        lambda: c.training_claim(requirement={**c.REQUIREMENT, "device": "CUDA"}),
        lambda: c.training_claim(requirement={**c.REQUIREMENT, "cuda_runtime_min": "12.4"}),
        lambda: c.training_claim(requirement={**c.REQUIREMENT, "framework_version": "2.x"}),
    ],
)
def test_unsupported_checkpoint_launches_without_checkpoints(claim) -> None:
    context = claim()
    assert _checkpoint(context) is None


def test_image_without_the_checkpoint_label_launches_without_checkpoints() -> None:
    context = c.training_claim()
    assert (
        ad.adapter_checkpoint(
            context, ad.adapter_of(context), image_capable=False, architecture=c.ARCH
        )
        is None
    )
    request = ad.adapter_execution_request(context, DIRECTORY, c.ARCH)
    assert request.adapter_launch.spec["checkpoint"] is None
    assert request.adapter_launch.interval_seconds is None


def test_claimed_restore_on_an_incapable_launch_is_unavailable() -> None:
    context = c.training_claim()
    context["restore_checkpoint"], _ = c.sealed_training_restore(context)
    with pytest.raises(RestoreUnavailable):
        ad.adapter_checkpoint(
            context, ad.adapter_of(context), image_capable=False, architecture=c.ARCH
        )


def test_restore_mounts_every_file_in_manifest_order() -> None:
    context = c.training_claim()
    context["restore_checkpoint"], files = c.sealed_training_restore(context, step=3)
    request = _request(context)
    restore = request.adapter_launch.spec["restore"]
    names = ["model.safetensors", "optimizer.safetensors", "rng.safetensors", "training-state.json"]
    assert [item["logical_name"] for item in restore["files"]] == names
    assert restore["cursor"]["step"] == 3
    assert restore["checkpoint_sequence"] == 1
    targets = {m.target_path: m for m in request.input_mounts}
    for name in names:
        mount = targets[adapter_launch.restore_path(name)]
        assert mount.source_path == str(DIRECTORY / "restore" / name)
        assert mount.content_checksum == "sha256:" + hashlib.sha256(files[name]).hexdigest()
    pairs = ad.adapter_downloads(
        context,
        DIRECTORY,
        restore_files=ad.verify_adapter_restore_manifest(
            context, ad.adapter_of(context), _checkpoint(context)[0]
        )[1],
    )
    assert [path.name for _, path in pairs] == ["dataset", *names]


def test_inherited_manual_retry_restore_is_accepted() -> None:
    context = c.training_claim()
    restore, _ = c.sealed_training_restore(context)
    restore["record"]["job_id"] = "018f0d60-7b6a-7a3f-9d82-1aa39c4f30b7"
    restore["manifest"]["provenance"]["job_id"] = restore["record"]["job_id"]
    restore["manifest"]["provenance"]["session_id"] = "018f0d60-7b6a-7a3e-9d82-1aa39c4f30b7"
    _reseal(restore)
    context["restore_checkpoint"] = restore
    assert _request(context).adapter_launch.spec["restore"] is not None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["manifest"]["provenance"].update(adapter_id="cpu.iterative"),
        lambda r: r["manifest"]["provenance"].update(image_digest="sha256:" + "f" * 64),
        lambda r: r["manifest"]["provenance"].update(spec_checksum="sha256:" + "f" * 64),
        lambda r: r["manifest"]["provenance"].update(input_checksum="sha256:" + "f" * 64),
        lambda r: r["manifest"]["provenance"].update(session_id=r["record"]["checkpoint_id"]),
        lambda r: r["manifest"]["compatibility"].update(restart_safe=False),
        lambda r: r["manifest"]["compatibility"].update(architecture="linux/arm64"),
        lambda r: r["manifest"]["compatibility"].update(framework_version="2.8.0"),
        lambda r: r["manifest"].update(state_components=["ACCUMULATOR"]),
        lambda r: r["manifest"]["cursor"].update(step=10_000),
        lambda r: r["manifest"]["cursor"].update(accumulator=1),
        lambda r: r["manifest"]["files"].reverse(),
        lambda r: r["manifest"]["files"].pop(),
        lambda r: r["manifest"]["files"][0].update(size_bytes=1),
        lambda r: r["manifest"]["files"][3].update(media_type="application/octet-stream"),
        lambda r: r["files"][1].update(state="PENDING"),
        lambda r: r["files"][1].update(tenant_id=r["record"]["checkpoint_id"]),
        lambda r: r["files"][2].update(kind="RESULT_FILE"),
        lambda r: r["files"].append(dict(r["files"][0])),
        lambda r: r["manifest"].update(chunk_output_manifest={}),
    ],
)
def test_restore_manifest_deviation_is_unavailable(mutate) -> None:
    context = c.training_claim()
    restore, _ = c.sealed_training_restore(context)
    mutate(restore)
    _reseal(restore)
    context["restore_checkpoint"] = restore
    with pytest.raises(RestoreUnavailable):
        ad.verify_adapter_restore_manifest(context, ad.adapter_of(context), _checkpoint(context)[0])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["record"].update(state="DELETED"),
        lambda r: r["record"].update(manifest_checksum="sha256:" + "0" * 64),
        lambda r: r["record"].update(sequence=9),
        lambda r: r["manifest"].update(manifest_checksum="sha256:" + "0" * 64),
        lambda r: r.pop("files"),
    ],
)
def test_unsealed_restore_identity_is_unavailable(mutate) -> None:
    context = c.training_claim()
    restore, _ = c.sealed_training_restore(context)
    mutate(restore)
    context["restore_checkpoint"] = restore
    with pytest.raises(RestoreUnavailable):
        ad.verify_adapter_restore_manifest(context, ad.adapter_of(context), _checkpoint(context)[0])


def test_restore_files_must_be_the_manifest_cursor_state() -> None:
    context = c.training_claim()
    context["restore_checkpoint"], files = c.sealed_training_restore(context, step=3)
    block, _ = ad.verify_adapter_restore_manifest(
        context, ad.adapter_of(context), _checkpoint(context)[0]
    )
    ad.verify_adapter_restore_files(files, context=context, restore=block, threads=1)
    with pytest.raises(RestoreUnavailable):
        ad.verify_adapter_restore_files(files, context=context, restore=block, threads=2)
    with pytest.raises(RestoreUnavailable):
        ad.verify_adapter_restore_files(
            c.job_checkpoint_files(context, 4), context=context, restore=block, threads=1
        )
    foreign = c.training_claim(parameters={**context["spec"]["parameters"], "seed": 8})
    with pytest.raises(RestoreUnavailable):
        ad.verify_adapter_restore_files(
            c.job_checkpoint_files(foreign, 3), context=context, restore=block, threads=1
        )
    broken = dict(files)
    broken["rng.safetensors"] = files["rng.safetensors"][:-1]
    with pytest.raises(RestoreUnavailable):
        ad.verify_adapter_restore_files(broken, context=context, restore=block, threads=1)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x.update(execution_intent="PAUSE"),
        lambda x: x["template_snapshot"]["capability_requirement"].update(
            architectures=["linux/arm64"]
        ),
        lambda x: x["input_artifacts"].append({**x["input_artifacts"][0], "artifact_id": "x"}),
        lambda x: x["input_artifacts"][0].update(kind="INPUT"),
        lambda x: x["spec"].update(input_artifact_id="018f0d60-7b6a-7a3d-9d82-1aa39c4f30b7"),
    ],
)
def test_malformed_training_claim_is_rejected(mutate) -> None:
    context = copy.deepcopy(c.training_claim())
    mutate(context)
    with pytest.raises(ValueError):
        ad.adapter_execution_request(context, DIRECTORY, c.ARCH)


def test_claimed_restore_without_launch_restore_is_rejected() -> None:
    context = c.training_claim()
    context["restore_checkpoint"], _ = c.sealed_training_restore(context)
    with pytest.raises(ValueError, match="disagree"):
        _request(context, restore=False)


def test_cpu_claim_is_not_an_adapter_graph() -> None:
    with pytest.raises(ValueError):
        ad.adapter_execution_request(cpu_claim(), DIRECTORY, c.ARCH)


def _mount(request, target, **changes):
    return tuple(
        replace(m, **changes) if m.target_path == target else m for m in request.input_mounts
    )


@pytest.mark.parametrize(
    "tamper",
    [
        lambda r: {"spec": {**r.adapter_launch.spec, "threads": 2}},
        lambda r: {
            "spec": {
                **r.adapter_launch.spec,
                "provenance": {
                    **r.adapter_launch.spec["provenance"],
                    "job_fence": 99,
                },
            }
        },
        lambda r: {
            "spec": {
                **r.adapter_launch.spec,
                "startup_nonce": "018f0d60-7b6a-7a3c-9d82-1aa39c4f30b7",
            }
        },
        lambda r: {
            "mounts": _mount(r, "/input/dataset.arrow", content_checksum="sha256:" + "0" * 64)
        },
        lambda r: {"mounts": _mount(r, "/input/dataset.arrow", target_path="/input/input.json")},
        lambda r: {"mounts": r.input_mounts + r.input_mounts[:1]},
        lambda r: {"mounts": _mount(r, "/input/restore/model.safetensors", size_bytes=7)},
        lambda r: {"mounts": r.input_mounts[:-1]},
        lambda r: {
            "cpu": CpuWorkloadSpec(
                iterations=10, seed=1, modulus=97, spec_checksum=c.h.SPEC_CHECKSUM
            )
        },
    ],
)
def test_start_execution_rejects_a_launch_detached_from_its_context(tamper) -> None:
    context = c.training_claim()
    context["restore_checkpoint"], _ = c.sealed_training_restore(context)
    request = _request(context)
    change = tamper(request)
    launch = request.adapter_launch
    if "spec" in change:
        launch = AdapterLaunch(spec=change["spec"], interval_seconds=launch.interval_seconds)
    with pytest.raises(ValueError):
        replace(
            request,
            adapter_launch=launch,
            input_mounts=change.get("mounts", request.input_mounts),
            cpu_workload=change.get("cpu"),
        )


@pytest.mark.parametrize("interval", [None, 4, 61, True])
def test_checkpoint_launch_requires_a_contract_interval(interval) -> None:
    request = _request(c.training_claim())
    with pytest.raises(ValueError, match="interval"):
        AdapterLaunch(spec=request.adapter_launch.spec, interval_seconds=interval)


def test_launch_without_checkpoint_rejects_an_interval() -> None:
    request = ad.adapter_execution_request(c.training_claim(), DIRECTORY, c.ARCH)
    with pytest.raises(ValueError, match="interval"):
        AdapterLaunch(spec=request.adapter_launch.spec, interval_seconds=5)


def test_mount_type_is_the_worker_input_mount() -> None:
    request = _request(c.training_claim())
    assert all(isinstance(m, InputMount) for m in request.input_mounts)
