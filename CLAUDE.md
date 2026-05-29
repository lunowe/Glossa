# Glossa

LLM-maintained wikis as a service — **markdown is the contract**. Feed it raw
sources; it keeps a structured, interlinked markdown wiki current (entity pages,
syntheses, an index, a log) and you query the wiki instead of re-synthesizing.
FastAPI + MongoDB + MinIO, with an MCP server, one-way Obsidian sync, multi-tenant
API-key auth, per-tenant quotas, and a Jinja2/HTMX dashboard. Python ≥ 3.12; run
locally with `docker compose up --build` (API at `:8200/docs`).

## Start here: the `glossa` skill

`.claude/skills/glossa/` is the working knowledge for this codebase — the 5-object
model, the HTTP API, the Python client, MCP, Obsidian sync, webhooks, internals,
and testing. **Read it before working on Glossa**, and follow its conventions
(e.g. logical page paths, 404-on-cross-tenant, `byo` is the only working LLM mode).

## Keep the skill up to date

The skill is part of Glossa's contract surface. **When you change that surface,
update the skill in the same change** — see `SKILL.md` § "Keeping this skill
accurate" for the file-by-file map. In short:

- route / request / response change → `reference/api.md`
- model field or enum value → `reference/data-model.md`
- `GLOSSA_*` env var, auth mode, or quota dimension → `reference/config.md`
- Python client / MCP / Obsidian / webhook signature → `reference/integrations.md`
- ingest/query/lint pipeline, storage/LLM interface, concurrency → `reference/internals.md`
- test fixtures / fake LLM / test client → `reference/testing.md`

The live OpenAPI at `/docs` is authoritative for endpoints; if the skill and the
code ever disagree, the code wins — fix the skill. Keep `README.md` (human-facing)
and the skill consistent.

## Before committing

```sh
pytest && ruff check . && ruff format --check .
```
