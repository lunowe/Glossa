"""Pydantic AI agents for ingest: single-shot extract + the agentic maintainer.

``extract_agent`` reads one source and returns structured entities + a summary.

``maintainer_agent`` is the agentic core: given the extraction it inspects the
existing wiki and applies the SMALLEST edits that capture what's new, using
surgical patch tools (section / substring edits) rather than rewriting whole
pages. It can dedup into existing pages and create synthesis pages. Edits mutate
an in-memory :class:`~glossa.ingest.working_copy.WorkingCopy`; the deterministic
``flush_working_copy`` validates and writes them through ``page_writer.upsert_page``
(quota enforcement + ``source_refs``/``updated_at`` bookkeeping live there, never
in the model). Guardrails (page / edit-byte / step caps) bound cost and the
per-space lock hold; hitting one ends the run cleanly and is recorded.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from glossa.db.client import get_db
from glossa.ingest import page_writer
from glossa.ingest.prompts import SYSTEM_INGEST_EXTRACT, SYSTEM_INGEST_MAINTAIN, maintainer_user_prompt
from glossa.ingest.working_copy import WorkingCopy
from glossa.models.page import PageKind
from glossa.utils import frontmatter, md_sections
from glossa.utils.slug import slugify
from glossa.utils.wikilinks import extract_wikilinks, normalize_page_path

if TYPE_CHECKING:
    from pydantic_ai.models import Model
    from pydantic_ai.usage import RunUsage

    from glossa.config import Settings
    from glossa.models.source import Source
    from glossa.models.space import Space
    from glossa.storage.base import StorageBackend


# --- Structured outputs ----------------------------------------------------


class EntityOut(BaseModel):
    type: str = "topic"
    title: str
    slug: str = ""
    page_path: str = ""
    page_action: Literal["update_existing", "create_candidate", "summary_only"] = "create_candidate"
    importance: int = Field(default=3, ge=1, le=5)
    relevance: str = ""


class ExtractionOut(BaseModel):
    entities: list[EntityOut] = Field(default_factory=list)
    source_summary_markdown: str = ""
    log_blurb: str = "ingested source"


class MaintainerReport(BaseModel):
    """The maintainer's self-report. The real change set is derived from the
    working copy at flush — not trusted from this object."""

    log_blurb: str = ""
    notes: str = ""


# --- Agents ----------------------------------------------------------------

extract_agent = Agent(output_type=ExtractionOut, instructions=SYSTEM_INGEST_EXTRACT)


@dataclass
class MaintainerDeps:
    wc: WorkingCopy
    storage: "StorageBackend"
    space_id: str
    source_id: str
    source_title: str
    source_summary_markdown: str
    summary_path: str
    max_pages: int
    max_edit_bytes: int


maintainer_agent = Agent(
    deps_type=MaintainerDeps,
    output_type=MaintainerReport,
    instructions=SYSTEM_INGEST_MAINTAIN,
)


# --- Tool helpers ----------------------------------------------------------


def _check_budget(deps: MaintainerDeps, path: str) -> None:
    path = normalize_page_path(path)
    if path not in deps.wc.dirty and len(deps.wc.dirty) >= deps.max_pages:
        raise ModelRetry(f"Page budget reached ({deps.max_pages} pages). Finalize with the edits made so far.")
    if deps.wc.edited_bytes >= deps.max_edit_bytes:
        raise ModelRetry("Edit-size budget reached. Finalize with the edits made so far.")


async def _load_existing(deps: MaintainerDeps, path: str) -> str:
    path = normalize_page_path(path)
    content = await deps.wc.load(path)
    if not content:
        raise ModelRetry(f"Page {path!r} does not exist. Use create_page to create it, or choose an existing page.")
    return content


def _page_search_score(*, query: str, path: str, title: str) -> int:
    needle = query.strip().lower()
    if not needle:
        return 0
    terms = [term for term in slugify(query).split("-") if len(term) >= 3]
    haystack = f"{path.lower()} {title.lower()} {slugify(title)}"
    score = 100 if needle in haystack else 0
    score += sum(1 for term in terms if term in haystack)
    return score


# --- Read tools ------------------------------------------------------------


@maintainer_agent.tool
async def search_pages(ctx: RunContext[MaintainerDeps], query: str) -> list[dict]:
    """Find existing wiki pages whose path or title contains `query` (case-insensitive).

    Use this to DEDUP before creating a page — if a matching entity already
    exists, edit that page instead of making a near-duplicate.
    """
    db = get_db()
    cursor = db.pages.find({"space_id": ctx.deps.space_id}, {"path": 1, "title": 1, "kind": 1})
    scored: list[tuple[int, dict]] = []
    async for d in cursor:
        title = d.get("title") or ""
        score = _page_search_score(query=query, path=d["path"], title=title)
        if score:
            scored.append((score, {"path": d["path"], "title": title, "kind": d.get("kind", "")}))
    return [item for _score, item in sorted(scored, key=lambda row: (-row[0], row[1]["path"]))[:20]]


@maintainer_agent.tool
async def list_pages(ctx: RunContext[MaintainerDeps], prefix: str = "entities/") -> list[str]:
    """List existing wiki page paths under `prefix`."""
    db = get_db()
    cursor = db.pages.find({"space_id": ctx.deps.space_id}, {"path": 1})
    return [d["path"] async for d in cursor if d["path"].startswith(prefix)][:200]


@maintainer_agent.tool
async def read_index(ctx: RunContext[MaintainerDeps]) -> str:
    """Read the wiki index (index.md) for navigation."""
    return await ctx.deps.storage.read_page(ctx.deps.space_id, "index.md") or "(empty)"


@maintainer_agent.tool
async def read_page(ctx: RunContext[MaintainerDeps], path: str) -> str:
    """Read a page's full markdown (frontmatter + body). Returns a placeholder if absent."""
    path = normalize_page_path(path)
    if path == ctx.deps.summary_path:
        return ctx.deps.source_summary_markdown or "(empty source summary)"
    content = await ctx.deps.wc.load(path)
    return content or "(page does not exist)"


