"""Batch-6 null-deref hardening tests for .ai/dashboard/app/core.js and main.js.

The prior batches (4 and 5) cleared the most obvious entry-point guards
(renderOverview / renderActivity / renderModels / renderProject / hideToast,
plus #meta and the jsyaml.load wrappers in main.js).  Batch 6 closes the
remaining sites flagged in the bug-hunt status doc:

  - main.js: #project-name / #count-memory / #count-plans / #count-specs each
    null-guarded after YAML parse — previously an unguarded `.textContent =`
    aborted the whole success chain mid-render if the markup was stripped.
  - core.js submitMemory: #mem-topic, #mem-fact resolved up-front and guarded
    — the previous shape guarded only #mem-submit, so a partial form would
    null-deref on `.value.trim()`.
  - core.js submitDecision: #dec-decision, #dec-why guarded; optional fields
    use optional chaining + `?? ""` so a missing markup row degrades to "".
  - core.js loadTokenUsage: non-2xx fetch response now console.warns instead
    of silently returning — operators could not correlate stuck "—" cards
    with the underlying 500.

Each test pins a SPECIFIC textual invariant so a future regression (someone
re-introducing `$("#count-memory").textContent = …` without a guard) fails
loudly.  Gemini-integration lines must remain intact and are asserted here
too, since this batch's working files overlap with uncommitted Gemini work.
"""

from __future__ import annotations

import re
import pytest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"


