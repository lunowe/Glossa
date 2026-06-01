"""Tests for glossa.utils.md_sections."""

import pytest

from glossa.utils.md_sections import (
    INTRO,
    AmbiguousSection,
    AmbiguousSubstring,
    SectionNotFound,
    SubstringNotFound,
    add_section,
    get_section,
    outline,
    remove_section,
    replace_section,
    replace_substring,
)

# ---------------------------------------------------------------------------
# Fixtures / shared bodies
# ---------------------------------------------------------------------------

MULTI = """\
Intro text here.

## Alpha

Alpha content.

### Alpha Sub

Sub content.

## Beta

Beta content.
"""

DUPE = """\
## Alpha

First.

## Alpha

Second.
"""

FENCED = """\
Some text.

```
# This is NOT a heading
## Also not
```

## Real Heading

Content.
"""

ONLY_INTRO = "No headings at all.\n"


# ---------------------------------------------------------------------------
# outline()
# ---------------------------------------------------------------------------


class TestOutline:
    def test_intro_present(self):
        secs = outline(MULTI)
        assert secs[0]["heading"] == INTRO
        assert secs[0]["level"] == 0

    def test_section_count(self):
        # intro + Alpha + Alpha Sub + Beta
        secs = outline(MULTI)
        assert len(secs) == 4

    def test_headings_and_levels(self):
        secs = outline(MULTI)
        headings = [(s["heading"], s["level"]) for s in secs]
        assert ("Alpha", 2) in headings
        assert ("Alpha Sub", 3) in headings
        assert ("Beta", 2) in headings

    def test_intro_only_body(self):
        secs = outline(ONLY_INTRO)
        assert len(secs) == 1
        assert secs[0]["heading"] == INTRO
        assert secs[0]["char_span"] == (0, len(ONLY_INTRO))

    def test_no_intro_when_heading_first(self):
        body = "## First\n\nContent.\n"
        secs = outline(body)
        assert secs[0]["heading"] == "First"

    def test_fenced_code_not_heading(self):
        secs = outline(FENCED)
        headings = [s["heading"] for s in secs]
        assert "This is NOT a heading" not in headings
        assert "Also not" not in headings
        assert "Real Heading" in headings

    def test_round_trip_reconstruction(self):
        """Replacing every section with its own content leaves the body unchanged."""
        for body in (MULTI, FENCED, ONLY_INTRO):
            secs = outline(body)
            # For each unique heading (skip DUPE-style docs), round-trip via replace_section.
            for sec in secs:
                h = sec["heading"]
                headings_matching = [s for s in secs if s["heading"] == h]
                if len(headings_matching) > 1:
                    continue  # ambiguous, skip
                current = body[sec["char_span"][0] : sec["char_span"][1]]
                result = replace_section(body, h, current)
                assert result == body, f"Round-trip failed for heading={h!r}"

    def test_spans_tile_top_level(self):
        """The outermost (non-nested) section spans tile the body without gaps or overlap."""
        for body in (MULTI, FENCED, ONLY_INTRO):
            secs = outline(body)
            # A section is "top-level" if its start is not inside any other section's span
            # from a section that started earlier and is at a strictly higher level (lower number).
            top: list[dict] = []
            for sec in secs:
                dominated = False
                for other in top:
                    os, oe = other["char_span"]
                    if os <= sec["char_span"][0] < oe and other["level"] < sec["level"]:
                        dominated = True
                        break
                if not dominated:
                    top.append(sec)
            reconstructed = "".join(body[s["char_span"][0] : s["char_span"][1]] for s in top)
            assert reconstructed == body, f"Tiling failed for body starting: {body[:40]!r}"

    def test_parent_span_includes_subsection(self):
        secs = outline(MULTI)
        alpha = next(s for s in secs if s["heading"] == "Alpha")
        sub = next(s for s in secs if s["heading"] == "Alpha Sub")
        # Alpha's span must contain Alpha Sub's span entirely
        assert alpha["char_span"][0] <= sub["char_span"][0]
        assert alpha["char_span"][1] >= sub["char_span"][1]

    def test_h1_through_h6(self):
        body = "# H1\n\n## H2\n\n### H3\n\n#### H4\n\n##### H5\n\n###### H6\n"
        secs = outline(body)
        levels = [s["level"] for s in secs]
        assert levels == [1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------------------
# get_section()
# ---------------------------------------------------------------------------


class TestGetSection:
    def test_returns_heading_and_content(self):
        text = get_section(MULTI, "Alpha")
        assert text.startswith("## Alpha\n")
        assert "Alpha Sub" in text  # subsection included

    def test_intro_via_sentinel(self):
        text = get_section(MULTI, INTRO)
        assert "Intro text here" in text
        assert "## Alpha" not in text

    def test_not_found(self):
        with pytest.raises(SectionNotFound):
            get_section(MULTI, "Nonexistent")

    def test_ambiguous(self):
        with pytest.raises(AmbiguousSection):
            get_section(DUPE, "Alpha")

    def test_nested_subsection(self):
        text = get_section(MULTI, "Alpha Sub")
        assert "Sub content" in text
        assert "Beta" not in text


# ---------------------------------------------------------------------------
# replace_section()
# ---------------------------------------------------------------------------


class TestReplaceSection:
    def test_round_trip(self):
        original = get_section(MULTI, "Beta")
        result = replace_section(MULTI, "Beta", original)
        assert result == MULTI

    def test_replaces_content(self):
        new_md = "## Beta\n\nNew beta content.\n"
        result = replace_section(MULTI, "Beta", new_md)
        assert "New beta content" in result
        assert "Beta content" not in result

    def test_not_found(self):
        with pytest.raises(SectionNotFound):
            replace_section(MULTI, "Missing", "## Missing\n\n")

    def test_ambiguous(self):
        with pytest.raises(AmbiguousSection):
            replace_section(DUPE, "Alpha", "## Alpha\n\nNew.\n")

    def test_replaces_whole_parent_span(self):
        # Replacing Alpha should remove Alpha Sub too
        new_md = "## Alpha\n\nSimplified.\n"
        result = replace_section(MULTI, "Alpha", new_md)
        assert "Alpha Sub" not in result
        assert "Beta" in result


# ---------------------------------------------------------------------------
# add_section()
# ---------------------------------------------------------------------------


class TestAddSection:
    def test_append_at_end(self):
        result = add_section(MULTI, "Gamma", "Gamma content.\n")
        assert result.endswith("## Gamma\nGamma content.\n")

    def test_insert_after(self):
        result = add_section(MULTI, "Inserted", "Hello.\n", after="Alpha")
        secs = outline(result)
        headings = [s["heading"] for s in secs]
        inserted_idx = headings.index("Inserted")
        alpha_idx = headings.index("Alpha")
        assert inserted_idx == alpha_idx + 1 or inserted_idx > alpha_idx

    def test_after_not_found(self):
        with pytest.raises(SectionNotFound):
            add_section(MULTI, "X", "content.\n", after="Missing")

    def test_after_ambiguous(self):
        with pytest.raises(AmbiguousSection):
            add_section(DUPE, "X", "content.\n", after="Alpha")

    def test_heading_with_hash_preserved(self):
        result = add_section(MULTI, "### Custom Level", "Body.\n")
        assert "### Custom Level\n" in result

    def test_plain_heading_gets_h2(self):
        result = add_section(MULTI, "NewSection", "Body.\n")
        assert "## NewSection\n" in result


# ---------------------------------------------------------------------------
# remove_section()
# ---------------------------------------------------------------------------


class TestRemoveSection:
    def test_removes_section(self):
        result = remove_section(MULTI, "Beta")
        assert "Beta content" not in result
        assert "Alpha" in result

    def test_removes_parent_and_children(self):
        result = remove_section(MULTI, "Alpha")
        assert "Alpha Sub" not in result
        assert "Sub content" not in result
        assert "Beta" in result

    def test_not_found(self):
        with pytest.raises(SectionNotFound):
            remove_section(MULTI, "Ghost")

    def test_ambiguous(self):
        with pytest.raises(AmbiguousSection):
            remove_section(DUPE, "Alpha")

    def test_remove_intro(self):
        result = remove_section(MULTI, INTRO)
        assert "Intro text here" not in result
        assert "## Alpha" in result


# ---------------------------------------------------------------------------
# replace_substring()
# ---------------------------------------------------------------------------


class TestReplaceSubstring:
    def test_basic_replace(self):
        assert replace_substring("hello world", "world", "there") == "hello there"

    def test_not_found(self):
        with pytest.raises(SubstringNotFound):
            replace_substring("hello world", "missing", "x")

    def test_ambiguous(self):
        with pytest.raises(AmbiguousSubstring):
            replace_substring("aaa", "a", "b")

    def test_unique_multi_char(self):
        text = "foo bar baz"
        assert replace_substring(text, "bar", "qux") == "foo qux baz"

    def test_empty_old_raises(self):
        # empty string is in every string infinitely — count("") > 1 for any non-trivial string
        with pytest.raises(AmbiguousSubstring):
            replace_substring("hello", "", "x")
