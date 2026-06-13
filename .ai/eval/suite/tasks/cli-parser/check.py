from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

TEST_JS = r"""
const test = require('node:test');
const assert = require('node:assert');
const { parseArgs } = require('../src/parser.js');

test('boolean flag sets true', () => {
  const r = parseArgs(['--verbose'], { verbose: { type: 'boolean' } });
  assert.strictEqual(r.options.verbose, true);
});

test('--no- prefix sets false', () => {
  const r = parseArgs(['--no-verbose'], { verbose: { type: 'boolean' } });
  assert.strictEqual(r.options.verbose, false);
});

test('string option with space', () => {
  const r = parseArgs(['--name', 'bob'], { name: { type: 'string' } });
  assert.strictEqual(r.options.name, 'bob');
});

test('string option with equals', () => {
  const r = parseArgs(['--name=bob'], { name: { type: 'string' } });
  assert.strictEqual(r.options.name, 'bob');
});

test('number option is coerced to a Number', () => {
  const r = parseArgs(['--port', '8080'], { port: { type: 'number' } });
  assert.strictEqual(r.options.port, 8080);
});

test('invalid number throws', () => {
  assert.throws(() => parseArgs(['--port', 'x'], { port: { type: 'number' } }));
});

test('short alias resolves to its option', () => {
  const r = parseArgs(['-v'], { verbose: { type: 'boolean', alias: 'v' } });
  assert.strictEqual(r.options.verbose, true);
});

test('short alias takes the next token for string/number', () => {
  const r = parseArgs(['-p', '8080'], { port: { type: 'number', alias: 'p' } });
  assert.strictEqual(r.options.port, 8080);
});

test('positionals are collected in order', () => {
  const r = parseArgs(['a', 'b'], {});
  assert.deepStrictEqual(r.positionals, ['a', 'b']);
});

test('-- terminates option parsing', () => {
  const r = parseArgs(['--name', 'x', '--', '--y'], { name: { type: 'string' } });
  assert.strictEqual(r.options.name, 'x');
  assert.deepStrictEqual(r.positionals, ['--y']);
});

test('unknown long option throws', () => {
  assert.throws(() => parseArgs(['--bogus'], {}));
});

test('default applied when absent', () => {
  const r = parseArgs([], { mode: { type: 'string', default: 'fast' } });
  assert.strictEqual(r.options.mode, 'fast');
});

test('missing required throws', () => {
  assert.throws(() => parseArgs([], { name: { type: 'string', required: true } }));
});

test('mixed flags, options and positionals', () => {
  const spec = {
    verbose: { type: 'boolean', alias: 'v' },
    name: { type: 'string' },
  };
  const r = parseArgs(['-v', '--name=bob', 'file.txt'], spec);
  assert.strictEqual(r.options.verbose, true);
  assert.strictEqual(r.options.name, 'bob');
  assert.deepStrictEqual(r.positionals, ['file.txt']);
});
"""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - best-effort; older/odd stdout objects
        pass
    node = shutil.which("node")
    if node is None:
        print("CHECK FAILED: node not found on PATH")
        return 1
    tests_dir = Path("tests")
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "parser.test.js").write_text(TEST_JS, encoding="utf-8")
    completed = subprocess.run(
        [node, "--test", "tests/parser.test.js"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sys.stdout.write(completed.stdout[-4000:])
    return completed.returncode


raise SystemExit(main())
