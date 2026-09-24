from dataclasses import asdict

import pytest

from nexa.worker.agent import WorkerAgent
from nexa.worker.client import WorkerApiError
from tests.worker.test_docker_config import start


def test_non_null_offer_enters_dispatch_handler():
    auth = start().context.authority

    class Client:
        def poll(self, *args):
            return {"offer": {"authority": asdict(auth)}}

    agent = WorkerAgent.for_test(
        Client(), worker_id=auth.worker_id, incarnation_id=auth.worker_incarnation_id
    )
    agent._server_ready = agent._reconcile_complete = True
    offers = []
    agent._dispatch_offer = offers.append
    agent._poll_once()
    assert offers == [{"authority": asdict(auth)}]


def test_claim_context_maps_cpu_contract_to_executor(tmp_path):
    import hashlib

    from nexa.worker import dispatch

    request = start()
    artifact = {
        "artifact_id": request.context.job_id,
        "tenant_id": request.context.tenant_id,
        "checksum": "sha256:" + hashlib.sha256(b"{}").hexdigest(),
        "size_bytes": 2,
        "media_type": "application/json",
    }
    spec = {
        "template_id": "cpu-iterative",
        "template_version": 1,
        "input_artifact_id": artifact["artifact_id"],
        "parameters": {"iterations": 2, "seed": 1, "modulus": 100},
        "runtime_limit_seconds": 30,
    }
    context = {
        "authority": asdict(request.context.authority),
        "job_id": request.context.job_id,
        "logical_session_id": request.context.logical_session_id,
        "spec": spec,
        "execution_intent": "RUN",
        "restore_checkpoint": None,
        "template_snapshot": {"capability_requirement": {"architectures": ["linux/amd64"]}},
        "adapter_id": "cpu.iterative",
        "adapter_version": "1.0.0",
        "image_digest": request.context.image_digest,
        "startup_nonce": request.startup_nonce,
        "allocation": {"resources": asdict(request.context.resources)},
        "input_artifacts": [artifact],
    }
    assert hasattr(dispatch, "execution_request"), "missing context mapping"
    mapped = dispatch.execution_request(context, tmp_path / "input.json", "linux/amd64")
    assert mapped.context.authority == request.context.authority
    assert mapped.cpu_workload.iterations == 2
    assert mapped.input_mounts[0].content_checksum == artifact["checksum"]


