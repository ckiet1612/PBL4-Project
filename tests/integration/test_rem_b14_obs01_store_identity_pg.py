"""Remediation B14-OBS-01 on PostgreSQL: only a verified store turns a missing blob CORRUPT.

Restore marks a checkpoint CORRUPT one way when its blob is missing. Before the fix a
missing file under any present ``committed`` directory counted, so a wrong or re-created
volume (and an API restarted on an empty mount, whose store re-creates the tree) marked
every checkpoint lost. The API now binds the store whose root records the identity
PostgreSQL recorded (migration 0022). Real filesystem damage, no mocks: a missing root, a
missing ``committed`` directory, a fresh tree and another store's tree fail the claim
closed with 503 and leave every checkpoint restorable; a single blob missing from the
verified store is still CORRUPT with fallback.
"""

import shutil
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.api.test_http_contract import _client
from tests.integration.test_checkpoint_b14 import CheckpointFixture
from tests.integration.test_checkpoint_restore_b14 import (
    _blob_path,
    _claim,
    _commit,
    _corruptions,
    _next_attempt,
    _restore_events,
)

pytestmark = pytest.mark.postgres

# Literal on purpose: the pre-fix tree must import this module to run red.
IDENTITY = "store-identity"


@pytest.fixture
def restore(migrated_postgres_engine, tmp_path):
    with _client(migrated_postgres_engine, tmp_path) as client:
        fixture = CheckpointFixture(migrated_postgres_engine, client, label="rem-obs01")
        yield fixture, tmp_path / "artifacts"


def _two_checkpoints(fixture):
    older = _commit(fixture, step=10, accumulator=7)
    newest = _commit(fixture, step=20, accumulator=8)
    _next_attempt(fixture)
    return older, newest


def _recorded(engine):
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT store_id FROM artifact_store_identity WHERE singleton_key = 'artifacts'")
        ).scalar_one_or_none()


def _assert_fails_closed(fixture):
    offline = _claim(fixture)
    assert offline.status_code == 503, offline.text
    rows = fixture.rows()
    assert rows["attempt"]["execution_context"] is None
    assert rows["attempt"]["state"] == "CREATED"
    assert _corruptions(fixture) == {}
    assert _restore_events(fixture) == []


def _assert_restores(fixture, newest):
    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    restored = claimed.json()["execution_context"]["restore_checkpoint"]
    assert restored["record"] == newest["record"]
    assert _corruptions(fixture) == {}


def _tree(root, identity=None):
    for directory in (root, root / "staging", root / "committed"):
        directory.mkdir(mode=0o700)
    if identity is not None:
        (root / IDENTITY).write_bytes(f"nexa-artifact-store v1 {identity}\n".encode())


def _root_missing(root, aside):
    root.rename(aside)
    return lambda: aside.rename(root)


def _committed_missing(root, aside):
    (root / "committed").rename(aside)
    return lambda: aside.rename(root / "committed")


def _fresh_tree(root, aside):
    root.rename(aside)
    _tree(root)
    return lambda: (shutil.rmtree(root), aside.rename(root))


def _foreign_store(root, aside):
    root.rename(aside)
    _tree(root, identity=uuid.uuid4())
    return lambda: (shutil.rmtree(root), aside.rename(root))


@pytest.mark.parametrize("damage", [_root_missing, _committed_missing, _fresh_tree, _foreign_store])
def test_an_unverifiable_store_fails_the_claim_closed_and_keeps_checkpoints(
    restore, tmp_path, damage
):
    fixture, root = restore
    _, newest = _two_checkpoints(fixture)
    undo = damage(root, tmp_path / "aside")
    _assert_fails_closed(fixture)
    undo()
    _assert_restores(fixture, newest)


def test_a_blob_missing_from_the_verified_store_is_corrupt_and_restore_falls_back(restore):
    fixture, _ = restore
    older, newest = _two_checkpoints(fixture)
    _blob_path(fixture, newest["state_artifact"]["artifact_id"]).unlink()
    claimed = _claim(fixture)
    assert claimed.status_code == 200, claimed.text
    restored = claimed.json()["execution_context"]["restore_checkpoint"]
    assert restored["record"] == older["record"]
    assert _corruptions(fixture) == {
        uuid.UUID(newest["record"]["checkpoint_id"]): "CHECKPOINT_BLOB_MISSING"
    }
    assert _restore_events(fixture) == [
        ("CHECKPOINT_CORRUPT", "CHECKPOINT_BLOB_MISSING"),
        ("CHECKPOINT_RESTORE_SELECTED", "CHECKPOINT_RESTORED"),
    ]


def test_an_api_restarted_on_an_empty_volume_neither_binds_nor_marks(
    restore, migrated_postgres_engine, tmp_path
):
    engine = migrated_postgres_engine
    fixture, root = restore
    _, newest = _two_checkpoints(fixture)
    recorded = _recorded(engine)
    assert recorded is not None
    aside = tmp_path / "aside"
    root.rename(aside)
    # The restarted store re-creates an empty tree at the configured root.
    with _client(engine, tmp_path) as restarted:
        assert (root / "committed").is_dir()
        assert not (root / IDENTITY).exists()
        fixture.client = restarted
        _assert_fails_closed(fixture)
    shutil.rmtree(root)
    aside.rename(root)
    with _client(engine, tmp_path) as remounted:
        fixture.client = remounted
        _assert_restores(fixture, newest)
    assert _recorded(engine) == recorded
    with engine.begin() as connection, pytest.raises(IntegrityError) as caught:
        connection.execute(text("UPDATE artifact_store_identity SET store_id = gen_random_uuid()"))
    assert caught.value.orig.sqlstate == "23514"


def test_the_first_start_after_the_upgrade_adopts_only_a_populated_store(
    restore, migrated_postgres_engine, clean_postgres_database, tmp_path
):
    engine = migrated_postgres_engine
    fixture, root = restore
    _, newest = _two_checkpoints(fixture)
    # A deployment from before migration 0022: no identity in PostgreSQL or at the root.
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", clean_postgres_database.replace("%", "%%"))
    command.downgrade(config, "20260928_0021")
    command.upgrade(config, "head")
    (root / IDENTITY).unlink(missing_ok=True)
    aside = tmp_path / "aside"
    root.rename(aside)
    with _client(engine, tmp_path) as empty:
        fixture.client = empty
        _assert_fails_closed(fixture)
    assert _recorded(engine) is None
    assert not (root / IDENTITY).exists()
    shutil.rmtree(root)
    aside.rename(root)
    with _client(engine, tmp_path) as upgraded:
        fixture.client = upgraded
        _assert_restores(fixture, newest)
    adopted = _recorded(engine)
    assert adopted is not None
    assert (root / IDENTITY).read_bytes() == f"nexa-artifact-store v1 {adopted}\n".encode()


def test_the_recorded_identity_is_insert_only(restore, migrated_postgres_engine):
    with migrated_postgres_engine.begin() as connection, pytest.raises(IntegrityError) as caught:
        connection.execute(text("DELETE FROM artifact_store_identity"))
    assert caught.value.orig.sqlstate == "23514"
