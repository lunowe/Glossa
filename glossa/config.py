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
    base_url: str = "http://localhost:8200"  # used by D-Phase 3 for OAuth redirect URIs; harmless here

    session_cookie_name: str = "glossa_session"
    session_ttl_hours: int = 168  # 7 days
    session_cookie_secure: bool = False  # set true in production behind https; SameSite=Lax + Secure

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
    # Document upload (upload-mode sources, parsed with LiteParse during ingest).
    ingest_max_upload_bytes: int = 25_000_000  # 25 MB cap on a single uploaded file
    liteparse_ocr_enabled: bool = False
    # Link ingestion (url-mode sources, fetched + converted to markdown during ingest).
    url_fetch_timeout_seconds: float = 30.0
    url_fetch_user_agent: str = "GlossaBot/0.1 (+https://github.com/glossa/glossa)"

    auth_required: bool = False
    bootstrap_admin_api_key: str | None = None

    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    oauth_state_ttl_minutes: int = 10


def get_settings() -> Settings:
    return Settings()
