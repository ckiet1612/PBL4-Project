"""Failure linearization and narrow cleanup identity; no Docker access."""

from datetime import timedelta
from secrets import randbelow

from sqlalchemy import insert, select, update

from nexa.application.errors import ApplicationError
from nexa.application.json_codec import json_wire_value
from nexa.coordinator.accounting import account_locked, rebase_locked
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from nexa.infrastructure.persistence.locking import clock_timestamp
from nexa.infrastructure.persistence.transactions import run_transaction

# Safe codes have a fixed meaning. Neither raw worker text nor stderr is persisted.
_FAILURE_REASONS = {
    "INFRASTRUCTURE": {
        "RECONCILIATION_IDENTITY_MISMATCH",
        "EXECUTOR_UNAVAILABLE",
        "STARTUP_FAILED",
        "INPUT_DOWNLOAD_FAILED",
        "OUTPUT_UPLOAD_FAILED",
        "RUNNER_UNAVAILABLE",
    },
    "TIMEOUT": {"RUNTIME_LIMIT_REACHED", "STARTUP_TIMEOUT"},
    "OOM": {"CONTAINER_OOM"},
    "INVALID_INPUT": {"INVALID_INPUT", "INPUT_CHECKSUM_MISMATCH"},
    "INCOMPATIBLE": {"CAPABILITY_MISMATCH", "IMAGE_MISMATCH"},
    "INTERNAL": {"WORKLOAD_EXIT_NONZERO", "RUNNER_PROTOCOL_ERROR", "INVALID_RESULT"},
}
_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def _conflict(message="Cleanup identity or state is not valid"):
    raise ApplicationError(code="state_conflict", status=409, message=message)


