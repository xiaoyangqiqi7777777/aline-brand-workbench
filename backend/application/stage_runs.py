from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from langgraph.types import Command
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.ports import RecoverableInvocationRecorder
from backend.agents.schemas.brand_spec import BrandSpec
from backend.agents.schemas.directions import DirectionOutput
from backend.agents.schemas.intake import IntakeOutput, IntakeResumePayload
from backend.agents.schemas.logo import LogoOutput
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


class StageDecisionError(ValueError):
    pass


class StageDecisionNotFoundError(StageDecisionError):
    pass


class InvalidStageDecisionError(StageDecisionError):
    pass


class UnsupportedStageDecisionError(StageDecisionError):
    pass


class StageDecisionConflictError(StageDecisionError):
    pass


class StageResumeError(ValueError):
    pass


class StageResumeNotFoundError(StageResumeError):
    pass


class StageResumeConflictError(StageResumeError):
    pass


async def create_stage_decision(
    session: AsyncSession,
    *,
    project_id: str,
    workspace_id: str,
    actor_id: str,
    stage_key: str,
    version_id: str,
    selected_item_id: str,
    action: str = "SELECT_VERSION",
) -> tuple[StageRun, Decision, OutboxEvent | None]:
    stage = normalize_stage_key(stage_key)
    if action != "SELECT_VERSION":
        raise InvalidStageDecisionError("Only SELECT_VERSION decisions are supported")
    if stage not in KNOWN_PROJECT_STAGES:
        raise InvalidStageDecisionError(f"Invalid stage key: {stage_key}")

    found_project_id = await session.scalar(
        select(Project.id).where(Project.id == project_id, Project.workspace_id == workspace_id)
    )
    if found_project_id is None:
        raise StageDecisionNotFoundError("Project not found")

    source_version = await session.get(StageVersion, version_id)
    if source_version is None:
        raise StageDecisionNotFoundError("Stage version not found")
    if source_version.project_id != project_id:
        raise StageDecisionNotFoundError("Stage version not found")
    if source_version.stage != stage:
        raise StageDecisionConflictError("Stage version does not belong to requested stage")

    if stage == "DIRECTIONS":
        return await create_direction_selection_run(
            session,
            source_stage_run_id=source_version.stage_run_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            version_id=version_id,
            direction_id=selected_item_id,
        )

    raise UnsupportedStageDecisionError(
        f"{stage} decisions are not supported by this worker milestone"
    )