@pytest.mark.parametrize(
    ("late_start", "delayed_cleanup", "failure_rejected"),
    [(False, False, False), (True, False, False), (True, True, False), (True, False, True)],
)
def test_production_dispatch_download_start_and_new_attempt_renewal(
    tmp_path, late_start, delayed_cleanup, failure_rejected
):
    import hashlib
    from types import SimpleNamespace

    from nexa.worker.docker_client import DockerCli
    from nexa.worker.executor import DockerExecutor
    from nexa.worker.journal import ExecutionJournal
    from nexa.worker.protocol import FrameDecoder, encode_frame
    from nexa.worker.state import PendingOperationStore
    from tests.worker.test_executor import FakeDocker

    requested = start()
    authority = requested.context.authority
    content = b'{"initial_value":17}'
    artifact = {
        "artifact_id": requested.context.job_id,
        "tenant_id": requested.context.tenant_id,
        "checksum": "sha256:" + hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "media_type": "application/vnd.nexa.cpu-iterative-input+json",
    }
    context = {
        "authority": asdict(authority),
        "job_id": requested.context.job_id,
        "logical_session_id": requested.context.logical_session_id,
        "spec": {
            "template_id": "cpu-iterative",
            "template_version": 1,
            "input_artifact_id": artifact["artifact_id"],
            "parameters": {"iterations": 2, "seed": 1, "modulus": 100},
            "runtime_limit_seconds": 30,
        },
        "execution_intent": "RUN",
        "restore_checkpoint": None,
        "template_snapshot": {"capability_requirement": {"architectures": ["linux/amd64"]}},
        "adapter_id": "cpu.iterative",
        "adapter_version": "1.0.0",
        "image_digest": requested.context.image_digest,
        "startup_nonce": requested.startup_nonce,
        "allocation": {"resources": asdict(requested.context.resources)},
        "input_artifacts": [artifact],
    }

    class Client:
        renewals = 0
        starts = 0
        failures = 0
        cleanups = 0

        def claim(self, attempt, callback, body):
            return {"accepted": True, "callback_id": callback, "execution_context": context}

        def download_execution(self, auth, descriptor, path):
            path.write_bytes(content)

        def start(self, attempt, callback, body):
            self.starts += 1
            if late_start:
                raise WorkerApiError(409, "state_conflict")
            return {
                "accepted": True,
                "callback_id": callback,
                "lease_duration_seconds": 45,
                "safety_margin_seconds": 5,
            }

        def fail(self, attempt, callback, body):
            self.failures += 1
            assert body["failure_class"] == "TIMEOUT"
            assert body["reason_code"] == "STARTUP_TIMEOUT"
            assert body["observation"]["observation_type"] == "CONTAINER"
            if failure_rejected:
                raise WorkerApiError(409, "stale_authority")
            return {"callback_id": callback, "accepted": True}

        def cleanup(self, attempt, callback, body):
            self.cleanups += 1
            assert body["proof"]["proof_type"] == "CONTAINER_STOPPED"
            if delayed_cleanup and self.cleanups == 1:
                return {
                    "callback_id": callback,
                    "verified": False,
                    "allocation_state": "QUARANTINED",
                }
            return {"callback_id": callback, "verified": True, "allocation_state": "RELEASED"}

        def reconciliation(self, *_args, **_kwargs):
            return self.page

        def renew(self, *args):
            self.renewals += 1
            return {"lease_duration_seconds": 45, "safety_margin_seconds": 5}

    class Channel:
        def settimeout(self, timeout):
            pass

        def sendall(self, raw):
            frame = FrameDecoder().feed(raw)[0]
            self.response = encode_frame(
                {
                    "schema_version": 1,
                    "ack_sequence": frame["control_sequence"],
                    "accepted": True,
                    "code": "ACCEPTED",
                }
            )

        def recv(self, maximum):
            return self.response

    client = Client()
    journal = ExecutionJournal(tmp_path / "journal")
    backend = FakeDocker()
    executor = DockerExecutor(
        journal, backend, image_ref="registry.invalid/cpu", staging_root=tmp_path / "staging"
    )
    agent = WorkerAgent(
        worker_id=authority.worker_id,
        incarnation_id=authority.worker_incarnation_id,
        installation_id="test",
        client=client,
        journal=journal,
        state=PendingOperationStore(tmp_path / "pending.json", boot_id="test"),
        docker=DockerCli(backend),
        executor=executor,
        provider=SimpleNamespace(discover=lambda: SimpleNamespace(architecture="linux/amd64")),
        channel_factory=lambda _: Channel(),
    )
    assert hasattr(agent, "_dispatch_offer"), "production dispatch orchestration absent"
    offer = {"authority": asdict(authority)}
    if late_start:
        with pytest.raises(WorkerApiError):
            agent._dispatch_offer(offer)
        assert backend.created == 1
        assert backend.stopped == [backend.container_id]
        assert backend.removed == [backend.container_id]
        if failure_rejected:
            assert client.failures == 1 and client.cleanups == 0
            assert {item["operation"] for item in agent.state.operations.values()} == {
                "claim",
                "start",
                "failure",
            }
            return
        assert client.failures == client.cleanups == 1
        if delayed_cleanup:
            identity = journal.load(authority.attempt_id).container
            client.page = {
                "items": [
                    {
                        "authority": asdict(authority),
                        "authority_state": "REVOKED",
                        "claim_state": "STARTED",
                        "startup_nonce": requested.startup_nonce,
                        "expected_container": {
                            "container_id": identity.container_id,
                            "runtime_identity_digest": identity.runtime_identity_digest,
                        },
                        "allocation": {"state": "QUARANTINED"},
                    }
                ],
                "page": {"next_cursor": None},
            }
            assert agent.reconcile_once().complete is True
            assert client.cleanups == 2
        assert agent.state.operations == {}
        return
    agent._dispatch_offer(offer)
    agent._dispatch_offer(offer)
    agent.renew_once()
    assert backend.created == 1
    assert client.starts == 1
    assert client.renewals == 1
    assert authority.attempt_id in agent._adopted
    assert journal.load(authority.attempt_id).runner_state["authority_deadline_monotonic_ns"] > 0


