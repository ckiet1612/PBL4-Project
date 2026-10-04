"""Hardened Docker create configuration built from immutable execution data."""

import re
from dataclasses import dataclass

from nexa.config import ResourceLimits

from .models import InputMount, StartExecution

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
DEFAULT_PID_LIMIT = 512
PID_LIMIT_RANGE = (32, 32_768)
# /tmp scratch is a tmpfs and counts against the memory limit, so it is capped at a
# quarter of the allocated memory as well as at NEXA_SCRATCH_MAX_BYTES.
SCRATCH_MEMORY_DIVISOR = 4
# Workload stdout/stderr goes to the runner, which only counts it against the attempt
# log bound (LOG_OVERFLOW fails the attempt; nothing is truncated). The json-file log
# holds only the runner's own short messages, so it keeps the earlier 1 MiB cap.
RUNNER_LOG_FILE_BYTES = 1024**2


def attempt_bounds(memory_bytes: int, limits: ResourceLimits) -> tuple[int, int]:
    """(scratch_bytes, log_bytes) for an attempt; scratch always stays below memory."""
    scratch = min(limits.scratch_max_bytes, memory_bytes // SCRATCH_MEMORY_DIVISOR)
    return scratch, limits.attempt_log_max_bytes


@dataclass(frozen=True, slots=True)
class ContainerConfig:
    image: str
    user: str
    network_mode: str
    read_only: bool
    cap_drop: tuple[str, ...]
    cap_add: tuple[str, ...]
    no_new_privileges: bool
    seccomp_profile: str
    privileged: bool
    pid_limit: int
    cpu_millis: int
    memory_bytes: int
    scratch_bytes: int
    log_bytes: int
    runtime_limit_seconds: int
    restart_policy: str
    labels: dict[str, str]
    mounts: tuple[InputMount, ...] = ()
    control_dir: str | None = None

    def argv(self) -> tuple[str, ...]:
        args = [
            "docker",
            "create",
            "--user",
            self.user,
            "--network",
            self.network_mode,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(self.pid_limit),
            "--memory",
            str(self.memory_bytes),
            "--memory-swap",
            str(self.memory_bytes),
            "--cpus",
            f"{self.cpu_millis / 1000:.3f}",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={self.scratch_bytes}",
            "--tmpfs",
            "/output:rw,noexec,nosuid,nodev,size=16777216,uid=1001,gid=1000,mode=0770",
            "--tmpfs",
            "/run/nexa:rw,noexec,nosuid,nodev,size=1048576,uid=1000,gid=1000,mode=0711",
            "--log-driver",
            "json-file",
            "--log-opt",
            f"max-size={min(self.log_bytes, RUNNER_LOG_FILE_BYTES)}",
            "--log-opt",
            "max-file=1",
            "--restart",
            self.restart_policy,
        ]
        for capability in self.cap_add:
            args.extend(("--cap-add", capability))
        for key, value in sorted(self.labels.items()):
            args.extend(("--label", f"{key}={value}"))
        for mount in self.mounts:
            args.extend(
                (
                    "--mount",
                    f"type=bind,src={mount.source_path},dst={mount.target_path},readonly",
                )
            )
        if self.control_dir is not None:
            args.extend(
                (
                    "--mount",
                    f"type=bind,src={self.control_dir},dst=/run/nexa-input,readonly",
                    "--env",
                    "NEXA_CONTROL_SOCKET=/run/nexa/control.sock",
                    "--env",
                    "NEXA_RUNNER_STATE=/run/nexa/runner-state.json",
                    "--env",
                    "NEXA_LAUNCH_SPEC=/run/nexa-input/launch-spec.json",
                )
            )
        args.extend(
            (
                self.image,
                "--runtime-limit",
                str(self.runtime_limit_seconds),
                "--log-limit",
                str(self.log_bytes),
            )
        )
        return tuple(args)


def build_container_config(
    request: StartExecution,
    *,
    image: str,
    control_dir: str | None = None,
    installation_id: str | None = None,
    pid_limit: int = DEFAULT_PID_LIMIT,
) -> ContainerConfig:
    digest = request.context.image_digest
    if "@" not in image or image.rsplit("@", 1)[1] != digest or not _DIGEST.fullmatch(digest):
        raise ValueError("image must use the exact verified digest")
    if request.scratch_bytes >= request.context.resources.memory_bytes:
        raise ValueError("scratch_bytes must fit inside the hard memory budget")
    if not PID_LIMIT_RANGE[0] <= pid_limit <= PID_LIMIT_RANGE[1]:
        raise ValueError("pid_limit is outside the contract range")
    labels = {
        "nexa.managed": "true",
        "nexa.attempt_id": request.context.authority.attempt_id,
        "nexa.allocation_id": request.context.authority.allocation_id,
        "nexa.startup_nonce": request.startup_nonce,
    }
    if installation_id is not None:
        labels["nexa.installation_id"] = installation_id
    return ContainerConfig(
        image=image,
        user="1000:1000",
        network_mode="none",
        read_only=True,
        cap_drop=("ALL",),
        cap_add=(),
        no_new_privileges=True,
        seccomp_profile="default",
        privileged=False,
        pid_limit=pid_limit,
        cpu_millis=request.context.resources.cpu_millis,
        memory_bytes=request.context.resources.memory_bytes,
        scratch_bytes=request.scratch_bytes,
        log_bytes=request.log_bytes,
        runtime_limit_seconds=request.runtime_limit_seconds,
        restart_policy="no",
        labels=labels,
        mounts=request.input_mounts,
        control_dir=control_dir,
    )
