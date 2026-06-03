"""Default root files for a new Glossa Space."""

DEFAULT_SCHEMA = """# Schema

This Space is an LLM-maintained markdown wiki. The raw sources are the source of
truth; the wiki is a generated working layer; this schema is the operating
contract for how the wiki should be maintained.

## Layers

- Raw sources are immutable evidence. Do not rewrite, reinterpret, or invent
  source content. Summaries and wiki pages must stay traceable to sources.
- The wiki is generated markdown. Use concise pages, durable wikilinks, and
  source-summary citations so later queries can answer from the wiki.
- This schema defines naming, linking, tone, and maintenance conventions. Follow
  it before making or updating pages.

## Page Types

- `summaries/src-<source_id>`: one source summary. It captures the source's main
  claims and links only to canonical wiki pages that exist.
- `entities/<type>/<slug>`: durable people, organizations, products, places,
  datasets, projects, or other named things that recur or matter.
- `entities/topic/<slug>`: durable concepts, themes, methods, risks, markets, or
  issues that are useful beyond one source.
- `syntheses/<slug>`: cross-source synthesis, comparison, tension, or pattern.
  Create these only when at least two durable entity/topic pages are connected by
  a specific relationship and at least one source summary is cited.
- `notes/<slug>`: saved query/chat output. Use sparingly for durable analyses,
  decisions, comparisons, or user-approved notes that should compound.

## Linking And Citations

- Use logical wikilinks without `pages/` and without `.md`, e.g.
  `[[entities/company/allianz]]`.
- Prefer existing canonical pages over creating near-duplicates. Search first.
- Every factual claim added to an entity/topic/synthesis/note page should cite a
  source summary with `[[summaries/src-...]]` when possible.
- Do not link to pages that do not exist. If a concept is useful but not
  page-worthy yet, leave it as plain text.
- External URLs belong in source metadata or summary pages, not as replacements
  for wiki citations.

## Ingest Rules

- Process one source into one summary, then update only the smallest set of
  relevant existing pages.
- Create new entity/topic pages only for durable, specific concepts or named
  things. Generic mentions, one-off examples, and vague categories stay in the
  source summary.
- Use small section or substring edits. Preserve useful existing wording and
  append new sourced context instead of rewriting whole pages.
- Avoid page clutter. A source touching a few strong pages is better than many
  weak pages.

## Query And Chat Rules

- Read `index.md` first, then load only the pages needed for the answer.
- Answer from the wiki and cite with `[[path]]` wikilinks. If the wiki lacks
  enough evidence, say what is missing instead of guessing.
- Save chat/query output as a `notes/<slug>` page only when the result is durable
  and useful later. Keep saved notes compact and cited.

## Lint Rules

- Broken links are defects and should be fixed promptly.
- Orphans, stale claims, contradictions, duplicated pages, and missing citations
  are maintenance signals.
- Prefer fixing canonical pages over creating more pages.
"""

DEFAULT_INDEX = "# Index\n\nNo pages yet.\n"
DEFAULT_LOG = "# Log\n\n"
