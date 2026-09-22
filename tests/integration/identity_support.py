from dataclasses import replace
from pathlib import Path
from uuid import UUID

from nexa.application.identity_service import IdentityService
from nexa.config import load_settings
from nexa.infrastructure.persistence.database import create_session_factory

INSTALLATION_ID = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f1")
WORKER_ID = UUID("018f05c4-a922-7d0d-9f55-f9084a72d0f2")
FINGERPRINT = "sha256:" + "a" * 64


def make_identity_service(
    migrated_postgres_engine,
    tmp_path: Path,
    *,
    installation_id: UUID = INSTALLATION_ID,
    worker_id: UUID = WORKER_ID,
) -> IdentityService:
    server_secret = tmp_path / "server-secret"
    bootstrap_secret = tmp_path / "bootstrap-secret"
    server_secret.write_bytes(b"s" * 32)
    bootstrap_secret.write_bytes(b"b" * 32)
    settings = load_settings(
        {
            "NEXA_ENVIRONMENT": "test",
            "NEXA_DATABASE_URL": str(migrated_postgres_engine.url).replace("***", "unused"),
            "NEXA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            "NEXA_PUBLIC_ORIGIN": "https://nexa.test",
            "NEXA_SERVER_SECRET_FILE": str(server_secret),
            "NEXA_BOOTSTRAP_SECRET_FILE": str(bootstrap_secret),
            "NEXA_INSTALLATION_ID": str(installation_id),
            "NEXA_LOCAL_WORKER_ID": str(worker_id),
            "NEXA_LOCAL_WORKER_FINGERPRINT": FINGERPRINT,
            "NEXA_MAINTENANCE_CIDRS": "127.0.0.0/8",
        }
    )
    settings = replace(settings, database_url="postgresql+psycopg://redacted/redacted")
    service = IdentityService(create_session_factory(migrated_postgres_engine), settings)
    service.initialize()
    return service


def bootstrap_admin(service: IdentityService) -> None:
    service.bootstrap_admin(
        bootstrap_secret=b"b" * 32,
        source_allowed=True,
        username="admin@example.test",
        display_name="Initial Admin",
        password="correct-horse-battery-staple",
        idempotency_key="bootstrap-admin-0001",
        request_hash="sha256:" + "a" * 64,
    )
