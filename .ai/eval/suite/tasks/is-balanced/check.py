from __future__ import annotations

import sys


def _fail(msg: str) -> None:
    print(f"CHECK FAILED: {msg}")
    sys.exit(1)


try:
    from solution import is_balanced
except Exception as exc:  # noqa: BLE001
    _fail(f"could not import is_balanced: {exc!r}")

cases = [
    ("", True),
    ("()", True),
    ("()[]{}", True),
    ("([])", True),
    ("a(b)c", True),  # non-bracket characters are ignored
    ("(", False),
    (")(", False),  # closer before opener
    ("([)]", False),  # correct counts but wrong nesting order
    ("(]", False),  # mismatched pair
]

for s, expected in cases:
    try:
        got = is_balanced(s)
    except Exception as exc:  # noqa: BLE001
        _fail(f"is_balanced({s!r}) raised {exc!r}")
    if got != expected:
        _fail(f"is_balanced({s!r}) == {got!r}, expected {expected!r}")

sys.exit(0)
