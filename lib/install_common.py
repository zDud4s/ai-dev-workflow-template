#!/usr/bin/env python3
"""install_common.py — shared Python payload for install.sh / update-workflow.sh.

Both shell scripts invoke this single file instead of duplicating their inline
heredocs. Behaviour is preserved verbatim from the two pre-refactor blocks; the
`mode` argument selects which steps run so each script keeps its unique
behaviour:

    python install_common.py install <target_dir> <script_dir>
    python install_common.py update  <target_dir> <script_dir>

install mode  — AGENTS.md / CLAUDE.md managed blocks, memory header refresh,
                .claude/settings.json merge, .gitignore managed block.
update  mode  — AGENTS.md / CLAUDE.md managed blocks, skeleton merges for
                memory/decisions/project/models, .claude/settings.json merge,
                pre-refactor ledger/proposal migrations. (No .gitignore block —
                update operates on an existing install.)
"""

from __future__ import annotations

from pathlib import Path
from datetime import date
import json
import re
import subprocess
import sys
import unicodedata


# --- shared block primitives -------------------------------------------------

def upsert_block(path: Path, start_marker: str, end_marker: str, block_text: str) -> None:
    if path.exists():
        content = path.read_text(encoding="utf-8")
        if start_marker in content and end_marker in content:
            before = content.split(start_marker)[0].rstrip()
            after = content.split(end_marker, 1)[1].lstrip()
        else:
            before = content.rstrip()
            after = ""
    else:
        before = ""
        after = ""
    new_content = ""
    if before:
        new_content += before + "\n\n"
    new_content += block_text.strip() + "\n"
    if after:
        new_content += "\n" + after
    # encoding="utf-8" so we don't crash on PT/UTF-8 content on Windows (default cp1252).
    # newline="\n" so the file stays LF on Windows (matches .gitattributes eol=lf).
    path.write_text(new_content, encoding="utf-8", newline="\n")


def write_managed_blocks(target_dir: Path, script_dir: Path) -> None:
    """AGENTS.md managed block + CLAUDE.md managed import. Identical in both scripts."""
    agents_block = (script_dir / ".ai/workflow/agents-block.md").read_text(encoding="utf-8")
    claude_import_block = """<!-- >>> AI WORKFLOW MANAGED IMPORT >>> -->
@.ai/workflow/workflow.md
<!-- <<< AI WORKFLOW MANAGED IMPORT <<< -->"""

    # AGENTS.md
    agents_path = target_dir / "AGENTS.md"
    upsert_block(
        agents_path,
        "# >>> AI WORKFLOW MANAGED BLOCK >>>",
        "# <<< AI WORKFLOW MANAGED BLOCK <<<",
        agents_block,
    )

    # CLAUDE target selection — prefer root (aligned with AGENTS.md);
    # legacy .claude/CLAUDE.md is respected when present but not created.
    root_claude = target_dir / "CLAUDE.md"
    dot_claude = target_dir / ".claude" / "CLAUDE.md"

    if dot_claude.exists() and not root_claude.exists():
        claude_target = dot_claude
    else:
        claude_target = root_claude

    upsert_block(
        claude_target,
        "<!-- >>> AI WORKFLOW MANAGED IMPORT >>> -->",
        "<!-- <<< AI WORKFLOW MANAGED IMPORT <<< -->",
        claude_import_block,
    )


# --- memory header / bullet migration ----------------------------------------

MEMORY_HEADER_START = "<!-- >>> WORKFLOW MANAGED MEMORY HEADER >>> -->"
MEMORY_HEADER_END = "<!-- <<< WORKFLOW MANAGED MEMORY HEADER <<< -->"

# Common Portuguese -> English topic-tag mapping for slugifying section headings
# when migrating bullets. Unknown words pass through as-is.
PT_EN_TOPIC_MAP = {
    "comandos": "commands", "comando": "commands",
    "ambiente": "env", "encoding": "encoding",
    "estrutura": "apps", "apps": "apps",
    "convencoes": "conventions",
    "anomalias": "anomalies",
    "factos": "facts", "fatos": "facts",
    "anotacoes": "notes", "notas": "notes",
}
PT_STOPWORDS = {
    "de", "do", "da", "das", "dos", "o", "a", "os", "as",
    "um", "uma", "uns", "umas", "e", "para", "em", "no", "na",
    "nas", "nos", "com", "por", "ao",
}


