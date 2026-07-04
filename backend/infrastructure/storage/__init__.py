"""Object storage adapters."""
from backend.infrastructure.storage.errors import (
    ArtifactNotFound,
    ArtifactStorageError,
    ArtifactStorageUnavailable,
    InvalidArtifactReference,
)
from backend.infrastructure.storage.models import (
    ArtifactCleanupResult,
    ArtifactMetadata,
    ArtifactReference,
    ArtifactUpload,
    PresignedArtifactUrl,
    StoredArtifact,
)
from backend.infrastructure.storage.s3 import S3ArtifactStorage
from backend.infrastructure.storage.service import ArtifactStorage

__all__ = [
    "ArtifactCleanupResult",
    "ArtifactMetadata",
    "ArtifactNotFound",
    "ArtifactReference",
    "ArtifactStorage",
    "ArtifactStorageError",
    "ArtifactStorageUnavailable",
    "ArtifactUpload",
    "InvalidArtifactReference",
    "PresignedArtifactUrl",
    "S3ArtifactStorage",
    "StoredArtifact",
]
