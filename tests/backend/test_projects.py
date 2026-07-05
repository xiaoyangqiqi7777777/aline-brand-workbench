from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.agents.schemas.directions import DirectionOutput
from backend.agents.schemas.intake import IntakeResumePayload
from backend.agents.schemas.logo import LogoOutput
from backend.agents.testing import InMemoryArtifactWriter
from backend.agents.workflow import build_brand_workflow
from backend.application.projects import (
    CreateProjectCommand,
    create_project,
    get_project,
    get_project_state,
    list_projects,
    list_stage_versions,
)
from backend.application.stage_runs import (
    create_intake_resume_run,
    create_stage_decision,
    execute_stage_run,
    get_stage_run,
    mark_outbox_published,
)
from backend.infrastructure.database.invocations import SqlAlchemyInvocationRecorder
from backend.infrastructure.database.models import (
    Base,
    Decision,
    ModelInvocation,
    OutboxEvent,
    StageRun,
    StageVersion,
)
from backend.providers.models.fake import FakeImageModelProvider, FakeTextModelProvider


class FailingTextModelProvider:
    provider_name = "failing-provider"
    model_name = "failing-model"

    def generate_structured(self, request):
        raise RuntimeError("provider unavailable")


def build_intake_test_workflow(session, stage_run_id, text_provider):
    recorder = SqlAlchemyInvocationRecorder(session, stage_run_id=stage_run_id)
    checkpointer = InMemorySaver()
    workflow = build_brand_workflow(
        text_provider=text_provider,
        image_provider=FakeImageModelProvider(),
        artifact_writer=InMemoryArtifactWriter(),
        invocation_recorder=recorder,
        checkpointer=checkpointer,
        interrupt_before=("generate_directions",),
    )
    return workflow, recorder, checkpointer


