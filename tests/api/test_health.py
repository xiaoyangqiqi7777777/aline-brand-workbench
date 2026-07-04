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
