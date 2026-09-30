"""Remediation B15-R05: the server proves an inherited checkpoint's provenance.

A manual retry may restore a checkpoint of another Job only through an explicit
same-tenant ``MANUAL_RETRY`` CheckpointReference. The worker cannot re-derive the source
Job's session, so the server is the party that proves the source: the retry request and
the claim of every Attempt of the new Job check that the checkpoint's owner is the Job
itself or an ancestor on its ``retry_of_job_id`` chain, in the same tenant, with the same
spec (so template, adapter and image). Before the fix neither checked the chain: a same-spec
checkpoint of an unrelated Job was referenced and restored, and a tampered reference to a
checkpoint of another spec or image marked that other Job's checkpoint CORRUPT, one way.
An unproven source is now an incompatible candidate: an exact event, restart-safe fallback
to input, and no mark on a checkpoint the Job does not own.
"""

import hashlib
from uuid import UUID

import pytest
import rfc8785
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.ids import new_uuid7
from tests.integration.test_checkpoint_corruption_b14 import _committed_checkpoint
from tests.integration.test_control_b15 import (
    _checkpoint_id,
    _fail_to_terminal,
    _first_attempt,
    _publish_checkpoint,
    _retry_body,
    _retry_control,
    _spec_row,
)

pytestmark = pytest.mark.postgres

UNPROVEN = [
    ("CHECKPOINT_INCOMPATIBLE", "CHECKPOINT_PROVENANCE_MISMATCH"),
    ("CHECKPOINT_FALLBACK_TO_INPUT", "CHECKPOINT_FALLBACK_TO_INPUT"),
]


def _failed_source(control):
    """The fixture Job publishes one checkpoint and ends FAILED; return that checkpoint."""
    published = _publish_checkpoint(control.job)
    assert published.status_code == 201, published.text
    _fail_to_terminal(control)
    return _checkpoint_id(control.engine, control.job.job_id), published.json()


def _spec_checksum(spec):
    return "sha256:" + hashlib.sha256(rfc8785.dumps(spec)).hexdigest()


def _other_job(control, *, state, retry_of_job_id=None, spec_changes=None, template_version=None):
    """A Job of the fixture tenant and submitter with the fixture Job's spec, or a variant.

    JobSpec rows are immutable, so the variant is written once, at insert.
    """
    source = dict(_spec_row(control.engine, control.job.job_id))
    spec = dict(source["canonical_spec"], **(spec_changes or {}))
    if template_version is not None:
        spec["template_version"] = template_version
    job_id = new_uuid7()
    with control.engine.begin() as connection:
        connection.execute(
            insert(s.jobs),
            {
                "job_id": job_id,
                "tenant_id": control.graph["tenant_id"],
                "submitter_user_id": control.graph["user_id"],
                "state": state,
                "desired_state": "RUNNING",
                "version": 1,
                "job_fence": 0,
                "event_sequence": 0,
                "checkpoint_sequence": 0,
                "retry_count": 0,
                "max_retries": 2,
                "retry_of_job_id": retry_of_job_id,
                "base_priority": 1,
                "ready_sequence": int(job_id.int & 0x7FFFFFFF),
            },
        )
        connection.execute(
            insert(s.job_specs),
            dict(
                source,
                job_id=job_id,
                canonical_spec=spec,
                spec_checksum=_spec_checksum(spec)
                if spec != source["canonical_spec"]
                else source["spec_checksum"],
                template_version=spec["template_version"],
            ),
        )
        connection.execute(
            insert(s.logical_sessions),
            {"session_id": new_uuid7(), "tenant_id": control.graph["tenant_id"], "job_id": job_id},
        )
    return job_id


def _second_template_version(control):
    """Version 2 of the fixture template: another adapter version and image digest."""
    source = _spec_row(control.engine, control.job.job_id)
    with control.engine.begin() as connection:
        v1 = dict(
            connection.execute(
                select(s.template_versions).where(
                    s.template_versions.c.template_id == source["template_id"],
                    s.template_versions.c.version == source["template_version"],
                )
            )
            .mappings()
            .one()
        )
        image = "sha256:" + "e" * 64
        v1.update(
            version=source["template_version"] + 1,
            image_digest=image,
            capability_requirements={**v1["capability_requirements"], "image_digest": image},
        )
        connection.execute(insert(s.template_versions).values(**v1))
    return source["template_version"] + 1


