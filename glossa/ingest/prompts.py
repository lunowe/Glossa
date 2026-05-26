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

Output JSON only — no prose, no markdown code fences."""


SYSTEM_INGEST_UPDATE_PAGE = """\
You are Glossa, an LLM that maintains a wiki of markdown pages from raw sources.

You are updating one wiki page based on a new source.

Rules:
- If the page already exists, MERGE the new information with existing content.
  Preserve existing claims unless contradicted; when contradicted, note the
  contradiction explicitly with both sources cited.
- Cite every claim with a [[summaries/src-<id>]] wikilink to the source page.
- Use [[entities/...]] wikilinks to reference other entities the page mentions.
- The page MUST start with a YAML frontmatter block between '---' markers.
- Required frontmatter keys: kind, title, source_refs, updated_at.
- Follow the Space's schema.md for tone, entity types, and naming.

Output JSON only — no prose, no markdown code fences."""


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
Identify the entities and concepts in this source that should have wiki
pages (new or updated). Write a self-contained source summary. Produce a
single log line.

Return JSON shaped like:
{{
  "entities": [
    {{
      "type": "<entity_type from schema>",
      "title": "<canonical name>",
      "slug": "<url-safe slug>",
      "page_path": "entities/<type>/<slug>",
      "relevance": "<one sentence: what this source adds about this entity>"
    }}
  ],
  "source_summary_markdown": "<200-600 word self-contained summary, in the schema's tone, using markdown>",
  "log_blurb": "<one sentence about what was ingested>"
}}
"""


def update_page_user_prompt(
    *,
    schema_markdown: str,
    entity_type: str,
    entity_title: str,
    page_path: str,
    existing_page_markdown: str | None,
    source_id: str,
    source_title: str,
    source_summary_markdown: str,
    entity_relevance: str,
) -> str:
    existing_block = existing_page_markdown if existing_page_markdown else "(no existing page — create a new one)"
    return f"""\
=== SCHEMA ===
{schema_markdown}

=== PAGE TO UPDATE ===
path: {page_path}
title: {entity_title}
entity_type: {entity_type}

=== EXISTING PAGE ===
{existing_block}

=== NEW SOURCE ===
source_id: {source_id}
source_title: {source_title}
source_summary_link: [[summaries/src-{source_id}]]

What this source adds about {entity_title}:
{entity_relevance}

Full source summary (for context):
{source_summary_markdown}

=== TASK ===
Return JSON:
{{
  "new_content": "<full markdown file content, including YAML frontmatter>",
  "is_changed": true | false,
  "change_summary": "<one sentence>"
}}

The new_content MUST begin with a YAML frontmatter block:
---
kind: entity
entity_type: {entity_type}
title: {entity_title}
source_refs: [...all source ids that contributed, including {source_id}...]
updated_at: <iso8601 timestamp>
---

Then the markdown body.
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
