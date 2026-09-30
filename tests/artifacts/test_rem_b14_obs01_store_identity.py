"""Remediation B14-OBS-01: a missing blob is proven only inside the bound, healthy store.

Restore marks a checkpoint CORRUPT, one way, when the store reports ``not_found``. Before
the fix any missing file under a present ``committed`` directory was ``not_found``, so a
wrong or freshly re-created volume turned every checkpoint CORRUPT. The store now keeps an
identity file at its root; ``not_found`` requires the identity the deployment bound to be
present and intact at the moment the blob is missing. A missing root, a missing
``committed`` directory, a tree without an identity, another store's identity or an
unreadable identity is ``storage_unavailable``: temporary and never evidence about a blob.
"""

import hashlib
import os
import uuid

import pytest

from nexa.infrastructure.artifacts.store import ArtifactError, FilesystemArtifactStore

CONTENT = b"checkpoint bytes"


def _commit(store):
    staged = store.begin_staging(
        "rem-obs01", len(CONTENT), "sha256:" + hashlib.sha256(CONTENT).hexdigest(), "x/y"
    )
    store.append(staged, CONTENT)
    return store.commit_blob(staged).blob_key


def _bound_store(tmp_path):
    root = tmp_path / "artifacts"
    store = FilesystemArtifactStore(root)
    store_id = store.create_identity(uuid.uuid4())
    store.bind_identity(store_id)
    return root, store, _commit(store)


def _code(call):
    with pytest.raises(ArtifactError) as caught:
        call()
    return caught.value.code


def _unlink_blob(root, key):
    (root / "committed" / key.split("/", 1)[1]).unlink()


def test_a_single_missing_blob_in_the_bound_store_is_not_found(tmp_path):
    root, store, key = _bound_store(tmp_path)
    store.check_readiness(critical_watermark_percent=100)
    with store.open(key) as reader:
        assert reader.read() == CONTENT
    _unlink_blob(root, key)
    assert _code(lambda: store.open(key)) == "not_found"
    assert _code(lambda: store.inspect(key)) == "not_found"


def test_an_unbound_store_never_proves_a_blob_missing(tmp_path):
    root, _, key = _bound_store(tmp_path)
    _unlink_blob(root, key)
    unbound = FilesystemArtifactStore(root)
    assert unbound.read_identity() is not None
    assert _code(lambda: unbound.open(key)) == "storage_unavailable"
    assert (
        _code(lambda: unbound.check_readiness(critical_watermark_percent=100))
        == "storage_unavailable"
    )


def _root_missing(root, moved):
    root.rename(moved)


def _committed_missing(root, moved):
    (root / "committed").rename(moved)


def _fresh_tree(root, moved):
    # A new volume mounted at the same path; the tree is re-created without an identity.
    root.rename(moved)
    FilesystemArtifactStore(root)


def _foreign_store(root, moved):
    root.rename(moved)
    FilesystemArtifactStore(root).create_identity(uuid.uuid4())


def _identity_symlink(root, moved):
    os.replace(root / "store-identity", moved)
    (root / "store-identity").symlink_to(moved)


def _identity_garbage(root, moved):
    os.replace(root / "store-identity", moved)
    (root / "store-identity").write_bytes(b"not an identity\n")


@pytest.mark.parametrize(
    "damage",
    [
        _root_missing,
        _committed_missing,
        _fresh_tree,
        _foreign_store,
        _identity_symlink,
        _identity_garbage,
    ],
)
def test_an_unverifiable_store_is_unavailable_not_missing(tmp_path, damage):
    root, store, key = _bound_store(tmp_path)
    damage(root, tmp_path / "moved")
    blob = root / "committed" / key.split("/", 1)[1]
    if blob.exists():
        blob.unlink()
    assert _code(lambda: store.open(key)) == "storage_unavailable"
    assert _code(lambda: store.inspect(key)) == "storage_unavailable"
    if damage is not _root_missing:
        # The worker readiness probe fails closed too, so nothing dispatches meanwhile.
        assert (
            _code(lambda: store.check_readiness(critical_watermark_percent=100))
            == "storage_unavailable"
        )


def test_the_identity_is_created_once_and_never_replaced(tmp_path):
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    assert store.read_identity() is None
    first, second = uuid.uuid4(), uuid.uuid4()
    assert store.create_identity(first) == first
    assert store.create_identity(second) == first
    assert store.read_identity() == first
    marker = tmp_path / "artifacts" / "store-identity"
    assert marker.read_bytes() == f"nexa-artifact-store v1 {first}\n".encode()
    assert marker.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in (tmp_path / "artifacts").iterdir() if p.name.startswith(".")] == []


def test_committed_content_is_reported_for_legacy_adoption(tmp_path):
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    assert store.has_committed_blobs() is False
    _commit(store)
    assert store.has_committed_blobs() is True
