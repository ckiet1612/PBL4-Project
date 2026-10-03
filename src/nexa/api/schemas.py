from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    UUID7,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
)

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


Architecture = Literal["linux/amd64", "linux/arm64"]


class RuntimeCapability(StrictRequest):
    docker_version: str = Field(min_length=1, max_length=64)
    oci_runtime: Literal["runc", "crun"]
    oci_runtime_version: str = Field(min_length=1, max_length=64)
    cgroups_version: Literal[2]
    kernel_release: str = Field(min_length=1, max_length=128)
    seccomp_available: bool


class AdapterCapability(StrictRequest):
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    adapter_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


class ImageCapability(StrictRequest):
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    architecture: Architecture
    verified: bool


class FrameworkCapability(StrictRequest):
    framework: Literal["PYTORCH", "NEXA_CPU"]
    framework_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
    device: Literal["CPU", "CUDA"]
    cuda_runtime_version: str | None = Field(
        default=None,
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$",
    )


class GpuDevice(StrictRequest):
    uuid: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    memory_bytes: StrictInt = Field(ge=1)
    compute_capability: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    driver_version: str = Field(min_length=1, max_length=64)
    cuda_driver_api_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
    healthy: bool


class WorkerInventory(StrictRequest):
    architecture: Architecture
    host_cpu_millis: StrictInt = Field(ge=1_000)
    host_memory_bytes: StrictInt = Field(ge=2_147_483_648)
    allocatable: ResourceCapacityVector
    runtime: RuntimeCapability
    adapters: list[AdapterCapability] = Field(max_length=64)
    images: list[ImageCapability] = Field(max_length=256)
    frameworks: list[FrameworkCapability] = Field(max_length=64)
    gpu_devices: list[GpuDevice] = Field(max_length=64)
    discovered_at: datetime


class WorkerIncarnationCreateRequest(StrictRequest):
    process_start_nonce: UuidV7


class ContainerIdentity(StrictRequest):
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_identity_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class Authority(StrictRequest):
    worker_id: UuidV7
    worker_incarnation_id: UuidV7
    attempt_id: UuidV7
    allocation_id: UuidV7
    lease_id: UuidV7
    job_fence: StrictInt = Field(ge=1)


class HeartbeatRequest(StrictRequest):
    worker_incarnation_id: UuidV7
    observed_health: Literal["STARTING", "READY"]
    reconcile_complete: bool
    inventory: WorkerInventory
    observed_containers: list[ContainerIdentity] = Field(max_length=1_000)


class PollRequest(StrictRequest):
    worker_incarnation_id: UuidV7
    long_poll_seconds: StrictInt = Field(default=20, ge=0, le=30)


class AdoptRequest(StrictRequest):
    prior_authority: Authority
    current_worker_incarnation_id: UuidV7
    container: ContainerIdentity


class ProgressSnapshot(StrictRequest):
    fraction: StrictFloat = Field(ge=0, le=1)
    step: StrictInt | None = Field(ge=0)
    epoch: StrictInt | None = Field(ge=0)
    item_cursor: StrictInt | None = Field(ge=0)


class RenewWithoutProgressRequest(StrictRequest):
    authority: Authority
    progress_sequence: StrictInt = Field(ge=0, le=0)
    progress: None


class RenewWithProgressRequest(StrictRequest):
    authority: Authority
    progress_sequence: StrictInt = Field(ge=1)
    progress: ProgressSnapshot


RenewRequest = RenewWithoutProgressRequest | RenewWithProgressRequest


class RenewBody(RootModel[RenewWithoutProgressRequest | RenewWithProgressRequest]):
    pass


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


_SEMVER_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$"


