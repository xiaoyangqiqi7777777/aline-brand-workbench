from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from uuid import UUID

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from backend.infrastructure.storage.errors import (
    ArtifactNotFound,
    ArtifactStorageUnavailable,
    InvalidArtifactReference,
)
from backend.infrastructure.storage.models import (
    ArtifactMetadata,
    ArtifactReference,
    PresignedArtifactUrl,
)

DEFAULT_TTL_SECONDS = 15 * 60
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 60 * 60

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}


class S3ArtifactStorage:
    def __init__(
        self,
        *,
        endpoint_url: str | None,
        access_key: str,
        secret_key: str,
        region_name: str,
        client: BaseClient | None = None,
        default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._default_ttl_seconds = _validate_ttl(default_ttl_seconds)
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
            config=Config(signature_version="s3v4"),
        )

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> S3ArtifactStorage:
        return cls(
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            default_ttl_seconds=default_ttl_seconds,
        )

    def ensure_artifact_exists(self, reference: ArtifactReference) -> ArtifactMetadata:
        _validate_reference(reference)

        try:
            response = self._client.head_object(
                Bucket=reference.bucket,
                Key=reference.object_key,
            )
        except ClientError as exc:
            if _is_not_found(exc):
                raise ArtifactNotFound(reference.artifact_id) from exc
            raise ArtifactStorageUnavailable() from exc
        except BotoCoreError as exc:
            raise ArtifactStorageUnavailable() from exc

        return ArtifactMetadata(
            artifact_id=reference.artifact_id,
            bucket=reference.bucket,
            object_key=reference.object_key,
            content_length=response.get("ContentLength"),
            content_type=response.get("ContentType"),
            etag=response.get("ETag"),
            last_modified=response.get("LastModified"),
        )

    def create_download_url(
        self,
        reference: ArtifactReference,
        *,
        expires_in_seconds: int | None = None,
    ) -> PresignedArtifactUrl:
        _validate_reference(reference)
        ttl = _validate_ttl(expires_in_seconds or self._default_ttl_seconds)
        params = {
            "Bucket": reference.bucket,
            "Key": reference.object_key,
        }
        disposition = _build_content_disposition(reference.filename)
        if disposition:
            params["ResponseContentDisposition"] = disposition
        if reference.content_type:
            params["ResponseContentType"] = reference.content_type

        try:
            url = self._client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=ttl,
                HttpMethod="GET",
            )
        except (BotoCoreError, ClientError) as exc:
            raise ArtifactStorageUnavailable("could not create artifact URL") from exc

        return PresignedArtifactUrl(
            artifact_id=reference.artifact_id,
            url=url,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
            expires_in_seconds=ttl,
        )


def _validate_reference(reference: ArtifactReference) -> None:
    _validate_uuid(reference.artifact_id)
    _validate_bucket(reference.bucket)
    _validate_object_key(reference.object_key)


def _validate_uuid(value: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as exc:
        raise InvalidArtifactReference("artifact_id must be a UUID string") from exc


def _validate_bucket(value: str) -> None:
    if not _BUCKET_RE.fullmatch(value):
        raise InvalidArtifactReference("bucket must be a valid S3 bucket name")


def _validate_object_key(value: str) -> None:
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        raise InvalidArtifactReference("object_key must be a safe relative S3 key")

    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidArtifactReference("object_key cannot contain empty or relative segments")


def _validate_ttl(value: int) -> int:
    if value < MIN_TTL_SECONDS or value > MAX_TTL_SECONDS:
        raise InvalidArtifactReference(
            f"expires_in_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}"
        )
    return value


def _build_content_disposition(filename: str | None) -> str | None:
    if not filename:
        return None

    safe_name = filename.replace("\r", "").replace("\n", "")
    safe_name = safe_name.rsplit("/", maxsplit=1)[-1].rsplit("\\", maxsplit=1)[-1]
    if not safe_name:
        return None

    return f"inline; filename*=UTF-8''{quote(safe_name)}"


def _is_not_found(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code")
    return code in _NOT_FOUND_CODES
