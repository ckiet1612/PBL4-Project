"""B16 template catalog: versioned definition files, local registration and tenant reads.

The contract has no REST admin surface for templates, so versions are registered by the
local maintenance CLI. A (template_id, version) row is never rewritten: the same content is
an idempotent no-op and different content is refused. The image digest is supplied at
registration because it comes from the image build output, not from the repository file.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from nexa.application.errors import ApplicationError
from nexa.application.identity_service import IdentityService
from nexa.application.json_codec import JsonRequestError, decode_json_object, jcs_request_hash
from nexa.domain import workload_adapters
from nexa.domain.identity import (
    AuthorizationError,
    Principal,
    TokenScope,
    require_tenant_membership,
)
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import transaction_timestamp
from nexa.infrastructure.persistence.schema import (
    audit_records,
    policy_versions,
    template_versions,
    templates,
)
from nexa.infrastructure.persistence.transactions import run_transaction

MAX_DEFINITION_BYTES = 64 * 1024
_TEMPLATE_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_PARAMETER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARCHITECTURES = frozenset({"linux/amd64", "linux/arm64"})
_DEFINITION_KEYS = frozenset(
    {
        "schema_version",
        "template_id",
        "version",
        "display_name",
        "description",
        "adapter_id",
        "adapter_version",
        "checkpointable",
        "restart_safe",
        "allowed_devices",
        "artifact_requirements",
        "parameter_schema",
        "resource_bounds",
        "capability_requirement",
    }
)
# The registered digest completes the 10-key WorkloadCapabilityRequirement.
_CAPABILITY_KEYS = frozenset(
    {
        "architectures",
        "adapter_id",
        "adapter_version",
        "device",
        "framework",
        "framework_version",
        "cuda_runtime_min",
        "driver_min",
        "compute_capability_min",
    }
)
_PARAMETER_KEYS = frozenset(
    {"name", "type", "required", "minimum", "maximum", "default", "enum_values", "unit"}
)
_PARAMETER_TYPES = frozenset({"INTEGER", "NUMBER", "BOOLEAN", "STRING", "ENUM"})
_MAX_TEMPLATES = 100


class TemplateDefinitionError(ValueError):
    """The definition file is not a valid B16 template version."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TemplateDefinitionError(message)


def _plain(value: Any) -> Any:
    # JSONB stores binary64 numbers; Decimal never reaches the database (B13-R12).
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _integer(value: Any) -> bool:
    return type(value) is int


def _bound(value: Any, *, upper: int | None = None) -> tuple[int | None, int]:
    if _integer(value):
        minimum, maximum = None, value
    else:
        _require(
            isinstance(value, dict) and set(value) in ({"maximum"}, {"minimum", "maximum"}),
            "resource bound must be an integer or {minimum?, maximum}",
        )
        minimum, maximum = value.get("minimum"), value["maximum"]
        _require(minimum is None or _integer(minimum), "resource bound minimum is invalid")
    _require(_integer(maximum) and maximum >= 0, "resource bound maximum is invalid")
    _require(minimum is None or 0 <= minimum <= maximum, "resource bound minimum is invalid")
    _require(upper is None or maximum <= upper, "resource bound exceeds the contract range")
    return minimum, maximum


def _validate_parameters(declarations: Any) -> None:
    _require(isinstance(declarations, list) and 1 <= len(declarations) <= 64, "parameter_schema")
    names = set()
    for item in declarations:
        _require(isinstance(item, dict) and set(item) <= _PARAMETER_KEYS, "parameter definition")
        _require(
            {"name", "type", "required", "minimum", "maximum", "default"} <= set(item),
            "parameter definition lacks a required member",
        )
        name = item["name"]
        _require(isinstance(name, str) and bool(_PARAMETER_NAME.fullmatch(name)), "parameter")
        _require(name not in names, "parameter names must be unique")
        names.add(name)
        _require(item["type"] in _PARAMETER_TYPES, "parameter type is invalid")
        _require(type(item["required"]) is bool, "parameter required must be boolean")
        for key in ("minimum", "maximum"):
            _require(
                item[key] is None or (type(item[key]) in {int, float}), f"parameter {key} invalid"
            )
        enum_values = item.get("enum_values")
        _require(
            (item["type"] == "ENUM")
            == (isinstance(enum_values, list) and 1 <= len(enum_values) <= 64),
            "ENUM parameters and only ENUM parameters declare enum_values",
        )
        if enum_values is not None:
            _require(
                all(isinstance(v, str) and len(v) <= 64 for v in enum_values)
                and len(set(enum_values)) == len(enum_values),
                "enum_values are invalid",
            )
        unit = item.get("unit")
        _require(unit is None or (isinstance(unit, str) and len(unit) <= 32), "unit is invalid")


