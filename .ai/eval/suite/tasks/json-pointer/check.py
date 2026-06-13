from __future__ import annotations

import sys


def _fail(msg: str) -> None:
    print(f"CHECK FAILED: {msg}")
    sys.exit(1)


try:
    from solution import json_pointer_get
except Exception as exc:  # noqa: BLE001
    _fail(f"could not import json_pointer_get: {exc!r}")

doc = {
    "a": {"b": 42},
    "arr": [10, 20, 30],
    "x/y": "slash",
    "m~n": "tilde",
}

cases = [
    ("", doc),
    ("/a", {"b": 42}),
    ("/a/b", 42),
    ("/arr", [10, 20, 30]),
    ("/arr/0", 10),
    ("/arr/2", 30),
    # escaping: ~1 decodes to '/', ~0 decodes to '~'
    ("/x~1y", "slash"),
    ("/m~0n", "tilde"),
]

for pointer, expected in cases:
    try:
        got = json_pointer_get(doc, pointer)
    except Exception as exc:  # noqa: BLE001
        _fail(f"json_pointer_get(doc, {pointer!r}) raised {exc!r}")
    if got != expected:
        _fail(f"json_pointer_get(doc, {pointer!r}) == {got!r}, expected {expected!r}")

sys.exit(0)
