from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GLOSSA_", env_file=".env", extra="ignore")

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "glossa"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "glossa"
    minio_secret_key: str = "glossa-secret"
    minio_bucket: str = "glossa-spaces"
    minio_secure: bool = False

    api_host: str = "0.0.0.0"
    api_port: int = 8200

    default_llm_mode: str = "byo"
    default_llm_endpoint: str | None = None
    default_llm_model: str = "gpt-4o-mini"
    default_llm_api_key: str | None = None

    hosted_anthropic_api_key: str | None = None
    hosted_default_model: str = "claude-opus-4-7"
    hosted_default_effort: str = "high"
    hosted_default_max_tokens: int = 16000
    hosted_enable_thinking: bool = True

    ingest_max_source_chars: int = 200_000

    auth_required: bool = False
    bootstrap_admin_api_key: str | None = None


def get_settings() -> Settings:
    return Settings()
