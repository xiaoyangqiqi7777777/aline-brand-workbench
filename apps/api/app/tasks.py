import asyncio
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver

from apps.api.app.celery_app import celery_app
from backend.agents.workflow import build_brand_workflow
from backend.application.stage_runs import execute_stage_run
from backend.infrastructure.database.invocations import SqlAlchemyInvocationRecorder
from backend.infrastructure.database.models import Project, StageRun
from backend.infrastructure.database.session import async_session_factory, engine
from backend.infrastructure.storage.s3_artifacts import S3ArtifactWriter
from backend.providers.models.factory import build_model_providers


@celery_app.task(name="dev.health_ping")
def health_ping(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"status": "ok", "payload": payload or {}}


async def _execute_agent_stage(stage_run_id: str) -> dict[str, Any]:
    from apps.api.app.config import get_settings

    settings = get_settings()
    text_provider, image_provider = build_model_providers(
        text_provider_name=settings.text_model_provider,
        image_provider_name=settings.image_model_provider,
    )
    try:
        with PostgresSaver.from_conn_string(settings.database_url) as checkpointer:
            checkpointer.setup()
            async with async_session_factory() as session:
                queued_run = await session.get(StageRun, stage_run_id)
                if queued_run is None:
                    raise ValueError("Stage run not found")
                project = await session.get(Project, queued_run.project_id)
                if project is None:
                    raise ValueError("Project not found")
                invocation_recorder = SqlAlchemyInvocationRecorder(
                    session,
                    stage_run_id=stage_run_id,
                )
                artifact_writer = S3ArtifactWriter(
                    session,
                    workspace_id=project.workspace_id,
                    project_id=project.id,
                    stage_run_id=stage_run_id,
                    bucket=settings.s3_bucket,
                    endpoint_url=settings.s3_endpoint_url,
                    access_key_id=settings.s3_access_key_id,
                    secret_access_key=settings.s3_secret_access_key,
                    region=settings.s3_region,
                    use_ssl=settings.s3_use_ssl,
                )
                workflow = build_brand_workflow(
                    text_provider=text_provider,
                    image_provider=image_provider,
                    artifact_writer=artifact_writer,
                    invocation_recorder=invocation_recorder,
                    checkpointer=checkpointer,
                    interrupt_before=(
                        ("generate_directions",) if queued_run.stage == "INTAKE" else ()
                    ),
                )
                stage_run = await execute_stage_run(
                    session,
                    stage_run_id=stage_run_id,
                    workflow=workflow,
                    invocation_recorder=invocation_recorder,
                )
            return {
                "stage_run_id": stage_run.id,
                "status": stage_run.status,
                "result_version_id": stage_run.result_version_id,
            }
    finally:
        await engine.dispose()


@celery_app.task(name="agent.execute_stage_run")
def execute_agent_stage(stage_run_id: str) -> dict[str, Any]:
    return asyncio.run(_execute_agent_stage(stage_run_id))
