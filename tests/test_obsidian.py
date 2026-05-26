import httpx

from glossa.mcp.client import GlossaClient
from glossa.obsidian.sync import rewrite_wikilinks, sync_space_to_vault


def _json(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_rewrite_wikilinks_prefixes_logical_paths():
    markdown = (
        "[[entities/company/allianz]] "
        "[[summaries/src-a#Notes|source A]] "
        "[[Glossa/entities/company/existing]] "
        "[[https://example.com/x]]"
    )

    result = rewrite_wikilinks(markdown, link_prefix="Glossa")

    assert "[[Glossa/entities/company/allianz]]" in result
    assert "[[Glossa/summaries/src-a#Notes|source A]]" in result
    assert "[[Glossa/entities/company/existing]]" in result
    assert "[[https://example.com/x]]" in result


async def test_sync_space_to_vault_writes_obsidian_mirror(tmp_path):
    requests: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req.url.path)
        if req.url.path == "/spaces/gls_one/schema":
            return _json({"path": "schema.md", "content": "# Schema\n"})
        if req.url.path == "/spaces/gls_one/index":
            return _json({"path": "index.md", "content": "# Index\n\n- [[entities/company/allianz]]\n"})
        if req.url.path == "/spaces/gls_one/log":
            return _json({"path": "log.md", "content": "# Log\n"})
        if req.url.path == "/spaces/gls_one/lint-report":
            return _json({"detail": "lint report not found"}, status=404)
        if req.url.path == "/spaces/gls_one/pages":
            return _json(
                [
                    {
                        "space_id": "gls_one",
                        "path": "entities/company/allianz",
                        "kind": "entity",
                        "title": "Allianz",
                        "frontmatter": {},
                        "source_refs": ["src_a"],
                        "backlinks": [],
                        "updated_at": "2026-05-22T10:00:00Z",
                    }
                ]
            )
        if req.url.path == "/spaces/gls_one/pages/entities/company/allianz":
            return _json(
                {
                    "space_id": "gls_one",
                    "path": "entities/company/allianz",
                    "kind": "entity",
                    "title": "Allianz",
                    "frontmatter": {},
                    "source_refs": ["src_a"],
                    "backlinks": [],
                    "updated_at": "2026-05-22T10:00:00Z",
                    "content": "# Allianz\n\nSee [[summaries/src-src_a]].\n",
                }
            )
        return _json({"detail": req.url.path}, status=500)

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with GlossaClient("http://api.test", client=async_client) as client:
        result = await sync_space_to_vault(
            client=client,
            space_id="gls_one",
            vault_path=tmp_path,
            subdir="Glossa",
        )

    assert result.files_written == 4
    assert result.pages_written == 1
    assert (tmp_path / "Glossa" / "schema.md").read_text() == "# Schema\n"
    assert "[[Glossa/entities/company/allianz]]" in (tmp_path / "Glossa" / "index.md").read_text()
    assert "[[Glossa/summaries/src-src_a]]" in (
        tmp_path / "Glossa" / "entities" / "company" / "allianz.md"
    ).read_text()
    assert "/spaces/gls_one/pages" in requests
