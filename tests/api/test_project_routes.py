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
class SeededDirectionsProject:
    project_id: str
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
            directions_version_id=directions_version.id,
            direction_ids=direction_ids,
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