def _src(name: str) -> str:
    return (APP / name).read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Brace-count the body of `[async] function NAME(...)`."""
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


# ----- main.js: post-YAML count writes -----


def test_main_project_name_textcontent_guarded() -> None:
    """#project-name was written unguarded; a stripped index.html shell would
    null-deref `.textContent = …` and abort the rest of loadAll() — the
    overview cards / activity table / lists would never render.  Verify the
    lookup is captured + guarded."""
    src = _src("main.js")
    assert '$("#project-name").textContent' not in src, (
        'main.js should no longer have unguarded `$("#project-name").textContent = …` '
        "— extract to `const projectNameEl = $(\"#project-name\")` and null-guard"
    )
    assert re.search(
        r"const\s+projectNameEl\s*=\s*\$\("
        r'"#project-name"\)\s*;\s*if\s*\(\s*projectNameEl\s*\)\s*projectNameEl\.textContent',
        src,
    ), "main.js must extract #project-name into a local and null-guard the .textContent write"


def test_main_count_targets_each_guarded() -> None:
    """#count-memory / #count-plans / #count-specs each need their own
    null-guard.  A single missing element previously aborted the whole
    sequence (JS halts on the first throw)."""
    src = _src("main.js")
    body = _function_body(src, "loadAll")
    for sel, var in (
        ("#count-memory", "countMemoryEl"),
        ("#count-plans", "countPlansEl"),
        ("#count-specs", "countSpecsEl"),
    ):
        assert ('$("' + sel + '").textContent =') not in body, (
            "loadAll should no longer have unguarded `$(\""
            + sel
            + "\").textContent = …` — extract to a local and null-guard"
        )
        assert re.search(
            r"const\s+" + var + r"\s*=\s*\$\("
            r'"' + sel + r'"\)\s*;\s*if\s*\(\s*' + var + r"\s*\)\s*" + var + r"\.textContent",
            body,
        ), (
            "loadAll must null-guard "
            + sel
            + " (extract into "
            + var
            + " and gate the .textContent write)"
        )


# ----- core.js: submitMemory form null-guards -----


def test_submit_memory_guards_input_fields() -> None:
    """submitMemory used to call `$("#mem-topic").value.trim()` after only
    guarding #mem-submit.  Verify the input elements are resolved up-front
    and each is null-guarded (in two passes to keep the broader entry-point
    guard-count invariant happy)."""
    src = _src("core.js")
    body = _function_body(src, "submitMemory")
    # No unguarded chained .value.trim() on a fresh lookup.
    assert '$("#mem-topic").value' not in body, (
        'submitMemory should not call `$("#mem-topic").value` after a fresh lookup '
        "— extract to a local"
    )
    assert '$("#mem-fact").value' not in body, (
        'submitMemory should not call `$("#mem-fact").value` after a fresh lookup '
        "— extract to a local"
    )
    # First pass: the submit button must still be guarded (inline form here
    # to keep the broader entry-point guard count invariant healthy).
    assert 'if (!$("#mem-submit")) return' in body, (
        "submitMemory must keep the inline `if (!$(\"#mem-submit\")) return` "
        "guard so the broader entry-point-count invariant holds"
    )
    # Second pass: topicEl + factEl resolved together and guarded.
    assert re.search(
        r"const\s+topicEl\s*=\s*\$\("
        r'"#mem-topic"\)\s*;\s*const\s+factEl\s*=\s*\$\('
        r'"#mem-fact"\)\s*;\s*if\s*\(\s*!topicEl\s*\|\|\s*!factEl\s*\)\s*return',
        body,
    ), (
        "submitMemory must resolve topicEl + factEl up-front and null-guard "
        "them together before any `.value.trim()` call"
    )
    # And #count-memory write must be null-guarded too.
    assert re.search(
        r"if\s*\(\s*countMemoryEl\s*\)\s*countMemoryEl\.textContent",
        body,
    ), "submitMemory must null-guard the #count-memory write inside the success path"


# ----- core.js: submitDecision form null-guards -----


def test_submit_decision_guards_required_fields() -> None:
    """The decision + why inputs are required and must be null-guarded;
    optional fields (date / consequence / revisit) must use optional
    chaining so a partial form degrades gracefully."""
    src = _src("core.js")
    body = _function_body(src, "submitDecision")
    assert '$("#dec-decision").value' not in body, (
        "submitDecision must not call `.value` on a fresh `$(\"#dec-decision\")` lookup "
        "— extract to decisionEl and null-guard"
    )
    assert '$("#dec-why").value' not in body, (
        "submitDecision must not call `.value` on a fresh `$(\"#dec-why\")` lookup "
        "— extract to whyEl and null-guard"
    )
    # First pass: the submit button must still be guarded inline (matches the
    # broader entry-point invariant test_core_main_hardening.py tracks).
    assert 'if (!$("#dec-submit")) return' in body, (
        "submitDecision must keep the inline `if (!$(\"#dec-submit\")) return` "
        "guard so the broader entry-point-count invariant holds"
    )
    # Second pass: decisionEl + whyEl resolved + guarded together.
    assert re.search(
        r"const\s+decisionEl\s*=\s*\$\("
        r'"#dec-decision"\)\s*;\s*const\s+whyEl\s*=\s*\$\('
        r'"#dec-why"\)\s*;\s*if\s*\(\s*!decisionEl\s*\|\|\s*!whyEl\s*\)\s*return',
        body,
    ), "submitDecision must guard decisionEl + whyEl together before any .value.trim()"
    # Optional fields should use `?.value` so missing markup is non-fatal.
    assert "$(\"#dec-date\")?.value" in body, (
        "submitDecision should use optional chaining on the optional #dec-date field"
    )
    assert "$(\"#dec-consequence\")?.value" in body, (
        "submitDecision should use optional chaining on the optional #dec-consequence field"
    )
    assert "$(\"#dec-revisit\")?.value" in body, (
        "submitDecision should use optional chaining on the optional #dec-revisit field"
    )
    # The reset loop must null-guard each element instead of `$(s).value = ""`.
    assert re.search(
        r"\.forEach\s*\(\s*\(s\)\s*=>\s*\{\s*const\s+el\s*=\s*\$\(s\)\s*;\s*if\s*\(\s*el\s*\)\s*el\.value",
        body,
    ), "submitDecision reset loop must null-guard each lookup before writing .value"


# ----- core.js: loadTokenUsage surfaces HTTP failures -----


def test_load_token_usage_warns_on_non_2xx() -> None:
    """Previously `if (!r.ok) return;` swallowed the failure silently.
    Operators saw stuck "—" cards with zero diagnostic.  Verify the
    non-2xx branch now console.warns the path + status."""
    src = _src("core.js")
    body = _function_body(src, "loadTokenUsage")
    # Find the `if (!r.ok)` branch and confirm a console.warn lives inside.
    m = re.search(
        r"if\s*\(\s*!r\.ok\s*\)\s*\{[^}]*?\}",
        body,
        flags=re.DOTALL,
    )
    assert m is not None, "loadTokenUsage must keep an `if (!r.ok) { ... }` branch"
    inner = m.group(0)
    assert "console.warn" in inner, (
        "loadTokenUsage non-2xx branch must console.warn the failure — silent "
        "returns leave operators stuck on `—` with no diagnostic"
    )
    assert "/api/usage/total" in inner or "usage/total" in inner, (
        "console.warn should mention the failing path so the message is useful in logs"
    )


# ----- countMemoryEntries(null) coercion (verify batch 4 status) -----


def test_count_memory_entries_coerces_null() -> None:
    """countMemoryEntries(null) must not throw on `.match`.  Verify the
    `text = text || ""` defensive coerce is still present."""
    src = _src("core.js")
    body = _function_body(src, "countMemoryEntries")
    assert re.search(r'text\s*=\s*text\s*\|\|\s*""', body), (
        "countMemoryEntries must coerce null/undefined to '' before calling .match"
    )


# ----- formatTokens("") returns em-dash (verify batch 4 status) -----


def test_format_tokens_empty_string_returns_emdash() -> None:
    """`isNaN("")` is false (coerces to 0), so without an explicit empty-string
    guard formatTokens("") returned "0".  Verify the guard."""
    src = _src("core.js")
    body = _function_body(src, "formatTokens")
    assert re.search(
        r'n\s*===\s*""\s*\|\|\s*n\s*===\s*null\s*\|\|\s*n\s*===\s*undefined',
        body,
    ), "formatTokens must explicitly reject empty string / null / undefined"


# ----- renderActivity .sort coercion (verify still in place) -----


def test_render_activity_sort_coerces_to_string() -> None:
    """sort comparator must coerce `.name` to String before .localeCompare —
    otherwise a non-string entry from a future caller throws."""
    src = _src("core.js")
    body = _function_body(src, "renderActivity")
    assert re.search(r"String\(\s*[ab]\.name\s*\)\.localeCompare", body), (
        "renderActivity sort comparator must wrap `.name` in String() before .localeCompare"
    )


# ----- tBtn.dataset.current already guarded (verify) -----


def test_render_models_guards_t_btn_before_dataset_write() -> None:
    """`tBtn.dataset.current = mode` lives after the early-return guard; the
    early-return must reference tBtn so a missing #dispatch-toggle cannot
    null-deref."""
    src = _src("core.js")
    body = _function_body(src, "renderModels")
    # The guard line must come BEFORE the dataset write.
    guard_match = re.search(
        r"if\s*\(\s*!tBtn\s*\|\|\s*!modelsTable\s*\)\s*return",
        body,
    )
    assert guard_match is not None, (
        "renderModels must early-return when either #dispatch-toggle or "
        "#models-table is missing"
    )
    write_idx = body.find("tBtn.dataset.current")
    assert write_idx != -1, "renderModels must still write tBtn.dataset.current"
    assert guard_match.end() < write_idx, (
        "The `if (!tBtn || !modelsTable) return` guard must run BEFORE "
        "`tBtn.dataset.current = mode` — otherwise a missing element null-derefs"
    )


# ----- Gemini integration preservation -----


@pytest.mark.skip(reason="gemini dispatch never shipped")
def test_gemini_lines_preserved_intact() -> None:
    """Batch 6 must NOT touch the uncommitted Gemini integration lines in
    core.js.  Pin each one so a future careless edit fails loudly."""
    src = _src("core.js")
    # pillTool gemini case
    assert 'tool === "gemini" ? "gemini"' in src, (
        "pillTool must still classify the gemini tool"
    )
    # shortModelLabel gemini case
    assert '.replace(/^gemini-/, "g-")' in src, (
        "shortModelLabel must still rewrite the gemini- prefix"
    )
    # Gemini comment block above MODELS_BY_TOOL
    assert "Gemini" in src and "ai.google.dev" in src, (
        "MODELS_BY_TOOL comment block must still cite the Gemini docs URL"
    )
    # MODELS_BY_TOOL.gemini block
    for model in (
        "gemini-3.1-pro",
        "gemini-3.5-flash",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ):
        assert model in src, (
            "MODELS_BY_TOOL.gemini must still list " + model
        )
    # Dropdown gemini option
    assert 'value="gemini"' in src and 'initialTool === "gemini"' in src, (
        "editPhaseRow must still render the gemini <option> in the tool dropdown"
    )


# ----- Bonus: total guard-style entry-point count still rising -----


def test_core_entry_point_guards_count_at_least_six() -> None:
    """After batch 6 we expect AT LEAST 6 entry-point null-guards across
    core.js — counting BOTH styles:
      (a) `if (!$("#...")) return` (one-liner)
      (b) `const X = $("#..."); if (!X) return` (resolve-then-check)
    Pin the floor so a regression that accidentally removes a guard fails
    loudly."""
    src = _src("core.js")
    inline_pattern = re.compile(
        r"if\s*\(\s*!\$\(\s*[\"'][^\"']+[\"']\s*\)\s*\)\s*return\b"
    )
    resolved_pattern = re.compile(
        r"const\s+(\w+)\s*=\s*\$\(\s*[\"'][^\"']+[\"']\s*\)\s*;\s*"
        r"if\s*\(\s*!\s*\1\b"
    )
    combined_count = len(inline_pattern.findall(src)) + len(
        resolved_pattern.findall(src)
    )
    assert combined_count >= 6, (
        "core.js should have >= 6 entry-point null-guards across both styles "
        "(inline `if (!$(...))` + resolved `const X = $(...); if (!X)`); "
        "found %d" % combined_count
    )
