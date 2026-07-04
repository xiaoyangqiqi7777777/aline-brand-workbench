import argparse
import asyncio
import logging

from app.health import check_dependencies
from app.settings import get_settings

logger = logging.getLogger("brand_studio.worker")


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logger.info("worker started with provider=%s", settings.model_provider)
    while True:
        await check_dependencies(settings)
        await asyncio.sleep(30)


async def healthcheck() -> None:
    await check_dependencies(get_settings())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    asyncio.run(healthcheck() if args.healthcheck else run())


if __name__ == "__main__":
    main()
