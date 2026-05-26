from typing import Any

import yaml

FRONTMATTER_MARKER = "---"


def parse(markdown: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter_dict, body).

    Returns ({}, markdown) if no frontmatter block is present.
    """
    if not markdown.startswith(FRONTMATTER_MARKER):
        return {}, markdown
    end = markdown.find(f"\n{FRONTMATTER_MARKER}", len(FRONTMATTER_MARKER))
    if end == -1:
        return {}, markdown
    fm_text = markdown[len(FRONTMATTER_MARKER) : end]
    body_start = end + len(FRONTMATTER_MARKER) + 1
    body = markdown[body_start:].lstrip("\n")
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return {}, markdown
    if not isinstance(fm, dict):
        return {}, markdown
    return fm, body


def serialize(frontmatter: dict[str, Any], body: str) -> str:
    """Render a markdown file with a YAML frontmatter block."""
    if not frontmatter:
        return body
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"{FRONTMATTER_MARKER}\n{fm_text}\n{FRONTMATTER_MARKER}\n\n{body.lstrip()}"
