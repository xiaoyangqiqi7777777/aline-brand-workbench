from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.agents.schemas.brand_spec import BrandSpec, SourceRecord, SourceType
from backend.infrastructure.database.models import (
    BrandSpecRecord,
    OutboxEvent,
    Project,
    StageRun,
)


@dataclass(frozen=True)
class CreateProjectCommand:
    workspace_id: str
    actor_id: str
    name: str
    requirement_text: str | None
    structured_fields: dict[str, Any]
    reference_artifact_ids: list[str]


async def create_project(
    session: AsyncSession,
    command: CreateProjectCommand,
) -> tuple[Project, StageRun, OutboxEvent]:
    project_id = str(uuid4())
    run_id = str(uuid4())
    clean_name = command.name.strip()
    if not clean_name:
        raise ValueError("Project name is required")

    allowed_fields = set(BrandSpec.model_fields) - {
        "schema_version",
        "project_name",
        "reference_artifact_ids",
        "source_map",
    }
    unknown_fields = set(command.structured_fields) - allowed_fields
    if unknown_fields:
        raise ValueError("Unsupported BrandSpec fields: " + ", ".join(sorted(unknown_fields)))
    source_map = {
        field: [
            SourceRecord(
                source_type=SourceType.USER_INPUT,
                source_id=f"project-create:{project_id}",
            )
        ]
        for field, value in command.structured_fields.items()
        if value not in (None, "", [])
    }
    brand_spec = BrandSpec.model_validate(
        {
            "project_name": clean_name,
            **command.structured_fields,
            "reference_artifact_ids": command.reference_artifact_ids,
            "source_map": source_map,
        }
    )
    project = Project(
        id=project_id,
        workspace_id=command.workspace_id,
        created_by=command.actor_id,
        name=clean_name,
        requirement_text=(command.requirement_text or "").strip() or None,
    )
    project.brand_spec = BrandSpecRecord(
        project_id=project_id,
        schema_version=brand_spec.schema_version,
        data_json=brand_spec.model_dump(mode="json", exclude={"source_map"}),
        source_map_json={
            key: [item.model_dump(mode="json") for item in value]
            for key, value in brand_spec.source_map.items()
        },
    )
    stage_run = StageRun(
        id=run_id,
        workflow_thread_id=run_id,
        project_id=project_id,
        stage="INTAKE",
        status="QUEUED",
        idempotency_key=f"create-project:{project_id}",
        input_json={},
    )
    event = OutboxEvent(
        topic="agent.stage_run.requested",
        payload_json={"stage_run_id": run_id},
    )
    session.add_all([project, stage_run, event])
    await session.commit()
    return project, stage_run, event


async def list_projects(
    session: AsyncSession,
    *,
    workspace_id: str,
) -> list[Project]:
    result = await session.scalars(
        select(Project)
        .where(Project.workspace_id == workspace_id)
        .order_by(Project.updated_at.desc())
    )
    return list(result)


async def get_project(
    session: AsyncSession,
    *,
    project_id: str,
    workspace_id: str,
) -> Project | None:
    return await session.scalar(
        select(Project)
        .options(selectinload(Project.brand_spec), selectinload(Project.stage_runs))
        .where(Project.id == project_id, Project.workspace_id == workspace_id)
    )
