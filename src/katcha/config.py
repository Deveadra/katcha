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
    temporal_production_task_queue: str = "katcha-production"
    temporal_publishing_task_queue: str = "katcha-publishing"
    temporal_longform_task_queue: str = "katcha-longform"

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

    tts_profile: str = "openai_youth_v2"
    renderer_url: str = "http://renderer:8787"
    render_width: int = Field(default=1080, ge=360, le=2160)
    render_height: int = Field(default=1920, ge=640, le=3840)
    render_fps: int = Field(default=30, ge=24, le=60)
    source_audio_volume: float = Field(default=0.45, ge=0, le=1)

    longform_render_width: int = Field(default=1920, ge=1280, le=3840)
    longform_render_height: int = Field(default=1080, ge=720, le=2160)
    longform_render_fps: int = Field(default=30, ge=24, le=60)
    longform_candidate_pool_limit: int = Field(default=80, ge=8, le=500)
    longform_min_segments: int = Field(default=8, ge=3, le=50)
    longform_max_segments: int = Field(default=30, ge=4, le=100)
    longform_default_target_seconds: int = Field(default=720, ge=180, le=3600)
    longform_max_target_seconds: int = Field(default=1800, ge=300, le=7200)

    credential_encryption_key: str | None = None
    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_redirect_uri: str = "http://localhost:8000/v1/integrations/youtube/oauth/callback"
    youtube_include_monetary_scope: bool = False
    youtube_default_category_id: str = "24"
    youtube_upload_chunk_mb: int = Field(default=8, ge=1, le=128)
    youtube_processing_poll_seconds: int = Field(default=30, ge=10, le=300)
    youtube_processing_max_polls: int = Field(default=120, ge=1, le=720)
    youtube_analytics_offsets_hours: str = "1,6,24,72,168,720"

    youtube_data_api_key: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "Katcha/0.1 trend-discovery"
    trend_source_max_backoff_seconds: int = Field(default=21600, ge=60, le=86400)
    trend_min_source_coverage: float = Field(default=0.75, ge=0.0, le=1.0)
    trend_observation_bucket_seconds: int = Field(default=300, ge=60, le=3600)

    log_level: str = "INFO"

    def analytics_offsets_hours(self) -> list[int]:
        values: list[int] = []
        for raw in self.youtube_analytics_offsets_hours.split(","):
            value = int(raw.strip())
            if value < 0:
                raise ValueError("analytics offsets must be non-negative")
            values.append(value)
        return sorted(set(values))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    return settings