@maintainer_agent.tool
async def read_outline(ctx: RunContext[MaintainerDeps], path: str) -> list[dict]:
    """List a page's section headings (cheap). Read this before reading/editing a section."""
    _, body = frontmatter.parse(await _load_existing(ctx.deps, path))
    return [{"heading": s["heading"], "level": s["level"]} for s in md_sections.outline(body)]


@maintainer_agent.tool
async def read_section(ctx: RunContext[MaintainerDeps], path: str, heading: str) -> str:
    """Read one section of a page by its heading text."""
    _, body = frontmatter.parse(await _load_existing(ctx.deps, path))
    try:
        return md_sections.get_section(body, heading)
    except md_sections.SectionError as e:
        raise ModelRetry(str(e)) from e


# --- Write tools (mutate the working copy only) ----------------------------


@maintainer_agent.tool
async def replace_in_section(ctx: RunContext[MaintainerDeps], path: str, heading: str, old: str, new: str) -> str:
    """Replace an exact, UNIQUE substring within one section — the cheapest edit.

    `old` must occur exactly once in that section. Prefer this for small changes.
    """
    path = normalize_page_path(path)
    _check_budget(ctx.deps, path)
    fm, body = frontmatter.parse(await _load_existing(ctx.deps, path))
    try:
        section = md_sections.get_section(body, heading)
        new_section = md_sections.replace_substring(section, old, new)
        new_body = md_sections.replace_section(body, heading, new_section)
    except md_sections.SectionError as e:
        raise ModelRetry(str(e)) from e
    ctx.deps.wc.put(path, frontmatter.serialize(fm, new_body) if fm else new_body)
    return f"edited section {heading!r} in {path}"


@maintainer_agent.tool
async def replace_section(ctx: RunContext[MaintainerDeps], path: str, heading: str, new_section_markdown: str) -> str:
    """Replace a whole section. `new_section_markdown` MUST include the heading line (e.g. '## Title')."""
    path = normalize_page_path(path)
    _check_budget(ctx.deps, path)
    fm, body = frontmatter.parse(await _load_existing(ctx.deps, path))
    try:
        new_body = md_sections.replace_section(body, heading, new_section_markdown)
    except md_sections.SectionError as e:
        raise ModelRetry(str(e)) from e
    ctx.deps.wc.put(path, frontmatter.serialize(fm, new_body) if fm else new_body)
    return f"replaced section {heading!r} in {path}"


