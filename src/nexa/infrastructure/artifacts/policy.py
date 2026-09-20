from __future__ import annotations

import re
from enum import StrEnum

from nexa.infrastructure.artifacts.store import ArtifactError


class PublicArtifactKind(StrEnum):
    INPUT = "INPUT"
    DATASET = "DATASET"
    MODEL = "MODEL"


# This is intentionally narrow. Template versions may add an allowlisted type later;
# the upload core never treats a client-declared media type as a capability grant.
PUBLIC_MEDIA_TYPES: dict[PublicArtifactKind, frozenset[str]] = {
    PublicArtifactKind.INPUT: frozenset(
        {
            "application/vnd.nexa.cpu-iterative-input+json",
            "application/json",
        }
    ),
    PublicArtifactKind.DATASET: frozenset(
        {
            "application/vnd.apache.arrow.file",
            "application/json",
        }
    ),
    # B07 permits only opaque binary model fixtures; a frozen template must still
    # validate its format before execution.
    PublicArtifactKind.MODEL: frozenset({"application/octet-stream"}),
}

_MEDIA_TYPE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+"
    r"(?:;[A-Za-z0-9!#$&^_.+-]+=[A-Za-z0-9!#$&^_.+\- =]+)?$"
)


def validate_public_artifact_metadata(kind: str, media_type: str) -> tuple[str, str]:
    try:
        normalized_kind = PublicArtifactKind(kind)
    except ValueError:
        raise ArtifactError(
            "validation_failed", "Artifact kind is not allowed for public upload"
        ) from None
    if (
        not isinstance(media_type, str)
        or not _MEDIA_TYPE.fullmatch(media_type)
        or len(media_type) > 127
    ):
        raise ArtifactError("validation_failed", "Artifact media type is invalid")
    if media_type not in PUBLIC_MEDIA_TYPES[normalized_kind]:
        raise ArtifactError(
            "validation_failed", "Artifact media type is not allowed for this artifact kind"
        )
    return normalized_kind.value, media_type


__all__ = ["PUBLIC_MEDIA_TYPES", "PublicArtifactKind", "validate_public_artifact_metadata"]