def heading_to_topic_slug(heading):
    # "## Comandos confirmados" -> "commands"; first significant word, stripped
    # of diacritics, mapped through PT_EN_TOPIC_MAP when known.
    text = re.sub(r"^#+\s*", "", heading).strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    words = re.findall(r"[a-z]+", text.lower())
    words = [w for w in words if w not in PT_STOPWORDS]
    if not words:
        return "misc"
    first = words[0]
    return PT_EN_TOPIC_MAP.get(first, first)


def upsert_block_at_top(path, start_marker, end_marker, block_text):
    # Like upsert_block but places the block at the TOP of the file. When
    # markers don't exist (legacy file), discards the legacy preamble — i.e.
    # everything before the first `## ` heading — since the managed block
    # replaces it. If the file has no `## ` heading at all, the entire legacy
    # content is treated as preamble and replaced.
    block_text = block_text.strip()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block_text + "\n", encoding="utf-8", newline="\n")
        return "created"
    content = path.read_text(encoding="utf-8")
    if start_marker in content and end_marker in content:
        before = content.split(start_marker)[0].rstrip()
        after = content.split(end_marker, 1)[1].lstrip()
        had_markers = True
    else:
        before = ""
        m = re.search(r"(?m)^## ", content)
        after = content[m.start():] if m else ""
        had_markers = False
    pieces = []
    if before:
        pieces.append(before)
    pieces.append(block_text)
    if after:
        pieces.append(after)
    new_content = "\n\n".join(pieces).rstrip() + "\n"
    if new_content == content:
        return "unchanged"
    path.write_text(new_content, encoding="utf-8", newline="\n")
    return "refreshed" if had_markers else "injected"


DATED_BULLET_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2})\s+(\[[^\]]+\]\s+)?(.*)$")
BULLET_RE = re.compile(r"^- ")


