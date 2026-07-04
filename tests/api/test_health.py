from fastapi.testclient import TestClient

from apps.api.app.main import app


def test_live_health() -> None:
    response = TestClient(app).get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_development_environment_uses_fake_models() -> None:
    response = TestClient(app).get("/api/v1/dev/environment")

    assert response.status_code == 200
    assert response.json()["text_model_provider"] == "fake"
    assert response.json()["image_model_provider"] == "fake"


def test_development_demo_flow_exposes_project_state_contract() -> None:
    response = TestClient(app).get("/api/v1/dev/demo-flow")

    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["id"] == "demo-project-001"
    assert payload["current_stage"] == "LOGO"
    assert payload["stage_runs"]["DIRECTIONS"]["status"] == "SUCCEEDED"
    assert payload["stage_runs"]["LOGO"]["status"] == "QUEUED"
    first_direction = payload["versions"]["DIRECTIONS"]["output"]["directions"][0]
    assert first_direction["id"] == "direction-001"
    assert payload["decisions"][0]["action"] == "SELECT_VERSION"
    assert payload["task"]["status"] == "WAITING_USER"
    assert len(payload["result"]["items"]) == 3
