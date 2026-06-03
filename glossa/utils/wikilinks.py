"""Helpers for Glossa logical page paths and Obsidian-style wikilinks."""

from __future__ import annotations

import re

_WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\n]+?)\]\]")


def is_external_target(target: str) -> bool:
    """Return true for targets that are not Glossa page paths."""
    return "://" in target or target.startswith(("mailto:", "tel:"))


def normalize_page_path(path: str) -> str:
    """Normalize user/model supplied page paths to Glossa logical paths.

    Glossa stores page records as logical paths like ``entities/company/a``.
    Models and humans sometimes include Obsidian/storage affordances such as a
    leading ``pages/`` prefix, a trailing ``.md``, anchors, or aliases; these
    should all resolve to the same page identity.
    """
    target = str(path).strip()
    if target.startswith("[[") and target.endswith("]]"):
        target = target[2:-2]
    target = target.split("|", 1)[0].split("#", 1)[0].strip()
    if not target or is_external_target(target):
        return target
    while target.startswith("./"):
        target = target[2:]
    target = target.lstrip("/")
    if target.startswith("pages/"):
        target = target[len("pages/") :]
    if target.endswith(".md"):
        target = target[: -len(".md")]
    return target.strip("/")


def extract_wikilinks(markdown: str, *, include_embeds: bool = True, include_external: bool = False) -> list[str]:
    """Return normalized page-path targets from ``[[...]]`` wikilinks."""
    targets: list[str] = []
    for embed_marker, raw in _WIKILINK_RE.findall(markdown):
        if embed_marker and not include_embeds:
            continue
        target = normalize_page_path(raw)
        if not target:
            continue
        if is_external_target(target) and not include_external:
            continue
        targets.append(target)
    return targets
