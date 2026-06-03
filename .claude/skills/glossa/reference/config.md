# Glossa configuration reference

All settings live in `glossa/config.py` (a pydantic `Settings`); env vars use the
`GLOSSA_` prefix. Access via `from glossa.config import get_settings; s = get_settings()`.
Copy `.env.example` → `.env` to start. Model strings in `.env.example` are
illustrative placeholders; the code defaults are below.

## Server / infra

| Env var | Default | Meaning |
|---|---|---|
| `GLOSSA_MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection |
| `GLOSSA_MONGO_DB` | `glossa` | DB name |
| `GLOSSA_MINIO_ENDPOINT` | `localhost:9000` | S3-compatible endpoint |
| `GLOSSA_MINIO_ACCESS_KEY` | `glossa` | MinIO access key |
| `GLOSSA_MINIO_SECRET_KEY` | `glossa-secret` | MinIO secret key |
| `GLOSSA_MINIO_BUCKET` | `glossa-spaces` | Bucket holding all spaces |
| `GLOSSA_MINIO_SECURE` | `false` | TLS to MinIO |
| `GLOSSA_API_HOST` | `0.0.0.0` | Bind host |
| `GLOSSA_API_PORT` | `8200` | Bind port |

## LLM (all inference via Pydantic AI)

All inference runs through **Pydantic AI**. There are exactly **five providers**,
each keyed by its own `GLOSSA_*` setting. Pick one default provider + model; a
space overrides per-space via `llm_config` (precedence: `llm_config.provider` →
`GLOSSA_DEFAULT_LLM_PROVIDER`). The `google`/`bedrock` SDKs are imported lazily in
`glossa/llm/models.py`, so trimming an extra in `requirements.txt` only disables
that one provider.

| Provider | model class | default auth setting(s) |
|---|---|---|
| `anthropic` | `AnthropicModel` | `GLOSSA_ANTHROPIC_API_KEY` |
| `openai` | `OpenAIChatModel` | `GLOSSA_OPENAI_API_KEY` (+ `GLOSSA_OPENAI_BASE_URL`) |
| `gemini` | `GoogleModel` (GLA) | `GLOSSA_GEMINI_API_KEY` |
| `bedrock` | `BedrockConverseModel` | `GLOSSA_AWS_*` / `GLOSSA_BEDROCK_API_KEY` (+ `GLOSSA_AWS_REGION`) |
| `vertex` | `GoogleModel` (Vertex) | `GLOSSA_VERTEX_PROJECT` / `_LOCATION` / `_SERVICE_ACCOUNT_FILE` |

### Default selection

| Env var | Default | Meaning |
|---|---|---|
| `GLOSSA_DEFAULT_LLM_PROVIDER` | `openai` | Provider for spaces that don't set `llm_config.provider`; one of the five above |
| `GLOSSA_DEFAULT_LLM_MODEL` | `gpt-4o-mini` | Model for spaces that don't set `llm_config.model` |

### Per-provider keys

| Env var | Default | Meaning |
|---|---|---|
| `GLOSSA_ANTHROPIC_API_KEY` | `None` | Key for the `anthropic` provider |
| `GLOSSA_OPENAI_API_KEY` | `None` | Key for the `openai` provider |
| `GLOSSA_OPENAI_BASE_URL` | `None` | OpenAI-compatible base URL (Azure, OpenRouter, Groq, vLLM, Ollama, …); unset = `api.openai.com` |
| `GLOSSA_GEMINI_API_KEY` | `None` | Key for the `gemini` provider (Google Gemini Developer API / AI Studio) |
| `GLOSSA_AWS_REGION` | `None` | AWS region for `bedrock` (**required** for that provider) |
| `GLOSSA_AWS_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` / `_SESSION_TOKEN` | `None` | Static AWS creds; if all unset, the host's default AWS credential chain is used |
| `GLOSSA_BEDROCK_API_KEY` | `None` | Bedrock bearer token (alternative to AWS creds) |
| `GLOSSA_VERTEX_PROJECT` / `GLOSSA_VERTEX_LOCATION` | `None` | GCP project + region for `vertex` |
| `GLOSSA_VERTEX_SERVICE_ACCOUNT_FILE` | `None` | Path to a service-account JSON; unset = Application Default Credentials |

### Anthropic-only tuning

| Env var | Default | Meaning |
|---|---|---|
| `GLOSSA_ANTHROPIC_EFFORT` | `high` | Thinking effort level |
| `GLOSSA_ANTHROPIC_MAX_TOKENS` | `16000` | Max output tokens |
| `GLOSSA_ANTHROPIC_ENABLE_THINKING` | `true` | Enable adaptive thinking + prompt caching |

A space's `llm_config.api_key_ref` (`"env:VAR"` or a literal) overrides the
per-provider key when set; otherwise the resolved provider's `GLOSSA_*` key is
used. For Bedrock, per-space `llm_config.extra.region`; for Vertex,
`llm_config.extra.{project,location}` override the settings defaults. See
`reference/internals.md` § Model layer for resolution precedence.

## Ingest

| Env var | Default | Meaning |
|---|---|---|
| `GLOSSA_INGEST_MAX_SOURCE_CHARS` | `200000` | Single source cap (no chunking yet — longer is truncated) |
| `GLOSSA_INGEST_MAX_UPLOAD_BYTES` | `25000000` | Max bytes for a single `upload`-mode file (413 if exceeded) |
| `GLOSSA_LITEPARSE_OCR_ENABLED` | `false` | Enable LiteParse OCR (Tesseract) for scanned uploads |
| `GLOSSA_URL_FETCH_TIMEOUT_SECONDS` | `30` | HTTP timeout when fetching a `url`-mode link |
| `GLOSSA_URL_FETCH_USER_AGENT` | `GlossaBot/0.1 (+…)` | User-Agent sent when fetching `url`-mode links |

### Agentic maintainer guardrails

The ingest maintainer is a Pydantic AI agent that edits pages with surgical patch
tools. These caps bound cost and the per-space lock hold; hitting one ends the run
cleanly (partial flush + log note `[partial: ingest step cap reached]`).

| Env var | Default | Meaning |
|---|---|---|
| `GLOSSA_INGEST_MAX_AGENT_STEPS` | `40` | Max tool calls (model requests) per maintainer run |
| `GLOSSA_INGEST_MAX_PAGES_PER_RUN` | `12` | Max distinct pages one ingest may touch |
| `GLOSSA_INGEST_MAX_EDIT_BYTES` | `200000` | Max total bytes written across the run |
| `GLOSSA_INGEST_AGENT_RETRIES` | `2` | Pydantic AI retries for output/tool validation failures |

`url`-mode ingestion uses **`trafilatura`** (fetch + readable-content→markdown);
`upload`-mode uses **`liteparse`** (`run-llama/liteparse`, local, no API key) to
parse documents to text. Both are in `requirements.txt`; their imports are lazy,
so the rest of Glossa (and the test suite) runs without them installed. LiteParse
parses PDFs natively but needs **LibreOffice** (Office formats), **ImageMagick**
(images), and **Tesseract** (OCR) on the host — the `Dockerfile` installs all
three; drop that apt layer for a smaller PDF-only image.

## Auth modes

| Env var | Default | Meaning |
|---|---|---|
| `GLOSSA_AUTH_REQUIRED` | `false` | `false` = self-host/dev (no header → synthetic admin); `true` = hosted (no/bad header → 401) |
| `GLOSSA_BOOTSTRAP_ADMIN_API_KEY` | `None` | Token that grants a synthetic admin context with no DB row. Issue real keys with it, then unset. |

`docker compose up` runs `auth_required=false` so existing local tooling (MCP,
Obsidian sync) works tokenless. Flip to `true` + issue `glsk_live_…` keys for
tenant enforcement.

## MCP / client / Obsidian helpers

| Env var | Default | Used by |
|---|---|---|
| `GLOSSA_BASE_URL` | `http://localhost:8200` | client, MCP server, **OAuth redirect URIs** |
| `GLOSSA_DEFAULT_SPACE_ID` | `None` | MCP/client default when a call omits `space_id` |
| `GLOSSA_API_TOKEN` | `None` | Bearer token the client/MCP forwards |
| `GLOSSA_OBSIDIAN_VAULT` | `None` | Obsidian vault root |
| `GLOSSA_OBSIDIAN_SUBDIR` | `Glossa` | Subfolder inside the vault |
| `GLOSSA_OBSIDIAN_PAGE_LIMIT` | `1000` | Max pages to mirror |

