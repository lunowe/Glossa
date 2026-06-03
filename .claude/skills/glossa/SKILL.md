---
name: glossa
description: >
  Work with and extend Glossa — the LLM-maintained-wiki service in this repo
  (FastAPI + MongoDB + MinIO, plus an MCP server, one-way Obsidian sync,
  multi-tenant API-key auth, per-tenant quotas, and a Jinja2/HTMX dashboard).
  Use whenever the task touches Glossa: calling its HTTP API or the Python
  GlossaClient, wiring the glossa-mcp server into Claude Desktop/Code/Cursor/Zed,
  mirroring a Space into an Obsidian vault, signing or verifying webhooks, or
  changing internals (routes, the 5 data models, storage backends, LLM drivers,
  the ingest/query/lint pipelines, auth/quota) and writing tests for them.
---

# Glossa

> **Glossa = LLM-maintained wikis as a service. Markdown is the contract.**
> You feed it raw sources (papers, articles, transcripts, web pages); it keeps a
> structured, interlinked markdown wiki current — entity pages, syntheses, an
> index, a log. You *query the wiki* instead of re-retrieving and re-synthesizing
> every time. The wiki is just markdown files in object storage; you can
> `aws s3 sync` it and open it in Obsidian.

This skill is the working knowledge for building **with** Glossa (API / SDK / MCP
/ Obsidian / webhooks) and for changing Glossa **itself** (routes, models,
pipelines, storage/LLM backends). Read this page first, then open the focused
reference under `reference/` for whatever you're doing.

## Mental model — five objects, three interfaces

The entire domain is five objects. Learn these and the rest follows.

| Object | One line |
|---|---|
| **Space** | One wiki. Owned by a tenant. Backed by one object-storage bucket prefix. `id` = `gls_…` |
| **Source** | One raw artifact. `push` (content inline) or `pull` (fetch_callback). `id` = `src_…` |
| **Page** | One markdown file, `kind`-typed via frontmatter (`entity`, `topic`, `summary`, `synthesis`, …). |
| **Job** | One async op (`ingest`, `lint`, `reindex`) with a `status` + `result`. `id` = `job_…` |
| **Webhook** | Host integration callback, Stripe-style signed. `id` = `wh_…` |

Two pluggable interfaces (see `reference/internals.md`):

- **`StorageBackend`** — where pages live (MinIO today; in-memory for tests; FS/S3 later).
- **SourceProvider** *(implicit)* — push (content stored) or pull (`fetch_callback` lets the host stay system of record).

LLM inference runs through **Pydantic AI** (`glossa/llm/models.py`). `build_model`
constructs the right provider from the space's `llm_config`; all agents are
module-level with no model bound (injected at call time).

**The contract is stable; the implementation behind it is not.** Treat the API
surface, the 5-object model, the bucket layout, and the webhook signature format
as the public contract. Everything in `reference/internals.md` (pipeline steps,
function names, the in-process job runner) is implementation that can change.

## The flow at a glance

```
POST /spaces                                  → create a wiki (Space)
POST /spaces/{id}/sources                     → hand it a Source (push or pull)
POST /spaces/{id}/sources/{sid}/ingest        → queue an ingest Job
GET  /jobs/{job_id}                            → poll until succeeded   (or subscribe a webhook)
POST /spaces/{id}/query   { "question": … }   → synthesized answer + citations back to sources
POST /spaces/{id}/lint                         → queue a lint Job (orphans / broken links / contradictions)
```

Ingest (per source): **fetch → extract (single LLM call: entities + summary) →
write summary page → agentic maintainer with surgical patch tools (dedup, minimal
section/substring edits, synthesis) → deterministic flush → regenerate index →
append log → fire webhooks.** Jobs are serialised per-space (one ingest/lint at a
time). Full detail in `reference/internals.md`.

## Reference map — open what you need

| You're doing… | Read |
|---|---|
| Calling the HTTP API; need exact endpoints, request/response shapes, scopes, errors | `reference/api.md` |
| Need field names / enums / frontmatter / slug rules / bucket layout | `reference/data-model.md` |
| Setting env vars, auth modes, bootstrap, OAuth, quotas config | `reference/config.md` |
| Using the Python `GlossaClient`, verifying webhooks, wiring MCP, syncing Obsidian, building a host integration | `reference/integrations.md` |
| Changing routes/models/pipelines, adding a StorageBackend or Pydantic AI provider, understanding ingest/query/lint internals & concurrency | `reference/internals.md` |
| Writing or running tests (fixtures, fake LLM, endpoint test client) | `reference/testing.md` |

## Run it locally

```sh
docker compose up --build
# API:   http://localhost:8200/docs   (OpenAPI — the live, authoritative endpoint list)
# MinIO: http://localhost:9001        (glossa / glossa-secret)
# Mongo: localhost:27017
```