@maintainer_agent.tool
async def add_section(
    ctx: RunContext[MaintainerDeps], path: str, heading: str, content: str, after: str | None = None
) -> str:
    """Add a new section to a page (rendered as a level-2 heading).

    Optionally insert immediately after an existing section's heading via `after`.
    """
    path = normalize_page_path(path)
    _check_budget(ctx.deps, path)
    fm, body = frontmatter.parse(await _load_existing(ctx.deps, path))
    try:
        new_body = md_sections.add_section(body, heading, content, after=after)
    except md_sections.SectionError as e:
        raise ModelRetry(str(e)) from e
    ctx.deps.wc.put(path, frontmatter.serialize(fm, new_body) if fm else new_body)
    return f"added section {heading!r} to {path}"


@maintainer_agent.tool
async def remove_section(ctx: RunContext[MaintainerDeps], path: str, heading: str) -> str:
    """Remove a section from a page by its heading text."""
    path = normalize_page_path(path)
    _check_budget(ctx.deps, path)
    fm, body = frontmatter.parse(await _load_existing(ctx.deps, path))
    try:
        new_body = md_sections.remove_section(body, heading)
    except md_sections.SectionError as e:
        raise ModelRetry(str(e)) from e
    ctx.deps.wc.put(path, frontmatter.serialize(fm, new_body) if fm else new_body)
    return f"removed section {heading!r} from {path}"


@maintainer_agent.tool
async def create_page(ctx: RunContext[MaintainerDeps], path: str, kind: str, title: str, body: str) -> str:
    """Create a NEW page.

    `kind` is typically 'entity' or 'synthesis'. `path` is a logical path like
    'entities/company/allianz' or 'syntheses/cyber-kmu'. `body` is markdown
    WITHOUT frontmatter (it is added automatically). Errors if the page already
    exists — edit it instead of creating a duplicate.
    """
    path = normalize_page_path(path)
    await ctx.deps.wc.load(path)
    if ctx.deps.wc.exists(path):
        raise ModelRetry(f"Page {path!r} already exists — edit it instead of creating a duplicate.")
    _check_budget(ctx.deps, path)
    fm = {"kind": kind, "title": title, "source_refs": [], "updated_at": ""}
    ctx.deps.wc.put(path, frontmatter.serialize(fm, body), created=True, kind=kind, title=title)
    return f"created {path}"


@maintainer_agent.tool
async def set_frontmatter(ctx: RunContext[MaintainerDeps], path: str, key: str, value: str) -> str:
    """Set a frontmatter key on a page (e.g. 'entity_type').

    `source_refs` and `updated_at` are managed automatically — do not set them.
    """
    if key in ("source_refs", "updated_at"):
        raise ModelRetry(f"{key!r} is managed automatically; do not set it.")
    path = normalize_page_path(path)
    _check_budget(ctx.deps, path)
    fm, body = frontmatter.parse(await _load_existing(ctx.deps, path))
    fm[key] = value
    ctx.deps.wc.put(path, frontmatter.serialize(fm, body))
    return f"set {key} on {path}"


# --- Output validation (self-correcting loop) ------------------------------


async def _known_page_paths(space_id: str, wc: WorkingCopy, extra_paths: list[str] | None = None) -> set[str]:
    db = get_db()
    paths = {normalize_page_path(path) for path in wc.dirty}
    for path in extra_paths or []:
        normalized = normalize_page_path(path)
        if normalized:
            paths.add(normalized)
    cursor = db.pages.find({"space_id": space_id}, {"path": 1})
    async for d in cursor:
        paths.add(normalize_page_path(d["path"]))
    return paths


async def _working_copy_problems(
    *, space_id: str, wc: WorkingCopy, extra_known_paths: list[str] | None = None
) -> list[str]:
    if not wc.dirty:
        return []
    known = await _known_page_paths(space_id, wc, extra_paths=extra_known_paths)
    problems: list[str] = []
    for path in sorted(wc.dirty):
        content = wc.content(path)
        fm, _ = frontmatter.parse(content)
        for key in ("kind", "title"):
            if not fm.get(key):
                problems.append(f"{path}: missing required frontmatter '{key}'")
        targets = extract_wikilinks(content)
        for target in targets:
            if "/" in target and target not in known:
                problems.append(f"{path}: link [[{target}]] does not resolve to any page")
        kind = str(fm.get("kind") or wc.meta(path).get("kind") or "")
        if kind in {"synthesis", "comparison"} or path.startswith("syntheses/"):
            entity_targets = {target for target in targets if target.startswith("entities/")}
            source_targets = {target for target in targets if target.startswith("summaries/src-")}
            if len(entity_targets) < 2:
                problems.append(f"{path}: synthesis pages must link at least two entity pages")
            if not source_targets:
                problems.append(f"{path}: synthesis pages must cite at least one source summary")
    return problems


