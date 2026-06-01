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

### LLMDriver — `glossa/llm/base.py`
- `LLMMessage{role: "system"|"user"|"assistant", content: str}`,
  `LLMResponse{content: str, usage: dict}`.
- `async chat(messages, *, model=None, temperature=0.2, max_tokens=None) -> LLMResponse`.

Implementations: `BYOLLMDriver(endpoint, api_key, default_model="gpt-4o-mini")`
(POSTs `{endpoint}/chat/completions`, OpenAI-compatible) and `HostedLLMDriver`
(Anthropic streaming + prompt caching + thinking — **currently raises
NotImplementedError**, do not rely on it). To add a provider, subclass
`LLMDriver`, implement `chat`, and route to it in the factory.

### LLM driver factory — `glossa/llm/factory.py`
`build_driver(space, settings) -> LLMDriver` reads `space.llm_config`, resolves
`api_key_ref` (`"env:VAR"` → env lookup; else literal; else settings fallback),
then branches on `mode`:
- **byo** → `BYOLLMDriver` with endpoint = `cfg.endpoint` else
  `settings.default_llm_endpoint` (error if neither), key = `cfg.api_key_ref` else
  `settings.default_llm_api_key`, model = `cfg.model` else `settings.default_llm_model`.
- **hosted** → `HostedLLMDriver` from `cfg`/`settings.hosted_*` (stubbed).
Pipelines obtain their driver via `from glossa.llm import build_driver`.

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
2. **Extract (LLM #1)** — `extract.extract_from_source(...)` → JSON
   `{entities[], source_summary_markdown, log_blurb}`; records
   `Operation.INGEST_EXTRACT` usage.
3. **Per-entity merge (LLM #2…N)** — for each entity: `page_writer.read_existing_page`
   → `page_writer.llm_update_entity_page(...)` (JSON `{new_content, is_changed,
   change_summary}`) → `page_writer.upsert_page(...)` (writes `pages/{path}.md`,
   merges `source_refs`, upserts the DB Page, emits `[[summaries/src-<id>]]`
   citations); records `Operation.INGEST_UPDATE_PAGE`.
4. **Summary** (deterministic) — `summaries/src-<source_id>.md` via `upsert_page`.
5. **Index** (deterministic) — `index_writer.regenerate_index(...)` rebuilds
   `index.md` from the DB page list, grouped by category.
6. **Log** (deterministic) — `log_writer.append_log_entry(...)` appends
   `## [iso] ingest | <title>` + created/updated bullets.
7. **Finalize + webhooks** — Job→`succeeded`, Source→`done`, Space stats bumped;
   `webhook_delivery.fire(..., WebhookEvent.JOB_COMPLETE, payload)`. On exception
   `_run_ingest_safely` marks the Job `failed` and fires `JOB_FAILED`.

Prompts live in `glossa/ingest/prompts.py` (`SYSTEM_INGEST_EXTRACT`,
`SYSTEM_INGEST_UPDATE_PAGE`, …). Extraction/merge expect strict JSON — parsed via
`glossa/utils/json_parse.py`.

## Query flow — `glossa/query.py`

`answer_question(space_id, request, storage, settings, llm) -> QueryResponse`, two
LLM calls:
1. **Route** (`SYSTEM_QUERY_ROUTE`, temp 0.0) — given `index.md` + question →
   `{pages_to_load, reasoning}`, capped at `request.max_pages`; usage
   `Operation.QUERY_ROUTE`.
2. **Answer** (`SYSTEM_QUERY_ANSWER`, temp 0.2) — given the loaded pages →
   markdown answer with `[[path]]` citations; usage `Operation.QUERY_ANSWER`.
Then `_resolve_cited_sources` extracts wikilinks → page `source_refs` → `Source`
docs → `CitedSource{id, title, external_uri}`.

## Lint flow — `glossa/lint/`

`enqueue_lint(*, space_id, app) -> Job` → `run_lint(...)` under the per-space lock:
1. **scanner** (deterministic) — `scanner.load_pages` then `scan_deterministic`:
   **broken_link** (`[[target]]` not in known paths, system pages excluded) and
   **orphan** (zero inbound wikilinks).
2. **contradictions** (LLM, only pages with `len(source_refs) >= 2`) —
   `contradictions.check_page_for_contradictions(...)` → findings with
   `kind ∈ {contradiction, supersession}`; usage `Operation.LINT` per page.
3. **report** — `report_writer.write_report(...)` writes `lint_report.md`
   (sectioned, `kind: system` frontmatter); `append_lint_log_entry(...)` adds a
   `## [iso] lint | <summary>` log line. Job result carries `lint_findings` +
   `lint_summary` (counts per category); fires `JOB_COMPLETE` with `kind: lint`.

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

**Add a storage backend / LLM provider.** Implement the interface above; wire the
storage choice in `main.py`'s lifespan and the LLM choice in
`glossa/llm/factory.py`. Update `reference/internals.md` + `reference/config.md`.

**Add a Job kind.** Add to `JobKind`, write an `enqueue_*`/`run_*` pair following
the ingest/lint shape (per-space lock + `track_background_task` + Job status
transitions + webhook), and extend `JobResult` if it returns new data.

**Move off the in-process runner.** The `Job` model + `asyncio.Task` split is
designed so `enqueue_*` can hand off to Arq/RQ/Celery and `lock_for_space` can
become a Redis lock without changing the API contract — that's the intended
upgrade path.
