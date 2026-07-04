from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.config import get_settings
from backend.application.projects import (
    CreateProjectCommand,
    create_project,
    get_project,
    list_projects,
)
from backend.application.stage_runs import mark_outbox_published
from backend.infrastructure.database.session import get_db_session

router = APIRouter(prefix="/projects", tags=["projects"])
SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    requirement_text: str | None = Field(default=None, max_length=10_000)
    structured_fields: dict[str, Any] = Field(default_factory=dict)
    reference_artifact_ids: list[str] = Field(default_factory=list, max_length=20)


class StageRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    stage: str
    status: str
    attempt: int
    error_code: str | None
    result_version_id: str | None


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    name: str
    current_stage: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class ProjectCreateResponse(BaseModel):
    project: ProjectResponse
    stage_run: StageRunResponse


class ProjectDetailResponse(ProjectResponse):
    brand_spec: dict[str, Any]
    stage_runs: list[StageRunResponse]


@router.post("", response_model=ProjectCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_project_route(
    payload: ProjectCreateRequest,
    session: SessionDependency,
) -> ProjectCreateResponse:
    settings = get_settings()
    try:
        project, stage_run, outbox_event = await create_project(
            session,
            CreateProjectCommand(
                workspace_id=settings.default_workspace_id,
                actor_id=settings.default_actor_id,
                name=payload.name,
                requirement_text=payload.requirement_text,
                structured_fields=payload.structured_fields,
                reference_artifact_ids=payload.reference_artifact_ids,
            ),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    from apps.api.app.tasks import execute_agent_stage

    execute_agent_stage.delay(stage_run.id)
    await mark_outbox_published(session, event_id=outbox_event.id)
    return ProjectCreateResponse(
        project=ProjectResponse.model_validate(project),
        stage_run=StageRunResponse.model_validate(stage_run),
    )


@router.get("", response_model=list[ProjectResponse])
async def list_projects_route(session: SessionDependency) -> list[ProjectResponse]:
    projects = await list_projects(
        session,
        workspace_id=get_settings().default_workspace_id,
    )
    return [ProjectResponse.model_validate(project) for project in projects]


@router.get("/{project_id}", response_model=ProjectDetailResponse)
async def get_project_route(
    project_id: str,
    session: SessionDependency,
) -> ProjectDetailResponse:
    project = await get_project(
        session,
        project_id=project_id,
        workspace_id=get_settings().default_workspace_id,
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectDetailResponse(
        **ProjectResponse.model_validate(project).model_dump(),
        brand_spec={
            **project.brand_spec.data_json,
            "source_map": project.brand_spec.source_map_json,
        },
        stage_runs=[StageRunResponse.model_validate(run) for run in project.stage_runs],
    )
