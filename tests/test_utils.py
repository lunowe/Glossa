import pytest

from glossa.utils import frontmatter
from glossa.utils.json_parse import LLMJSONError, parse
from glossa.utils.slug import slugify


class TestFrontmatter:
    def test_round_trip(self):
        fm = {"kind": "entity", "title": "Allianz", "source_refs": ["a", "b"]}
        body = "# Allianz\n\nHello.\n"
        rendered = frontmatter.serialize(fm, body)
        parsed_fm, parsed_body = frontmatter.parse(rendered)
        assert parsed_fm == fm
        assert parsed_body.strip() == body.strip()

    def test_parse_no_frontmatter(self):
        text = "# Just a heading\n\nBody.\n"
        fm, body = frontmatter.parse(text)
        assert fm == {}
        assert body == text

    def test_parse_malformed_yaml_falls_back(self):
        text = "---\nthis is not: : valid: yaml\n---\nbody"
        fm, body = frontmatter.parse(text)
        assert fm == {}
        assert body == text


class TestJsonParse:
    def test_plain_object(self):
        assert parse('{"a": 1}') == {"a": 1}

    def test_with_fence(self):
        assert parse('```json\n{"a": 1}\n```') == {"a": 1}

    def test_with_plain_fence(self):
        assert parse('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_around_object(self):
        text = 'Sure, here is the answer:\n{"answer": "yes"}\nThat is correct.'
        assert parse(text) == {"answer": "yes"}

    def test_prose_around_array(self):
        text = "First:\n[1, 2, 3]\nDone."
        assert parse(text) == [1, 2, 3]

    def test_invalid_raises(self):
        with pytest.raises(LLMJSONError):
            parse("this is not json at all")


class TestSlugify:
    def test_basic(self):
        assert slugify("Hello World") == "hello-world"

    def test_german_umlauts(self):
        assert slugify("Allianz Österreich") == "allianz-oesterreich"
        assert slugify("Müller GmbH") == "mueller-gmbh"
        assert slugify("Spaß") == "spass"

    def test_collapses_dashes(self):
        assert slugify("foo -- bar   baz") == "foo-bar-baz"

    def test_empty(self):
        assert slugify("") == "untitled"
        assert slugify("---") == "untitled"

    def test_max_length(self):
        result = slugify("a" * 200, max_length=20)
        assert len(result) <= 20
