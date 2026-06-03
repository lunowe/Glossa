# Glossa data model reference

Pydantic models live in `glossa/models/`. They are the single source of truth for
field names — import them directly (`from glossa.models import Space, Source, …`).
IDs are `{prefix}_{uuid4().hex[:12]}` (or `secrets.token_urlsafe` for tokens).

## The five core objects

### Space — `glossa/models/space.py`
`id` (`gls_…`), `tenant_id`, `name`, `slug`, `bucket_uri` (`s3://<bucket>/<space_id>/`),
`schema_path` (default `"schema.md"`), `llm_config: LLMConfig`, `stats: SpaceStats`,
`created_at`, `updated_at`.
- **`LLMConfig`** — per-space LLM selection (all fields optional; unset → the
  `GLOSSA_DEFAULT_LLM_*` settings apply):
  - `provider?: str` — one of `"anthropic"`, `"openai"`, `"gemini"`, `"bedrock"`,
    `"vertex"`. When set, takes precedence over `settings.default_llm_provider`.
  - `base_url?: str` — custom base URL (anthropic/openai; e.g. an
    OpenAI-compatible endpoint).
  - `model?: str` — model name (else `settings.default_llm_model`).
  - `api_key_ref?: str` — `"env:VAR"` or a literal key; overrides the provider's
    default `GLOSSA_*_API_KEY` setting when set.
  - `extra: dict` — provider-specific overrides: `region` (bedrock),
    `project`/`location` (vertex).
  - Resolution precedence (see `glossa/llm/models.py` `build_model`): `provider`
    set → that provider; else → `settings.default_llm_provider`. Unknown provider
    → `ValueError`. The `_BUILDERS` table maps each provider to its model class.
- **`SpaceStats`**: `source_count` (0), `page_count` (0), `last_ingest_at?`.

### Source — `glossa/models/source.py`
`id` (`src_…`), `space_id`, `title`, `ingestion_mode: SourceIngestionMode`
(`push`\|`pull`\|`url`\|`upload`), `content_inline?` (push), `fetch_callback?:
FetchCallback` (pull), `external_uri?` (the link to fetch for `url` mode; also the
citation link-back), `asset_path?` (storage-relative path of the uploaded raw
file for `upload` mode, e.g. `assets/src-<id>/report.pdf`), `metadata: dict`,
`status: SourceStatus` (`received`→`ingesting`→`done`\|`failed`, default
`received`), `created_at`, `last_ingested_at?`, `last_ingest_job_id?`.
- **`FetchCallback`**: `url`, `method` (`"GET"`), `headers: dict`, `auth_ref?`.
- **The four ingestion modes** all resolve to a plain-text string fed to the same
  extract pipeline (`glossa/ingest/source_fetcher.fetch_content`): `push` =
  `content_inline`; `pull` = host `fetch_callback`; `url` = fetch the link and
  convert its readable content to markdown (single page, no crawl); `upload` =
  parse the stored raw file to text with LiteParse. `upload` sources are created
  via the dedicated upload route (not `SourceCreate`), which stores
  `filename`/`content_type`/`byte_size` in `metadata`.

### Page — `glossa/models/page.py`
`space_id`, `path`, `kind: PageKind`, `title`, `frontmatter: dict`,
`source_refs: list[str]`, `backlinks: list[str]`, `size_bytes` (0), `updated_at`,
`last_touched_by_job_id?`. **`PageWithContent`** adds `content: str`.
- **`PageKind`**: `entity`, `topic`, `summary`, `synthesis`, `comparison`,
  `system` (schema/index/log), `custom`.
- **Path is logical**: no `pages/` prefix, no `.md` (e.g.
  `entities/companies/allianz`). Storage key = `pages/{path}.md`. Wikilinks use
  the logical path. Summaries: `summaries/src-<source_id>`.

### Job — `glossa/models/job.py`
`id` (`job_…`), `space_id`, `kind: JobKind`, `inputs: dict`, `status: JobStatus`,
`result?: JobResult`, `webhook_url?`, `started_at?`, `ended_at?`,
`error_message?`, `created_at`.
- **`JobKind`**: `ingest`, `lint`, `reindex`, `rebuild_index`.
- **`JobStatus`**: `queued`, `running`, `succeeded`, `failed`.
- **`JobResult`**: `pages_created: list[str]`, `pages_updated: list[str]`,
  `contradictions_flagged: list[dict]`, `lint_findings: list[dict]`,
  `lint_summary: dict[str,int]`, `log_entry?`.

### Webhook — `glossa/models/webhook.py`
`id` (`wh_…`), `space_id`, `url`, `events: list[WebhookEvent]`, `secret`,
`active` (True), `created_at`.
- **`WebhookEvent`**: `job.complete`, `job.failed`, `page.updated`,
  `page.created`, `source.received`.

