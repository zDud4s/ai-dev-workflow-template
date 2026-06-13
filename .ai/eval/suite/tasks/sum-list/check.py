from __future__ import annotations

import sys


try:
    from solution import sum_list

    assert sum_list([1, 2, 3]) == 6
    assert sum_list([]) == 0
except Exception:
    sys.exit(1)

sys.exit(0)
