"""Foundation assertions: .ai/local/pipelines/ exists and is wired into ignore/project rules."""
from __future__ import annotations
import pathlib
import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_pipelines_gitkeep_tracked() -> None:
    p = REPO_ROOT / ".ai" / "local" / "pipelines" / ".gitkeep"
    assert p.is_file(), ".ai/local/pipelines/.gitkeep must exist and be tracked"


def test_gitignore_ignores_pipelines_but_keeps_gitkeep() -> None:
    g = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".ai/local/pipelines/*" in g, "gitignore must ignore .ai/local/pipelines/* contents"
    assert "!.ai/local/pipelines/.gitkeep" in g, "gitignore must unignore .gitkeep"


def test_project_yaml_lists_pipelines_in_generated_files() -> None:
    # This asserts a property of THIS repo's working project.yaml, where .ai/ is
    # the product so .ai/local/pipelines is genuinely a generated dir. The working
    # file is gitignored (filled per project) and the shipped .template is blank
    # by design — downstream generated_files lists the HOST project's artifacts,
    # not the workflow's. So skip on a clean checkout/CI where the file is absent.
    working = REPO_ROOT / ".ai" / "project.yaml"
    if not working.exists():
        pytest.skip("working .ai/project.yaml absent (gitignored); template is intentionally blank")
    d = yaml.safe_load(working.read_text(encoding="utf-8"))
    gen = d["boundaries"]["generated_files"]
    assert ".ai/local/pipelines" in gen, f"expected .ai/local/pipelines in generated_files, got {gen}"
