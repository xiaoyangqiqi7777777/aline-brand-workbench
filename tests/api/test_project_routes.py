from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.api.app.main import app
from backend.application.projects import CreateProjectCommand, create_project
from backend.infrastructure.database.models import Base, OutboxEvent, StageRun, StageVersion
from backend.infrastructure.database.session import get_db_session


@dataclass(frozen=True)
class SeededIntakeProject:
    project_id: str
    intake_run_id: str
    intake_version_id: str


@dataclass(frozen=True)
class SeededDirectionsProject:
    project_id: str
    directions_run_id: str
    directions_version_id: str
    direction_ids: list[str]


@pytest.fixture
def api_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, async_sessionmaker[AsyncSession]]]:
    database_path = tmp_path / "api-test.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        poolclass=NullPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def override_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    asyncio.run(create_schema())
    previous_override = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = override_db_session

    with TestClient(app) as client:
        yield client, session_factory

    if previous_override is None:
        app.dependency_overrides.pop(get_db_session, None)
    else:
        app.dependency_overrides[get_db_session] = previous_override
    asyncio.run(engine.dispose())


async def seed_directions_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> SeededDirectionsProject:
    async with session_factory() as session:
        project, intake_run, _ = await create_project(
            session,
            CreateProjectCommand(
                workspace_id="local-workspace",
                actor_id="local-developer",
                name="API 契约测试品牌",
                requirement_text=None,
                structured_fields={"industry": "茶饮"},
                reference_artifact_ids=[],
            ),
        )
        directions_run = StageRun(
            workflow_thread_id=intake_run.workflow_thread_id,
            parent_stage_run_id=intake_run.id,
            project_id=project.id,
            stage="DIRECTIONS",
            status="SUCCEEDED",
            idempotency_key=f"api-test-directions:{project.id}",
            input_json={},
        )
        session.add(directions_run)
        await session.flush()

        direction_ids = ["direction-a", "direction-b", "direction-c"]
        directions_version = StageVersion(
            project_id=project.id,
            stage_run_id=directions_run.id,
            stage="DIRECTIONS",
            version_no=1,
            schema_version=1,
            input_refs_json={"brand_spec_version": 1},
            output_json=build_direction_output(direction_ids),
            status="GENERATED",
        )
        session.add(directions_version)
        await session.flush()
        directions_run.result_version_id = directions_version.id
        project.current_stage = "DIRECTIONS"
        await session.commit()

        return SeededDirectionsProject(
            project_id=project.id,
            directions_run_id=directions_run.id,
            directions_version_id=directions_version.id,
            direction_ids=direction_ids,
        )


async def seed_stage_version(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: str,
    stage: str,
) -> str:
    async with session_factory() as session:
        run = StageRun(
            workflow_thread_id=str(uuid4()),
            project_id=project_id,
            stage=stage,
            status="SUCCEEDED",
            idempotency_key=f"api-test-{stage.lower()}:{project_id}",
            input_json={},
        )
        session.add(run)
        await session.flush()

        version = StageVersion(
            project_id=project_id,
            stage_run_id=run.id,
            stage=stage,
            version_no=1,
            schema_version=1,
            input_refs_json={},
            output_json={},
            status="GENERATED",
        )
        session.add(version)
        await session.flush()
        run.result_version_id = version.id
        await session.commit()
        return version.id


async def seed_logo_version(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: str,
) -> str:
    return await seed_stage_version(session_factory, project_id=project_id, stage="LOGO")


async def seed_succeeded_intake_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> SeededIntakeProject:
    async with session_factory() as session:
        project, intake_run, _ = await create_project(
            session,
            CreateProjectCommand(
                workspace_id="local-workspace",
                actor_id="local-developer",
                name="Intake API 契约测试品牌",
                requirement_text=None,
                structured_fields={},
                reference_artifact_ids=[],
            ),
        )
        intake_run.status = "SUCCEEDED"
        intake_version = StageVersion(
            project_id=project.id,
            stage_run_id=intake_run.id,
            stage="INTAKE",
            version_no=1,
            schema_version=1,
            input_refs_json={},
            output_json={
                "schema_version": 1,
                "ready": False,
                "questions": [
                    {
                        "id": "q-industry",
                        "field_path": "industry",
                        "prompt": "请补充行业。",
                        "reason": "用于生成品牌方向。",
                        "required": True,
                        "answer_type": "TEXT",
                        "options": [],
                    }
                ],
                "brand_spec_patch": {},
                "suggestions": [],
                "conflicts": [],
            },
            status="GENERATED",
        )
        session.add(intake_version)
        await session.flush()
        intake_run.result_version_id = intake_version.id
        await session.commit()

        return SeededIntakeProject(
            project_id=project.id,
            intake_run_id=intake_run.id,
            intake_version_id=intake_version.id,
        )


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


