# Glossa

**LLM-maintained wikis as a service. Markdown is the contract.**

You feed Glossa raw sources (papers, articles, transcripts, web pages); it keeps
a structured, interlinked markdown wiki current — entity pages, syntheses, an
index, a log. You *query the wiki* instead of re-retrieving and re-synthesizing
every time. The wiki is just markdown files in object storage, so you can
`aws s3 sync` it to your laptop and open it in Obsidian.

FastAPI + MongoDB + MinIO, with an MCP server, one-way Obsidian sync,
multi-tenant API-key auth, per-tenant quotas, and a Jinja2/HTMX dashboard.
Python ≥ 3.12.

> Standalone product. Chatforen is the first integration.

## Mental model — five objects, three interfaces

The entire domain is five objects:

| Object | One line |
|---|---|
| **Space** | One wiki. Owned by a tenant. Backed by one object-storage bucket prefix. `id` = `gls_…` |
| **Source** | One raw artifact. `push` (content inline), `pull` (`fetch_callback`), `url` (fetch a link → markdown), or `upload` (a document parsed to text). `id` = `src_…` |
| **Page** | One markdown file, `kind`-typed via frontmatter (`entity`, `topic`, `summary`, `synthesis`, …). |
| **Job** | One async op (`ingest`, `lint`, `reindex`) with a `status` + `result`. `id` = `job_…` |
| **Webhook** | Host integration callback, Stripe-style signed. `id` = `wh_…` |

…and three pluggable interfaces:

- **`StorageBackend`** — where pages live (MinIO today; in-memory for tests; FS/S3 later).
- **`LLMDriver`** — who runs inference (`byo` = any OpenAI-compatible endpoint; `hosted` = Anthropic, **stubbed**).
- **`SourceProvider`** *(implicit)* — push (content stored), pull (`fetch_callback` lets the host stay system of record), url (a pasted link, fetched + converted to markdown), or upload (a document parsed to text with LiteParse).

The API surface, the 5-object model, the bucket layout, and the webhook
signature format are the **stable contract**. Everything behind it (pipeline
steps, the in-process job runner) is implementation that can change.

## How it works

The whole lifecycle is a handful of calls:

```
POST /spaces                                  → create a wiki (Space)
POST /spaces/{id}/sources                     → hand it a Source (push / pull / url)
POST /spaces/{id}/sources/upload              → upload a document (PDF/DOCX/PPTX/…)
POST /spaces/{id}/sources/{sid}/ingest        → queue an ingest Job
GET  /jobs/{job_id}                            → poll until succeeded   (or subscribe a webhook)
POST /spaces/{id}/query   { "question": … }   → synthesized answer + citations back to sources
POST /spaces/{id}/lint                         → queue a lint Job (orphans / broken links / contradictions)
```

**Ingest** (one source at a time per space): fetch content → one LLM call
extracts `{entities, summary, log_blurb}` → for each entity, read the existing
page and LLM-merge the new claims → write a deterministic `summaries/src-<id>`
page → regenerate `index.md` → append a line to `log.md` → fire webhooks.

**Query** makes two LLM calls: *route* (pick ≤8 pages from `index.md`) then
*answer* (write markdown with `[[path]]` citations). Citations resolve back to
each cited source's `external_uri`, so integrators can deep-link to the host.

**Lint** runs three checks: orphans and broken `[[wikilinks]]` (deterministic) +
contradictions/supersessions (LLM, only on pages citing ≥2 sources). It writes
`lint_report.md`, a `log.md` entry, and structured findings on the Job.

Jobs are serialised per-space (one ingest/lint at a time).

## Run locally

```sh
docker compose up --build
# API:   http://localhost:8200/docs   (OpenAPI — the authoritative endpoint list)
# MinIO: http://localhost:9001        (glossa / glossa-secret)
# Mongo: localhost:27017
```

`docker compose up` starts in **`GLOSSA_AUTH_REQUIRED=false`** (self-host/dev): no
token needed, every request gets a synthetic admin context, and the local tooling
(MCP server, Obsidian sync) works tokenless. Set `GLOSSA_AUTH_REQUIRED=true` and
issue real `glsk_live_…` keys to enforce tenants.

Config lives in `glossa/config.py`; copy `.env.example` → `.env` to start. You
need a BYO LLM endpoint — set `GLOSSA_DEFAULT_LLM_ENDPOINT` (any OpenAI-compatible
URL), `GLOSSA_DEFAULT_LLM_MODEL`, and `GLOSSA_DEFAULT_LLM_API_KEY`, or set
`llm_config` per space.

Tests / lint / format (run before committing):

```sh
pip install -r requirements-dev.txt
pytest && ruff check . && ruff format --check .
```

## API

The live OpenAPI at `/docs` is authoritative. The surface:

