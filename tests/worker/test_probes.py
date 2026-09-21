import json

from nexa.worker.probes import DockerProbeBackend


class FakeDockerProbe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        self.calls.append(argv)
        if argv[:3] == ("docker", "info", "--format"):
            return (
                0,
                json.dumps(
                    {
                        "ServerVersion": "29.5.3",
                        "OSType": "linux",
                        "Architecture": "aarch64",
                        "NCPU": 4,
                        "MemTotal": 8 * 1024**3,
                        "CgroupVersion": "2",
                        "KernelVersion": "6.8.0",
                        "DefaultRuntime": "runc",
                        "Runtimes": {"runc": {}},
                        "SecurityOptions": ["name=seccomp", "name=apparmor"],
                    }
                ).encode(),
                b"",
            )
        if argv[:3] == ("docker", "image", "inspect"):
            return (
                0,
                json.dumps(
                    [
                        {
                            "Id": "sha256:" + "a" * 64,
                            "Architecture": "arm64",
                            "Os": "linux",
                            "RepoDigests": ["nexa/cpu@sha256:" + "b" * 64],
                            "Config": {
                                "Entrypoint": [
                                    "python",
                                    "-m",
                                    "nexa.workloads.trusted_runner",
                                ],
                                "Labels": {
                                    "io.nexa.adapter.id": "cpu.iterative",
                                    "io.nexa.adapter.version": "1.0.0",
                                    "io.nexa.framework": "NEXA_CPU",
                                    "io.nexa.framework.version": "1.0.0",
                                    "io.nexa.runner": "trusted-runner-v1",
                                },
                            },
                        }
                    ]
                ).encode(),
                b"",
            )
        raise AssertionError(argv)


def test_live_probe_backend_reads_docker_host_inventory() -> None:
    backend = DockerProbeBackend(FakeDockerProbe(), image_ref="nexa/cpu@sha256:" + "b" * 64)
    probe = backend.snapshot()
    assert probe.system == "Linux"
    assert probe.architecture == "linux/arm64"
    assert probe.host_cpu_millis == 4000
    assert probe.host_memory_bytes == 8 * 1024**3
    assert probe.cgroups_version == 2
    assert probe.seccomp_available is True
    assert probe.images[0].verified is True
    assert probe.adapters[0].adapter_id == "cpu.iterative"
    assert probe.frameworks[0].framework == "NEXA_CPU"


def test_live_probe_does_not_advertise_unverified_base_image_capabilities() -> None:
    backend = FakeDockerProbe()
    original = backend.run

    def run(argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        code, stdout, stderr = original(argv, timeout_seconds)
        if argv[:3] == ("docker", "image", "inspect"):
            payload = json.loads(stdout)
            payload[0]["Config"] = {"Entrypoint": ["python3"]}
            stdout = json.dumps(payload).encode()
        return code, stdout, stderr

    backend.run = run  # type: ignore[method-assign]
    probe = DockerProbeBackend(backend, image_ref="python@sha256:" + "b" * 64).snapshot()
    assert probe.images[0].verified is False
    assert probe.adapters == ()
    assert probe.frameworks == ()


def test_live_probe_does_not_advertise_image_for_a_different_architecture() -> None:
    backend = FakeDockerProbe()
    original = backend.run

    def run(argv: tuple[str, ...], timeout_seconds: float) -> tuple[int, bytes, bytes]:
        code, stdout, stderr = original(argv, timeout_seconds)
        if argv[:3] == ("docker", "image", "inspect"):
            payload = json.loads(stdout)
            payload[0]["Architecture"] = "amd64"
            stdout = json.dumps(payload).encode()
        return code, stdout, stderr

    backend.run = run  # type: ignore[method-assign]
    probe = DockerProbeBackend(backend, image_ref="nexa/cpu@sha256:" + "b" * 64).snapshot()
    assert probe.images[0].architecture == "linux/amd64"
    assert probe.images[0].verified is False
    assert probe.adapters == ()
    assert probe.frameworks == ()
