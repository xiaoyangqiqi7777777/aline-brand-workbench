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
from backend.agents.schemas.ip import IPOutput
from backend.agents.schemas.logo import LogoOutput
from backend.agents.schemas.materials import MaterialOutput
from backend.agents.schemas.proposal import ProposalOutput
from backend.agents.schemas.review import ReviewOutput
from backend.agents.schemas.vi import VIOutput
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

SUPPORTED_STAGE_DECISION_ACTIONS = frozenset({"SELECT_VERSION", "CONFIRM_VERSION"})


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
    selected_item_id: str | None = None,
    confirmed: bool | None = None,
    action: str = "SELECT_VERSION",
) -> tuple[StageRun, Decision, OutboxEvent | None]:
    stage = normalize_stage_key(stage_key)
    if action not in SUPPORTED_STAGE_DECISION_ACTIONS:
        raise InvalidStageDecisionError(f"Unsupported decision action: {action}")
    if action == "SELECT_VERSION" and selected_item_id is None:
        raise InvalidStageDecisionError("selected_item_id is required for SELECT_VERSION decisions")
    if action == "CONFIRM_VERSION" and confirmed is not True:
        raise InvalidStageDecisionError("confirmed=true is required for CONFIRM_VERSION decisions")
    if stage not in KNOWN_PROJECT_STAGES:
        raise InvalidStageDecisionError(f"Invalid stage key: {stage_key}")

    project = await session.scalar(
        select(Project).where(Project.id == project_id, Project.workspace_id == workspace_id)
    )
    if project is None:
        raise StageDecisionNotFoundError("Project not found")

    source_version = await session.get(StageVersion, version_id)
    if source_version is None:
        raise StageDecisionNotFoundError("Stage version not found")
    if source_version.project_id != project_id:
        raise StageDecisionNotFoundError("Stage version not found")
    if source_version.stage != stage:
        raise StageDecisionConflictError("Stage version does not belong to requested stage")
    if source_version.status != "GENERATED":
        raise StageDecisionConflictError("Only a generated Stage version can be decided")
    if project.status == "COMPLETED" and not (stage == "PROPOSAL" and action == "CONFIRM_VERSION"):
        raise StageDecisionConflictError("Completed project cannot accept stage decisions")

    if stage == "DIRECTIONS":
        if action != "SELECT_VERSION":
            raise UnsupportedStageDecisionError(
                f"{stage} {action} decisions are not supported by this worker milestone"
            )
        assert selected_item_id is not None
        return await create_direction_selection_run(
            session,
            source_stage_run_id=source_version.stage_run_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            version_id=version_id,
            direction_id=selected_item_id,
        )

    if stage == "LOGO":
        if action != "SELECT_VERSION":
            raise UnsupportedStageDecisionError(
                f"{stage} {action} decisions are not supported by this worker milestone"
            )
        assert selected_item_id is not None
        return await create_logo_selection_run(
            session,
            source_stage_run_id=source_version.stage_run_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            version_id=version_id,
            logo_id=selected_item_id,
        )

    if stage == "VI":
        if action != "CONFIRM_VERSION":
            raise UnsupportedStageDecisionError(
                f"{stage} {action} decisions are not supported by this worker milestone"
            )
        return await create_stage_confirmation_run(
            session,
            source_stage_run_id=source_version.stage_run_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            version_id=version_id,
            stage="VI",
            next_stage="IP",
            resume_payload={
                "version_id": version_id,
                "confirmed": True,
            },
        )

    if stage == "IP":
        if action != "CONFIRM_VERSION":
            raise UnsupportedStageDecisionError(
                f"{stage} {action} decisions are not supported by this worker milestone"
            )
        return await create_stage_confirmation_run(
            session,
            source_stage_run_id=source_version.stage_run_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            version_id=version_id,
            stage="IP",
            next_stage="MATERIALS",
            resume_payload={
                "version_id": version_id,
                "confirmed": True,
            },
        )

    if stage == "MATERIALS":
        if action != "CONFIRM_VERSION":
            raise UnsupportedStageDecisionError(
                f"{stage} {action} decisions are not supported by this worker milestone"
            )
        return await create_stage_confirmation_run(
            session,
            source_stage_run_id=source_version.stage_run_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            version_id=version_id,
            stage="MATERIALS",
            next_stage="REVIEW",
            resume_payload={
                "version_id": version_id,
                "confirmed": True,
            },
        )

    if stage == "REVIEW":
        if action != "CONFIRM_VERSION":
            raise UnsupportedStageDecisionError(
                f"{stage} {action} decisions are not supported by this worker milestone"
            )
        return await create_stage_confirmation_run(
            session,
            source_stage_run_id=source_version.stage_run_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            version_id=version_id,
            stage="REVIEW",
            next_stage="PROPOSAL",
            resume_payload={
                "version_id": version_id,
                "proceed": True,
                "accepted_issue_ids": [],
            },
        )

    if stage == "PROPOSAL":
        if action != "CONFIRM_VERSION":
            raise UnsupportedStageDecisionError(
                f"{stage} {action} decisions are not supported by this worker milestone"
            )
        return await create_final_stage_confirmation_run(
            session,
            source_stage_run_id=source_version.stage_run_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            version_id=version_id,
            stage="PROPOSAL",
            resume_payload={
                "version_id": version_id,
                "confirmed": True,
            },
        )

    raise UnsupportedStageDecisionError(
        f"{stage} {action} decisions are not supported by this worker milestone"
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
    project = await session.get(Project, source_run.project_id)
    if project is None:
        raise StageDecisionNotFoundError("Project not found")
    if project.status == "COMPLETED":
        raise StageDecisionConflictError("Completed project cannot accept stage decisions")
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
    if source_version.status != "GENERATED":
        raise StageDecisionConflictError("Only a generated Stage version can be decided")
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


async def create_stage_confirmation_run(
    session: AsyncSession,
    *,
    source_stage_run_id: str,
    workspace_id: str,
    actor_id: str,
    version_id: str,
    stage: str,
    next_stage: str,
    resume_payload: dict[str, object],
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
    if source_run.stage != stage or source_run.status != "SUCCEEDED":
        raise StageDecisionConflictError(f"Only a succeeded {stage} run can be confirmed")
    if source_run.result_version_id != version_id:
        raise StageDecisionConflictError(f"Confirmed version is not the result of this {stage} run")

    source_version = await session.get(StageVersion, version_id)
    if (
        source_version is None
        or source_version.project_id != source_run.project_id
        or source_version.stage != stage
    ):
        raise StageDecisionNotFoundError(f"{stage} version not found")

    existing_decision = await session.scalar(
        select(Decision).where(
            Decision.source_version_id == version_id,
            Decision.action == "CONFIRM_VERSION",
        )
    )
    if existing_decision is not None:
        existing_run = await session.get(
            StageRun,
            existing_decision.resulting_stage_run_id,
        )
        if existing_run is None:
            raise StageDecisionConflictError(f"{stage} decision has no resulting Stage run")
        return existing_run, existing_decision, None

    run_id = str(uuid4())
    decision_id = str(uuid4())
    resumed_run = StageRun(
        id=run_id,
        workflow_thread_id=source_run.workflow_thread_id,
        parent_stage_run_id=source_run.id,
        project_id=source_run.project_id,
        stage=next_stage,
        status="QUEUED",
        idempotency_key=f"confirm-{stage.lower()}:{version_id}",
        input_json={
            "resume": resume_payload,
            "decision_id": decision_id,
            f"{stage.lower()}_version_id": version_id,
        },
    )
    session.add(resumed_run)
    await session.flush()
    decision = Decision(
        id=decision_id,
        project_id=source_run.project_id,
        stage=stage,
        action="CONFIRM_VERSION",
        source_version_id=version_id,
        selected_item_id=None,
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


async def create_final_stage_confirmation_run(
    session: AsyncSession,
    *,
    source_stage_run_id: str,
    workspace_id: str,
    actor_id: str,
    version_id: str,
    stage: str,
    resume_payload: dict[str, object],
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
    if source_run.stage != stage or source_run.status != "SUCCEEDED":
        raise StageDecisionConflictError(f"Only a succeeded {stage} run can be confirmed")
    if source_run.result_version_id != version_id:
        raise StageDecisionConflictError(f"Confirmed version is not the result of this {stage} run")

    source_version = await session.get(StageVersion, version_id)
    if (
        source_version is None
        or source_version.project_id != source_run.project_id
        or source_version.stage != stage
    ):
        raise StageDecisionNotFoundError(f"{stage} version not found")

    existing_decision = await session.scalar(
        select(Decision).where(
            Decision.source_version_id == version_id,
            Decision.action == "CONFIRM_VERSION",
        )
    )
    if existing_decision is not None:
        existing_run = await session.get(
            StageRun,
            existing_decision.resulting_stage_run_id,
        )
        if existing_run is None:
            raise StageDecisionConflictError(f"{stage} decision has no resulting Stage run")
        return existing_run, existing_decision, None

    run_id = str(uuid4())
    decision_id = str(uuid4())
    confirmation_run = StageRun(
        id=run_id,
        workflow_thread_id=source_run.workflow_thread_id,
        parent_stage_run_id=source_run.id,
        project_id=source_run.project_id,
        stage=stage,
        status="SUCCEEDED",
        idempotency_key=f"confirm-{stage.lower()}:{version_id}",
        input_json={
            "resume": resume_payload,
            "decision_id": decision_id,
            f"{stage.lower()}_version_id": version_id,
        },
        result_version_id=version_id,
    )
    session.add(confirmation_run)
    await session.flush()
    decision = Decision(
        id=decision_id,
        project_id=source_run.project_id,
        stage=stage,
        action="CONFIRM_VERSION",
        source_version_id=version_id,
        selected_item_id=None,
        resulting_stage_run_id=run_id,
        created_by=actor_id,
        payload_json=resume_payload,
    )
    project = await session.get(Project, source_run.project_id)
    if project is None:
        raise StageDecisionNotFoundError("Project not found")
    project.current_stage = stage
    project.status = "COMPLETED"
    project.version += 1
    session.add(decision)
    await session.commit()
    return confirmation_run, decision, None


async def create_logo_selection_run(
    session: AsyncSession,
    *,
    source_stage_run_id: str,
    workspace_id: str,
    actor_id: str,
    version_id: str,
    logo_id: str,
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
    if source_run.stage != "LOGO" or source_run.status != "SUCCEEDED":
        raise StageDecisionConflictError("Only a succeeded Logo run can accept a selection")
    if source_run.result_version_id != version_id:
        raise StageDecisionConflictError("Selected version is not the result of this Logo run")

    source_version = await session.get(StageVersion, version_id)
    if (
        source_version is None
        or source_version.project_id != source_run.project_id
        or source_version.stage != "LOGO"
    ):
        raise StageDecisionNotFoundError("Logo version not found")
    logo_output = LogoOutput.model_validate(source_version.output_json)
    if logo_id not in {item.id for item in logo_output.concepts}:
        raise StageDecisionConflictError("Selected logo does not exist in current version")

    existing_decision = await session.scalar(
        select(Decision).where(
            Decision.source_version_id == version_id,
            Decision.action == "SELECT_VERSION",
        )
    )
    if existing_decision is not None:
        if existing_decision.selected_item_id != logo_id:
            raise StageDecisionConflictError("This Logo version already has another selection")
        existing_run = await session.get(
            StageRun,
            existing_decision.resulting_stage_run_id,
        )
        if existing_run is None:
            raise StageDecisionConflictError("Logo decision has no resulting Stage run")
        return existing_run, existing_decision, None

    run_id = str(uuid4())
    decision_id = str(uuid4())
    resume_payload = {
        "version_id": version_id,
        "selected_item_id": logo_id,
    }
    resumed_run = StageRun(
        id=run_id,
        workflow_thread_id=source_run.workflow_thread_id,
        parent_stage_run_id=source_run.id,
        project_id=source_run.project_id,
        stage="VI",
        status="QUEUED",
        idempotency_key=f"select-logo:{version_id}",
        input_json={
            "resume": resume_payload,
            "decision_id": decision_id,
            "logo_version_id": version_id,
        },
    )
    session.add(resumed_run)
    await session.flush()
    decision = Decision(
        id=decision_id,
        project_id=source_run.project_id,
        stage="LOGO",
        action="SELECT_VERSION",
        source_version_id=version_id,
        selected_item_id=logo_id,
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
    project = await session.get(Project, source_run.project_id)
    if project is None:
        raise StageResumeNotFoundError("Project not found")
    if project.status == "COMPLETED":
        raise StageResumeConflictError("Completed project cannot accept intake answers")
    if source_run.stage != "INTAKE" or source_run.status != "SUCCEEDED":
        raise StageResumeConflictError("Only a succeeded Intake run can accept answers")
    if source_run.result_version_id is None:
        raise StageResumeConflictError("Intake run has no result to resume")
    source_version = await session.get(StageVersion, source_run.result_version_id)
    if source_version is None or source_version.project_id != source_run.project_id:
        raise StageResumeNotFoundError("Intake version not found")
    if source_version.status != "GENERATED":
        raise StageResumeConflictError("Only a generated Intake version can accept answers")

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
    if stage_run.status in {"SUCCEEDED", "WAITING_USER"}:
        return stage_run
    if stage_run.stage not in {
        "INTAKE",
        "DIRECTIONS",
        "LOGO",
        "VI",
        "IP",
        "MATERIALS",
        "REVIEW",
        "PROPOSAL",
    }:
        raise ValueError(
            "Only INTAKE, DIRECTIONS, LOGO, VI, IP, MATERIALS, REVIEW, and PROPOSAL "
            "are supported by this worker milestone"
        )

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
            "VI": (VIOutput, "vi_output"),
            "IP": (IPOutput, "ip_output"),
            "MATERIALS": (MaterialOutput, "material_output"),
            "REVIEW": (ReviewOutput, "review_output"),
            "PROPOSAL": (ProposalOutput, "proposal_output"),
        }
        interrupt_value = result.get("__interrupt__", [None])[0]
        interrupt_kind = getattr(interrupt_value, "value", {}).get("kind")
        if stage_run.stage == "IP" and interrupt_kind == "ip_choice":
            stage_run.status = "WAITING_USER"
            project = await session.get(Project, stage_run.project_id)
            if project is None:
                raise ValueError("Project not found")
            project.current_stage = stage_run.stage
            project.version += 1
            await session.commit()
            return stage_run

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
        if stage_run.stage == "VI":
            input_refs.update(
                {
                    "logo_version_id": stage_run.input_json["logo_version_id"],
                    "decision_id": stage_run.input_json["decision_id"],
                }
            )
        if stage_run.stage == "IP":
            input_refs.update(
                {
                    "vi_version_id": stage_run.input_json["vi_version_id"],
                    "decision_id": stage_run.input_json["decision_id"],
                    "ip_skipped": stage_run.input_json.get("ip_skipped", False),
                }
            )
        if stage_run.stage == "MATERIALS":
            input_refs["decision_id"] = stage_run.input_json["decision_id"]
            input_refs["ip_skipped"] = stage_run.input_json.get("ip_skipped", False)
            if input_refs["ip_skipped"]:
                input_refs["vi_version_id"] = stage_run.input_json["vi_version_id"]
            else:
                input_refs["ip_version_id"] = stage_run.input_json["ip_version_id"]
        if stage_run.stage == "REVIEW":
            input_refs.update(
                {
                    "materials_version_id": stage_run.input_json["materials_version_id"],
                    "decision_id": stage_run.input_json["decision_id"],
                }
            )
        if stage_run.stage == "PROPOSAL":
            input_refs.update(
                {
                    "review_version_id": stage_run.input_json["review_version_id"],
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
            output_json=output.model_dump(mode="json", by_alias=True),
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