```
# Spaces
POST   /spaces                                   create
GET    /spaces                                   list (?tenant_id= for admin)
GET    /spaces/{id}                              detail
PATCH  /spaces/{id}                              update name / llm_config
GET    /spaces/{id}/schema                        read schema.md
PUT    /spaces/{id}/schema                        write schema.md

# Sources
POST   /spaces/{id}/sources                       receive a source (push/pull/url)
POST   /spaces/{id}/sources/upload                upload a document (multipart; PDF/DOCX/PPTX/…)
GET    /spaces/{id}/sources                       list
GET    /spaces/{id}/sources/{sid}                 detail
POST   /spaces/{id}/sources/{sid}/ingest          queue an ingest Job

# Pages  (path is logical: no `pages/` prefix, no `.md`)
GET    /spaces/{id}/pages                          list (?kind= &path_prefix=)
GET    /spaces/{id}/pages/{path}                   one page + markdown content
GET    /spaces/{id}/index                          index.md
GET    /spaces/{id}/log                            log.md (?tail=N)
GET    /spaces/{id}/lint-report                    lint_report.md (404 if none yet)

# Jobs
GET    /jobs/{job_id}                              status + result

# Query & lint
POST   /spaces/{id}/query                          { question } → answer + citations   (scope: query)
POST   /spaces/{id}/lint                            queue a lint Job                     (scope: lint)

# Webhooks
POST   /spaces/{id}/webhooks                       register
GET    /spaces/{id}/webhooks                       list
DELETE /spaces/{id}/webhooks/{wid}                 remove

# Tenants & API keys (admin)
POST   /admin/tenants                              create tenant
GET    /admin/tenants                              list (?status=)
GET    /admin/tenants/{tid}                        detail
PATCH  /admin/tenants/{tid}                        suspend / reactivate / re-plan
POST   /tenants/{tid}/api-keys                     issue (plaintext shown once)
GET    /tenants/{tid}/api-keys                     list (no plaintext)
DELETE /tenants/{tid}/api-keys/{kid}               revoke

# Activity, usage & quotas
GET    /tenants/{tid}/activity/requests            paginated audit log
GET    /tenants/{tid}/activity/summary             counts by status / path
GET    /tenants/{tid}/usage                        token/cost summary (?period=YYYY-MM)
GET    /tenants/{tid}/quota                         live usage gauges
PUT    /tenants/{tid}/quota                         set caps

# Meta
GET    /healthz
```

### Quickstart — drive it end to end

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

# 3b. …or paste a link (fetched + converted to markdown on ingest)
curl -s -X POST $BASE/spaces/$SID/sources -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Blog post","ingestion_mode":"url","external_uri":"https://example.com/post"}'

# 3c. …or upload a document (parsed to text with LiteParse on ingest)
curl -s -X POST $BASE/spaces/$SID/sources/upload -H "Authorization: Bearer $KEY" \
  -F file=@report.pdf -F title="Q3 report"
# then POST …/sources/{sid}/ingest as above