def validate_definition(value: dict[str, Any]) -> dict[str, Any]:
    """Validate one decoded definition file against the registry and the Template schema."""
    _require(set(value) == _DEFINITION_KEYS, "definition members do not match schema_version 1")
    _require(value["schema_version"] == 1, "schema_version must be 1")
    template_id = value["template_id"]
    _require(
        isinstance(template_id, str) and bool(_TEMPLATE_ID.fullmatch(template_id)),
        "template_id is invalid",
    )
    _require(_integer(value["version"]) and value["version"] >= 1, "version must be >= 1")
    display_name = value["display_name"]
    _require(
        isinstance(display_name, str) and 1 <= len(display_name) <= 100, "display_name is invalid"
    )
    _require(
        isinstance(value["description"], str) and len(value["description"]) <= 1000,
        "description is invalid",
    )
    adapter = workload_adapters.descriptor(value["adapter_id"], value["adapter_version"])
    _require(adapter is not None, "adapter is not registered")
    _require(adapter.template_id == template_id, "adapter does not own this template family")
    for key in ("checkpointable", "restart_safe"):
        _require(type(value[key]) is bool, f"{key} must be boolean")
    # B16 ships CPU-only templates; CUDA stays unavailable without a GPU provider.
    _require(value["allowed_devices"] == ["CPU"], "allowed_devices must be exactly [CPU]")
    _require(
        value["artifact_requirements"]
        == {
            "input": {"kind": adapter.input_kind, "media_type": adapter.input_media_type},
            "model": None
            if adapter.model_kind is None
            else {"kind": adapter.model_kind, "media_type": adapter.model_media_type},
        },
        "artifact_requirements must equal the adapter declaration",
    )
    _validate_parameters(value["parameter_schema"])
    bounds = value["resource_bounds"]
    _require(
        isinstance(bounds, dict)
        and set(bounds) == {"resources", "runtime_limit_seconds", "checkpoint_interval_seconds"},
        "resource_bounds members are invalid",
    )
    resources = bounds["resources"]
    _require(
        isinstance(resources, dict)
        and set(resources) == {"cpu_millis", "memory_bytes", "gpu_count"},
        "resource_bounds.resources members are invalid",
    )
    _bound(resources["cpu_millis"], upper=100_000_000)
    _bound(resources["memory_bytes"])
    _require(_bound(resources["gpu_count"]) == (None, 0), "CPU templates must bound gpu_count to 0")
    _bound(bounds["runtime_limit_seconds"], upper=300)
    _bound(bounds["checkpoint_interval_seconds"], upper=60)
    capability = value["capability_requirement"]
    _require(
        isinstance(capability, dict) and set(capability) == _CAPABILITY_KEYS,
        "capability_requirement must declare the nine non-digest members",
    )
    architectures = capability["architectures"]
    _require(
        isinstance(architectures, list)
        and 1 <= len(architectures) == len(set(architectures))
        and set(architectures) <= _ARCHITECTURES,
        "architectures are invalid",
    )
    _require(
        capability["adapter_id"] == adapter.adapter_id
        and capability["adapter_version"] == adapter.adapter_version,
        "capability adapter must equal the template adapter",
    )
    _require(capability["device"] == "CPU", "capability device must be CPU")
    _require(capability["framework"] == adapter.framework, "framework must be the adapter's")
    _require(
        isinstance(capability["framework_version"], str)
        and bool(_SEMVER.fullmatch(capability["framework_version"]))
        and adapter.framework_version_matches(capability["framework_version"]),
        "framework_version is invalid for the adapter",
    )
    _require(
        capability["cuda_runtime_min"] is None
        and capability["driver_min"] is None
        and capability["compute_capability_min"] is None,
        "CPU templates declare no CUDA requirement",
    )
    return value


