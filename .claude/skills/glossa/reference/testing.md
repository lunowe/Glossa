# Glossa testing reference

350+ tests under `tests/`. They run **without MinIO or Mongo**: an autouse fixture
swaps in `mongomock-motor`, and storage is an in-memory backend. Match the
existing patterns when adding features — every contract change needs a test.

## Commands

```sh
pip install -r requirements-dev.txt
pytest                      # all tests   (asyncio_mode=auto — async tests need no decorator)
pytest tests/test_ingest.py # one file
pytest -x -q                # stop on first failure, quiet
ruff check .                # lint  (E,W,F,I,N,UP,B,SIM,T20,RUF; line-length 120; E501 ignored)
ruff format --check .       # format check
```

Config in `pyproject.toml`: `[tool.pytest.ini_options]` (`testpaths=["tests"]`,
`asyncio_mode="auto"`, `pythonpath=["."]`, `addopts=["-ra","--strict-markers"]`)
and ruff. Tests relax some lints (`tests/** = ["T20","F821","F811","B020"]`), so
`print()` is fine in tests.

## Fixtures — `tests/conftest.py`

- **`mongomock_db`** (autouse) — replaces `db_client._client/_db` with an
  `AsyncMongoMockClient` (`glossa_test` DB) for the test, then tears down. Get the
  handle with `from glossa.db.client import get_db; db = get_db()`.
- **`storage`** — a fresh `InMemoryStorageBackend()`.
- **`settings`** — a `Settings()` with `GLOSSA_DEFAULT_LLM_ENDPOINT=http://test/v1`
  and `GLOSSA_DEFAULT_LLM_API_KEY=test-key` monkeypatched in. Add more
  `monkeypatch.setenv(...)` in your own fixtures/tests for other config.

## Faking the LLM — `tests/fake_llm.py`

`FakeLLMDriver(responses)` where `responses` is either a `list[str]` served in
order, or a callable `(messages) -> str`. It records every call in `.calls`
(a `list[list[LLMMessage]]`) and returns `LLMResponse(content=...)`. Script the
**exact JSON** each pipeline step expects:

```python
import json
from tests.fake_llm import FakeLLMDriver

extract = json.dumps({"entities": [
    {"type": "company", "title": "Allianz", "slug": "allianz",
     "page_path": "entities/company/allianz", "relevance": "…"}],
    "source_summary_markdown": "…", "log_blurb": "…"})
update  = json.dumps({"new_content": "# Allianz\n…", "is_changed": True, "change_summary": "…"})
llm = FakeLLMDriver([extract, update])     # extract call, then one per entity
# query: [route_json, answer_markdown]; lint: [contradiction_json] per ≥2-source page
```

## Skeleton — exercise a pipeline directly (no HTTP)

```python
import json
from datetime import UTC, datetime
from glossa.db.client import get_db
from glossa.models.space import Space
from glossa.models.source import Source, SourceIngestionMode
from glossa.ingest.workflow import run_ingest          # call the runner directly
from tests.fake_llm import FakeLLMDriver

async def test_ingest_creates_pages(storage, settings):   # fixtures injected
    db = get_db()
    now = datetime.now(UTC)
    space = Space(id="gls_t", tenant_id="t1", name="W", slug="w",
                  bucket_uri="s3://glossa-spaces/gls_t/", created_at=now, updated_at=now)
    await db.spaces.insert_one(space.model_dump())
    await storage.init_space(space.id)
    src = Source(id="src_1", space_id=space.id, title="Q3",
                 ingestion_mode=SourceIngestionMode.PUSH, content_inline="Allianz …", created_at=now)
    await db.sources.insert_one(src.model_dump())

    llm = FakeLLMDriver([extract_json, update_json])
    # run_ingest's exact signature lives in glossa/ingest/workflow.py — confirm there.
    result = await run_ingest(space_id=space.id, source_id=src.id,
                              storage=storage, settings=settings, llm=llm)

    page = await storage.read_page(space.id, "pages/entities/company/allianz.md")
    assert "[[summaries/src-src_1]]" in page
    assert "[[entities/company/allianz]]" in await storage.read_page(space.id, "index.md")
    assert len(llm.calls) == 2
```

> Pipeline runners (`run_ingest`, `run_lint`, `answer_question`) take `storage`,
> `settings`, and `llm` as parameters precisely so tests can inject fakes. The
> `enqueue_*` wrappers build the real driver via the factory and run in the
> background — for unit tests call the runner directly. Confirm the current
> signature in the source before relying on it.

## Skeleton — hit the HTTP API

The app is module-level in `glossa/main.py`. **Do not** enter the `TestClient`
context manager (its `lifespan` would dial MinIO/Mongo); instead wire
`app.state` yourself, exactly like `tests/test_auth.py`:

```python
from fastapi.testclient import TestClient
from glossa.main import app
from glossa.config import Settings
from glossa.storage.memory import InMemoryStorageBackend

def make_client(*, auth_required=False, bootstrap=None):
    # build Settings with the env you need (see tests/test_auth.py:_make_settings)
    app.state.settings = Settings(...)               # auth_required, bootstrap key, etc.
    app.state.storage = InMemoryStorageBackend()
    return TestClient(app)                            # NOT `with TestClient(app) as c:`

def test_healthz():
    c = make_client()
    assert c.get("/healthz").json()["status"] == "ok"

def test_requires_auth_when_enabled():
    c = make_client(auth_required=True)
    assert c.get("/spaces").status_code == 401        # no header → 401 in hosted mode
```

For authenticated routes, seed an `ApiKey` doc (hash a known plaintext via
`glossa.models.api_key.hash_key`) and send `Authorization: Bearer <plaintext>`, or
use `auth_required=False` for the synthetic-admin path. See `tests/test_auth.py`,
`tests/test_admin.py`, `tests/test_tenant_isolation.py` for worked examples.

## What to mirror per area

| Adding/Changing | Look at | Assert on |
|---|---|---|
| Ingest | `test_ingest.py` | pages in storage, DB Page docs, `index.md`/`log.md`, `llm.calls` |
| URL / upload ingest | `test_ingest_url_upload.py` | monkeypatch `url_fetcher.fetch_url_as_markdown` / `doc_parser.parse_asset_to_text` (never hit the network or LiteParse); `storage.write_asset`/`read_asset`; the upload HTTP route |
| Query | `test_query.py` | `pages_consulted`, `cited_pages`, `cited_sources` |
| Lint | `test_lint.py` | `lint_findings`, `lint_summary`, `lint_report.md` |
| Auth/scopes/isolation | `test_auth.py`, `test_tenant_isolation.py` | 401/403/404, synthetic admin, bootstrap |
| Admin/keys | `test_admin.py`, `test_api_keys.py` | `plaintext` shown once, revoke idempotency, 409 on dup email |
| Activity/usage/quota | `test_activity.py`, `test_usage.py`, `test_quota_extensions.py` | recorded events, 402 on block |
| Webhooks | `test_webhook_signing.py` | `sign_payload`/`verify_signature`, replay window |
| OAuth/sessions/dashboard | `test_oauth.py`, `test_sessions.py`, `test_dashboard*.py` | cookie set, membership gates, e2e signup→key→API call |
| MCP/Obsidian | `test_mcp.py`, `test_obsidian.py` | tool wiring, files written, wikilink rewrite |
