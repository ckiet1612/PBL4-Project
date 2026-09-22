from nexa.worker.executor import _immutable_runtime_identity


def test_installation_label_participates_only_when_present_in_runtime_digest() -> None:
    legacy = {"Id": "a" * 64, "Config": {"Labels": {"nexa.managed": "true"}}}
    legacy_identity = _immutable_runtime_identity(legacy)
    assert "nexa.installation_id" not in legacy_identity["labels"]
    labelled = {
        "Id": "a" * 64,
        "Config": {"Labels": {"nexa.managed": "true", "nexa.installation_id": "installation"}},
    }
    new_identity = _immutable_runtime_identity(labelled)
    assert new_identity["labels"]["nexa.installation_id"] == "installation"
    assert new_identity != legacy_identity