def load_definition(raw: bytes) -> dict[str, Any]:
    try:
        decoded = decode_json_object(raw, max_bytes=MAX_DEFINITION_BYTES)
    except JsonRequestError as exc:
        raise TemplateDefinitionError(str(exc)) from None
    return validate_definition(_plain(decoded))


def version_values(definition: dict[str, Any], image_digest: str) -> dict[str, Any]:
    """The immutable template_versions row for one definition and built image."""
    _require(isinstance(image_digest, str) and bool(_DIGEST.fullmatch(image_digest)), "digest")
    capability = dict(definition["capability_requirement"], image_digest=image_digest)
    return {
        "template_id": definition["template_id"],
        "version": definition["version"],
        "parameter_schema": definition["parameter_schema"],
        "resource_bounds": definition["resource_bounds"],
        "capability_requirements": {key: capability[key] for key in sorted(capability)},
        "adapter_id": definition["adapter_id"],
        "adapter_version": definition["adapter_version"],
        "image_digest": image_digest,
        "checkpointable": definition["checkpointable"],
        "restart_safe": definition["restart_safe"],
    }


_COMPARED = (
    "parameter_schema",
    "resource_bounds",
    "capability_requirements",
    "adapter_id",
    "adapter_version",
    "image_digest",
    "checkpointable",
    "restart_safe",
)


def _content_hash(row: dict[str, Any]) -> str:
    return jcs_request_hash({key: row[key] for key in _COMPARED})


