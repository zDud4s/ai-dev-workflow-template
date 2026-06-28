from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = REPO_ROOT / ".ai" / "eval"
SUITE_ROOT = EVAL_ROOT / "suite"
sys.path.insert(0, str(EVAL_ROOT))

from harness.loader import ManifestError, load_manifest  # noqa: E402
from harness.partition import PartitionError, assert_results_path, held_out_ids  # noqa: E402
from harness.runner import (  # noqa: E402
    InvokeResult,
    PhaseResult,
    run_arm_a,
    run_arm_c,
)


def test_manifest_loads_and_partitions() -> None:
    manifest = load_manifest(SUITE_ROOT)

    assert [task.id for task in manifest.all()] == [
        "sum-list",
        "reverse-words",
        "compare-version",
        "flatten",
        "roman-to-int",
        "is-balanced",
        "json-pointer",
        "int-to-roman",
        "kv-store",
        "cli-parser",
        "lru-cache",
    ]
    assert {task.id for task in manifest.tuning()} == {
        "sum-list",
        "compare-version",
        "flatten",
        "json-pointer",
        "kv-store",
        "cli-parser",
    }
    assert held_out_ids(manifest) == {
        "reverse-words",
        "roman-to-int",
        "is-balanced",
        "int-to-roman",
        "lru-cache",
    }


