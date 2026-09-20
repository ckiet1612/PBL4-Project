"""Immutable B06 schema generation layered on the frozen B05 metadata."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from nexa.infrastructure.persistence import schema_v1

SCHEMA_GENERATION = 2
metadata = MetaData(naming_convention=dict(schema_v1.metadata.naming_convention))
for _table in schema_v1.metadata.tables.values():
    _table.to_metadata(metadata)

UUID_TYPE = UUID(as_uuid=True)
UTC_TIMESTAMP = DateTime(timezone=True)

auth_control = Table(
    "auth_control",
    metadata,
    Column("singleton_key", String(32), primary_key=True),
    Column("installation_id", UUID_TYPE),
    Column("local_worker_id", UUID_TYPE),
    Column("worker_credential_fingerprint", String(71)),
    Column("admin_window_opened_at", UTC_TIMESTAMP),
    Column("admin_window_expires_at", UTC_TIMESTAMP),
    Column("admin_bootstrap_completed_at", UTC_TIMESTAMP),
    Column("worker_window_opened_at", UTC_TIMESTAMP),
    Column("worker_window_expires_at", UTC_TIMESTAMP),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    CheckConstraint("singleton_key = 'auth'", name="singleton_key"),
    CheckConstraint("version >= 1", name="version_positive"),
    CheckConstraint(
        "(admin_window_opened_at IS NULL) = (admin_window_expires_at IS NULL)",
        name="admin_window_pair",
    ),
    CheckConstraint(
        "admin_window_expires_at IS NULL OR admin_window_expires_at > admin_window_opened_at",
        name="admin_window_order",
    ),
    CheckConstraint(
        "admin_bootstrap_completed_at IS NULL OR admin_window_opened_at IS NOT NULL",
        name="admin_completion_requires_window",
    ),
    CheckConstraint(
        "(worker_window_opened_at IS NULL) = (worker_window_expires_at IS NULL)",
        name="worker_window_pair",
    ),
    CheckConstraint(
        "worker_window_expires_at IS NULL OR worker_window_expires_at > worker_window_opened_at",
        name="worker_window_order",
    ),
    CheckConstraint(
        "worker_credential_fingerprint IS NULL OR "
        "worker_credential_fingerprint ~ '^sha256:[0-9a-f]{64}$'",
        name="worker_fingerprint",
    ),
)

login_rate_limits = Table(
    "login_rate_limits",
    metadata,
    Column("source_hash", LargeBinary, nullable=False),
    Column("username", String(255), nullable=False),
    Column("window_started_at", UTC_TIMESTAMP, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("source_hash", "username"),
    CheckConstraint("octet_length(source_hash) = 32", name="source_hash_length"),
    CheckConstraint("username = lower(username)", name="username_normalized"),
    CheckConstraint("length(username) BETWEEN 1 AND 255", name="username_length"),
    CheckConstraint("attempts BETWEEN 1 AND 120", name="attempts_range"),
)
Index(
    "ix_login_rate_limits_updated",
    login_rate_limits.c.updated_at,
    login_rate_limits.c.source_hash,
)

worker_credentials = metadata.tables["worker_credentials"]
Index(
    "uq_worker_credentials_current",
    worker_credentials.c.worker_id,
    unique=True,
    postgresql_where=worker_credentials.c.revoked_at.is_(None),
)

__all__ = ["SCHEMA_GENERATION", "auth_control", "login_rate_limits", "metadata"]
