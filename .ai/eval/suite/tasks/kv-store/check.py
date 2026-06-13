from __future__ import annotations

import sys


def _fail(msg: str) -> None:
    print(f"CHECK FAILED: {msg}")
    sys.exit(1)


try:
    from store import KVStore
except Exception as exc:  # noqa: BLE001
    _fail(f"could not import KVStore from store.py: {exc!r}")

# --- regression: existing behavior must be preserved ---
s = KVStore()
s.set("a", 1)
s.set("b", 2)
if s.get("a") != 1:
    _fail("regression: get/set broke (expected get('a') == 1)")
if s.keys() != ["a", "b"]:
    _fail(f"regression: keys() should be sorted ['a','b'], got {s.keys()!r}")
s.delete("a")
if s.get("a") is not None or s.keys() != ["b"]:
    _fail("regression: delete broke")

# --- new: incr ---
try:
    if s.incr("counter") != 1:
        _fail("incr on missing key should return 1")
    if s.incr("counter", by=5) != 6:
        _fail("incr by=5 should return 6")
except AttributeError as exc:
    _fail(f"incr not implemented: {exc!r}")

# --- new: save/load round trip ---
try:
    s.save("kv.json")
    s2 = KVStore()
    s2.load("kv.json")
    if s2.get("counter") != 6 or s2.get("b") != 2:
        _fail(f"save/load round trip lost data: keys={s2.keys()!r}")
except AttributeError as exc:
    _fail(f"save/load not implemented: {exc!r}")

# --- new: backup.py module reusing KVStore ---
try:
    from backup import backup, restore
except Exception as exc:  # noqa: BLE001
    _fail(f"could not import backup/restore from backup.py: {exc!r}")

try:
    backup(s, "backup.json")
    restored = restore("backup.json")
except Exception as exc:  # noqa: BLE001
    _fail(f"backup/restore raised {exc!r}")

if not isinstance(restored, KVStore):
    _fail("restore() must return a KVStore instance")
if restored.get("counter") != 6 or restored.get("b") != 2:
    _fail(f"backup/restore lost data: keys={restored.keys()!r}")

sys.exit(0)
