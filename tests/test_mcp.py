"""MCP server tests.

Covers the ``GlossaClient`` HTTP wrapper (URL routing + payload shape against
an httpx MockTransport) and the ``FastMCP`` tool/resource wiring (so the
tools fan out to the right client calls and respect the default-space env
var).
"""

import json
from collections.abc import Callable

import httpx
import pytest

from glossa.mcp.client import GlossaClient, GlossaClientError
from glossa.mcp.server import build_server


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _render_tool_result(result) -> str:
    """Flatten the various shapes ``FastMCP.call_tool`` can return into one string.

    Some versions return ``Sequence[ContentBlock]``; others return a
    ``(blocks, structured_result)`` tuple when structured output is enabled.
    For assertion purposes we just want the concatenated text content.
    """
    blocks = result[0] if isinstance(result, tuple) else result
    parts: list[str] = []
    for b in blocks:
        text = getattr(b, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


# ---------- client tests ----------


async def test_client_list_spaces_hits_correct_url():
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return _json([{"id": "gls_one", "name": "One"}])

    async with GlossaClient("http://api.test", client=_mock_client(handler)) as client:
        spaces = await client.list_spaces()

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "http://api.test/spaces"
    assert spaces == [{"id": "gls_one", "name": "One"}]


async def test_client_forwards_bearer_token_when_set():
    captured: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req.headers.get("authorization"))
        return _json([])

    async with GlossaClient("http://api.test", api_token="tkn-123", client=_mock_client(handler)) as client:
        await client.list_spaces()

    assert captured == ["Bearer tkn-123"]


async def test_client_query_posts_question_payload():
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return _json({"answer": "ok", "pages_consulted": [], "cited_pages": [], "cited_sources": []})

    async with GlossaClient("http://api.test", client=_mock_client(handler)) as client:
        result = await client.query("gls_one", question="What is X?", max_pages=4)

    assert captured == [{"question": "What is X?", "max_pages": 4}]
    assert result["answer"] == "ok"


async def test_client_chat_posts_message_payload():
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return _json({"answer": "ok"})

    async with GlossaClient("http://api.test", client=_mock_client(handler)) as client:
        result = await client.chat("gls_one", message="What is X?", context="posted", max_pages=4, allow_writes=True)

    assert captured == [
        {
            "messages": [{"role": "user", "content": "What is X?"}],
            "context": "posted",
            "max_pages": 4,
            "allow_writes": True,
        }
    ]
    assert result["answer"] == "ok"


async def test_client_create_source_serializes_push_payload():
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return _json({"id": "src_xyz", "title": "T"})

    async with GlossaClient("http://api.test", client=_mock_client(handler)) as client:
        await client.create_source(
            "gls_one",
            title="Article",
            content="Body text",
            external_uri="https://example.com/x",
            metadata={"tag": "v1"},
        )

    assert captured == [
        {
            "title": "Article",
            "ingestion_mode": "push",
            "content_inline": "Body text",
            "external_uri": "https://example.com/x",
            "metadata": {"tag": "v1"},
        }
    ]


async def test_client_raises_on_http_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return _json({"detail": "not found"}, status=404)

    async with GlossaClient("http://api.test", client=_mock_client(handler)) as client:
        with pytest.raises(GlossaClientError) as exc:
            await client.get_page("gls_one", "missing")

    assert exc.value.status == 404
    assert exc.value.body == {"detail": "not found"}


async def test_client_resolve_space_id_uses_default_when_missing():
    async with GlossaClient(
        "http://api.test", default_space_id="gls_default", client=_mock_client(lambda r: _json({}))
    ) as client:
        assert client.resolve_space_id(None) == "gls_default"
        assert client.resolve_space_id("gls_other") == "gls_other"


async def test_client_resolve_space_id_raises_without_default():
    async with GlossaClient("http://api.test", client=_mock_client(lambda r: _json({}))) as client:
        with pytest.raises(ValueError, match="No space_id"):
            client.resolve_space_id(None)


# ---------- FastMCP wiring tests ----------


async def test_mcp_query_tool_calls_query_endpoint():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _json(
            {
                "answer": "Allianz launched a new product in 2025.",
                "pages_consulted": ["entities/company/allianz"],
                "cited_pages": ["entities/company/allianz"],
                "cited_sources": [{"id": "src_a", "title": "Press release"}],
            }
        )

    async with GlossaClient("http://api.test", default_space_id="gls_one", client=_mock_client(handler)) as client:
        mcp = build_server(client)
        result = await mcp.call_tool("glossa_query", {"question": "What did Allianz launch?"})

    assert len(captured) == 1
    assert captured[0].method == "POST"
    assert captured[0].url.path == "/spaces/gls_one/query"
    rendered = _render_tool_result(result)
    assert "Allianz" in rendered


