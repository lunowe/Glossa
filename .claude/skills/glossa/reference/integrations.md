# Glossa integrations: Python client, webhooks, MCP, Obsidian

How to build **with** Glossa from the outside. `glossa/__init__.py` exports only
`__version__`; import the building blocks from their submodules (shown below).

## Python client — `glossa.mcp.client.GlossaClient`

Async `httpx`-based client (60s timeout). Forwards `GLOSSA_API_TOKEN` as
`Authorization: Bearer …` when set; in `auth_required=false` mode the token can be
omitted.

```python
from glossa.mcp.client import GlossaClient, GlossaClientError

async with GlossaClient.from_env() as c:          # reads GLOSSA_BASE_URL / _API_TOKEN / _DEFAULT_SPACE_ID
    sid = c.resolve_space_id(None)                 # default space, or raises ValueError
    src = await c.create_source(sid, title="Q3 report", content="Allianz reported …")
    job = await c.ingest_source(sid, src["id"])
    # poll
    while (j := await c.get_job(job["id"]))["status"] in ("queued", "running"):
        ...
    ans = await c.query(sid, question="What did Allianz report?", max_pages=8)
    print(ans["answer"], ans["cited_sources"])
```

Constructor: `GlossaClient(base_url, *, api_token=None, default_space_id=None,
client=None)` (or `GlossaClient.from_env(*, client=None)`). Read-only property
`default_space_id`. Raises `GlossaClientError(status, body)` on HTTP ≥ 400.

**Methods** (all `async` unless noted): `list_spaces(tenant_id=None)`,
`get_space(space_id)`, `get_schema(space_id)`, `list_pages(space_id, *, kind=None,
path_prefix=None, limit=100)`, `get_page(space_id, path)`, `get_index(space_id)`,
`get_log(space_id, *, tail=None)`, `get_lint_report(space_id)`,
`create_source(space_id, *, title, content=None, external_uri=None,
metadata=None)`, `ingest_source(space_id, source_id)`, `get_job(job_id)`,
`query(space_id, *, question, max_pages=8)`, `lint(space_id)`, and (sync)
`resolve_space_id(space_id) -> str`.

> The client covers read + source/ingest/query/lint. For admin/key/quota/webhook
> management, call the HTTP API directly (`reference/api.md`) — e.g. with `httpx`.

## Webhooks — receiving & verifying — `glossa.webhooks.signing`

Outbound deliveries carry **`X-Glossa-Signature: t=<unix>,v1=<hex_hmac_sha256>`**
where `v1 = HMAC_SHA256(secret, f"{t}.".encode() + body_bytes)`. Verify inbound
with the SDK — don't re-implement the format:

```python
from glossa.webhooks.signing import verify_signature, SignatureError

# in your handler (FastAPI/Flask/etc.):
try:
    verify_signature(
        payload=request_body_bytes,                       # raw bytes, not parsed JSON
        signature_header=request.headers["X-Glossa-Signature"],
        secret=webhook_secret,                            # the secret from POST …/webhooks
        tolerance_seconds=300,                            # reject replays > 5 min old
    )
except SignatureError:
    return Response(status_code=400)
```

`verify_signature(*, payload, signature_header, secret, tolerance_seconds=300,
now=None) -> None` raises `SignatureError` on malformed / expired / mismatched.
`sign_payload(*, secret, body, timestamp=None) -> (ts, hex_sig)` is the inverse
(useful in tests/mocks). Subscribe events at `POST /spaces/{id}/webhooks`
(`reference/api.md`); event types: `job.complete`, `job.failed`, `page.updated`,
`page.created`, `source.received`.

## MCP server — `glossa-mcp` (`glossa.mcp.server`)

A Model Context Protocol server (stdio) that exposes Glossa as tools/resources to
any MCP client (Claude Desktop/Code, Cursor, Zed). Server name: `glossa`. Reads
`GLOSSA_BASE_URL` (default `http://localhost:8200`), `GLOSSA_DEFAULT_SPACE_ID`
(optional), `GLOSSA_API_TOKEN` (optional). Run standalone:

