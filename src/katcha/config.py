from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KATCHA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    database_url: str = "postgresql+psycopg://katcha:katcha@localhost:5432/katcha"

    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "katcha-media"
    temporal_analysis_task_queue: str = "katcha-analysis"

    s3_endpoint_url: str | None = "http://localhost:9000"
    s3_access_key: str = "katcha"
    s3_secret_key: str = "katcha-local-secret"
    s3_bucket: str = "katcha-media"
    s3_region: str = "auto"
    s3_force_path_style: bool = True

    work_dir: Path = Path("/tmp/katcha")
    analysis_frame_count: int = Field(default=6, ge=3, le=12)
    whisper_model: str = "base.en"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    ai_enabled: bool = False
    ai_budget_usd_monthly: float = Field(default=25.0, ge=0)

    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    return settings
