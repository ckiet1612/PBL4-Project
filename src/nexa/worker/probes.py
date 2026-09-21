"""Live Docker-host probe adapter."""

import json
import os
from typing import Protocol

from .capabilities import (
    AdapterCapability,
    FrameworkCapability,
    ImageCapability,
    ProbeBackend,
    ResourceProvider,
)
from .errors import DiscoveryError, DiscoveryErrorCode


class ProbeCommandBackend(Protocol):
    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]: ...


class DockerProbeBackend:
    def __init__(
        self, command_backend: ProbeCommandBackend | None = None, *, image_ref: str | None = None
    ) -> None:
        if command_backend is None:
            from .docker_client import SubprocessDockerBackend

            command_backend = SubprocessDockerBackend()
        self.command_backend = command_backend
        self.image_ref = image_ref

    def _run_json(self, argv: tuple[str, ...]) -> object:
        code, stdout, _ = self.command_backend.run(argv, timeout_seconds=5)
        if code != 0:
            raise RuntimeError("Docker probe command failed")
        try:
            return json.loads(stdout.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Docker probe returned invalid JSON") from exc

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
        image_capabilities: tuple[ImageCapability, ...] = ()
        adapters: tuple[AdapterCapability, ...] = ()
        frameworks: tuple[FrameworkCapability, ...] = ()
        if self.image_ref is not None:
            image_info = self._run_json(("docker", "image", "inspect", self.image_ref))
            if (
                not isinstance(image_info, list)
                or not image_info
                or not isinstance(image_info[0], dict)
            ):
                raise RuntimeError("Docker image inspect payload is invalid")
            image = image_info[0]
            digest = self.image_ref.rsplit("@", 1)[-1]
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
            adapter_id = labels.get("io.nexa.adapter.id")
            adapter_version = labels.get("io.nexa.adapter.version")
            framework = labels.get("io.nexa.framework")
            framework_version = labels.get("io.nexa.framework.version")
            metadata_verified = (
                runner_verified
                and adapter_id == "cpu.iterative"
                and adapter_version == "1.0.0"
                and framework == "NEXA_CPU"
                and framework_version == "1.0.0"
            )
            image_arch = {
                "aarch64": "linux/arm64",
                "arm64": "linux/arm64",
                "amd64": "linux/amd64",
            }.get(str(image.get("Architecture")), "unknown")
            verified = (
                digest_verified
                and metadata_verified
                and image.get("Os") == "linux"
                and image_arch == architecture
            )
            image_capabilities = (ImageCapability(digest, image_arch, verified),)
            if verified:
                adapters = (AdapterCapability(str(adapter_id), str(adapter_version)),)
                frameworks = (FrameworkCapability(str(framework), str(framework_version), "CPU"),)
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
            images=image_capabilities,
            adapters=adapters,
            frameworks=frameworks,
            gpu_devices=(),
        )


def live_provider() -> ResourceProvider:
    try:
        snapshot = DockerProbeBackend(image_ref=os.environ.get("NEXA_CPU_IMAGE_REF")).snapshot()
    except (RuntimeError, KeyError, TypeError, ValueError) as exc:
        raise DiscoveryError(
            DiscoveryErrorCode.PROBE_FAILED, "live Docker runtime probe failed"
        ) from exc
    return ResourceProvider(backend=snapshot)


__all__ = ["DockerProbeBackend", "ProbeBackend", "ResourceProvider", "live_provider"]
