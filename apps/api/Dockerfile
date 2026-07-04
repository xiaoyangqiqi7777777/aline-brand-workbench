FROM ghcr.io/astral-sh/uv:0.11.26 AS uv
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/workspace/.venv/bin:$PATH" \
    PYTHONPATH="/workspace/apps/api:/workspace"

WORKDIR /workspace

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY apps/api apps/api
COPY backend backend

WORKDIR /workspace/apps/api

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
