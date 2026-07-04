import asyncio
from typing import Any

import boto3
from botocore.config import Config
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.settings import Settings


async def check_dependencies(settings: Settings) -> dict[str, str]:
    results = await asyncio.gather(
        _check_database(settings),
        _check_redis(settings),
        _check_object_storage(settings),
        return_exceptions=True,
    )
    names = ("database", "redis", "object_storage")
    statuses: dict[str, str] = {}
    errors: list[str] = []

    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            statuses[name] = "unavailable"
            errors.append(name)
        else:
            statuses[name] = "ok"

    if errors:
        raise DependencyUnavailable(statuses)
    return statuses


class DependencyUnavailable(RuntimeError):
    def __init__(self, dependencies: dict[str, str]) -> None:
        super().__init__("one or more dependencies are unavailable")
        self.dependencies = dependencies


async def _check_database(settings: Settings) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def _check_redis(settings: Settings) -> None:
    client = Redis.from_url(settings.redis_url)
    try:
        await client.ping()
    finally:
        await client.aclose()


async def _check_object_storage(settings: Settings) -> None:
    def head_bucket() -> dict[str, Any]:
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(signature_version="s3v4"),
        )
        return client.head_bucket(Bucket=settings.s3_bucket)

    await asyncio.to_thread(head_bucket)