```sh
pip install -e .
GLOSSA_BASE_URL=http://localhost:8200 GLOSSA_DEFAULT_SPACE_ID=gls_abc glossa-mcp
```

**Tools** (omit `space_id` to use `GLOSSA_DEFAULT_SPACE_ID`):

| Tool | Signature | Returns |
|---|---|---|
| `glossa_query` | `(question, space_id=None, max_pages=8)` | `{answer, pages_consulted, cited_pages, cited_sources}` |
| `glossa_list_spaces` | `(tenant_id=None)` | `list[{id,name,slug,tenant_id,stats}]` |
| `glossa_list_pages` | `(space_id=None, kind=None, path_prefix=None, limit=100)` | `list[page-meta]` |
| `glossa_get_page` | `(path, space_id=None)` | page meta + `content` |
| `glossa_add_source` | `(title, content, external_uri=None, space_id=None, metadata=None, auto_ingest=True)` | `{source, job?}` |
| `glossa_get_job` | `(job_id)` | job status + `result` |
| `glossa_lint` | `(space_id=None)` | queued job |

**Resources**: `glossa://{space_id}/index` (the catalogue),
`glossa://{space_id}/log` (recent history, tail).

Wire into Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`);
Claude Code / Cursor / Zed use the same `mcpServers` shape:

```json
{
  "mcpServers": {
    "glossa": {
      "command": "glossa-mcp",
      "env": { "GLOSSA_BASE_URL": "http://localhost:8200", "GLOSSA_DEFAULT_SPACE_ID": "gls_abc" }
    }
  }
}
```

## Obsidian mirror — `glossa-obsidian-sync` (`glossa.obsidian.sync`)

**One-way** mirror of one Space into an Obsidian vault (Glossa owns the wiki;
Obsidian is a read/browse/graph surface — don't build write-back). Writes
`schema.md`, `index.md`, `log.md`, `lint_report.md` (unless skipped), and every
page at its logical path.

```sh
GLOSSA_BASE_URL=http://localhost:8200 \
GLOSSA_DEFAULT_SPACE_ID=gls_abc \
GLOSSA_OBSIDIAN_VAULT="$HOME/Documents/My Vault" \
glossa-obsidian-sync
```

Flags (each backed by an env var): `--space-id` (`GLOSSA_DEFAULT_SPACE_ID`),
`--vault` (`GLOSSA_OBSIDIAN_VAULT`), `--subdir` (`GLOSSA_OBSIDIAN_SUBDIR`, default
`Glossa`), `--limit` (`GLOSSA_OBSIDIAN_PAGE_LIMIT`, default 1000),
`--skip-lint-report`.

- Default `--subdir Glossa` writes under `Glossa/` **and rewrites wikilinks**
  `[[entities/…]]` → `[[Glossa/entities/…]]` so they resolve in the vault.
- `--subdir ""` writes into the vault root and leaves wikilinks unchanged
  (dedicated-vault setup).

Programmatic: `from glossa.obsidian.sync import sync_space_to_vault, SyncResult`
— `await sync_space_to_vault(*, client, space_id, vault_path, subdir="Glossa",
limit=1000, include_lint_report=True) -> SyncResult(space_id, vault_path,
files_written, pages_written)`. Run it after ingest/lint jobs complete, or on a
cron/launchd timer.

## Host-integration pattern (how Chatforen wires in)

The reference integration (lives in the *host* codebase, not this repo):

1. **Source syncer** — post new artifacts to `POST /spaces/{id}/sources` (push),
   or register a `fetch_callback` per source (pull) so the host stays system of
   record. Then `POST …/ingest`.
2. **Agent tool `query_glossa`** — wraps `POST /spaces/{id}/query`; the agent
   consults the wiki first and falls back to raw retrieval for verification.
3. **Wiki renderer** in the host UI — render page markdown + resolve `[[wikilinks]]`.
4. **Webhooks** — subscribe `job.complete`/`job.failed`; verify with
   `verify_signature`; refresh UI / trigger the Obsidian sync.

Host-extracted fields (author, company, tags) become entity-page anchors during
ingest — no separate extraction pipeline needed on the host side. Query responses
include each cited source's `external_uri`, so the host can deep-link back.
