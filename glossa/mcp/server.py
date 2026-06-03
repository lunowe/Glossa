"""Glossa MCP server.

Run with ``glossa-mcp`` (after ``pip install``) or ``python -m glossa.mcp.server``.
Configured entirely via environment variables:

- ``GLOSSA_BASE_URL`` (default ``http://localhost:8200``) — the running Glossa API
- ``GLOSSA_DEFAULT_SPACE_ID`` (optional) — used when a tool call omits ``space_id``
- ``GLOSSA_API_TOKEN`` (optional, future) — bearer token forwarded to Glossa

Tools mirror the HTTP API one-for-one. Each tool's docstring is what the LLM
client sees when deciding whether to call it — keep them specific, actionable,
and grounded in *what the tool returns*.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from glossa.mcp.client import GlossaClient, GlossaClientError

logger = logging.getLogger(__name__)


def build_server(client: GlossaClient) -> FastMCP:
    """Wire a FastMCP instance against a given Glossa client.

    Split out from ``main`` so tests can inject a client backed by a mock
    transport. Returns the configured FastMCP — call ``.run()`` to start it.
    """
    mcp = FastMCP(
        "glossa",
        instructions=(
            "Glossa is an LLM-maintained wiki. Use these tools to consult and "
            "extend a Space's knowledge base. Prefer `glossa_query` for "
            "questions; reach for `glossa_get_page` only when you need the "
            "raw markdown of a specific page. To remember something new, call "
            "`glossa_add_source` with auto_ingest=True."
        ),
    )

    @mcp.tool()
    async def glossa_list_spaces(tenant_id: str | None = None) -> list[dict]:
        """List every Glossa Space available on this server.

        Each entry includes ``id``, ``name``, ``slug``, ``tenant_id`` and
        rolled-up ``stats`` (page_count, source_count, last_ingest_at). Use
        when the user has multiple wikis and you need to disambiguate or when
        no default space is configured.
        """
        return await client.list_spaces(tenant_id=tenant_id)

    @mcp.tool()
    async def glossa_query(
        question: str,
        space_id: str | None = None,
        max_pages: int = 8,
    ) -> dict:
        """Ask the wiki a question. Returns a synthesized markdown answer with citations.

        Glossa first routes the question against the wiki's index, loads at
        most ``max_pages`` pages, then composes the answer citing those pages
        with ``[[path]]`` wikilinks. The response contains:

        - ``answer`` — markdown, possibly multi-paragraph
        - ``pages_consulted`` — page paths Glossa loaded
        - ``cited_pages`` — pages the answer actually cites
        - ``cited_sources`` — original Source records (with ``external_uri``)

        Prefer this over reading raw pages — Glossa has already synthesized
        across multiple sources and resolved citations.
        """
        resolved = client.resolve_space_id(space_id)
        return await client.query(resolved, question=question, max_pages=max_pages)

    @mcp.tool()
    async def glossa_chat(
        message: str,
        space_id: str | None = None,
        context: str | None = None,
        max_pages: int = 8,
        allow_writes: bool = False,
    ) -> dict:
        """Have an interactive wiki chat turn with optional write-back.

        Chat can use tools to read the wiki index, recent log entries, and
        selected pages. With ``allow_writes=True`` it may save a compact durable
        note under ``notes/<slug>`` when the user explicitly asks to save or
        remember the result. For streaming tool-call events, use the HTTP
        ``POST /spaces/{space_id}/chat/stream`` endpoint directly.
        """
        resolved = client.resolve_space_id(space_id)
        return await client.chat(
            resolved,
            message=message,
            context=context,
            max_pages=max_pages,
            allow_writes=allow_writes,
        )

    @mcp.tool()
    async def glossa_list_pages(
        space_id: str | None = None,
        kind: str | None = None,
        path_prefix: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List pages in a Glossa Space (metadata only, no content).

        Filter by ``kind`` (``entity``, ``topic``, ``summary``, ``synthesis``,
        ``comparison``, ``system``, ``custom``) or by ``path_prefix`` (e.g.
        ``entities/company/`` for all company pages). Use to browse the wiki's
        structure before deciding which specific page to read.
        """
        resolved = client.resolve_space_id(space_id)
        return await client.list_pages(
            resolved,
            kind=kind,
            path_prefix=path_prefix,
            limit=limit,
        )

    @mcp.tool()
    async def glossa_get_page(path: str, space_id: str | None = None) -> dict:
        """Read one wiki page's full markdown (frontmatter + body).

        ``path`` is the canonical wiki path without the ``.md`` suffix, e.g.
        ``entities/company/allianz`` or ``summaries/src-abc123``. Returns the
        page metadata plus its ``content`` field. Use when ``glossa_query``
        cites a page and you need the raw markdown.
        """
        resolved = client.resolve_space_id(space_id)
        return await client.get_page(resolved, path)

    @mcp.tool()
    async def glossa_add_source(
        title: str,
        content: str,
        external_uri: str | None = None,
        space_id: str | None = None,
        metadata: dict | None = None,
        auto_ingest: bool = True,
    ) -> dict:
        """Push a new source into the wiki, optionally kicking off an ingest.

        Use when you encounter content the user wants to remember — an article,
        a paste, a transcript, a chat excerpt. ``title`` is human-readable;
        ``content`` is the full text; ``external_uri`` is the canonical link
        back to the original if any.

        With ``auto_ingest=True`` (the default), Glossa queues an ingest Job
        immediately. Response contains the created ``source`` and (when ingest
        was triggered) the resulting ``job`` you can poll with
        ``glossa_get_job``.
        """
        resolved = client.resolve_space_id(space_id)
        source = await client.create_source(
            resolved,
            title=title,
            content=content,
            external_uri=external_uri,
            metadata=metadata,
        )
        result: dict[str, Any] = {"source": source}
        if auto_ingest:
            job = await client.ingest_source(resolved, source["id"])
            result["job"] = job
        return result

    @mcp.tool()
    async def glossa_get_job(job_id: str) -> dict:
        """Look up an async Job by id.

        ``status`` progresses ``queued`` → ``running`` → ``succeeded`` /
        ``failed``. On success, ``result`` holds the structured outcome —
        ingest reports ``pages_created`` / ``pages_updated``; lint reports
        ``lint_findings`` and ``lint_summary``. Poll this after kicking off
        an ingest or lint.
        """
        return await client.get_job(job_id)

    @mcp.tool()
    async def glossa_lint(space_id: str | None = None) -> dict:
        """Run a lint pass over a Space. Returns the queued Job.

        Lint scans every page for orphans (no inbound wikilinks), broken
        wikilinks (target page missing), and — for pages citing 2+ sources —
        LLM-detected contradictions or supersessions. The full report is
        written to ``lint_report.md`` at the bucket root; poll the returned
        Job with ``glossa_get_job`` for structured findings.
        """
        resolved = client.resolve_space_id(space_id)
        return await client.lint(resolved)

    @mcp.resource("glossa://{space_id}/index")
    async def index_resource(space_id: str) -> str:
        """The Space's index.md — the catalogue of every wiki page."""
        try:
            data = await client.get_index(space_id)
        except GlossaClientError as e:
            return f"# Error\n\nCould not read index for space {space_id}: {e}"
        return str(data.get("content") or "")

    @mcp.resource("glossa://{space_id}/log")
    async def log_resource(space_id: str) -> str:
        """The Space's log.md — chronological ingest/lint history."""
        try:
            data = await client.get_log(space_id, tail=20)
        except GlossaClientError as e:
            return f"# Error\n\nCould not read log for space {space_id}: {e}"
        return str(data.get("content") or "")

    return mcp


def main() -> None:
    """Console entry point. Reads config from env and starts the stdio server."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    client = GlossaClient.from_env()
    mcp = build_server(client)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
