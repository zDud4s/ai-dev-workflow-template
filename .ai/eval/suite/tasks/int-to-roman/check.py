from __future__ import annotations

import sys


def _fail(msg: str) -> None:
    print(f"CHECK FAILED: {msg}")
    sys.exit(1)


try:
    from solution import int_to_roman
except Exception as exc:  # noqa: BLE001
    _fail(f"could not import int_to_roman: {exc!r}")

cases = [
    (1, "I"),
    (3, "III"),
    (4, "IV"),
    (9, "IX"),
    (40, "XL"),
    (58, "LVIII"),
    (90, "XC"),
    (400, "CD"),
    (444, "CDXLIV"),
    (900, "CM"),
    (1994, "MCMXCIV"),
    (2023, "MMXXIII"),
    (3999, "MMMCMXCIX"),
]

for n, expected in cases:
    try:
        got = int_to_roman(n)
    except Exception as exc:  # noqa: BLE001
        _fail(f"int_to_roman({n}) raised {exc!r}")
    if got != expected:
        _fail(f"int_to_roman({n}) == {got!r}, expected {expected!r}")

sys.exit(0)
