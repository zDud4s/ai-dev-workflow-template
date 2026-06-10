from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on py<3.11
    tomllib = None  # type: ignore[assignment]


VALID_PARTITIONS = {"tuning", "held-out"}


class ManifestError(Exception):
    """Raised when an eval manifest is missing or malformed."""


@dataclass(frozen=True)
class Task:
    id: str
    partition: str
    path: str
    entrypoint: str
    check: str


class Manifest:
    def __init__(self, tasks: list[Task]) -> None:
        self._tasks = tuple(tasks)

    def all(self) -> list[Task]:
        return list(self._tasks)

    def tuning(self) -> list[Task]:
        return [task for task in self._tasks if task.partition == "tuning"]

    def held_out(self) -> list[Task]:
        return [task for task in self._tasks if task.partition == "held-out"]


def load_manifest(suite_root: str | Path) -> Manifest:
    root = Path(suite_root)
    manifest_path = root / "manifest.toml"
    if not manifest_path.exists():
        raise ManifestError(f"missing manifest: {manifest_path}")

    data = _load_manifest_data(manifest_path)
    if "version" not in data:
        raise ManifestError("manifest missing version")

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ManifestError("manifest tasks must be a list")

    seen: set[str] = set()
    tasks: list[Task] = []
    for index, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, dict):
            raise ManifestError(f"task #{index + 1} must be a table")
        task = _task_from_table(raw_task, index)
        if task.id in seen:
            raise ManifestError(f"duplicate task id: {task.id}")
        seen.add(task.id)
        if task.partition not in VALID_PARTITIONS:
            raise ManifestError(f"invalid partition for {task.id}: {task.partition}")

        task_dir = root / task.path
        if not task_dir.exists() or not task_dir.is_dir():
            raise ManifestError(f"missing task path for {task.id}: {task_dir}")
        check_path = task_dir / task.check
        if not check_path.exists() or not check_path.is_file():
            raise ManifestError(f"missing check file for {task.id}: {check_path}")
        tasks.append(task)

    return Manifest(tasks)


def _task_from_table(raw_task: dict[str, Any], index: int) -> Task:
    fields = {}
    for key in ("id", "partition", "path", "entrypoint", "check"):
        value = raw_task.get(key)
        if not isinstance(value, str) or not value:
            raise ManifestError(f"task #{index + 1} missing string field: {key}")
        fields[key] = value
    return Task(**fields)


def _load_manifest_data(path: Path) -> dict[str, Any]:
    if tomllib is not None:
        try:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ManifestError(f"invalid TOML: {exc}") from exc
    return _parse_flat_manifest(path.read_text(encoding="utf-8"))


def _parse_flat_manifest(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    tasks: list[dict[str, Any]] = []
    current_task: dict[str, Any] | None = None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line == "[[tasks]]":
            current_task = {}
            tasks.append(current_task)
            continue
        if "=" not in line:
            raise ManifestError(f"invalid manifest line: {raw_line}")
        key, raw_value = [part.strip() for part in line.split("=", 1)]
        value = _parse_scalar(raw_value)
        if current_task is None:
            data[key] = value
        else:
            current_task[key] = value

    if tasks:
        data["tasks"] = tasks
    return data


def _parse_scalar(raw_value: str) -> str | int:
    if raw_value.startswith('"') and raw_value.endswith('"'):
        return raw_value[1:-1]
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ManifestError(f"unsupported manifest value: {raw_value}") from exc