def register_template(
    session_factory, definition: dict[str, Any], image_digest: str
) -> dict[str, Any]:
    """Insert one immutable template version; same content replays, other content conflicts."""
    values = version_values(definition, image_digest)
    content_hash = _content_hash(values)
    template_id, version = values["template_id"], values["version"]

    def operation(session: Session) -> dict[str, Any]:
        mode = session.execute(
            select(policy_versions.c.operational_mode)
            .where(policy_versions.c.is_current.is_(True))
            .with_for_update(read=True)
        ).scalar_one()
        if mode == "WRITE_FROZEN":
            raise ApplicationError(
                code="state_conflict",
                status=409,
                message="Templates cannot be registered while writes are frozen",
            )
        catalog = (
            session.execute(
                select(templates).where(templates.c.template_id == template_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        existing = (
            session.execute(
                select(template_versions).where(
                    template_versions.c.template_id == template_id,
                    template_versions.c.version == version,
                )
            )
            .mappings()
            .one_or_none()
        )
        result = {
            "template_id": template_id,
            "version": version,
            "image_digest": image_digest,
            "content_checksum": content_hash,
        }
        if existing is not None:
            if _content_hash(dict(existing)) != content_hash:
                raise ApplicationError(
                    code="state_conflict",
                    status=409,
                    message="Template version already exists with different content",
                )
            return {**result, "status": "UNCHANGED", "current_version": catalog["current_version"]}
        now = transaction_timestamp(session)
        if catalog is None:
            # The templates -> current version FK is deferred to commit time.
            session.execute(
                insert(templates).values(
                    template_id=template_id,
                    current_version=version,
                    display_name=definition["display_name"],
                    description=definition["description"],
                    enabled=True,
                )
            )
            current = version
        else:
            current = max(catalog["current_version"], version)
            if current != catalog["current_version"]:
                session.execute(
                    update(templates)
                    .where(templates.c.template_id == template_id)
                    .values(
                        current_version=current,
                        display_name=definition["display_name"],
                        description=definition["description"],
                        updated_at=now,
                    )
                )
        session.execute(insert(template_versions).values(**values))
        session.execute(
            insert(audit_records).values(
                audit_id=new_uuid7(),
                actor_type="SYSTEM",
                actor_id="local-operator",
                action="template.version.register",
                target_type="TEMPLATE_VERSION",
                target_id=f"{template_id}:{version}",
                after_version=version,
                reason="Local operator registered a template version",
                safe_metadata={
                    "adapter_id": values["adapter_id"],
                    "adapter_version": values["adapter_version"],
                    "image_digest": image_digest,
                    "content_checksum": content_hash,
                    "current_version": current,
                },
            )
        )
        return {**result, "status": "REGISTERED", "current_version": current}

    return run_transaction(session_factory, operation)


def _parameter_wire(item: dict[str, Any]) -> dict[str, Any]:
    wire = {key: item.get(key) for key in ("name", "type", "required", "minimum", "maximum")}
    wire["default"] = item.get("default")
    for key in ("enum_values", "unit"):
        if key in item:
            wire[key] = item[key]
    return wire


def template_wire(catalog: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    capability = dict(row["capability_requirements"])
    return {
        "template_id": catalog["template_id"],
        "version": row["version"],
        "display_name": catalog["display_name"],
        "adapter_id": row["adapter_id"],
        "adapter_version": row["adapter_version"],
        "image_digest": row["image_digest"],
        "enabled": catalog["enabled"],
        "checkpointable": row["checkpointable"],
        "restart_safe": row["restart_safe"],
        "allowed_devices": [capability.get("device")],
        "capability_requirement": capability,
        "parameter_schema": [_parameter_wire(item) for item in row["parameter_schema"]],
    }


class TemplateCatalog:
    """Tenant-context template reads; the catalog itself is global and admin-registered."""

    def __init__(self, session_factory, identity: IdentityService) -> None:
        self.session_factory = session_factory
        self.identity = identity

    def _authorize(self, session: Session, principal: Principal, tenant_id: UUID) -> None:
        live = self.identity.revalidate_principal(session, principal)
        try:
            require_tenant_membership(live, tenant_id)
        except AuthorizationError as exc:
            raise ApplicationError(
                code="permission_denied", status=403, message="Active tenant membership is required"
            ) from exc
        if live.credential_kind.name == "CLI" and not live.has_scope(TokenScope.JOBS_READ):
            raise ApplicationError(
                code="permission_denied",
                status=403,
                message=f"The exact {TokenScope.JOBS_READ.value} scope is required",
            )

    @staticmethod
    def _current():
        return (
            select(
                templates.c.template_id,
                templates.c.display_name,
                templates.c.enabled,
                template_versions.c.version,
                template_versions.c.parameter_schema,
                template_versions.c.capability_requirements,
                template_versions.c.adapter_id,
                template_versions.c.adapter_version,
                template_versions.c.image_digest,
                template_versions.c.checkpointable,
                template_versions.c.restart_safe,
            )
            .select_from(templates)
            .join(
                template_versions,
                (template_versions.c.template_id == templates.c.template_id)
                & (template_versions.c.version == templates.c.current_version),
            )
            .order_by(templates.c.template_id)
        )

    @staticmethod
    def _wire(row) -> dict[str, Any]:
        mapping = dict(row)
        return template_wire(mapping, mapping)

    def list_templates(
        self, principal: Principal, *, tenant_id: UUID, enabled: bool = True
    ) -> list[dict[str, Any]]:
        def operation(session: Session) -> list[dict[str, Any]]:
            self._authorize(session, principal, tenant_id)
            rows = session.execute(
                self._current().where(templates.c.enabled.is_(enabled)).limit(_MAX_TEMPLATES)
            ).mappings()
            return [self._wire(row) for row in rows]

        return run_transaction(self.session_factory, operation)

    def get_template(
        self, principal: Principal, *, tenant_id: UUID, template_id: str
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            self._authorize(session, principal, tenant_id)
            row = (
                session.execute(self._current().where(templates.c.template_id == template_id))
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ApplicationError(
                    code="resource_not_found", status=404, message="Template was not found"
                )
            return self._wire(row)

        return run_transaction(self.session_factory, operation)
