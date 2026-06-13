# Feature: LRU cache with TTL

Implement an `LRUCache` class in `src/lru.js` (CommonJS export `{ LRUCache }`).

Constructor: `new LRUCache({ capacity, ttlMs, now })`
- `capacity`: maximum number of live entries (required, > 0).
- `ttlMs`: optional. If set, an entry expires once `now() - <time it was last set> >= ttlMs`.
  If absent, entries never expire.
- `now`: optional function returning the current time in ms; defaults to `Date.now`.
  All time reads must go through it (so tests can inject a clock).

Methods:
- `set(key, value)`: insert or update. Marks the key most-recently-used and records
  the current time as its set-time. If this grows the cache beyond `capacity`, evict
  the least-recently-used entry.
- `get(key)`: return the value, or `undefined` if the key is missing or expired
  (remove it if expired). A successful `get` marks the key most-recently-used.
- `has(key)`: return `true`/`false`. Expired entries are removed and report `false`.
  `has` does NOT change recency.
- `peek(key)`: like `get` but does NOT change recency. Expired → `undefined`.
- `delete(key)`: remove the key.
- `size` (getter): the number of live (non-expired) entries.
- `keys()`: array of live keys ordered most-recently-used first, least-recently-used last.

Notes:
- `get` refreshes recency but NOT the set-time (TTL is measured from the last `set`).
- Updating an existing key via `set` refreshes both its recency and its set-time, and
  must not increase `size` beyond the live entry count.
