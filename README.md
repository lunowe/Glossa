# Glossa

LLM-maintained wikis as a service. Markdown is the contract.

## What it is

You give Glossa a stream of raw sources (papers, articles, transcripts, web
pages). Glossa keeps a structured, interlinked wiki current — entity pages,
topic syntheses, an index, a log — that compounds with every source you add.
You query the wiki instead of re-retrieving and re-synthesizing every time.

The wiki is just a bucket of markdown files. You can `aws s3 sync` it to your
laptop and open it in Obsidian.

> Standalone product. Chatforen is the first integration.

## Architecture

Five objects. That's it.

| | |
|---|---|
| **Space** | One wiki. Owned by a tenant. Backed by one MinIO bucket prefix. |
| **Source** | One raw artifact. `push` (content stored) or `pull` (fetch_callback). |
| **Page** | One markdown file. `kind`-typed via frontmatter (`entity`, `topic`, …). |
| **Job** | One async op (`ingest`, `lint`, `reindex`). |
| **Webhook** | Host integration callbacks. |

Three pluggable interfaces:

- **`StorageBackend`** — where pages live (MinIO today; FS / S3 later)
- **`LLMDriver`** — who provides inference (BYO endpoint or Glossa-hosted)
- **`SourceProvider`** — *(implicit)* push or pull; pull callback lets the host stay system of record

## API

```
# Spaces
POST   /spaces                                    create
GET    /spaces                                    list (tenant filter)
GET    /spaces/{id}                               detail
PATCH  /spaces/{id}                               update name / llm_config
GET    /spaces/{id}/schema                        read schema.md
PUT    /spaces/{id}/schema                        write schema.md

# Sources
POST   /spaces/{id}/sources                       receive a source (push/pull)
GET    /spaces/{id}/sources                       list
GET    /spaces/{id}/sources/{sid}                 detail
POST   /spaces/{id}/sources/{sid}/ingest          queue an ingest Job

# Pages
GET    /spaces/{id}/pages                         list (filter by kind / prefix)
GET    /spaces/{id}/pages/{path}                  one page + markdown content
GET    /spaces/{id}/index                         index.md
GET    /spaces/{id}/log                           log.md (?tail=N)

# Jobs
GET    /jobs/{job_id}                             status + result

# Query (synthesized answer with citations)
POST   /spaces/{id}/query                         { question } → answer + citations

# Lint (orphans / broken links / contradictions)
POST   /spaces/{id}/lint                          queue a lint Job

# Webhooks
POST   /spaces/{id}/webhooks                      register
GET    /spaces/{id}/webhooks                      list
DELETE /spaces/{id}/webhooks/{wid}                remove

# Tenants & API keys (admin)
POST   /admin/tenants                              create tenant
GET    /admin/tenants                              list (?status=)
GET    /admin/tenants/{tid}                        detail
PATCH  /admin/tenants/{tid}                        suspend / reactivate / re-plan
POST   /tenants/{tid}/api-keys                     issue (plaintext shown once)
GET    /tenants/{tid}/api-keys                     list (no plaintext)
DELETE /tenants/{tid}/api-keys/{kid}               revoke

# Activity
GET    /tenants/{tid}/activity/requests            paginated audit log
GET    /tenants/{tid}/activity/summary             counts by status / path

# Meta
GET    /healthz
```

## Authentication

Every API call carries a Bearer token tied to a tenant:

    Authorization: Bearer glsk_live_<random>

Keys are issued through the admin surface and tied 1:1 to a tenant. The
plaintext key is shown ONCE at creation — only the SHA-256 hash is stored.
Lose it, rotate it.

### Modes

`GLOSSA_AUTH_REQUIRED` (default `false`) controls the absence-of-header
path:

| Mode | No header | Bad header |
|------|-----------|------------|
| `false` (self-host / dev) | Synthetic admin context. All routes pass. | 401 |
| `true` (hosted) | 401 | 401 |

A request that DOES carry an `Authorization` header is always validated —
sending a token is a positive identity claim; we don't silently ignore it.

### Bootstrapping the first tenant