@maintainer_agent.output_validator
async def _validate_changes(ctx: RunContext[MaintainerDeps], report: MaintainerReport) -> MaintainerReport:
    problems = await _working_copy_problems(
        space_id=ctx.deps.space_id,
        wc=ctx.deps.wc,
        extra_known_paths=[ctx.deps.summary_path],
    )
    if problems:
        raise ModelRetry("Fix these issues, then finish:\n- " + "\n- ".join(problems[:20]))
    return report


# --- Orchestration ---------------------------------------------------------


async def run_maintainer(
    *,
    model: "Model",
    model_settings: dict,
    retries: int,
    space: "Space",
    source: "Source",
    source_summary_markdown: str,
    entities: list[dict],
    schema_markdown: str,
    storage: "StorageBackend",
    settings: "Settings",
) -> tuple[WorkingCopy, MaintainerReport | None, "RunUsage | None", bool]:
    """Run the maintainer agent. Returns (working_copy, report, usage, capped).

    ``capped`` is True if the step cap stopped the run early; edits made so far
    are still in the working copy for the flush (never silently dropped).
    """
    wc = WorkingCopy(storage, space.id)
    deps = MaintainerDeps(
        wc=wc,
        storage=storage,
        space_id=space.id,
        source_id=source.id,
        source_title=source.title,
        source_summary_markdown=source_summary_markdown,
        summary_path=f"summaries/src-{source.id}",
        max_pages=settings.ingest_max_pages_per_run,
        max_edit_bytes=settings.ingest_max_edit_bytes,
    )
    prompt = maintainer_user_prompt(
        schema_markdown=schema_markdown,
        source_id=source.id,
        source_title=source.title,
        source_summary_markdown=source_summary_markdown,
        entities=entities,
    )
    try:
        result = await maintainer_agent.run(
            prompt,
            model=model,
            model_settings=model_settings,
            deps=deps,
            retries=retries,
            usage_limits=UsageLimits(request_limit=settings.ingest_max_agent_steps),
        )
        return wc, result.output, result.usage, False
    except UsageLimitExceeded:
        return wc, None, None, True


async def flush_working_copy(
    *,
    wc: WorkingCopy,
    space: "Space",
    source: "Source",
    job_id: str,
    storage: "StorageBackend",
) -> tuple[list[str], list[str]]:
    """Validate, stamp, and persist every touched page. Returns (created, updated)."""
    db = get_db()
    problems = await _working_copy_problems(
        space_id=space.id,
        wc=wc,
        extra_known_paths=[f"summaries/src-{source.id}"],
    )
    if problems:
        raise RuntimeError("Refusing to flush invalid maintainer edits:\n- " + "\n- ".join(problems[:20]))

    now_iso = datetime.now(UTC).isoformat()
    created: list[str] = []
    updated: list[str] = []
    for path in sorted(wc.dirty):
        fm, body = frontmatter.parse(wc.content(path))
        kind = fm.get("kind") or wc.meta(path).get("kind") or PageKind.ENTITY.value
        title = fm.get("title") or wc.meta(path).get("title") or path.rsplit("/", 1)[-1]
        existing = await db.pages.find_one({"space_id": space.id, "path": path}, {"source_refs": 1})
        existing_refs = (existing or {}).get("source_refs") or []
        merged_refs = list(dict.fromkeys([*existing_refs, *(fm.get("source_refs") or []), source.id]))
        fm["kind"], fm["title"], fm["source_refs"], fm["updated_at"] = kind, title, merged_refs, now_iso
        try:
            page_kind = PageKind(kind)
        except ValueError:
            page_kind = PageKind.ENTITY
        is_new, is_changed = await page_writer.upsert_page(
            storage=storage,
            space_id=space.id,
            page_path=path,
            kind=page_kind,
            title=title,
            new_content=frontmatter.serialize(fm, body),
            source_refs=merged_refs,
            job_id=job_id,
            tenant_id=space.tenant_id,
        )
        if is_new:
            created.append(path)
        elif is_changed:
            updated.append(path)
    return created, updated
