from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from nexa.api.schemas import JobSubmitRequest
from nexa.application.errors import ApplicationError
from nexa.application.idempotency import begin_idempotency, complete_idempotency
from nexa.application.identity_service import IdentityService
from nexa.application.json_codec import jcs_request_hash, json_wire_value
from nexa.config import Settings
from nexa.domain.identity import (
    AuthorizationError,
    Principal,
    TokenScope,
    require_tenant_membership,
)
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import clock_timestamp, transaction_timestamp
from nexa.infrastructure.persistence.schema import (
    admission_counters,
    artifact_references,
    artifacts,
    audit_records,
    checkpoint_corruptions,
    checkpoints,
    events,
    gpu_devices,
    job_specs,
    jobs,
    logical_sessions,
    policy_versions,
    rate_buckets,
    results,
    template_versions,
    templates,
    tenant_policies,
    worker_inventories,
    workers,
)
from nexa.infrastructure.persistence.transactions import run_transaction
from nexa.infrastructure.security import CursorCodec, CursorError, read_secret_file

_TEMPLATE_ARTIFACT_REQUIREMENTS = {
    ("cpu-iterative", 1): (
        "INPUT",
        "application/vnd.nexa.cpu-iterative-input+json",
        None,
        None,
    ),
    ("pytorch-cifar10-cnn", 1): (
        "DATASET",
        "application/vnd.apache.arrow.file",
        None,
        None,
    ),
    ("batch-inference", 1): (
        "DATASET",
        "application/vnd.apache.arrow.file",
        "MODEL",
        "application/octet-stream",
    ),
}


@dataclass(frozen=True, slots=True)
class JobOperationResult:
    status: int
    body: dict[str, Any]
    headers: dict[str, str]