def test_get_project_state_returns_latest_stage_data(api_client) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))

    response = client.get(f"/api/v1/projects/{seeded.project_id}/state")

    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["id"] == seeded.project_id
    assert payload["brand_spec"]["industry"] == "茶饮"
    assert payload["current_stage"] == "DIRECTIONS"
    assert payload["stage_runs"]["DIRECTIONS"]["status"] == "SUCCEEDED"
    assert payload["versions"]["DIRECTIONS"]["id"] == seeded.directions_version_id
    assert payload["decisions"] == []


def test_get_project_state_missing_project_returns_404(api_client) -> None:
    client, _ = api_client

    response = client.get(f"/api/v1/projects/{uuid4()}/state")

    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found"}


def test_list_stage_versions_returns_versions_for_project_stage(api_client) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))

    response = client.get(
        f"/api/v1/projects/{seeded.project_id}/stages/directions/versions",
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload] == [seeded.directions_version_id]
    assert payload[0]["stage"] == "DIRECTIONS"
    assert payload[0]["output"]["directions"][0]["id"] == "direction-a"


def test_list_stage_versions_missing_project_returns_404(api_client) -> None:
    client, _ = api_client

    response = client.get(f"/api/v1/projects/{uuid4()}/stages/directions/versions")

    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found"}


def test_list_stage_versions_invalid_stage_returns_422(api_client) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))

    response = client.get(f"/api/v1/projects/{seeded.project_id}/stages/nope/versions")

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid stage key: nope"}


def test_create_stage_decision_dispatches_logo_run_and_is_idempotent(
    api_client,
    monkeypatch,
) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))
    dispatched_stage_run_ids: list[str] = []

    def fake_delay(stage_run_id: str) -> None:
        dispatched_stage_run_ids.append(stage_run_id)

    from apps.api.app import tasks

    monkeypatch.setattr(tasks.execute_agent_stage, "delay", fake_delay)

    request_payload = {
        "version_id": seeded.directions_version_id,
        "selected_item_id": seeded.direction_ids[0],
        "action": "SELECT_VERSION",
    }

    first_response = client.post(
        f"/api/v1/projects/{seeded.project_id}/stages/directions/decisions",
        json=request_payload,
    )
    repeated_response = client.post(
        f"/api/v1/projects/{seeded.project_id}/stages/directions/decisions",
        json=request_payload,
    )

    assert first_response.status_code == 202
    assert repeated_response.status_code == 202
    first_payload = first_response.json()
    repeated_payload = repeated_response.json()
    assert first_payload["stage_run"]["stage"] == "LOGO"
    assert repeated_payload["stage_run"]["id"] == first_payload["stage_run"]["id"]
    assert repeated_payload["decision"]["id"] == first_payload["decision"]["id"]
    assert dispatched_stage_run_ids == [first_payload["stage_run"]["id"]]

    async def load_outbox_statuses() -> list[tuple[str, int]]:
        async with session_factory() as session:
            events = list(await session.scalars(select(OutboxEvent)))
            return sorted((event.status, event.attempt) for event in events)

    assert asyncio.run(load_outbox_statuses()) == [
        ("PENDING", 0),
        ("PUBLISHED", 1),
    ]


def test_create_stage_decision_conflicting_selection_returns_409(
    api_client,
    monkeypatch,
) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))

    from apps.api.app import tasks

    monkeypatch.setattr(tasks.execute_agent_stage, "delay", lambda _: None)

    endpoint = f"/api/v1/projects/{seeded.project_id}/stages/directions/decisions"
    first_payload = {
        "version_id": seeded.directions_version_id,
        "selected_item_id": seeded.direction_ids[0],
    }
    conflicting_payload = {
        "version_id": seeded.directions_version_id,
        "selected_item_id": seeded.direction_ids[1],
    }

    assert client.post(endpoint, json=first_payload).status_code == 202
    response = client.post(endpoint, json=conflicting_payload)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "This Directions version already has another selection",
    }


