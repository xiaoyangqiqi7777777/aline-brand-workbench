from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.agents.schemas.brand_spec import BrandSpec, SourceRecord, SourceType
from backend.application.stages import (
    KNOWN_PROJECT_STAGES,
    downstream_project_stages,
    normalize_stage_key,
)
from backend.infrastructure.database.models import (
    BrandSpecRecord,
    Decision,
    OutboxEvent,
    Project,
    StageRun,
    StageVersion,
)


@dataclass(frozen=True)
class CreateProjectCommand:
    workspace_id: str
    actor_id: str
    name: str
    requirement_text: str | None
    structured_fields: dict[str, Any]
    reference_artifact_ids: list[str]


@dataclass(frozen=True)
class ProjectState:
    project: Project
    stage_runs: list[StageRun]
    stage_versions: list[StageVersion]
    decisions: list[Decision]


@dataclass(frozen=True)
class StageControlResult:
    project_id: str
    stage: str
    action: str
    status: str
    outbox_event: OutboxEvent | None = None


class ProjectStageControlError(ValueError):
    pass


class ProjectNotFoundError(ProjectStageControlError):
    pass


class StageControlNotFoundError(ProjectStageControlError):
    pass


class InvalidStageKeyError(ProjectStageControlError):
    pass


class StageControlConflictError(ProjectStageControlError):
    pass


class UnsupportedStageControlError(ProjectStageControlError):
    pass


SUPPORTED_REDO_STAGES = frozenset({"INTAKE", "DIRECTIONS"})


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


async def get_project_state(
    session: AsyncSession,
    *,
    project_id: str,
    workspace_id: str,
) -> ProjectState | None:
    project = await session.scalar(
        select(Project)
        .options(selectinload(Project.brand_spec))
        .where(Project.id == project_id, Project.workspace_id == workspace_id)
    )
    if project is None:
        return None

    stage_runs = await _latest_stage_runs(session, project_id=project_id)
    stage_versions = await _latest_stage_versions(session, project_id=project_id)
    decisions = list(
        await session.scalars(
            select(Decision)
            .where(Decision.project_id == project_id)
            .order_by(Decision.created_at.asc())
        )
    )
    return ProjectState(
        project=project,
        stage_runs=stage_runs,
        stage_versions=stage_versions,
        decisions=decisions,
    )


async def _latest_stage_runs(
    session: AsyncSession,
    *,
    project_id: str,
) -> list[StageRun]:
    runs = await session.scalars(
        select(StageRun)
        .where(StageRun.project_id == project_id)
        .order_by(StageRun.stage.asc(), StageRun.updated_at.desc(), StageRun.created_at.desc())
    )
    latest_by_stage: dict[str, StageRun] = {}
    for run in runs:
        latest_by_stage.setdefault(run.stage, run)
    return list(latest_by_stage.values())


async def _latest_stage_versions(
    session: AsyncSession,
    *,
    project_id: str,
) -> list[StageVersion]:
    versions = await session.scalars(
        select(StageVersion)
        .where(StageVersion.project_id == project_id)
        .order_by(StageVersion.stage.asc(), StageVersion.version_no.desc())
    )
    latest_by_stage: dict[str, StageVersion] = {}
    for version in versions:
        latest_by_stage.setdefault(version.stage, version)
    return list(latest_by_stage.values())


async def list_stage_versions(
    session: AsyncSession,
    *,
    project_id: str,
    workspace_id: str,
    stage_key: str,
) -> list[StageVersion] | None:
    stage = normalize_stage_key(stage_key)
    if stage not in KNOWN_PROJECT_STAGES:
        raise InvalidStageKeyError(f"Invalid stage key: {stage_key}")

    found_project_id = await session.scalar(
        select(Project.id).where(Project.id == project_id, Project.workspace_id == workspace_id)
    )
    if found_project_id is None:
        return None

    versions = await session.scalars(
        select(StageVersion)
        .where(
            StageVersion.project_id == project_id,
            StageVersion.stage == stage,
        )
        .order_by(StageVersion.version_no.desc())
    )
    return list(versions)


