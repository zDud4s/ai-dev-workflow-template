"""Static-lint tests for batch-8 (final) fixes on skills.js + agents.js.

This batch closes the residual MEDIUM/LOW/PERF items in
`docs/bug-hunt-status.md` for the skills/agents scope. Specifically:

  Null-guards for missing DOM elements
  --------------------------------------
    - renderSkillsSummary / renderSkillsFilters / renderSkillsGrid
    - openSkillDetail / closeSkillDetail
    - openProposalModal / closeProposalModal
    - decideProposal (post-await `if (msgEl)` etc)
    - renderSkillSuggestions
    - renderAgentsSummary / renderAgentsFilters / renderAgentsGrid
    - openAgentDetail / closeAgentDetail
    - openAgentProposalModal / closeAgentProposalModal
    - decideAgentProposal (post-await guards)

  Fire-and-forget rejection surfacing
  -----------------------------------
    - loadSkills wraps loadSkillProposals/loadSkillSuggestions in
      `Promise.resolve(...).catch(...)` so async failures surface to the
      console instead of vanishing into an unhandled rejection.
    - loadAgents wraps loadAgentProposals identically.

  Epoch race guard (closes MEDIUM "_currentProposalId race during click spam")
  ----------------------------------------------------------------------------
    - `_decideProposalEpoch` declared at module scope in skills.js.
    - `_decideAgentProposalEpoch` declared at module scope in agents.js.
    - Both functions tick + snapshot the epoch before the first await and
      guard the success + error paths against a stale epoch.

  Magic numbers → named constants
  -------------------------------
    - `PROPOSAL_AUTO_CLOSE_MS` (600) extracted from decideProposal.
    - `DRAFT_AUTOOPEN_MS` (300) + `DRAFT_BUTTON_RESET_MS` (2400) extracted
      from draftSkillFromCluster.
    - `AGENT_PROPOSAL_AUTO_CLOSE_MS` (700) extracted from
      decideAgentProposal.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILLS_JS = ROOT / ".ai" / "dashboard" / "app" / "skills.js"
AGENTS_JS = ROOT / ".ai" / "dashboard" / "app" / "agents.js"


def _skills() -> str:
    return SKILLS_JS.read_text(encoding="utf-8")


def _agents() -> str:
    return AGENTS_JS.read_text(encoding="utf-8")


def _function_body(src: str, name: str, *, is_async: bool = False) -> str:
    """Return brace-balanced body of a top-level function (incl. braces)."""
    prefix = r"async\s+" if is_async else r"(?:async\s+)?"
    pat = re.compile(prefix + r"function\s+" + re.escape(name) + r"\s*\(")
    m = pat.search(src)
    assert m, f"function {name!r} not found"
    i = src.find("{", m.end())
    assert i != -1, f"opening brace for {name!r} not found"
    depth = 0
    j = i
    while j < len(src):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
        j += 1
    raise AssertionError(f"could not find end of function {name!r}")


# --------------------------------------------------------------------- #
# Null-guards: skills.js render*                                         #
# --------------------------------------------------------------------- #


def test_render_skills_summary_null_guards_summary():
    """renderSkillsSummary must bail when #skills-summary is missing.

    Without the guard, an unguarded `summary.innerHTML = html` would crash
    boot in a partial-DOM teardown / test harness, masking the underlying
    layout change."""
    body = _function_body(_skills(), "renderSkillsSummary")
    assert "if (!summary) return" in body, (
        "renderSkillsSummary must early-bail when #skills-summary is null"
    )


def test_render_skills_filters_null_guards_wrap():
    body = _function_body(_skills(), "renderSkillsFilters")
    assert "if (!wrap) return" in body, (
        "renderSkillsFilters must early-bail when #skills-filters is null"
    )


def test_render_skills_grid_null_guards_meta_and_grid():
    body = _function_body(_skills(), "renderSkillsGrid")
    # metaEl is guarded by `if (metaEl)`, grid by early-return.
    assert "if (metaEl) metaEl.textContent" in body, (
        "renderSkillsGrid must null-guard #skills-meta"
    )
    assert "if (!grid) return" in body, (
        "renderSkillsGrid must early-bail when #skills-grid is null"
    )


def test_render_skill_suggestions_null_guards_count_and_wrap():
    body = _function_body(_skills(), "renderSkillSuggestions")
    assert "if (countEl) countEl.textContent" in body, (
        "renderSkillSuggestions must null-guard #suggestions-count"
    )
    assert "if (!wrap) return" in body, (
        "renderSkillSuggestions must early-bail when #skills-suggestions is null"
    )


# --------------------------------------------------------------------- #
# Null-guards: skills.js open/close modal helpers                        #
# --------------------------------------------------------------------- #


def test_open_skill_detail_null_guards_modal():
    body = _function_body(_skills(), "openSkillDetail", is_async=True)
    assert "if (!modal) return" in body, (
        "openSkillDetail must bail when #skill-detail-modal is missing"
    )
    # And each subsequent slot must be guarded with `if (xEl)`.
    for slot in ("titleEl", "contentEl", "recentEl", "historyEl"):
        assert re.search(rf"if \({slot}\)", body), (
            f"openSkillDetail must null-guard {slot}"
        )


def test_close_skill_detail_null_guards_modal():
    body = _function_body(_skills(), "closeSkillDetail")
    assert re.search(r"if \(modal\) modal\.hidden = true", body), (
        "closeSkillDetail must null-guard the modal lookup"
    )


def test_open_proposal_modal_null_guards_each_slot():
    body = _function_body(_skills(), "openProposalModal", is_async=True)
    assert "if (!modal) return" in body
    for slot in ("titleEl", "metaEl", "diffEl", "acceptBtn", "rejectBtn", "msgEl"):
        assert re.search(rf"if \({slot}\)", body), (
            f"openProposalModal must null-guard {slot}"
        )


def test_close_proposal_modal_null_guards_modal():
    body = _function_body(_skills(), "closeProposalModal")
    assert re.search(r"if \(modal\) modal\.hidden = true", body)


def test_decide_proposal_null_guards_buttons_and_msg():
    body = _function_body(_skills(), "decideProposal", is_async=True)
    # The pre-await sets must all be guarded.
    for slot in ("acceptBtn", "rejectBtn", "msgEl"):
        assert re.search(rf"if \({slot}\)", body), (
            f"decideProposal must null-guard {slot}"
        )


# --------------------------------------------------------------------- #
# Null-guards: agents.js mirrors                                         #
# --------------------------------------------------------------------- #


def test_render_agents_summary_null_guards():
    body = _function_body(_agents(), "renderAgentsSummary")
    assert "if (!summary) return" in body


def test_render_agents_filters_null_guards():
    body = _function_body(_agents(), "renderAgentsFilters")
    assert "if (!wrap) return" in body


def test_render_agents_grid_null_guards_meta_and_grid():
    body = _function_body(_agents(), "renderAgentsGrid")
    assert "if (metaEl) metaEl.textContent" in body
    assert "if (!grid) return" in body


def test_open_agent_detail_null_guards_modal():
    body = _function_body(_agents(), "openAgentDetail", is_async=True)
    assert "if (!modal) return" in body
    for slot in ("titleEl", "contentEl", "metaEl"):
        assert re.search(rf"if \({slot}\)", body), (
            f"openAgentDetail must null-guard {slot}"
        )


def test_close_agent_detail_null_guards_modal():
    body = _function_body(_agents(), "closeAgentDetail")
    assert re.search(r"if \(modal\) modal\.hidden = true", body)


def test_open_agent_proposal_modal_null_guards_each_slot():
    body = _function_body(_agents(), "openAgentProposalModal", is_async=True)
    assert "if (!modal) return" in body
    for slot in ("titleEl", "metaEl", "bodyEl", "acceptBtn", "rejectBtn", "msgEl"):
        assert re.search(rf"if \({slot}\)", body), (
            f"openAgentProposalModal must null-guard {slot}"
        )


def test_close_agent_proposal_modal_null_guards_modal():
    body = _function_body(_agents(), "closeAgentProposalModal")
    assert re.search(r"if \(modal\) modal\.hidden = true", body)


def test_decide_agent_proposal_null_guards_buttons_and_msg():
    body = _function_body(_agents(), "decideAgentProposal", is_async=True)
    for slot in ("acceptBtn", "rejectBtn", "msgEl"):
        assert re.search(rf"if \({slot}\)", body), (
            f"decideAgentProposal must null-guard {slot}"
        )


# --------------------------------------------------------------------- #
# Fire-and-forget rejection surfacing                                    #
# --------------------------------------------------------------------- #


def test_load_skills_wraps_loadSkillProposals_in_promise_catch():
    """loadSkills calls loadSkillProposals + loadSkillSuggestions without
    awaiting them. Unhandled rejection from either swallows the failure
    silently. Wrap with `Promise.resolve(...).catch(...)` so the rejection
    at least logs to the console."""
    body = _function_body(_skills(), "loadSkills", is_async=True)
    assert "Promise.resolve(loadSkillProposals())" in body, (
        "loadSkills must wrap loadSkillProposals in a promise catch"
    )
    assert "Promise.resolve(loadSkillSuggestions())" in body, (
        "loadSkills must wrap loadSkillSuggestions in a promise catch"
    )
    # And each wrapper must attach a `.catch(...)`.
    assert body.count(".catch((err)") >= 2, (
        "Both fire-and-forget calls must attach a .catch handler"
    )


def test_load_agents_wraps_loadAgentProposals_in_promise_catch():
    body = _function_body(_agents(), "loadAgents", is_async=True)
    assert "Promise.resolve(loadAgentProposals())" in body, (
        "loadAgents must wrap loadAgentProposals in a promise catch"
    )
    assert body.count(".catch((err)") >= 1


# --------------------------------------------------------------------- #
# Epoch race guard (skills + agents)                                     #
# --------------------------------------------------------------------- #


def test_skills_decide_proposal_epoch_declared():
    src = _skills()
    assert re.search(r"\bvar\s+_decideProposalEpoch\s*=\s*0\b", src), (
        "skills.js must declare _decideProposalEpoch at module scope"
    )


def test_skills_decide_proposal_ticks_epoch_before_await():
    body = _function_body(_skills(), "decideProposal", is_async=True)
    await_match = re.search(r"\bawait\b", body)
    assert await_match
    pre_await = body[: await_match.start()]
    assert re.search(
        r"\bconst\s+epoch\s*=\s*\+\+\s*_decideProposalEpoch\b",
        pre_await,
    ), "decideProposal must snapshot ++_decideProposalEpoch before its first await"


def test_skills_decide_proposal_epoch_guards_both_paths():
    body = _function_body(_skills(), "decideProposal", is_async=True)
    catch_match = re.search(r"\}\s*catch\s*\(", body)
    assert catch_match
    success_half = body[: catch_match.start()]
    error_half = body[catch_match.start():]
    assert re.search(r"epoch\s*!==?\s*_decideProposalEpoch", success_half), (
        "success path of decideProposal must guard against stale epoch"
    )
    assert re.search(r"epoch\s*!==?\s*_decideProposalEpoch", error_half), (
        "error path of decideProposal must also guard against stale epoch"
    )


def test_agents_decide_agent_proposal_epoch_declared():
    src = _agents()
    assert re.search(r"\bvar\s+_decideAgentProposalEpoch\s*=\s*0\b", src), (
        "agents.js must declare _decideAgentProposalEpoch at module scope"
    )


def test_agents_decide_agent_proposal_ticks_epoch_before_await():
    body = _function_body(_agents(), "decideAgentProposal", is_async=True)
    await_match = re.search(r"\bawait\b", body)
    assert await_match
    pre_await = body[: await_match.start()]
    assert re.search(
        r"\bconst\s+epoch\s*=\s*\+\+\s*_decideAgentProposalEpoch\b",
        pre_await,
    ), "decideAgentProposal must snapshot ++_decideAgentProposalEpoch before its first await"


def test_agents_decide_agent_proposal_epoch_guards_both_paths():
    body = _function_body(_agents(), "decideAgentProposal", is_async=True)
    catch_match = re.search(r"\}\s*catch\s*\(", body)
    assert catch_match
    success_half = body[: catch_match.start()]
    error_half = body[catch_match.start():]
    assert re.search(r"epoch\s*!==?\s*_decideAgentProposalEpoch", success_half), (
        "success path of decideAgentProposal must guard against stale epoch"
    )
    assert re.search(r"epoch\s*!==?\s*_decideAgentProposalEpoch", error_half), (
        "error path of decideAgentProposal must also guard against stale epoch"
    )


# --------------------------------------------------------------------- #
# Magic numbers → named constants                                        #
# --------------------------------------------------------------------- #


def test_skills_proposal_auto_close_ms_constant_present():
    src = _skills()
    assert re.search(r"\bvar\s+PROPOSAL_AUTO_CLOSE_MS\s*=\s*600\b", src), (
        "PROPOSAL_AUTO_CLOSE_MS constant missing (was a bare 600 in setTimeout)"
    )
    body = _function_body(src, "decideProposal", is_async=True)
    assert "PROPOSAL_AUTO_CLOSE_MS" in body, (
        "decideProposal must use the named PROPOSAL_AUTO_CLOSE_MS constant"
    )


def test_skills_draft_constants_present():
    src = _skills()
    assert re.search(r"\bvar\s+DRAFT_AUTOOPEN_MS\s*=\s*300\b", src), (
        "DRAFT_AUTOOPEN_MS constant missing"
    )
    assert re.search(r"\bvar\s+DRAFT_BUTTON_RESET_MS\s*=\s*2400\b", src), (
        "DRAFT_BUTTON_RESET_MS constant missing"
    )
    body = _function_body(src, "draftSkillFromCluster", is_async=True)
    assert "DRAFT_AUTOOPEN_MS" in body
    assert "DRAFT_BUTTON_RESET_MS" in body


def test_agents_proposal_auto_close_ms_constant_present():
    src = _agents()
    assert re.search(r"\bvar\s+AGENT_PROPOSAL_AUTO_CLOSE_MS\s*=\s*700\b", src), (
        "AGENT_PROPOSAL_AUTO_CLOSE_MS constant missing (was a bare 700)"
    )
    body = _function_body(src, "decideAgentProposal", is_async=True)
    assert "AGENT_PROPOSAL_AUTO_CLOSE_MS" in body


# --------------------------------------------------------------------- #
# Regression guards for the count badge `!` literal in catch paths.      #
# Tests in test_skills_agents_batch5.py pin the substring shape.         #
# --------------------------------------------------------------------- #


def test_load_skills_catch_keeps_count_badge_literal():
    body = _function_body(_skills(), "loadSkills", is_async=True)
    catch_match = re.search(r"\}\s*catch\s*\(\s*e\s*\)\s*\{", body)
    assert catch_match
    catch_block = body[catch_match.end():]
    assert '$("#count-skills").textContent = "!"' in catch_block, (
        "loadSkills catch must keep the literal `$(\"#count-skills\")."
        "textContent = \"!\"` shape that batch-5 tests pin against"
    )


def test_load_agents_catch_keeps_count_badge_literal():
    body = _function_body(_agents(), "loadAgents", is_async=True)
    catch_match = re.search(r"\}\s*catch\s*\(\s*e\s*\)\s*\{", body)
    assert catch_match
    catch_block = body[catch_match.end():]
    assert '$("#count-agents").textContent = "!"' in catch_block, (
        "loadAgents catch must keep the literal `$(\"#count-agents\")."
        "textContent = \"!\"` shape"
    )
