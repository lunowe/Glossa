"""Lint workflow tests.

Covers:
- ``extract_wikilinks`` pure-function semantics (suffix/prefix stripping, alt/anchor)
- ``scan_deterministic`` orphan + broken-link detection
- End-to-end ``run_lint`` with Pydantic AI TestModel overrides:
  - clean wiki (no findings)
  - orphan + broken link findings only (no LLM call)
  - one page with two sources → LLM contradiction finding recorded
"""

from datetime import UTC, datetime

from pydantic_ai.models.test import TestModel

from glossa.db.client import get_db
from glossa.lint import scanner
from glossa.lint.contradictions import contradictions_agent
from glossa.lint.workflow import run_lint
from glossa.models.job import Job, JobKind, JobStatus
from glossa.models.page import Page, PageKind
from glossa.models.source import Source, SourceIngestionMode
from glossa.models.space import Space
from glossa.models.webhook import WebhookEvent
from glossa.usage.models import Operation

# ---------- pure-function tests ----------


def test_extract_wikilinks_strips_prefix_suffix_anchor_alt():
    md = (
        "See [[entities/company/allianz]] and [[pages/entities/topic/cyber.md]] "
        "and [[entities/company/allianz#section]] and [[entities/company/allianz|Allianz]]. "
        "Plus an embed ![[assets/img.png]]."
    )
    links = scanner.extract_wikilinks(md)
    assert "entities/company/allianz" in links
    assert "entities/topic/cyber" in links
    assert "assets/img.png" in links
    assert all("#" not in t and "|" not in t for t in links)


def test_scan_deterministic_flags_orphan_and_broken_link():
    pages = [
        scanner.PageRecord(
            path="entities/company/allianz",
            title="Allianz",
            kind="entity",
            content="# Allianz\n\nMentions [[entities/topic/cyber]] and [[entities/topic/ghost]].",
        ),
        scanner.PageRecord(
            path="entities/topic/cyber",
            title="Cyber",
            kind="entity",
            content="# Cyber\n\nReferenced from elsewhere.",
        ),
        scanner.PageRecord(
            path="entities/topic/lonely",
            title="Lonely",
            kind="entity",
            content="# Lonely\n\nNo one links to me.",
        ),
    ]
    result = scanner.scan_deterministic(pages)
    categories = sorted({f.category for f in result.findings})
    assert "broken_link" in categories
    assert "orphan" in categories

    broken = [f for f in result.findings if f.category == "broken_link"]
    assert any("entities/topic/ghost" in f.related_paths for f in broken)

    orphans = sorted(f.page_path for f in result.findings if f.category == "orphan")
    assert "entities/topic/lonely" in orphans
    assert "entities/company/allianz" in orphans
    assert "entities/topic/cyber" not in orphans


def test_scan_deterministic_ignores_self_links_and_system_targets():
    pages = [
        scanner.PageRecord(
            path="entities/company/x",
            title="X",
            kind="entity",
            content="See [[entities/company/x]] and [[index]] and [[lint_report]] and [[entities/topic/y]].",
        ),
        scanner.PageRecord(
            path="entities/topic/y",
            title="Y",
            kind="entity",
            content="# Y",
        ),
    ]
    result = scanner.scan_deterministic(pages)
    assert not any(f.category == "broken_link" for f in result.findings)


# ---------- end-to-end fixtures ----------


async def _seed_space(storage, *, slug: str = "test") -> Space:
    db = get_db()
    now = datetime.now(UTC)
    space = Space(
        id=f"gls_{slug}",
        tenant_id="t1",
        name=slug,
        slug=slug,
        bucket_uri=f"mem://gls_{slug}/",
        created_at=now,
        updated_at=now,
    )
    await db.spaces.insert_one(space.model_dump())
    await storage.init_space(space.id)
    return space


async def _seed_page(
    storage,
    space_id: str,
    *,
    path: str,
    title: str,
    body: str,
    source_refs: list[str] | None = None,
    kind: PageKind = PageKind.ENTITY,
):
    db = get_db()
    now = datetime.now(UTC)
    fm = (
        "---\n"
        f"kind: {kind.value}\n"
        f"title: {title}\n"
        f"source_refs: [{', '.join(source_refs or [])}]\n"
        f"updated_at: {now.isoformat()}\n"
        "---\n\n"
    )
    await storage.write_page(space_id, f"pages/{path}.md", fm + body)
    page = Page(
        space_id=space_id,
        path=path,
        kind=kind,
        title=title,
        source_refs=source_refs or [],
        updated_at=now,
    )
    await db.pages.insert_one(page.model_dump())


async def _seed_source(space_id: str, source_id: str, *, title: str):
    db = get_db()
    now = datetime.now(UTC)
    source = Source(
        id=source_id,
        space_id=space_id,
        title=title,
        ingestion_mode=SourceIngestionMode.PUSH,
        content_inline="",
        created_at=now,
    )
    await db.sources.insert_one(source.model_dump())


def _new_job(space_id: str) -> Job:
    return Job(
        id=f"job_{space_id[-4:]}",
        space_id=space_id,
        kind=JobKind.LINT,
        inputs={},
        status=JobStatus.QUEUED,
        created_at=datetime.now(UTC),
    )


# ---------- end-to-end tests ----------