async def request_stage_control(
    session: AsyncSession,
    *,
    project_id: str,
    workspace_id: str,
    actor_id: str,
    stage_key: str,
    action: Literal["REDO", "SKIP", "GENERATE"],
    source_version_id: str | None = None,
    reason: str | None = None,
) -> StageControlResult:
    stage = normalize_stage_key(stage_key)
    if stage not in KNOWN_PROJECT_STAGES:
        raise InvalidStageKeyError(f"Invalid stage key: {stage_key}")

    found_project_id = await session.scalar(
        select(Project.id).where(Project.id == project_id, Project.workspace_id == workspace_id)
    )
    if found_project_id is None:
        raise ProjectNotFoundError("Project not found")

    if stage == "IP" and action == "SKIP" and source_version_id is not None:
        raise StageControlConflictError("IP skip does not accept source_version_id")
    if stage == "IP" and action == "GENERATE" and source_version_id is not None:
        raise StageControlConflictError("IP generate does not accept source_version_id")

    source_version: StageVersion | None = None
    if source_version_id is not None:
        source_version = await session.get(StageVersion, source_version_id)
        if source_version is None or source_version.project_id != project_id:
            raise StageControlNotFoundError("Stage version not found")
        if source_version.stage != stage:
            raise StageControlConflictError("Stage version does not belong to requested stage")

    if action == "REDO":
        if source_version is None:
            raise StageControlConflictError("REDO requires source_version_id")
        return await _redo_stage(
            session,
            source_version=source_version,
            actor_id=actor_id,
            reason=reason,
        )

    if stage == "IP" and action == "SKIP":
        return await _skip_ip_choice(
            session,
            project_id=project_id,
            actor_id=actor_id,
            reason=reason,
        )

    if stage == "IP" and action == "GENERATE":
        return await _generate_ip_choice(
            session,
            project_id=project_id,
            actor_id=actor_id,
            reason=reason,
        )

    raise UnsupportedStageControlError(
        f"{action} is not supported by this worker milestone for {stage}"
    )


async def _redo_stage(
    session: AsyncSession,
    *,
    source_version: StageVersion,
    actor_id: str,
    reason: str | None,
) -> StageControlResult:
    if source_version.stage not in SUPPORTED_REDO_STAGES:
        raise UnsupportedStageControlError(
            f"REDO is not supported by this worker milestone for {source_version.stage}"
        )

    existing_decision = await session.scalar(
        select(Decision).where(
            Decision.source_version_id == source_version.id,
            Decision.action == "REDO",
        )
    )
    if existing_decision is not None:
        existing_run = await session.get(StageRun, existing_decision.resulting_stage_run_id)
        if existing_run is None:
            raise StageControlConflictError("REDO decision has no resulting Stage run")
        return StageControlResult(
            project_id=source_version.project_id,
            stage=existing_run.stage,
            action="REDO",
            status=existing_run.status,
        )

    if source_version.status != "GENERATED":
        raise StageControlConflictError("Only a generated Stage version can be redone")

    source_run = await session.scalar(
        select(StageRun)
        .where(
            StageRun.id == source_version.stage_run_id,
            StageRun.project_id == source_version.project_id,
        )
        .with_for_update()
    )
    if source_run is None:
        raise StageControlNotFoundError("Stage run not found")
    if source_run.stage != source_version.stage or source_run.status != "SUCCEEDED":
        raise StageControlConflictError(
            f"Only a succeeded {source_version.stage} run can be redone"
        )
    if source_run.result_version_id != source_version.id:
        raise StageControlConflictError(
            f"Redone version is not the result of this {source_version.stage} run"
        )

    run_id = str(uuid4())
    decision_id = str(uuid4())
    redo_payload = {"source_version_id": source_version.id}
    if reason:
        redo_payload["reason"] = reason
    redo_run = StageRun(
        id=run_id,
        workflow_thread_id=run_id,
        parent_stage_run_id=source_run.id,
        project_id=source_version.project_id,
        stage=source_version.stage,
        status="QUEUED",
        idempotency_key=f"redo-{source_version.stage.lower()}:{source_version.id}",
        input_json={"redo": redo_payload, "decision_id": decision_id},
    )
    session.add(redo_run)
    await session.flush()
    decision = Decision(
        id=decision_id,
        project_id=source_version.project_id,
        stage=source_version.stage,
        action="REDO",
        source_version_id=source_version.id,
        selected_item_id=None,
        resulting_stage_run_id=run_id,
        created_by=actor_id,
        payload_json=redo_payload,
    )
    event = OutboxEvent(
        topic="agent.stage_run.requested",
        payload_json={"stage_run_id": run_id},
    )
    stages_to_stale = (source_version.stage, *downstream_project_stages(source_version.stage))
    await session.execute(
        update(StageVersion)
        .where(
            StageVersion.project_id == source_version.project_id,
            StageVersion.stage.in_(stages_to_stale),
            StageVersion.status != "STALE",
        )
        .values(status="STALE")
    )
    project = await session.get(Project, source_version.project_id)
    if project is None:
        raise ProjectNotFoundError("Project not found")
    project.current_stage = redo_run.stage
    project.status = "ACTIVE"
    project.version += 1
    session.add_all([decision, event])
    await session.commit()
    return StageControlResult(
        project_id=source_version.project_id,
        stage=redo_run.stage,
        action="REDO",
        status=redo_run.status,
        outbox_event=event,
    )


