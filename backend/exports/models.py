from dataclasses import dataclass, field
from enum import StrEnum


class ExportFormat(StrEnum):
    PDF = "pdf"
    PPTX = "pptx"
    ZIP = "zip"


@dataclass(frozen=True, slots=True)
class ExportSection:
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class ExportAsset:
    path: str
    body: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class ExportDocument:
    title: str
    sections: tuple[ExportSection, ...] = ()
    assets: tuple[ExportAsset, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExportRequest:
    export_id: str
    format: ExportFormat
    document: ExportDocument
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class ExportResult:
    export_id: str
    format: ExportFormat
    filename: str
    content_type: str
    body: bytes
