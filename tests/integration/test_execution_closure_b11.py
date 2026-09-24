"""Production HTTP/DB closure for a fenced CPU result and graph download."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import pytest
import rfc8785
from sqlalchemy import insert, select, update

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.api.test_http_contract import _client
from tests.integration._factories import seed_authority, seed_job, seed_tenant_graph
from tests.integration.test_cli_b12_vertical import _success
from tests.integration.test_jobs_b08 import _running_api_process
from tests.integration.test_worker_api_b10 import WORKER_ID, _bootstrap_worker, _create_incarnation
from tests.integration.test_worker_authority_b10 import CONTAINER_ID, DIGEST

pytestmark = pytest.mark.postgres


def _checksum(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _worker_headers(credential, authority):
    return {
        "Authorization": f"Bearer {credential}",
        "X-Worker-Id": authority["worker_id"],
        "X-Worker-Incarnation-Id": authority["worker_incarnation_id"],
        "X-Allocation-Id": authority["allocation_id"],
        "X-Lease-Id": authority["lease_id"],
        "X-Job-Fence": str(authority["job_fence"]),
    }


def _upload(client, credential, authority, kind, media_type, content, key):
    headers = {
        **_worker_headers(credential, authority),
        "Content-Type": "application/octet-stream",
        "Idempotency-Key": key,
        "X-Artifact-Kind": kind,
        "X-Artifact-Media-Type": media_type,
        "X-Artifact-Checksum": _checksum(content),
        "X-Artifact-Size": str(len(content)),
    }
    response = client.post(
        f"/v1/attempts/{authority['attempt_id']}/artifacts", headers=headers, content=content
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_http_graph_result_recognition_cleanup_and_replay(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        incarnation = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-closure-incarnation"
        )
        input_content = b'{"seed":7}'
        store = client.app.state.services.artifact.store
        staged = store.begin_staging(
            "b11-closure-input", len(input_content), _checksum(input_content), "application/json"
        )
        store.append(staged, input_content)
        blob = store.commit_blob(staged)
        input_id = new_uuid7()
        with engine.begin() as connection:
            graph = seed_tenant_graph(connection, label="b11-closure")
            connection.execute(
                insert(s.artifacts).values(
                    artifact_id=input_id,
                    tenant_id=graph["tenant_id"],
                    kind="INPUT",
                    media_type="application/json",
                    size_bytes=len(input_content),
                    checksum=blob.checksum,
                    blob_key=blob.blob_key,
                    state="COMMITTED",
                    version=1,
                )
            )
            seeded_job = seed_job(connection, graph, state="RUNNING", artifact_id=input_id)
            worker = {
                "worker_id": UUID(WORKER_ID),
                "incarnation_id": UUID(incarnation["worker_incarnation_id"]),
            }
            ids = seed_authority(connection, graph, seeded_job, worker)
            connection.execute(
                update(s.attempts)
                .where(s.attempts.c.attempt_id == ids["attempt_id"])
                .values(state="RUNNING", started_at=datetime.now(UTC))
            )
            nonce = connection.execute(
                select(s.attempts.c.startup_nonce).where(
                    s.attempts.c.attempt_id == ids["attempt_id"]
                )
            ).scalar_one()
            connection.execute(
                insert(s.container_identities).values(
                    tenant_id=graph["tenant_id"],
                    job_id=seeded_job["job_id"],
                    attempt_id=ids["attempt_id"],
                    allocation_id=ids["allocation_id"],
                    startup_nonce=nonce,
                    executor_create_sequence=1,
                    container_id=CONTAINER_ID,
                    runtime_identity_digest=DIGEST,
                    created_at=datetime.now(UTC),
                )
            )
        authority = {
            "worker_id": WORKER_ID,
            "worker_incarnation_id": incarnation["worker_incarnation_id"],
            "attempt_id": str(ids["attempt_id"]),
            "allocation_id": str(ids["allocation_id"]),
            "lease_id": str(ids["lease_id"]),
            "job_fence": 1,
        }
        with engine.connect() as connection:
            job = connection.execute(select(s.jobs)).mappings().one()
            spec = connection.execute(select(s.job_specs)).mappings().one()
            template = connection.execute(select(s.template_versions)).mappings().one()
            input_row = (
                connection.execute(select(s.artifacts).where(s.artifacts.c.artifact_id == input_id))
                .mappings()
                .one()
            )
            session_id = connection.execute(select(s.logical_sessions.c.session_id)).scalar_one()
        with engine.begin() as connection:
            outside_id = new_uuid7()
            connection.execute(
                insert(s.artifacts).values(
                    artifact_id=outside_id,
                    tenant_id=job["tenant_id"],
                    kind="INPUT",
                    media_type="application/json",
                    size_bytes=len(input_content),
                    checksum="sha256:" + "d" * 64,
                    blob_key=f"blob/outside/{outside_id}",
                    state="COMMITTED",
                    version=1,
                )
            )
            other_graph = seed_tenant_graph(connection, label="b11-closure-other")
            context_input = {
                "artifact_id": str(input_row["artifact_id"]),
                "tenant_id": str(job["tenant_id"]),
                "kind": "INPUT",
                "media_type": "application/json",
                "size_bytes": len(input_content),
                "checksum": blob.checksum,
            }
            connection.execute(
                update(s.attempts)
                .where(s.attempts.c.attempt_id == UUID(authority["attempt_id"]))
                .values(execution_context={"input_artifacts": [context_input]})
            )
            for scope, scope_id in (
                ("GLOBAL", "global"),
                ("TENANT", str(job["tenant_id"])),
                ("USER", f"{job['tenant_id']}:{job['submitter_user_id']}"),
            ):
                connection.execute(
                    insert(s.admission_counters).values(
                        scope_type=scope,
                        scope_id=scope_id,
                        outstanding=1,
                        active_attempts=1,
                    )
                )
        worker_headers = _worker_headers(credential, authority)
        graph_url = f"/v1/attempts/{authority['attempt_id']}/execution-artifacts"
        valid = client.get(
            f"{graph_url}/{input_row['artifact_id']}/content", headers=worker_headers
        )
        assert valid.status_code == 200, valid.text
        assert valid.content == input_content
        assert valid.headers["content-type"] == "application/octet-stream"
        assert valid.headers["x-artifact-media-type"] == "application/json"
        partial = client.get(
            f"{graph_url}/{input_row['artifact_id']}/content",
            headers={**worker_headers, "Range": "bytes=0-3"},
        )
        assert partial.status_code == 206 and partial.content == input_content[:4]
        for invalid_id in (outside_id, other_graph["artifact_id"]):
            denied = client.get(f"{graph_url}/{invalid_id}/content", headers=worker_headers)
            assert denied.status_code == 404, denied.text

        bootstrap = client.post(
            "/v1/internal/admin-bootstrap",
            headers={
                "X-Nexa-Bootstrap-Secret": "b" * 32,
                "Idempotency-Key": "b11-closure-admin-key",
            },
            json={
                "username": "b11-closure@example.test",
                "display_name": "Closure Admin",
                "password": "correct-horse-battery-staple",
            },
        )
        assert bootstrap.status_code == 201, bootstrap.text
        with engine.begin() as connection:
            connection.execute(
                insert(s.memberships).values(
                    tenant_id=job["tenant_id"],
                    user_id=UUID(bootstrap.json()["user_id"]),
                    role="TENANT_ADMIN",
                )
            )
        login = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b11-closure@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert login.status_code == 200, login.text
        user_headers = {"X-Nexa-Tenant-Id": str(job["tenant_id"])}
        result_url = f"/v1/jobs/{job['job_id']}/result"
        assert client.get(result_url, headers=user_headers).status_code == 404
        assert (
            client.get(
                result_url, headers={"X-Nexa-Tenant-Id": str(other_graph["tenant_id"])}
            ).status_code
            == 403
        )
        client.cookies.clear()
        callback = str(new_uuid7())
        base = f"/v1/attempts/{authority['attempt_id']}"
        reserved = client.post(
            base + "/result-reservations",
            headers={**worker_headers, "X-Callback-Id": callback},
            json={"authority": authority},
        )
        assert reserved.status_code == 201, reserved.text
        relogin = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b11-closure@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert relogin.status_code == 200, relogin.text
        assert client.get(result_url, headers=user_headers).status_code == 404
        client.cookies.clear()
        output_bytes = b'{"iterations":100,"value":42}'
        output = _upload(
            client,
            credential,
            authority,
            "RESULT_FILE",
            "application/vnd.nexa.cpu-iterative-result+json",
            output_bytes,
            "b11-closure-result-file",
        )
        provenance = {
            "tenant_id": str(job["tenant_id"]),
            "job_id": str(job["job_id"]),
            "session_id": str(session_id),
            "attempt_id": authority["attempt_id"],
            "job_fence": authority["job_fence"],
            "input_checksum": blob.checksum,
            "spec_checksum": spec["spec_checksum"],
            "template_id": spec["template_id"],
            "template_version": spec["template_version"],
            "adapter_id": template["adapter_id"],
            "adapter_version": template["adapter_version"],
            "image_digest": template["image_digest"],
        }
        manifest = {
            "kind": "RESULT",
            "schema_version": 1,
            "result_id": reserved.json()["result_id"],
            "created_at": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "provenance": provenance,
            "status": "SUCCEEDED",
            "files": [
                {
                    "artifact_id": output["artifact_id"],
                    "logical_name": "result.json",
                    "media_type": output["media_type"],
                    "size_bytes": output["size_bytes"],
                    "checksum": output["checksum"],
                }
            ],
            "metrics": {},
        }
        manifest["manifest_checksum"] = _checksum(rfc8785.dumps(manifest))
        manifest_blob = _upload(
            client,
            credential,
            authority,
            "RESULT_MANIFEST",
            "application/json",
            rfc8785.dumps(manifest),
            "b11-closure-result-manifest",
        )
        complete_body = {
            "authority": authority,
            "result_manifest_artifact_id": manifest_blob["artifact_id"],
            "manifest": manifest,
        }

        def rejected(body, expected_status):
            response = client.post(
                base + "/complete",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Callback-Id": str(new_uuid7()),
                },
                json=body,
            )
            assert response.status_code == expected_status, response.text

        rejected({**complete_body, "manifest": {}}, 409)
        corrupt_wire = deepcopy(manifest)
        corrupt_wire["metrics"] = {"unverified": True}
        rejected({**complete_body, "manifest": corrupt_wire}, 422)
        for name, change, status in (
            ("provenance", {"provenance": {**provenance, "job_fence": 2}}, 422),
            (
                "input-binding",
                {"files": [{**manifest["files"][0], "artifact_id": str(input_id)}]},
                409,
            ),
        ):
            invalid = {**manifest, **change}
            invalid["manifest_checksum"] = _checksum(
                rfc8785.dumps(
                    {key: value for key, value in invalid.items() if key != "manifest_checksum"}
                )
            )
            invalid_blob = _upload(
                client,
                credential,
                authority,
                "RESULT_MANIFEST",
                "application/json",
                rfc8785.dumps(invalid),
                f"b11-closure-invalid-{name}",
            )
            rejected(
                {
                    **complete_body,
                    "result_manifest_artifact_id": invalid_blob["artifact_id"],
                    "manifest": invalid,
                },
                status,
            )
        with engine.connect() as connection:
            assert connection.execute(select(s.results)).all() == []
            assert set(connection.execute(select(s.admission_counters.c.outstanding))) == {(1,)}

        completion_id = str(new_uuid7())
        completion_headers = {
            "Authorization": f"Bearer {credential}",
            "X-Callback-Id": completion_id,
        }
        completed = client.post(base + "/complete", headers=completion_headers, json=complete_body)
        assert completed.status_code == 200, completed.text
        assert completed.json()["accepted"] is True
        pending_cleanup = client.get(
            f"/v1/workers/{WORKER_ID}/reconciliation?page_size=100",
            headers={
                "Authorization": f"Bearer {credential}",
                "X-Worker-Incarnation-Id": incarnation["worker_incarnation_id"],
            },
        )
        assert pending_cleanup.status_code == 200, pending_cleanup.text
        assert pending_cleanup.json()["items"][0]["authority_state"] == "REVOKED"
        assert pending_cleanup.json()["items"][0]["completion_receipt"] == {
            "callback_id": completion_id,
            "result_id": manifest["result_id"],
            "manifest_artifact_id": manifest_blob["artifact_id"],
            "acknowledgment": completed.json(),
        }
        relogin = client.post(
            "/v1/auth/login",
            headers={"Origin": "https://nexa.test"},
            json={
                "username": "b11-closure@example.test",
                "password": "correct-horse-battery-staple",
            },
        )
        assert relogin.status_code == 200, relogin.text
        recognized = client.get(result_url, headers=user_headers)
        assert recognized.status_code == 200, recognized.text
        assert recognized.json()["manifest_artifact_id"] == manifest_blob["artifact_id"]
        assert recognized.json()["result_id"] == reserved.json()["result_id"]
        downloaded = client.get(
            f"/v1/artifacts/{output['artifact_id']}/content", headers=user_headers
        )
        assert downloaded.status_code == 200 and _checksum(downloaded.content) == output["checksum"]
        event_page = client.get(f"/v1/jobs/{job['job_id']}/events", headers=user_headers)
        assert event_page.status_code == 200, event_page.text
        tokens = {}
        for name, scopes in (
            ("read", ["jobs:read"]),
            ("write", ["jobs:write"]),
            ("cli-result", ["jobs:read", "artifacts:read"]),
        ):
            created = client.post(
                "/v1/tokens",
                headers={
                    "Origin": "https://nexa.test",
                    "X-CSRF-Token": relogin.json()["csrf_token"],
                    "Idempotency-Key": f"b11-closure-token-{name}",
                },
                json={"name": f"b11-{name}", "scopes": scopes, "expires_in_seconds": 300},
            )
            assert created.status_code == 201, created.text
            tokens[name] = created.json()["token"]
        with _running_api_process(engine, tmp_path) as (base_url, _process):
            cli_result = _success(
                base_url,
                tokens["cli-result"],
                tmp_path,
                "job",
                "result",
                str(job["job_id"]),
                "--tenant",
                str(job["tenant_id"]),
            )
            assert cli_result["manifest_artifact_id"] == manifest_blob["artifact_id"]
            manifest_path = tmp_path / "b12-cli-result-manifest.json"
            cli_download = _success(
                base_url,
                tokens["cli-result"],
                tmp_path,
                "job",
                "result-download",
                str(job["job_id"]),
                "--output-file",
                str(manifest_path),
                "--checksum",
                manifest_blob["checksum"],
                "--tenant",
                str(job["tenant_id"]),
            )
            assert manifest_path.read_bytes() == rfc8785.dumps(manifest)
            assert cli_download["sha256"] == manifest_blob["checksum"]
        client.cookies.clear()
        assert (
            client.get(
                result_url,
                headers={**user_headers, "Authorization": f"Bearer {tokens['read']}"},
            ).status_code
            == 200
        )
        assert (
            client.get(
                result_url,
                headers={**user_headers, "Authorization": f"Bearer {tokens['write']}"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                result_url,
                headers={
                    "X-Nexa-Tenant-Id": str(other_graph["tenant_id"]),
                    "Authorization": f"Bearer {tokens['read']}",
                },
            ).status_code
            == 403
        )
        with engine.connect() as connection:
            assert connection.execute(select(s.jobs.c.state)).scalar_one() == "SUCCEEDED"
            assert connection.execute(select(s.results.c.result_id)).scalar_one() == UUID(
                reserved.json()["result_id"]
            )
            references = (
                connection.execute(
                    select(s.artifact_references.c.purpose).where(
                        s.artifact_references.c.owner_type == "RESULT"
                    )
                )
                .scalars()
                .all()
            )
            assert sorted(references) == ["RESULT_FILE", "RESULT_MANIFEST"]
            assert set(
                connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                )
            ) == {(0, 1)}
            assert connection.execute(select(s.allocations.c.state)).scalar_one() == "HELD"
        client.cookies.clear()
        proof = {
            "proof_type": "CONTAINER_STOPPED",
            "startup_nonce": str(nonce),
            "executor_operation_sequence": 2,
            "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
            "stopped_at": datetime.now(UTC).isoformat(),
            "exit_code": 0,
            "inspection_checksum": "sha256:" + "c" * 64,
        }
        cleanup_body = {k: v for k, v in authority.items() if k != "lease_id"}
        cleanup_body["proof"] = proof
        cleaned = client.post(
            base + "/cleanup",
            headers={"Authorization": f"Bearer {credential}", "X-Callback-Id": str(new_uuid7())},
            json=cleanup_body,
        )
        assert cleaned.status_code == 200 and cleaned.json()["verified"] is True, cleaned.text
        replay = client.post(base + "/complete", headers=completion_headers, json=complete_body)
        assert replay.status_code == 200 and replay.json() == completed.json(), replay.text
        conflict = client.post(
            base + "/complete",
            headers={**completion_headers, "X-Callback-Id": str(new_uuid7())},
            json=complete_body,
        )
        assert conflict.status_code == 409, conflict.text
        stale = client.get(
            f"{graph_url}/{input_row['artifact_id']}/content", headers=worker_headers
        )
        assert stale.status_code == 409, stale.text
        with engine.connect() as connection:
            assert connection.execute(select(s.allocations.c.state)).scalar_one() == "RELEASED"
            assert set(
                connection.execute(
                    select(
                        s.admission_counters.c.outstanding, s.admission_counters.c.active_attempts
                    )
                )
            ) == {(0, 0)}
            assert len(connection.execute(select(s.results)).all()) == 1


def test_upload_adoption_replays_original_artifact_only_through_grant_lineage(
    migrated_postgres_engine, tmp_path
):
    from tests.integration.test_worker_authority_b10 import _running_authority

    engine = migrated_postgres_engine
    with _client(engine, tmp_path) as client:
        credential = _bootstrap_worker(client)
        first = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-upload-old-incarnation"
        )
        prior = _running_authority(engine, first)
        old = {k: v for k, v in prior.items() if k not in {"grant_id", "job_id"}}
        content = b'{"value":23}'
        key = "b11-adoption-output-0001"
        original = _upload(
            client,
            credential,
            old,
            "RESULT_FILE",
            "application/vnd.nexa.cpu-iterative-result+json",
            content,
            key,
        )
        current = _create_incarnation(
            client, credential, nonce=str(new_uuid7()), key="b11-upload-new-incarnation"
        )
        adopted = client.post(
            f"/v1/attempts/{old['attempt_id']}/adopt",
            headers={"Authorization": f"Bearer {credential}", "X-Callback-Id": str(new_uuid7())},
            json={
                "prior_authority": old,
                "current_worker_incarnation_id": current["worker_incarnation_id"],
                "container": {"container_id": CONTAINER_ID, "runtime_identity_digest": DIGEST},
            },
        )
        assert adopted.status_code == 200, adopted.text
        new = adopted.json()["authority"]
        assert (
            _upload(
                client,
                credential,
                new,
                "RESULT_FILE",
                "application/vnd.nexa.cpu-iterative-result+json",
                content,
                key,
            )
            == original
        )
        stale = client.post(
            f"/v1/attempts/{old['attempt_id']}/artifacts",
            headers={
                **_worker_headers(credential, old),
                "Content-Type": "application/octet-stream",
                "Idempotency-Key": key,
                "X-Artifact-Kind": "RESULT_FILE",
                "X-Artifact-Media-Type": "application/vnd.nexa.cpu-iterative-result+json",
                "X-Artifact-Checksum": _checksum(content),
                "X-Artifact-Size": str(len(content)),
            },
            content=content,
        )
        assert stale.status_code == 409, stale.text
        with engine.connect() as connection:
            upload = connection.execute(select(s.upload_sessions)).mappings().one()
            assert upload["authority_grant_id"] == prior["grant_id"]
            grants = connection.execute(select(s.attempt_authority_grants)).mappings().all()
            assert len(grants) == 2
            descendant = next(row for row in grants if row["grant_id"] != prior["grant_id"])
            assert descendant["predecessor_grant_id"] == prior["grant_id"]
            assert len(connection.execute(select(s.artifacts)).all()) == 2
