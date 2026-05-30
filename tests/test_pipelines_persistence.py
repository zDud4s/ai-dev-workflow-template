"""Foundation assertions: .ai/pipelines/ exists and is wired into ignore/project rules."""
from __future__ import annotations
import pathlib
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_pipelines_gitkeep_tracked() -> None:
    p = REPO_ROOT / ".ai" / "pipelines" / ".gitkeep"
    assert p.is_file(), ".ai/pipelines/.gitkeep must exist and be tracked"


def test_gitignore_ignores_pipelines_but_keeps_gitkeep() -> None:
    g = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".ai/pipelines/*" in g, "gitignore must ignore .ai/pipelines/* contents"
    assert "!.ai/pipelines/.gitkeep" in g, "gitignore must unignore .gitkeep"


def test_project_yaml_lists_pipelines_in_generated_files() -> None:
    d = yaml.safe_load((REPO_ROOT / ".ai" / "project.yaml").read_text(encoding="utf-8"))
    gen = d["boundaries"]["generated_files"]
    assert ".ai/pipelines" in gen, f"expected .ai/pipelines in generated_files, got {gen}"
