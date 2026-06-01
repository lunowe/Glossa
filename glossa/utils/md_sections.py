"""Surgical, token-cheap operations on the body of a markdown page.

Operates on the *body* — the part after YAML frontmatter, which is handled
separately by :mod:`glossa.utils.frontmatter`.  ATX headings only; ``#`` lines
inside fenced code-block spans (``` … ```) are ignored.

Public API
----------
INTRO               sentinel heading for content before the first ATX heading
SectionError        base exception
SectionNotFound     heading text not found
AmbiguousSection    heading text matches more than one section
SubstringNotFound   old string not present in text
AmbiguousSubstring  old string occurs more than once

outline(body)                          → list[dict]
get_section(body, heading)             → str
replace_section(body, heading, new)    → str
add_section(body, heading, content, *, after=None) → str
remove_section(body, heading)          → str
replace_substring(text, old, new)      → str
"""

import re

# ---------------------------------------------------------------------------
# Sentinel & exceptions
# ---------------------------------------------------------------------------

INTRO: str = ""  # sentinel for content before the first ATX heading


class SectionError(ValueError):
    """Base class for md_sections errors."""


class SectionNotFound(SectionError):  # noqa: N818
    """Raised when a heading is not found in the document."""


class AmbiguousSection(SectionError):  # noqa: N818
    """Raised when a heading text matches more than one section."""


class SubstringNotFound(SectionError):  # noqa: N818
    """Raised when the target substring is not present in the text."""


class AmbiguousSubstring(SectionError):  # noqa: N818
    """Raised when the target substring occurs more than once in the text."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_FENCE_RE = re.compile(r"^```", re.MULTILINE)


def _fenced_ranges(body: str) -> list[tuple[int, int]]:
    """Return a list of (start, end) char ranges that are inside fenced blocks.

    ``start`` is the position of the opening `` ``` `` line's first char;
    ``end`` is the position just after the closing `` ``` `` line's newline
    (or end-of-string if the fence is unclosed).
    """
    ranges: list[tuple[int, int]] = []
    fence_starts: list[int] = [m.start() for m in _FENCE_RE.finditer(body)]
    i = 0
    while i + 1 < len(fence_starts):
        ranges.append((fence_starts[i], fence_starts[i + 1] + 3))
        i += 2
    # unclosed fence: treat rest of document as fenced
    if len(fence_starts) % 2 == 1:
        ranges.append((fence_starts[-1], len(body)))
    return ranges


def _in_fence(pos: int, fenced: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in fenced)


def _parse_headings(body: str) -> list[tuple[int, int, str]]:
    """Return list of (match_start, level, heading_text) for real headings."""
    fenced = _fenced_ranges(body)
    results: list[tuple[int, int, str]] = []
    for m in _HEADING_RE.finditer(body):
        if not _in_fence(m.start(), fenced):
            results.append((m.start(), len(m.group(1)), m.group(2).strip()))
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def outline(body: str) -> list[dict]:
    """Return one dict per section in document order.

    Each dict has::

        {
            "heading":   str,           # "" (INTRO) or heading text without '#'s
            "level":     int,           # 0 for INTRO, 1-6 for ATX headings
            "char_span": tuple[int, int],  # (start, end) offsets into body
        }

    ``char_span`` covers the heading line through just before the next heading
    of same-or-higher level (i.e. the section includes its nested subsections).
    All spans tile the body exactly: concatenating body[s:e] for every entry
    reproduces body.
    """
    headings = _parse_headings(body)

    # Build raw section boundaries (start of each heading line, level, text)
    # Prepend a synthetic INTRO entry if there is content before the first heading.
    entries: list[tuple[int, int, str]] = []  # (start, level, text)
    if not headings or headings[0][0] > 0:
        entries.append((0, 0, INTRO))
    for start, level, text in headings:
        entries.append((start, level, text))

    sections: list[dict] = []
    for i, (start, level, text) in enumerate(entries):
        # Find end: next heading at same or higher level (lower number)
        end = len(body)
        for j in range(i + 1, len(entries)):
            next_level = entries[j][1]
            # INTRO (level 0) is only ended by EOF conceptually; but if a
            # heading follows at any level it ends the intro.
            if level == 0 or next_level <= level:
                end = entries[j][0]
                break
        sections.append({"heading": text, "level": level, "char_span": (start, end)})
    return sections


def _find_section(body: str, heading: str) -> dict:
    """Return the unique outline entry for *heading*; raise on not-found/ambiguous."""
    matches = [s for s in outline(body) if s["heading"] == heading]
    if not matches:
        raise SectionNotFound(f"Section not found: {heading!r}")
    if len(matches) > 1:
        raise AmbiguousSection(f"Section heading is ambiguous (occurs {len(matches)} times): {heading!r}")
    return matches[0]


def get_section(body: str, heading: str) -> str:
    """Return the section text (including its heading line) for the unique matching section.

    Raises :exc:`SectionNotFound` or :exc:`AmbiguousSection`.
    """
    section = _find_section(body, heading)
    start, end = section["char_span"]
    return body[start:end]


def replace_section(body: str, heading: str, new_section_markdown: str) -> str:
    """Replace the matched section's full span with *new_section_markdown*.

    Raises :exc:`SectionNotFound` or :exc:`AmbiguousSection`.
    Returns the new body.
    """
    section = _find_section(body, heading)
    start, end = section["char_span"]
    return body[:start] + new_section_markdown + body[end:]


def add_section(body: str, heading: str, content: str, *, after: str | None = None) -> str:
    """Append a new section to *body*.

    Parameters
    ----------
    body:
        Existing markdown body.
    heading:
        Plain heading text (or a full ATX heading if it already starts with ``#``).
        Rendered as ``## {heading}`` unless it already starts with ``#``.
    content:
        Markdown body beneath the heading (should NOT include the heading line).
    after:
        If given, insert immediately after that section's span.  Raises
        :exc:`SectionNotFound` or :exc:`AmbiguousSection` if *after* is
        missing or ambiguous.  If ``None``, append at the end of *body*.

    Returns the new body.
    """
    heading_line = heading if heading.startswith("#") else f"## {heading}"
    new_block = f"{heading_line}\n{content}"

    if after is not None:
        section = _find_section(body, after)
        insert_at = section["char_span"][1]
        # ensure we don't squash adjacent content without a newline boundary
        separator = "" if (not body[:insert_at] or body[insert_at - 1] == "\n") else "\n"
        return body[:insert_at] + separator + new_block + body[insert_at:]

    # append
    separator = "" if (not body or body[-1] == "\n") else "\n"
    return body + separator + new_block


def remove_section(body: str, heading: str) -> str:
    """Remove the matched section entirely.

    Raises :exc:`SectionNotFound` or :exc:`AmbiguousSection`.
    Returns the new body.
    """
    section = _find_section(body, heading)
    start, end = section["char_span"]
    return body[:start] + body[end:]


def replace_substring(text: str, old: str, new: str) -> str:
    """Exact, unique substring replacement.

    Raises :exc:`SubstringNotFound` if *old* is not present, or
    :exc:`AmbiguousSubstring` if it occurs more than once.
    Returns the new text.
    """
    count = text.count(old)
    if count == 0:
        raise SubstringNotFound(f"Substring not found: {old!r}")
    if count > 1:
        raise AmbiguousSubstring(f"Substring occurs {count} times: {old!r}")
    return text.replace(old, new, 1)
