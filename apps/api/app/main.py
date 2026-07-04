from fastapi import FastAPI, Response, status

from app.health import DependencyUnavailable, check_dependencies
from app.settings import get_settings

app = FastAPI(
    title="Brand Agent Studio API",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)


@app.get("/api/v1/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok", "service": "api"}


@app.get("/api/v1/health/ready", tags=["health"])
async def readiness(response: Response) -> dict[str, object]:
    try:
        dependencies = await check_dependencies(get_settings())
    except DependencyUnavailable as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "dependencies": exc.dependencies}
    return {"status": "ok", "dependencies": dependencies}