# 4. Query
curl -s -X POST $BASE/spaces/$SID/query -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"question":"What did Allianz report?"}'
```

## Authentication

Every API call carries a Bearer token tied to a tenant:

```
Authorization: Bearer glsk_live_<random>
```

One key ↔ one tenant. The plaintext key is shown **once** at creation — only the
SHA-256 hash is stored. Lose it, rotate it.

### Modes

`GLOSSA_AUTH_REQUIRED` (default `false`) controls the *no-header* path:

| Mode | No header | Bad / revoked header |
|---|---|---|
| `false` (self-host / dev) | synthetic admin context; all routes pass | 401 |
| `true` (hosted) | 401 | 401 |

A request that **does** carry an `Authorization` header is always validated —
sending a token is a positive identity claim, never silently ignored.

### Bootstrapping the first key

Set `GLOSSA_BOOTSTRAP_ADMIN_API_KEY=<long random string>`. A request bearing
exactly that token gets a synthetic admin context — no DB row required. Use it
once to create a tenant and issue a real key (see the quickstart above), then
unset it.

### Scopes

Issued keys carry a list of scopes. Default set: `spaces:read`, `spaces:write`,
`sources:write`, `query`, `lint` — `admin` is **not** included. `admin` unlocks
all `/admin/*` and cross-tenant operations. A missing scope returns **403**.

### Tenant isolation & quotas

Every tenant-scoped route filters by the caller's `tenant_id`. Cross-tenant
access returns **404**, never 403 — Glossa never reveals whether someone else's
space, job, or page exists. A quota-exceeded request returns **402** with
`{reason, quota}`.

Quotas have six dimensions (all optional; null = unlimited): monthly cost,
monthly tokens, max sources per space, max storage bytes, requests per minute,
and an allowed-models whitelist. Set them with `PUT /tenants/{tid}/quota`; read
live gauges with `GET /tenants/{tid}/quota`. Every request (except `/healthz`)
is recorded for the audit log under `/tenants/{tid}/activity/*`.

> The rate limiter and per-space lock are in-process. Multi-worker deployments
> need Redis-backed coordination (deferred).

## Dashboard

A browser dashboard at `/dashboard/` is the recommended way for humans to manage
keys, members, activity, and quotas. It uses **session-cookie auth** (HttpOnly),
distinct from the API-key auth above.

Sign-in is OAuth federation — **Google** or **GitHub** (no email + password).
On first sign-in Glossa creates a `User`, auto-creates a starter tenant
(`"{name}'s Workspace"`), and makes the user its `owner`. Signing in with Google
then GitHub under the same email links both accounts to one user.

| Path | What |
|---|---|
| `/dashboard/` | Your tenants (with role pills) |
| `/dashboard/t/{tid}/members` | List + add/remove members + role change |
| `/dashboard/t/{tid}/invites` | Token-link invites (admin/owner only, single-use, no email sent) |
| `/dashboard/t/{tid}/keys` | Issue / revoke API keys (plaintext shown once) |
| `/dashboard/t/{tid}/activity` | Recent requests + 24h summary |
| `/dashboard/t/{tid}/quotas` | Live gauges for all six dimensions + update form |

Roles: **owner** & **admin** manage members/keys/quotas; **member** is
dashboard-read-only but can use the API. You can't demote or remove the sole
owner. Register `${GLOSSA_BASE_URL}/auth/{google,github}/callback` with each IdP
and set the OAuth credentials in `.env`.

## Integrations

- **Python client** — `from glossa.mcp.client import GlossaClient`. Async,
  `httpx`-based; `GlossaClient.from_env()` reads `GLOSSA_BASE_URL` /
  `GLOSSA_API_TOKEN` / `GLOSSA_DEFAULT_SPACE_ID`. Covers read + source / ingest /
  query / lint; call the HTTP API directly for admin/key/quota/webhook ops.
- **MCP server** — `glossa-mcp` exposes Glossa as MCP tools (`glossa_query`,
  `glossa_list_spaces`, `glossa_list_pages`, `glossa_get_page`,
  `glossa_add_source`, `glossa_get_job`, `glossa_lint`) + two resources
  (`glossa://{space_id}/index`, `…/log`). Drop it into Claude Desktop/Code,
  Cursor, or Zed via the standard `mcpServers` config.
- **Obsidian mirror** — `glossa-obsidian-sync` mirrors one Space into an Obsidian
  vault. **One-way** by design (Glossa owns the wiki). Writes `schema.md`,
  `index.md`, `log.md`, `lint_report.md`, and every page at its logical path.
- **Webhooks** — outbound deliveries carry
  `X-Glossa-Signature: t=<unix>,v1=<hex_hmac_sha256>` where
  `v1 = HMAC_SHA256(secret, f"{t}.".encode() + body_bytes)`. Verify with
  `glossa.webhooks.signing.verify_signature` (rejects replays older than 5 min).

## Bucket layout (one prefix per Space)

```
{minio_bucket}/{space_id}/
├── schema.md            # LLM-facing config; co-evolves with the tenant
├── index.md             # auto-maintained catalog (logical wikilinks)
├── log.md               # auto-maintained chronological log (## [iso] kind | title)
├── lint_report.md       # latest lint pass (after first POST /lint)
├── pages/
│   ├── entities/<type>/<slug>.md
│   ├── syntheses/<slug>.md
│   └── summaries/src-<source_id>.md
└── assets/src-<source_id>/<file>
```

A Page's `path` in the DB is **logical** — no `pages/` prefix, no `.md` (e.g.
`entities/companies/allianz`); the storage object key is `pages/{path}.md`, and
wikilinks use the logical path. `aws s3 sync` it anywhere — the wiki travels.

## Status

v0.1 functional MVP. The contract — API surface, bucket layout, 5-object model,
webhook signature — is stable. Known gaps:

- **Hosted `LLMDriver` is stubbed** — only `byo` (OpenAI-compatible) works.
- No content chunking for very long sources (capped at
  `GLOSSA_INGEST_MAX_SOURCE_CHARS`, default 200k — longer is truncated).
- **Document upload** parses PDFs natively; Office formats need LibreOffice,
  images need ImageMagick, and OCR needs Tesseract on the host (the `Dockerfile`
  installs all three). Uploaded raw files count as one source but their bytes are
  not yet metered against `max_storage_bytes`.
- No backlink derivation pass.
- Jobs run in-process (`asyncio.create_task`); a job in flight at restart is
  stuck in `running`. The per-space lock and rate limiter are in-process too —
  multi-worker deployments need a real worker (Arq/RQ) and Redis-backed locking.

## Working on Glossa?

Read **`.claude/skills/glossa/`** first — it's the working knowledge for this
codebase (the 5-object model, the HTTP API, the Python client, MCP, Obsidian
sync, webhooks, internals, and testing), with focused references under
`reference/`. Keep it and this README consistent when you change the contract
surface; if the skill and the code ever disagree, the code wins.