Set `GLOSSA_BOOTSTRAP_ADMIN_API_KEY=<some long random string>` in the
environment. Requests bearing exactly that token get a synthetic admin
context — no DB row required. Use it once to issue real keys, then unset
the env var:

    # Create the first tenant
    curl -X POST http://localhost:8200/admin/tenants \
      -H "Authorization: Bearer $GLOSSA_BOOTSTRAP_ADMIN_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"name": "Acme", "owner_email": "ops@acme.com"}'

    # Issue a key for that tenant
    curl -X POST http://localhost:8200/tenants/<tid>/api-keys \
      -H "Authorization: Bearer $GLOSSA_BOOTSTRAP_ADMIN_API_KEY" \
      -d '{"label": "production"}'

The response includes a `plaintext` field exactly once. Save it; the API
will never show it again.

### Tenant isolation

Every route filters by the caller's `tenant_id`. Cross-tenant access
returns **404** (not 403) — Glossa never reveals whether a space, job,
or page exists for someone else.

### Scopes

Issued keys carry a list of scopes. Defaults: `spaces:read`,
`spaces:write`, `sources:write`, `query`, `lint`. `admin` is not in the
default set; admin scope is what unlocks `/admin/*` and cross-tenant
operations.

### Activity & quotas

`GET /tenants/{tid}/activity/requests` — per-request audit log
(method, path, status, duration, key_id). Retained 90 days.

`GET /tenants/{tid}/activity/summary` — counts by status / path over
a window (default 24h).

`PUT /tenants/{tid}/quota` — set caps. Six dimensions:

- `monthly_cost_limit_usd` — LLM spend across the calendar month
- `monthly_token_limit` — total tokens across the calendar month
- `max_sources_per_space` — count of sources in any one space
- `max_storage_bytes` — total page bytes across all spaces of the tenant
- `max_requests_per_minute` — sliding-window rate limit (cost/token/lint/query)
- `allowed_models` — optional whitelist of model strings

`GET /tenants/{tid}/quota` returns the live usage gauge for every dimension.
The rate limiter is in-process; multi-worker deployments need Redis-backed
coordination (deferred).

### Self-hosting

`docker compose up --build` starts in `auth_required=false` mode by
default. Existing local tooling (the MCP server, the Obsidian sync) all
keep working with no token configured. Set `GLOSSA_AUTH_REQUIRED=true`
and issue real keys to flip on tenant enforcement.

