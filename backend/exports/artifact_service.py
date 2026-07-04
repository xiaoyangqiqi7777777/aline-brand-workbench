from __future__ import annotations

import re

from backend.exports.models import (
    ExportArtifactTarget,
    ExportFormat,
    ExportRequest,
    StoredExportArtifact,
)
from backend.exports.service import ExportRenderer
from backend.infrastructure.storage import (
    ArtifactReference,
    ArtifactStorage,
    ArtifactUpload,
)

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ExportArtifactService:
    def __init__(
        self,
        *,
        storage: ArtifactStorage,
        renderer: ExportRenderer | None = None,
    ) -> None:
        self._storage = storage
        self._renderer = renderer or ExportRenderer()

    def render_and_store(
        self,
        request: ExportRequest,
        target: ExportArtifactTarget,
    ) -> StoredExportArtifact:
        export = self._renderer.render(request)
        object_key = _build_export_object_key(
            prefix=target.object_key_prefix,
            artifact_id=target.artifact_id,
            filename=export.filename,
            export_format=export.format,
        )
        reference = ArtifactReference(
            artifact_id=target.artifact_id,
            bucket=target.bucket,
            object_key=object_key,
            filename=export.filename,
            content_type=export.content_type,
        )
        stored = self._storage.put_artifact(
            ArtifactUpload(
                reference=reference,
                body=export.body,
                cache_control=target.cache_control,
            )
        )

        return StoredExportArtifact(
            export_id=export.export_id,
            artifact_id=target.artifact_id,
            format=export.format,
            filename=export.filename,
            content_type=export.content_type,
            byte_size=len(export.body),
            bucket=stored.bucket,
            object_key=stored.object_key,
            etag=stored.etag,
            version_id=stored.version_id,
        )


def _build_export_object_key(
    *,
    prefix: str,
    artifact_id: str,
    filename: str,
    export_format: ExportFormat,
) -> str:
    safe_prefix = _safe_prefix(prefix)
    safe_filename = _safe_filename(filename) or f"brand-export.{export_format.value}"
    return f"{safe_prefix}/{artifact_id}/{safe_filename}"


def _safe_prefix(value: str) -> str:
    parts = [
        _safe_filename(part)
        for part in value.replace("\\", "/").split("/")
        if part not in {"", ".", ".."}
    ]
    return "/".join(parts or ["exports"])


def _safe_filename(value: str) -> str:
    filename = value.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    safe = _SAFE_FILENAME_RE.sub("-", filename).strip(".-")
    safe = re.sub(r"-+(\.[A-Za-z0-9]+)$", r"\1", safe)
    return safe[:180]