def _lock_policy_counters(session, attempt_id):
    ref = (
        session.execute(
            select(s.jobs)
            .join(s.attempts, s.jobs.c.job_id == s.attempts.c.job_id)
            .where(s.attempts.c.attempt_id == attempt_id)
        )
        .mappings()
        .one_or_none()
    )
    if ref is None:
        _conflict()
    mode = session.execute(
        select(s.policy_versions.c.operational_mode)
        .where(s.policy_versions.c.is_current.is_(True))
        .with_for_update()
    ).scalar_one()
    session.execute(
        select(s.tenant_policies)
        .where(
            s.tenant_policies.c.tenant_id == ref["tenant_id"],
            s.tenant_policies.c.is_current.is_(True),
        )
        .with_for_update()
    ).all()
    counters = []
    for kind, identity in [
        ("GLOBAL", "global"),
        ("TENANT", str(ref["tenant_id"])),
        ("USER", f"{ref['tenant_id']}:{ref['submitter_user_id']}"),
    ]:
        row = (
            session.execute(
                select(s.admission_counters)
                .where(
                    s.admission_counters.c.scope_type == kind,
                    s.admission_counters.c.scope_id == identity,
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApplicationError(
                code="dependency_unavailable",
                status=503,
                message="Admission accounting is unavailable",
                retry_after=1,
            )
        counters.append(dict(row))
    return mode, counters


def _adjust_counters(session, counters, now, *, outstanding=0, active=0):
    for row in counters:
        if row["outstanding"] < outstanding or row["active_attempts"] < active:
            raise ApplicationError(
                code="dependency_unavailable",
                status=503,
                message="Admission accounting is inconsistent",
                retry_after=1,
            )
        session.execute(
            update(s.admission_counters)
            .where(
                s.admission_counters.c.scope_type == row["scope_type"],
                s.admission_counters.c.scope_id == row["scope_id"],
            )
            .values(
                outstanding=row["outstanding"] - outstanding,
                active_attempts=row["active_attempts"] - active,
                version=row["version"] + 1,
                updated_at=now,
            )
        )


def _event(session, job, worker_id, now, event_type, reason, **changes):
    sequence = job["event_sequence"] + 1
    version = job["version"] + 1
    session.execute(
        update(s.jobs)
        .where(s.jobs.c.job_id == job["job_id"])
        .values(**changes, event_sequence=sequence, version=version, updated_at=now)
    )
    session.execute(
        insert(s.events).values(
            event_id=new_uuid7(),
            tenant_id=job["tenant_id"],
            job_id=job["job_id"],
            sequence=sequence,
            event_type=event_type,
            reason=reason,
            actor_type="WORKER",
            actor_id=str(worker_id),
            created_at=now,
        )
    )
    session.execute(
        insert(s.audit_records).values(
            audit_id=new_uuid7(),
            tenant_id=job["tenant_id"],
            actor_type="WORKER",
            actor_id=str(worker_id),
            action=event_type.lower(),
            target_type="JOB",
            target_id=str(job["job_id"]),
            before_version=job["version"],
            after_version=version,
            reason=reason,
            created_at=now,
        )
    )
    return version


def _container(session, attempt_id):
    return (
        session.execute(
            select(s.container_identities).where(s.container_identities.c.attempt_id == attempt_id)
        )
        .mappings()
        .one_or_none()
    )


def _proof_valid(attempt, container, proof):
    if proof.startup_nonce != attempt["startup_nonce"]:
        return False
    sequence = attempt["executor_operation_sequence"] or 1
    if proof.executor_operation_sequence < sequence:
        return False
    if proof.proof_type == "NO_CONTAINER":
        return (
            container is None
            and attempt["started_at"] is None
            and proof.tombstone_sequence >= proof.executor_operation_sequence
        )
    return container is not None and (
        proof.container.container_id == container["container_id"]
        and proof.container.runtime_identity_digest == container["runtime_identity_digest"]
        and proof.executor_operation_sequence >= container["executor_create_sequence"]
        and proof.stopped_at >= container["created_at"]
    )


class ExecutionCleanupMixin:
    def _store_cleanup_acknowledgment(
        self, session, receipt, replay, worker_id, callback_id, response
    ):
        if replay is None:
            self._complete_callback(session, receipt, response)
            return
        # _begin_callback locked this exact receipt and checked its payload hash.
        # Only a pending cleanup (202) may be promoted after release.
        result = session.execute(
            update(s.callback_receipts)
            .where(
                s.callback_receipts.c.worker_id == worker_id,
                s.callback_receipts.c.operation_id == "workerReportCleanup",
                s.callback_receipts.c.callback_id == callback_id,
                s.callback_receipts.c.acknowledgment == replay,
            )
            .values(acknowledgment=response)
        )
        if result.rowcount != 1:
            raise RuntimeError("Pending cleanup receipt was not promoted")

    def fail_attempt(
        self, *, worker_id, credential, attempt_id, callback_id, payload_hash, request
    ):
        def operation(session):
            receipt, replay = self._begin_callback(
                session,
                worker_id=worker_id,
                operation_id="workerFailAttempt",
                callback_id=callback_id,
                payload_hash=payload_hash,
            )
            mode, _ = _lock_policy_counters(session, attempt_id)
            worker = self._worker_auth(session, worker_id=worker_id, credential=credential)
            self._require_current_incarnation(
                session, worker, request.authority.worker_incarnation_id, require_reconciling=False
            )
            if (
                request.authority.attempt_id != attempt_id
                or request.authority.worker_id != worker_id
            ):
                _conflict()
            if replay is not None:
                return replay
            if mode == "WRITE_FROZEN":
                _conflict("Writes are frozen")
            job, attempt, lease, allocation, grant = self._authority_rows(
                session, request.authority, worker_id=worker_id
            )
            now = clock_timestamp(session)
            if (
                job["state"] not in {"DISPATCHING", "RUNNING", "PAUSING"}
                or allocation["state"] != "HELD"
                or lease["revoked_at"] is not None
                or lease["expires_at"] <= now
            ):
                _conflict("Failure authority is stale")
            if request.reason_code not in _FAILURE_REASONS[request.failure_class]:
                raise ApplicationError(
                    code="validation_failed", status=422, message="Unknown failure reason"
                )
            observation = request.observation
            container = _container(session, attempt_id)
            if observation.observation_type == "NO_CONTAINER":
                if not _proof_valid(attempt, container, observation.proof):
                    _conflict()
                if request.failure_class == "OOM":
                    _conflict("Failure class contradicts observation")
            else:
                if (
                    container is None
                    and attempt["state"] == "CLAIMED"
                    and attempt["started_at"] is None
                    and job["state"] == "DISPATCHING"
                    and attempt["created_at"]
                    <= observation.observed_at
                    <= now + timedelta(seconds=5)
                ):
                    # Docker may have created the exact container before the
                    # start callback was rejected. Bind the worker's observed
                    # identity while fencing; only a later stopped proof can
                    # release its allocation.
                    session.execute(
                        insert(s.container_identities).values(
                            tenant_id=job["tenant_id"],
                            job_id=job["job_id"],
                            attempt_id=attempt_id,
                            allocation_id=allocation["allocation_id"],
                            startup_nonce=attempt["startup_nonce"],
                            executor_create_sequence=attempt["executor_operation_sequence"] or 1,
                            container_id=observation.container.container_id,
                            runtime_identity_digest=(observation.container.runtime_identity_digest),
                            created_at=observation.observed_at,
                        )
                    )
                    container = _container(session, attempt_id)
                if (
                    container is None
                    or observation.container.container_id != container["container_id"]
                    or observation.container.runtime_identity_digest
                    != container["runtime_identity_digest"]
                ):
                    _conflict()
                runtime_timeout = (
                    request.failure_class == "TIMEOUT"
                    and request.reason_code == "RUNTIME_LIMIT_REACHED"
                )
                if (request.failure_class == "OOM") != observation.oom_killed or (
                    runtime_timeout != observation.runtime_limit_reached
                ):
                    _conflict("Failure class contradicts observation")
            session.execute(
                update(s.attempts)
                .where(s.attempts.c.attempt_id == attempt_id)
                .values(
                    state="STOPPING",
                    failure_class=request.failure_class,
                    failure_reason=request.reason_code,
                    updated_at=now,
                )
            )
            session.execute(
                update(s.attempt_leases)
                .where(s.attempt_leases.c.lease_id == lease["lease_id"])
                .values(revoked_at=now)
            )
            session.execute(
                update(s.attempt_authority_grants)
                .where(s.attempt_authority_grants.c.grant_id == grant["grant_id"])
                .values(ended_at=now)
            )
            session.execute(
                update(s.allocations)
                .where(s.allocations.c.allocation_id == allocation["allocation_id"])
                .values(state="QUARANTINED", quarantined_at=now)
            )
            version = _event(
                session,
                job,
                worker_id,
                now,
                "ATTEMPT_FAILED",
                request.reason_code,
                state="RECOVERING",
                job_fence=job["job_fence"] + 1,
            )
            # Typed observation stays in the immutable event, never raw process output.
            session.execute(
                update(s.result_reservations)
                .where(
                    s.result_reservations.c.attempt_id == attempt_id,
                    s.result_reservations.c.state == "ACTIVE",
                )
                .values(state="ABANDONED")
            )
            session.execute(
                update(s.checkpoint_reservations)
                .where(
                    s.checkpoint_reservations.c.attempt_id == attempt_id,
                    s.checkpoint_reservations.c.state == "RESERVED",
                )
                .values(state="ABANDONED")
            )
            response = json_wire_value(
                dict(
                    callback_id=callback_id,
                    accepted=True,
                    server_time=now,
                    job_state="RECOVERING",
                    job_version=version,
                )
            )
            self._complete_callback(session, receipt, response)
            return response

        return run_transaction(self.session_factory, operation)

    def report_cleanup(
        self, *, worker_id, credential, attempt_id, callback_id, payload_hash, request
    ):
        def operation(session):
            receipt, replay = self._begin_callback(
                session,
                worker_id=worker_id,
                operation_id="workerReportCleanup",
                callback_id=callback_id,
                payload_hash=payload_hash,
            )
            mode, counters = _lock_policy_counters(session, attempt_id)
            self._worker_auth(session, worker_id=worker_id, credential=credential)
            if request.worker_id != worker_id or request.attempt_id != attempt_id:
                _conflict()
            ref = session.execute(
                select(s.attempts.c.job_id).where(s.attempts.c.attempt_id == attempt_id)
            ).scalar_one()
            job = dict(
                session.execute(select(s.jobs).where(s.jobs.c.job_id == ref).with_for_update())
                .mappings()
                .one()
            )
            session.execute(
                select(s.logical_sessions)
                .where(s.logical_sessions.c.job_id == ref)
                .with_for_update()
            ).all()
            attempt = dict(
                session.execute(
                    select(s.attempts)
                    .where(s.attempts.c.attempt_id == attempt_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            lease = (
                session.execute(
                    select(s.attempt_leases)
                    .where(s.attempt_leases.c.attempt_id == attempt_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            allocation = (
                session.execute(
                    select(s.allocations)
                    .where(s.allocations.c.allocation_id == request.allocation_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            lineage = (
                session.execute(
                    select(s.attempt_authority_grants).where(
                        s.attempt_authority_grants.c.attempt_id == attempt_id,
                        s.attempt_authority_grants.c.worker_id == worker_id,
                        s.attempt_authority_grants.c.worker_incarnation_id
                        == request.worker_incarnation_id,
                        s.attempt_authority_grants.c.allocation_id == request.allocation_id,
                        s.attempt_authority_grants.c.job_fence == request.job_fence,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                allocation is None
                or lineage is None
                or attempt["worker_id"] != worker_id
                or attempt["job_fence"] != request.job_fence
                or allocation["attempt_id"] != attempt_id
            ):
                _conflict()
            container = _container(session, attempt_id)
            if not _proof_valid(attempt, container, request.proof):
                _conflict()
            if replay is not None and replay["verified"]:
                return replay
            now = clock_timestamp(session)
            if mode == "WRITE_FROZEN":
                _conflict("Writes are frozen")
            if allocation["state"] == "RELEASED":
                response = json_wire_value(
                    dict(
                        callback_id=callback_id,
                        verified=True,
                        allocation_state="RELEASED",
                        server_time=now,
                    )
                )
                self._store_cleanup_acknowledgment(
                    session, receipt, replay, worker_id, callback_id, response
                )
                return response
            # Cleanup cannot replace failure/revoke linearization or revive an attempt.
            if job["state"] not in _TERMINAL | {"RECOVERING", "CANCELLING"}:
                _conflict("Failure or completion must be committed before cleanup")
            if job["state"] == "RECOVERING" and attempt["failure_class"] is None:
                response = json_wire_value(
                    dict(
                        callback_id=callback_id,
                        verified=False,
                        allocation_state=allocation["state"],
                        server_time=now,
                    )
                )
                if replay is not None:
                    return replay
                self._complete_callback(session, receipt, response)
                return response
            account_locked(session, now)
            session.execute(
                update(s.allocations)
                .where(s.allocations.c.allocation_id == request.allocation_id)
                .values(state="RELEASED", released_at=now, release_reason="VERIFIED_CLEANUP")
            )
            session.execute(
                update(s.allocation_gpu_claims)
                .where(s.allocation_gpu_claims.c.allocation_id == request.allocation_id)
                .values(released_at=now)
            )
            next_state = job["state"]
            changes = {}
            if job["state"] == "CANCELLING":
                next_state = "CANCELLED"
            elif job["state"] == "RECOVERING":
                next_state = "FAILED"
                spec = (
                    session.execute(
                        select(s.template_versions)
                        .join(
                            s.job_specs,
                            (s.job_specs.c.template_id == s.template_versions.c.template_id)
                            & (s.job_specs.c.template_version == s.template_versions.c.version),
                        )
                        .where(s.job_specs.c.job_id == ref)
                    )
                    .mappings()
                    .one()
                )
                if (
                    attempt["failure_class"] == "INFRASTRUCTURE"
                    and job["retry_count"] < job["max_retries"]
                    and spec["restart_safe"]
                    and job["desired_state"] == "RUNNING"
                ):
                    next_state = "RETRY_WAIT"
                    retry = job["retry_count"] + 1
                    changes["retry_count"] = retry
                    jitter_ms = randbelow(1001)
                    session.execute(
                        insert(s.retry_schedules).values(
                            job_id=ref,
                            tenant_id=job["tenant_id"],
                            retry_number=retry,
                            ready_at=now
                            + timedelta(seconds=min(30, 2 ** (retry - 1)), milliseconds=jitter_ms),
                            jitter_milliseconds=jitter_ms,
                            reason="INFRASTRUCTURE",
                        )
                    )
                session.execute(
                    update(s.attempts)
                    .where(s.attempts.c.attempt_id == attempt_id)
                    .values(state="FAILED", ended_at=now, updated_at=now)
                )
            if next_state == "CANCELLED" and job["state"] not in _TERMINAL:
                session.execute(
                    update(s.attempts)
                    .where(s.attempts.c.attempt_id == attempt_id)
                    .values(state="CANCELLED", ended_at=now, updated_at=now)
                )
            terminalized = next_state in _TERMINAL and job["state"] not in _TERMINAL
            _adjust_counters(session, counters, now, outstanding=int(terminalized), active=1)
            rebase_locked(session, now)
            if container is not None:
                session.execute(
                    update(s.container_identities)
                    .where(s.container_identities.c.attempt_id == attempt_id)
                    .values(stopped_at=request.proof.stopped_at, verified_at=now)
                )
            if lease["revoked_at"] is None:
                session.execute(
                    update(s.attempt_leases)
                    .where(s.attempt_leases.c.lease_id == lease["lease_id"])
                    .values(revoked_at=now)
                )
            session.execute(
                update(s.attempt_authority_grants)
                .where(
                    s.attempt_authority_grants.c.attempt_id == attempt_id,
                    s.attempt_authority_grants.c.ended_at.is_(None),
                )
                .values(ended_at=now)
            )
            _event(
                session,
                job,
                worker_id,
                now,
                "ALLOCATION_RELEASED",
                "VERIFIED_CLEANUP",
                state=next_state,
                **changes,
            )
            response = json_wire_value(
                dict(
                    callback_id=callback_id,
                    verified=True,
                    allocation_state="RELEASED",
                    server_time=now,
                )
            )
            self._store_cleanup_acknowledgment(
                session, receipt, replay, worker_id, callback_id, response
            )
            return response

        return run_transaction(self.session_factory, operation)
