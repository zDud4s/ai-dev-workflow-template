from __future__ import annotations

import sys


def _fail(msg: str) -> None:
    print(f"CHECK FAILED: {msg}")
    sys.exit(1)


try:
    from solution import roman_to_int
except Exception as exc:  # noqa: BLE001
    _fail(f"could not import roman_to_int: {exc!r}")

cases = [
    ("III", 3),
    ("LVIII", 58),
    # subtractive notation: IV is 4 (not 6), IX is 9
    ("IV", 4),
    ("IX", 9),
    ("XL", 40),
    ("XC", 90),
    ("CD", 400),
    ("CM", 900),
    ("MCMXCIV", 1994),
]

for s, expected in cases:
    try:
        got = roman_to_int(s)
    except Exception as exc:  # noqa: BLE001
        _fail(f"roman_to_int({s!r}) raised {exc!r}")
    if got != expected:
        _fail(f"roman_to_int({s!r}) == {got!r}, expected {expected!r}")

sys.exit(0)
