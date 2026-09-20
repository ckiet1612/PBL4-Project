from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexa.domain.identity import TokenScope


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictRequest):
    username: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=1024)


class TokenCreateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=64)
    scopes: list[TokenScope] = Field(min_length=1)
    expires_in_seconds: int = Field(ge=300, le=2_592_000)

    @field_validator("scopes")
    @classmethod
    def scopes_are_unique(cls, value: list[TokenScope]) -> list[TokenScope]:
        if len(value) != len(set(value)):
            raise ValueError("Token scopes must be unique")
        return value


class AdminBootstrapRequest(StrictRequest):
    username: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=1024)


class WorkerBootstrapRequest(StrictRequest):
    installation_id: UUID
    credential_public_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class TenantCreateRequest(StrictRequest):
    slug: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9-]{1,61}[a-zA-Z0-9]$")
    display_name: str = Field(min_length=1, max_length=100)


class TenantUpdateRequest(StrictRequest):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None


class UserCreateRequest(StrictRequest):
    username: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=1024)
    system_roles: list[Literal["SYSTEM_ADMIN"]] = Field(max_length=1)

    @field_validator("system_roles")
    @classmethod
    def system_roles_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("System roles must be unique")
        return value


class UserUpdateRequest(StrictRequest):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    password: str | None = Field(default=None, min_length=12, max_length=1024)
    enabled: bool | None = None
    system_roles: list[Literal["SYSTEM_ADMIN"]] | None = Field(default=None, max_length=1)

    @field_validator("system_roles")
    @classmethod
    def updated_system_roles_are_unique(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("System roles must be unique")
        return value


class MembershipWriteRequest(StrictRequest):
    user_id: UUID
    role: Literal["MEMBER", "TENANT_ADMIN"]


class GlobalPolicyUpdate(StrictRequest):
    global_outstanding_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    operational_mode: Literal["NORMAL", "ADMISSION_OFF", "WRITE_FROZEN"] | None = None


class ResourceCapacityVector(StrictRequest):
    cpu_millis: int = Field(ge=0, le=100_000_000)
    memory_bytes: int = Field(ge=0, le=9_223_372_036_854_775_807)
    gpu_count: int = Field(ge=0, le=1)


class TenantPolicyUpdate(StrictRequest):
    weight: Decimal | None = Field(default=None, gt=0, le=Decimal("1000"))
    outstanding_limit: int | None = Field(default=None, ge=1)
    user_outstanding_limit: int | None = Field(default=None, ge=1)
    concurrent_attempt_limit: int | None = Field(default=None, ge=1)
    user_concurrent_attempt_limit: int | None = Field(default=None, ge=1)
    resource_limit: ResourceCapacityVector | None = None
    submit_rate_per_second: Decimal | None = Field(default=None, gt=0)
    submit_burst: int | None = Field(default=None, ge=1)
    user_submit_rate_per_second: Decimal | None = Field(default=None, gt=0)
    user_submit_burst: int | None = Field(default=None, ge=1)
