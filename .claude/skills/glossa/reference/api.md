# Glossa HTTP API reference

Base URL in dev: `http://localhost:8200`. The **live OpenAPI at `/docs` is
authoritative** — if this file disagrees with it, the code wins (fix this file).
All app routers are mounted in `glossa/main.py`; per-endpoint logic lives under
`glossa/routes/`.

## Authentication, scopes, isolation

- **Header:** `Authorization: Bearer glsk_live_<random>`. One key ↔ one tenant.
- **`GLOSSA_AUTH_REQUIRED`** (default `false`) controls the *no-header* path:

  | Mode | No header | Bad/revoked header |
  |---|---|---|
  | `false` (self-host/dev) | synthetic admin context; all routes pass | 401 |
  | `true` (hosted) | 401 | 401 |

  A request that **does** carry an `Authorization` header is always validated —
  sending a token is a positive identity claim.
- **Bootstrap:** set `GLOSSA_BOOTSTRAP_ADMIN_API_KEY`; a request bearing exactly
  that token gets a synthetic admin context (no DB row). Use once to issue real
  keys, then unset.
- **Scopes** (`Scope` enum): `spaces:read`, `spaces:write`, `sources:write`,
  `query`, `lint`, `admin`. **Default scopes** on a new key omit `admin`:
  `[spaces:read, spaces:write, sources:write, query, lint]`. Explicitly enforced
  today: `query` → `POST …/query`, `lint` → `POST …/lint`, `admin` → all
  `/admin/*` and cross-tenant ops. `sources:write` is the intended scope for
  ingest. A missing scope returns **403** (`missing scope: <scope>`).
- **Tenant isolation:** every tenant-scoped route filters by the caller's
  `tenant_id`. Cross-tenant access → **404** (never 403 — existence is never
  revealed). Admins may pass a `tenant_id` filter / hit `/admin/*`.
- **Quota exceeded:** **402** with body `{"reason": <str>, "quota": <QuotaStatus>}`.
- **Activity:** `ActivityMiddleware` records every request except `/healthz`
  (method, path, status, duration_ms, tenant_id, api_key_id) → queryable under
  `/tenants/{tid}/activity/*`.

## Spaces — `glossa/routes/spaces.py`

| Method | Path | Body / params | Returns |
|---|---|---|---|
| POST | `/spaces` | `SpaceCreate` | `Space` |
| GET | `/spaces` | `?tenant_id=` (admin), `?limit=50` | `list[Space]` |
| GET | `/spaces/{space_id}` | — | `Space` |
| PATCH | `/spaces/{space_id}` | `SpaceUpdate` | `Space` |
| GET | `/spaces/{space_id}/schema` | — | `{path, content}` |
| PUT | `/spaces/{space_id}/schema` | `?schema_markdown=` | `{ok, path}` (402 on storage quota) |

**`SpaceCreate`**: `name` (req), `slug?` (auto-slugified from name if omitted),
`llm_config?` (`LLMConfig`; unset → the `GLOSSA_DEFAULT_LLM_*` settings apply),
`schema_markdown?`,
`tenant_id?` (admin-only override; non-admins always create in their own tenant).
**`SpaceUpdate`**: `name?`, `llm_config?`.
**`Space`** response: see `reference/data-model.md`.

## Sources — `glossa/routes/sources.py`

| Method | Path | Body / params | Returns |
|---|---|---|---|
| POST | `/spaces/{space_id}/sources` | `SourceCreate` | `Source` (402 if `max_sources_per_space` exceeded) |
| POST | `/spaces/{space_id}/sources/upload` | multipart form (see below) | `Source` (`upload` mode; 402 quota, 413 too large) |
| GET | `/spaces/{space_id}/sources` | `?limit=50&offset=0` | `list[Source]` |
| GET | `/spaces/{space_id}/sources/{source_id}` | — | `Source` |
| POST | `/spaces/{space_id}/sources/{source_id}/ingest` | (no body) | `Job` (queued; 402 on cost/token quota) |

