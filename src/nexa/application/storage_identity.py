"""Bind the artifact store to the identity this deployment recorded (B14-OBS-01)."""

from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nexa.infrastructure.artifacts.store import ArtifactError, FilesystemArtifactStore
from nexa.infrastructure.persistence import schema as s
from nexa.infrastructure.persistence.transactions import run_transaction

_LOG = logging.getLogger(__name__)
_SINGLETON = "artifacts"


def bind_artifact_store(session_factory, store: FilesystemArtifactStore) -> bool:
    """Bind `store` when its root records this deployment's identity; return whether it did.

    The first start (the upgrade to migration 0022 included) records a new identity in
    PostgreSQL and at the root, or adopts the identity already at the root, unless
    PostgreSQL references committed blobs while the store holds none: that is an empty or
    wrong volume. An unbound store never reports a blob missing and never passes worker
    readiness, so restore answers 503 and nothing dispatches until the right volume is
    mounted and the API restarts.
    """

    def recorded(session):
        return session.execute(
            select(s.artifact_store_identity.c.store_id).where(
                s.artifact_store_identity.c.singleton_key == _SINGLETON
            )
        ).scalar_one_or_none()

    def committed_blobs(session):
        return session.execute(
            select(exists().where(s.artifacts.c.state.in_(("COMMITTED", "DELETING"))))
        ).scalar_one()

    try:
        at_root = store.read_identity()
        expected, referenced = run_transaction(
            session_factory, lambda session: (recorded(session), committed_blobs(session))
        )
        if expected is None:
            if referenced and not store.has_committed_blobs():
                _LOG.warning(
                    "artifact store not bound: PostgreSQL references committed blobs but "
                    "the store is empty"
                )
                return False
            candidate = store.create_identity(uuid4()) if at_root is None else at_root

            def record(session):
                session.execute(
                    pg_insert(s.artifact_store_identity)
                    .values(singleton_key=_SINGLETON, store_id=candidate)
                    .on_conflict_do_nothing(index_elements=["singleton_key"])
                )
                return recorded(session)

            expected = run_transaction(session_factory, record)
            at_root = store.read_identity()
    except ArtifactError:
        _LOG.warning("artifact store not bound: storage identity is unavailable")
        return False
    if at_root != expected:
        _LOG.warning("artifact store not bound: storage identity does not match this deployment")
        return False
    store.bind_identity(expected)
    return True
