# E2E Smoke Tests

These tests verify a locally running Docker Compose stack. They are skipped by
default so normal unit-test and CI runs do not require Docker services.

Run manually:

```sh
cp .env.example .env
docker compose up --build -d
BRAND_STUDIO_RUN_E2E=1 .venv/bin/python -m pytest tests/e2e
docker compose stop
```

Current scope:

- Docker Compose long-running services are running and healthy.
- Gateway health endpoint responds.
- Web health endpoint responds.
- API readiness and docs endpoints respond.
- MinIO live health endpoint responds.

After M0 is merged, add the real business flow here.