The dashboard surface (`/dashboard/`) is the recommended way to manage
keys / activity / quotas once you have a real user. See
[Dashboard](#dashboard) below.

## Dashboard

A browser dashboard lives at `/dashboard/` for humans. Authentication is
session-based (HttpOnly cookie) — distinct from API-key auth, which the
API surface keeps using.

### Sign-in

Users sign in via OAuth federation: **Google** or **GitHub**. There is
no self-IdP (no email + password). On first sign-in:

- A `User` row is created with the provider's email + name.
- A starter tenant is auto-created (`"{name}'s Workspace"`).
- The user becomes that tenant's `owner`.

If a user signs in with Google then later with GitHub using the same
email address, the two `OAuthAccount`s link to the same `User`.

### What you can do from the dashboard

| Path | What |
|---|---|
| `/dashboard/` | List your tenants (with role pills) |
| `/dashboard/t/{tid}/` | Tenant overview |
| `/dashboard/t/{tid}/members` | List + add/remove members + role change |
| `/dashboard/t/{tid}/invites` | Generate token-link invites (admin/owner only) |
| `/dashboard/t/{tid}/keys` | Issue / revoke API keys — plaintext shown once at issuance |
| `/dashboard/t/{tid}/activity` | Recent requests + 24h summary, filterable |
| `/dashboard/t/{tid}/quotas` | Live gauges for all six quota dimensions + update form |
| `/dashboard/invites/accept/{token}` | Accept an invite — public landing |

### Roles

| Role | Can manage members / keys / quotas? | Can use API? |
|---|---|---|
| owner | yes (and only owners can demote / remove other owners) | yes |
| admin | yes | yes |
| member | no — read-only on the dashboard | yes |

You cannot demote or remove the **sole** owner of a tenant — promote
someone else first.

### Inviting people

Admins or owners go to `/dashboard/t/{tid}/invites`, pick a role and an
expiry (1h–30d), and get a share URL of the shape:

```
https://glossa.example.com/dashboard/invites/accept/<token>
```

Share it via your channel of choice (no email is sent — there's no SMTP
plumbing). The link is single-use; an `accepted_at` timestamp marks it
consumed. Pending invites can be revoked from the same page.

### Environment

Add to your `.env`:

```
GLOSSA_BASE_URL=https://glossa.example.com         # required for OAuth redirect URIs
GLOSSA_SESSION_COOKIE_NAME=glossa_session          # default
GLOSSA_SESSION_TTL_HOURS=168                       # 7 days, default
GLOSSA_SESSION_COOKIE_SECURE=true                  # required in production behind HTTPS
GLOSSA_GOOGLE_OAUTH_CLIENT_ID=...
GLOSSA_GOOGLE_OAUTH_CLIENT_SECRET=...
GLOSSA_GITHUB_OAUTH_CLIENT_ID=...
GLOSSA_GITHUB_OAUTH_CLIENT_SECRET=...
GLOSSA_OAUTH_STATE_TTL_MINUTES=10                  # default
```

Provider callback URLs to register with each IdP:

- Google: `${GLOSSA_BASE_URL}/auth/google/callback`
- GitHub: `${GLOSSA_BASE_URL}/auth/github/callback`

### Self-hosting

The dashboard works without any OAuth credentials in `auth_required=false`
mode — but the only way in is the bootstrap admin path against the API:
no human sign-in is available, no humans can issue keys. That's fine for
local dev. For real deployments, set the four OAuth credentials and
`GLOSSA_AUTH_REQUIRED=true`, register a first user via OAuth, and let
them issue keys from the dashboard.

### Architecture

| | |
|---|---|
| **Sessions** | DB-backed (`sessions` collection, TTL-indexed). Cookie value IS the session id. HttpOnly, SameSite=Lax, Secure when configured. |
| **State** | `oauth_states` collection holds PKCE verifier + CSRF nonce, TTL-pruned at expires_at (10 min). State is single-use — deleted before token exchange so a failed exchange still consumes it. |
| **Templates** | Jinja2, server-rendered. Pico.css + HTMX from CDN. No JS toolchain. |
| **Authorization** | `require_session` for any signed-in user; `require_membership(tid)` for tenant pages (404 if not a member); `require_admin_membership(tid)` for mutations. |
| **Open-redirect defense** | The OAuth `redirect_to` query param is honored only for paths starting with `/` and containing no `://`. |

## Bucket layout

```
{minio_bucket}/{space_id}/
├── schema.md                         # LLM-facing config; co-evolves with the tenant
├── index.md                          # auto-maintained catalog
├── log.md                            # auto-maintained chronological log
├── lint_report.md                    # latest lint pass (after first POST /lint)
├── pages/
│   ├── entities/
│   │   ├── companies/<slug>.md
│   │   ├── people/<slug>.md
│   │   └── topics/<slug>.md
│   ├── syntheses/<slug>.md
│   └── summaries/src-<source_id>.md
└── assets/src-<source_id>/<file>
```

`aws s3 sync` it anywhere — the wiki travels.

## Run locally

```sh
docker compose up --build
# API:     http://localhost:8200/docs
# MinIO:   http://localhost:9001    (glossa / glossa-secret)
# Mongo:   localhost:27017
```

## Run the test suite

```sh
pip install -r requirements-dev.txt
pytest
ruff check .
ruff format --check .
```

## How ingest works

Each `POST /spaces/{id}/sources/{sid}/ingest` queues a Job and starts a
background task. The task is serialised per space (one ingest at a time) and
runs roughly:

1. **Fetch content** — inline for push sources, `fetch_callback` for pull sources.
2. **Extract** — one LLM call returns `{entities, source_summary_markdown, log_blurb}`.
3. **Per-entity update** — for each extracted entity, read the existing page (if any), call the LLM to merge the new claims, write the page back. Wikilinks `[[summaries/src-<id>]]` are emitted as citations.
4. **Write summary** — `summaries/src-<id>.md`, deterministic.
5. **Regenerate index** — `index.md`, deterministic from DB page list.
6. **Append log** — one `## [iso] ingest | <title>` block to `log.md`.
7. **Fire webhooks** — `job.complete` or `job.failed`.

Job status (`queued` → `running` → `succeeded`/`failed`) and source status are
both kept current; poll `GET /jobs/{id}` or subscribe a webhook.

### Webhook signatures

Outbound webhook requests carry a Stripe-style signature:

    X-Glossa-Signature: t=<unix_seconds>,v1=<hex_hmac_sha256>

Where `v1` is `hmac_sha256(secret, f"{t}.".encode() + body_bytes)`.

The `t=` timestamp lets receivers reject replays — Glossa rejects
timestamps outside a 5-minute window when verifying inbound calls.
Use `glossa.webhooks.signing.verify_signature(...)` from the SDK to
verify on the receiving side without re-implementing the format.

## How lint works

`POST /spaces/{id}/lint` queues a Job and runs three checks:

1. **Orphans** (deterministic) — pages with zero inbound `[[wikilinks]]`.
2. **Broken links** (deterministic) — `[[path]]` targets that don't resolve to a known page.
3. **Contradictions / supersessions** (LLM) — one call per page citing ≥2 sources; flags claims that newer sources override or that disagree across sources. Pages with 0–1 cited sources are skipped (nothing to contradict).

Output:
- `lint_report.md` at the bucket root — sectioned by category, with wikilinks to every affected page
- `## [iso] lint | <summary>` entry in `log.md`
- `JobResult.lint_findings` (structured) + `JobResult.lint_summary` (counts per category) on `GET /jobs/{id}`
- `JOB_COMPLETE` / `JOB_FAILED` webhooks with `kind: "lint"` in the payload

The same per-space lock serialises lint and ingest, so a lint never races an ingest.

## How query works

`POST /spaces/{id}/query` makes two LLM calls:

1. **Route** — given the question + `index.md`, the LLM picks ≤8 pages to load.
2. **Answer** — given the picked pages, the LLM writes a markdown answer with `[[path]]` citations.

Cited pages are mapped back to their `source_refs`, which are mapped back to
`Source` records, so the response includes the original `external_uri` for
every cited source — letting integrators link back to the host (e.g. the
original vortrag in Chatforen).

## MCP server

`glossa-mcp` is a Model Context Protocol server that exposes the Glossa API
as MCP tools. Drop it into Claude Desktop, Claude Code, Cursor, Zed, or any
other MCP-aware client and that client gets a "consult-my-wiki" tool for
free.

Tools registered:

| | |
|---|---|
| `glossa_query` | Ask the wiki a question; returns answer + citations |
| `glossa_list_spaces` | Discover available Spaces |
| `glossa_list_pages` | Browse pages by kind / path prefix |
| `glossa_get_page` | Read one page's full markdown |
| `glossa_add_source` | Push a source (and optionally auto-ingest) |
| `glossa_get_job` | Poll an async Job (ingest / lint) |
| `glossa_lint` | Trigger a lint pass |

Resources:

- `glossa://{space_id}/index` — the catalogue
- `glossa://{space_id}/log` — recent ingest/lint history

Config (env vars):

- `GLOSSA_BASE_URL` (default `http://localhost:8200`) — the running Glossa API
- `GLOSSA_DEFAULT_SPACE_ID` (optional) — used when a tool call omits `space_id`
- `GLOSSA_API_TOKEN` (optional, forward-compatible) — bearer token forwarded to Glossa

Run it standalone:

```sh
pip install -e .
GLOSSA_BASE_URL=http://localhost:8200 GLOSSA_DEFAULT_SPACE_ID=gls_abc glossa-mcp
```

Wire it into Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "glossa": {
      "command": "glossa-mcp",
      "env": {
        "GLOSSA_BASE_URL": "http://localhost:8200",
        "GLOSSA_DEFAULT_SPACE_ID": "gls_abc"
      }
    }
  }
}
```

Cursor / Claude Code / Zed use the same `mcpServers` shape — see the client's
docs for the file location.

## Obsidian mirror

`glossa-obsidian-sync` mirrors one Glossa Space into an Obsidian vault. This is
one-way by design: Glossa remains the system that maintains the wiki; Obsidian
is a local markdown browser/editor surface for reading, graph view, backlinks,
Dataview, and manual notes around the generated pages.

The mirror writes:

- `schema.md`, `index.md`, `log.md`
- `lint_report.md` when a lint report exists
- every wiki page at its logical path, e.g. `entities/company/allianz.md`

Run it:

```sh
pip install -e .
GLOSSA_BASE_URL=http://localhost:8200 \
GLOSSA_DEFAULT_SPACE_ID=gls_abc \
GLOSSA_OBSIDIAN_VAULT="$HOME/Documents/My Vault" \
glossa-obsidian-sync
```

By default files are written under `Glossa/` inside the vault and wikilinks are
rewritten from `[[entities/...]]` to `[[Glossa/entities/...]]` so they resolve
inside Obsidian. Use `--subdir ""` to sync directly into the vault root and keep
the original Glossa wikilinks unchanged.

Useful variants:

```sh
# Mirror to a specific folder in the vault
glossa-obsidian-sync --space-id gls_abc --vault "$HOME/Documents/My Vault" --subdir Research/Glossa

