from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    bucket: str
    object_key: str
    filename: str | None = None
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    artifact_id: str
    bucket: str
    object_key: str
    content_length: int | None
    content_type: str | None
    etag: str | None
    last_modified: datetime | None


@dataclass(frozen=True, slots=True)
class PresignedArtifactUrl:
    artifact_id: str
    url: str
    expires_at: datetime
    expires_in_seconds: int
    method: str = "GET"
