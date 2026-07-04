from typing import Protocol

from backend.infrastructure.storage.models import (
    ArtifactMetadata,
    ArtifactReference,
    PresignedArtifactUrl,
)


class ArtifactStorage(Protocol):
    def ensure_artifact_exists(self, reference: ArtifactReference) -> ArtifactMetadata:
        """Return metadata for an existing artifact or raise ArtifactNotFound."""

    def create_download_url(
        self,
        reference: ArtifactReference,
        *,
        expires_in_seconds: int | None = None,
    ) -> PresignedArtifactUrl:
        """Create a short-lived URL that can be returned by a business API."""