**`SourceCreate`**: `title` (req), `ingestion_mode` (`"push"` \| `"pull"` \|
`"url"`, req), `content_inline?` (**required when push**), `fetch_callback?`
(**required when pull**: `{url, method="GET", headers={}, auth_ref?}`),
`external_uri?` (**required when url** — the link to fetch; also the citation
link-back), `metadata?` (dict). Validation enforces the push/pull/url pairing;
`"upload"` is **rejected here** (use the upload route).

**Upload** (`POST …/sources/upload`, `multipart/form-data`): fields `file`
(required, the document), `title?` (defaults to the filename), `external_uri?`,
`metadata?` (JSON-object string). Stores the raw bytes as a storage asset
(`assets/src-<id>/<file>`) and returns an `upload`-mode `Source`; **call
`…/ingest` next** — LiteParse parses the file to text during that job. **413** if
the file exceeds `GLOSSA_INGEST_MAX_UPLOAD_BYTES`; **400** if empty; **422** if
`metadata` isn't a JSON object. Supported types track LiteParse (PDF native; Office
needs LibreOffice, images need ImageMagick, OCR needs Tesseract).

## Pages — `glossa/routes/pages.py`

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/spaces/{space_id}/pages` | `?kind=&path_prefix=&limit=100` | `list[Page]` |
| GET | `/spaces/{space_id}/pages/{path:path}` | greedy `path` | `PageWithContent` (Page + `content`) |
| GET | `/spaces/{space_id}/index` | — | `{path:"index.md", content}` |
| GET | `/spaces/{space_id}/log` | `?tail=N` (last N `## [` entries) | `{path:"log.md", content}` |
| GET | `/spaces/{space_id}/lint-report` | — | `{path:"lint_report.md", content}` (404 if none yet) |

`kind` filters by `PageKind`; `path` is the **logical** path (no `pages/`, no
`.md`).

## Jobs — `glossa/routes/jobs.py`

| Method | Path | Returns |
|---|---|---|
| GET | `/jobs/{job_id}` | `Job` (404 if the job's space isn't yours) |

Poll `status`: `queued → running → succeeded` / `failed`. On success, `result`
(`JobResult`) carries `pages_created`, `pages_updated`, `contradictions_flagged`,
`lint_findings`, `lint_summary`, `log_entry`.

## Query — `glossa/routes/query.py` (scope: `query`)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/spaces/{space_id}/query` | `QueryRequest` | `QueryResponse` (402 on quota) |

**`QueryRequest`**: `question` (req), `max_pages` (int, default 8, 1–20).
**`QueryResponse`**: `answer` (markdown), `pages_consulted` (list[str]),
`cited_pages` (list[str]), `cited_sources` (list of `{id, title, external_uri?}`),
`reasoning?` (str). Citations resolve `[[path]]` → page `source_refs` → `Source`,
so you get the original `external_uri` to link back to the host.

## Lint — `glossa/routes/lint.py` (scope: `lint`)

| Method | Path | Returns |
|---|---|---|
| POST | `/spaces/{space_id}/lint` | `Job` (queued; 402 on quota) |

Runs orphans + broken-links (deterministic) + contradictions/supersessions (LLM,
only on pages citing ≥2 sources). Writes `lint_report.md`, a `log.md` entry, and
`JobResult.lint_findings` / `lint_summary`.

