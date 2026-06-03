# Glossa internals & how to extend it

Everything here is **implementation behind the contract** — it can change. The
contract (API, 5-object model, bucket layout, webhook signature) is what's stable.
Always pair an internals change with the matching reference + test update
(`SKILL.md` § Keeping this skill accurate).

## App wiring — `glossa/main.py`

Module-level `app = FastAPI(...)`. `ActivityMiddleware` is added first, then every
router is included with `dependencies=[Depends(get_auth_context)]` (so auth runs
per request); the `auth` and `dashboard` routers are mounted without it. The
`lifespan` builds `app.state.settings = get_settings()`, `app.state.storage =
MinioStorageBackend(settings)`, calls `ensure_bucket()`, and registers OAuth
strategies. Handlers reach shared state via `request.app.state.{settings,storage}`.

## Pluggable interfaces

### StorageBackend — `glossa/storage/base.py`
Paths are **relative to the space root** (`schema.md`, `pages/entities/…`). All
async; implement these to add a backend (`MinioStorageBackend`,
`InMemoryStorageBackend` exist):

- `ensure_bucket() -> None` — idempotent create.
- `init_space(space_id, schema_markdown=None) -> None` — seed `schema.md`/`index.md`/`log.md`.
- `read_page(space_id, path) -> str` — `""` if absent (not an error).
- `write_page(space_id, path, content) -> None` — create/overwrite (UTF-8, `text/markdown`).
- `delete_page(space_id, path) -> None` — idempotent.
- `list_pages(space_id, prefix="pages/") -> list[str]` — recursive, relative paths.
- `write_asset(space_id, path, data: bytes, content_type) -> None` — store raw
  binary (uploaded documents) under `assets/…`, outside the `pages/` markdown
  namespace so they never surface in `list_pages`.
- `read_asset(space_id, path) -> bytes` — raises `FileNotFoundError` if absent.

To add (e.g. local FS or raw S3): subclass `StorageBackend`, implement all eight,
and swap the construction in `main.py`'s lifespan (or make it settings-driven).

### Model layer (Pydantic AI) — `glossa/llm/models.py`

All Glossa inference runs through **Pydantic AI**. The `glossa/llm/` package
re-exports five functions. There are exactly **five providers**, dispatched via the
`_BUILDERS` table in `glossa/llm/models.py`:

| provider | model class | default auth |
|---|---|---|
| `anthropic` | `AnthropicModel` | `settings.anthropic_api_key` |
| `openai` | `OpenAIChatModel` | `settings.openai_api_key` (+ `openai_base_url`) |
| `gemini` | `GoogleModel` (GLA) | `settings.gemini_api_key` |
| `bedrock` | `BedrockConverseModel` | `settings.aws_*` / `bedrock_api_key` (+ `aws_region`) |
| `vertex` | `GoogleModel` via `GoogleCloudProvider` | `vertex_project`/`_location`/`_service_account_file` (else ADC) |

The `google` and `bedrock` SDKs are imported **lazily** inside their builders so
Glossa (and the test suite) runs with only `pydantic-ai-slim[openai,anthropic]`
installed; the validation errors (missing key/region) fire *before* the lazy import.

- `build_model(space, settings) -> pydantic_ai.models.Model` — constructs the
  Pydantic AI model for a space (no network until first call). Resolution
  precedence:
  1. `llm_config.provider` set → that provider.
  2. Else → `settings.default_llm_provider`.

  Unknown provider → `ValueError`. Per-space overrides: `llm_config.base_url`
  (anthropic/openai), `llm_config.api_key_ref` (`"env:VAR"`/literal, all
  key-based providers), `llm_config.extra.region` (bedrock),
  `llm_config.extra.{project,location}` (vertex).
- `resolve_provider(space, settings) -> str` — the string provider name (used for
  usage/billing and Anthropic-specific settings).
- `resolve_model_name(space, settings) -> str` — bare model name for
  usage/billing attribution.
- `model_settings_for(space, settings, *, temperature) -> dict` — per-call
  `model_settings`. For Anthropic: omits sampling params, enables adaptive
  thinking + effort (`settings.anthropic_*`), and sets
  `anthropic_cache_instructions=True` for prompt-cache reuse. For every other
  provider (openai/gemini/bedrock/vertex): passes `{"temperature": temperature}`.
- `usage_to_dict(run_usage, *, provider) -> dict` — maps a Pydantic AI
  `RunUsage` to the dict `record_usage` expects. Anthropic reports `input_tokens`
  excluding cached tokens; every other provider folds cache reads into
  `input_tokens`, so `usage_to_dict` subtracts them to match `glossa.pricing`
  (`_CACHE_EXCLUDED_FROM_INPUT = {"anthropic"}`).

