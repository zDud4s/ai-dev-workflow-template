# Test catalog — the workflow's "central de testes"

The suite has ~1400 tests across 144 files. Running all of them on every edit is
slow and floods a phase's context with irrelevant output. This directory is a
**selector** that maps a working-tree diff to the *relevant* subset, so a phase
iterates fast — while a conservative gate still catches contract regressions.

It exists to give a phase three things at once: **speed** (run a subset),
**governance** (a curated map of what belongs together, not a blind guess), and
**less context noise** (read ~20 groups, not 1400 test names).

## How it decides — hybrid, fail-safe

The selector cross-references the `git diff` with a two-layer catalog:

| File | Source of truth | Edited by |
|---|---|---|
| `catalog.generated.yaml` | Auto-derived from disk | `select_tests.py --sync` — **never by hand** |
| `catalog.overlay.yaml` | Curated knobs | Humans, rarely |

**Generated layer (automatic, zero upkeep per test):**
- *Group membership* comes from the `test_<area>_*.py` filename convention. Add
  `test_jobs_batch9.py` and it joins the `jobs` group with no edit.
- *`covers`* (which source files a group is coupled to) comes from a **static
  scan** of each test's repo-relative path literals — both `"a/b.js"` single
  strings and segmented `"a" / "b" / "c.js"` joins — validated by file
  existence, so over-capture is harmless. No coverage run, so `--sync` is
  instant and deterministic.

**Overlay layer (curated, small):**
- `aliases` — fold area tokens into one group (`pty -> terminals`).
- `always` — groups that run at **every** gate because they guard scaffold-wide
  invariants (`structure`, `schema`, `dispatch`, …) that a diff can't be trusted
  to imply.
- `extra_covers` — couplings the scanner can't see (rare).

**Fail-safe:** a changed source file that maps to *no* group (and isn't inert)
forces the gate to fall back to the full suite. Unmapped change → run
everything, never a tiny subset. Safety beats speed when the map has a gap.

## Commands

```bash
python .ai/scripts/select_tests.py --list          # human summary of the selection
python .ai/scripts/select_tests.py --fast          # pytest cmd: touched groups only (iterate)
python .ai/scripts/select_tests.py --gate          # pytest cmd: touched + always (validation)
python .ai/scripts/select_tests.py --gate --run    # ...and run it instead of printing
python .ai/scripts/select_tests.py --gate --base main   # diff vs a ref, not HEAD
python .ai/scripts/select_tests.py --sync          # regenerate the catalog (after adding tests)
python .ai/scripts/select_tests.py --sync --check  # exit 1 if the catalog is stale
```

`--fast` is the inner loop (smallest subset). `--gate` is the **conservative
superset** = the groups your diff touched *plus* every `always` group; it
excludes `slow` integration tests so it stays fast.

## How it ties into the workflow gate (Rule 3 / Rule 6)

The executor's mandatory *Validation evidence* (Rule 3) uses the selector as
follows:

- **trivial / small:** `python .ai/scripts/select_tests.py --gate --run` is the
  accepted evidence. The conservative superset is the gate.
- **medium / large, or elevated risk:** run the **true full suite** (`pytest`,
  including `slow`). The selector still drives the fast inner loop while
  iterating, but the gate is the whole suite.
- **catalog out of sync:** the `catalog` group (always-on) carries a lockfile
  meta-test that fails the gate until you run `--sync`. Staleness can't hide.

## Maintenance

You touch `catalog.overlay.yaml` only when a genuinely **new area** appears
(new top-level `test_<area>_` prefix that should alias or be `always`). Adding
tests to an existing area needs nothing but `--sync` (and the meta-test reminds
you if you forget). The `maintenance` phase is a good place to run `--sync`.