def migrate_bullets_to_entries(target_path):
    # Move bullet lines living under non-Entries sections into the canonical
    # `## Entries` section. Dated bullets keep their date/topic; undated ones
    # get today's date and a [<section_slug>] tag derived from their original
    # section heading. Empty headings (no remaining bullets, no prose) are
    # removed. Headings inside the managed marker region are preserved as-is.
    if not target_path.exists():
        return
    content = target_path.read_text(encoding="utf-8")
    # Split off the managed header so we don't touch it during migration.
    if MEMORY_HEADER_START in content and MEMORY_HEADER_END in content:
        head_end_pos = content.find(MEMORY_HEADER_END) + len(MEMORY_HEADER_END)
        header = content[:head_end_pos]
        body = content[head_end_pos:]
    else:
        header = ""
        body = content
    # Walk body into sections keyed by their `## ` heading. The pre-section
    # area (text before first ##) is preserved as-is.
    lines = body.splitlines(keepends=True)
    sections = []  # [(heading or None, [content lines])]
    current = (None, [])
    for line in lines:
        if line.startswith("## "):
            sections.append(current)
            current = (line, [])
        else:
            current[1].append(line)
    sections.append(current)
    today = date.today().isoformat()
    migrated = []  # list of line_text to append to ## Entries
    drained_sections = []  # headings whose bullets were moved
    removed_sections = []  # headings dropped entirely (now empty)
    new_sections = []
    entries_idx = None
    for i, (heading, body_lines) in enumerate(sections):
        if heading is None:
            new_sections.append((heading, body_lines))
            continue
        if heading.strip().lower() == "## entries":
            entries_idx = len(new_sections)
            new_sections.append((heading, body_lines))
            continue
        slug = heading_to_topic_slug(heading)
        kept_lines = []
        moved_count = 0
        for line in body_lines:
            if BULLET_RE.match(line):
                m = DATED_BULLET_RE.match(line.rstrip("\n"))
                if m:
                    # Already dated — keep its date/topic verbatim.
                    migrated.append(line if line.endswith("\n") else line + "\n")
                else:
                    rest = line[2:].rstrip("\n")
                    migrated.append(f"- {today} [{slug}] {rest}\n")
                moved_count += 1
            else:
                kept_lines.append(line)
        if moved_count > 0:
            drained_sections.append((heading.strip(), moved_count))
        non_blank = [l for l in kept_lines if l.strip()]
        if not non_blank:
            removed_sections.append(heading.strip())
            continue
        new_sections.append((heading, kept_lines))
    if not migrated and not removed_sections:
        return  # nothing to do
    # If target had no ## Entries, create one (placed where the markers' close
    # already sits — i.e., immediately after the header / first content gap).
    if entries_idx is None:
        new_sections.insert(0, ("## Entries\n", ["\n"]))
        entries_idx = 0
    # Append migrated bullets to ## Entries body (after existing entries).
    heading, body_lines = new_sections[entries_idx]
    # Ensure body ends with a blank line before appending if it has content.
    has_content = any(l.strip() for l in body_lines)
    if has_content and body_lines and body_lines[-1].strip():
        body_lines.append("\n")
    if not has_content and not body_lines:
        body_lines.append("\n")
    body_lines.extend(migrated)
    new_sections[entries_idx] = (heading, body_lines)
    # Rebuild body text.
    rebuilt = []
    for heading, body_lines in new_sections:
        if heading is not None:
            rebuilt.append(heading)
        rebuilt.extend(body_lines)
    new_body = "".join(rebuilt)
    new_content = (header + "\n\n" + new_body.lstrip("\n")).rstrip("\n") + "\n"
    target_path.write_text(new_content, encoding="utf-8", newline="\n")
    drained_desc = ", ".join(f"{h.lstrip('#').strip()} ({n})" for h, n in drained_sections)
    print(f"Migrated {len(migrated)} bullet(s) to ## Entries from: {drained_desc}")
    if removed_sections:
        removed_desc = ", ".join(h.lstrip("#").strip() for h in removed_sections)
        print(f"Removed empty heading(s): {removed_desc}")


def refresh_memory_header(template_path, target_path, *, skeleton_fallback=False):
    # Refresh the managed memory.md header (preamble + `## Entries`) and then
    # migrate any stray bullets into `## Entries` so the structure matches the
    # template contract: bullets live ONLY under `## Entries`.
    if not template_path.exists():
        return
    template_text = template_path.read_text(encoding="utf-8")
    if MEMORY_HEADER_START not in template_text or MEMORY_HEADER_END not in template_text:
        if skeleton_fallback:
            # Template doesn't define markers — fall back to the additive merge.
            merge_md_skeleton(template_path, target_path, "memory")
        return
    block = template_text.split(MEMORY_HEADER_START, 1)[1]
    block = block.split(MEMORY_HEADER_END, 1)[0]
    block = f"{MEMORY_HEADER_START}{block}{MEMORY_HEADER_END}"
    result = upsert_block_at_top(target_path, MEMORY_HEADER_START, MEMORY_HEADER_END, block)
    if result == "created":
        print(f"Created {target_path} (workflow memory header)")
    elif result == "injected":
        print(f"Injected {target_path} managed header (no markers found before)")
    elif result == "refreshed":
        print(f"Refreshed {target_path} managed header")
    # else: unchanged — print nothing
    migrate_bullets_to_entries(target_path)


# --- skeleton merge for project state files (update mode only) ---------------
# Goal: when the template grows a new ## section in memory.md/decisions.md or a
# new top-level key in project.yaml/models.yaml, propagate it to the target
# WITHOUT touching entries/values the user wrote. Strictly additive: missing
# scaffold appended at end; existing content untouched.

MD_SECTION_RE = re.compile(r'(?m)^(## .+)$')
YAML_TOPLEVEL_RE = re.compile(r'^([A-Za-z_][\w-]*):')
DATED_ENTRY_RE = re.compile(r'^- \d{4}-\d{2}-\d{2}\s')