def test_create_stage_decision_missing_project_returns_404(api_client) -> None:
    client, _ = api_client

    response = client.post(
        f"/api/v1/projects/{uuid4()}/stages/directions/decisions",
        json={
            "version_id": str(uuid4()),
            "selected_item_id": "direction-a",
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found"}


def test_create_stage_decision_missing_version_returns_404(api_client) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))

    response = client.post(
        f"/api/v1/projects/{seeded.project_id}/stages/directions/decisions",
        json={
            "version_id": str(uuid4()),
            "selected_item_id": "direction-a",
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Stage version not found"}


def test_create_stage_decision_invalid_stage_returns_422(api_client) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))

    response = client.post(
        f"/api/v1/projects/{seeded.project_id}/stages/nope/decisions",
        json={
            "version_id": seeded.directions_version_id,
            "selected_item_id": "direction-a",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid stage key: nope"}


def test_create_stage_decision_unsupported_stage_returns_409(api_client) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))
    logo_version_id = asyncio.run(
        seed_logo_version(session_factory, project_id=seeded.project_id),
    )

    response = client.post(
        f"/api/v1/projects/{seeded.project_id}/stages/logo/decisions",
        json={
            "version_id": logo_version_id,
            "selected_item_id": "logo-a",
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "LOGO SELECT_VERSION decisions are not supported by this worker milestone",
    }


def test_create_stage_decision_confirm_version_skeleton_returns_409(api_client) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))
    vi_version_id = asyncio.run(
        seed_stage_version(
            session_factory,
            project_id=seeded.project_id,
            stage="VI",
        ),
    )

    response = client.post(
        f"/api/v1/projects/{seeded.project_id}/stages/vi/decisions",
        json={
            "version_id": vi_version_id,
            "action": "CONFIRM_VERSION",
            "confirmed": True,
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "VI CONFIRM_VERSION decisions are not supported by this worker milestone",
    }


def test_create_stage_decision_select_version_requires_selected_item(api_client) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))

    response = client.post(
        f"/api/v1/projects/{seeded.project_id}/stages/directions/decisions",
        json={
            "version_id": seeded.directions_version_id,
            "action": "SELECT_VERSION",
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "selected_item_id is required for SELECT_VERSION decisions",
    }


def test_create_stage_decision_confirm_version_requires_confirmation(api_client) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))
    vi_version_id = asyncio.run(
        seed_stage_version(
            session_factory,
            project_id=seeded.project_id,
            stage="VI",
        ),
    )

    response = client.post(
        f"/api/v1/projects/{seeded.project_id}/stages/vi/decisions",
        json={
            "version_id": vi_version_id,
            "action": "CONFIRM_VERSION",
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "confirmed=true is required for CONFIRM_VERSION decisions",
    }


def test_stage_decision_exposes_stale_downstream_versions(
    api_client,
    monkeypatch,
) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))
    stale_logo_version_id = asyncio.run(
        seed_logo_version(session_factory, project_id=seeded.project_id),
    )

    from apps.api.app import tasks

    monkeypatch.setattr(tasks.execute_agent_stage, "delay", lambda _: None)

    decision_response = client.post(
        f"/api/v1/projects/{seeded.project_id}/stages/directions/decisions",
        json={
            "version_id": seeded.directions_version_id,
            "selected_item_id": seeded.direction_ids[0],
        },
    )
    state_response = client.get(f"/api/v1/projects/{seeded.project_id}/state")
    logo_versions_response = client.get(
        f"/api/v1/projects/{seeded.project_id}/stages/logo/versions",
    )

    assert decision_response.status_code == 202
    state_payload = state_response.json()
    logo_versions_payload = logo_versions_response.json()
    assert state_response.status_code == 200
    assert state_payload["current_stage"] == "LOGO"
    assert state_payload["stage_runs"]["LOGO"]["status"] == "QUEUED"
    assert state_payload["versions"]["LOGO"]["id"] == stale_logo_version_id
    assert state_payload["versions"]["LOGO"]["status"] == "STALE"
    assert logo_versions_response.status_code == 200
    assert logo_versions_payload[0]["id"] == stale_logo_version_id
    assert logo_versions_payload[0]["status"] == "STALE"