`docker compose up` starts in **`GLOSSA_AUTH_REQUIRED=false`** (self-host/dev): no
token needed, every request gets a synthetic admin context. Set
`GLOSSA_AUTH_REQUIRED=true` and issue real `glsk_live_…` keys to enforce tenants.

Tests / lint / format (run before committing — see `reference/testing.md`):

```sh
pip install -r requirements-dev.txt
pytest
ruff check .
ruff format --check .
```

## Quickstart — drive the API end to end

In hosted mode every call carries `Authorization: Bearer glsk_live_…`. Bootstrap
the first key with `GLOSSA_BOOTSTRAP_ADMIN_API_KEY` (a synthetic admin token — no
DB row), use it once to create a tenant + real key, then unset it.

```sh
BASE=http://localhost:8200
# In dev (auth_required=false) you can drop the Authorization header entirely.
AUTH="Authorization: Bearer $GLOSSA_BOOTSTRAP_ADMIN_API_KEY"

# 1. Tenant + key (admin)
TID=$(curl -s -X POST $BASE/admin/tenants -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme","owner_email":"ops@acme.com"}' | jq -r .id)
KEY=$(curl -s -X POST $BASE/tenants/$TID/api-keys -H "$AUTH" \
  -d '{"label":"prod"}' | jq -r .plaintext)          # plaintext shown ONCE

# 2. Space
SID=$(curl -s -X POST $BASE/spaces -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Research Wiki"}' | jq -r .id)

# 3. Source (push) → ingest → poll
SRC=$(curl -s -X POST $BASE/spaces/$SID/sources -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Q3 report","ingestion_mode":"push","content_inline":"Allianz reported …"}' | jq -r .id)
JOB=$(curl -s -X POST $BASE/spaces/$SID/sources/$SRC/ingest -H "Authorization: Bearer $KEY" | jq -r .id)
curl -s $BASE/jobs/$JOB -H "Authorization: Bearer $KEY"     # poll: queued→running→succeeded

# 4. Query
curl -s -X POST $BASE/spaces/$SID/query -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"question":"What did Allianz report?"}'
```

(Python equivalent via `GlossaClient` is in `reference/integrations.md`.)

## Conventions that bite if you miss them

- **Logical page path vs storage key.** A Page's `path` in the DB has **no
  `pages/` prefix and no `.md`** (e.g. `entities/companies/allianz`). The storage
  object key is `pages/{path}.md`. Wikilinks and `index.md` use the logical path:
  `[[entities/companies/allianz]]`. Summaries live at `summaries/src-<source_id>`.
- **Cross-tenant access returns 404, never 403.** Glossa never reveals whether
  someone else's space/job/page exists. Quota-exceeded returns **402**.
- **Keys are shown once.** `POST …/api-keys` returns `plaintext` exactly once;
  only the SHA-256 hash is stored. Lost = rotate.
- **LLM config: five providers, one key each.** Set `llm_config.provider` to
  `anthropic` | `openai` | `gemini` | `bedrock` | `vertex` (else
  `GLOSSA_DEFAULT_LLM_PROVIDER`). Each provider has its own `GLOSSA_*` key/auth
  setting; a space's `api_key_ref` overrides it. Resolution: `provider` set → that
  provider; else default; unknown → `ValueError`. See `reference/config.md` and
  `reference/internals.md` § Model layer.
- **Jobs are in-process** (`asyncio.create_task`). A job in flight at restart is
  stuck in `running`; the per-space lock is an `asyncio.Lock` (single-worker).
- **The Obsidian sync is one-way.** Glossa owns the wiki; Obsidian is a local
  read/browse surface. Don't build write-back.

## Keeping this skill accurate

This skill is part of Glossa's contract surface — **when you change that surface,
update the skill in the same change.** Specifically:

- **New/changed/removed route or request/response field** → update `reference/api.md`.
- **New/changed model field or enum value** (Space/Source/Page/Job/Webhook/Tenant/
  ApiKey/User/TenantMember, or any `*Kind`/`*Status`/`Scope`/`*Event`) → update `reference/data-model.md`.
- **New/changed `GLOSSA_*` env var, auth mode, or quota dimension** → update `reference/config.md`.
- **Change to the Python client, MCP tools/resources, Obsidian CLI flags, or
  webhook signature format** → update `reference/integrations.md`.
- **Change to ingest/query/lint pipeline, the StorageBackend interface, the
  Pydantic AI model layer, or concurrency model** → update `reference/internals.md`.
- **Change to test fixtures / fake LLM / how the test client is built** → update `reference/testing.md`.

The live OpenAPI at `/docs` is the source of truth for endpoints; if this skill
ever disagrees with the code, the code wins — fix the skill. `README.md` is the
human-facing companion to this skill; keep the two consistent.
