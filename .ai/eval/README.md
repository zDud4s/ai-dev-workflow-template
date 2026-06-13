# Objective Eval Suite

This subtree contains a partitioned objective evaluation suite for workflow-arm
experiments.

- `suite/manifest.toml` registers tasks and their partitions.
- `suite/tasks/` contains task prompts and check scripts.
- `harness/` loads the manifest, enforces partition boundaries, and runs arm
  `a` with an injectable invoker.
- `results/` is the only intended location for eval-arm JSONL outputs.

The manifest is parsed with Python's standard library only. On Python 3.11+ the
harness uses `tomllib`; older runtimes use a small parser for this manifest's
flat TOML subset.
