"""Tests for Codex run-pipeline dispatch defaults."""
from __future__ import annotations

from pathlib import Path

import yaml


def test_run_pipeline_codex_dispatch_keys_present() -> None:
    path = Path(__file__).resolve().parent.parent / ".ai" / "models.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    codex_dispatch = data["run_pipeline"]["codex_dispatch"]
    assert codex_dispatch["model"] == "gpt-5.5"
    assert codex_dispatch["reasoning_effort"] == "medium"
    assert codex_dispatch["timeout_seconds"] == 1800