def strip_dated_entries(body):
    # Template ships its own dev memory under sections like "## Entries". When
    # we add that section to a downstream project, the heading/scaffold travels
    # but the dated entries (per-project content) must not. Drop lines matching
    # `- YYYY-MM-DD ...` and collapse the blank-line runs left behind.
    kept = [
        line for line in body.splitlines(keepends=True)
        if not DATED_ENTRY_RE.match(line)
    ]
    return re.sub(r'\n{3,}', '\n\n', "".join(kept))


def md_top_level_sections(text):
    parts = MD_SECTION_RE.split(text)
    sections = []
    for i in range(1, len(parts), 2):
        heading = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections.append((heading, body))
    return sections


def yaml_top_level_blocks(text):
    # Comments/blank lines immediately preceding a NON-FIRST top-level key are
    # attached to THAT key (its leading doc-comment travels with it on merge).
    # File-leading comments (before the first key) are file preamble and dropped
    # from all blocks — otherwise we'd duplicate the header on merge.
    blocks = []
    current_key = None
    current_lines = []
    pending = []
    seen_any_key = False
    for line in text.splitlines(keepends=True):
        m = YAML_TOPLEVEL_RE.match(line)
        if m:
            if current_key is not None:
                blocks.append((current_key, "".join(current_lines)))
            current_key = m.group(1)
            current_lines = ([line] if not seen_any_key else pending + [line])
            pending = []
            seen_any_key = True
        elif line.strip() == "" or line.lstrip().startswith("#"):
            pending.append(line)
        elif current_key is not None:
            current_lines.extend(pending)
            current_lines.append(line)
            pending = []
        else:
            pending = []
    if current_key is not None:
        current_lines.extend(pending)
        blocks.append((current_key, "".join(current_lines)))
    return blocks


def append_with_blank_line(target_text, addition):
    if not target_text:
        return addition
    out = target_text.rstrip("\n") + "\n\n"
    return out + addition


def merge_md_skeleton(template_path, target_path, label):
    if not target_path.exists():
        return
    template_text = template_path.read_text(encoding="utf-8")
    target_text = target_path.read_text(encoding="utf-8")
    target_headings = {h.strip() for h, _ in md_top_level_sections(target_text)}
    missing = [
        (h, b) for h, b in md_top_level_sections(template_text)
        if h.strip() not in target_headings
    ]
    if not missing:
        print(f"Kept {target_path} ({label}, no missing ## sections)")
        return
    addition = "".join(h + strip_dated_entries(b) for h, b in missing).rstrip("\n") + "\n"
    target_path.write_text(
        append_with_blank_line(target_text, addition),
        encoding="utf-8", newline="\n",
    )
    added = ", ".join(h.strip().lstrip("#").strip() for h, _ in missing)
    print(f"Merged {target_path} ({label}, added sections: {added})")


def merge_yaml_skeleton(template_path, target_path, label):
    if not target_path.exists():
        return
    template_text = template_path.read_text(encoding="utf-8")
    target_text = target_path.read_text(encoding="utf-8")
    target_keys = {k for k, _ in yaml_top_level_blocks(target_text)}
    missing = [
        (k, b) for k, b in yaml_top_level_blocks(template_text)
        if k not in target_keys
    ]
    if not missing:
        print(f"Kept {target_path} ({label}, no missing top-level keys)")
        return
    addition = ""
    for _, b in missing:
        addition += b if b.endswith("\n") else b + "\n"
    target_path.write_text(
        append_with_blank_line(target_text, addition),
        encoding="utf-8", newline="\n",
    )
    added = ", ".join(k for k, _ in missing)
    print(f"Merged {target_path} ({label}, added top-level keys: {added})")


# --- .claude/settings.json merge ---------------------------------------------

