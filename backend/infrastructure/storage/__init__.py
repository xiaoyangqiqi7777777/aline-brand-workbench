"""Object storage adapters."""
from backend.infrastructure.storage.errors import (
    ArtifactNotFound,
    ArtifactStorageError,
    ArtifactStorageUnavailable,
    InvalidArtifactReference,
)
from backend.infrastructure.storage.models import (
    ArtifactMetadata,
    ArtifactReference,
    PresignedArtifactUrl,
)
from backend.infrastructure.storage.s3 import S3ArtifactStorage
from backend.infrastructure.storage.service import ArtifactStorage

__all__ = [
    "ArtifactMetadata",
    "ArtifactNotFound",
    "ArtifactReference",
    "ArtifactStorage",
    "ArtifactStorageError",
    "ArtifactStorageUnavailable",
    "InvalidArtifactReference",
    "PresignedArtifactUrl",
    "S3ArtifactStorage",
]