class ParameterDefinition(BaseModel):
    model_config = ConfigDict(title="ParameterDefinition", extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    type: Literal["INTEGER", "NUMBER", "BOOLEAN", "STRING", "ENUM"]
    required: bool
    minimum: StrictInt | StrictFloat | None
    maximum: StrictInt | StrictFloat | None
    default: StrictInt | StrictFloat | bool | str | None
    enum_values: list[Annotated[str, Field(max_length=64)]] | None = Field(
        default=None, max_length=64
    )
    unit: str | None = Field(default=None, max_length=32)


class WorkloadCapabilityRequirement(BaseModel):
    model_config = ConfigDict(title="WorkloadCapabilityRequirement", extra="forbid")

    architectures: list[Architecture] = Field(min_length=1)
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    adapter_version: str = Field(pattern=_SEMVER_PATTERN)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    device: Literal["CPU", "CUDA"]
    framework: Literal["PYTORCH", "NEXA_CPU"]
    framework_version: str = Field(pattern=_SEMVER_PATTERN)
    cuda_runtime_min: str | None = Field(pattern=_SEMVER_PATTERN)
    driver_min: str | None = Field(min_length=1, max_length=64)
    compute_capability_min: str | None = Field(pattern=r"^[0-9]+\.[0-9]+$")


class Template(BaseModel):
    model_config = ConfigDict(title="Template", extra="forbid")

    template_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    version: int = Field(ge=1)
    display_name: str = Field(min_length=1, max_length=100)
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    adapter_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    enabled: bool
    checkpointable: bool
    restart_safe: bool
    allowed_devices: list[Literal["CPU", "CUDA"]] = Field(min_length=1)
    capability_requirement: WorkloadCapabilityRequirement
    parameter_schema: list[ParameterDefinition] = Field(max_length=64)


class TemplateList(RootModel[list[Template]]):
    root: list[Template] = Field(max_length=100)


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
WaitingReasonValue = Literal[
    "waiting_for_worker",
    "waiting_for_capacity",
    "waiting_for_quota",
    "waiting_for_reservation",
    "waiting_for_retry",
    "waiting_for_compatibility",
]
WaitingReason = Literal[WaitingReasonValue, None]


class PageInfo(BaseModel):
    model_config = ConfigDict(title="PageInfo", extra="forbid")

    next_cursor: str | None = Field(max_length=2048)
    page_size: int = Field(ge=0, le=100)


class WorkerIncarnation(BaseModel):
    model_config = ConfigDict(title="WorkerIncarnation", extra="forbid")

    worker_id: UuidV7
    worker_incarnation_id: UuidV7
    sequence: int = Field(ge=1)
    process_start_nonce: UuidV7
    health: Literal["STARTING"]
    created_at: datetime


class HeartbeatResponse(BaseModel):
    model_config = ConfigDict(title="HeartbeatResponse", extra="forbid")

    server_time: datetime
    admin_state: Literal["ENABLED", "DRAINING", "DISABLED"]
    accepted_incarnation_id: UuidV7
    next_heartbeat_seconds: Literal[5]


class Allocation(BaseModel):
    model_config = ConfigDict(title="Allocation", extra="forbid")

    allocation_id: UuidV7
    tenant_id: UuidV7
    job_id: UuidV7
    attempt_id: UuidV7
    worker_id: UuidV7
    resources: ResourceRequestVector
    gpu_uuids: list[str] = Field(max_length=1)
    state: Literal["HELD", "QUARANTINED", "RELEASED"]
    held_at: datetime
    quarantined_at: datetime | None
    released_at: datetime | None


class CompletionAcknowledgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    callback_id: UuidV7
    accepted: Literal[True]
    server_time: datetime
    job_state: Literal["SUCCEEDED"]
    job_version: int = Field(ge=1)

    @field_serializer("server_time")
    def _original_timestamp(self, value: datetime) -> str:
        # A reconciliation receipt must preserve the committed callback wire.
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CommittedCompletionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    callback_id: UuidV7
    result_id: UuidV7
    manifest_artifact_id: UuidV7
    acknowledgment: CompletionAcknowledgment


class ReconciliationItem(BaseModel):
    model_config = ConfigDict(title="ReconciliationItem", extra="forbid")

    authority: Authority
    authority_state: Literal["LIVE", "REVOKED"]
    lease_expires_at: datetime
    desired_state: DesiredState
    allocation: Allocation
    startup_nonce: UuidV7
    claim_state: Literal["UNCLAIMED", "CLAIMED", "STARTED"]
    expected_container: ContainerIdentity | None
    completion_receipt: CommittedCompletionReceipt | None


class ReconciliationPage(BaseModel):
    model_config = ConfigDict(title="ReconciliationPage", extra="forbid")

    server_time: datetime
    items: list[ReconciliationItem] = Field(max_length=100)
    page: PageInfo


class DispatchOffer(BaseModel):
    model_config = ConfigDict(title="DispatchOffer", extra="forbid")

    authority: Authority
    dispatch_coordinator_epoch: int = Field(ge=1)
    spec: JobSpec
    input_artifact: "Artifact"
    checkpoint: dict | None
    startup_limit_seconds: Literal[30]
    lease_duration_seconds: Literal[45]


class PollResponse(BaseModel):
    model_config = ConfigDict(title="PollResponse", extra="forbid")

    server_time: datetime
    offer: DispatchOffer | None
    retry_after_seconds: int = Field(ge=0, le=30)


class CheckpointReservationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    callback_id: UuidV7
    checkpoint_id: UuidV7
    job_id: UuidV7
    attempt_id: UuidV7
    sequence: int = Field(ge=1)
    reserved_at: datetime


class ResultReservationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    callback_id: UuidV7
    result_id: UuidV7
    job_id: UuidV7
    attempt_id: UuidV7
    reserved_at: datetime


class ResultRecord(BaseModel):
    model_config = ConfigDict(title="ResultRecord", extra="forbid")

    result_id: UuidV7
    job_id: UuidV7
    attempt_id: UuidV7
    manifest_artifact_id: UuidV7
    manifest_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: datetime


class ProgressAbsent(BaseModel):
    model_config = ConfigDict(title="ProgressAbsent", extra="forbid")

    job_id: UuidV7
    available: Literal[False]


class ProgressPresent(BaseModel):
    model_config = ConfigDict(title="ProgressPresent", extra="forbid")

    job_id: UuidV7
    available: Literal[True]
    attempt_id: UuidV7
    progress_sequence: int = Field(ge=1)
    snapshot: ProgressSnapshot
    restore_checkpoint_id: UuidV7 | None
    reported_at: datetime


class ProgressRecord(
    RootModel[Annotated[ProgressAbsent | ProgressPresent, Field(discriminator="available")]]
):
    model_config = ConfigDict(title="ProgressRecord")


class AdoptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    callback_id: UuidV7
    accepted: Literal[True]
    server_time: datetime
    authority: Authority
    transferred_checkpoint_reservation: CheckpointReservationResponse | None
    transferred_result_reservation: ResultReservationResponse | None
    lease_expires_at: datetime
    lease_duration_seconds: Literal[45]
    renew_interval_seconds: Literal[5]
    safety_margin_seconds: Literal[5]


class RenewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_time: datetime
    lease_expires_at: datetime
    desired_state: DesiredState
    renew_interval_seconds: Literal[5]
    safety_margin_seconds: Literal[5]


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


class FairnessBucket(BaseModel):
    model_config = ConfigDict(title="FairnessBucket", extra="forbid")

    tenant_id: UuidV7
    start_at: datetime
    end_at: datetime
    weight: float = Field(gt=0)
    dominant_resource_time_seconds: float = Field(ge=0)
    normalized_service: float = Field(ge=0)
    allocation_occupancy_seconds: float = Field(ge=0)


class FairnessReport(BaseModel):
    model_config = ConfigDict(title="FairnessReport", extra="forbid", populate_by_name=True)

    from_at: datetime = Field(alias="from")
    to_at: datetime = Field(alias="to")
    bucket_seconds: int = Field(ge=1)
    buckets: list[FairnessBucket] = Field(max_length=1000)


class ControlRequest(StrictRequest):
    reason: str = Field(min_length=1, max_length=256)


class RetryRequest(StrictRequest):
    reason: str = Field(min_length=1, max_length=256)
    checkpoint_id: UuidV7 | None


class AdminReasonRequest(StrictRequest):
    reason: str = Field(min_length=1, max_length=256)


AttemptState = Literal[
    "CREATED",
    "CLAIMED",
    "STARTING",
    "RUNNING",
    "CHECKPOINTING",
    "STOPPING",
    "SUCCEEDED",
    "FAILED",
    "LOST",
    "CANCELLED",
]


class Attempt(BaseModel):
    model_config = ConfigDict(title="Attempt", extra="forbid")

    attempt_id: UuidV7
    job_id: UuidV7
    attempt_number: int = Field(ge=1, le=100)
    state: AttemptState
    worker_id: UuidV7
    worker_incarnation_id: UuidV7
    allocation_id: UuidV7
    lease_id: UuidV7
    job_fence: int = Field(ge=1)
    started_at: datetime | None
    ended_at: datetime | None
    failure_class: (
        Literal[
            "INFRASTRUCTURE",
            "TIMEOUT",
            "OOM",
            "INVALID_INPUT",
            "USER_CANCEL",
            "INCOMPATIBLE",
            "INTERNAL",
        ]
        | None
    )
    created_at: datetime


class AttemptPage(BaseModel):
    model_config = ConfigDict(title="AttemptPage", extra="forbid")

    items: list[Attempt] = Field(max_length=100)
    page: PageInfo


class Worker(BaseModel):
    model_config = ConfigDict(title="Worker", extra="forbid")

    worker_id: UuidV7
    current_incarnation_id: UuidV7 | None
    health: Literal["STARTING", "READY", "SUSPECT", "UNAVAILABLE"]
    admin_state: Literal["ENABLED", "DRAINING", "DISABLED"]
    inventory: WorkerInventory | None
    version: int = Field(ge=1)
    last_heartbeat_at: datetime | None
    ready_at: datetime | None


class WorkerPage(BaseModel):
    model_config = ConfigDict(title="WorkerPage", extra="forbid")

    items: list[Worker] = Field(max_length=100)
    page: PageInfo


class AllocationPage(BaseModel):
    model_config = ConfigDict(title="AllocationPage", extra="forbid")

    items: list[Allocation] = Field(max_length=100)
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
    # Present only where a contract names a safe classification (B16-R21).
    reason: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")


class SweepDimension(StrictRequest):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    # uniqueItems by RFC 8785 form: a repeat is 422 during expansion (B16-R10).
    values: list[StrictBool | StrictInt | StrictFloat | StrictStr] = Field(
        min_length=1, max_length=100
    )


class SweepSubmitRequest(StrictRequest):
    base_spec: TrainingJobSpec
    dimensions: list[SweepDimension] = Field(min_length=1, max_length=16)


class SweepChildOutcome(BaseModel):
    model_config = ConfigDict(title="SweepChildOutcome", extra="forbid")

    child_index: int = Field(ge=0, le=99)
    parameter_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["ACCEPTED", "REJECTED"]
    job_id: UuidV7 | None
    error: ErrorResponse | None


class Sweep(BaseModel):
    model_config = ConfigDict(title="Sweep", extra="forbid")

    sweep_id: UuidV7
    tenant_id: UuidV7
    child_count: int = Field(ge=1, le=100)
    accepted_count: int = Field(ge=0, le=100)
    rejected_count: int = Field(ge=0, le=100)
    children: list[SweepChildOutcome] = Field(max_length=100)
    page: PageInfo
    created_at: datetime


class AuthorityRequest(StrictRequest):
    authority: Authority


class StartRequest(AuthorityRequest):
    startup_nonce: UuidV7
    executor_operation_sequence: StrictInt = Field(ge=1)
    container: ContainerIdentity


class CompleteRequest(StrictRequest):
    authority: Authority
    result_manifest_artifact_id: UuidV7
    manifest: dict


class CheckpointPublishRequest(StrictRequest):
    authority: Authority
    manifest_artifact_id: UuidV7
    manifest: dict


class CheckpointRecord(BaseModel):
    model_config = ConfigDict(title="CheckpointRecord", extra="forbid")

    checkpoint_id: UuidV7
    job_id: UuidV7
    attempt_id: UuidV7
    sequence: int = Field(ge=1)
    manifest_artifact_id: UuidV7
    manifest_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    state: Literal["COMMITTED", "REJECTED", "CORRUPT"]
    created_at: datetime


class CheckpointPage(BaseModel):
    model_config = ConfigDict(title="CheckpointPage", extra="forbid")

    items: list[CheckpointRecord] = Field(max_length=100)
    page: PageInfo
