"""Prompts for the lint LLM passes.

Only contradiction/supersession detection is LLM-driven in v1. Each call takes
one wiki page plus the summaries of every source it cites, and asks the model
to identify claims that conflict or are superseded.
"""

SYSTEM_LINT_CONTRADICTIONS = """\
You are Glossa, a wiki linter. You are given one wiki page and the summaries
of every source the page cites. Your task: identify claims in the page that
are contradicted by another source, or that have been superseded by a newer
source.

Rules:
- Only flag a finding when at least two sources disagree or when a newer source
  clearly overrides an older claim. Do not invent findings.
- Be specific: paraphrase the affected claim in one short sentence.
- Cite by source_id. The caller maps source_ids back to wiki page paths.
- An empty findings list is the correct answer when the page is consistent.

Output JSON only — no prose, no markdown code fences."""


def contradictions_user_prompt(
    *,
    schema_markdown: str,
    page_path: str,
    page_content: str,
    source_summaries: list[dict],
) -> str:
    summaries_block = "\n\n".join(
        f"--- source_id: {s['source_id']}\n"
        f"title: {s.get('title') or '?'}\n"
        f"created_at: {s.get('created_at') or '?'}\n\n"
        f"{s.get('summary') or ''}"
        for s in source_summaries
    )
    return f"""\
=== SCHEMA (for tone / language only) ===
{schema_markdown}

=== PAGE ===
path: {page_path}

{page_content}

=== CITED SOURCES ===
{summaries_block}

=== TASK ===
Return JSON shaped like:
{{
  "findings": [
    {{
      "claim": "<one-sentence paraphrase of the contradicted/superseded claim from the page>",
      "kind": "contradiction" | "supersession",
      "explanation": "<one sentence: why these sources disagree, or why the newer one supersedes>",
      "source_ids": ["<src_id1>", "<src_id2>"]
    }}
  ]
}}

Return at most 5 findings. If the page is internally consistent, return
{{"findings": []}}.
"""