def build_direction_output(direction_ids: list[str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "brief": {
            "positioning": "城市茶饮品牌",
            "audience_insight": "年轻消费者需要清爽、有记忆点的日常茶饮。",
            "brand_promise": "用东方茶香提供轻盈的城市片刻。",
            "tone": "清爽、现代、可信赖",
        },
        "directions": [
            {
                "id": direction_id,
                "name": f"方向 {index}",
                "concept": "以东方茶叶和城市线条构建现代视觉。",
                "keywords": ["现代", "东方", "清爽"],
                "palette": [
                    {"name": "Tea Green", "hex": "#2F8F5B", "usage": "主品牌色"},
                    {"name": "Rice White", "hex": "#F6F1E6", "usage": "背景色"},
                    {"name": "Ink Black", "hex": "#1E1E1E", "usage": "文字色"},
                ],
                "typography": {
                    "heading_style": "几何无衬线标题",
                    "body_style": "高可读性无衬线正文",
                },
                "composition": "使用垂直中轴和留白强化识别。",
                "rationale": "平衡东方感和城市效率。",
                "risks": [],
                "image_prompt": "modern tea brand visual direction",
                "preview_asset_id": str(uuid4()),
            }
            for index, direction_id in enumerate(direction_ids, start=1)
        ],
    }


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_project_persists_brand_spec_run_and_outbox(session) -> None:
    project, stage_run, _ = await create_project(
        session,
        CreateProjectCommand(
            workspace_id="workspace-one",
            actor_id="developer-two",
            name="  云山咖啡  ",
            requirement_text="做一个现代、克制的咖啡品牌。",
            structured_fields={
                "industry": "精品咖啡",
                "target_audiences": ["城市通勤者"],
            },
            reference_artifact_ids=[str(uuid4())],
        ),
    )

    assert project.name == "云山咖啡"
    assert project.current_stage == "INTAKE"
    assert project.brand_spec.data_json["industry"] == "精品咖啡"
    assert stage_run.project_id == project.id
    assert stage_run.status == "QUEUED"
    assert stage_run.stage == "INTAKE"
    assert await session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


@pytest.mark.asyncio
async def test_project_queries_are_scoped_to_workspace(session) -> None:
    project, _, _ = await create_project(
        session,
        CreateProjectCommand(
            workspace_id="workspace-one",
            actor_id="developer-two",
            name="演示品牌",
            requirement_text=None,
            structured_fields={},
            reference_artifact_ids=[],
        ),
    )

    visible = await list_projects(session, workspace_id="workspace-one")
    hidden = await list_projects(session, workspace_id="workspace-two")

    assert [item.id for item in visible] == [project.id]
    assert hidden == []
    assert (
        await get_project(
            session,
            project_id=project.id,
            workspace_id="workspace-two",
        )
        is None
    )


@pytest.mark.asyncio
async def test_unknown_brand_spec_field_is_rejected(session) -> None:
    with pytest.raises(ValueError, match="Unsupported BrandSpec fields"):
        await create_project(
            session,
            CreateProjectCommand(
                workspace_id="workspace-one",
                actor_id="developer-two",
                name="演示品牌",
                requirement_text=None,
                structured_fields={"invented_field": "not allowed"},
                reference_artifact_ids=[],
            ),
        )


@pytest.mark.asyncio
async def test_fake_intake_persists_result_and_is_idempotent(session) -> None:
    project, stage_run, outbox = await create_project(
        session,
        CreateProjectCommand(
            workspace_id="workspace-one",
            actor_id="developer-two",
            name="只有名字的品牌",
            requirement_text=None,
            structured_fields={},
            reference_artifact_ids=[],
        ),
    )
    await mark_outbox_published(session, event_id=outbox.id)

    workflow, recorder, checkpointer = build_intake_test_workflow(
        session,
        stage_run.id,
        FakeTextModelProvider(),
    )
    completed = await execute_stage_run(
        session,
        stage_run_id=stage_run.id,
        workflow=workflow,
        invocation_recorder=recorder,
    )
    repeated = await execute_stage_run(
        session,
        stage_run_id=stage_run.id,
        workflow=workflow,
        invocation_recorder=recorder,
    )
    found = await get_stage_run(
        session,
        stage_run_id=stage_run.id,
        workspace_id="workspace-one",
    )

    assert completed.status == "SUCCEEDED"
    assert repeated.result_version_id == completed.result_version_id
    assert found is not None
    _, version = found
    assert version is not None
    assert version.output_json["ready"] is False
    assert {item["field_path"] for item in version.output_json["questions"]} == {
        "industry",
        "brand_background",
        "target_audiences",
        "style_keywords",
    }
    assert await session.scalar(select(func.count()).select_from(StageVersion)) == 1
    assert await session.scalar(select(func.count()).select_from(ModelInvocation)) == 1
    assert (await session.get(OutboxEvent, outbox.id)).status == "PUBLISHED"
    assert project.current_stage == "INTAKE"
    assert checkpointer.get_tuple({"configurable": {"thread_id": stage_run.id}}) is not None


@pytest.mark.asyncio
async def test_failed_intake_keeps_failed_invocation_audit(session) -> None:
    _, stage_run, _ = await create_project(
        session,
        CreateProjectCommand(
            workspace_id="workspace-one",
            actor_id="developer-two",
            name="失败审计测试",
            requirement_text=None,
            structured_fields={},
            reference_artifact_ids=[],
        ),
    )

    workflow, recorder, _ = build_intake_test_workflow(
        session,
        stage_run.id,
        FailingTextModelProvider(),
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await execute_stage_run(
            session,
            stage_run_id=stage_run.id,
            workflow=workflow,
            invocation_recorder=recorder,
        )

    await session.refresh(stage_run)
    invocation = await session.scalar(
        select(ModelInvocation).where(ModelInvocation.stage_run_id == stage_run.id)
    )

    assert stage_run.status == "FAILED"
    assert stage_run.error_code == "INTAKE_EXECUTION_FAILED"
    assert invocation is not None
    assert invocation.status == "FAILED"
    assert invocation.provider == "failing-provider"


@pytest.mark.asyncio
async def test_intake_answers_resume_checkpoint_and_generate_directions(session) -> None:
    project, intake_run, _ = await create_project(
        session,
        CreateProjectCommand(
            workspace_id="workspace-one",
            actor_id="developer-two",
            name="恢复测试品牌",
            requirement_text=None,
            structured_fields={},
            reference_artifact_ids=[],
        ),
    )
    first_workflow, first_recorder, checkpointer = build_intake_test_workflow(
        session,
        intake_run.id,
        FakeTextModelProvider(),
    )
    await execute_stage_run(
        session,
        stage_run_id=intake_run.id,
        workflow=first_workflow,
        invocation_recorder=first_recorder,
    )
    payload = IntakeResumePayload.model_validate(
        {
            "answers": [
                {"field_path": "industry", "value": "茶饮"},
                {"field_path": "brand_background", "value": "城市东方茶饮品牌"},
                {"field_path": "target_audiences", "value": ["年轻城市消费者"]},
                {"field_path": "style_keywords", "value": ["现代", "东方", "清爽"]},
            ]
        }
    )
    directions_run, event = await create_intake_resume_run(
        session,
        source_stage_run_id=intake_run.id,
        workspace_id="workspace-one",
        resume_payload=payload,
    )
    recorder = SqlAlchemyInvocationRecorder(session, stage_run_id=directions_run.id)
    artifacts = InMemoryArtifactWriter()
    workflow = build_brand_workflow(
        text_provider=FakeTextModelProvider(),
        image_provider=FakeImageModelProvider(),
        artifact_writer=artifacts,
        invocation_recorder=recorder,
        checkpointer=checkpointer,
    )

    completed = await execute_stage_run(
        session,
        stage_run_id=directions_run.id,
        workflow=workflow,
        invocation_recorder=recorder,
    )
    version = await session.get(StageVersion, completed.result_version_id)
    direction_output = DirectionOutput.model_validate(version.output_json)

    assert event is not None
    assert completed.status == "SUCCEEDED"
    assert directions_run.parent_stage_run_id == intake_run.id
    assert directions_run.workflow_thread_id == intake_run.workflow_thread_id
    assert len(direction_output.directions) == 3
    assert len(artifacts.items) == 3
    assert project.current_stage == "DIRECTIONS"
    assert project.brand_spec.data_json["industry"] == "茶饮"


@pytest.mark.asyncio
async def test_direction_selection_resumes_checkpoint_and_generates_logo(session) -> None:
    project, intake_run, _ = await create_project(
        session,
        CreateProjectCommand(
            workspace_id="workspace-one",
            actor_id="developer-two",
            name="Logo 恢复测试品牌",
            requirement_text=None,
            structured_fields={},
            reference_artifact_ids=[],
        ),
    )
    intake_workflow, intake_recorder, checkpointer = build_intake_test_workflow(
        session,
        intake_run.id,
        FakeTextModelProvider(),
    )
    await execute_stage_run(
        session,
        stage_run_id=intake_run.id,
        workflow=intake_workflow,
        invocation_recorder=intake_recorder,
    )
    directions_run, _ = await create_intake_resume_run(
        session,
        source_stage_run_id=intake_run.id,
        workspace_id="workspace-one",
        resume_payload=IntakeResumePayload.model_validate(
            {
                "answers": [
                    {"field_path": "industry", "value": "茶饮"},
                    {
                        "field_path": "brand_background",
                        "value": "城市东方茶饮品牌",
                    },
                    {
                        "field_path": "target_audiences",
                        "value": ["年轻城市消费者"],
                    },
                    {
                        "field_path": "style_keywords",
                        "value": ["现代", "东方", "清爽"],
                    },
                ]
            }
        ),
    )
    directions_recorder = SqlAlchemyInvocationRecorder(
        session,
        stage_run_id=directions_run.id,
    )
    directions_workflow = build_brand_workflow(
        text_provider=FakeTextModelProvider(),
        image_provider=FakeImageModelProvider(),
        artifact_writer=InMemoryArtifactWriter(),
        invocation_recorder=directions_recorder,
        checkpointer=checkpointer,
    )
    completed_directions = await execute_stage_run(
        session,
        stage_run_id=directions_run.id,
        workflow=directions_workflow,
        invocation_recorder=directions_recorder,
    )
    directions_version = await session.get(
        StageVersion,
        completed_directions.result_version_id,
    )
    direction_output = DirectionOutput.model_validate(directions_version.output_json)
    selected_id = direction_output.directions[0].id

    logo_run, decision, event = await create_stage_decision(
        session,
        project_id=project.id,
        workspace_id="workspace-one",
        actor_id="developer-two",
        stage_key="directions",
        version_id=directions_version.id,
        selected_item_id=selected_id,
    )
    repeated_run, repeated_decision, repeated_event = await create_stage_decision(
        session,
        project_id=project.id,
        workspace_id="workspace-one",
        actor_id="developer-two",
        stage_key="directions",
        version_id=directions_version.id,
        selected_item_id=selected_id,
    )

    assert event is not None
    assert repeated_event is None
    assert repeated_run.id == logo_run.id
    assert repeated_decision.id == decision.id
    assert logo_run.parent_stage_run_id == directions_run.id
    assert logo_run.workflow_thread_id == intake_run.workflow_thread_id
    assert decision.source_version_id == directions_version.id
    assert decision.selected_item_id == selected_id
    assert await session.scalar(select(func.count()).select_from(Decision)) == 1
    assert project.current_stage == "LOGO"

    with pytest.raises(ValueError, match="already has another selection"):
        await create_stage_decision(
            session,
            project_id=project.id,
            workspace_id="workspace-one",
            actor_id="developer-two",
            stage_key="directions",
            version_id=directions_version.id,
            selected_item_id=direction_output.directions[1].id,
        )

    with pytest.raises(ValueError, match="Project not found"):
        await create_stage_decision(
            session,
            project_id=project.id,
            workspace_id="workspace-two",
            actor_id="developer-two",
            stage_key="directions",
            version_id=directions_version.id,
            selected_item_id=selected_id,
        )

    logo_recorder = SqlAlchemyInvocationRecorder(session, stage_run_id=logo_run.id)
    logo_artifacts = InMemoryArtifactWriter()
    logo_workflow = build_brand_workflow(
        text_provider=FakeTextModelProvider(),
        image_provider=FakeImageModelProvider(),
        artifact_writer=logo_artifacts,
        invocation_recorder=logo_recorder,
        checkpointer=checkpointer,
    )
    completed_logo = await execute_stage_run(
        session,
        stage_run_id=logo_run.id,
        workflow=logo_workflow,
        invocation_recorder=logo_recorder,
    )
    logo_version = await session.get(StageVersion, completed_logo.result_version_id)
    logo_output = LogoOutput.model_validate(logo_version.output_json)

    assert completed_logo.status == "SUCCEEDED"
    assert len(logo_output.concepts) == 3
    assert len(logo_artifacts.items) == 3
    assert logo_version.input_refs_json == {
        "brand_spec_version": 2,
        "direction_version_id": directions_version.id,
        "decision_id": decision.id,
    }
    assert project.current_stage == "LOGO"

    state = await get_project_state(
        session,
        project_id=project.id,
        workspace_id="workspace-one",
    )

    assert state is not None
    assert state.project.id == project.id
    assert state.project.current_stage == "LOGO"
    assert state.project.brand_spec.data_json["industry"] == "茶饮"
    assert {run.stage for run in state.stage_runs} == {"INTAKE", "DIRECTIONS", "LOGO"}
    assert {version.stage for version in state.stage_versions} == {
        "INTAKE",
        "DIRECTIONS",
        "LOGO",
    }
    assert [item.selected_item_id for item in state.decisions] == [selected_id]

    direction_versions = await list_stage_versions(
        session,
        project_id=project.id,
        workspace_id="workspace-one",
        stage_key="directions",
    )
    logo_versions = await list_stage_versions(
        session,
        project_id=project.id,
        workspace_id="workspace-one",
        stage_key="logo",
    )
    hidden_versions = await list_stage_versions(
        session,
        project_id=project.id,
        workspace_id="workspace-two",
        stage_key="directions",
    )

    assert [item.id for item in direction_versions] == [directions_version.id]
    assert [item.id for item in logo_versions] == [logo_version.id]
    assert hidden_versions is None


@pytest.mark.asyncio
async def test_new_direction_selection_marks_downstream_versions_stale(session) -> None:
    project, intake_run, _ = await create_project(
        session,
        CreateProjectCommand(
            workspace_id="workspace-one",
            actor_id="developer-two",
            name="下游过期测试品牌",
            requirement_text=None,
            structured_fields={},
            reference_artifact_ids=[],
        ),
    )
    old_logo_run = StageRun(
        workflow_thread_id=intake_run.workflow_thread_id,
        project_id=project.id,
        stage="LOGO",
        status="SUCCEEDED",
        idempotency_key=f"stale-test-logo:{project.id}",
        input_json={},
    )
    new_directions_run = StageRun(
        workflow_thread_id=intake_run.workflow_thread_id,
        project_id=project.id,
        stage="DIRECTIONS",
        status="SUCCEEDED",
        idempotency_key=f"stale-test-directions:{project.id}",
        input_json={},
    )
    session.add_all([old_logo_run, new_directions_run])
    await session.flush()

    old_logo_version = StageVersion(
        project_id=project.id,
        stage_run_id=old_logo_run.id,
        stage="LOGO",
        version_no=1,
        schema_version=1,
        input_refs_json={},
        output_json={},
        status="GENERATED",
    )
    new_direction_ids = ["new-direction-a", "new-direction-b", "new-direction-c"]
    new_directions_version = StageVersion(
        project_id=project.id,
        stage_run_id=new_directions_run.id,
        stage="DIRECTIONS",
        version_no=1,
        schema_version=1,
        input_refs_json={},
        output_json=build_direction_output(new_direction_ids),
        status="GENERATED",
    )
    session.add_all([old_logo_version, new_directions_version])
    await session.flush()
    old_logo_run.result_version_id = old_logo_version.id
    new_directions_run.result_version_id = new_directions_version.id
    await session.commit()

    next_logo_run, _, event = await create_stage_decision(
        session,
        project_id=project.id,
        workspace_id="workspace-one",
        actor_id="developer-two",
        stage_key="directions",
        version_id=new_directions_version.id,
        selected_item_id=new_direction_ids[0],
    )

    await session.refresh(old_logo_version)

    assert event is not None
    assert old_logo_version.status == "STALE"
    assert next_logo_run.stage == "LOGO"
    assert next_logo_run.status == "QUEUED"
    assert project.current_stage == "LOGO"


@pytest.mark.asyncio
async def test_intake_answers_mark_downstream_versions_stale_and_advance_stage(session) -> None:
    project, intake_run, _ = await create_project(
        session,
        CreateProjectCommand(
            workspace_id="workspace-one",
            actor_id="developer-two",
            name="Intake 下游过期测试品牌",
            requirement_text=None,
            structured_fields={},
            reference_artifact_ids=[],
        ),
    )
    intake_run.status = "SUCCEEDED"
    old_directions_run = StageRun(
        workflow_thread_id=intake_run.workflow_thread_id,
        project_id=project.id,
        stage="DIRECTIONS",
        status="SUCCEEDED",
        idempotency_key=f"stale-test-old-directions:{project.id}",
        input_json={},
    )
    old_logo_run = StageRun(
        workflow_thread_id=intake_run.workflow_thread_id,
        project_id=project.id,
        stage="LOGO",
        status="SUCCEEDED",
        idempotency_key=f"stale-test-old-logo:{project.id}",
        input_json={},
    )
    session.add_all([old_directions_run, old_logo_run])
    await session.flush()

    intake_version = StageVersion(
        project_id=project.id,
        stage_run_id=intake_run.id,
        stage="INTAKE",
        version_no=1,
        schema_version=1,
        input_refs_json={},
        output_json={},
        status="GENERATED",
    )
    old_directions_version = StageVersion(
        project_id=project.id,
        stage_run_id=old_directions_run.id,
        stage="DIRECTIONS",
        version_no=1,
        schema_version=1,
        input_refs_json={},
        output_json={},
        status="GENERATED",
    )
    old_logo_version = StageVersion(
        project_id=project.id,
        stage_run_id=old_logo_run.id,
        stage="LOGO",
        version_no=1,
        schema_version=1,
        input_refs_json={},
        output_json={},
        status="GENERATED",
    )
    session.add_all([intake_version, old_directions_version, old_logo_version])
    await session.flush()
    intake_run.result_version_id = intake_version.id
    old_directions_run.result_version_id = old_directions_version.id
    old_logo_run.result_version_id = old_logo_version.id
    project.current_stage = "LOGO"
    await session.commit()

    directions_run, event = await create_intake_resume_run(
        session,
        source_stage_run_id=intake_run.id,
        workspace_id="workspace-one",
        resume_payload=IntakeResumePayload.model_validate(
            {
                "answers": [
                    {"field_path": "industry", "value": "茶饮"},
                ]
            }
        ),
    )

    await session.refresh(old_directions_version)
    await session.refresh(old_logo_version)

    assert event is not None
    assert directions_run.stage == "DIRECTIONS"
    assert directions_run.status == "QUEUED"
    assert old_directions_version.status == "STALE"
    assert old_logo_version.status == "STALE"
    assert project.current_stage == "DIRECTIONS"
