"""Live Docker-host probe adapter."""

import json
import os
import re
from dataclasses import dataclass
from typing import Protocol

from nexa.domain import workload_adapters

from .capabilities import (
    AdapterCapability,
    FrameworkCapability,
    ImageCapability,
    ProbeBackend,
    ResourceProvider,
)
from .errors import DiscoveryError, DiscoveryErrorCode

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class _Advertised:
    adapter_id: str
    adapter_version: str
    framework: str
    label_version: str


class ProbeCommandBackend(Protocol):
    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]: ...


class DockerProbeBackend:
    def __init__(
        self,
        command_backend: ProbeCommandBackend | None = None,
        *,
        image_ref: str | None = None,
        image_refs: tuple[str, ...] = (),
    ) -> None:
        if command_backend is None:
            from .docker_client import SubprocessDockerBackend

            command_backend = SubprocessDockerBackend()
        self.command_backend = command_backend
        refs = ((image_ref,) if image_ref is not None else ()) + tuple(image_refs)
        self.image_refs = tuple(dict.fromkeys(refs))

    def _run_json(self, argv: tuple[str, ...]) -> object:
        code, stdout, _ = self.command_backend.run(argv, timeout_seconds=5)
        if code != 0:
            raise RuntimeError("Docker probe command failed")
        try:
            return json.loads(stdout.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Docker probe returned invalid JSON") from exc

    def _probe_image(self, image_ref: str, architecture: str):
        """Return (ImageCapability, verified adapter or None) for one digest-pinned image."""
        image_info = self._run_json(("docker", "image", "inspect", image_ref))
        if (
            not isinstance(image_info, list)
            or not image_info
            or not isinstance(image_info[0], dict)
        ):
            raise RuntimeError("Docker image inspect payload is invalid")
        image = image_info[0]
        digest = image_ref.rsplit("@", 1)[-1]
        repo_digests = image.get("RepoDigests", [])
        digest_verified = isinstance(repo_digests, list) and any(
            str(item).endswith(digest) for item in repo_digests
        )
        config = image.get("Config", {})
        if not isinstance(config, dict):
            config = {}
        labels = config.get("Labels", {})
        if not isinstance(labels, dict):
            labels = {}
        entrypoint = config.get("Entrypoint", [])
        runner_verified = (
            isinstance(entrypoint, list)
            and len(entrypoint) >= 3
            and [str(item) for item in entrypoint[-3:]]
            == ["python", "-m", "nexa.workloads.trusted_runner"]
            and labels.get("io.nexa.runner") == "trusted-runner-v1"
        )
        descriptor = workload_adapters.descriptor_for_labels(labels) if runner_verified else None
        image_arch = {
            "aarch64": "linux/arm64",
            "arm64": "linux/arm64",
            "amd64": "linux/amd64",
        }.get(str(image.get("Architecture")), "unknown")
        verified = (
            digest_verified
            and descriptor is not None
            and image.get("Os") == "linux"
            and image_arch == architecture
        )
        if not verified:
            return ImageCapability(digest, image_arch, False), None
        return ImageCapability(digest, image_arch, True), _Advertised(
            descriptor.adapter_id,
            descriptor.adapter_version,
            descriptor.framework,
            str(labels["io.nexa.framework.version"]),
        )

    def snapshot(self) -> ProbeBackend:
        info = self._run_json(("docker", "info", "--format", "{{json .}}"))
        if not isinstance(info, dict):
            raise RuntimeError("Docker info payload is invalid")
        architecture = {
            "aarch64": "linux/arm64",
            "arm64": "linux/arm64",
            "x86_64": "linux/amd64",
        }.get(str(info.get("Architecture")), "unknown")
        security_options = info.get("SecurityOptions", [])
        if not isinstance(security_options, list):
            security_options = []
        image_capabilities: list[ImageCapability] = []
        adapters: list[AdapterCapability] = []
        frameworks: list[FrameworkCapability] = []
        for image_ref in self.image_refs:
            image, adapter = self._probe_image(image_ref, architecture)
            image_capabilities.append(image)
            if adapter is None:
                continue
            advertised = AdapterCapability(adapter.adapter_id, adapter.adapter_version)
            if advertised not in adapters:
                adapters.append(advertised)
            framework = FrameworkCapability(adapter.framework, adapter.label_version, "CPU")
            if framework not in frameworks:
                frameworks.append(framework)
        return ProbeBackend(
            system=str(info.get("OSType", "unknown")).capitalize(),
            architecture=architecture,
            host_cpu_millis=int(info["NCPU"]) * 1000,
            host_memory_bytes=int(info["MemTotal"]),
            docker_version=str(info["ServerVersion"]),
            oci_runtime=str(info.get("DefaultRuntime", "unknown")),
            oci_runtime_version="unknown",
            cgroups_version=int(str(info.get("CgroupVersion", "0")).removeprefix("v")),
            kernel_release=str(info.get("KernelVersion", "unknown")),
            seccomp_available=any("seccomp" in str(item).lower() for item in security_options),
            images=tuple(image_capabilities),
            adapters=tuple(adapters),
            frameworks=tuple(frameworks),
            gpu_devices=(),
        )


def configured_image_refs() -> tuple[str, ...]:
    """Legacy NEXA_CPU_IMAGE_REF first, then NEXA_WORKLOAD_IMAGE_REFS; every ref digest-pinned."""
    refs = [os.environ.get("NEXA_CPU_IMAGE_REF") or ""]
    refs += os.environ.get("NEXA_WORKLOAD_IMAGE_REFS", "").split(",")
    unique = tuple(dict.fromkeys(ref.strip() for ref in refs if ref.strip()))
    for ref in unique:
        if ref.count("@") != 1 or not _DIGEST.fullmatch(ref.rsplit("@", 1)[1]):
            raise ValueError("workload image refs must be pinned by @sha256 digest")
    return unique


def live_provider() -> ResourceProvider:
    try:
        snapshot = DockerProbeBackend(image_refs=configured_image_refs()).snapshot()
    except (RuntimeError, KeyError, TypeError, ValueError) as exc:
        raise DiscoveryError(
            DiscoveryErrorCode.PROBE_FAILED, "live Docker runtime probe failed"
        ) from exc
    return ResourceProvider(backend=snapshot)


__all__ = [
    "DockerProbeBackend",
    "configured_image_refs",
    "ProbeBackend",
    "ResourceProvider",
    "live_provider",
]
