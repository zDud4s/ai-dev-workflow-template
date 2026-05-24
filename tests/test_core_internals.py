"""Internal-helper hardening tests for .ai/dashboard/app/core.js.

Covers batch-3 fixes on top of the earlier hardening waves:
  - At least 8 entry-point null-guards (3 original + 4 batch-2 + 1+ new).
  - `_toastRoot` checks for an existing #toast-root BEFORE creating one
    (avoids dead/duplicate root creation in the normal page path).
  - `marked.setOptions` is wrapped in a `typeof marked` guard so a missing
    CDN script no longer aborts the rest of core.js at parse time.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"


def _src(name):
    return (APP / name).read_text(encoding="utf-8")


def _function_body(src, name):
    """Naively extract `function NAME(...) { ... }` body by brace counting."""
    # Match both `function name(` and `async function name(`.
    for marker in (
        "async function " + name + "(",
        "function " + name + "(",
    ):
        idx = src.find(marker)
        if idx != -1:
            break
    else:
        raise AssertionError("Could not find function " + name)
    brace_open = src.index("{", idx)
    depth = 0
    for i in range(brace_open, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace_open : i + 1]
    raise AssertionError("Could not locate body of " + name)


def test_core_has_at_least_8_null_guards():
    """The null-guard rollout proceeded in three batches. Batch 1 added
    3 entry-point guards (renderOverview / renderActivity / renderModels).
    Batch 2 added 4 more (loadTokenUsage / editPhaseRow / savePhaseRow /
    renderProject). Batch 3 adds further internal-helper guards. The total
    null-guard count should be at least 8.

    Two equivalent shapes count as guards here:
      - Inline:    `if (!$("#foo")) return`
      - Captured:  `const x = $("#foo"); if (!x) return`
    Several renderers were later refactored to the captured shape so a
    single lookup is reused for both the guard AND the assignment (avoids
    a null-deref race between two $() calls).
    """
    src = _src("core.js")
    inline_pattern = re.compile(r'if\s*\(\s*!\$\(["\'][^"\']+["\']\)\s*\)\s*return\b')
    inline_matches = inline_pattern.findall(src)

    # Captured pattern: `const VAR = $("#sel"); ... if (!VAR) return`.
    # Require both halves within a short window so an unrelated `if (!X) return`
    # elsewhere in the function doesn't get attributed to the lookup.
    captured_pattern = re.compile(
        r'const\s+(\w+)\s*=\s*\$\(["\'][^"\']+["\']\)\s*;\s*'
        r'(?:[^;{}]*;\s*){0,3}'  # tolerate up to a few intervening one-liners
        r'if\s*\(\s*!\s*\1\s*\)\s*return\b'
    )
    captured_matches = captured_pattern.findall(src)

    total = len(inline_matches) + len(captured_matches)
    assert total >= 8, (
        "core.js should have at least 8 null-guards (inline OR captured "
        "shape); found %d inline + %d captured = %d total" % (
            len(inline_matches), len(captured_matches), total,
        )
    )


def test_toast_root_check_before_create():
    """_toastRoot historically created the #toast-root <div> dynamically.
    index.html already declares <div id="toast-root">, so that branch was
    dead code in the normal path. The function must now check for an
    existing element FIRST and only fall back to createElement if missing.
    """
    src = _src("core.js")
    body = _function_body(src, "_toastRoot")

    # Locate the lookup (either via $() or getElementById) and the
    # createElement("div") call. The lookup must come before creation.
    lookup_match = re.search(
        r'\$\(\s*["\']#toast-root["\']\s*\)|getElementById\(\s*["\']toast-root["\']\s*\)',
        body,
    )
    assert lookup_match, (
        "_toastRoot should look up #toast-root via $() or getElementById "
        "before falling back to createElement"
    )
    create_match = re.search(r'document\.createElement\(\s*["\']div["\']\s*\)', body)
    assert create_match, (
        "_toastRoot should still createElement('div') as a fallback for "
        "unusual injection scenarios where the HTML omitted #toast-root"
    )
    assert lookup_match.start() < create_match.start(), (
        "_toastRoot must check for an existing #toast-root BEFORE calling "
        "document.createElement — otherwise the lookup is dead code"
    )


def test_marked_setoptions_guarded():
    """`marked.setOptions(...)` runs at script-parse time. If the CDN
    failed to deliver the `marked` library, accessing it throws
    synchronously and aborts the rest of core.js, leaving the dashboard
    blank. The call must be wrapped in a `typeof marked` (or equivalent)
    guard so a missing library degrades gracefully.
    """
    src = _src("core.js")
    # Find the actual CALL (followed by `(`), not a comment mention.
    setopts_match = re.search(r"marked\.setOptions\s*\(", src)
    assert setopts_match, "marked.setOptions(...) call not found in core.js"
    # Inspect the preface preceding the call for a guard. Allow generous
    # context so an explanatory comment block between the guard and the
    # call doesn't cause a false negative.
    preface = src[max(0, setopts_match.start() - 600) : setopts_match.start()]
    has_typeof_guard = "typeof marked" in preface
    has_truthy_guard = re.search(r"\bmarked\s*&&", preface) is not None
    assert has_typeof_guard or has_truthy_guard, (
        "marked.setOptions should be wrapped in a `typeof marked !== "
        "'undefined'` (or `marked &&`) guard so the script tolerates a "
        "missing CDN library"
    )
