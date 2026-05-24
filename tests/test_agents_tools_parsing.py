from __future__ import annotations

import json
import re


def _parse_agent_tools(raw: str) -> list[str]:
    items = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            items = parsed
    except json.JSONDecodeError:
        items = None
    if items is None:
        items = re.split(r"\s*,\s*", raw or "")
    return [
        re.sub(r"""^[\s\[\]"']+|[\s\[\]"']+$""", "", str(item))
        for item in items
        if re.sub(r"""^[\s\[\]"']+|[\s\[\]"']+$""", "", str(item))
    ]


def test_json_array_string():
    assert _parse_agent_tools('["Read", "Grep"]') == ["Read", "Grep"]


def test_comma_string_fallback():
    assert _parse_agent_tools("Read, Grep") == ["Read", "Grep"]