def test_manifest_rejects_bad_manifest(tmp_path: Path) -> None:
    suite_root = tmp_path / "suite"
    task_dir = suite_root / "tasks" / "sum-list"
    task_dir.mkdir(parents=True)
    (task_dir / "check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    (suite_root / "manifest.toml").write_text(
        """
version = 1

[[tasks]]
id = "sum-list"
partition = "tuning"
path = "tasks/sum-list"
entrypoint = "solution.py"
check = "check.py"

[[tasks]]
id = "sum-list"
partition = "held-out"
path = "tasks/sum-list"
entrypoint = "solution.py"
check = "check.py"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ManifestError):
        load_manifest(suite_root)


def test_partition_guard_blocks_tuning_ledger() -> None:
    with pytest.raises(PartitionError):
        assert_results_path(".ai/local/ledgers/metrics.jsonl")

    assert_results_path(".ai/eval/results/arm-a.jsonl")


def test_seed_checks_are_well_formed(tmp_path: Path) -> None:
    manifest = load_manifest(SUITE_ROOT)
    solutions = {
        "sum-list": {
            True: "def sum_list(nums):\n    return sum(nums)\n",
            False: "def sum_list(nums):\n    return 0\n",
        },
        "reverse-words": {
            True: "def reverse_words(s):\n    return ' '.join(reversed(s.split()))\n",
            False: "def reverse_words(s):\n    return s\n",
        },
        # The False solution for each hard task is a plausible naive attempt that
        # passes the happy path but trips the trap the check is designed to catch.
        "compare-version": {
            True: (
                "def compare_version(a, b):\n"
                "    pa = [int(x) for x in a.split('.')]\n"
                "    pb = [int(x) for x in b.split('.')]\n"
                "    n = max(len(pa), len(pb))\n"
                "    pa += [0] * (n - len(pa))\n"
                "    pb += [0] * (n - len(pb))\n"
                "    return (pa > pb) - (pa < pb)\n"
            ),
            # lexicographic string compare: '1.10' < '1.9' is wrong
            False: "def compare_version(a, b):\n    return (a > b) - (a < b)\n",
        },
        "flatten": {
            True: (
                "def flatten(xs):\n"
                "    out = []\n"
                "    for x in xs:\n"
                "        if isinstance(x, list):\n"
                "            out.extend(flatten(x))\n"
                "        else:\n"
                "            out.append(x)\n"
                "    return out\n"
            ),
            # one level only: deep nesting is left unflattened
            False: (
                "def flatten(xs):\n"
                "    out = []\n"
                "    for x in xs:\n"
                "        if isinstance(x, list):\n"
                "            out.extend(x)\n"
                "        else:\n"
                "            out.append(x)\n"
                "    return out\n"
            ),
        },
        "roman-to-int": {
            True: (
                "def roman_to_int(s):\n"
                "    vals = {'I': 1, 'V': 5, 'X': 10, 'L': 50,\n"
                "            'C': 100, 'D': 500, 'M': 1000}\n"
                "    total = 0\n"
                "    prev = 0\n"
                "    for ch in reversed(s):\n"
                "        v = vals[ch]\n"
                "        if v < prev:\n"
                "            total -= v\n"
                "        else:\n"
                "            total += v\n"
                "            prev = v\n"
                "    return total\n"
            ),
            # pure sum ignores subtractive notation: 'IV' becomes 6
            False: (
                "def roman_to_int(s):\n"
                "    vals = {'I': 1, 'V': 5, 'X': 10, 'L': 50,\n"
                "            'C': 100, 'D': 500, 'M': 1000}\n"
                "    return sum(vals[ch] for ch in s)\n"
            ),
        },
        "is-balanced": {
            True: (
                "def is_balanced(s):\n"
                "    pairs = {')': '(', ']': '[', '}': '{'}\n"
                "    openers = set(pairs.values())\n"
                "    stack = []\n"
                "    for ch in s:\n"
                "        if ch in openers:\n"
                "            stack.append(ch)\n"
                "        elif ch in pairs:\n"
                "            if not stack or stack.pop() != pairs[ch]:\n"
                "                return False\n"
                "    return not stack\n"
            ),
            # counts only, ignores nesting order: '([)]' wrongly passes
            False: (
                "def is_balanced(s):\n"
                "    return (\n"
                "        s.count('(') == s.count(')')\n"
                "        and s.count('[') == s.count(']')\n"
                "        and s.count('{') == s.count('}')\n"
                "    )\n"
            ),
        },
        "int-to-roman": {
            True: (
                "def int_to_roman(n):\n"
                "    table = [(1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),\n"
                "             (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),\n"
                "             (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')]\n"
                "    out = []\n"
                "    for val, sym in table:\n"
                "        while n >= val:\n"
                "            out.append(sym)\n"
                "            n -= val\n"
                "    return ''.join(out)\n"
            ),
            # additive only, no subtractive pairs: 4 becomes 'IIII'
            False: (
                "def int_to_roman(n):\n"
                "    table = [(1000, 'M'), (500, 'D'), (100, 'C'),\n"
                "             (50, 'L'), (10, 'X'), (5, 'V'), (1, 'I')]\n"
                "    out = []\n"
                "    for val, sym in table:\n"
                "        while n >= val:\n"
                "            out.append(sym)\n"
                "            n -= val\n"
                "    return ''.join(out)\n"
            ),
        },
        "json-pointer": {
            True: (
                "def json_pointer_get(doc, pointer):\n"
                "    if pointer == '':\n"
                "        return doc\n"
                "    cur = doc\n"
                "    for raw in pointer.split('/')[1:]:\n"
                "        token = raw.replace('~1', '/').replace('~0', '~')\n"
                "        if isinstance(cur, list):\n"
                "            cur = cur[int(token)]\n"
                "        else:\n"
                "            cur = cur[token]\n"
                "    return cur\n"
            ),
            # ignores ~0/~1 escaping: key 'x/y' addressed as '/x~1y' is missed
            False: (
                "def json_pointer_get(doc, pointer):\n"
                "    if pointer == '':\n"
                "        return doc\n"
                "    cur = doc\n"
                "    for token in pointer.split('/')[1:]:\n"
                "        if isinstance(cur, list):\n"
                "            cur = cur[int(token)]\n"
                "        else:\n"
                "            cur = cur[token]\n"
                "    return cur\n"
            ),
        },
    }

    for task in manifest.all():
        if task.kind != "single":
            continue  # project tasks are exercised by test_project_task_well_formed
        task_dir = SUITE_ROOT / task.path
        for should_pass, source in solutions[task.id].items():
            workdir = tmp_path / task.id / str(should_pass)
            workdir.mkdir(parents=True)
            (workdir / task.entrypoint).write_text(source, encoding="utf-8")
            shutil.copyfile(task_dir / task.check, workdir / task.check)

            completed = subprocess.run(
                [sys.executable, task.check],
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            if should_pass:
                assert completed.returncode == 0
            else:
                assert completed.returncode != 0


_KV_STORE_GOOD = (
    "from __future__ import annotations\n"
    "import json\n\n\n"
    "class KVStore:\n"
    "    def __init__(self):\n"
    "        self._data = {}\n"
    "    def get(self, key):\n"
    "        return self._data.get(key)\n"
    "    def set(self, key, value):\n"
    "        self._data[key] = value\n"
    "    def delete(self, key):\n"
    "        self._data.pop(key, None)\n"
    "    def keys(self):\n"
    "        return sorted(self._data)\n"
    "    def incr(self, key, by=1):\n"
    "        value = self._data.get(key, 0) + by\n"
    "        self._data[key] = value\n"
    "        return value\n"
    "    def save(self, path):\n"
    "        with open(path, 'w', encoding='utf-8') as fh:\n"
    "            json.dump(self._data, fh)\n"
    "    def load(self, path):\n"
    "        with open(path, encoding='utf-8') as fh:\n"
    "            self._data = json.load(fh)\n"
)

_BACKUP_GOOD = (
    "from __future__ import annotations\n"
    "from store import KVStore\n\n\n"
    "def backup(store, path):\n"
    "    store.save(path)\n\n\n"
    "def restore(path):\n"
    "    s = KVStore()\n"
    "    s.load(path)\n"
    "    return s\n"
)

# Breaks the existing delete() behavior — the regression part of the check bites.
_KV_STORE_REGRESSION = _KV_STORE_GOOD.replace(
    "    def delete(self, key):\n        self._data.pop(key, None)\n",
    "    def delete(self, key):\n        pass\n",
)


def _run_project_check(tmp_path: Path, store_src: str, backup_src: str) -> int:
    task = next(t for t in load_manifest(SUITE_ROOT).all() if t.id == "kv-store")
    task_dir = SUITE_ROOT / task.path
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True)
    # seed is copied into the workdir, then the "agent" writes its files
    shutil.copytree(task_dir / task.seed, workdir, dirs_exist_ok=True)
    (workdir / "store.py").write_text(store_src, encoding="utf-8")
    (workdir / "backup.py").write_text(backup_src, encoding="utf-8")
    shutil.copyfile(task_dir / task.check, workdir / task.check)
    return subprocess.run(
        [sys.executable, task.check],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    ).returncode


def test_project_task_well_formed(tmp_path: Path) -> None:
    # a correct multi-file solution passes
    assert _run_project_check(tmp_path / "good", _KV_STORE_GOOD, _BACKUP_GOOD) == 0
    # breaking the existing delete() behavior is caught by the regression check
    assert _run_project_check(tmp_path / "bad", _KV_STORE_REGRESSION, _BACKUP_GOOD) != 0


def test_manifest_kind_and_seed_validation(tmp_path: Path) -> None:
    suite_root = tmp_path / "suite"
    task_dir = suite_root / "tasks" / "proj"
    (task_dir / "seed").mkdir(parents=True)
    (task_dir / "check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")

    def write_manifest(body: str) -> None:
        (suite_root / "manifest.toml").write_text(body, encoding="utf-8")

    # project task without entrypoint loads fine; seed recognized
    write_manifest(
        'version = 1\n\n[[tasks]]\nid = "proj"\npartition = "tuning"\n'
        'path = "tasks/proj"\nkind = "project"\ncheck = "check.py"\nseed = "seed"\n'
    )
    task = load_manifest(suite_root).tuning()[0]
    assert task.kind == "project"
    assert task.entrypoint == ""
    assert task.seed == "seed"

    # invalid kind is rejected
    write_manifest(
        'version = 1\n\n[[tasks]]\nid = "proj"\npartition = "tuning"\n'
        'path = "tasks/proj"\nkind = "bogus"\ncheck = "check.py"\n'
    )
    with pytest.raises(ManifestError):
        load_manifest(suite_root)

    # seed pointing at a missing dir is rejected
    write_manifest(
        'version = 1\n\n[[tasks]]\nid = "proj"\npartition = "tuning"\n'
        'path = "tasks/proj"\nkind = "project"\ncheck = "check.py"\nseed = "nope"\n'
    )
    with pytest.raises(ManifestError):
        load_manifest(suite_root)


def _kv_store_task():
    return next(t for t in load_manifest(SUITE_ROOT).all() if t.id == "kv-store")


def test_run_arm_a_project_writes_files(tmp_path: Path) -> None:
    task = _kv_store_task()
    workdir = tmp_path / "work"

    def invoke(prompt: str) -> InvokeResult:
        # the agent writes real files into the workdir (no single entrypoint)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "store.py").write_text(_KV_STORE_GOOD, encoding="utf-8")
        (workdir / "backup.py").write_text(_BACKUP_GOOD, encoding="utf-8")
        return InvokeResult(text="wrote files", tokens_in=None, tokens_out=None)

    result = run_arm_a(task, SUITE_ROOT, invoke, workdir)
    assert result.success is True
    assert result.task_id == "kv-store"
    # the seed file was made available before the agent ran
    assert (workdir / "store.py").exists()


def test_run_arm_c_project_recovers_after_fix(tmp_path: Path) -> None:
    task = _kv_store_task()
    workdir = tmp_path / "work"

    def phase_runner(phase_name: str, prompt: str) -> PhaseResult:
        if phase_name == "execute":
            # first attempt breaks the existing delete() -> regression check fails
            (workdir / "store.py").write_text(_KV_STORE_REGRESSION, encoding="utf-8")
            (workdir / "backup.py").write_text(_BACKUP_GOOD, encoding="utf-8")
            return PhaseResult(text="broken", tokens_in=None, tokens_out=None)
        if phase_name == "fix":
            # the fix phase, seeing the failing check, writes the correct version
            (workdir / "store.py").write_text(_KV_STORE_GOOD, encoding="utf-8")
            return PhaseResult(text="fixed", tokens_in=None, tokens_out=None)
        return PhaseResult(text="", tokens_in=None, tokens_out=None)

    result = run_arm_c(task, SUITE_ROOT, phase_runner, workdir)
    assert result.success is True  # arm c recovered via the gate-fix loop


def test_run_arm_a_success_and_failure(tmp_path: Path) -> None:
    task = load_manifest(SUITE_ROOT).tuning()[0]

    def correct_invoke(prompt: str) -> InvokeResult:
        assert "sum_list" in prompt
        return InvokeResult(
            text="def sum_list(nums):\n    return sum(nums)\n",
            tokens_in=12,
            tokens_out=8,
        )

    success = run_arm_a(task, SUITE_ROOT, correct_invoke, tmp_path / "success")
    assert success.success is True
    assert success.arm == "a"
    assert success.task_id == "sum-list"
    assert success.partition == "tuning"
    assert success.tokens_in == 12
    assert success.tokens_out == 8
    assert isinstance(success.duration_ms, int)
    assert success.duration_ms >= 0

    def bad_invoke(prompt: str) -> InvokeResult:
        assert "sum_list" in prompt
        return InvokeResult(text="not python", tokens_in=None, tokens_out=None)

    failure = run_arm_a(task, SUITE_ROOT, bad_invoke, tmp_path / "failure")
    assert failure.success is False
    assert failure.duration_ms >= 0