def test_intake_answers_dispatches_directions_run(api_client, monkeypatch) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_succeeded_intake_project(session_factory))
    dispatched_stage_run_ids: list[str] = []

    def fake_delay(stage_run_id: str) -> None:
        dispatched_stage_run_ids.append(stage_run_id)

    from apps.api.app import tasks

    monkeypatch.setattr(tasks.execute_agent_stage, "delay", fake_delay)

    response = client.post(
        f"/api/v1/stage-runs/{seeded.intake_run_id}/intake-answers",
        json={
            "answers": [
                {
                    "field_path": "industry",
                    "value": "茶饮",
                }
            ]
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["stage"] == "DIRECTIONS"
    assert payload["parent_stage_run_id"] == seeded.intake_run_id
    assert dispatched_stage_run_ids == [payload["id"]]


def test_intake_answers_missing_stage_run_returns_404(api_client) -> None:
    client, _ = api_client

    response = client.post(
        f"/api/v1/stage-runs/{uuid4()}/intake-answers",
        json={
            "answers": [
                {
                    "field_path": "industry",
                    "value": "茶饮",
                }
            ]
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Stage run not found"}


def test_intake_answers_conflicting_stage_run_returns_409(api_client) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))

    response = client.post(
        f"/api/v1/stage-runs/{seeded.directions_run_id}/intake-answers",
        json={
            "answers": [
                {
                    "field_path": "industry",
                    "value": "茶饮",
                }
            ]
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Only a succeeded Intake run can accept answers"}


def test_legacy_direction_selection_dispatches_logo_run(api_client, monkeypatch) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))
    dispatched_stage_run_ids: list[str] = []

    def fake_delay(stage_run_id: str) -> None:
        dispatched_stage_run_ids.append(stage_run_id)

    from apps.api.app import tasks

    monkeypatch.setattr(tasks.execute_agent_stage, "delay", fake_delay)

    response = client.post(
        f"/api/v1/stage-runs/{seeded.directions_run_id}/direction-selection",
        json={
            "version_id": seeded.directions_version_id,
            "direction_id": seeded.direction_ids[0],
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["stage"] == "LOGO"
    assert payload["parent_stage_run_id"] == seeded.directions_run_id
    assert dispatched_stage_run_ids == [payload["id"]]


def test_legacy_direction_selection_missing_stage_run_returns_404(api_client) -> None:
    client, _ = api_client

    response = client.post(
        f"/api/v1/stage-runs/{uuid4()}/direction-selection",
        json={
            "version_id": str(uuid4()),
            "direction_id": "direction-a",
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Stage run not found"}


def test_legacy_direction_selection_conflicting_selection_returns_409(
    api_client,
    monkeypatch,
) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))

    from apps.api.app import tasks

    monkeypatch.setattr(tasks.execute_agent_stage, "delay", lambda _: None)

    endpoint = f"/api/v1/stage-runs/{seeded.directions_run_id}/direction-selection"
    first_payload = {
        "version_id": seeded.directions_version_id,
        "direction_id": seeded.direction_ids[0],
    }
    conflicting_payload = {
        "version_id": seeded.directions_version_id,
        "direction_id": seeded.direction_ids[1],
    }

    assert client.post(endpoint, json=first_payload).status_code == 202
    response = client.post(endpoint, json=conflicting_payload)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "This Directions version already has another selection",
    }


@pytest.mark.parametrize("action", ["redo", "skip"])
def test_stage_control_missing_project_returns_404(api_client, action: str) -> None:
    client, _ = api_client

    response = client.post(f"/api/v1/projects/{uuid4()}/stages/directions/{action}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found"}


@pytest.mark.parametrize("action", ["redo", "skip"])
def test_stage_control_invalid_stage_returns_422(api_client, action: str) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))

    response = client.post(f"/api/v1/projects/{seeded.project_id}/stages/nope/{action}")

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid stage key: nope"}


@pytest.mark.parametrize(
    ("action", "expected_detail"),
    [
        ("redo", "REDO is not supported by this worker milestone for DIRECTIONS"),
        ("skip", "SKIP is not supported by this worker milestone for DIRECTIONS"),
    ],
)
def test_stage_control_supported_stage_returns_current_milestone_error(
    api_client,
    action: str,
    expected_detail: str,
) -> None:
    client, session_factory = api_client
    seeded = asyncio.run(seed_directions_project(session_factory))

    response = client.post(
        f"/api/v1/projects/{seeded.project_id}/stages/directions/{action}",
    )

    assert response.status_code == 409
    assert response.json() == {"detail": expected_detail}
