"""Shared parsing of free-form LLM stdout into structured data.

Neutral home (depended on by both the improver and agent-suggestion domains)
for ``_parse_improver_output`` — a robust "extract one JSON object from a
chatty model response" helper. Kept here, rather than in either domain module,
so neither has to import the other.
"""
from __future__ import annotations

import json
import re


def _parse_improver_output(stdout: str) -> dict | None:
    """Robustly extract one JSON object from the model's free-form output.

    Handles three common shapes:
      1. ``stdout`` IS JSON (no prose around it)
      2. Fenced block: ```` ```json ... ``` ````
      3. JSON embedded in prose — scanned with a brace counter that
         respects strings + backslash escapes, so ``{`` characters inside
         JSON string values don't confuse the search.

    Returns ``None`` if no parseable object is found."""
    if not stdout:
        return None
    s = stdout.strip()

    # 1. Whole output IS JSON.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Fenced block from a chatty model.
    fence = re.search(r"```(?:json)?\s*\n?(.+?)\n?```", s, re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. String-aware brace counter; tries each top-level ``{...}`` slice.
    n = len(s)
    i = 0
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        depth = 0
        j = i
        in_str = False
        escape = False
        while j < n:
            c = s[j]
            if escape:
                escape = False
            elif c == "\\" and in_str:
                escape = True
            elif c == '"':
                in_str = not in_str
            elif not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(s[i:j + 1])
                            if isinstance(obj, dict):
                                return obj
                        except (json.JSONDecodeError, ValueError):
                            pass
                        break
            j += 1
        i += 1
    return None
