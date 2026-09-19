"""Immutable physical schema snapshot for Alembic revision 20260919_0001."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from nexa.infrastructure.persistence.values import ExactDecimalText

SCHEMA_GENERATION = 1

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

UUID_TYPE = UUID(as_uuid=True)
UTC_TIMESTAMP = DateTime(timezone=True)
EXACT_DECIMAL_TEXT = ExactDecimalText()
MAX_INT64 = 9_223_372_036_854_775_807


def _timestamps() -> tuple[Column[object], Column[object]]:
    return (
        Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
        Column(
            "updated_at",
            UTC_TIMESTAMP,
            nullable=False,
            server_default=func.now(),
        ),
    )


def _version_check(column: str = "version") -> CheckConstraint:
    return CheckConstraint(
        f"{column} BETWEEN 1 AND {MAX_INT64}",
        name=f"{column}_positive_int64",
    )


def _nonnegative_check(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} BETWEEN 0 AND {MAX_INT64}",
        name=f"{column}_nonnegative_int64",
    )


def _finite_nonnegative_decimal(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"nexa_decimal_is_nonnegative({column})",
        name=f"{column}_finite_nonnegative",
    )


nexa_schema_metadata = Table(
    "nexa_schema_metadata",
    metadata,
    Column("singleton_key", String(32), primary_key=True),
    Column("schema_generation", BigInteger, nullable=False),
    Column("contract_version", String(64), nullable=False),
    Column("installed_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    CheckConstraint("singleton_key = 'nexa'", name="singleton_key"),
    CheckConstraint("schema_generation >= 1", name="schema_generation_positive"),
)

tenants = Table(
    "tenants",
    metadata,
    Column("tenant_id", UUID_TYPE, primary_key=True),
    Column("slug", String(63), nullable=False),
    Column("display_name", String(255), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    *_timestamps(),
    CheckConstraint("slug = lower(slug)", name="slug_normalized"),
    CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'", name="slug_format"),
    _version_check(),
    UniqueConstraint("tenant_id", name="uq_tenants_tenant_owner"),
)
Index("uq_tenants_slug_casefold", func.lower(tenants.c.slug), unique=True)

users = Table(
    "users",
    metadata,
    Column("user_id", UUID_TYPE, primary_key=True),
    Column("username", String(255), nullable=False),
    Column("display_name", String(255), nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    *_timestamps(),
    CheckConstraint("username = lower(username)", name="username_normalized"),
    CheckConstraint("length(username) BETWEEN 1 AND 255", name="username_length"),
    CheckConstraint("length(password_hash) >= 32", name="password_hash_length"),
    _version_check(),
)
Index("uq_users_username_casefold", func.lower(users.c.username), unique=True)

membership_sets = Table(
    "membership_sets",
    metadata,
    Column(
        "tenant_id",
        UUID_TYPE,
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT", deferrable=True),
        primary_key=True,
    ),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    *_timestamps(),
    _version_check(),
)

memberships = Table(
    "memberships",
    metadata,
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("user_id", UUID_TYPE, nullable=False),
    Column("role", String(32), nullable=False),
    *_timestamps(),
    PrimaryKeyConstraint("tenant_id", "user_id"),
    ForeignKeyConstraint(["tenant_id"], ["membership_sets.tenant_id"], ondelete="RESTRICT"),
    ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="RESTRICT"),
    CheckConstraint("role IN ('MEMBER', 'TENANT_ADMIN')", name="role"),
)

system_role_grants = Table(
    "system_role_grants",
    metadata,
    Column("user_id", UUID_TYPE, ForeignKey("users.user_id", ondelete="RESTRICT")),
    Column("role", String(32), nullable=False),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("granted_by_user_id", UUID_TYPE, ForeignKey("users.user_id", ondelete="RESTRICT")),
    Column("granted_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    Column("revoked_at", UTC_TIMESTAMP),
    PrimaryKeyConstraint("user_id", "role"),
    CheckConstraint("role = 'SYSTEM_ADMIN'", name="role"),
    _version_check(),
)

browser_sessions = Table(
    "browser_sessions",
    metadata,
    Column("browser_session_id", UUID_TYPE, primary_key=True),
    Column("user_id", UUID_TYPE, ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False),
    Column("secret_hash", LargeBinary, nullable=False),
    Column("csrf_secret_hash", LargeBinary, nullable=False),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("revoked_at", UTC_TIMESTAMP),
    Column("last_seen_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    *_timestamps(),
    UniqueConstraint("secret_hash"),
    CheckConstraint("octet_length(secret_hash) >= 32", name="secret_hash_length"),
    CheckConstraint("octet_length(csrf_secret_hash) >= 32", name="csrf_hash_length"),
)
Index("ix_browser_sessions_user_expiry", browser_sessions.c.user_id, browser_sessions.c.expires_at)

cli_tokens = Table(
    "cli_tokens",
    metadata,
    Column("token_id", UUID_TYPE, primary_key=True),
    Column("user_id", UUID_TYPE, ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False),
    Column("token_hash", LargeBinary, nullable=False),
    Column("name", String(255), nullable=False),
    Column("scopes", ARRAY(String(64)), nullable=False),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("revoked_at", UTC_TIMESTAMP),
    *_timestamps(),
    UniqueConstraint("token_hash"),
    CheckConstraint("octet_length(token_hash) >= 32", name="token_hash_length"),
)
Index("ix_cli_tokens_user_expiry", cli_tokens.c.user_id, cli_tokens.c.expires_at)

templates = Table(
    "templates",
    metadata,
    Column("template_id", String(128), primary_key=True),
    Column("current_version", BigInteger, nullable=False),
    Column("display_name", String(255), nullable=False),
    Column("description", Text, nullable=False, server_default=text("''")),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    *_timestamps(),
    _version_check("current_version"),
)

template_versions = Table(
    "template_versions",
    metadata,
    Column("template_id", String(128), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("parameter_schema", JSONB, nullable=False),
    Column("resource_bounds", JSONB, nullable=False),
    Column("capability_requirements", JSONB, nullable=False),
    Column("adapter_id", String(128), nullable=False),
    Column("adapter_version", String(64), nullable=False),
    Column("image_digest", String(71), nullable=False),
    Column("checkpointable", Boolean, nullable=False),
    Column("restart_safe", Boolean, nullable=False),
    *_timestamps(),
    PrimaryKeyConstraint("template_id", "version"),
    ForeignKeyConstraint(["template_id"], ["templates.template_id"], ondelete="RESTRICT"),
    _version_check(),
    CheckConstraint("image_digest ~ '^sha256:[0-9a-f]{64}$'", name="image_digest"),
)
templates.append_constraint(
    ForeignKeyConstraint(
        [templates.c.template_id, templates.c.current_version],
        [template_versions.c.template_id, template_versions.c.version],
        name="fk_templates_current_version",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)

artifacts = Table(
    "artifacts",
    metadata,
    Column("artifact_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("kind", String(64), nullable=False),
    Column("media_type", String(127), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("checksum", String(71), nullable=False),
    Column("blob_key", String(512), nullable=False),
    Column("state", String(32), nullable=False),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    *_timestamps(),
    ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="RESTRICT"),
    UniqueConstraint("tenant_id", "artifact_id", name="uq_artifacts_tenant_artifact"),
    UniqueConstraint(
        "tenant_id", "artifact_id", "state", name="uq_artifacts_tenant_artifact_state"
    ),
    UniqueConstraint("blob_key"),
    UniqueConstraint("tenant_id", "checksum", "kind", "media_type", name="uq_artifacts_dedup"),
    _nonnegative_check("size_bytes"),
    _version_check(),
    CheckConstraint("checksum ~ '^sha256:[0-9a-f]{64}$'", name="checksum"),
    CheckConstraint("state IN ('STAGING', 'COMMITTED', 'DELETING', 'DELETED')", name="state"),
)
Index(
    "ix_artifacts_tenant_created_keyset",
    artifacts.c.tenant_id,
    artifacts.c.created_at.desc(),
    artifacts.c.artifact_id.desc(),
)

artifact_reference_guards = Table(
    "artifact_reference_guards",
    metadata,
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("artifact_id", UUID_TYPE, nullable=False),
    Column("artifact_state", String(32), nullable=False, server_default=text("'COMMITTED'")),
    PrimaryKeyConstraint("tenant_id", "artifact_id"),
    ForeignKeyConstraint(
        ["tenant_id", "artifact_id", "artifact_state"],
        ["artifacts.tenant_id", "artifacts.artifact_id", "artifacts.state"],
        ondelete="RESTRICT",
    ),
    CheckConstraint("artifact_state = 'COMMITTED'", name="committed_state"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("job_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("submitter_user_id", UUID_TYPE, nullable=False),
    Column("state", String(32), nullable=False),
    Column("desired_state", String(32), nullable=False),
    Column("waiting_reason", String(64)),
    Column("recovery_intent", String(64)),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("job_fence", BigInteger, nullable=False, server_default=text("0")),
    Column("event_sequence", BigInteger, nullable=False, server_default=text("0")),
    Column("checkpoint_sequence", BigInteger, nullable=False, server_default=text("0")),
    Column("retry_count", Integer, nullable=False, server_default=text("0")),
    Column("max_retries", Integer, nullable=False, server_default=text("2")),
    Column("retry_of_job_id", UUID_TYPE),
    Column("base_priority", Integer, nullable=False, server_default=text("1")),
    Column("ready_sequence", BigInteger, nullable=False),
    Column("eligible_since", UTC_TIMESTAMP),
    Column("retry_ready_at", UTC_TIMESTAMP),
    Column("terminal_at", UTC_TIMESTAMP),
    *_timestamps(),
    ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="RESTRICT"),
    ForeignKeyConstraint(["submitter_user_id"], ["users.user_id"], ondelete="RESTRICT"),
    UniqueConstraint("tenant_id", "job_id", name="uq_jobs_tenant_job"),
    _version_check(),
    _nonnegative_check("job_fence"),
    _nonnegative_check("event_sequence"),
    _nonnegative_check("checkpoint_sequence"),
    CheckConstraint("retry_count BETWEEN 0 AND 2", name="retry_count"),
    CheckConstraint("max_retries BETWEEN 0 AND 2", name="max_retries"),
    CheckConstraint("base_priority IN (0, 1, 2)", name="base_priority"),
    CheckConstraint("ready_sequence >= 0", name="ready_sequence"),
    CheckConstraint(
        "state IN ('QUEUED','DISPATCHING','RUNNING','PAUSING','PAUSED','RECOVERING',"
        "'RETRY_WAIT','CANCELLING','SUCCEEDED','FAILED','CANCELLED')",
        name="state",
    ),
    CheckConstraint("desired_state IN ('RUNNING','PAUSED','CANCELLED')", name="desired_state"),
    CheckConstraint(
        "recovery_intent IS NULL OR recovery_intent = 'CHECKPOINT_FOR_PAUSE'",
        name="recovery_intent",
    ),
)
jobs.append_constraint(
    ForeignKeyConstraint(
        [jobs.c.tenant_id, jobs.c.retry_of_job_id],
        [jobs.c.tenant_id, jobs.c.job_id],
        name="fk_jobs_same_tenant_retry_source",
        ondelete="RESTRICT",
        deferrable=True,
    )
)
Index(
    "ix_jobs_queue_head",
    jobs.c.state,
    jobs.c.tenant_id,
    jobs.c.base_priority.desc(),
    jobs.c.ready_sequence,
    jobs.c.job_id,
    postgresql_where=jobs.c.state == "QUEUED",
)
Index(
    "ix_jobs_oldest_eligible",
    jobs.c.tenant_id,
    jobs.c.eligible_since,
    jobs.c.ready_sequence,
    jobs.c.job_id,
    postgresql_where=jobs.c.state == "QUEUED",
)
Index(
    "ix_jobs_retry_ready",
    jobs.c.retry_ready_at,
    jobs.c.job_id,
    postgresql_where=jobs.c.state == "RETRY_WAIT",
)
Index(
    "ix_jobs_tenant_created_keyset",
    jobs.c.tenant_id,
    jobs.c.created_at.desc(),
    jobs.c.job_id.desc(),
)

job_specs = Table(
    "job_specs",
    metadata,
    Column("job_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("canonical_spec", JSONB, nullable=False),
    Column("spec_checksum", String(71), nullable=False),
    Column("template_id", String(128), nullable=False),
    Column("template_version", BigInteger, nullable=False),
    Column("input_artifact_id", UUID_TYPE),
    Column("model_artifact_id", UUID_TYPE),
    Column("cpu_millis", BigInteger, nullable=False),
    Column("memory_bytes", BigInteger, nullable=False),
    Column("gpu_count", Integer, nullable=False),
    Column("runtime_limit_seconds", Integer, nullable=False),
    Column("checkpoint_interval_seconds", Integer),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["tenant_id", "job_id"], ["jobs.tenant_id", "jobs.job_id"], ondelete="RESTRICT"
    ),
    ForeignKeyConstraint(
        ["template_id", "template_version"],
        ["template_versions.template_id", "template_versions.version"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "input_artifact_id"],
        ["artifact_reference_guards.tenant_id", "artifact_reference_guards.artifact_id"],
        name="fk_job_specs_input_artifact",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "model_artifact_id"],
        ["artifact_reference_guards.tenant_id", "artifact_reference_guards.artifact_id"],
        name="fk_job_specs_model_artifact",
        ondelete="RESTRICT",
    ),
    CheckConstraint("spec_checksum ~ '^sha256:[0-9a-f]{64}$'", name="spec_checksum"),
    CheckConstraint("cpu_millis >= 100", name="cpu_millis"),
    CheckConstraint("memory_bytes > 0", name="memory_bytes"),
    CheckConstraint("gpu_count IN (0, 1)", name="gpu_count"),
    CheckConstraint("runtime_limit_seconds > 0", name="runtime_limit"),
    CheckConstraint(
        "checkpoint_interval_seconds IS NULL OR checkpoint_interval_seconds > 0",
        name="checkpoint_interval",
    ),
)

logical_sessions = Table(
    "logical_sessions",
    metadata,
    Column("session_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["tenant_id", "job_id"], ["jobs.tenant_id", "jobs.job_id"], ondelete="RESTRICT"
    ),
    UniqueConstraint("tenant_id", "job_id", name="uq_logical_sessions_tenant_job"),
    UniqueConstraint("tenant_id", "session_id", name="uq_logical_sessions_tenant_session"),
    UniqueConstraint(
        "tenant_id",
        "job_id",
        "session_id",
        name="uq_logical_sessions_tenant_job_session",
    ),
)

sweep_parents = Table(
    "sweep_parents",
    metadata,
    Column("sweep_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("submitter_user_id", UUID_TYPE, nullable=False),
    Column("base_spec_checksum", String(71), nullable=False),
    Column("idempotency_context", String(128), nullable=False),
    Column("child_count", Integer, nullable=False),
    Column("accepted_count", Integer, nullable=False, server_default=text("0")),
    Column("rejected_count", Integer, nullable=False, server_default=text("0")),
    *_timestamps(),
    ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="RESTRICT"),
    ForeignKeyConstraint(["submitter_user_id"], ["users.user_id"], ondelete="RESTRICT"),
    UniqueConstraint("tenant_id", "sweep_id", name="uq_sweep_parents_tenant_sweep"),
    CheckConstraint("child_count BETWEEN 1 AND 100", name="child_count"),
    CheckConstraint("accepted_count >= 0 AND rejected_count >= 0", name="outcome_counts"),
    CheckConstraint("accepted_count + rejected_count <= child_count", name="bounded_outcomes"),
    CheckConstraint("base_spec_checksum ~ '^sha256:[0-9a-f]{64}$'", name="base_spec_checksum"),
)

sweep_children = Table(
    "sweep_children",
    metadata,
    Column("sweep_id", UUID_TYPE, nullable=False),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("child_index", Integer, nullable=False),
    Column("parameter_hash", String(71), nullable=False),
    Column("job_id", UUID_TYPE),
    Column("rejected_error", JSONB),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("sweep_id", "child_index"),
    ForeignKeyConstraint(
        ["tenant_id", "sweep_id"],
        ["sweep_parents.tenant_id", "sweep_parents.sweep_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id"], ["jobs.tenant_id", "jobs.job_id"], ondelete="RESTRICT"
    ),
    UniqueConstraint("sweep_id", "parameter_hash"),
    CheckConstraint("child_index BETWEEN 0 AND 99", name="child_index"),
    CheckConstraint("parameter_hash ~ '^sha256:[0-9a-f]{64}$'", name="parameter_hash"),
    CheckConstraint("(job_id IS NULL) <> (rejected_error IS NULL)", name="exactly_one_outcome"),
)

workers = Table(
    "workers",
    metadata,
    Column("worker_id", UUID_TYPE, primary_key=True),
    Column("admin_state", String(32), nullable=False),
    Column("health", String(32), nullable=False),
    Column("current_incarnation_id", UUID_TYPE),
    Column("current_inventory_version", BigInteger),
    Column("last_heartbeat_at", UTC_TIMESTAMP),
    Column("ready_at", UTC_TIMESTAMP),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    *_timestamps(),
    CheckConstraint("admin_state IN ('ENABLED','DRAINING','DISABLED')", name="admin_state"),
    CheckConstraint("health IN ('STARTING','READY','SUSPECT','UNAVAILABLE')", name="health"),
    _version_check(),
)
Index(
    "ix_workers_admin_health", workers.c.admin_state, workers.c.health, workers.c.last_heartbeat_at
)

worker_credentials = Table(
    "worker_credentials",
    metadata,
    Column("credential_id", UUID_TYPE, primary_key=True),
    Column(
        "worker_id", UUID_TYPE, ForeignKey("workers.worker_id", ondelete="RESTRICT"), nullable=False
    ),
    Column("credential_hash", LargeBinary, nullable=False),
    Column("scopes", ARRAY(String(64)), nullable=False),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("revoked_at", UTC_TIMESTAMP),
    *_timestamps(),
    UniqueConstraint("credential_hash"),
    CheckConstraint("octet_length(credential_hash) >= 32", name="credential_hash_length"),
)
Index(
    "ix_worker_credentials_worker_expiry",
    worker_credentials.c.worker_id,
    worker_credentials.c.expires_at,
)

worker_incarnations = Table(
    "worker_incarnations",
    metadata,
    Column("worker_incarnation_id", UUID_TYPE, primary_key=True),
    Column("worker_id", UUID_TYPE, nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("process_start_nonce", UUID_TYPE, nullable=False),
    Column("process_started_at", UTC_TIMESTAMP, nullable=False),
    Column("reconcile_completed_at", UTC_TIMESTAMP),
    Column("ready_at", UTC_TIMESTAMP),
    Column("ended_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    ForeignKeyConstraint(["worker_id"], ["workers.worker_id"], ondelete="RESTRICT"),
    UniqueConstraint(
        "worker_id", "worker_incarnation_id", name="uq_worker_incarnations_worker_incarnation"
    ),
    UniqueConstraint("worker_id", "sequence", name="uq_worker_incarnations_sequence"),
    UniqueConstraint(
        "worker_id",
        "process_start_nonce",
        name="uq_worker_incarnations_process_nonce",
    ),
    _version_check("sequence"),
)
Index(
    "uq_worker_incarnations_current",
    worker_incarnations.c.worker_id,
    unique=True,
    postgresql_where=worker_incarnations.c.ended_at.is_(None),
)
workers.append_constraint(
    ForeignKeyConstraint(
        [workers.c.worker_id, workers.c.current_incarnation_id],
        [worker_incarnations.c.worker_id, worker_incarnations.c.worker_incarnation_id],
        name="fk_workers_current_incarnation",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)

worker_inventories = Table(
    "worker_inventories",
    metadata,
    Column("inventory_id", UUID_TYPE, primary_key=True),
    Column("worker_id", UUID_TYPE, nullable=False),
    Column("worker_incarnation_id", UUID_TYPE, nullable=False),
    Column("inventory_version", BigInteger, nullable=False),
    Column("architecture", String(64), nullable=False),
    Column("host_cpu_millis", BigInteger, nullable=False),
    Column("host_memory_bytes", BigInteger, nullable=False),
    Column("allocatable_cpu_millis", BigInteger, nullable=False),
    Column("allocatable_memory_bytes", BigInteger, nullable=False),
    Column("allocatable_gpu_count", Integer, nullable=False),
    Column("runtime_capabilities", JSONB, nullable=False),
    Column("workload_capabilities", JSONB, nullable=False),
    Column("checksum", String(71), nullable=False),
    Column("observed_at", UTC_TIMESTAMP, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["worker_id", "worker_incarnation_id"],
        ["worker_incarnations.worker_id", "worker_incarnations.worker_incarnation_id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("worker_id", "inventory_version"),
    UniqueConstraint(
        "worker_id", "inventory_version", "inventory_id", name="uq_worker_inventories_identity"
    ),
    _version_check("inventory_version"),
    _nonnegative_check("host_cpu_millis"),
    _nonnegative_check("host_memory_bytes"),
    _nonnegative_check("allocatable_cpu_millis"),
    _nonnegative_check("allocatable_memory_bytes"),
    CheckConstraint("allocatable_gpu_count >= 0", name="allocatable_gpu_count"),
    CheckConstraint("allocatable_cpu_millis <= host_cpu_millis", name="cpu_within_host"),
    CheckConstraint("allocatable_memory_bytes <= host_memory_bytes", name="memory_within_host"),
    CheckConstraint("checksum ~ '^sha256:[0-9a-f]{64}$'", name="checksum"),
)

gpu_devices = Table(
    "gpu_devices",
    metadata,
    Column("worker_id", UUID_TYPE, nullable=False),
    Column("inventory_version", BigInteger, nullable=False),
    Column("inventory_id", UUID_TYPE, nullable=False),
    Column("gpu_uuid", String(128), nullable=False),
    Column("model", String(255), nullable=False),
    Column("memory_bytes", BigInteger, nullable=False),
    Column("compute_capability", String(32), nullable=False),
    Column("driver_version", String(64), nullable=False),
    Column("api_version", String(64), nullable=False),
    Column("healthy", Boolean, nullable=False),
    PrimaryKeyConstraint("worker_id", "inventory_version", "gpu_uuid"),
    ForeignKeyConstraint(
        ["worker_id", "inventory_version", "inventory_id"],
        [
            "worker_inventories.worker_id",
            "worker_inventories.inventory_version",
            "worker_inventories.inventory_id",
        ],
        ondelete="RESTRICT",
    ),
    CheckConstraint("memory_bytes > 0", name="memory_bytes"),
)

coordinator_leadership = Table(
    "coordinator_leadership",
    metadata,
    Column("singleton_key", String(32), primary_key=True),
    Column("holder_id", UUID_TYPE),
    Column("epoch", BigInteger, nullable=False, server_default=text("0")),
    Column("lease_expires_at", UTC_TIMESTAMP),
    Column("renewed_at", UTC_TIMESTAMP),
    CheckConstraint("singleton_key = 'coordinator'", name="singleton_key"),
    _nonnegative_check("epoch"),
)

attempts = Table(
    "attempts",
    metadata,
    Column("attempt_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("state", String(32), nullable=False),
    Column("execution_intent", String(64), nullable=False),
    Column("worker_id", UUID_TYPE),
    Column("worker_incarnation_id", UUID_TYPE),
    Column("job_fence", BigInteger, nullable=False),
    Column("startup_nonce", UUID_TYPE, nullable=False),
    Column("failure_class", String(32)),
    Column("failure_reason", String(64)),
    Column("progress_sequence", BigInteger, nullable=False, server_default=text("0")),
    Column("progress_snapshot", JSONB),
    Column("started_at", UTC_TIMESTAMP),
    Column("ended_at", UTC_TIMESTAMP),
    *_timestamps(),
    ForeignKeyConstraint(
        ["tenant_id", "job_id"], ["jobs.tenant_id", "jobs.job_id"], ondelete="RESTRICT"
    ),
    ForeignKeyConstraint(
        ["worker_id", "worker_incarnation_id"],
        ["worker_incarnations.worker_id", "worker_incarnations.worker_incarnation_id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("job_id", "attempt_number"),
    UniqueConstraint("startup_nonce"),
    UniqueConstraint("tenant_id", "job_id", "attempt_id", name="uq_attempts_tenant_job_attempt"),
    UniqueConstraint(
        "tenant_id", "job_id", "attempt_id", "worker_id", name="uq_attempts_worker_identity"
    ),
    UniqueConstraint(
        "tenant_id", "job_id", "attempt_id", "job_fence", name="uq_attempts_authority_identity"
    ),
    UniqueConstraint(
        "tenant_id",
        "job_id",
        "attempt_id",
        "job_fence",
        "worker_id",
        name="uq_attempts_authority_worker_identity",
    ),
    CheckConstraint("attempt_number >= 1", name="attempt_number"),
    _nonnegative_check("job_fence"),
    _nonnegative_check("progress_sequence"),
    CheckConstraint(
        "state IN ('CREATED','CLAIMED','STARTING','RUNNING','CHECKPOINTING','STOPPING',"
        "'SUCCEEDED','FAILED','LOST','CANCELLED')",
        name="state",
    ),
    CheckConstraint("execution_intent IN ('RUN','CHECKPOINT_FOR_PAUSE')", name="execution_intent"),
)
Index(
    "ix_attempts_job_created_keyset",
    attempts.c.job_id,
    attempts.c.created_at.desc(),
    attempts.c.attempt_id.desc(),
)

retry_schedules = Table(
    "retry_schedules",
    metadata,
    Column("job_id", UUID_TYPE, nullable=False),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("retry_number", Integer, nullable=False),
    Column("ready_at", UTC_TIMESTAMP, nullable=False),
    Column("jitter_milliseconds", Integer, nullable=False),
    Column("reason", String(64), nullable=False),
    Column("closed_at", UTC_TIMESTAMP),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("job_id", "retry_number"),
    ForeignKeyConstraint(
        ["tenant_id", "job_id"], ["jobs.tenant_id", "jobs.job_id"], ondelete="RESTRICT"
    ),
    CheckConstraint("retry_number BETWEEN 1 AND 2", name="retry_number"),
    CheckConstraint("jitter_milliseconds BETWEEN 0 AND 1000", name="jitter"),
)
Index(
    "ix_retry_schedules_ready",
    retry_schedules.c.ready_at,
    retry_schedules.c.job_id,
    postgresql_where=retry_schedules.c.closed_at.is_(None),
)

allocations = Table(
    "allocations",
    metadata,
    Column("allocation_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("attempt_id", UUID_TYPE, nullable=False),
    Column("worker_id", UUID_TYPE, nullable=False),
    Column("cpu_millis", BigInteger, nullable=False),
    Column("memory_bytes", BigInteger, nullable=False),
    Column("gpu_count", Integer, nullable=False),
    Column("state", String(32), nullable=False),
    Column("held_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    Column("quarantined_at", UTC_TIMESTAMP),
    Column("released_at", UTC_TIMESTAMP),
    Column("release_reason", String(64)),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id", "worker_id"],
        [
            "attempts.tenant_id",
            "attempts.job_id",
            "attempts.attempt_id",
            "attempts.worker_id",
        ],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(["worker_id"], ["workers.worker_id"], ondelete="RESTRICT"),
    UniqueConstraint("attempt_id"),
    UniqueConstraint(
        "tenant_id",
        "job_id",
        "attempt_id",
        "allocation_id",
        name="uq_allocations_authority_identity",
    ),
    UniqueConstraint(
        "tenant_id",
        "job_id",
        "attempt_id",
        "allocation_id",
        "worker_id",
        name="uq_allocations_authority_worker_identity",
    ),
    UniqueConstraint("tenant_id", "allocation_id", name="uq_allocations_tenant_allocation"),
    CheckConstraint("cpu_millis > 0", name="cpu_millis"),
    CheckConstraint("memory_bytes > 0", name="memory_bytes"),
    CheckConstraint("gpu_count IN (0, 1)", name="gpu_count"),
    CheckConstraint("state IN ('HELD','QUARANTINED','RELEASED')", name="state"),
    CheckConstraint("(state = 'RELEASED') = (released_at IS NOT NULL)", name="released_timestamp"),
    CheckConstraint(
        "(state = 'QUARANTINED') = (quarantined_at IS NOT NULL) OR state = 'RELEASED'",
        name="quarantine_timestamp",
    ),
)
Index(
    "ix_allocations_unreleased_tenant",
    allocations.c.tenant_id,
    allocations.c.state,
    allocations.c.allocation_id,
    postgresql_where=allocations.c.state != "RELEASED",
)
Index(
    "ix_allocations_unreleased_worker",
    allocations.c.worker_id,
    allocations.c.state,
    allocations.c.allocation_id,
    postgresql_where=allocations.c.state != "RELEASED",
)

allocation_gpu_claims = Table(
    "allocation_gpu_claims",
    metadata,
    Column("allocation_id", UUID_TYPE, nullable=False),
    Column("worker_id", UUID_TYPE, nullable=False),
    Column("inventory_version", BigInteger, nullable=False),
    Column("gpu_uuid", String(128), nullable=False),
    Column("claimed_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    Column("released_at", UTC_TIMESTAMP),
    PrimaryKeyConstraint("allocation_id", "gpu_uuid"),
    ForeignKeyConstraint(["allocation_id"], ["allocations.allocation_id"], ondelete="RESTRICT"),
    ForeignKeyConstraint(
        ["worker_id", "inventory_version", "gpu_uuid"],
        ["gpu_devices.worker_id", "gpu_devices.inventory_version", "gpu_devices.gpu_uuid"],
        ondelete="RESTRICT",
    ),
)
Index(
    "uq_allocation_gpu_claims_active_device",
    allocation_gpu_claims.c.worker_id,
    allocation_gpu_claims.c.gpu_uuid,
    unique=True,
    postgresql_where=allocation_gpu_claims.c.released_at.is_(None),
)
Index(
    "ix_allocation_gpu_claims_active_allocation",
    allocation_gpu_claims.c.allocation_id,
    postgresql_where=allocation_gpu_claims.c.released_at.is_(None),
)

attempt_leases = Table(
    "attempt_leases",
    metadata,
    Column("lease_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("attempt_id", UUID_TYPE, nullable=False),
    Column("allocation_id", UUID_TYPE, nullable=False),
    Column("worker_id", UUID_TYPE, nullable=False),
    Column("current_worker_incarnation_id", UUID_TYPE, nullable=False),
    Column("job_fence", BigInteger, nullable=False),
    Column("issued_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("revoked_at", UTC_TIMESTAMP),
    Column("revoke_reason", String(64)),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id", "job_fence", "worker_id"],
        [
            "attempts.tenant_id",
            "attempts.job_id",
            "attempts.attempt_id",
            "attempts.job_fence",
            "attempts.worker_id",
        ],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id", "allocation_id", "worker_id"],
        [
            "allocations.tenant_id",
            "allocations.job_id",
            "allocations.attempt_id",
            "allocations.allocation_id",
            "allocations.worker_id",
        ],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["worker_id", "current_worker_incarnation_id"],
        ["worker_incarnations.worker_id", "worker_incarnations.worker_incarnation_id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint(
        "tenant_id",
        "job_id",
        "attempt_id",
        "lease_id",
        "job_fence",
        "allocation_id",
        name="uq_attempt_leases_authority_identity",
    ),
    UniqueConstraint(
        "tenant_id",
        "job_id",
        "attempt_id",
        "lease_id",
        "job_fence",
        "allocation_id",
        "worker_id",
        name="uq_attempt_leases_authority_worker_identity",
    ),
)
Index(
    "uq_attempt_leases_active_attempt",
    attempt_leases.c.attempt_id,
    unique=True,
    postgresql_where=attempt_leases.c.revoked_at.is_(None),
)
Index(
    "ix_attempt_leases_active_expiry",
    attempt_leases.c.expires_at,
    attempt_leases.c.lease_id,
    postgresql_where=attempt_leases.c.revoked_at.is_(None),
)

attempt_authority_grants = Table(
    "attempt_authority_grants",
    metadata,
    Column("grant_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("attempt_id", UUID_TYPE, nullable=False),
    Column("allocation_id", UUID_TYPE, nullable=False),
    Column("lease_id", UUID_TYPE, nullable=False),
    Column("worker_id", UUID_TYPE, nullable=False),
    Column("worker_incarnation_id", UUID_TYPE, nullable=False),
    Column("job_fence", BigInteger, nullable=False),
    Column("predecessor_grant_id", UUID_TYPE),
    Column("callback_id", UUID_TYPE, nullable=False),
    Column("granted_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    Column("ended_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id", "job_fence", "worker_id"],
        [
            "attempts.tenant_id",
            "attempts.job_id",
            "attempts.attempt_id",
            "attempts.job_fence",
            "attempts.worker_id",
        ],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id", "allocation_id", "worker_id"],
        [
            "allocations.tenant_id",
            "allocations.job_id",
            "allocations.attempt_id",
            "allocations.allocation_id",
            "allocations.worker_id",
        ],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        [
            "tenant_id",
            "job_id",
            "attempt_id",
            "lease_id",
            "job_fence",
            "allocation_id",
            "worker_id",
        ],
        [
            "attempt_leases.tenant_id",
            "attempt_leases.job_id",
            "attempt_leases.attempt_id",
            "attempt_leases.lease_id",
            "attempt_leases.job_fence",
            "attempt_leases.allocation_id",
            "attempt_leases.worker_id",
        ],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["worker_id", "worker_incarnation_id"],
        ["worker_incarnations.worker_id", "worker_incarnations.worker_incarnation_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["predecessor_grant_id"],
        ["attempt_authority_grants.grant_id"],
        ondelete="RESTRICT",
        deferrable=True,
    ),
    UniqueConstraint("callback_id"),
    UniqueConstraint(
        "attempt_id",
        "worker_incarnation_id",
        name="uq_authority_grants_attempt_incarnation",
    ),
    UniqueConstraint(
        "tenant_id", "job_id", "attempt_id", "grant_id", name="uq_authority_grants_owner"
    ),
)
Index(
    "uq_attempt_authority_grants_current_job",
    attempt_authority_grants.c.job_id,
    unique=True,
    postgresql_where=attempt_authority_grants.c.ended_at.is_(None),
)
Index(
    "ix_attempt_authority_grants_attempt_lineage",
    attempt_authority_grants.c.attempt_id,
    attempt_authority_grants.c.granted_at,
    attempt_authority_grants.c.grant_id,
)

container_identities = Table(
    "container_identities",
    metadata,
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("attempt_id", UUID_TYPE, nullable=False),
    Column("allocation_id", UUID_TYPE, nullable=False),
    Column("startup_nonce", UUID_TYPE, nullable=False),
    Column("executor_create_sequence", BigInteger, nullable=False),
    Column("container_id", String(128), nullable=False),
    Column("runtime_identity_digest", String(71), nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False),
    Column("stopped_at", UTC_TIMESTAMP),
    Column("verified_at", UTC_TIMESTAMP),
    PrimaryKeyConstraint("attempt_id", "container_id"),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id"],
        ["attempts.tenant_id", "attempts.job_id", "attempts.attempt_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id", "allocation_id"],
        [
            "allocations.tenant_id",
            "allocations.job_id",
            "allocations.attempt_id",
            "allocations.allocation_id",
        ],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("startup_nonce"),
    _version_check("executor_create_sequence"),
    CheckConstraint(
        "runtime_identity_digest ~ '^sha256:[0-9a-f]{64}$'", name="runtime_identity_digest"
    ),
)

policy_versions = Table(
    "policy_versions",
    metadata,
    Column("policy_version", BigInteger, primary_key=True),
    Column("global_outstanding_limit", BigInteger, nullable=False),
    Column("operational_mode", String(32), nullable=False),
    Column("created_by_user_id", UUID_TYPE, ForeignKey("users.user_id", ondelete="RESTRICT")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    Column("is_current", Boolean, nullable=False, server_default=text("false")),
    _version_check("policy_version"),
    CheckConstraint(
        "global_outstanding_limit BETWEEN 1 AND 1000000", name="global_outstanding_limit"
    ),
    CheckConstraint(
        "operational_mode IN ('NORMAL','ADMISSION_OFF','WRITE_FROZEN')", name="operational_mode"
    ),
)
Index(
    "uq_policy_versions_current",
    policy_versions.c.is_current,
    unique=True,
    postgresql_where=policy_versions.c.is_current.is_(True),
)

tenant_policies = Table(
    "tenant_policies",
    metadata,
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("weight", EXACT_DECIMAL_TEXT, nullable=False),
    Column("cpu_limit_millis", BigInteger, nullable=False),
    Column("memory_limit_bytes", BigInteger, nullable=False),
    Column("gpu_limit", Integer, nullable=False),
    Column("outstanding_limit", BigInteger, nullable=False),
    Column("user_outstanding_limit", BigInteger, nullable=False),
    Column("tenant_active_limit", Integer, nullable=False),
    Column("user_active_limit", Integer, nullable=False),
    Column("tenant_rate_per_second", EXACT_DECIMAL_TEXT, nullable=False),
    Column("tenant_rate_burst", EXACT_DECIMAL_TEXT, nullable=False),
    Column("user_rate_per_second", EXACT_DECIMAL_TEXT, nullable=False),
    Column("user_rate_burst", EXACT_DECIMAL_TEXT, nullable=False),
    Column("is_current", Boolean, nullable=False, server_default=text("false")),
    *_timestamps(),
    PrimaryKeyConstraint("tenant_id", "version"),
    ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="RESTRICT"),
    _version_check(),
    CheckConstraint(
        "nexa_decimal_is_positive(weight)",
        name="weight_finite_positive",
    ),
    _nonnegative_check("cpu_limit_millis"),
    _nonnegative_check("memory_limit_bytes"),
    CheckConstraint("gpu_limit >= 0", name="gpu_limit"),
    CheckConstraint("outstanding_limit >= 1", name="outstanding_limit"),
    CheckConstraint("user_outstanding_limit >= 1", name="user_outstanding_limit"),
    CheckConstraint("tenant_active_limit >= 1 AND user_active_limit >= 1", name="active_limits"),
    CheckConstraint(
        "nexa_decimal_is_positive(tenant_rate_per_second)",
        name="tenant_rate",
    ),
    CheckConstraint(
        "nexa_decimal_is_positive(tenant_rate_burst)",
        name="tenant_burst",
    ),
    CheckConstraint(
        "nexa_decimal_is_positive(user_rate_per_second)",
        name="user_rate",
    ),
    CheckConstraint(
        "nexa_decimal_is_positive(user_rate_burst)",
        name="user_burst",
    ),
)
Index(
    "uq_tenant_policies_current",
    tenant_policies.c.tenant_id,
    unique=True,
    postgresql_where=tenant_policies.c.is_current.is_(True),
)

admission_counters = Table(
    "admission_counters",
    metadata,
    Column("scope_type", String(16), nullable=False),
    Column("scope_id", String(128), nullable=False),
    Column("outstanding", BigInteger, nullable=False, server_default=text("0")),
    Column("active_attempts", BigInteger, nullable=False, server_default=text("0")),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("scope_type", "scope_id"),
    CheckConstraint("scope_type IN ('GLOBAL','TENANT','USER')", name="scope_type"),
    _nonnegative_check("outstanding"),
    _nonnegative_check("active_attempts"),
    _version_check(),
)

rate_buckets = Table(
    "rate_buckets",
    metadata,
    Column("scope_type", String(16), nullable=False),
    Column("scope_id", String(128), nullable=False),
    Column("tokens", EXACT_DECIMAL_TEXT, nullable=False),
    Column("capacity", EXACT_DECIMAL_TEXT, nullable=False),
    Column("refill_rate", EXACT_DECIMAL_TEXT, nullable=False),
    Column("last_refill_at", UTC_TIMESTAMP, nullable=False),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("scope_type", "scope_id"),
    CheckConstraint("scope_type IN ('GLOBAL','TENANT','USER','LOGIN')", name="scope_type"),
    _finite_nonnegative_decimal("tokens"),
    CheckConstraint(
        "nexa_decimal_is_positive(capacity)",
        name="capacity_finite_positive",
    ),
    CheckConstraint(
        "nexa_decimal_is_positive(refill_rate)",
        name="refill_rate_finite_positive",
    ),
    CheckConstraint("nexa_decimal_compare(tokens, capacity) <= 0", name="tokens_within_capacity"),
    _version_check(),
)

fairness_state = Table(
    "fairness_state",
    metadata,
    Column("singleton_key", String(32), primary_key=True),
    Column("virtual_floor", EXACT_DECIMAL_TEXT, nullable=False),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    CheckConstraint("singleton_key = 'local'", name="singleton_key"),
    _finite_nonnegative_decimal("virtual_floor"),
    _version_check(),
)

fairness_ledgers = Table(
    "fairness_ledgers",
    metadata,
    Column(
        "tenant_id",
        UUID_TYPE,
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("virtual_score", EXACT_DECIMAL_TEXT, nullable=False),
    Column("accounted_through", UTC_TIMESTAMP, nullable=False),
    Column("had_eligible_demand", Boolean, nullable=False),
    Column("version", BigInteger, nullable=False, server_default=text("1")),
    Column("updated_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    _finite_nonnegative_decimal("virtual_score"),
    _version_check(),
)
Index(
    "ix_fairness_ledgers_score",
    func.nexa_decimal_zero_rank(fairness_ledgers.c.virtual_score),
    func.nexa_decimal_adjusted_exponent(fairness_ledgers.c.virtual_score),
    func.left(
        func.nexa_decimal_normalized_significand(fairness_ledgers.c.virtual_score), 256
    ).collate("C"),
    fairness_ledgers.c.tenant_id,
)

allocation_ledger_segments = Table(
    "allocation_ledger_segments",
    metadata,
    Column("segment_id", UUID_TYPE, primary_key=True),
    Column("allocation_id", UUID_TYPE, nullable=False),
    Column(
        "tenant_id", UUID_TYPE, ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), nullable=False
    ),
    Column("started_at", UTC_TIMESTAMP, nullable=False),
    Column("ended_at", UTC_TIMESTAMP),
    Column("dominant_share", EXACT_DECIMAL_TEXT, nullable=False),
    Column("weight", EXACT_DECIMAL_TEXT, nullable=False),
    Column("charged_amount", EXACT_DECIMAL_TEXT, nullable=False),
    Column(
        "policy_version",
        BigInteger,
        ForeignKey("policy_versions.policy_version", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["tenant_id", "allocation_id"],
        ["allocations.tenant_id", "allocations.allocation_id"],
        ondelete="RESTRICT",
    ),
    _finite_nonnegative_decimal("dominant_share"),
    CheckConstraint(
        "nexa_decimal_is_positive(weight)",
        name="weight_finite_positive",
    ),
    _finite_nonnegative_decimal("charged_amount"),
    CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="time_order"),
)
Index(
    "ix_allocation_ledger_segments_open",
    allocation_ledger_segments.c.allocation_id,
    unique=True,
    postgresql_where=allocation_ledger_segments.c.ended_at.is_(None),
)
Index(
    "ix_allocation_ledger_segments_tenant_time",
    allocation_ledger_segments.c.tenant_id,
    allocation_ledger_segments.c.started_at,
    allocation_ledger_segments.c.segment_id,
)

reservations = Table(
    "reservations",
    metadata,
    Column("reservation_id", UUID_TYPE, primary_key=True),
    Column("slot_key", String(32), nullable=False, server_default=text("'local'")),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("eligibility_since", UTC_TIMESTAMP, nullable=False),
    Column(
        "policy_version",
        BigInteger,
        ForeignKey("policy_versions.policy_version", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    Column("invalidated_at", UTC_TIMESTAMP),
    Column("invalidation_reason", String(64)),
    ForeignKeyConstraint(
        ["tenant_id", "job_id"], ["jobs.tenant_id", "jobs.job_id"], ondelete="RESTRICT"
    ),
    CheckConstraint(
        "(invalidated_at IS NULL) = (invalidation_reason IS NULL)", name="invalidation_pair"
    ),
    CheckConstraint("slot_key = 'local'", name="slot_key"),
)
Index(
    "uq_reservations_one_active",
    reservations.c.slot_key,
    unique=True,
    postgresql_where=reservations.c.invalidated_at.is_(None),
)

queue_heads = Table(
    "queue_heads",
    metadata,
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("priority", Integer, nullable=False),
    Column("candidate_job_id", UUID_TYPE, nullable=False),
    Column("ready_sequence", BigInteger, nullable=False),
    Column("eligible_since", UTC_TIMESTAMP),
    Column("retry_ready_at", UTC_TIMESTAMP),
    Column("rebuilt_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("tenant_id", "priority"),
    ForeignKeyConstraint(
        ["tenant_id", "candidate_job_id"], ["jobs.tenant_id", "jobs.job_id"], ondelete="RESTRICT"
    ),
    CheckConstraint("priority IN (0, 1, 2)", name="priority"),
    CheckConstraint("ready_sequence >= 0", name="ready_sequence"),
)

events = Table(
    "events",
    metadata,
    Column("event_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE),
    Column("job_id", UUID_TYPE),
    Column("sequence", BigInteger),
    Column("event_type", String(64), nullable=False),
    Column("reason", String(64)),
    Column("actor_type", String(32), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("safe_metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["tenant_id", "job_id"], ["jobs.tenant_id", "jobs.job_id"], ondelete="RESTRICT"
    ),
    UniqueConstraint("job_id", "sequence"),
    CheckConstraint("(job_id IS NULL) = (tenant_id IS NULL)", name="job_tenant_pair"),
    CheckConstraint("(job_id IS NULL) = (sequence IS NULL)", name="job_sequence_pair"),
    CheckConstraint("sequence IS NULL OR sequence >= 1", name="sequence_positive"),
)
Index("ix_events_job_sequence_keyset", events.c.job_id, events.c.sequence, events.c.event_id)

audit_records = Table(
    "audit_records",
    metadata,
    Column("audit_id", UUID_TYPE, primary_key=True),
    Column("actor_type", String(32), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("tenant_id", UUID_TYPE, ForeignKey("tenants.tenant_id", ondelete="RESTRICT")),
    Column("action", String(128), nullable=False),
    Column("target_type", String(64), nullable=False),
    Column("target_id", String(128), nullable=False),
    Column("before_version", BigInteger),
    Column("after_version", BigInteger),
    Column("reason", String(128)),
    Column("safe_metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    CheckConstraint("before_version IS NULL OR before_version >= 1", name="before_version"),
    CheckConstraint("after_version IS NULL OR after_version >= 1", name="after_version"),
)
Index(
    "ix_audit_records_created_action",
    audit_records.c.created_at.desc(),
    audit_records.c.action,
    audit_records.c.audit_id.desc(),
)
Index(
    "ix_audit_records_tenant_created",
    audit_records.c.tenant_id,
    audit_records.c.created_at.desc(),
    audit_records.c.audit_id.desc(),
)

idempotency_records = Table(
    "idempotency_records",
    metadata,
    Column("idempotency_id", UUID_TYPE, primary_key=True),
    Column("context", String(128), nullable=False),
    Column("principal_id", String(128), nullable=False),
    Column("operation_id", String(128), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_hash", String(71), nullable=False),
    Column("original_authority", JSONB),
    Column("normalized_upload_metadata", JSONB),
    Column("state", String(16), nullable=False),
    Column("response_status", Integer),
    Column("response_body", JSONB),
    Column("response_headers", JSONB),
    Column("resource_id", UUID_TYPE),
    Column("one_time_secret", Boolean, nullable=False, server_default=text("false")),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    *_timestamps(),
    UniqueConstraint(
        "context", "principal_id", "operation_id", "idempotency_key", name="uq_idempotency_scope"
    ),
    CheckConstraint(
        "context <> '' AND principal_id <> '' AND operation_id <> ''", name="nonempty_scope"
    ),
    CheckConstraint(
        "context IN ('GLOBAL', 'BOOTSTRAP') OR "
        "context ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' OR "
        "context ~ '^WORKER:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'",
        name="context_namespace",
    ),
    CheckConstraint(
        "length(idempotency_key) BETWEEN 16 AND 128 AND idempotency_key !~ '[[:space:]]'",
        name="key_format",
    ),
    CheckConstraint("request_hash ~ '^sha256:[0-9a-f]{64}$'", name="request_hash"),
    CheckConstraint("state IN ('PENDING','COMPLETED')", name="state"),
    CheckConstraint(
        "response_status IS NULL OR response_status BETWEEN 100 AND 599", name="response_status"
    ),
)
Index(
    "ix_idempotency_records_expiry",
    idempotency_records.c.expires_at,
    idempotency_records.c.idempotency_id,
)

callback_receipts = Table(
    "callback_receipts",
    metadata,
    Column("receipt_id", UUID_TYPE, primary_key=True),
    Column(
        "worker_id", UUID_TYPE, ForeignKey("workers.worker_id", ondelete="RESTRICT"), nullable=False
    ),
    Column("operation_id", String(128), nullable=False),
    Column("callback_id", UUID_TYPE, nullable=False),
    Column("payload_hash", String(71), nullable=False),
    Column("acknowledgment", JSONB, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    UniqueConstraint("worker_id", "operation_id", "callback_id", name="uq_callback_scope"),
    CheckConstraint("payload_hash ~ '^sha256:[0-9a-f]{64}$'", name="payload_hash"),
)

checkpoint_reservations = Table(
    "checkpoint_reservations",
    metadata,
    Column("checkpoint_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("attempt_id", UUID_TYPE, nullable=False),
    Column("authority_grant_id", UUID_TYPE, nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("callback_id", UUID_TYPE, nullable=False),
    Column("state", String(16), nullable=False),
    Column("reserved_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    Column("ended_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id"],
        ["attempts.tenant_id", "attempts.job_id", "attempts.attempt_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id", "authority_grant_id"],
        [
            "attempt_authority_grants.tenant_id",
            "attempt_authority_grants.job_id",
            "attempt_authority_grants.attempt_id",
            "attempt_authority_grants.grant_id",
        ],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("job_id", "sequence"),
    UniqueConstraint("callback_id"),
    UniqueConstraint(
        "tenant_id",
        "job_id",
        "attempt_id",
        "checkpoint_id",
        "sequence",
        name="uq_checkpoint_reservations_identity",
    ),
    CheckConstraint("sequence >= 1", name="sequence"),
    CheckConstraint("state IN ('RESERVED','COMMITTED','REJECTED','ABANDONED')", name="state"),
)
Index(
    "uq_checkpoint_reservations_reserved_attempt",
    checkpoint_reservations.c.attempt_id,
    unique=True,
    postgresql_where=checkpoint_reservations.c.state == "RESERVED",
)

checkpoints = Table(
    "checkpoints",
    metadata,
    Column("checkpoint_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("attempt_id", UUID_TYPE, nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("manifest_artifact_id", UUID_TYPE, nullable=False),
    Column("manifest_checksum", String(71), nullable=False),
    Column("provenance", JSONB, nullable=False),
    Column("compatibility", JSONB, nullable=False),
    Column("state", String(16), nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["checkpoint_id"], ["checkpoint_reservations.checkpoint_id"], ondelete="RESTRICT"
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id", "checkpoint_id", "sequence"],
        [
            "checkpoint_reservations.tenant_id",
            "checkpoint_reservations.job_id",
            "checkpoint_reservations.attempt_id",
            "checkpoint_reservations.checkpoint_id",
            "checkpoint_reservations.sequence",
        ],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id"],
        ["attempts.tenant_id", "attempts.job_id", "attempts.attempt_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "manifest_artifact_id"],
        ["artifact_reference_guards.tenant_id", "artifact_reference_guards.artifact_id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("job_id", "sequence"),
    UniqueConstraint("tenant_id", "checkpoint_id", name="uq_checkpoints_tenant_checkpoint"),
    CheckConstraint("state = 'COMMITTED'", name="state"),
    CheckConstraint("manifest_checksum ~ '^sha256:[0-9a-f]{64}$'", name="manifest_checksum"),
)
Index(
    "ix_checkpoints_job_created",
    checkpoints.c.job_id,
    checkpoints.c.sequence.desc(),
    checkpoints.c.checkpoint_id.desc(),
)

checkpoint_references = Table(
    "checkpoint_references",
    metadata,
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("source_checkpoint_id", UUID_TYPE, nullable=False),
    Column("target_job_id", UUID_TYPE, nullable=False),
    Column("reason", String(64), nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("target_job_id", "source_checkpoint_id"),
    ForeignKeyConstraint(
        ["tenant_id", "source_checkpoint_id"],
        ["checkpoints.tenant_id", "checkpoints.checkpoint_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "target_job_id"], ["jobs.tenant_id", "jobs.job_id"], ondelete="RESTRICT"
    ),
)

result_reservations = Table(
    "result_reservations",
    metadata,
    Column("result_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("attempt_id", UUID_TYPE, nullable=False),
    Column("authority_grant_id", UUID_TYPE, nullable=False),
    Column("callback_id", UUID_TYPE, nullable=False),
    Column("state", String(16), nullable=False),
    Column("reserved_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    Column("superseded_at", UTC_TIMESTAMP),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id"],
        ["attempts.tenant_id", "attempts.job_id", "attempts.attempt_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id", "authority_grant_id"],
        [
            "attempt_authority_grants.tenant_id",
            "attempt_authority_grants.job_id",
            "attempt_authority_grants.attempt_id",
            "attempt_authority_grants.grant_id",
        ],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("callback_id"),
    UniqueConstraint(
        "tenant_id", "job_id", "attempt_id", "result_id", name="uq_result_reservations_identity"
    ),
    CheckConstraint("state IN ('ACTIVE','COMMITTED','ABANDONED')", name="state"),
)
Index(
    "uq_result_reservations_active_job",
    result_reservations.c.job_id,
    unique=True,
    postgresql_where=result_reservations.c.state == "ACTIVE",
)

results = Table(
    "results",
    metadata,
    Column("result_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("attempt_id", UUID_TYPE, nullable=False),
    Column("manifest_artifact_id", UUID_TYPE, nullable=False),
    Column("manifest_checksum", String(71), nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    ForeignKeyConstraint(["result_id"], ["result_reservations.result_id"], ondelete="RESTRICT"),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id", "result_id"],
        [
            "result_reservations.tenant_id",
            "result_reservations.job_id",
            "result_reservations.attempt_id",
            "result_reservations.result_id",
        ],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id"],
        ["attempts.tenant_id", "attempts.job_id", "attempts.attempt_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "manifest_artifact_id"],
        ["artifact_reference_guards.tenant_id", "artifact_reference_guards.artifact_id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("job_id"),
    UniqueConstraint("tenant_id", "result_id", name="uq_results_tenant_result"),
    CheckConstraint("manifest_checksum ~ '^sha256:[0-9a-f]{64}$'", name="manifest_checksum"),
)
Index(
    "ix_results_tenant_created",
    results.c.tenant_id,
    results.c.created_at.desc(),
    results.c.result_id.desc(),
)

recognized_chunks = Table(
    "recognized_chunks",
    metadata,
    Column("recognized_chunk_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("session_id", UUID_TYPE, nullable=False),
    Column("chunk_id", String(128), nullable=False),
    Column("range_start", BigInteger, nullable=False),
    Column("range_end", BigInteger, nullable=False),
    Column("artifact_id", UUID_TYPE, nullable=False),
    Column("checksum", String(71), nullable=False),
    Column("source_attempt_id", UUID_TYPE, nullable=False),
    Column("source_job_fence", BigInteger, nullable=False),
    Column("recognized_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    UniqueConstraint("job_id", "chunk_id"),
    UniqueConstraint("tenant_id", "recognized_chunk_id", name="uq_recognized_chunks_tenant_chunk"),
    ForeignKeyConstraint(
        ["tenant_id", "job_id"], ["jobs.tenant_id", "jobs.job_id"], ondelete="RESTRICT"
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "session_id"],
        [
            "logical_sessions.tenant_id",
            "logical_sessions.job_id",
            "logical_sessions.session_id",
        ],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "source_attempt_id", "source_job_fence"],
        ["attempts.tenant_id", "attempts.job_id", "attempts.attempt_id", "attempts.job_fence"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "artifact_id"],
        ["artifact_reference_guards.tenant_id", "artifact_reference_guards.artifact_id"],
        ondelete="RESTRICT",
    ),
    CheckConstraint("range_start >= 0 AND range_end > range_start", name="chunk_range"),
    CheckConstraint("checksum ~ '^sha256:[0-9a-f]{64}$'", name="checksum"),
)

log_segments = Table(
    "log_segments",
    metadata,
    Column("log_segment_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE, nullable=False),
    Column("attempt_id", UUID_TYPE, nullable=False),
    Column("start_offset", BigInteger, nullable=False),
    Column("end_offset", BigInteger, nullable=False),
    Column("artifact_id", UUID_TYPE, nullable=False),
    Column("checksum", String(71), nullable=False),
    Column("truncated", Boolean, nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id"],
        ["attempts.tenant_id", "attempts.job_id", "attempts.attempt_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "artifact_id"],
        ["artifact_reference_guards.tenant_id", "artifact_reference_guards.artifact_id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("attempt_id", "start_offset"),
    CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="offset_range"),
    CheckConstraint("checksum ~ '^sha256:[0-9a-f]{64}$'", name="checksum"),
)
Index("ix_log_segments_attempt_offset", log_segments.c.attempt_id, log_segments.c.start_offset)

artifact_references = Table(
    "artifact_references",
    metadata,
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("artifact_id", UUID_TYPE, nullable=False),
    Column("owner_type", String(32), nullable=False),
    Column("owner_id", UUID_TYPE, nullable=False),
    Column(
        "upload_session_owner_id",
        UUID_TYPE,
        Computed("CASE WHEN owner_type = 'UPLOAD_SESSION' THEN owner_id END", persisted=True),
    ),
    Column("purpose", String(64), nullable=False),
    Column("logical_name", String(128), nullable=False),
    Column("created_at", UTC_TIMESTAMP, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("artifact_id", "owner_type", "owner_id", "purpose", "logical_name"),
    ForeignKeyConstraint(
        ["tenant_id", "artifact_id"],
        ["artifact_reference_guards.tenant_id", "artifact_reference_guards.artifact_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "upload_session_owner_id"],
        ["upload_sessions.tenant_id", "upload_sessions.upload_id"],
        name="fk_artifact_references_upload_session_owner",
        ondelete="RESTRICT",
    ),
    UniqueConstraint(
        "owner_type",
        "owner_id",
        "purpose",
        "logical_name",
        name="uq_artifact_references_owner_name",
    ),
    CheckConstraint(
        "owner_type IN ('JOB_SPEC','CHECKPOINT','RESULT','RECOGNIZED_CHUNK',"
        "'LOG_SEGMENT','UPLOAD_SESSION')",
        name="owner_type",
    ),
    CheckConstraint("logical_name ~ '^[a-z][a-z0-9_.-]{0,127}$'", name="logical_name"),
)
Index(
    "ix_artifact_references_owner",
    artifact_references.c.tenant_id,
    artifact_references.c.owner_type,
    artifact_references.c.owner_id,
)

upload_sessions = Table(
    "upload_sessions",
    metadata,
    Column("upload_id", UUID_TYPE, primary_key=True),
    Column("tenant_id", UUID_TYPE, nullable=False),
    Column("job_id", UUID_TYPE),
    Column("attempt_id", UUID_TYPE),
    Column("expected_size_bytes", BigInteger, nullable=False),
    Column("expected_checksum", String(71), nullable=False),
    Column("staged_key", String(512), nullable=False),
    Column("bytes_received", BigInteger, nullable=False, server_default=text("0")),
    Column("expires_at", UTC_TIMESTAMP, nullable=False),
    Column("state", String(16), nullable=False),
    *_timestamps(),
    ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="RESTRICT"),
    ForeignKeyConstraint(
        ["tenant_id", "job_id", "attempt_id"],
        ["attempts.tenant_id", "attempts.job_id", "attempts.attempt_id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("tenant_id", "upload_id", name="uq_upload_sessions_tenant_upload"),
    UniqueConstraint("staged_key"),
    _nonnegative_check("expected_size_bytes"),
    _nonnegative_check("bytes_received"),
    CheckConstraint("bytes_received <= expected_size_bytes", name="bytes_within_expected"),
    CheckConstraint("expected_checksum ~ '^sha256:[0-9a-f]{64}$'", name="expected_checksum"),
    CheckConstraint("state IN ('ACTIVE','COMMITTED','EXPIRED','ABORTED')", name="state"),
    CheckConstraint("(job_id IS NULL) = (attempt_id IS NULL)", name="attempt_pair"),
)
Index(
    "ix_upload_sessions_active_expiry",
    upload_sessions.c.expires_at,
    upload_sessions.c.upload_id,
    postgresql_where=upload_sessions.c.state == "ACTIVE",
)