def test_invalid_downloaded_input_fails_attempt_and_releases_unclaimed_allocation(tmp_path):
    from types import SimpleNamespace

    import pytest

    from nexa.worker.docker_client import DockerCli
    from nexa.worker.errors import ExecutorError
    from nexa.worker.executor import DockerExecutor
    from nexa.worker.journal import ExecutionJournal
    from nexa.worker.state import PendingOperationStore
    from tests.worker.test_executor import FakeDocker

    request = start()
    authority = request.context.authority
    artifact = {
        "artifact_id": request.context.job_id,
        "tenant_id": request.context.tenant_id,
        "checksum": request.context.input_checksum,
        "size_bytes": 5,
        "media_type": "application/vnd.nexa.cpu-iterative-input+json",
    }
    context = {
        "authority": asdict(authority),
        "job_id": request.context.job_id,
        "logical_session_id": request.context.logical_session_id,
        "spec": {
            "template_id": "cpu-iterative",
            "template_version": 1,
            "input_artifact_id": artifact["artifact_id"],
            "parameters": {"iterations": 2, "seed": 1, "modulus": 100},
            "runtime_limit_seconds": 30,
        },
        "execution_intent": "RUN",
        "restore_checkpoint": None,
        "template_snapshot": {"capability_requirement": {"architectures": ["linux/amd64"]}},
        "adapter_id": "cpu.iterative",
        "adapter_version": "1.0.0",
        "image_digest": request.context.image_digest,
        "startup_nonce": request.startup_nonce,
        "allocation": {"resources": asdict(request.context.resources)},
        "input_artifacts": [artifact],
    }
    observed = []

    class Client:
        def claim(self, attempt, callback, body):
            return {"accepted": True, "callback_id": callback, "execution_context": context}

        def download_execution(self, auth, descriptor, path):
            path.write_bytes(b"wrong")

        def fail(self, attempt, callback, body):
            observed.append(("fail", body["failure_class"], body["reason_code"]))
            return {"accepted": True}

        def cleanup(self, attempt, callback, body):
            observed.append(("cleanup", body["proof"]["proof_type"]))
            return {"verified": True, "allocation_state": "RELEASED"}

    journal = ExecutionJournal(tmp_path / "journal")
    backend = FakeDocker()
    executor = DockerExecutor(
        journal, backend, image_ref="registry.invalid/cpu", staging_root=tmp_path / "staging"
    )
    agent = WorkerAgent(
        worker_id=authority.worker_id,
        incarnation_id=authority.worker_incarnation_id,
        installation_id="test",
        client=Client(),
        journal=journal,
        state=PendingOperationStore(tmp_path / "pending", boot_id="test"),
        docker=DockerCli(backend),
        executor=executor,
        provider=SimpleNamespace(discover=lambda: SimpleNamespace(architecture="linux/amd64")),
    )
    with pytest.raises(ExecutorError):
        agent._dispatch_offer({"authority": asdict(authority)})
    assert observed == [
        ("fail", "INVALID_INPUT", "INPUT_CHECKSUM_MISMATCH"),
        ("cleanup", "NO_CONTAINER"),
    ]
    assert backend.created == 0