class JobService:
    def __init__(self, session_factory, settings: Settings, identity: IdentityService) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.identity = identity
        self._cursor_key = read_secret_file(settings.server_secret_file)

    def _authorize(
        self,
        session: Session,
        principal: Principal,
        tenant_id: UUID,
        *,
        write: bool,
    ) -> Principal:
        live = self.identity.revalidate_principal(session, principal)
        try:
            require_tenant_membership(live, tenant_id)
        except AuthorizationError as exc:
            raise ApplicationError(
                code="permission_denied", status=403, message="Active tenant membership is required"
            ) from exc
        if live.credential_kind.name == "CLI":
            required = TokenScope.JOBS_WRITE if write else TokenScope.JOBS_READ
            if not live.has_scope(required):
                raise ApplicationError(
                    code="permission_denied",
                    status=403,
                    message=f"The exact {required.value} scope is required",
                )
        return live

    @staticmethod
    def _scope_id(scope_type: str, scope_id: str | UUID) -> str:
        return str(scope_id) if scope_type != "GLOBAL" else "global"

    @staticmethod
    def _counter_lock(session: Session, scope_type: str, scope_id: str) -> dict[str, Any]:
        session.execute(
            pg_insert(admission_counters)
            .values(scope_type=scope_type, scope_id=scope_id)
            .on_conflict_do_nothing(
                index_elements=[admission_counters.c.scope_type, admission_counters.c.scope_id]
            )
        )
        row = (
            session.execute(
                select(admission_counters)
                .where(
                    admission_counters.c.scope_type == scope_type,
                    admission_counters.c.scope_id == scope_id,
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )
        return dict(row)

    @staticmethod
    def _rate_lock(
        session: Session,
        *,
        scope_type: str,
        scope_id: str,
        capacity: Decimal,
        refill_rate: Decimal,
        now: datetime,
    ) -> Decimal:
        session.execute(
            pg_insert(rate_buckets)
            .values(
                scope_type=scope_type,
                scope_id=scope_id,
                tokens=capacity,
                capacity=capacity,
                refill_rate=refill_rate,
                last_refill_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[rate_buckets.c.scope_type, rate_buckets.c.scope_id]
            )
        )
        row = (
            session.execute(
                select(rate_buckets)
                .where(
                    rate_buckets.c.scope_type == scope_type,
                    rate_buckets.c.scope_id == scope_id,
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )
        elapsed = max(Decimal("0"), Decimal(str((now - row["last_refill_at"]).total_seconds())))
        tokens = row["tokens"] + elapsed * refill_rate
        tokens = max(Decimal("0"), min(capacity, tokens))
        if tokens < Decimal("1"):
            wait = max(
                1,
                int(
                    ((Decimal("1") - tokens) / refill_rate).to_integral_value(
                        rounding="ROUND_CEILING"
                    )
                ),
            )
            raise ApplicationError(
                code="rate_limited",
                status=429,
                message="Submit rate limit exceeded",
                retry_after=wait,
            )
        session.execute(
            update(rate_buckets)
            .where(
                rate_buckets.c.scope_type == scope_type,
                rate_buckets.c.scope_id == scope_id,
            )
            .values(
                tokens=tokens - Decimal("1"),
                capacity=capacity,
                refill_rate=refill_rate,
                last_refill_at=now,
                updated_at=now,
                version=rate_buckets.c.version + 1,
            )
        )
        return tokens - Decimal("1")

    @staticmethod
    def _job_columns():
        return [
            jobs.c.job_id.label("job_id"),
            jobs.c.tenant_id.label("tenant_id"),
            jobs.c.submitter_user_id.label("user_id"),
            jobs.c.state.label("state"),
            jobs.c.desired_state.label("desired_state"),
            jobs.c.waiting_reason.label("waiting_reason"),
            jobs.c.version.label("version"),
            jobs.c.job_fence.label("job_fence"),
            jobs.c.retry_count.label("retry_count"),
            jobs.c.max_retries.label("max_retries"),
            jobs.c.retry_of_job_id.label("retry_of_job_id"),
            jobs.c.event_sequence.label("event_sequence"),
            jobs.c.created_at.label("created_at"),
            jobs.c.updated_at.label("updated_at"),
            job_specs.c.canonical_spec.label("canonical_spec"),
            job_specs.c.spec_checksum.label("spec_checksum"),
            logical_sessions.c.session_id.label("session_id"),
        ]

    @classmethod
    def _job_view(cls, row: Any) -> dict[str, Any]:
        return json_wire_value(
            {
                "job_id": row["job_id"],
                "tenant_id": row["tenant_id"],
                "user_id": row["user_id"],
                "session_id": row["session_id"],
                "state": row["state"],
                "desired_state": row["desired_state"],
                "waiting_reason": row["waiting_reason"],
                "spec": row["canonical_spec"],
                "spec_checksum": row["spec_checksum"],
                "job_fence": row["job_fence"],
                "retry_count": row["retry_count"],
                "max_retries": row["max_retries"],
                "retry_of_job_id": row["retry_of_job_id"],
                "version": row["version"],
                "event_sequence": row["event_sequence"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    @staticmethod
    def _job_query(tenant_id: UUID | None = None):
        statement = select(*JobService._job_columns()).select_from(
            jobs.join(job_specs, job_specs.c.job_id == jobs.c.job_id).join(
                logical_sessions, logical_sessions.c.job_id == jobs.c.job_id
            )
        )
        if tenant_id is not None:
            statement = statement.where(jobs.c.tenant_id == tenant_id)
        return statement

    @staticmethod
    def _validate_parameter_schema(template: dict[str, Any], parameters: dict[str, Any]) -> None:
        declarations = template.get("parameter_schema")
        if declarations is None:
            declarations = []
        if not isinstance(declarations, list) or any(
            not isinstance(declaration, dict) or not isinstance(declaration.get("name"), str)
            for declaration in declarations
        ):
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="The template parameter schema is unavailable",
            )
        definitions = {declaration["name"]: declaration for declaration in declarations}
        if len(definitions) != len(declarations) or set(parameters) - set(definitions):
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="Job parameters do not match the selected template",
            )
        for name, declaration in definitions.items():
            if name not in parameters:
                if declaration.get("required", True):
                    raise ApplicationError(
                        code="infeasible_request",
                        status=422,
                        message="A required template parameter is missing",
                    )
                continue
            value = parameters[name]
            declared_type = declaration.get("type")
            valid_type = {
                "INTEGER": type(value) is int,
                "NUMBER": type(value) in {int, float} and not isinstance(value, bool),
                "BOOLEAN": type(value) is bool,
                "STRING": isinstance(value, str),
                "ENUM": isinstance(value, str),
            }.get(declared_type, False)
            if not valid_type:
                raise ApplicationError(
                    code="infeasible_request",
                    status=422,
                    message="A template parameter has the wrong type",
                )
            minimum = declaration.get("minimum")
            maximum = declaration.get("maximum")
            if (minimum is not None and value < minimum) or (
                maximum is not None and value > maximum
            ):
                raise ApplicationError(
                    code="infeasible_request",
                    status=422,
                    message="A template parameter is outside its allowed bounds",
                )
            enum_values = declaration.get("enum_values")
            if enum_values is not None and value not in enum_values:
                raise ApplicationError(
                    code="infeasible_request",
                    status=422,
                    message="A template parameter is not an allowed value",
                )

    @staticmethod
    def _integer_bound(declared: Any) -> tuple[int | None, int]:
        minimum: Any = None
        maximum: Any = None
        if type(declared) is int:
            maximum = declared
        elif isinstance(declared, dict):
            minimum_keys = [key for key in ("minimum", "min") if key in declared]
            maximum_keys = [key for key in ("maximum", "max") if key in declared]
            if len(minimum_keys) > 1 or len(maximum_keys) != 1:
                raise ApplicationError(
                    code="infeasible_request",
                    status=422,
                    message="The template resource bounds are unavailable",
                )
            if minimum_keys:
                minimum = declared[minimum_keys[0]]
            maximum = declared[maximum_keys[0]]
        if (
            type(maximum) is not int
            or maximum < 0
            or (minimum is not None and (type(minimum) is not int or minimum < 0))
            or (minimum is not None and minimum > maximum)
        ):
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="The template resource bounds are unavailable",
            )
        return minimum, maximum

    @classmethod
    def _validate_resource_bounds(cls, template: dict[str, Any], spec: Any) -> None:
        declared_bounds = template.get("resource_bounds")
        if not isinstance(declared_bounds, dict):
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="The template resource bounds are unavailable",
            )
        resource_bounds = declared_bounds.get("resources")
        if not isinstance(resource_bounds, dict):
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="The template resource bounds are unavailable",
            )
        requested = spec.resources.model_dump()
        for key in ("cpu_millis", "memory_bytes", "gpu_count"):
            if key not in resource_bounds:
                raise ApplicationError(
                    code="infeasible_request",
                    status=422,
                    message="The template resource bounds are unavailable",
                )
            minimum, maximum = cls._integer_bound(resource_bounds[key])
            if (minimum is not None and requested[key] < minimum) or requested[key] > maximum:
                raise ApplicationError(
                    code="infeasible_request",
                    status=422,
                    message="Requested resources exceed the template bounds",
                )
        for key in ("runtime_limit_seconds", "checkpoint_interval_seconds"):
            if key not in declared_bounds:
                raise ApplicationError(
                    code="infeasible_request",
                    status=422,
                    message="The template resource bounds are unavailable",
                )
            minimum, maximum = cls._integer_bound(declared_bounds[key])
            value = getattr(spec, key)
            if (minimum is not None and value < minimum) or value > maximum:
                raise ApplicationError(
                    code="infeasible_request",
                    status=422,
                    message="Runtime limits exceed the template bounds",
                )

    @staticmethod
    def _validate_template_and_artifacts(
        session: Session, tenant_id: UUID, spec: Any
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        template = (
            session.execute(
                select(templates, template_versions)
                .join(
                    template_versions,
                    and_(
                        template_versions.c.template_id == templates.c.template_id,
                        template_versions.c.version == spec.template_version,
                    ),
                )
                .where(
                    templates.c.template_id == spec.template_id,
                    templates.c.enabled.is_(True),
                )
            )
            .mappings()
            .one_or_none()
        )
        if template is None:
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="The requested template is unavailable",
            )
        JobService._validate_parameter_schema(
            dict(template), spec.parameters.model_dump(mode="json")
        )
        artifact_ids = [spec.input_artifact_id]
        if getattr(spec, "model_artifact_id", None) is not None:
            artifact_ids.append(spec.model_artifact_id)
        artifact_rows = (
            session.execute(
                select(artifacts)
                .where(
                    artifacts.c.tenant_id == tenant_id,
                    artifacts.c.artifact_id.in_(artifact_ids),
                    artifacts.c.state == "COMMITTED",
                )
                .with_for_update(read=True)
            )
            .mappings()
            .all()
        )
        artifacts_by_id = {row["artifact_id"]: dict(row) for row in artifact_rows}
        if len(artifacts_by_id) != len(set(artifact_ids)):
            raise ApplicationError(
                code="resource_not_found",
                status=404,
                message="The referenced artifact was not found",
            )
        input_artifact = artifacts_by_id[spec.input_artifact_id]
        requirements = _TEMPLATE_ARTIFACT_REQUIREMENTS.get(
            (spec.template_id, spec.template_version)
        )
        if requirements is None:
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="The template artifact compatibility rules are unavailable",
            )
        input_kind, input_media_type, model_kind, model_media_type = requirements
        model_artifact = artifacts_by_id.get(getattr(spec, "model_artifact_id", None))
        if (
            input_artifact["kind"] != input_kind
            or input_artifact["media_type"] != input_media_type
            or (
                model_kind is not None
                and (
                    model_artifact is None
                    or model_artifact["kind"] != model_kind
                    or model_artifact["media_type"] != model_media_type
                )
            )
            or (spec.template_id == "cpu-iterative" and spec.resources.gpu_count != 0)
        ):
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="The input or model artifact is incompatible with the template",
            )
        JobService._validate_resource_bounds(dict(template), spec)
        capability_requirements = template.get("capability_requirements") or {}
        if not isinstance(capability_requirements, dict):
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="The template capability requirements are unavailable",
            )
        device = capability_requirements.get("device")
        if (device == "CPU" and spec.resources.gpu_count != 0) or (
            device == "CUDA" and spec.resources.gpu_count != 1
        ):
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="Requested resources are incompatible with the template capability",
            )
        return (
            dict(template),
            input_artifact,
            (
                artifacts_by_id.get(spec.model_artifact_id)
                if getattr(spec, "model_artifact_id", None) is not None
                else None
            ),
        )

    @staticmethod
    def _version_parts(value: Any) -> tuple[int, ...] | None:
        if not isinstance(value, str) or not value:
            return None
        core = value.split("+", 1)[0].split("-", 1)[0]
        parts = core.split(".")
        if not parts or any(not part.isdigit() for part in parts):
            return None
        return tuple(int(part) for part in parts)

    @classmethod
    def _version_at_least(cls, actual: Any, minimum: Any) -> bool:
        if minimum is None:
            return True
        actual_parts = cls._version_parts(actual)
        minimum_parts = cls._version_parts(minimum)
        if actual_parts is None or minimum_parts is None:
            return False
        width = max(len(actual_parts), len(minimum_parts))
        return actual_parts + (0,) * (width - len(actual_parts)) >= minimum_parts + (0,) * (
            width - len(minimum_parts)
        )

    @classmethod
    def _inventory_supports(cls, row: dict[str, Any], requirements: dict[str, Any]) -> bool:
        if not requirements:
            return True
        architectures = requirements.get("architectures")
        if architectures is not None and row.get("architecture") not in architectures:
            return False
        capabilities = row.get("workload_capabilities") or {}
        if not isinstance(capabilities, dict):
            return False

        adapters = capabilities.get("adapters")
        adapter_id = requirements.get("adapter_id")
        adapter_version = requirements.get("adapter_version")
        if (adapter_id is not None or adapter_version is not None) and (
            not isinstance(adapters, list)
            or not any(
                isinstance(entry, dict)
                and entry.get("adapter_id") == adapter_id
                and entry.get("adapter_version") == adapter_version
                for entry in adapters
            )
        ):
            return False

        images = capabilities.get("images")
        image_digest = requirements.get("image_digest")
        if image_digest is not None and (
            not isinstance(images, list)
            or not any(
                isinstance(entry, dict)
                and entry.get("image_digest") == image_digest
                and entry.get("verified") is True
                and entry.get("architecture") == row.get("architecture")
                for entry in images
            )
        ):
            return False

        frameworks = capabilities.get("frameworks")
        framework = requirements.get("framework")
        framework_version = requirements.get("framework_version")
        device = requirements.get("device")
        matching_frameworks = [
            entry
            for entry in (frameworks if isinstance(frameworks, list) else [])
            if isinstance(entry, dict)
            and (framework is None or entry.get("framework") == framework)
            and (framework_version is None or entry.get("framework_version") == framework_version)
            and (device is None or entry.get("device") == device)
            and cls._version_at_least(
                entry.get("cuda_runtime_version"), requirements.get("cuda_runtime_min")
            )
        ]
        if (
            framework is not None
            or framework_version is not None
            or device is not None
            or requirements.get("cuda_runtime_min") is not None
        ) and not matching_frameworks:
            return False

        if device == "CUDA":
            gpu_devices = row.get("gpu_devices")
            if not isinstance(gpu_devices, list):
                gpu_devices = capabilities.get("gpu_devices")
            if not isinstance(gpu_devices, list) or not any(
                isinstance(entry, dict)
                and entry.get("healthy") is True
                and cls._version_at_least(
                    entry.get("driver_version"), requirements.get("driver_min")
                )
                and cls._version_at_least(
                    entry.get("compute_capability"), requirements.get("compute_capability_min")
                )
                for entry in gpu_devices
            ):
                return False
        return True

    @staticmethod
    def _inventory_requirements(template: dict[str, Any]) -> dict[str, Any]:
        capability_requirements = template.get("capability_requirements") or {}
        if not isinstance(capability_requirements, dict):
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="The template capability requirements are unavailable",
            )
        required = {
            "architectures",
            "adapter_id",
            "adapter_version",
            "image_digest",
            "device",
            "framework",
            "framework_version",
            "cuda_runtime_min",
            "driver_min",
            "compute_capability_min",
        }
        if set(capability_requirements) != required:
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="The template capability requirements are invalid",
            )
        architectures = capability_requirements["architectures"]
        string_fields = (
            "adapter_id",
            "adapter_version",
            "image_digest",
            "framework_version",
        )
        nullable_string_fields = ("cuda_runtime_min", "driver_min", "compute_capability_min")
        if (
            not isinstance(architectures, list)
            or not architectures
            or any(
                not isinstance(architecture, str)
                or architecture not in {"linux/amd64", "linux/arm64"}
                for architecture in architectures
            )
            or len(architectures) != len(set(architectures))
            or any(
                not isinstance(capability_requirements[field], str)
                or not capability_requirements[field]
                for field in string_fields
            )
            or capability_requirements["device"] not in {"CPU", "CUDA"}
            or capability_requirements["framework"] not in {"PYTORCH", "NEXA_CPU"}
            or any(
                value is not None and (not isinstance(value, str) or not value)
                for value in (capability_requirements[field] for field in nullable_string_fields)
            )
        ):
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="The template capability requirements are invalid",
            )
        requirements = dict(capability_requirements)
        for key in ("adapter_id", "adapter_version", "image_digest"):
            if key in requirements and requirements[key] != template.get(key):
                raise ApplicationError(
                    code="infeasible_request",
                    status=422,
                    message="The template capability binding is inconsistent",
                )
            if key not in requirements:
                requirements[key] = template.get(key)
        return {key: value for key, value in requirements.items() if value is not None}

    @staticmethod
    def _worker_is_ready(row: dict[str, Any], now: datetime) -> bool:
        heartbeat = row.get("last_heartbeat_at")
        return (
            row.get("health") == "READY"
            and row.get("admin_state") == "ENABLED"
            and isinstance(heartbeat, datetime)
            and heartbeat.tzinfo is not None
            and heartbeat.utcoffset() is not None
            and heartbeat >= now - timedelta(seconds=30)
        )

    @classmethod
    def _waiting_reason(
        cls, session: Session, spec: Any, requirements: dict[str, Any] | None = None
    ) -> str | None:
        now = transaction_timestamp(session)
        rows = (
            session.execute(
                select(
                    workers.c.worker_id,
                    workers.c.health,
                    workers.c.admin_state,
                    workers.c.last_heartbeat_at,
                    worker_inventories.c.inventory_version,
                    worker_inventories.c.architecture,
                    worker_inventories.c.allocatable_cpu_millis,
                    worker_inventories.c.allocatable_memory_bytes,
                    worker_inventories.c.allocatable_gpu_count,
                    worker_inventories.c.workload_capabilities,
                )
                .join(
                    worker_inventories,
                    and_(
                        worker_inventories.c.worker_id == workers.c.worker_id,
                        worker_inventories.c.worker_incarnation_id
                        == workers.c.current_incarnation_id,
                        worker_inventories.c.inventory_version
                        == workers.c.current_inventory_version,
                    ),
                )
                .where(workers.c.admin_state != "DISABLED")
            )
            .mappings()
            .all()
        )
        rows = [dict(row) for row in rows]
        if requirements and rows:
            for row in rows:
                row["gpu_devices"] = [
                    dict(gpu)
                    for gpu in session.execute(
                        select(gpu_devices).where(
                            gpu_devices.c.worker_id == row["worker_id"],
                            gpu_devices.c.inventory_version == row["inventory_version"],
                        )
                    ).mappings()
                ]
        requested = spec.resources
        if not rows:
            if requested.gpu_count:
                raise ApplicationError(
                    code="infeasible_request",
                    status=422,
                    message="No configured GPU can satisfy the request",
                )
            return "waiting_for_worker"
        capable = [
            row
            for row in rows
            if row["allocatable_cpu_millis"] >= requested.cpu_millis
            and row["allocatable_memory_bytes"] >= requested.memory_bytes
            and row["allocatable_gpu_count"] >= requested.gpu_count
            and cls._inventory_supports(row, requirements or {})
        ]
        if not capable:
            raise ApplicationError(
                code="infeasible_request",
                status=422,
                message="No configured worker can satisfy the request",
            )
        if any(cls._worker_is_ready(row, now) for row in capable):
            return None
        return "waiting_for_worker"

    def submit(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        request: JobSubmitRequest,
        idempotency_key: str,
        request_hash: str,
    ) -> JobOperationResult:
        spec = request.spec
        spec_payload = spec.model_dump(mode="json")
        spec_checksum = jcs_request_hash(spec_payload)
        job_id = new_uuid7()
        session_id = new_uuid7()

        def operation(session: Session) -> JobOperationResult:
            live = self._authorize(session, principal, tenant_id, write=True)
            now = transaction_timestamp(session)
            outcome = begin_idempotency(
                session,
                context=str(tenant_id),
                principal_id=str(live.user_id),
                operation_id="submitJob",
                key=idempotency_key,
                request_hash=request_hash,
                expires_at=now + timedelta(days=self.settings.idempotency_terminal_retention_days),
                pending_wait_milliseconds=self.settings.idempotency_pending_wait_milliseconds,
            )
            if outcome.replay is not None:
                return JobOperationResult(
                    status=outcome.replay.status,
                    body=dict(outcome.replay.body or {}),
                    headers=dict(outcome.replay.headers),
                )

            global_policy = (
                session.execute(
                    select(policy_versions)
                    .where(policy_versions.c.is_current.is_(True))
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if global_policy["operational_mode"] in {"ADMISSION_OFF", "WRITE_FROZEN"}:
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="New job submissions are disabled in the current operational mode",
                )
            tenant_policy = (
                session.execute(
                    select(tenant_policies)
                    .where(
                        tenant_policies.c.tenant_id == tenant_id,
                        tenant_policies.c.is_current.is_(True),
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if tenant_policy is None:
                raise ApplicationError(
                    code="dependency_unavailable",
                    status=503,
                    message="Tenant admission policy is unavailable",
                    retry_after=1,
                )
            global_counter = self._counter_lock(session, "GLOBAL", "global")
            tenant_counter = self._counter_lock(session, "TENANT", str(tenant_id))
            user_scope_id = f"{tenant_id}:{live.user_id}"
            user_counter = self._counter_lock(session, "USER", user_scope_id)
            if global_counter["outstanding"] >= global_policy["global_outstanding_limit"]:
                raise ApplicationError(
                    code="queue_full",
                    status=503,
                    message="The durable queue is full",
                    retry_after=1,
                )
            if tenant_counter["outstanding"] >= tenant_policy["outstanding_limit"]:
                raise ApplicationError(
                    code="quota_exceeded",
                    status=429,
                    message="Tenant outstanding quota exceeded",
                    retry_after=1,
                )
            if user_counter["outstanding"] >= tenant_policy["user_outstanding_limit"]:
                raise ApplicationError(
                    code="quota_exceeded",
                    status=429,
                    message="User outstanding quota exceeded",
                    retry_after=1,
                )
            self._rate_lock(
                session,
                scope_type="TENANT",
                scope_id=str(tenant_id),
                capacity=Decimal(tenant_policy["tenant_rate_burst"]),
                refill_rate=Decimal(tenant_policy["tenant_rate_per_second"]),
                now=clock_timestamp(session),
            )
            self._rate_lock(
                session,
                scope_type="USER",
                scope_id=user_scope_id,
                capacity=Decimal(tenant_policy["user_rate_burst"]),
                refill_rate=Decimal(tenant_policy["user_rate_per_second"]),
                now=clock_timestamp(session),
            )

            template, _input_artifact, _model_artifact = self._validate_template_and_artifacts(
                session, tenant_id, spec
            )
            resource_limit = {
                "cpu_millis": tenant_policy["cpu_limit_millis"],
                "memory_bytes": tenant_policy["memory_limit_bytes"],
                "gpu_count": tenant_policy["gpu_limit"],
            }
            requested = spec.resources.model_dump()
            if any(requested[key] > resource_limit[key] for key in resource_limit):
                raise ApplicationError(
                    code="infeasible_request",
                    status=422,
                    message="Requested resources exceed the tenant limit",
                )
            waiting_reason = self._waiting_reason(
                session, spec, self._inventory_requirements(template)
            )

            ready_sequence = int(global_counter["version"])
            session.execute(
                insert(jobs).values(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    submitter_user_id=live.user_id,
                    state="QUEUED",
                    desired_state="RUNNING",
                    waiting_reason=waiting_reason,
                    version=1,
                    job_fence=0,
                    event_sequence=1,
                    checkpoint_sequence=0,
                    retry_count=0,
                    max_retries=2,
                    retry_of_job_id=None,
                    base_priority=spec.priority,
                    ready_sequence=ready_sequence,
                    eligible_since=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.execute(
                insert(logical_sessions).values(
                    session_id=session_id, tenant_id=tenant_id, job_id=job_id, created_at=now
                )
            )
            session.execute(
                insert(job_specs).values(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    canonical_spec=spec_payload,
                    spec_checksum=spec_checksum,
                    template_id=spec.template_id,
                    template_version=spec.template_version,
                    input_artifact_id=spec.input_artifact_id,
                    model_artifact_id=getattr(spec, "model_artifact_id", None),
                    cpu_millis=spec.resources.cpu_millis,
                    memory_bytes=spec.resources.memory_bytes,
                    gpu_count=spec.resources.gpu_count,
                    runtime_limit_seconds=spec.runtime_limit_seconds,
                    checkpoint_interval_seconds=spec.checkpoint_interval_seconds,
                    created_at=now,
                )
            )
            references = [
                {
                    "tenant_id": tenant_id,
                    "artifact_id": _input_artifact["artifact_id"],
                    "owner_type": "JOB_SPEC",
                    "owner_id": job_id,
                    "purpose": "INPUT",
                    "logical_name": "input.data",
                }
            ]
            if _model_artifact is not None:
                references.append(
                    {
                        "tenant_id": tenant_id,
                        "artifact_id": _model_artifact["artifact_id"],
                        "owner_type": "JOB_SPEC",
                        "owner_id": job_id,
                        "purpose": "MODEL",
                        "logical_name": "model.data",
                    }
                )
            session.execute(insert(artifact_references), references)
            event_id = new_uuid7()
            session.execute(
                insert(events).values(
                    event_id=event_id,
                    tenant_id=tenant_id,
                    job_id=job_id,
                    sequence=1,
                    event_type="JOB_ACCEPTED",
                    reason="Job accepted",
                    actor_type="USER",
                    actor_id=str(live.user_id),
                    safe_metadata={},
                    created_at=now,
                )
            )
            session.execute(
                update(admission_counters)
                .where(
                    or_(
                        and_(
                            admission_counters.c.scope_type == "GLOBAL",
                            admission_counters.c.scope_id == "global",
                        ),
                        and_(
                            admission_counters.c.scope_type == "TENANT",
                            admission_counters.c.scope_id == str(tenant_id),
                        ),
                        and_(
                            admission_counters.c.scope_type == "USER",
                            admission_counters.c.scope_id == user_scope_id,
                        ),
                    )
                )
                .values(
                    outstanding=admission_counters.c.outstanding + 1,
                    version=admission_counters.c.version + 1,
                    updated_at=now,
                )
            )
            session.execute(
                insert(audit_records).values(
                    audit_id=new_uuid7(),
                    actor_type="USER",
                    actor_id=str(live.user_id),
                    tenant_id=tenant_id,
                    action="job.submit",
                    target_type="JOB",
                    target_id=str(job_id),
                    before_version=None,
                    after_version=1,
                    reason="Job accepted",
                    safe_metadata={"template_id": spec.template_id},
                    created_at=now,
                )
            )
            row = (
                session.execute(self._job_query(tenant_id).where(jobs.c.job_id == job_id))
                .mappings()
                .one()
            )
            body = self._job_view(row)
            headers = {"Location": f"/v1/jobs/{job_id}", "ETag": '"v1"'}
            complete_idempotency(
                session,
                outcome.record_id,
                status=202,
                body=body,
                headers=headers,
                resource_id=job_id,
            )
            return JobOperationResult(status=202, body=body, headers=headers)

        return run_transaction(self.session_factory, operation)

    def _cursor(self, now: datetime) -> CursorCodec:
        return CursorCodec(
            self._cursor_key, ttl_seconds=self.settings.cursor_ttl_seconds, now=lambda: now
        )

    def _decode_cursor(
        self, cursor: str, *, binding: dict[str, str], now: datetime
    ) -> tuple[datetime, UUID]:
        try:
            position = self._cursor(now).decode(cursor, expected_binding=binding)
            if set(position) != {"created_at", "id"}:
                raise ValueError("cursor position fields are invalid")
            created_at = datetime.fromisoformat(position["created_at"])
            job_id = UUID(position["id"])
            if created_at.tzinfo is None or created_at.utcoffset() is None or job_id.version != 7:
                raise ValueError("cursor position values are invalid")
            return created_at.astimezone(UTC), job_id
        except (CursorError, KeyError, ValueError):
            raise ApplicationError(
                code="invalid_cursor", status=400, message="The pagination cursor is invalid"
            ) from None

    def list_jobs(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        page_size: int,
        cursor: str | None,
        state: str | None,
        template_id: str | None,
        created_after: datetime | None,
    ) -> dict[str, Any]:
        if not 1 <= page_size <= 100:
            raise ApplicationError(
                code="validation_failed", status=422, message="Page size must be between 1 and 100"
            )
        if created_after is not None and (
            created_after.tzinfo is None or created_after.utcoffset() is None
        ):
            raise ApplicationError(
                code="validation_failed",
                status=422,
                message="created_after must include a timezone",
            )

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, tenant_id, write=False)
            now = transaction_timestamp(session)
            binding = {
                "actor_id": str(live.user_id),
                "tenant_id": str(tenant_id),
                "operation_id": "listJobs",
                "state": state or "",
                "template_id": template_id or "",
                "created_after": created_after.isoformat() if created_after else "",
            }
            statement = self._job_query(tenant_id)
            if state:
                statement = statement.where(jobs.c.state == state)
            if template_id:
                statement = statement.where(job_specs.c.template_id == template_id)
            if created_after:
                statement = statement.where(jobs.c.created_at >= created_after)
            if cursor:
                created_at, last_id = self._decode_cursor(cursor, binding=binding, now=now)
                statement = statement.where(
                    or_(
                        jobs.c.created_at < created_at,
                        and_(jobs.c.created_at == created_at, jobs.c.job_id < last_id),
                    )
                )
            rows = (
                session.execute(
                    statement.order_by(jobs.c.created_at.desc(), jobs.c.job_id.desc()).limit(
                        page_size + 1
                    )
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            next_cursor = None
            if len(rows) > page_size and visible:
                last = visible[-1]
                next_cursor = self._cursor(now).encode(
                    binding=binding,
                    position={
                        "created_at": last["created_at"].isoformat(),
                        "id": str(last["job_id"]),
                    },
                )
            return json_wire_value(
                {
                    "items": [self._job_view(row) for row in visible],
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    def get_job(self, principal: Principal, *, tenant_id: UUID, job_id: UUID) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            self._authorize(session, principal, tenant_id, write=False)
            row = (
                session.execute(self._job_query(tenant_id).where(jobs.c.job_id == job_id))
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Job was not found"
                )
            return self._job_view(row)

        return run_transaction(self.session_factory, operation)

    def get_result(self, principal: Principal, *, tenant_id: UUID, job_id: UUID) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            self._authorize(session, principal, tenant_id, write=False)
            row = (
                session.execute(
                    select(results).where(
                        results.c.tenant_id == tenant_id,
                        results.c.job_id == job_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                # A reservation or upload without a recognized Result is intentionally invisible.
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Recognized result was not found"
                )
            return json_wire_value(
                {
                    "result_id": row["result_id"],
                    "job_id": row["job_id"],
                    "attempt_id": row["attempt_id"],
                    "manifest_artifact_id": row["manifest_artifact_id"],
                    "manifest_checksum": row["manifest_checksum"],
                    "created_at": row["created_at"],
                }
            )

        return run_transaction(self.session_factory, operation)

    def get_session(
        self, principal: Principal, *, tenant_id: UUID, session_id: UUID
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            self._authorize(session, principal, tenant_id, write=False)
            row = (
                session.execute(
                    select(
                        logical_sessions.c.session_id,
                        logical_sessions.c.job_id,
                        logical_sessions.c.tenant_id,
                        logical_sessions.c.created_at,
                        jobs.c.state,
                    )
                    .join(jobs, jobs.c.job_id == logical_sessions.c.job_id)
                    .where(
                        logical_sessions.c.tenant_id == tenant_id,
                        logical_sessions.c.session_id == session_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Session was not found"
                )
            return json_wire_value(
                {
                    "session_id": row["session_id"],
                    "job_id": row["job_id"],
                    "tenant_id": row["tenant_id"],
                    "derived_state": row["state"],
                    "created_at": row["created_at"],
                }
            )

        return run_transaction(self.session_factory, operation)

    def list_events(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        job_id: UUID,
        after_sequence: int,
        page_size: int,
    ) -> dict[str, Any]:
        if after_sequence < 0 or not 1 <= page_size <= 100:
            raise ApplicationError(
                code="validation_failed", status=422, message="Event pagination is invalid"
            )

        def operation(session: Session) -> dict[str, Any]:
            self._authorize(session, principal, tenant_id, write=False)
            exists = session.execute(
                select(jobs.c.job_id).where(jobs.c.tenant_id == tenant_id, jobs.c.job_id == job_id)
            ).scalar_one_or_none()
            if exists is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Job was not found"
                )
            rows = (
                session.execute(
                    select(events)
                    .where(
                        events.c.tenant_id == tenant_id,
                        events.c.job_id == job_id,
                        events.c.sequence > after_sequence,
                    )
                    .order_by(events.c.sequence.asc(), events.c.event_id.asc())
                    .limit(page_size + 1)
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            return json_wire_value(
                {
                    "items": [
                        {
                            "event_id": row["event_id"],
                            "tenant_id": row["tenant_id"],
                            "job_id": row["job_id"],
                            "sequence": row["sequence"],
                            "type": row["event_type"],
                            "reason": row["reason"] or "Event",
                            "actor_type": row["actor_type"],
                            "created_at": row["created_at"],
                        }
                        for row in visible
                    ],
                    "page": {"next_cursor": None, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    def list_checkpoints(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        job_id: UUID,
        cursor: str | None,
        page_size: int,
    ) -> dict[str, Any]:
        if not 1 <= page_size <= 100:
            raise ApplicationError(
                code="validation_failed", status=422, message="Page size must be between 1 and 100"
            )

        def operation(session: Session) -> dict[str, Any]:
            live = self._authorize(session, principal, tenant_id, write=False)
            now = transaction_timestamp(session)
            exists = session.execute(
                select(jobs.c.job_id).where(jobs.c.tenant_id == tenant_id, jobs.c.job_id == job_id)
            ).scalar_one_or_none()
            if exists is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Job was not found"
                )
            binding = {
                "actor_id": str(live.user_id),
                "tenant_id": str(tenant_id),
                "operation_id": "listJobCheckpoints",
                "job_id": str(job_id),
            }
            # Only published rows exist in checkpoints; reservations, staging and
            # orphan uploads live elsewhere and are never joined here.
            statement = (
                select(
                    checkpoints.c.checkpoint_id,
                    checkpoints.c.job_id,
                    checkpoints.c.attempt_id,
                    checkpoints.c.sequence,
                    checkpoints.c.manifest_artifact_id,
                    checkpoints.c.manifest_checksum,
                    checkpoints.c.created_at,
                    checkpoint_corruptions.c.checkpoint_id.is_not(None).label("corrupt"),
                )
                .outerjoin(
                    checkpoint_corruptions,
                    and_(
                        checkpoint_corruptions.c.tenant_id == checkpoints.c.tenant_id,
                        checkpoint_corruptions.c.checkpoint_id == checkpoints.c.checkpoint_id,
                    ),
                )
                .where(checkpoints.c.tenant_id == tenant_id, checkpoints.c.job_id == job_id)
            )
            if cursor:
                before = self._decode_sequence_cursor(cursor, binding=binding, now=now)
                statement = statement.where(checkpoints.c.sequence < before)
            rows = (
                session.execute(
                    statement.order_by(checkpoints.c.sequence.desc()).limit(page_size + 1)
                )
                .mappings()
                .all()
            )
            visible = rows[:page_size]
            next_cursor = None
            if len(rows) > page_size and visible:
                next_cursor = self._cursor(now).encode(
                    binding=binding, position={"sequence": str(visible[-1]["sequence"])}
                )
            return json_wire_value(
                {
                    "items": [
                        {
                            "checkpoint_id": row["checkpoint_id"],
                            "job_id": row["job_id"],
                            "attempt_id": row["attempt_id"],
                            "sequence": row["sequence"],
                            "manifest_artifact_id": row["manifest_artifact_id"],
                            "manifest_checksum": row["manifest_checksum"],
                            "state": "CORRUPT" if row["corrupt"] else "COMMITTED",
                            "created_at": row["created_at"],
                        }
                        for row in visible
                    ],
                    "page": {"next_cursor": next_cursor, "page_size": page_size},
                }
            )

        return run_transaction(self.session_factory, operation)

    def _decode_sequence_cursor(
        self, cursor: str, *, binding: dict[str, str], now: datetime
    ) -> int:
        try:
            position = self._cursor(now).decode(cursor, expected_binding=binding)
            text = position["sequence"]
            if set(position) != {"sequence"} or not text.isascii() or not text.isdigit():
                raise ValueError("cursor position is invalid")
            sequence = int(text)
            if sequence < 2:
                raise ValueError("cursor position is invalid")
            return sequence
        except (CursorError, KeyError, ValueError, TypeError):
            raise ApplicationError(
                code="invalid_cursor", status=400, message="The pagination cursor is invalid"
            ) from None


__all__ = ["JobOperationResult", "JobService"]
