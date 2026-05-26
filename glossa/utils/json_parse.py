import json
import re
from typing import Any


class LLMJSONError(ValueError):
    """The LLM did not return parseable JSON."""


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def parse(text: str) -> Any:
    """Parse a JSON response from an LLM, tolerating common quirks.

    Handles:
      - leading / trailing whitespace
      - ```json fenced code blocks
      - extra prose around the JSON (extracts the outer {...} or [...])
    """
    candidate = text.strip()
    fenced = _FENCE_RE.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    extracted = _extract_outer_json(candidate)
    if extracted is None:
        raise LLMJSONError(f"Could not find JSON in LLM response: {text[:500]!r}")
    try:
        return json.loads(extracted)
    except json.JSONDecodeError as e:
        raise LLMJSONError(f"Invalid JSON in LLM response: {e}: {extracted[:500]!r}") from e


def _extract_outer_json(text: str) -> str | None:
    """Find the outermost balanced JSON object or array in text."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None
