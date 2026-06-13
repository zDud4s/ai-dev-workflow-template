from __future__ import annotations

import sys


try:
    from solution import reverse_words

    assert reverse_words("a b c") == "c b a"
except Exception:
    sys.exit(1)

sys.exit(0)