async def _skip_ip_choice(
    session: AsyncSession,
    *,
    project_id: str,
    actor_id: str,
    reason: str | None,
) -> StageControlResult:
    ip_choice_run = await session.scalar(
        select(StageRun)
        .where(
            StageRun.project_id == project_id,
            StageRun.stage == "IP",
            StageRun.status == "WAITING_USER",
        )
        .order_by(StageRun.updated_at.desc(), StageRun.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if ip_choice_run is None:
        raise StageControlConflictError("No waiting IP choice found")

    vi_version_id = ip_choice_run.input_json.get("vi_version_id")
    if not isinstance(vi_version_id, str):
        raise StageControlConflictError("IP choice is missing source VI version")
    await _validate_ip_choice_vi_version(
        session,
        project_id=project_id,
        vi_version_id=vi_version_id,
    )

    existing_choice = await session.scalar(
        select(Decision).where(
            Decision.project_id == project_id,
            Decision.stage == "IP",
            Decision.source_version_id == vi_version_id,
        )
    )
    if existing_choice is not None:
        if existing_choice.action != "SKIP":
            raise StageControlConflictError("IP choice already has another action")
        existing_run = await session.get(
            StageRun,
            existing_choice.resulting_stage_run_id,
        )
        if existing_run is None:
            raise StageControlConflictError("IP skip decision has no resulting Stage run")
        return StageControlResult(
            project_id=project_id,
            stage=existing_run.stage,
            action="SKIP",
            status=existing_run.status,
        )

    run_id = str(uuid4())
    decision_id = str(uuid4())
    resume_payload = {"action": "SKIP"}
    if reason:
        resume_payload["reason"] = reason
    materials_run = StageRun(
        id=run_id,
        workflow_thread_id=ip_choice_run.workflow_thread_id,
        parent_stage_run_id=ip_choice_run.id,
        project_id=project_id,
        stage="MATERIALS",
        status="QUEUED",
        idempotency_key=f"skip-ip:{ip_choice_run.id}",
        input_json={
            "resume": {"action": "SKIP"},
            "decision_id": decision_id,
            "vi_version_id": vi_version_id,
            "ip_skipped": True,
        },
    )
    session.add(materials_run)
    await session.flush()
    decision = Decision(
        id=decision_id,
        project_id=project_id,
        stage="IP",
        action="SKIP",
        source_version_id=vi_version_id,
        selected_item_id=None,
        resulting_stage_run_id=run_id,
        created_by=actor_id,
        payload_json=resume_payload,
    )
    event = OutboxEvent(
        topic="agent.stage_run.requested",
        payload_json={"stage_run_id": run_id},
    )
    await session.execute(
        update(StageVersion)
        .where(
            StageVersion.project_id == project_id,
            StageVersion.stage.in_(("IP", "MATERIALS", "REVIEW", "PROPOSAL")),
            StageVersion.status != "STALE",
        )
        .values(status="STALE")
    )
    project = await session.get(Project, project_id)
    if project is None:
        raise ProjectNotFoundError("Project not found")
    project.current_stage = materials_run.stage
    project.version += 1
    session.add_all([decision, event])
    await session.commit()
    return StageControlResult(
        project_id=project_id,
        stage=materials_run.stage,
        action="SKIP",
        status=materials_run.status,
        outbox_event=event,
    )


async def _generate_ip_choice(
    session: AsyncSession,
    *,
    project_id: str,
    actor_id: str,
    reason: str | None,
) -> StageControlResult:
    ip_choice_run = await session.scalar(
        select(StageRun)
        .where(
            StageRun.project_id == project_id,
            StageRun.stage == "IP",
            StageRun.status == "WAITING_USER",
        )
        .order_by(StageRun.updated_at.desc(), StageRun.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if ip_choice_run is None:
        raise StageControlConflictError("No waiting IP choice found")

    vi_version_id = ip_choice_run.input_json.get("vi_version_id")
    if not isinstance(vi_version_id, str):
        raise StageControlConflictError("IP choice is missing source VI version")
    await _validate_ip_choice_vi_version(
        session,
        project_id=project_id,
        vi_version_id=vi_version_id,
    )

    existing_choice = await session.scalar(
        select(Decision).where(
            Decision.project_id == project_id,
            Decision.stage == "IP",
            Decision.source_version_id == vi_version_id,
        )
    )
    if existing_choice is not None:
        if existing_choice.action != "GENERATE":
            raise StageControlConflictError("IP choice already has another action")
        existing_run = await session.get(
            StageRun,
            existing_choice.resulting_stage_run_id,
        )
        if existing_run is None:
            raise StageControlConflictError("IP generate decision has no resulting Stage run")
        return StageControlResult(
            project_id=project_id,
            stage=existing_run.stage,
            action="GENERATE",
            status=existing_run.status,
        )

    run_id = str(uuid4())
    decision_id = str(uuid4())
    resume_payload = {"action": "GENERATE"}
    if reason:
        resume_payload["reason"] = reason
    ip_run = StageRun(
        id=run_id,
        workflow_thread_id=ip_choice_run.workflow_thread_id,
        parent_stage_run_id=ip_choice_run.id,
        project_id=project_id,
        stage="IP",
        status="QUEUED",
        idempotency_key=f"generate-ip:{ip_choice_run.id}",
        input_json={
            "resume": {"action": "GENERATE"},
            "decision_id": decision_id,
            "vi_version_id": vi_version_id,
            "ip_skipped": False,
        },
    )
    session.add(ip_run)
    await session.flush()
    decision = Decision(
        id=decision_id,
        project_id=project_id,
        stage="IP",
        action="GENERATE",
        source_version_id=vi_version_id,
        selected_item_id=None,
        resulting_stage_run_id=run_id,
        created_by=actor_id,
        payload_json=resume_payload,
    )
    event = OutboxEvent(
        topic="agent.stage_run.requested",
        payload_json={"stage_run_id": run_id},
    )
    await session.execute(
        update(StageVersion)
        .where(
            StageVersion.project_id == project_id,
            StageVersion.stage.in_(("IP", "MATERIALS", "REVIEW", "PROPOSAL")),
            StageVersion.status != "STALE",
        )
        .values(status="STALE")
    )
    project = await session.get(Project, project_id)
    if project is None:
        raise ProjectNotFoundError("Project not found")
    project.current_stage = ip_run.stage
    project.version += 1
    session.add_all([decision, event])
    await session.commit()
    return StageControlResult(
        project_id=project_id,
        stage=ip_run.stage,
        action="GENERATE",
        status=ip_run.status,
        outbox_event=event,
    )


async def _validate_ip_choice_vi_version(
    session: AsyncSession,
    *,
    project_id: str,
    vi_version_id: str,
) -> None:
    vi_version = await session.get(StageVersion, vi_version_id)
    if vi_version is None or vi_version.project_id != project_id or vi_version.stage != "VI":
        raise StageControlConflictError("IP choice source VI version not found")
    if vi_version.status != "GENERATED":
        raise StageControlConflictError("Only a generated VI version can choose IP handling")
