from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = (
        "postgresql+psycopg://brand_studio:brand_studio_local@localhost:5432/brand_studio"
    )
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "brand_studio"
    s3_secret_key: str = "brand_studio_local"
    s3_bucket: str = "brand-studio"
    s3_region: str = "us-east-1"
    model_provider: str = "fake"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