## Tenancy, auth, dashboard models

### Tenant — `glossa/models/tenant.py`
`id` (`tnt_…`), `name`, `owner_email` (unique), `plan: TenantPlan`
(`free`\|`pro`\|`enterprise`, default `free`), `status: TenantStatus`
(`active`\|`suspended`\|`deleted`, default `active`), `created_at`, `updated_at`.

### ApiKey — `glossa/models/api_key.py`
`id` (`key_…`), `tenant_id`, `hashed_key` (SHA-256), `prefix` (`glsk_live_<8>`),
`label?`, `scopes: list[Scope]` (default set), `created_at`, `last_used_at?`,
`revoked_at?`.
- **`Scope`**: `spaces:read`, `spaces:write`, `sources:write`, `query`, `lint`,
  `admin`. Default = all but `admin`.
- Plaintext format `glsk_live_<random>`; helpers `generate_key() ->
  (plaintext, prefix, hashed_key)` and `hash_key(plaintext) -> str`.

### User — `glossa/models/user.py`
`id` (`usr_…`), `email`, `name`, `avatar_url?`, `oauth_accounts:
list[OAuthAccount]`, `created_at`, `last_login_at?`.
- **`OAuthAccount`**: `provider: OAuthProvider` (`google`\|`github`),
  `provider_user_id`, `email`, `linked_at`. Same email across providers links to
  one `User`.

### TenantMember — `glossa/models/membership.py`
`id` (`mem_…`), `tenant_id`, `user_id`, `role: TenantRole`
(`owner`\|`admin`\|`member`), `joined_at`.
- **Roles:** owner & admin manage members/keys/quotas; member is dashboard
  read-only but can use the API. Can't demote/remove the sole owner.
- **Invite** (same file): `id` (`inv_…`), `tenant_id`, `token` (URL-safe),
  `role`, `created_by_user_id`, `created_at`, `expires_at`, `accepted_at?`,
  `revoked_at?`. Single-use share link; no email is sent.

### Session / OAuthState — `glossa/models/session.py`, `oauth_state.py`
- **Session**: `id` (`ses_…`, **the cookie value**), `user_id`, `created_at`,
  `expires_at` (TTL-indexed), `last_seen_at`, `ip?`, `user_agent?`.
- **OAuthState**: `id` (sent to IdP as `state`), `provider`, `code_verifier`
  (PKCE), `redirect_to?`, `created_at`, `expires_at` (~10 min). Single-use:
  deleted before token exchange.

## Usage & quota models — `glossa/usage/models.py`

- **`UsageEvent`**: `id, tenant_id, space_id, job_id?, operation: Operation,
  model, input_tokens, output_tokens, cache_creation_input_tokens,
  cache_read_input_tokens, cost_usd, created_at`. `Operation` ∈ `{ingest_extract,
  ingest_update_page, query_route, query_answer, lint}`.
- **`UsagePeriodSummary`**: `tenant_id, period (YYYY-MM), input_tokens,
  output_tokens, cache_*_tokens, total_tokens, cost_usd, event_count,
  by_operation, by_model`.
- **`TenantQuota`**: `tenant_id, monthly_cost_limit_usd?, monthly_token_limit?,
  allowed_models?, max_sources_per_space?, max_storage_bytes?,
  max_requests_per_minute?, notes?, updated_at`. `TenantQuotaUpdate` = all
  optional (null preserves existing).
- **`QuotaStatus`**: used/limit/remaining for cost & tokens, plus
  sources-per-space, storage bytes, requests-per-minute gauges, and `blocked: bool`.

## Frontmatter — `glossa/utils/frontmatter.py`

YAML between `---` fences, then the markdown body. `safe_load`/`safe_dump`
(`sort_keys=False, allow_unicode=True`).
- `parse(markdown) -> (dict, body)` — `({}, markdown)` if no/invalid frontmatter.
- `serialize(frontmatter, body) -> str` — `---\n{yaml}\n---\n\n{body}` (body as-is
  if frontmatter empty).

Conventional keys (not enforced): `kind`, `title`, `sources` (source IDs),
`tags`. Generated artifacts (`schema.md`/`index.md`/`log.md`/`lint_report.md`)
carry `kind: system`.

## Slugs — `glossa/utils/slug.py`

`slugify(value, max_length=80)`: German folding (`ä→ae ö→oe ü→ue ß→ss`) → NFKD
ASCII fold → lowercase → non-`[a-z0-9]` runs to single `-` → strip/ truncate →
`"untitled"` if empty. E.g. `"Allianz Österreich"` → `"allianz-oesterreich"`.

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

`aws s3 sync` it anywhere — the wiki travels.
