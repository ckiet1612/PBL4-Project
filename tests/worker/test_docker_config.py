from dataclasses import replace

from nexa.worker.docker_config import ContainerConfig, build_container_config
from nexa.worker.models import AllocationIdentity, InputMount, StartExecution
from tests.worker.test_models import context


def start() -> StartExecution:
    return StartExecution(
        context=context(),
        allocation=AllocationIdentity(
            allocation_id=context().authority.allocation_id,
            attempt_id=context().authority.attempt_id,
            resources=context().resources,
        ),
        startup_nonce=context().startup_nonce,
        operation_sequence=1,
        scratch_bytes=64 * 1024 * 1024,
        log_bytes=1024 * 1024,
        runtime_limit_seconds=30,
    )


def test_container_config_is_hardened_and_identity_bound() -> None:
    request = start()
    request = replace(
        request,
        input_mounts=(
            InputMount(
                artifact_id="018f0d60-7b6a-7a27-9d82-1aa39c4f30b7",
                source_path="/var/lib/nexa/staging/input-a",
                target_path="/input/input.json",
                content_checksum=context().input_checksum,
                size_bytes=1,
            ),
        ),
    )
    config = build_container_config(
        request,
        image="registry.invalid/cpu@" + context().image_digest,
        control_dir="/var/lib/nexa/control/attempt",
    )
    assert isinstance(config, ContainerConfig)
    assert config.image.endswith(context().image_digest)
    assert config.user == "1000:1000"
    assert config.network_mode == "none"
    assert config.read_only is True
    assert config.cap_drop == ("ALL",)
    assert config.cap_add == ()
    assert config.no_new_privileges is True
    assert config.seccomp_profile == "default"
    assert config.privileged is False
    assert config.pid_limit > 0
    assert config.memory_bytes == context().resources.memory_bytes
    assert config.restart_policy == "no"
    assert config.labels["nexa.attempt_id"] == context().authority.attempt_id
    assert config.labels["nexa.startup_nonce"] == context().startup_nonce
    assert config.mounts[0].target_path == "/input/input.json"
    argv = " ".join(config.argv())
    assert "--cap-add" not in config.argv()
    assert "readonly" in argv
    assert "type=bind,src=/var/lib/nexa/control/attempt,dst=/run/nexa-input,readonly" in argv
    assert "/output:rw,noexec,nosuid,nodev,size=16777216,uid=1001,gid=1000,mode=0770" in argv
    assert "/run/nexa:rw,noexec,nosuid,nodev,size=1048576,uid=1000,gid=1000,mode=0711" in argv
    assert "NEXA_CONTROL_SOCKET=/run/nexa/control.sock" in argv
    assert "NEXA_RUNNER_STATE=/run/nexa/runner-state.json" in argv
    assert "NEXA_LAUNCH_SPEC=/run/nexa-input/launch-spec.json" in argv
    assert config.argv()[-5:] == (
        config.image,
        "--runtime-limit",
        str(request.runtime_limit_seconds),
        "--log-limit",
        str(request.log_bytes),
    )


def test_container_config_rejects_tag_or_client_command() -> None:
    try:
        build_container_config(start(), image="cpu:latest")
    except ValueError as exc:
        assert "digest" in str(exc)
    else:
        raise AssertionError("tag image must be rejected")
