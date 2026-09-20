import hashlib

import pytest

from tests.api.test_http_contract import _bootstrap_and_login, _client

pytestmark = pytest.mark.postgres


def _checksum(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def test_artifact_openapi_declares_binary_upload_and_range_download(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as client:
        paths = client.app.openapi()["paths"]
        upload = paths["/v1/artifacts"]["post"]
        download = paths["/v1/artifacts/{artifact_id}/content"]["get"]
        artifact_schema = client.app.openapi()["components"]["schemas"]["Artifact"]
        page_schema = client.app.openapi()["components"]["schemas"]["ArtifactPage"]

        assert upload["requestBody"]["content"]["application/octet-stream"]
        assert "201" in upload["responses"]
        assert set(upload["responses"]["201"]["headers"]) == {"Location", "ETag"}
        assert "500" in upload["responses"]
        assert "503" in upload["responses"]
        upload_params = {parameter["name"]: parameter for parameter in upload["parameters"]}
        assert upload_params["Idempotency-Key"]["schema"]["pattern"] == r"^[!-~]{16,128}$"
        assert upload_params["X-Artifact-Checksum"]["schema"]["pattern"] == (
            r"^sha256:[0-9a-f]{64}$"
        )
        assert upload_params["X-Artifact-Size"]["schema"]["maximum"] == 1099511627776
        assert upload_params["X-Artifact-Kind"]["schema"]["$ref"] == (
            "#/components/schemas/UserArtifactKind"
        )
        assert client.app.openapi()["components"]["schemas"]["UserArtifactKind"]["enum"] == [
            "INPUT",
            "DATASET",
            "MODEL",
        ]
        assert upload_params["X-Artifact-Media-Type"]["schema"]["maxLength"] == 127
        assert download["responses"]["200"]["content"]["application/octet-stream"]
        assert download["responses"]["206"]["content"]["application/octet-stream"]
        assert set(download["responses"]["200"]["headers"]) == {
            "ETag",
            "X-Artifact-Media-Type",
            "Content-Length",
        }
        assert set(download["responses"]["206"]["headers"]) == {
            "Content-Range",
            "ETag",
            "X-Artifact-Media-Type",
        }
        assert "500" in download["responses"]
        range_schema = {
            **download["parameters"][-1]["schema"],
        }
        range_variants = range_schema.pop("anyOf", [range_schema])
        assert any(variant.get("pattern") == r"^bytes=[0-9]+-[0-9]*$" for variant in range_variants)
        assert artifact_schema["properties"]["kind"]["$ref"] == "#/components/schemas/ArtifactKind"
        assert artifact_schema["properties"]["artifact_id"]["format"] == "uuid7"
        assert page_schema["properties"]["page"]["$ref"] == "#/components/schemas/PageInfo"


def test_artifact_http_upload_metadata_list_and_range_download(
    migrated_postgres_engine, tmp_path
) -> None:
    with _client(migrated_postgres_engine, tmp_path) as admin:
        admin_login, _ = _bootstrap_and_login(admin)
        mutation = {"Origin": "https://nexa.test", "X-CSRF-Token": admin_login["csrf_token"]}
        tenant = admin.post(
            "/v1/admin/tenants",
            headers={**mutation, "Idempotency-Key": "b07-api-tenant-01"},
            json={"slug": "b07-api", "display_name": "B07 API"},
        )
        assert tenant.status_code == 201, tenant.text
        tenant_id = tenant.json()["tenant_id"]
        user = admin.post(
            "/v1/admin/users",
            headers={**mutation, "Idempotency-Key": "b07-api-user-001"},
            json={
                "username": "b07-api-user@example.test",
                "display_name": "B07 API User",
                "password": "b07-api-user-password",
                "system_roles": [],
            },
        )
        assert user.status_code == 201, user.text
        user_id = user.json()["user_id"]
        membership = admin.post(
            f"/v1/admin/tenants/{tenant_id}/memberships",
            headers={**mutation, "Idempotency-Key": "b07-api-member-001", "If-Match": '"v1"'},
            json={"user_id": user_id, "role": "MEMBER"},
        )
        assert membership.status_code == 200, membership.text

        member_root = tmp_path / "member"
        member_root.mkdir()
        with _client(migrated_postgres_engine, member_root) as member:
            login = member.post(
                "/v1/auth/login",
                headers={"Origin": "https://nexa.test"},
                json={
                    "username": "b07-api-user@example.test",
                    "password": "b07-api-user-password",
                },
            )
            assert login.status_code == 200, login.text
            headers = {
                "Origin": "https://nexa.test",
                "X-CSRF-Token": login.json()["csrf_token"],
                "X-Nexa-Tenant-Id": tenant_id,
                "Idempotency-Key": "b07-api-upload-key",
                "X-Artifact-Checksum": _checksum(b"abcd"),
                "X-Artifact-Size": "4",
                "X-Artifact-Kind": "INPUT",
                "X-Artifact-Media-Type": "application/vnd.nexa.cpu-iterative-input+json",
                "Content-Type": "application/octet-stream",
            }
            uploaded = member.post("/v1/artifacts", headers=headers, content=b"abcd")
            assert uploaded.status_code == 201, uploaded.text
            assert uploaded.headers["location"].startswith("/v1/artifacts/")
            assert "blob_key" not in uploaded.text
            artifact_id = uploaded.json()["artifact_id"]

            replay = member.post("/v1/artifacts", headers=headers, content=b"abcd")
            assert replay.status_code == 201
            assert replay.json() == uploaded.json()

            metadata = member.get(
                f"/v1/artifacts/{artifact_id}", headers={"X-Nexa-Tenant-Id": tenant_id}
            )
            assert metadata.status_code == 200
            assert metadata.headers["etag"] == '"v1"'
            listing = member.get("/v1/artifacts", headers={"X-Nexa-Tenant-Id": tenant_id})
            assert listing.status_code == 200
            assert [item["artifact_id"] for item in listing.json()["items"]] == [artifact_id]

            full = member.get(
                f"/v1/artifacts/{artifact_id}/content", headers={"X-Nexa-Tenant-Id": tenant_id}
            )
            assert full.status_code == 200
            assert full.content == b"abcd"
            assert full.headers["content-type"].startswith("application/octet-stream")
            assert (
                full.headers["x-artifact-media-type"]
                == "application/vnd.nexa.cpu-iterative-input+json"
            )
            ranged = member.get(
                f"/v1/artifacts/{artifact_id}/content",
                headers={"X-Nexa-Tenant-Id": tenant_id, "Range": "bytes=1-2"},
            )
            assert ranged.status_code == 206
            assert ranged.content == b"bc"
            assert ranged.headers["content-range"] == "bytes 1-2/4"

            invalid_kind = member.post(
                "/v1/artifacts",
                headers={
                    **headers,
                    "Idempotency-Key": "b07-api-invalid-kind",
                    "X-Artifact-Kind": "LOG",
                },
                content=b"abcd",
            )
            assert invalid_kind.status_code == 422