async def test_mcp_chat_tool_calls_chat_endpoint():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _json({"answer": "ok"})

    async with GlossaClient("http://api.test", default_space_id="gls_one", client=_mock_client(handler)) as client:
        mcp = build_server(client)
        result = await mcp.call_tool("glossa_chat", {"message": "What changed?", "allow_writes": True})

    assert len(captured) == 1
    assert captured[0].method == "POST"
    assert captured[0].url.path == "/spaces/gls_one/chat"
    payload = json.loads(captured[0].content)
    assert payload["messages"] == [{"role": "user", "content": "What changed?"}]
    assert payload["allow_writes"] is True
    rendered = _render_tool_result(result)
    assert "ok" in rendered


async def test_mcp_add_source_auto_ingests_by_default():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if req.url.path.endswith("/sources"):
            return _json({"id": "src_new", "title": "T"})
        if req.url.path.endswith("/ingest"):
            return _json({"id": "job_abc", "status": "queued"})
        return _json({}, status=500)

    async with GlossaClient("http://api.test", default_space_id="gls_one", client=_mock_client(handler)) as client:
        mcp = build_server(client)
        result = await mcp.call_tool(
            "glossa_add_source",
            {"title": "Article", "content": "Body"},
        )

    paths = [r.url.path for r in captured]
    assert "/spaces/gls_one/sources" in paths
    assert "/spaces/gls_one/sources/src_new/ingest" in paths
    payload = _render_tool_result(result)
    assert "src_new" in payload
    assert "job_abc" in payload


async def test_mcp_add_source_can_skip_ingest():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _json({"id": "src_new", "title": "T"})

    async with GlossaClient("http://api.test", default_space_id="gls_one", client=_mock_client(handler)) as client:
        mcp = build_server(client)
        await mcp.call_tool(
            "glossa_add_source",
            {"title": "T", "content": "B", "auto_ingest": False},
        )

    paths = [r.url.path for r in captured]
    assert "/spaces/gls_one/sources" in paths
    assert not any(p.endswith("/ingest") for p in paths)


async def test_mcp_lint_tool_uses_default_space():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _json({"id": "job_lint", "kind": "lint", "status": "queued"})

    async with GlossaClient("http://api.test", default_space_id="gls_one", client=_mock_client(handler)) as client:
        mcp = build_server(client)
        await mcp.call_tool("glossa_lint", {})

    assert captured[0].url.path == "/spaces/gls_one/lint"
    assert captured[0].method == "POST"


async def test_mcp_lint_tool_accepts_explicit_space():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _json({"id": "job_lint", "status": "queued"})

    async with GlossaClient("http://api.test", default_space_id="gls_default", client=_mock_client(handler)) as client:
        mcp = build_server(client)
        await mcp.call_tool("glossa_lint", {"space_id": "gls_other"})

    assert captured[0].url.path == "/spaces/gls_other/lint"


async def test_mcp_index_resource_reads_index_endpoint():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/spaces/gls_one/index":
            return _json({"path": "index.md", "content": "# Index\n\n- entry"})
        return _json({}, status=404)

    async with GlossaClient("http://api.test", client=_mock_client(handler)) as client:
        mcp = build_server(client)
        contents = await mcp.read_resource("glossa://gls_one/index")

    rendered = "".join(c.content for c in contents)
    assert "# Index" in rendered


async def test_mcp_server_registers_expected_tools_and_resources():
    """Smoke test: the public surface doesn't silently shrink between releases."""
    async with GlossaClient("http://api.test", client=_mock_client(lambda r: _json({}))) as client:
        mcp = build_server(client)
        tools = {t.name for t in mcp._tool_manager.list_tools()}
        resources = {str(r.uri_template) for r in mcp._resource_manager.list_templates()}

    assert tools == {
        "glossa_list_spaces",
        "glossa_query",
        "glossa_chat",
        "glossa_list_pages",
        "glossa_get_page",
        "glossa_add_source",
        "glossa_get_job",
        "glossa_lint",
    }
    assert resources == {
        "glossa://{space_id}/index",
        "glossa://{space_id}/log",
    }
