"""Closed B11 failure and proof-based cleanup wire models."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, StrictInt

from nexa.api.schemas import Authority, ContainerIdentity, StrictRequest, UuidV7

Checksum = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Sequence = Annotated[StrictInt, Field(ge=1)]


class NoContainerProof(StrictRequest):
    proof_type: Literal["NO_CONTAINER"]
    startup_nonce: UuidV7
    executor_operation_sequence: Sequence
    tombstone_sequence: Sequence
    observed_at: AwareDatetime
    inspection_checksum: Checksum


class ContainerStoppedProof(StrictRequest):
    proof_type: Literal["CONTAINER_STOPPED"]
    startup_nonce: UuidV7
    executor_operation_sequence: Sequence
    container: ContainerIdentity
    stopped_at: AwareDatetime
    exit_code: Annotated[StrictInt, Field(ge=-1, le=255)]
    inspection_checksum: Checksum


CleanupProof = Annotated[
    NoContainerProof | ContainerStoppedProof, Field(discriminator="proof_type")
]


class NoContainerObservation(StrictRequest):
    observation_type: Literal["NO_CONTAINER"]
    proof: NoContainerProof


class ContainerObservation(StrictRequest):
    observation_type: Literal["CONTAINER"]
    container: ContainerIdentity
    observed_at: AwareDatetime
    exit_code: Annotated[StrictInt, Field(ge=-1, le=255)] | None
    oom_killed: StrictBool
    runtime_limit_reached: StrictBool


class FailureRequest(StrictRequest):
    authority: Authority
    failure_class: Literal[
        "INFRASTRUCTURE", "TIMEOUT", "OOM", "INVALID_INPUT", "INCOMPATIBLE", "INTERNAL"
    ]
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    observation: Annotated[
        NoContainerObservation | ContainerObservation, Field(discriminator="observation_type")
    ]


class CleanupRequest(StrictRequest):
    worker_id: UuidV7
    worker_incarnation_id: UuidV7
    attempt_id: UuidV7
    allocation_id: UuidV7
    job_fence: Sequence
    proof: CleanupProof


class WorkerAck(StrictRequest):
    callback_id: UuidV7
    accepted: bool
    server_time: AwareDatetime
    job_state: str
    job_version: int


class CleanupResponse(StrictRequest):
    callback_id: UuidV7
    verified: bool
    allocation_state: Literal["HELD", "QUARANTINED", "RELEASED"]
    server_time: AwareDatetime
