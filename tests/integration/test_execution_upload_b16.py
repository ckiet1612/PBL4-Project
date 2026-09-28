"""B16: attempt upload media is allowlisted by the job's registered WorkloadAdapter."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import update

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration._factories import seed_authority, seed_job, seed_tenant_graph
from tests.integration.test_execution_closure_b11 import _checksum, _worker_headers
from tests.integration.test_worker_api_b10 import WORKER_ID, _bootstrap_worker, _create_incarnation

pytestmark = pytest.mark.postgres


def _running(engine, client, *, adapter_id, state="RUNNING"):
    credential = _bootstrap_worker(client)
    incarnation = _create_incarnation(
        client, credential, nonce=str(new_uuid7()), key=f"b16-upload-{adapter_id}"
    )
    with engine.begin() as connection:
        graph = seed_tenant_graph(connection, label="b16-upload")
        connection.execute(
            update(s.template_versions)
            .where(s.template_versions.c.template_id == graph["template_id"])
            .values(adapter_id=adapter_id)
        )
        job = seed_job(connection, graph, state="RUNNING")
        worker = {
            "worker_id": UUID(WORKER_ID),
            "incarnation_id": UUID(incarnation["worker_incarnation_id"]),
        }
        ids = seed_authority(connection, graph, job, worker)
        connection.execute(
            update(s.attempts)
            .where(s.attempts.c.attempt_id == ids["attempt_id"])
            .values(state=state, started_at=datetime.now(UTC))
        )
    return credential, {
        "worker_id": WORKER_ID,
        "worker_incarnation_id": incarnation["worker_incarnation_id"],
        "attempt_id": str(ids["attempt_id"]),
        "allocation_id": str(ids["allocation_id"]),
        "lease_id": str(ids["lease_id"]),
        "job_fence": 1,
    }


def _post(client, credential, authority, media_type, key, *, kind="RESULT_FILE"):
    content = b"\x00safetensors-bytes"
    return client.post(
        f"/v1/attempts/{authority['attempt_id']}/artifacts",
        headers={
            **_worker_headers(credential, authority),
            "Content-Type": "application/octet-stream",
            "Idempotency-Key": key,
            "X-Artifact-Kind": kind,
            "X-Artifact-Media-Type": media_type,
            "X-Artifact-Checksum": _checksum(content),
            "X-Artifact-Size": str(len(content)),
        },
        content=content,
    )


@pytest.mark.parametrize(
    ("adapter_id", "media_type", "status"),
    [
        ("cpu.iterative", "application/octet-stream", 422),
        ("pytorch.cifar10", "application/octet-stream", 201),
        ("pytorch.cifar10", "application/x-ndjson", 422),
        ("batch.inference", "application/x-ndjson", 201),
        ("pytorch.cifar10", "application/vnd.nexa.cpu-iterative-result+json", 422),
    ],
)
def test_upload_media_follows_job_adapter(
    migrated_postgres_engine, tmp_path, adapter_id, media_type, status
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential, authority = _running(migrated_postgres_engine, client, adapter_id=adapter_id)
        response = _post(client, credential, authority, media_type, f"b16-upload-{status}-0001")
        assert response.status_code == status, response.text
        if status == 422:
            assert response.json()["code"] == "validation_failed"


@pytest.mark.parametrize(
    ("adapter_id", "state", "kind", "media_type", "status"),
    [
        # B16-R19: a chunked checkpoint cycle binds chunk files and the chunk-output
        # manifest inside the reserved checkpoint window.
        ("batch.inference", "CHECKPOINTING", "RESULT_FILE", "application/x-ndjson", 201),
        ("batch.inference", "CHECKPOINTING", "RESULT_FILE", "application/vnd.apache.parquet", 201),
        ("batch.inference", "CHECKPOINTING", "CHUNK_OUTPUT_MANIFEST", "application/json", 201),
        ("batch.inference", "RUNNING", "CHUNK_OUTPUT_MANIFEST", "application/json", 201),
        # The summary is a result-only file; other adapters keep their single window.
        ("batch.inference", "CHECKPOINTING", "RESULT_FILE", "application/json", 409),
        ("batch.inference", "RUNNING", "CHECKPOINT_FILE", "application/json", 409),
        ("pytorch.cifar10", "CHECKPOINTING", "RESULT_FILE", "application/octet-stream", 409),
        ("pytorch.cifar10", "RUNNING", "CHUNK_OUTPUT_MANIFEST", "application/json", 422),
    ],
)
def test_chunk_uploads_follow_the_chunked_windows(
    migrated_postgres_engine, tmp_path, adapter_id, state, kind, media_type, status
):
    with _client(migrated_postgres_engine, tmp_path) as client:
        credential, authority = _running(
            migrated_postgres_engine, client, adapter_id=adapter_id, state=state
        )
        key = f"b16-window-{state}-{kind}-{status}"[:64]
        response = _post(client, credential, authority, media_type, key, kind=kind)
        assert response.status_code == status, response.text
