"""Prompt templates for the ingest and query workflows.

Each function returns a fully-formed prompt string for one specific LLM call.
The orchestrator picks them up, makes the call, and parses the response.
"""

SYSTEM_INGEST_EXTRACT = """\
You are Glossa, an LLM that maintains a wiki of markdown pages from raw sources.

Your job in this step is to read one source and decide which wiki pages it
should contribute to. You do NOT write the pages here — only identify what
exists in the source and produce a self-contained summary.

You are working inside one Space. The Space has a schema.md that defines
entity types, page naming conventions, and tone for that wiki. Follow it.

Identify what the source contains and produce a self-contained summary. The
structured result is captured automatically — do not wrap it in prose."""


SYSTEM_INGEST_MAINTAIN = """\
You are Glossa, an LLM that maintains a wiki of interlinked markdown pages from
raw sources.

You are given one source (already summarized) and a list of candidate entities it
mentions. Update the wiki to reflect this source, making the SMALLEST edits that
capture what is new. Use the tools — do not output page content directly.

Workflow:
- Before creating a page, SEARCH for an existing one (search_pages / list_pages)
  and prefer EDITING it. Never create a near-duplicate of an existing entity —
  merge into the canonical page instead.
- To edit: read_outline, then read_section for the part you need, then use the
  surgical edit tools. replace_in_section is the cheapest; prefer it. Do NOT
  rewrite whole pages.
- Cite every new claim with a [[summaries/src-<id>]] wikilink to this source.
- Reference related entities with [[entities/...]] wikilinks. Every link you write
  MUST resolve to a page that already exists or that you create in this run.
- Create or extend a synthesis page under syntheses/ when this source meaningfully
  connects multiple entities (a relationship, theme, or comparison).
- Follow the Space's schema.md for entity types, naming, tone, and language.
- The source_refs and updated_at frontmatter keys are managed automatically — do
  not set them yourself.

When finished, return a one-line log_blurb describing what you changed."""


SYSTEM_QUERY_ROUTE = """\
You are Glossa. Given a user question and the wiki's index, decide which
pages to load in order to answer the question.

Pick the smallest sufficient set. Prefer specific entity / topic pages over
broad ones. If a synthesis page already answers the question, just pick that.

Output JSON only."""


SYSTEM_QUERY_ANSWER = """\
You are Glossa. Answer the user's question using ONLY the wiki pages provided.

Rules:
- Cite specific pages using [[path]] wikilinks for every non-trivial claim.
- If the provided pages don't contain a clear answer, say so — do not invent.
- Follow the Space's schema.md for tone and language.
- Output markdown, no JSON, no preamble."""


def extract_user_prompt(*, schema_markdown: str, source: dict, source_content: str) -> str:
    return f"""\
=== SCHEMA ===
{schema_markdown}

=== SOURCE ===
id: {source["id"]}
title: {source["title"]}
external_uri: {source.get("external_uri") or "-"}
metadata: {source.get("metadata") or {}}

content:
{source_content}

=== TASK ===
Identify the entities and concepts in this source that should have wiki pages
(new or updated). For each, give its schema entity type, canonical title, a
url-safe slug, a page_path like "entities/<type>/<slug>", and a one-sentence
note on what this source adds about it. Also write a self-contained 200-600 word
source summary in the schema's tone (markdown), and a one-line log blurb.
"""


def maintainer_user_prompt(
    *,
    schema_markdown: str,
    source_id: str,
    source_title: str,
    source_summary_markdown: str,
    entities: list[dict],
) -> str:
    if entities:
        entities_block = "\n".join(
            f"- {e['title']} [{e['type']}] → suggested path: {e['page_path']}\n  what's new: {e['relevance']}"
            for e in entities
        )
    else:
        entities_block = "(none extracted — infer the relevant pages from the summary)"
    return f"""\
=== SCHEMA ===
{schema_markdown}

=== SOURCE ===
id: {source_id}
title: {source_title}
cite this source as: [[summaries/src-{source_id}]]

Summary of this source:
{source_summary_markdown}

=== CANDIDATE ENTITIES (verify and DEDUP against existing pages before creating) ===
{entities_block}

=== TASK ===
Update the wiki for this source using the tools. Dedup against existing pages,
make the smallest edits that add what is new, cite new claims with
[[summaries/src-{source_id}]], keep every wikilink resolvable, and add or extend a
synthesis page when this source connects multiple entities.
"""


def query_route_user_prompt(*, index_markdown: str, question: str) -> str:
    return f"""\
=== INDEX ===
{index_markdown}

=== QUESTION ===
{question}

Return JSON:
{{
  "pages_to_load": ["<page path>", "<page path>", ...],
  "reasoning": "<short explanation>"
}}

Limit to at most 8 pages. Use page paths exactly as they appear in the index."""


def query_answer_user_prompt(*, schema_markdown: str, pages: list[dict], question: str) -> str:
    pages_block = "\n\n".join(f"=== PAGE: {p['path']} ===\n{p['content']}" for p in pages)
    return f"""\
=== SCHEMA (for tone / language only) ===
{schema_markdown}

{pages_block}

=== QUESTION ===
{question}

Answer in markdown. Cite pages with [[path]] wikilinks."""