async def test_lint_clean_wiki_produces_no_findings(storage, settings):
    space = await _seed_space(storage, slug="clean")
    await _seed_page(
        storage,
        space.id,
        path="entities/company/a",
        title="A",
        body="# A\n\nLinks to [[entities/topic/x]].",
    )
    await _seed_page(
        storage,
        space.id,
        path="entities/topic/x",
        title="X",
        body="# X\n\nLinks back to [[entities/company/a]].",
    )
    db = get_db()
    job = _new_job(space.id)
    await db.jobs.insert_one(job.model_dump())

    # No page has ≥2 sources, so the contradiction agent is never called.
    result = await run_lint(
        job_id=job.id,
        space_id=space.id,
        storage=storage,
        settings=settings,
    )

    assert result.lint_findings == []
    assert result.lint_summary == {}

    job_doc = await db.jobs.find_one({"id": job.id})
    assert job_doc["status"] == JobStatus.SUCCEEDED.value

    report = await storage.read_page(space.id, "lint_report.md")
    assert "All checks clean" in report
    assert "kind: system" in report

    log = await storage.read_page(space.id, "log.md")
    assert "lint | no findings" in log


async def test_lint_flags_orphan_and_broken_link_without_llm(storage, settings):
    space = await _seed_space(storage, slug="orphan")
    await _seed_page(
        storage,
        space.id,
        path="entities/company/a",
        title="A",
        body="# A\n\nLinks to [[entities/topic/ghost]] (broken).",
    )
    await _seed_page(
        storage,
        space.id,
        path="entities/topic/lonely",
        title="Lonely",
        body="# Lonely\n\nNobody references me.",
    )
    db = get_db()
    job = _new_job(space.id)
    await db.jobs.insert_one(job.model_dump())

    # No page has ≥2 sources, so the contradiction agent is never called.
    result = await run_lint(
        job_id=job.id,
        space_id=space.id,
        storage=storage,
        settings=settings,
    )

    categories = sorted({f["category"] for f in result.lint_findings})
    assert "orphan" in categories
    assert "broken_link" in categories
    assert result.lint_summary.get("orphan", 0) >= 2
    assert result.lint_summary.get("broken_link", 0) >= 1

    report = await storage.read_page(space.id, "lint_report.md")
    assert "Orphan pages" in report
    assert "Broken wikilinks" in report
    assert "[[entities/topic/lonely]]" in report
    assert "entities/topic/ghost" in report


async def test_lint_runs_llm_contradiction_check_for_pages_with_two_sources(storage, settings):
    space = await _seed_space(storage, slug="contra")
    await _seed_source(space.id, "src_a", title="Whitepaper 2023")
    await _seed_source(space.id, "src_b", title="Press release 2025")

    await storage.write_page(
        space.id,
        "pages/summaries/src-src_a.md",
        "# Whitepaper 2023\n\nAllianz withdrew from cyber insurance in 2023.",
    )
    await storage.write_page(
        space.id,
        "pages/summaries/src-src_b.md",
        "# Press release 2025\n\nAllianz launches a new cyber product line in 2025.",
    )
    db = get_db()
    await db.pages.insert_one(
        Page(
            space_id=space.id,
            path="summaries/src-src_a",
            kind=PageKind.SUMMARY,
            title="Whitepaper 2023",
            source_refs=["src_a"],
            updated_at=datetime.now(UTC),
        ).model_dump()
    )
    await db.pages.insert_one(
        Page(
            space_id=space.id,
            path="summaries/src-src_b",
            kind=PageKind.SUMMARY,
            title="Press release 2025",
            source_refs=["src_b"],
            updated_at=datetime.now(UTC),
        ).model_dump()
    )

    await _seed_page(
        storage,
        space.id,
        path="entities/company/allianz",
        title="Allianz",
        body=(
            "# Allianz\n\nAllianz withdrew from cyber insurance in 2023 "
            "([[summaries/src-src_a]]). Allianz also launched a new cyber "
            "product line in 2025 ([[summaries/src-src_b]])."
        ),
        source_refs=["src_a", "src_b"],
    )

    job = _new_job(space.id)
    await db.jobs.insert_one(job.model_dump())

    contradiction_model = TestModel(
        custom_output_args={
            "findings": [
                {
                    "claim": "Allianz withdrew from cyber insurance in 2023",
                    "kind": "supersession",
                    "explanation": "The 2025 press release overrides the 2023 withdrawal claim.",
                    "source_ids": ["src_a", "src_b"],
                }
            ]
        },
        call_tools=[],
    )

    with contradictions_agent.override(model=contradiction_model):
        result = await run_lint(
            job_id=job.id,
            space_id=space.id,
            storage=storage,
            settings=settings,
        )

    supersessions = [f for f in result.lint_findings if f["category"] == "supersession"]
    assert len(supersessions) == 1
    assert supersessions[0]["page_path"] == "entities/company/allianz"
    assert supersessions[0]["claim"].startswith("Allianz withdrew")
    assert "summaries/src-src_a" in supersessions[0]["related_paths"]

    report = await storage.read_page(space.id, "lint_report.md")
    assert "Supersessions" in report
    assert "Allianz withdrew from cyber insurance" in report

    usage_count = await db.usage_events.count_documents({"space_id": space.id, "operation": Operation.LINT.value})
    assert usage_count == 1


async def test_lint_webhook_fires_on_success(storage, settings, monkeypatch):
    """Smoke test that the lint workflow fires JOB_COMPLETE."""
    delivered: list[dict] = []

    async def fake_fire(*, space_id, event, payload):
        delivered.append({"space_id": space_id, "event": event, "payload": payload})

    from glossa.ingest import webhook_delivery

    monkeypatch.setattr(webhook_delivery, "fire", fake_fire)

    space = await _seed_space(storage, slug="webhook")
    db = get_db()
    job = _new_job(space.id)
    await db.jobs.insert_one(job.model_dump())

    await run_lint(
        job_id=job.id,
        space_id=space.id,
        storage=storage,
        settings=settings,
    )

    assert any(d["event"] == WebhookEvent.JOB_COMPLETE for d in delivered)
    assert any(d["payload"].get("kind") == JobKind.LINT.value for d in delivered)