## Webhooks — `glossa/routes/webhooks.py`

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/spaces/{space_id}/webhooks` | `WebhookCreate` | `Webhook` |
| GET | `/spaces/{space_id}/webhooks` | — | `list[Webhook]` |
| DELETE | `/spaces/{space_id}/webhooks/{webhook_id}` | — | `{ok:true}` (404 if absent) |

**`WebhookCreate`**: `url` (req), `events` (list of `WebhookEvent`, req),
`secret?` (auto-generated 32-byte URL-safe if omitted). Events:
`job.complete`, `job.failed`, `page.updated`, `page.created`, `source.received`.
Outbound deliveries carry `X-Glossa-Signature: t=<unix>,v1=<hex_hmac_sha256>` —
verify with `glossa.webhooks.signing.verify_signature` (see
`reference/integrations.md`).

## Admin: tenants — `glossa/routes/admin.py` (scope: `admin`)

| Method | Path | Body / params | Returns |
|---|---|---|---|
| POST | `/admin/tenants` | `TenantCreate` | `Tenant` (409 if `owner_email` taken) |
| GET | `/admin/tenants` | `?status=&limit=100` | `list[Tenant]` |
| GET | `/admin/tenants/{tenant_id}` | — | `Tenant` |
| PATCH | `/admin/tenants/{tenant_id}` | `TenantUpdate` | `Tenant` (suspend/reactivate/re-plan) |

**`TenantCreate`**: `name`, `owner_email` (unique), `plan` (`free`\|`pro`\|`enterprise`, default `free`).
**`TenantUpdate`**: `name?`, `owner_email?`, `plan?`, `status?` (`active`\|`suspended`\|`deleted`).

## API keys — `glossa/routes/api_keys.py` (tenant-match or admin)

| Method | Path | Body / params | Returns |
|---|---|---|---|
| POST | `/tenants/{tenant_id}/api-keys` | `ApiKeyCreate` | `ApiKeyIssued` |
| GET | `/tenants/{tenant_id}/api-keys` | `?include_revoked=false` | `list[ApiKey]` (no plaintext) |
| DELETE | `/tenants/{tenant_id}/api-keys/{key_id}` | — | `ApiKey` (revoked; idempotent) |

**`ApiKeyCreate`**: `label?`, `scopes?` (defaults to the default scope set).
**`ApiKeyIssued`**: `{api_key: ApiKey, plaintext: "glsk_live_…"}` — `plaintext`
is returned **once** and never stored.

## Activity — `glossa/routes/activity.py` (tenant-match or admin)

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/tenants/{tenant_id}/activity/requests` | `?method=&path_prefix=&status_min=&limit=100` | `list[RequestEvent]` |
| GET | `/tenants/{tenant_id}/activity/summary` | `?hours=24` | `RequestActivitySummary` |

`RequestEvent`: `id, tenant_id?, api_key_id?, method, path, status_code,
duration_ms, created_at, error?`. Audit log retained ~90 days.
`RequestActivitySummary`: `period_start/end, request_count, error_count,
avg_duration_ms, by_status, by_path`.

## Usage & quota — `glossa/routes/usage.py` (tenant-match or admin)

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/tenants/{tenant_id}/usage` | `?period=YYYY-MM` | `UsagePeriodSummary` |
| GET | `/tenants/{tenant_id}/usage/summary` | — | dict (all-time) |
| GET | `/tenants/{tenant_id}/usage/by-space` | `?period=` | `list[dict]` |
| GET | `/tenants/{tenant_id}/usage/events` | `?space_id=&limit=50` | `list[UsageEvent]` |
| GET | `/tenants/{tenant_id}/quota` | — | `QuotaStatus` (live gauges + `blocked`) |
| GET | `/tenants/{tenant_id}/quota/config` | — | `TenantQuota \| null` |
| PUT | `/tenants/{tenant_id}/quota` | `TenantQuotaUpdate` | `TenantQuota` |
| GET | `/spaces/{space_id}/usage/events` | `?limit=50` | `list[UsageEvent]` |

Quota dimensions (all optional, null = unlimited): `monthly_cost_limit_usd`,
`monthly_token_limit`, `max_sources_per_space`, `max_storage_bytes`,
`max_requests_per_minute`, `allowed_models`. `UsageEvent.operation` ∈
`{ingest_extract, ingest_update_page, query_route, query_answer, lint}`. Field
lists in `reference/data-model.md`.

## OAuth / dashboard — `glossa/routes/auth.py`, `glossa/dashboard/routes.py`

Browser surface; **session-cookie** auth (HttpOnly), distinct from API-key auth.
`GET /auth/{provider}/start` (`google`\|`github`, `?redirect_to=`) → 303 to IdP;
`GET /auth/{provider}/callback?code=&state=` → sets session cookie, 303 to
`/dashboard/`; `POST /auth/logout`. Dashboard pages under `/dashboard/…` (tenants,
members, invites, keys, activity, quotas). Details in `reference/config.md` and
`README.md` § Dashboard.

## Meta

`GET /healthz` → `{status:"ok", version}` (no auth, not recorded by activity).