def _reference(control, checkpoint_id, target_job_id, reason="MANUAL_RETRY"):
    with control.engine.begin() as connection:
        connection.execute(
            insert(s.checkpoint_references).values(
                tenant_id=control.tenant_id,
                source_checkpoint_id=checkpoint_id,
                target_job_id=target_job_id,
                reason=reason,
            )
        )


def _claim(control, job_id):
    _first_attempt(control, job_id)
    claimed = control.job.post("/claim", {"authority": control.job.authority})
    assert claimed.status_code == 200, claimed.text
    return claimed.json()["execution_context"]["restore_checkpoint"]


def _restore_events(engine, job_id):
    with engine.connect() as connection:
        return [
            (row.event_type, row.reason)
            for row in connection.execute(
                select(s.events.c.event_type, s.events.c.reason)
                .where(s.events.c.job_id == job_id)
                .order_by(s.events.c.sequence)
            )
            if row.event_type.startswith("CHECKPOINT_")
        ]


def _corruptions(engine):
    with engine.connect() as connection:
        return dict(
            connection.execute(
                select(
                    s.checkpoint_corruptions.c.checkpoint_id,
                    s.checkpoint_corruptions.c.reason_code,
                )
            ).all()
        )


def _count(engine, table):
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def test_retry_refuses_a_same_spec_checkpoint_of_a_job_outside_the_retry_chain(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _retry_control(engine, tmp_path, "rem-r05-retry-chain") as control:
        checkpoint_id, _ = _failed_source(control)
        # Another FAILED Job of the same tenant, submitter and exact spec; never retried
        # from the checkpoint's owner, so the owner is not its ancestor.
        unrelated = _other_job(control, state="FAILED")
        assert (
            _spec_row(engine, unrelated)["spec_checksum"]
            == _spec_row(engine, control.job.job_id)["spec_checksum"]
        )
        jobs, references = _count(engine, s.jobs), _count(engine, s.checkpoint_references)
        before = control.counter_values()
        response = control.control("retry", job_id=unrelated, body=_retry_body(checkpoint_id))
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "infeasible_request"
        assert response.json()["message"] == "Checkpoint is not a compatible committed checkpoint"
        assert (_count(engine, s.jobs), _count(engine, s.checkpoint_references)) == (
            jobs,
            references,
        )
        assert control.counter_values() == before
        # The same Job may still be retried from input.
        plain = control.control("retry", job_id=unrelated, body=_retry_body())
        assert plain.status_code == 202, plain.text


def test_a_retry_of_a_retry_inherits_and_restores_the_ancestor_checkpoint(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _retry_control(engine, tmp_path, "rem-r05-grandparent") as control:
        checkpoint_id, published = _failed_source(control)
        first = control.control("retry", body=_retry_body(checkpoint_id))
        assert first.status_code == 202, first.text
        child = UUID(first.json()["job_id"])
        with engine.begin() as connection:
            connection.execute(
                update(s.jobs).where(s.jobs.c.job_id == child).values(state="FAILED")
            )
        # The checkpoint's owner is the child's parent: an ancestor two links up.
        second = control.control("retry", job_id=child, body=_retry_body(checkpoint_id))
        assert second.status_code == 202, second.text
        grandchild = UUID(second.json()["job_id"])
        restore = _claim(control, grandchild)
        assert restore["record"] == published
        assert _restore_events(engine, grandchild) == [
            ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED")
        ]
        assert _corruptions(engine) == {}


def _unrelated(control, checkpoint_id):
    job_id = _other_job(control, state="QUEUED")
    _reference(control, checkpoint_id, job_id)
    return job_id


def _other_spec(control, checkpoint_id):
    source = _spec_row(control.engine, control.job.job_id)["canonical_spec"]
    parameters = dict(source["parameters"], iterations=source["parameters"]["iterations"] + 1)
    job_id = _other_job(
        control,
        state="QUEUED",
        retry_of_job_id=control.job.job_id,
        spec_changes={"parameters": parameters},
    )
    _reference(control, checkpoint_id, job_id)
    return job_id


def _other_image(control, checkpoint_id):
    job_id = _other_job(
        control,
        state="QUEUED",
        retry_of_job_id=control.job.job_id,
        template_version=_second_template_version(control),
    )
    _reference(control, checkpoint_id, job_id)
    return job_id


def _other_reason(control, checkpoint_id):
    job_id = _other_job(control, state="QUEUED", retry_of_job_id=control.job.job_id)
    _reference(control, checkpoint_id, job_id, reason="IMPORTED")
    return job_id


@pytest.mark.parametrize("tamper", [_unrelated, _other_spec, _other_image, _other_reason])
def test_claim_never_restores_or_marks_an_unproven_inherited_checkpoint(
    migrated_postgres_engine, tmp_path, tamper
):
    engine = migrated_postgres_engine
    label = "rem-r05-" + tamper.__name__.strip("_").replace("_", "-")
    with _retry_control(engine, tmp_path, label) as control:
        checkpoint_id, _ = _failed_source(control)
        # A reference no manual retry could write: only a tampered database has it.
        target = tamper(control, checkpoint_id)
        restore = _claim(control, target)
        assert restore is None
        assert _restore_events(engine, target) == UNPROVEN
        # The checkpoint belongs to another Job: never marked from here.
        assert _corruptions(engine) == {}
        with engine.connect() as connection:
            owner = connection.execute(
                select(s.checkpoints.c.job_id, s.checkpoints.c.state).where(
                    s.checkpoints.c.checkpoint_id == checkpoint_id
                )
            ).one()
        assert tuple(owner) == (control.job.job_id, "COMMITTED")


def test_claim_marks_an_inherited_checkpoint_whose_bytes_changed(
    migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    with _retry_control(engine, tmp_path, "rem-r05-fake-checksum") as control:
        checkpoint_id, _ = _failed_source(control)
        response = control.control("retry", body=_retry_body(checkpoint_id))
        assert response.status_code == 202, response.text
        target = UUID(response.json()["job_id"])
        with engine.connect() as connection:
            key = connection.execute(
                select(s.artifacts.c.blob_key)
                .join(
                    s.artifact_references,
                    s.artifact_references.c.artifact_id == s.artifacts.c.artifact_id,
                )
                .where(
                    s.artifact_references.c.owner_id == checkpoint_id,
                    s.artifact_references.c.purpose == "CHECKPOINT_FILE",
                )
            ).scalar_one()
        path = control.store._path_for_key(key)
        path.chmod(0o600)
        path.write_bytes(path.read_bytes()[:-1] + b" ")
        # A proven source whose committed bytes no longer match is corrupt for every Job.
        assert _claim(control, target) is None
        assert _restore_events(engine, target) == [
            ("CHECKPOINT_CORRUPT", "CHECKPOINT_CHECKSUM_MISMATCH"),
            ("CHECKPOINT_FALLBACK_TO_INPUT", "CHECKPOINT_FALLBACK_TO_INPUT"),
        ]
        assert _corruptions(engine) == {checkpoint_id: "CHECKPOINT_CHECKSUM_MISMATCH"}


def test_a_reference_across_tenants_cannot_be_stored(migrated_postgres_engine, tmp_path):
    engine = migrated_postgres_engine
    with _retry_control(engine, tmp_path, "rem-r05-foreign") as control:
        checkpoint_id, _ = _failed_source(control)
        response = control.control("retry", body=_retry_body(checkpoint_id))
        assert response.status_code == 202, response.text
        target = UUID(response.json()["job_id"])
        foreign_graph, foreign_id = _committed_checkpoint(engine, "rem-r05-foreign-tenant")
        for tenant_id in (control.tenant_id, foreign_graph["tenant_id"]):
            with engine.begin() as connection, pytest.raises(IntegrityError) as caught:
                connection.execute(
                    insert(s.checkpoint_references).values(
                        tenant_id=tenant_id,
                        source_checkpoint_id=foreign_id,
                        target_job_id=target,
                        reason="MANUAL_RETRY",
                    )
                )
            assert caught.value.orig.sqlstate == "23503"
