from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import UUID7, BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator

from nexa.domain.identity import TokenScope

_UUID7_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
UuidV7 = Annotated[UUID7, Field(json_schema_extra={"pattern": _UUID7_PATTERN})]


class UserArtifactKind(StrEnum):
    INPUT = "INPUT"
    DATASET = "DATASET"
    MODEL = "MODEL"


class ArtifactKind(StrEnum):
    INPUT = "INPUT"
    DATASET = "DATASET"
    MODEL = "MODEL"
    CHECKPOINT_FILE = "CHECKPOINT_FILE"
    CHECKPOINT_MANIFEST = "CHECKPOINT_MANIFEST"
    RESULT_FILE = "RESULT_FILE"
    RESULT_MANIFEST = "RESULT_MANIFEST"
    CHUNK_OUTPUT_MANIFEST = "CHUNK_OUTPUT_MANIFEST"
    LOG = "LOG"


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
    cpu_millis: StrictInt = Field(ge=0, le=100_000_000)
    memory_bytes: StrictInt = Field(ge=0, le=9_223_372_036_854_775_807)
    gpu_count: StrictInt = Field(ge=0, le=64)


class ResourceRequestVector(StrictRequest):
    cpu_millis: StrictInt = Field(ge=100, le=100_000_000)
    memory_bytes: StrictInt = Field(ge=67_108_864, le=9_223_372_036_854_775_807)
    gpu_count: StrictInt = Field(ge=0, le=1)


class CpuParameters(StrictRequest):
    iterations: StrictInt = Field(ge=1, le=1_000_000_000)
    seed: StrictInt = Field(ge=0, le=2_147_483_647)
    modulus: StrictInt = Field(ge=2, le=2_147_483_647)


class TrainingParameters(StrictRequest):
    epochs: StrictInt = Field(ge=1, le=100)
    batch_size: StrictInt = Field(ge=1, le=512)
    learning_rate: StrictFloat = Field(gt=0, le=1)
    seed: StrictInt = Field(ge=0, le=2_147_483_647)
    subset_size: StrictInt = Field(ge=100, le=50_000)


class InferenceParameters(StrictRequest):
    chunk_size: StrictInt = Field(ge=1, le=100_000)
    batch_size: StrictInt = Field(ge=1, le=4_096)
    output_format: Literal["JSONL", "PARQUET"]


class JobSpecBase(StrictRequest):
    template_version: StrictInt = Field(ge=1)
    input_artifact_id: UuidV7
    resources: ResourceRequestVector
    priority: StrictInt = Field(ge=0, le=2)
    runtime_limit_seconds: StrictInt = Field(ge=1, le=300)
    checkpoint_interval_seconds: StrictInt = Field(ge=5, le=60)


class CpuJobSpec(JobSpecBase):
    template_id: Literal["cpu-iterative"]
    parameters: CpuParameters


class TrainingJobSpec(JobSpecBase):
    template_id: Literal["pytorch-cifar10-cnn"]
    parameters: TrainingParameters


class InferenceJobSpec(JobSpecBase):
    template_id: Literal["batch-inference"]
    model_artifact_id: UuidV7
    parameters: InferenceParameters


JobSpec = Annotated[
    CpuJobSpec | TrainingJobSpec | InferenceJobSpec,
    Field(discriminator="template_id"),
]


class JobSubmitRequest(StrictRequest):
    spec: JobSpec


JobState = Literal[
    "QUEUED",
    "DISPATCHING",
    "RUNNING",
    "PAUSING",
    "PAUSED",
    "RECOVERING",
    "RETRY_WAIT",
    "CANCELLING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
]
DesiredState = Literal["RUNNING", "PAUSED", "CANCELLED"]
WaitingReason = Literal[
    "waiting_for_worker",
    "waiting_for_capacity",
    "waiting_for_quota",
    "waiting_for_reservation",
    "waiting_for_retry",
    "waiting_for_compatibility",
    None,
]


class PageInfo(BaseModel):
    model_config = ConfigDict(title="PageInfo", extra="forbid")

    next_cursor: str | None = Field(max_length=2048)
    page_size: int = Field(ge=0, le=100)


class Job(BaseModel):
    model_config = ConfigDict(title="Job", extra="forbid")

    job_id: UuidV7
    tenant_id: UuidV7
    user_id: UuidV7
    session_id: UuidV7
    state: JobState
    desired_state: DesiredState
    waiting_reason: WaitingReason
    spec: JobSpec
    spec_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    job_fence: int = Field(ge=0)
    retry_count: int = Field(ge=0, le=2)
    max_retries: Literal[2]
    retry_of_job_id: UuidV7 | None
    version: int = Field(ge=1)
    event_sequence: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class JobPage(BaseModel):
    model_config = ConfigDict(title="JobPage", extra="forbid")

    items: list[Job] = Field(max_length=100)
    page: PageInfo


class LogicalSession(BaseModel):
    model_config = ConfigDict(title="LogicalSession", extra="forbid")

    session_id: UuidV7
    job_id: UuidV7
    tenant_id: UuidV7
    derived_state: JobState
    created_at: datetime


class Event(BaseModel):
    model_config = ConfigDict(title="Event", extra="forbid")

    event_id: UuidV7
    tenant_id: UuidV7
    job_id: UuidV7 | None
    sequence: int = Field(ge=1)
    type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=1, max_length=256)
    actor_type: Literal["USER", "ADMIN", "WORKER", "COORDINATOR", "SYSTEM"]
    created_at: datetime


class EventPage(BaseModel):
    model_config = ConfigDict(title="EventPage", extra="forbid")

    items: list[Event] = Field(max_length=100)
    page: PageInfo


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


class Artifact(BaseModel):
    model_config = ConfigDict(title="Artifact", extra="forbid")

    artifact_id: UuidV7
    tenant_id: UuidV7
    kind: ArtifactKind
    media_type: str = Field(min_length=1, max_length=127)
    size_bytes: int = Field(ge=0)
    checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    state: Literal["COMMITTED"]
    version: int = Field(ge=1)
    created_at: datetime


ArtifactResponse = Artifact


class ArtifactPage(BaseModel):
    model_config = ConfigDict(title="ArtifactPage", extra="forbid")

    items: list[Artifact] = Field(max_length=100)
    page: PageInfo


ArtifactPageResponse = ArtifactPage


class ErrorResponse(BaseModel):
    model_config = ConfigDict(title="ErrorResponse", extra="forbid")

    code: str
    message: str
    request_id: str
