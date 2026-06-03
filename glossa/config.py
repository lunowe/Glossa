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

    # --- LLM (all inference via Pydantic AI) --------------------------------
    # One default provider + model for spaces that don't override via
    # llm_config. Provider is one of: anthropic | openai | gemini | bedrock |
    # vertex. See glossa/llm/models.py for how each is constructed.
    default_llm_provider: str = "openai"
    default_llm_model: str = "gpt-4o-mini"

    # Per-provider API keys. A space uses the key for its resolved provider
    # unless it sets llm_config.api_key_ref explicitly.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None  # Google Gemini Developer API (AI Studio)

    # OpenAI-compatible servers (Azure, OpenRouter, Groq, vLLM, Ollama, …):
    # point the openai provider at a custom base URL. Unset = api.openai.com.
    openai_base_url: str | None = None

    # AWS Bedrock. Provide static credentials OR a Bedrock bearer token, OR — if
    # all are unset — rely on the host's default AWS credential chain (env vars,
    # ~/.aws, IAM role). A region is always required.
    aws_region: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    bedrock_api_key: str | None = None  # AWS_BEARER_TOKEN_BEDROCK-style bearer token

    # Google Vertex AI. Uses Application Default Credentials unless a service
    # account JSON file is given; project/location target the Vertex endpoint.
    vertex_project: str | None = None
    vertex_location: str | None = None
    vertex_service_account_file: str | None = None

    # Anthropic-only tuning (adaptive thinking + prompt caching are Anthropic
    # features; ignored by the other providers).
    anthropic_effort: str = "high"
    anthropic_max_tokens: int = 16000
    anthropic_enable_thinking: bool = True

    ingest_max_source_chars: int = 200_000
    ingest_max_candidate_entities: int = 12
    # Agentic ingest ("wiki maintainer") guardrails. The maintainer agent edits
    # pages with surgical patch tools under these caps; hitting one ends the run
    # cleanly and is recorded (never silently truncated).
    ingest_max_agent_steps: int = 40  # max tool calls (model requests) per run
    ingest_max_pages_per_run: int = 12  # max distinct pages one ingest may touch
    ingest_max_edit_bytes: int = 200_000  # max total bytes written across the run
    ingest_agent_retries: int = 2  # Pydantic AI retries for output/tool validation
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
