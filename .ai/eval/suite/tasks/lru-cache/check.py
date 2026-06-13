from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

TEST_JS = r"""
const test = require('node:test');
const assert = require('node:assert');
const { LRUCache } = require('../src/lru.js');

test('set then get returns the value', () => {
  const c = new LRUCache({ capacity: 2 });
  c.set('a', 1);
  assert.strictEqual(c.get('a'), 1);
  assert.strictEqual(c.get('missing'), undefined);
});

test('evicts the least-recently-used over capacity', () => {
  const c = new LRUCache({ capacity: 2 });
  c.set('a', 1); c.set('b', 2); c.set('c', 3);
  assert.strictEqual(c.get('a'), undefined);
  assert.strictEqual(c.get('b'), 2);
  assert.strictEqual(c.get('c'), 3);
});

test('get refreshes recency', () => {
  const c = new LRUCache({ capacity: 2 });
  c.set('a', 1); c.set('b', 2);
  c.get('a');           // a is now most-recently-used
  c.set('c', 3);        // should evict b, not a
  assert.strictEqual(c.get('a'), 1);
  assert.strictEqual(c.get('b'), undefined);
  assert.strictEqual(c.get('c'), 3);
});

test('has does not refresh recency', () => {
  const c = new LRUCache({ capacity: 2 });
  c.set('a', 1); c.set('b', 2);
  assert.strictEqual(c.has('a'), true);
  c.set('c', 3);        // a was NOT refreshed by has -> a is LRU -> evicted
  assert.strictEqual(c.has('a'), false);
  assert.strictEqual(c.get('b'), 2);
  assert.strictEqual(c.get('c'), 3);
});

test('peek does not refresh recency', () => {
  const c = new LRUCache({ capacity: 2 });
  c.set('a', 1); c.set('b', 2);
  assert.strictEqual(c.peek('a'), 1);
  c.set('c', 3);        // a was NOT refreshed by peek -> evicted
  assert.strictEqual(c.peek('a'), undefined);
});

test('delete removes the key', () => {
  const c = new LRUCache({ capacity: 2 });
  c.set('a', 1);
  c.delete('a');
  assert.strictEqual(c.get('a'), undefined);
  assert.strictEqual(c.size, 0);
});

test('size reflects live entries', () => {
  const c = new LRUCache({ capacity: 3 });
  c.set('a', 1); c.set('b', 2);
  assert.strictEqual(c.size, 2);
});

test('keys() are ordered most-recently-used first', () => {
  const c = new LRUCache({ capacity: 3 });
  c.set('a', 1); c.set('b', 2); c.set('c', 3);
  assert.deepStrictEqual(c.keys(), ['c', 'b', 'a']);
  c.get('a');
  assert.deepStrictEqual(c.keys(), ['a', 'c', 'b']);
});

test('updating an existing key does not grow size and refreshes recency', () => {
  const c = new LRUCache({ capacity: 2 });
  c.set('a', 1); c.set('b', 2);
  c.set('a', 11);       // update -> a is MRU, b is LRU
  c.set('c', 3);        // evict b
  assert.strictEqual(c.get('a'), 11);
  assert.strictEqual(c.get('b'), undefined);
  assert.strictEqual(c.get('c'), 3);
  assert.strictEqual(c.size, 2);
});

test('TTL expires entries using the injected clock', () => {
  const clock = { t: 0 };
  const c = new LRUCache({ capacity: 5, ttlMs: 100, now: () => clock.t });
  c.set('a', 1);
  clock.t = 50;
  assert.strictEqual(c.get('a'), 1);   // still live
  clock.t = 100;
  assert.strictEqual(c.get('a'), undefined);  // expired
  assert.strictEqual(c.size, 0);
});

test('without ttlMs entries do not expire', () => {
  const clock = { t: 0 };
  const c = new LRUCache({ capacity: 5, now: () => clock.t });
  c.set('a', 1);
  clock.t = 1e9;
  assert.strictEqual(c.get('a'), 1);
});
"""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - best-effort
        pass
    node = shutil.which("node")
    if node is None:
        print("CHECK FAILED: node not found on PATH")
        return 1
    tests_dir = Path("tests")
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "lru.test.js").write_text(TEST_JS, encoding="utf-8")
    completed = subprocess.run(
        [node, "--test", "tests/lru.test.js"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sys.stdout.write(completed.stdout[-4000:])
    return completed.returncode


raise SystemExit(main())