Agents are defined at module level with no model bound; `build_model` injects the
model at call time via `agent.run(..., model=...)`. To support a new provider: add
a builder fn + `_BUILDERS` entry in `glossa/llm/models.py`, add its key/auth
settings to `glossa.config.Settings`, update `_CACHE_EXCLUDED_FROM_INPUT` if it
reports input/cache separately, and install the matching `pydantic-ai-slim` extra
(lazy-import the SDK if it isn't a default install).

## Ingest pipeline — `glossa/ingest/`

`enqueue_ingest(*, space_id, source_id, app) -> Job` (in `workflow.py`) persists a
`Job(kind=ingest, status=queued)` and launches `asyncio.create_task(_run_ingest_safely(...))`
(tracked via `track_background_task`). `run_ingest(...)` acquires the per-space
lock and runs, updating the Job after each phase:

1. **Fetch** — `source_fetcher.fetch_content(source, max_chars, *, storage,
   settings)` dispatches on `ingestion_mode` → a plain string, truncated at
   `GLOSSA_INGEST_MAX_SOURCE_CHARS`: **push** = `content_inline`; **pull** =
   `fetch_callback` HTTP; **url** = `url_fetcher.fetch_url_as_markdown` (httpx GET
   + `trafilatura` readable-content→markdown, single page); **upload** =
   `storage.read_asset(asset_path)` → `doc_parser.parse_asset_to_text` (LiteParse,
   off-loop). `url_fetcher`/`doc_parser` import their third-party deps lazily and
   wrap failures as `SourceFetchError`.
2. **Extract (single-shot LLM)** — `extract_agent` (Pydantic AI, structured
   output `ExtractionOut`) returns `{entities[], source_summary_markdown,
   log_blurb}`; records `Operation.INGEST_EXTRACT` usage. Defined in
   `glossa/ingest/agents.py`; prompt is `SYSTEM_INGEST_EXTRACT`.
3. **Summary page** (deterministic, written first) —
   `page_writer.build_summary_page(...)` → `upsert_page(...)` at
   `summaries/src-<source_id>`. Written before the maintainer runs so its
   `[[summaries/src-<id>]]` wikilink is already resolvable.
4. **Maintainer agent** (agentic, `glossa/ingest/agents.py`) — `maintainer_agent`
   holds an in-memory `WorkingCopy` (via `MaintainerDeps`) and uses two tool sets:
   - *Read-only*: `read_index`, `list_pages`, `search_pages`, `read_outline`,
     `read_section`, `read_page`.
   - *Write (mutate working copy)*: `replace_in_section`, `replace_section`,
     `add_section`, `remove_section`, `create_page` (entity or synthesis),
     `set_frontmatter`.
   The agent deduplicates (search before create), makes minimal section/substring
   edits rather than full rewrites, and may create synthesis pages. An
   `output_validator` (`_validate_changes`) checks required frontmatter keys
   (`kind`, `title`) and resolvable `[[wikilinks]]` on every dirty page; it raises
   `ModelRetry` to trigger a self-correction loop. Section/substring operations
   are implemented in `glossa/utils/md_sections.py`.
   Guardrails (config): `ingest_max_agent_steps` caps total tool calls
   (`UsageLimits(request_limit=…)`) and `ingest_max_pages_per_run` /
   `ingest_max_edit_bytes` raise `ModelRetry` mid-run. Hitting the step cap
   (`UsageLimitExceeded`) ends the run cleanly — partial edits remain in the
   working copy; records `Operation.INGEST_UPDATE_PAGE` usage.
5. **Flush** (deterministic) — `flush_working_copy(wc, ...)` iterates every dirty
   path: validates, auto-stamps `updated_at`, merges `source_refs` with existing
   DB refs, and calls `page_writer.upsert_page(...)` (quota enforcement, DB upsert,
   storage write). Quota/write safety never lives in the model. If the step cap was
   hit the log entry is suffixed `[partial: ingest step cap reached]`.
6. **Index** (deterministic) — `index_writer.regenerate_index(...)` rebuilds
   `index.md` from the DB page list, grouped by category.
7. **Log** (deterministic) — `log_writer.append_log_entry(...)` appends
   `## [iso] ingest | <title>` + created/updated bullets.
8. **Finalize + webhooks** — Job→`succeeded`, Source→`done`, Space stats bumped;
   `webhook_delivery.fire(..., WebhookEvent.JOB_COMPLETE, payload)`. On exception
   `_run_ingest_safely` marks the Job `failed` and fires `JOB_FAILED`.

Key modules: `glossa/ingest/agents.py` (agents + tools + flush),
`glossa/ingest/working_copy.py` (`WorkingCopy`),
`glossa/ingest/workflow.py` (orchestration),
`glossa/utils/md_sections.py` (section/substring operations).

## Query flow — `glossa/query.py`

`answer_question(space_id, request, storage, settings, model=None) -> QueryResponse`
— two Pydantic AI agent calls:

1. **Route** — `query_route_agent` (output_type `RouteOut{pages_to_load,
   reasoning}`, `SYSTEM_QUERY_ROUTE`, temp 0.0) — given `index.md` + question,
   selects pages; capped at `request.max_pages`; usage `Operation.QUERY_ROUTE`.
2. **Answer** — `query_answer_agent` (output_type `str`, `SYSTEM_QUERY_ANSWER`,
   temp 0.2) — given the loaded page content → markdown answer with `[[path]]`
   citations; usage `Operation.QUERY_ANSWER`.

Then `_resolve_cited_sources` extracts wikilinks → page `source_refs` → `Source`
docs → `CitedSource{id, title, external_uri}`.

Both agents are module-level with no model bound; `build_model` injects the model
at call time.

## Lint flow — `glossa/lint/`

`enqueue_lint(*, space_id, app) -> Job` → `run_lint(...)` under the per-space lock:

1. **scanner** (deterministic) — `scanner.load_pages` then `scan_deterministic`:
   **broken_link** (`[[target]]` not in known paths, system pages excluded) and
   **orphan** (zero inbound wikilinks).
2. **contradictions** (Pydantic AI, only pages with `len(source_refs) >= 2`) —
   `contradictions_agent` (output_type `ContradictionsOut{findings[]}`,
   `SYSTEM_LINT_CONTRADICTIONS`, temp 0.1) via
   `check_page_for_contradictions(*, model, provider, ...)` → findings with
   `kind ∈ {contradiction, supersession}`; usage `Operation.LINT` per page.
3. **report** — `report_writer.write_report(...)` writes `lint_report.md`
   (sectioned, `kind: system` frontmatter); `append_lint_log_entry(...)` adds a
   `## [iso] lint | <summary>` log line. Job result carries `lint_findings` +
   `lint_summary` (counts per category); fires `JOB_COMPLETE` with `kind: lint`.

The `contradictions_agent` is module-level in `glossa/lint/contradictions.py`
with no model bound; model and provider are passed at call time.

## Concurrency — `glossa/concurrency.py`

- `lock_for_space(space_id) -> asyncio.Lock` — lazily cached per space; ingest and
  lint both acquire it, so they never race within a space.
- `track_background_task(task)` — keeps a strong ref in a module set, auto-discards
  on done.
- **Caveats (MVP):** in-process only. Jobs in flight at process restart stay stuck
  in `running`; multi-worker deployments need a real worker (Arq/RQ/Celery) and a
  distributed lock (Redis). The rate limiter is likewise in-process.

## Recipes — extending Glossa

**Add an API endpoint.** Add the handler to the relevant router in `glossa/routes/`
(or a new router included in `main.py`). Reach state via
`request.app.state.{settings,storage}`. Define request/response Pydantic models
(reuse/extend those in `glossa/models/`). Enforce scope with the auth dependency
and keep tenant filtering (404, not 403, on cross-tenant). Add tests
(`reference/testing.md`) and update `reference/api.md`.

**Add a model field or enum value.** Edit the model in `glossa/models/`; if it
affects stored docs, update writers/readers and any backfill in `scripts/`. Update
`reference/data-model.md`. Keep enum *values* (the wire strings) stable.

**Add a webhook event.** Extend `WebhookEvent` in `glossa/models/webhook.py` and
fire it via `webhook_delivery.fire(...)` at the right point. Document it in
`reference/data-model.md` + `reference/api.md`.

**Add a storage backend.** Subclass `StorageBackend`, implement all eight methods,
and swap the construction in `main.py`'s lifespan. Update `reference/internals.md`
+ `reference/config.md`.

**Add a Pydantic AI LLM provider.** Add a `_build_<name>` fn and a `_BUILDERS`
entry in `glossa/llm/models.py` (lazy-import the SDK if it's not a default
install); add the provider to `SUPPORTED_PROVIDERS` and, if it's key-based, to
`_PROVIDER_KEY_SETTING`. Add its key/auth fields to `glossa.config.Settings`. If
the provider reports input/cache separately (Anthropic does; the others fold cache
reads into `input_tokens`) add it to `_CACHE_EXCLUDED_FROM_INPUT`. Install the
matching `pydantic-ai-slim` extra in `requirements.txt`, add list prices to
`glossa.pricing`, and update `reference/internals.md` + `reference/config.md`.

**Add a Job kind.** Add to `JobKind`, write an `enqueue_*`/`run_*` pair following
the ingest/lint shape (per-space lock + `track_background_task` + Job status
transitions + webhook), and extend `JobResult` if it returns new data.

**Move off the in-process runner.** The `Job` model + `asyncio.Task` split is
designed so `enqueue_*` can hand off to Arq/RQ/Celery and `lock_for_space` can
become a Redis lock without changing the API contract — that's the intended
upgrade path.
