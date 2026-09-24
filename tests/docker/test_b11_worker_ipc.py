"""Real Docker relay startup must fit the worker's bounded IPC receive window."""

import hashlib
import json
import os
import subprocess
import time

import pytest

from nexa.worker.agent import WorkerAgent
from nexa.worker.docker_client import DockerCli, DockerControlChannel
from tests.docker.test_real_runner import CALLBACK, Peer, _production_container

pytestmark = pytest.mark.docker


def test_worker_receives_staged_messages_and_reads_tmpfs_output(tmp_path):
    if os.environ.get("NEXA_B11_RUNTIME_EVIDENCE") != "1":
        pytest.skip("opt-in B11 Linux Docker relay regression")
    with _production_container(tmp_path.resolve() / "ipc", iterations=100) as (executor, identity):
        with DockerControlChannel(identity.container_id) as connection:
            peer = Peer(connection)
            peer.send(
                {
                    "schema_version": 1,
                    "control_sequence": 1,
                    "type": "SET_AUTHORITY_DEADLINE",
                    "payload": {
                        "source_callback_id": CALLBACK,
                        "deadline_monotonic_ns": 2**63 - 1,
                    },
                }
            )
            assert peer.receive_ack(1)["accepted"] is True

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "1000:1000",
                    identity.container_id,
                    "python",
                    "-c",
                    "import json; s=json.load(open('/run/nexa/runner-state.json')); "
                    "print(any(m['type']=='RESULT_PREPARE' for m in s['pending_messages']))",
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if state.returncode == 0 and state.stdout.strip() == "True":
                break
            time.sleep(0.05)
        else:
            pytest.fail("real runner did not stage RESULT_PREPARE")

        record = executor.journal.load(identity.attempt_id)
        agent = WorkerAgent.for_test(
            object(),
            worker_id=record.authority.worker_id,
            incarnation_id=record.authority.worker_incarnation_id,
        )
        agent.journal = executor.journal
        agent.channel_factory = DockerControlChannel
        agent._adopted[identity.attempt_id] = record.authority
        agent._ipc_once()
        runner_state = executor.journal.load(identity.attempt_id).runner_state or {}
        assert runner_state["message_sequences"]["highest"] >= 3
        assert runner_state["pending_execution_message"]["type"] == "RESULT_PREPARE"
        on_runner = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "exec",
                    "--user",
                    "1000:1000",
                    identity.container_id,
                    "cat",
                    "/run/nexa/runner-state.json",
                ]
            )
        )
        descriptor = on_runner["result"]["descriptor"]
        output = DockerCli().read_output(identity.container_id, descriptor)
        assert "sha256:" + hashlib.sha256(output).hexdigest() == descriptor["checksum"]