def merge_claude_settings(template_path, target_path):
    # .claude/settings.json carries permissions + hooks the workflow needs to
    # orchestrate (dashboard event hook, codex exec permission, etc.). Merge
    # required entries in without overwriting user-added permissions or hooks.
    # Create the file if missing.
    if not template_path.exists():
        return
    template_data = json.loads(template_path.read_text(encoding="utf-8"))
    if not target_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(template_data, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
        print(f"Created {target_path} (workflow settings)")
        return
    try:
        target_data = json.loads(target_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        # A malformed/empty user settings.json must not abort the whole
        # install/update under `set -euo pipefail` after files were already
        # copied. Warn and skip the merge so the operator can fix the file and
        # re-run.
        print(f"WARNING: {target_path} is not valid JSON ({exc}); "
              f"skipping workflow settings merge — fix the file and re-run.")
        return
    added_perms = []
    added_hooks = []
    # permissions.allow: union, preserve target order, append new ones.
    template_allow = (template_data.get("permissions") or {}).get("allow") or []
    target_perms = target_data.setdefault("permissions", {})
    target_allow = target_perms.setdefault("allow", [])
    target_allow_set = set(target_allow)
    for perm in template_allow:
        if perm not in target_allow_set:
            target_allow.append(perm)
            target_allow_set.add(perm)
            added_perms.append(perm)
    # hooks.<event>: per-matcher merge, per-command dedup.
    template_hooks = template_data.get("hooks") or {}
    target_hooks = target_data.setdefault("hooks", {})
    for event, template_entries in template_hooks.items():
        target_entries = target_hooks.setdefault(event, [])
        for template_entry in template_entries:
            matcher = template_entry.get("matcher", "")
            target_entry = next(
                (te for te in target_entries if te.get("matcher") == matcher),
                None,
            )
            if target_entry is None:
                target_entry = {"matcher": matcher, "hooks": []}
                target_entries.append(target_entry)
            existing_cmds = {h.get("command") for h in target_entry.get("hooks") or []}
            for template_hook in template_entry.get("hooks") or []:
                cmd = template_hook.get("command")
                if cmd and cmd not in existing_cmds:
                    target_entry.setdefault("hooks", []).append(template_hook)
                    existing_cmds.add(cmd)
                    added_hooks.append(f"{event}/{matcher}")
    if not (added_perms or added_hooks):
        print(f"Kept {target_path} (workflow settings already present)")
        return
    target_path.write_text(
        json.dumps(target_data, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    parts = []
    if added_perms:
        parts.append(f"+{len(added_perms)} permission(s)")
    if added_hooks:
        parts.append(f"+{len(added_hooks)} hook(s)")
    print(f"Merged {target_path} ({', '.join(parts)})")


# --- .gitignore managed block (install mode only) ----------------------------

def upsert_gitignore(target_path):
    # The workflow footprint (.ai/, .claude/, .agents/) is machinery for THIS
    # developer's local agent runs — it should not land in the host project's
    # history. Upsert a managed block into the target's .gitignore so every
    # install keeps the workflow folders out of git. Idempotent: re-running
    # rewrites only the managed block and preserves the rest of the file.
    block = """# >>> AI WORKFLOW MANAGED >>>
# Installed by ai-dev-workflow-template install.sh — keeps the workflow
# footprint out of this project's git history.
.ai/
.claude/
.agents/
# <<< AI WORKFLOW MANAGED <<<"""
    existed = target_path.exists()
    had_block = False
    if existed:
        content = target_path.read_text(encoding="utf-8")
        had_block = (
            "# >>> AI WORKFLOW MANAGED >>>" in content
            and "# <<< AI WORKFLOW MANAGED <<<" in content
        )
    upsert_block(
        target_path,
        "# >>> AI WORKFLOW MANAGED >>>",
        "# <<< AI WORKFLOW MANAGED <<<",
        block,
    )
    if not existed:
        print(f"Created {target_path} (workflow footprint ignore block)")
    elif had_block:
        print(f"Refreshed {target_path} (workflow footprint ignore block)")
    else:
        print(f"Updated {target_path} (added workflow footprint ignore block)")


# --- structure migration (update mode only) ----------------------------------
# Generated runtime data now lives under .ai/local/ (ledgers, agent-runs,
# pipelines, jobs, proposals). Earlier layouts wrote it directly under .ai/ and
# .ai/dashboard/ (very old), then under .ai/ledgers, .ai/agent-runs,
# .ai/pipelines and .ai/dashboard/{jobs,proposals} (intermediate). The current
# code reads ONLY .ai/local/, so leftover data at any older path would become
# invisible. Move it into place. Idempotent: ledgers merge (old lines first,
# append new); dir movers skip per-file collisions to avoid clobbering.
LEDGER_MIGRATIONS = [
    # Very old: ledgers written directly under .ai/ and .ai/dashboard/.
    (".ai/dashboard/jobs.jsonl",          ".ai/local/ledgers/jobs.jsonl"),
    (".ai/events.jsonl",                  ".ai/local/ledgers/events.jsonl"),
    (".ai/metrics.jsonl",                 ".ai/local/ledgers/metrics.jsonl"),
    (".ai/dashboard/skill_metrics.jsonl", ".ai/local/ledgers/skill_metrics.jsonl"),
    (".ai/dashboard/improvements.jsonl",  ".ai/local/ledgers/improvements.jsonl"),
    # Intermediate .ai/ledgers/ layout -> .ai/local/ledgers/.
    (".ai/ledgers/jobs.jsonl",            ".ai/local/ledgers/jobs.jsonl"),
    (".ai/ledgers/events.jsonl",          ".ai/local/ledgers/events.jsonl"),
    (".ai/ledgers/metrics.jsonl",         ".ai/local/ledgers/metrics.jsonl"),
    (".ai/ledgers/skill_metrics.jsonl",   ".ai/local/ledgers/skill_metrics.jsonl"),
    (".ai/ledgers/improvements.jsonl",    ".ai/local/ledgers/improvements.jsonl"),
    (".ai/ledgers/todos.jsonl",           ".ai/local/ledgers/todos.jsonl"),
    (".ai/ledgers/todos-archive.jsonl",   ".ai/local/ledgers/todos-archive.jsonl"),
]
PROPOSAL_DIR_MIGRATIONS = [
    # Very old: flat proposal/backup dirs under .ai/dashboard/.
    (".ai/dashboard/skill_proposals", ".ai/local/proposals/skills"),
    (".ai/dashboard/agent_proposals", ".ai/local/proposals/agents"),
    (".ai/dashboard/skill_backups",   ".ai/local/proposals/skill_backups"),
    # Intermediate .ai/dashboard/proposals/ layout -> .ai/local/proposals/.
    (".ai/dashboard/proposals/skills",        ".ai/local/proposals/skills"),
    (".ai/dashboard/proposals/agents",        ".ai/local/proposals/agents"),
    (".ai/dashboard/proposals/skill_backups", ".ai/local/proposals/skill_backups"),
]
# Whole runtime dirs that simply relocated under .ai/local/. Move their
# children into place and remove the empty legacy dir (reuses the proposal mover).
DIR_MIGRATIONS = [
    (".ai/agent-runs",     ".ai/local/agent-runs"),
    (".ai/pipelines",      ".ai/local/pipelines"),
    (".ai/dashboard/jobs", ".ai/local/jobs"),
]


def migrate_ledger_file(target_dir, old_rel, new_rel):
    old = target_dir / old_rel
    new = target_dir / new_rel
    if not old.is_file():
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    if not new.exists():
        old.replace(new)
        print(f"Migrated {old} -> {new} (pre-refactor ledger)")
        return
    old_text = old.read_text(encoding="utf-8")
    if not old_text:
        old.unlink()
        return
    if not old_text.endswith("\n"):
        # JSONL is line-oriented; missing trailing newline on old would fuse
        # the last old record with the first new one. Force one.
        old_text += "\n"
    new_text = new.read_text(encoding="utf-8")
    new.write_text(old_text + new_text, encoding="utf-8", newline="\n")
    old.unlink()
    print(f"Merged {old} into {new} (pre-refactor ledger; old lines kept on top)")


def migrate_proposal_dir(target_dir, old_rel, new_rel):
    old = target_dir / old_rel
    new = target_dir / new_rel
    if not old.is_dir() or old.is_symlink():
        return
    new.mkdir(parents=True, exist_ok=True)
    moved = 0
    for child in list(old.iterdir()):
        dest = new / child.name
        if dest.exists():
            continue  # don't overwrite — leave the collision in the old dir
        child.replace(dest)
        moved += 1
    leftovers = list(old.iterdir())
    if not leftovers:
        try:
            old.rmdir()
            print(f"Migrated {old} -> {new} ({moved} item(s) moved, legacy dir removed)")
        except OSError:
            print(f"Migrated {old} -> {new} ({moved} item(s) moved; legacy dir not removable)")
    else:
        print(f"Partially migrated {old} -> {new} ({moved} moved, {len(leftovers)} collision(s) left in old)")


def run_pre_refactor_migrations(target_dir):
    for old_rel, new_rel in LEDGER_MIGRATIONS:
        migrate_ledger_file(target_dir, old_rel, new_rel)
    for old_rel, new_rel in PROPOSAL_DIR_MIGRATIONS:
        migrate_proposal_dir(target_dir, old_rel, new_rel)
    for old_rel, new_rel in DIR_MIGRATIONS:
        migrate_proposal_dir(target_dir, old_rel, new_rel)


# --- workflow version stamp --------------------------------------------------

def stamp_workflow_version(target_dir: Path, script_dir: Path) -> None:
    # Record the template's HEAD sha into .ai/workflow/.version so a fresh
    # install/update starts "versioned". Without this the file is only ever
    # written by a *successful* dashboard apply — but the apply button is gated
    # on a recorded version, so an unstamped install can never reach it
    # (chicken-and-egg). The dashboard's /api/workflow/check reads this file to
    # compute ahead/behind. Best-effort: when script_dir is not a git checkout
    # (e.g. installed from a tarball) or git is unavailable, skip silently so
    # install/update never aborts under `set -euo pipefail`.
    try:
        proc = subprocess.run(
            ["git", "-C", str(script_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if proc.returncode != 0:
        return
    sha = proc.stdout.strip()
    if not sha:
        return
    version_file = target_dir / ".ai" / "workflow" / ".version"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(sha + "\n", encoding="utf-8", newline="\n")
    print(f"Stamped {version_file} ({sha[:7]})")


# --- mode dispatch -----------------------------------------------------------

def run_install(target_dir: Path, script_dir: Path) -> None:
    write_managed_blocks(target_dir, script_dir)
    refresh_memory_header(
        script_dir / ".ai/memory.md",
        target_dir / ".ai/memory.md",
        skeleton_fallback=False,
    )
    merge_claude_settings(
        script_dir / ".claude/settings.json",
        target_dir / ".claude/settings.json",
    )
    upsert_gitignore(target_dir / ".gitignore")
    stamp_workflow_version(target_dir, script_dir)


def run_update(target_dir: Path, script_dir: Path) -> None:
    write_managed_blocks(target_dir, script_dir)
    refresh_memory_header(
        script_dir / ".ai/memory.md",
        target_dir / ".ai/memory.md",
        skeleton_fallback=True,
    )
    merge_md_skeleton(
        script_dir / ".ai/decisions.md",
        target_dir / ".ai/decisions.md",
        "decisions",
    )
    merge_yaml_skeleton(
        # project.yaml ships as a tracked .template skeleton (the working file is
        # gitignored in the template repo); the target's working .ai/project.yaml
        # is the merge destination as before.
        script_dir / ".ai/project.yaml.template",
        target_dir / ".ai/project.yaml",
        "project state",
    )
    merge_yaml_skeleton(
        script_dir / ".ai/models.yaml",
        target_dir / ".ai/models.yaml",
        "models config",
    )
    merge_claude_settings(
        script_dir / ".claude/settings.json",
        target_dir / ".claude/settings.json",
    )
    run_pre_refactor_migrations(target_dir)
    stamp_workflow_version(target_dir, script_dir)


def main(argv) -> int:
    if len(argv) != 4:
        print(
            "usage: install_common.py <install|update> <target_dir> <script_dir>",
            file=sys.stderr,
        )
        return 2
    mode, target_dir, script_dir = argv[1], Path(argv[2]), Path(argv[3])
    if mode == "install":
        run_install(target_dir, script_dir)
    elif mode == "update":
        run_update(target_dir, script_dir)
    else:
        print(f"unknown mode: {mode!r} (expected install|update)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
