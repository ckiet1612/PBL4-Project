import pytest

from nexa.infrastructure.artifacts.policy import validate_public_artifact_metadata
from nexa.infrastructure.artifacts.store import ArtifactError


def test_public_media_allowlist_is_kind_specific() -> None:
    assert validate_public_artifact_metadata(
        "INPUT", "application/vnd.nexa.cpu-iterative-input+json"
    ) == ("INPUT", "application/vnd.nexa.cpu-iterative-input+json")
    assert (
        validate_public_artifact_metadata("DATASET", "application/vnd.apache.arrow.file")[0]
        == "DATASET"
    )
    assert validate_public_artifact_metadata("MODEL", "application/octet-stream")[0] == "MODEL"

    with pytest.raises(ArtifactError) as unknown:
        validate_public_artifact_metadata("INPUT", "application/x-unknown")
    assert unknown.value.code == "validation_failed"

    with pytest.raises(ArtifactError):
        validate_public_artifact_metadata("CHECKPOINT_FILE", "application/json")
