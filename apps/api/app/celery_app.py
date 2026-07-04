from celery import Celery

from apps.api.app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "brand_agent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["apps.api.app.tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    timezone="Asia/Shanghai",
    enable_utc=True,
)
