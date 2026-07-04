from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from backend.infrastructure.storage import (
    ArtifactNotFound,
    ArtifactReference,
    InvalidArtifactReference,
    S3ArtifactStorage,
)


def test_create_download_url_returns_short_lived_get_url() -> None:
    reference = _artifact_reference()
    storage = S3ArtifactStorage(
        endpoint_url="http://minio.test",
        access_key="test",
        secret_key="test",
        region_name="us-east-1",
    )

    result = storage.create_download_url(reference)
    parsed = urlparse(result.url)
    query = parse_qs(parsed.query)

    assert result.artifact_id == reference.artifact_id
    assert result.method == "GET"
    assert result.expires_in_seconds == 900
    assert result.expires_at > datetime.now(UTC)
    assert parsed.path.endswith(f"/{reference.bucket}/{reference.object_key}")
    assert query["X-Amz-Expires"] == ["900"]
    assert "response-content-disposition" in query
    assert query["response-content-type"] == ["image/png"]


def test_ensure_artifact_exists_returns_s3_metadata() -> None:
    reference = _artifact_reference()
    client = _s3_client()
    storage = S3ArtifactStorage(
        endpoint_url=None,
        access_key="unused",
        secret_key="unused",
        region_name="us-east-1",
        client=client,
    )
    last_modified = datetime(2026, 1, 1, tzinfo=UTC)

    with Stubber(client) as stubber:
        stubber.add_response(
            "head_object",
            {
                "ContentLength": 42,
                "ContentType": "image/png",
                "ETag": '"abc123"',
                "LastModified": last_modified,
            },
            {"Bucket": reference.bucket, "Key": reference.object_key},
        )

        metadata = storage.ensure_artifact_exists(reference)

    assert metadata.artifact_id == reference.artifact_id
    assert metadata.content_length == 42
    assert metadata.content_type == "image/png"
    assert metadata.etag == '"abc123"'
    assert metadata.last_modified == last_modified


def test_ensure_artifact_exists_maps_missing_objects() -> None:
    reference = _artifact_reference()
    client = _s3_client()
    storage = S3ArtifactStorage(
        endpoint_url=None,
        access_key="unused",
        secret_key="unused",
        region_name="us-east-1",
        client=client,
    )

    with Stubber(client) as stubber:
        stubber.add_client_error(
            "head_object",
            service_error_code="404",
            http_status_code=404,
            expected_params={"Bucket": reference.bucket, "Key": reference.object_key},
        )

        with pytest.raises(ArtifactNotFound):
            storage.ensure_artifact_exists(reference)


@pytest.mark.parametrize(
    ("artifact_id", "object_key", "ttl"),
    [
        ("not-a-uuid", "artifacts/project/logo.png", 900),
        (str(uuid4()), "../logo.png", 900),
        (str(uuid4()), "artifacts//logo.png", 900),
        (str(uuid4()), "artifacts/project/logo.png", 59),
        (str(uuid4()), "artifacts/project/logo.png", 3601),
    ],
)
def test_create_download_url_rejects_unsafe_references(
    artifact_id: str,
    object_key: str,
    ttl: int,
) -> None:
    reference = ArtifactReference(
        artifact_id=artifact_id,
        bucket="brand-studio",
        object_key=object_key,
    )
    storage = S3ArtifactStorage(
        endpoint_url="http://minio.test",
        access_key="test",
        secret_key="test",
        region_name="us-east-1",
    )

    with pytest.raises(InvalidArtifactReference):
        storage.create_download_url(reference, expires_in_seconds=ttl)


def _artifact_reference() -> ArtifactReference:
    return ArtifactReference(
        artifact_id=str(uuid4()),
        bucket="brand-studio",
        object_key="artifacts/project/logo.png",
        filename="logo.png",
        content_type="image/png",
    )


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url="http://minio.test",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
        config=Config(signature_version="s3v4"),
    )
