from __future__ import annotations

import io
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.schemas.proposal import ProposalOutput
from backend.infrastructure.database.models import Decision, Project, StageVersion


@dataclass(frozen=True)
class ProposalExportManifest:
    project_id: str
    project_name: str
    proposal_version_id: str
    proposal_stage_run_id: str
    decision_id: str
    title: str
    narrative: str
    sections: list[dict[str, Any]]
    asset_refs: list[str]
    generated_at: datetime


class ProjectExportError(ValueError):
    pass


class ProjectExportNotFoundError(ProjectExportError):
    pass


class ProjectExportConflictError(ProjectExportError):
    pass


async def get_proposal_export_manifest(
    session: AsyncSession,
    *,
    project_id: str,
    workspace_id: str,
) -> ProposalExportManifest:
    project = await session.scalar(
        select(Project).where(Project.id == project_id, Project.workspace_id == workspace_id)
    )
    if project is None:
        raise ProjectExportNotFoundError("Project not found")
    if project.status != "COMPLETED":
        raise ProjectExportConflictError("Project is not completed")

    proposal_version = await session.scalar(
        select(StageVersion)
        .where(
            StageVersion.project_id == project_id,
            StageVersion.stage == "PROPOSAL",
            StageVersion.status == "GENERATED",
        )
        .order_by(StageVersion.version_no.desc())
        .limit(1)
    )
    if proposal_version is None:
        raise ProjectExportConflictError("Completed project has no Proposal version")

    final_decision = await session.scalar(
        select(Decision)
        .where(
            Decision.project_id == project_id,
            Decision.stage == "PROPOSAL",
            Decision.action == "CONFIRM_VERSION",
            Decision.source_version_id == proposal_version.id,
        )
        .order_by(Decision.created_at.desc())
        .limit(1)
    )
    if final_decision is None:
        raise ProjectExportConflictError("Completed project has no Proposal confirmation")

    proposal = ProposalOutput.model_validate(proposal_version.output_json)
    proposal_json = proposal.model_dump(mode="json")
    return ProposalExportManifest(
        project_id=project.id,
        project_name=project.name,
        proposal_version_id=proposal_version.id,
        proposal_stage_run_id=proposal_version.stage_run_id,
        decision_id=final_decision.id,
        title=proposal.title,
        narrative=proposal.narrative,
        sections=proposal_json["sections"],
        asset_refs=proposal_json["asset_refs"],
        generated_at=proposal_version.created_at,
    )


def render_proposal_markdown(manifest: ProposalExportManifest) -> str:
    lines = [
        f"# {manifest.title}",
        "",
        manifest.narrative,
        "",
        "## Export Metadata",
        "",
        f"- Project: {manifest.project_name} (`{manifest.project_id}`)",
        f"- Proposal version: `{manifest.proposal_version_id}`",
        f"- Proposal stage run: `{manifest.proposal_stage_run_id}`",
        f"- Final decision: `{manifest.decision_id}`",
        f"- Generated at: {manifest.generated_at.isoformat()}",
        "",
        "## Sections",
        "",
    ]
    for section in manifest.sections:
        lines.extend(
            [
                f"### {section['title']}",
                "",
                f"- Type: `{section['type']}`",
                f"- Version: `{section['version_id']}`",
                f"- Summary: {section['summary']}",
            ]
        )
        asset_ids = section.get("asset_ids", [])
        if asset_ids:
            lines.append("- Assets:")
            lines.extend(f"  - `{asset_id}`" for asset_id in asset_ids)
        else:
            lines.append("- Assets: none")
        lines.append("")

    lines.extend(["## Asset References", ""])
    lines.extend(f"- `{asset_id}`" for asset_id in manifest.asset_refs)
    lines.append("")
    return "\n".join(lines)


def serialize_proposal_manifest(manifest: ProposalExportManifest) -> str:
    payload = {
        "project_id": manifest.project_id,
        "project_name": manifest.project_name,
        "proposal_version_id": manifest.proposal_version_id,
        "proposal_stage_run_id": manifest.proposal_stage_run_id,
        "decision_id": manifest.decision_id,
        "title": manifest.title,
        "narrative": manifest.narrative,
        "sections": manifest.sections,
        "asset_refs": manifest.asset_refs,
        "generated_at": manifest.generated_at.isoformat(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_proposal_zip(manifest: ProposalExportManifest) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, mode="w") as archive:
        _write_zip_text(
            archive,
            filename="proposal.md",
            content=render_proposal_markdown(manifest),
            generated_at=manifest.generated_at,
        )
        _write_zip_text(
            archive,
            filename="proposal-manifest.json",
            content=serialize_proposal_manifest(manifest),
            generated_at=manifest.generated_at,
        )
    return buffer.getvalue()


def _write_zip_text(
    archive: ZipFile,
    *,
    filename: str,
    content: str,
    generated_at: datetime,
) -> None:
    info = ZipInfo(filename=filename)
    info.compress_type = ZIP_DEFLATED
    info.date_time = (
        generated_at.year,
        generated_at.month,
        generated_at.day,
        generated_at.hour,
        generated_at.minute,
        generated_at.second,
    )
    info.external_attr = 0o644 << 16
    archive.writestr(info, content.encode("utf-8"))
