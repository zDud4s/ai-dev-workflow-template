from __future__ import annotations

import sys


def _fail(msg: str) -> None:
    print(f"CHECK FAILED: {msg}")
    sys.exit(1)


try:
    from solution import compare_version
except Exception as exc:  # noqa: BLE001
    _fail(f"could not import compare_version: {exc!r}")

cases = [
    ("1.0", "1.0", 0),
    ("1.0", "1.1", -1),
    ("1.1", "1.0", 1),
    # numeric-not-lexicographic: 10 > 9 even though "10" < "9" as strings
    ("1.10", "1.9", 1),
    ("1.9", "1.10", -1),
    # trailing zero components are equal: "1.0" == "1.0.0"
    ("1.0", "1.0.0", 0),
    ("1.0.1", "1.0", 1),
]

for a, b, expected in cases:
    try:
        got = compare_version(a, b)
    except Exception as exc:  # noqa: BLE001
        _fail(f"compare_version({a!r}, {b!r}) raised {exc!r}")
    if got != expected:
        _fail(f"compare_version({a!r}, {b!r}) == {got!r}, expected {expected!r}")

sys.exit(0)