# Dedicated vault, no link rewriting
glossa-obsidian-sync --space-id gls_abc --vault "$HOME/Documents/Glossa Vault" --subdir ""
```

Recommended Obsidian setup:

- Use a dedicated vault or a dedicated `Glossa/` folder.
- Treat mirrored files as generated; add personal notes in sibling folders.
- Run the sync after ingest/lint jobs complete, or on a timer via cron/launchd.

## LLM configuration

Per-space `llm_config` decides who runs the inference:

```jsonc
{
  "mode": "byo",                            // or "hosted"
  "endpoint": "https://api.openai.com/v1",  // any OpenAI-compatible URL
  "model": "gpt-4o-mini",
  "api_key_ref": "env:OPENAI_API_KEY"       // or a literal key
}
```

Missing fields fall back to `GLOSSA_DEFAULT_LLM_*` env vars, so a tenant can
just create a space without overriding anything and it works.

## Status

v0.1 functional MVP. **Implemented:**

- Full ingest pipeline (extract → entity merge → summary → index → log → webhooks)
- Query endpoint with routing + synthesis + citation resolution
- Lint endpoint (orphans + broken links + LLM contradiction / supersession detection, `lint_report.md` artifact, log entry, webhooks)
- `glossa-mcp` Model Context Protocol server (7 tools + 2 resources, stdio transport, env-var config)
- `glossa-obsidian-sync` one-way Obsidian vault mirror
- BYO LLM driver (any OpenAI-compatible endpoint)
- Push and pull source ingestion
- In-process background task runner with per-space serialisation
- Hosted multi-tenant auth (`glsk_live_*` API keys, tenant scoping with 404-on-cross-tenant, activity tracking, Stripe-style webhook signatures)
- Per-tenant quotas across six dimensions (cost, tokens, sources/space, storage bytes, requests/minute, model whitelist)
- Browser dashboard with Google + GitHub OAuth, multi-user tenants, token-link invites, role-based access (owner/admin/member), in-app key issuance/revoke, activity views, and quota gauges
- 350+ tests covering frontmatter, JSON parsing, slugging, end-to-end ingest, end-to-end query, end-to-end lint, MCP client + server wiring, auth + tenant isolation, admin + key issuance, activity middleware, webhook signing, quota extensions, OAuth + sessions, dashboard views, and the full sign-up → key-issued → API-call e2e flow

**Not implemented yet:**

- Hosted `LLMDriver` (only BYO works; hosted stub raises NotImplementedError)
- Backlink derivation pass
- Content chunking for very long sources (single source capped at `GLOSSA_INGEST_MAX_SOURCE_CHARS`, default 200k)
- A real worker (Arq/RQ). In-process `asyncio.create_task` is the MVP; jobs in flight at restart will be stuck in `running`.
- Cross-process locking (per-space lock is `asyncio.Lock` in-process; multi-worker deployments need Redis-backed locking).

The contract — API surface, bucket layout, 5-object data model — is stable
from here. The list above is implementation behind the contract.

## Chatforen integration (the first customer)

Lives in the Chatforen backend, not here. The shape:

1. `WissensdatenbankSourceSyncer` — posts new vortraege to
   `POST /spaces/{id}/sources` (or registers `fetch_callback` per source for
   pull mode; Wissensdatenbank stays system of record).
2. Agent tool `query_glossa` — sits next to `query_wissensdatenbank`, wraps
   the Glossa query endpoint. Agent consults the wiki first; falls back to
   raw retrieval for verification.
3. Wiki page renderer in the Vue frontend — markdown + wikilink resolution.
4. "In Wiki ergänzen" button on the document detail dialog.

The AI-extracted fields Chatforen already produces per vortrag (`ai.autor`,
`ai.unternehmen`, `ai.thementags`) become entity-page anchors during ingest —
no new extraction pipeline needed.
