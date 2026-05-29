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

## LLM — BYO (the only working mode)

| Env var | Default | Meaning |
|---|---|---|
| `GLOSSA_DEFAULT_LLM_MODE` | `byo` | Default mode for new spaces (`byo`\|`hosted`) |
| `GLOSSA_DEFAULT_LLM_ENDPOINT` | `None` | OpenAI-compatible base URL; required for BYO unless the space sets its own |
| `GLOSSA_DEFAULT_LLM_MODEL` | `gpt-4o-mini` | Fallback model |
| `GLOSSA_DEFAULT_LLM_API_KEY` | `None` | Fallback BYO key |

## LLM — hosted (Anthropic; **stubbed — raises NotImplementedError**)

| Env var | Default |
|---|---|
| `GLOSSA_HOSTED_ANTHROPIC_API_KEY` | `None` |
| `GLOSSA_HOSTED_DEFAULT_MODEL` | `claude-opus-4-7` |
| `GLOSSA_HOSTED_DEFAULT_EFFORT` | `high` |
| `GLOSSA_HOSTED_DEFAULT_MAX_TOKENS` | `16000` |
| `GLOSSA_HOSTED_ENABLE_THINKING` | `true` |

Per-space `llm_config.api_key_ref` may be `"env:OPENAI_API_KEY"` /
`"env:ANTHROPIC_API_KEY"`; provide those keys in the environment too. See
`reference/internals.md` § LLM driver factory for resolution order.

## Ingest

| Env var | Default | Meaning |
|---|---|---|
| `GLOSSA_INGEST_MAX_SOURCE_CHARS` | `200000` | Single source cap (no chunking yet — longer is truncated) |

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

## Provider keys (for per-space `api_key_ref`)

| Env var | Meaning |
|---|---|
| `OPENAI_API_KEY` | Referenced by `api_key_ref: "env:OPENAI_API_KEY"` |
| `ANTHROPIC_API_KEY` | Referenced by `api_key_ref: "env:ANTHROPIC_API_KEY"` |

## Self-hosting notes

- Dashboard works without OAuth creds in `auth_required=false`, but the only way
  in is the bootstrap-admin API path (no human sign-in). Fine for local dev.
- For real deployments: set the four OAuth creds + `GLOSSA_AUTH_REQUIRED=true`,
  register a first user via OAuth, let them issue keys from the dashboard.
- Rate limiter and per-space lock are **in-process** — multi-worker deployments
  need Redis-backed coordination (deferred; see `reference/internals.md`).