async def create_direction_selection_run(
    session: AsyncSession,
    *,
    source_stage_run_id: str,
    workspace_id: str,
    actor_id: str,
    version_id: str,
    direction_id: str,
) -> tuple[StageRun, Decision, OutboxEvent | None]:
    source_run = await session.scalar(
        select(StageRun)
        .join(Project, Project.id == StageRun.project_id)
        .where(
            StageRun.id == source_stage_run_id,
            Project.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if source_run is None:
        raise StageDecisionNotFoundError("Stage run not found")
    if source_run.stage != "DIRECTIONS" or source_run.status != "SUCCEEDED":
        raise StageDecisionConflictError("Only a succeeded Directions run can accept a selection")
    if source_run.result_version_id != version_id:
        raise StageDecisionConflictError(
            "Selected version is not the result of this Directions run"
        )

    source_version = await session.get(StageVersion, version_id)
    if (
        source_version is None
        or source_version.project_id != source_run.project_id
        or source_version.stage != "DIRECTIONS"
    ):
        raise StageDecisionNotFoundError("Directions version not found")
    direction_output = DirectionOutput.model_validate(source_version.output_json)
    if direction_id not in {item.id for item in direction_output.directions}:
        raise StageDecisionConflictError("Selected direction does not exist in current version")

    existing_decision = await session.scalar(
        select(Decision).where(
            Decision.source_version_id == version_id,
            Decision.action == "SELECT_VERSION",
        )
    )
    if existing_decision is not None:
        if existing_decision.selected_item_id != direction_id:
            raise StageDecisionConflictError(
                "This Directions version already has another selection"
            )
        existing_run = await session.get(
            StageRun,
            existing_decision.resulting_stage_run_id,
        )
        if existing_run is None:
            raise StageDecisionConflictError("Direction decision has no resulting Stage run")
        return existing_run, existing_decision, None

    run_id = str(uuid4())
    decision_id = str(uuid4())
    resume_payload = {
        "version_id": version_id,
        "selected_item_id": direction_id,
    }
    resumed_run = StageRun(
        id=run_id,
        workflow_thread_id=source_run.workflow_thread_id,
        parent_stage_run_id=source_run.id,
        project_id=source_run.project_id,
        stage="LOGO",
        status="QUEUED",
        idempotency_key=f"select-direction:{version_id}",
        input_json={
            "resume": resume_payload,
            "decision_id": decision_id,
            "direction_version_id": version_id,
        },
    )
    session.add(resumed_run)
    await session.flush()
    decision = Decision(
        id=decision_id,
        project_id=source_run.project_id,
        stage="DIRECTIONS",
        action="SELECT_VERSION",
        source_version_id=version_id,
        selected_item_id=direction_id,
        resulting_stage_run_id=run_id,
        created_by=actor_id,
        payload_json=resume_payload,
    )
    event = OutboxEvent(
        topic="agent.stage_run.requested",
        payload_json={"stage_run_id": run_id},
    )
    await mark_downstream_versions_stale(
        session,
        project_id=source_run.project_id,
        stage=source_run.stage,
    )
    project = await session.get(Project, source_run.project_id)
    if project is None:
        raise StageDecisionNotFoundError("Project not found")
    project.current_stage = resumed_run.stage
    project.version += 1
    session.add_all([decision, event])
    await session.commit()
    return resumed_run, decision, event


async def mark_downstream_versions_stale(
    session: AsyncSession,
    *,
    project_id: str,
    stage: str,
) -> None:
    downstream_stages = downstream_project_stages(stage)
    if not downstream_stages:
        return

    await session.execute(
        update(StageVersion)
        .where(
            StageVersion.project_id == project_id,
            StageVersion.stage.in_(downstream_stages),
            StageVersion.status != "STALE",
        )
        .values(status="STALE")
    )


async def create_intake_resume_run(
    session: AsyncSession,
    *,
    source_stage_run_id: str,
    workspace_id: str,
    resume_payload: IntakeResumePayload,
) -> tuple[StageRun, OutboxEvent | None]:
    source_run = await session.scalar(
        select(StageRun)
        .join(Project, Project.id == StageRun.project_id)
        .where(
            StageRun.id == source_stage_run_id,
            Project.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if source_run is None:
        raise StageResumeNotFoundError("Stage run not found")
    if source_run.stage != "INTAKE" or source_run.status != "SUCCEEDED":
        raise StageResumeConflictError("Only a succeeded Intake run can accept answers")
    if source_run.result_version_id is None:
        raise StageResumeConflictError("Intake run has no result to resume")

    payload_json = resume_payload.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(payload_json, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:32]
    idempotency_key = f"resume-intake:{source_run.id}:{digest}"
    existing = await session.scalar(
        select(StageRun).where(
            StageRun.project_id == source_run.project_id,
            StageRun.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing, None

    run_id = str(uuid4())
    resumed_run = StageRun(
        id=run_id,
        workflow_thread_id=source_run.workflow_thread_id,
        parent_stage_run_id=source_run.id,
        project_id=source_run.project_id,
        stage="DIRECTIONS",
        status="QUEUED",
        idempotency_key=idempotency_key,
        input_json={"resume": payload_json},
    )
    event = OutboxEvent(
        topic="agent.stage_run.requested",
        payload_json={"stage_run_id": run_id},
    )
    await mark_downstream_versions_stale(
        session,
        project_id=source_run.project_id,
        stage=source_run.stage,
    )
    project = await session.get(Project, source_run.project_id)
    if project is None:
        raise StageResumeNotFoundError("Project not found")
    project.current_stage = resumed_run.stage
    project.version += 1
    session.add_all([resumed_run, event])
    await session.commit()
    return resumed_run, event


async def mark_outbox_published(
    session: AsyncSession,
    *,
    event_id: str,
) -> None:
    event = await session.get(OutboxEvent, event_id)
    if event is None:
        raise ValueError("Outbox event not found")
    event.status = "PUBLISHED"
    event.attempt += 1
    await session.commit()


async def execute_stage_run(
    session: AsyncSession,
    *,
    stage_run_id: str,
    workflow,
    invocation_recorder: RecoverableInvocationRecorder,
) -> StageRun:
    stage_run = await session.scalar(
        select(StageRun).where(StageRun.id == stage_run_id).with_for_update()
    )
    if stage_run is None:
        raise ValueError("Stage run not found")
    if stage_run.status == "SUCCEEDED":
        return stage_run
    if stage_run.stage not in {"INTAKE", "DIRECTIONS", "LOGO"}:
        raise ValueError("Only INTAKE, DIRECTIONS, and LOGO are supported by this worker milestone")

    stage_run.status = "RUNNING"
    stage_run.attempt += 1
    stage_run.error_code = None
    stage_run.error_message = None
    await session.commit()

    try:
        brand_spec_record = await session.get(BrandSpecRecord, stage_run.project_id)
        if brand_spec_record is None:
            raise ValueError("BrandSpec not found")
        brand_spec = BrandSpec.model_validate(
            {
                **brand_spec_record.data_json,
                "source_map": brand_spec_record.source_map_json,
            }
        )
        graph_input = (
            Command(resume=stage_run.input_json["resume"])
            if "resume" in stage_run.input_json
            else {
                "project_id": stage_run.project_id,
                "brand_spec": brand_spec.model_dump(mode="json"),
                "status": "INTAKE",
            }
        )
        result = workflow.invoke(
            graph_input,
            config={"configurable": {"thread_id": stage_run.workflow_thread_id}},
        )
        output_by_stage = {
            "INTAKE": (IntakeOutput, "intake_output"),
            "DIRECTIONS": (DirectionOutput, "direction_output"),
            "LOGO": (LogoOutput, "logo_output"),
        }
        output_model, output_key = output_by_stage[stage_run.stage]
        output = output_model.model_validate(result[output_key])
        next_version = (
            await session.scalar(
                select(func.coalesce(func.max(StageVersion.version_no), 0)).where(
                    StageVersion.project_id == stage_run.project_id,
                    StageVersion.stage == stage_run.stage,
                )
            )
        ) + 1
        input_refs = {"brand_spec_version": brand_spec_record.version}
        if stage_run.stage == "LOGO":
            input_refs.update(
                {
                    "direction_version_id": stage_run.input_json["direction_version_id"],
                    "decision_id": stage_run.input_json["decision_id"],
                }
            )
        version = StageVersion(
            project_id=stage_run.project_id,
            stage_run_id=stage_run.id,
            stage=stage_run.stage,
            version_no=next_version,
            schema_version=output.schema_version,
            input_refs_json=input_refs,
            output_json=output.model_dump(mode="json"),
            status="GENERATED",
        )
        session.add(version)
        await session.flush()
        stage_run.status = "SUCCEEDED"
        stage_run.result_version_id = version.id
        project = await session.get(Project, stage_run.project_id)
        if project is None:
            raise ValueError("Project not found")
        if "brand_spec" in result:
            resumed_spec = BrandSpec.model_validate(result["brand_spec"])
            brand_spec_record.data_json = resumed_spec.model_dump(
                mode="json", exclude={"source_map"}
            )
            brand_spec_record.source_map_json = {
                key: [item.model_dump(mode="json") for item in value]
                for key, value in resumed_spec.source_map.items()
            }
            if resumed_spec != brand_spec:
                brand_spec_record.version += 1
        project.current_stage = stage_run.stage
        project.version += 1
        await session.commit()
        return stage_run
    except Exception as error:
        await session.rollback()
        invocation_recorder.restore_after_rollback()
        failed_run = await session.get(StageRun, stage_run_id)
        if failed_run is not None:
            failed_run.status = "FAILED"
            failed_run.error_code = getattr(
                error,
                "code",
                f"{stage_run.stage}_EXECUTION_FAILED",
            )
            failed_run.error_message = str(error)[:500]
            await session.commit()
        raise


async def get_stage_run(
    session: AsyncSession,
    *,
    stage_run_id: str,
    workspace_id: str,
) -> tuple[StageRun, StageVersion | None] | None:
    stage_run = await session.scalar(
        select(StageRun)
        .join(Project, Project.id == StageRun.project_id)
        .where(StageRun.id == stage_run_id, Project.workspace_id == workspace_id)
    )
    if stage_run is None:
        return None
    version = (
        await session.get(StageVersion, stage_run.result_version_id)
        if stage_run.result_version_id
        else None
    )
    return stage_run, version
