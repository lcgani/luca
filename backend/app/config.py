from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "LUCA"
    app_env: str = Field(default="dev", alias="LUCA_APP_ENV")
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
    public_base_url: str = Field(default="http://127.0.0.1:8000", alias="LUCA_PUBLIC_BASE_URL")

    bedrock_text_model_id: str = Field(default="", alias="LUCA_BEDROCK_TEXT_MODEL_ID")
    bedrock_embed_model_id: str = Field(default="", alias="LUCA_BEDROCK_EMBED_MODEL_ID")

    ddb_table: str = Field(default="", alias="LUCA_DDB_TABLE")
    artifacts_bucket: str = Field(default="", alias="LUCA_ARTIFACTS_BUCKET")
    static_bucket: str = Field(default="", alias="LUCA_STATIC_BUCKET")
    cloudfront_distribution_id: str = Field(default="", alias="LUCA_CLOUDFRONT_DISTRIBUTION_ID")
    discovery_state_machine_arn: str = Field(default="", alias="LUCA_DISCOVERY_STATE_MACHINE_ARN")
    generation_state_machine_arn: str = Field(default="", alias="LUCA_GENERATION_STATE_MACHINE_ARN")

    storage_mode: str = Field(default="memory", alias="LUCA_STORAGE_MODE")
    artifact_mode: str = Field(default="local", alias="LUCA_ARTIFACT_MODE")
    workflow_mode: str = Field(default="inline", alias="LUCA_WORKFLOW_MODE")

    session_ttl_hours: int = 24
    default_probe_budget: int = 10
    max_planner_turns: int = 8
    request_timeout_seconds: int = 8
    chunk_size: int = 1200
    chunk_overlap: int = 120
    preview_limit: int = 12


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
