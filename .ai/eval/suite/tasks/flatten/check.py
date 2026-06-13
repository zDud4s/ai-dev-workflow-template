from __future__ import annotations

import sys


def _fail(msg: str) -> None:
    print(f"CHECK FAILED: {msg}")
    sys.exit(1)


try:
    from solution import flatten
except Exception as exc:  # noqa: BLE001
    _fail(f"could not import flatten: {exc!r}")

cases = [
    ([1, 2, 3], [1, 2, 3]),
    ([1, [2, 3]], [1, 2, 3]),
    # arbitrary depth, not just one level
    ([1, [2, [3, [4]]]], [1, 2, 3, 4]),
    ([], []),
    ([1, [], [2, []]], [1, 2]),
    # strings are atomic, not descended into character-by-character
    (["ab", ["cd"]], ["ab", "cd"]),
]

for xs, expected in cases:
    try:
        got = flatten(xs)
    except Exception as exc:  # noqa: BLE001
        _fail(f"flatten({xs!r}) raised {exc!r}")
    if got != expected:
        _fail(f"flatten({xs!r}) == {got!r}, expected {expected!r}")

sys.exit(0)
