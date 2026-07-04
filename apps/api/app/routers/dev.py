import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from apps.api.app.config import get_settings

router = APIRouter(prefix="/dev", tags=["development"])
CONTRACTS_DIR = Path(__file__).resolve().parents[4] / "contracts" / "examples"


@router.get("/environment")
async def environment() -> dict[str, str]:
    settings = get_settings()
    return {
        "app_env": settings.app_env,
        "text_model_provider": settings.text_model_provider,
        "image_model_provider": settings.image_model_provider,
    }


@router.get("/demo-flow")
async def demo_flow() -> dict[str, Any]:
    path = CONTRACTS_DIR / "demo-flow.json"
    return json.loads(path.read_text(encoding="utf-8"))
