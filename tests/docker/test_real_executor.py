import json
import os
import subprocess

import pytest

from nexa.worker.probes import DockerProbeBackend


def _image() -> str:
    value = os.environ.get("NEXA_B09_IMAGE_REF", "")
    if "@sha256:" not in value:
        pytest.fail("NEXA_B09_IMAGE_REF must be an exact digest reference")
    return value


def _evidence(scenario: str, **observations: object) -> None:
    if os.environ.get("NEXA_B09_EVIDENCE") == "1":
        print(
            json.dumps(
                {"evidence": "B09", "scenario": scenario, "status": "pass", **observations},
                sort_keys=True,
            ),
            flush=True,
        )


@pytest.mark.docker
def test_real_image_has_hardened_runtime_config() -> None:
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in real Docker suite; use scripts/b09_docker_tests.sh --require")
    image = _image()
    created = subprocess.run(
        [
            "docker",
            "create",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--user",
            "1000:1000",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "128",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            image,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    container_id = created.stdout.strip()
    try:
        completed = subprocess.run(
            ["docker", "inspect", container_id], check=True, capture_output=True, text=True
        )
        payload = json.loads(completed.stdout)[0]
        config = payload["Config"]
        host = payload["HostConfig"]
        assert config["User"] == "1000:1000"
        assert config["Entrypoint"] == ["python", "-m", "nexa.workloads.trusted_runner"]
        assert config["Labels"]["io.nexa.runner"] == "trusted-runner-v1"
        assert host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True
        assert "ALL" in (host.get("CapDrop") or [])
        assert host.get("CapAdd") in (None, [])
        assert host["RestartPolicy"]["Name"] in {"", "no"}
        _evidence(
            "hardened-runtime-config",
            image=image,
            user=config["User"],
            network=host["NetworkMode"],
            read_only=host["ReadonlyRootfs"],
            cap_drop=host.get("CapDrop") or [],
            cap_add=host.get("CapAdd") or [],
            pids_limit=host["PidsLimit"],
            memory_bytes=host["Memory"],
            memory_swap_bytes=host["MemorySwap"],
            restart=host["RestartPolicy"]["Name"] or "no",
        )
    finally:
        subprocess.run(["docker", "rm", "--force", container_id], check=True, capture_output=True)


@pytest.mark.docker
def test_real_discovery_advertises_only_the_verified_nexa_image() -> None:
    if os.environ.get("NEXA_RUN_DOCKER") != "1":
        pytest.skip("opt-in real Docker suite; use scripts/b09_docker_tests.sh --require")
    valid = DockerProbeBackend(image_ref=_image()).snapshot()
    assert valid.images[0].verified is True
    assert [(item.adapter_id, item.adapter_version) for item in valid.adapters] == [
        ("cpu.iterative", "1.0.0")
    ]
    assert [(item.framework, item.framework_version) for item in valid.frameworks] == [
        ("NEXA_CPU", "1.0.0")
    ]

    base_ref = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "python:3.12-slim",
            "--format",
            "{{index .RepoDigests 0}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    base = DockerProbeBackend(image_ref=base_ref).snapshot()
    assert base.images[0].verified is False
    assert base.adapters == ()
    assert base.frameworks == ()
    _evidence(
        "closed-image-discovery",
        verified_image=_image(),
        verified_adapter="cpu.iterative@1.0.0",
        rejected_image=base_ref,
        rejected_adapter_count=len(base.adapters),
        rejected_framework_count=len(base.frameworks),
    )