## Dashboard / sessions / OAuth

| Env var | Default | Meaning |
|---|---|---|
| `GLOSSA_BASE_URL` | `http://localhost:8200` | Public base URL (required for OAuth redirect URIs) |
| `GLOSSA_SESSION_COOKIE_NAME` | `glossa_session` | Session cookie name |
| `GLOSSA_SESSION_TTL_HOURS` | `168` | Session lifetime (7 days) |
| `GLOSSA_SESSION_COOKIE_SECURE` | `false` | Set `true` behind HTTPS in prod |
| `GLOSSA_GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | `None` | Google OAuth app |
| `GLOSSA_GITHUB_OAUTH_CLIENT_ID` / `_SECRET` | `None` | GitHub OAuth app |
| `GLOSSA_OAUTH_STATE_TTL_MINUTES` | `10` | PKCE/CSRF state lifetime |

Register these provider callbacks with each IdP:
`${GLOSSA_BASE_URL}/auth/google/callback`,
`${GLOSSA_BASE_URL}/auth/github/callback`. On first sign-in Glossa creates a
`User`, a starter tenant (`"{name}'s Workspace"`), and an `owner` membership.

## Per-space `api_key_ref`

The five `GLOSSA_*_API_KEY` settings above cover the common case. A space only
needs `llm_config.api_key_ref` when it wants a *different* key than its provider's
default — e.g. `api_key_ref: "env:CUSTOMER_42_OPENAI_KEY"` reading from an extra
environment variable you define, or a literal key string.

## Self-hosting notes

- Dashboard works without OAuth creds in `auth_required=false`, but the only way
  in is the bootstrap-admin API path (no human sign-in). Fine for local dev.
- For real deployments: set the four OAuth creds + `GLOSSA_AUTH_REQUIRED=true`,
  register a first user via OAuth, let them issue keys from the dashboard.
- Rate limiter and per-space lock are **in-process** — multi-worker deployments
  need Redis-backed coordination (deferred; see `reference/internals.md`).
