"""B19-R15: PID, scratch and attempt log bounds come from the worker configuration."""

from types import SimpleNamespace

import pytest

from nexa.config import ConfigError, ResourceLimits, resource_limits
from nexa.worker import adapter_dispatch, dispatch, main
from nexa.worker.docker_config import (
    RUNNER_LOG_FILE_BYTES,
    attempt_bounds,
    build_container_config,
)
from nexa.worker.executor import DockerExecutor
from nexa.worker.journal import ExecutionJournal
from tests.worker import b16_claims as c
from tests.worker.test_docker_config import start
from tests.worker.test_models import context

MIB = 1024**2
GIB = 1024**3
IMAGE = "registry.invalid/cpu@" + context().image_digest


def _limits(**changes) -> ResourceLimits:
    values = {"container_pid_limit": 64, "scratch_max_bytes": 96 * MIB}
    values["attempt_log_max_bytes"] = 2 * MIB
    return ResourceLimits(**{**values, **changes})


@pytest.mark.parametrize(
    ("memory", "scratch_max", "expected"),
    [
        (GIB, 2 * GIB, GIB // 4),  # memory share wins
        (GIB, 96 * MIB, 96 * MIB),  # configured maximum wins
        (8 * GIB, 2 * GIB, 2 * GIB),
        (64 * MIB, 2 * GIB, 16 * MIB),
    ],
)
def test_scratch_is_the_smaller_of_the_maximum_and_a_quarter_of_memory(
    memory, scratch_max, expected
):
    scratch, log = attempt_bounds(memory, _limits(scratch_max_bytes=scratch_max))
    assert scratch == expected
    assert scratch < memory
    assert log == 2 * MIB


def test_container_config_uses_the_configured_pid_limit_and_caps_the_json_file():
    default = build_container_config(start(), image=IMAGE)
    assert default.pid_limit == 512
    request = start()
    configured = build_container_config(request, image=IMAGE, pid_limit=64)
    argv = configured.argv()
    assert argv[argv.index("--pids-limit") + 1] == "64"
    # The workload output goes to the runner, which only counts it; the json-file
    # holds the runner's own short messages and keeps a small cap of its own.
    assert f"max-size={min(request.log_bytes, RUNNER_LOG_FILE_BYTES)}" in argv
    assert argv[-1] == str(request.log_bytes)
    for bad in (31, 32_769):
        with pytest.raises(ValueError, match="pid"):
            build_container_config(request, image=IMAGE, pid_limit=bad)


def test_large_attempt_log_bound_reaches_the_runner_but_not_the_json_file():
    from dataclasses import replace

    request = replace(start(), log_bytes=100 * MIB)
    argv = build_container_config(request, image=IMAGE).argv()
    assert f"max-size={RUNNER_LOG_FILE_BYTES}" in argv
    assert argv[-2:] == ("--log-limit", str(100 * MIB))


def test_dispatch_builders_take_scratch_and_log_from_the_limits(tmp_path):
    from tests.worker.test_checkpoint_flow_b14 import claim_context

    cpu = dispatch.execution_request(
        claim_context(), tmp_path / "input.json", "linux/amd64", limits=_limits()
    )
    memory = cpu.context.resources.memory_bytes
    assert cpu.scratch_bytes == min(96 * MIB, memory // 4)
    assert cpu.log_bytes == 2 * MIB
    defaults = dispatch.execution_request(claim_context(), tmp_path / "input.json", "linux/amd64")
    assert defaults.scratch_bytes == min(2 * GIB, memory // 4)
    assert defaults.log_bytes == 100 * MIB

    claim = c.training_claim()
    adapter = adapter_dispatch.adapter_execution_request(
        claim, tmp_path, c.ARCH, limits=_limits(scratch_max_bytes=64 * MIB)
    )
    assert adapter.scratch_bytes == min(64 * MIB, adapter.context.resources.memory_bytes // 4)
    assert adapter.log_bytes == 2 * MIB
    assert adapter.adapter_launch is not None


def test_the_executor_carries_its_limits_into_the_container(tmp_path):
    executor = DockerExecutor(
        ExecutionJournal(tmp_path / "journal"),
        object(),
        image_ref="registry.invalid/cpu",
        staging_root=tmp_path / "staging",
        limits=_limits(),
    )
    assert executor.limits == _limits()
    assert DockerExecutor(
        ExecutionJournal(tmp_path / "journal-2"), object(), image_ref="registry.invalid/cpu"
    ).limits == resource_limits({})


def test_worker_main_reads_limits_before_starting(monkeypatch):
    monkeypatch.setattr("sys.argv", ["nexa-worker"])
    monkeypatch.setenv("NEXA_CONTAINER_PID_LIMIT", "31")
    with pytest.raises(SystemExit) as caught:
        main.main()
    assert "NEXA_CONTAINER_PID_LIMIT" in str(caught.value)

    seen = {}
    monkeypatch.setenv("NEXA_CONTAINER_PID_LIMIT", "64")
    config = SimpleNamespace(worker_id="worker")
    monkeypatch.setattr(main.WorkerConfig, "from_environment", classmethod(lambda cls: config))

    def run(config, *, bootstrap, ops_bind, limits):
        seen["limits"] = limits
        return 0

    monkeypatch.setattr(main, "run", run)
    assert main.main() == 0
    assert seen["limits"].container_pid_limit == 64


def test_invalid_limit_text_is_a_config_error():
    with pytest.raises(ConfigError):
        resource_limits({"NEXA_ATTEMPT_LOG_MAX_BYTES": "lots"})
