A starter file `store.py` already exists with a working `KVStore` class
(`get`, `set`, `delete`, `keys`). Extend the project as follows, WITHOUT changing
the behavior of the existing methods:

1. In `store.py`, add to `KVStore`:
   - `incr(key, by=1)`: treat a missing key as 0, add `by`, store the result, and
     return the new integer value.
   - `save(path)`: write all items to `path` as JSON.
   - `load(path)`: replace all items with the JSON object read from `path`.

2. Create a new module `backup.py` exposing:
   - `backup(store, path)`: persist the given store's items to `path` as JSON.
   - `restore(path)`: return a NEW `KVStore` whose items are loaded from `path`.

`backup.py` must reuse `KVStore` from `store.py` (import it), not reimplement it.
