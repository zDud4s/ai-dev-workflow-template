"""Dry-run or purge stale Claude Code transcripts created by skill improvers."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from server.improver import _transcript_policy as policy

PURGE_BUCKETS = {"orphan", "resolved", "unmatched_pre_audit"}


def _default_project_dir() -> Path | None:
    dashboard_dir = Path(__file__).resolve().parents[1]
    if str(dashboard_dir) not in sys.path:
        sys.path.insert(0, str(dashboard_dir))
    try:
        import serve  # type: ignore
    except (ImportError, AttributeError) as e:
        print(f"could not import serve helper: {e}", file=sys.stderr)
        return None
    return serve._transcripts_dir_for_cwd(Path.cwd())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify and optionally delete stale auto-improver transcripts.",
    )
    parser.add_argument("--apply", action="store_true", help="delete non-keep buckets")
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="Claude project transcript directory (default: serve.py helper for cwd)",
    )
    parser.add_argument("--days", type=int, default=7, help="staleness age in days")
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path(".ai") / "ledgers" / "improvements.jsonl",
        help="improvements ledger path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # A non-positive --days makes the staleness threshold <= 0, classifying
    # every improver transcript as stale and routing recent ones into purge
    # buckets — under --apply that deletes transcripts the default window is
    # meant to protect. Reject before mutating policy.STALE_DAYS.
    if args.days < 1:
        print("--days must be a positive integer (>= 1)", file=sys.stderr)
        return 2
    project_dir = args.project_dir or _default_project_dir()
    if project_dir is None:
        print("could not resolve Claude project transcript directory", file=sys.stderr)
        return 2

    old_stale_days = policy.STALE_DAYS
    policy.STALE_DAYS = int(args.days)
    try:
        ledger_rows = policy.load_ledger_rows(args.ledger)
        counts = {"orphan": 0, "resolved": 0, "unmatched_pre_audit": 0, "keep": 0}
        rows: list[tuple[str, Path]] = []
        now = time.time()
        for path in sorted(Path(project_dir).glob("*.jsonl")):
            bucket = policy.classify_transcript(path, ledger_rows, now)
            counts[bucket] += 1
            rows.append((bucket, path))

        removed = 0
        print("bucket\tfile")
        for bucket, path in rows:
            print(f"{bucket}\t{path}")
            if args.apply and bucket in PURGE_BUCKETS:
                try:
                    path.unlink()
                    removed += 1
                except OSError as e:
                    print(f"failed\t{path}\t{e}")

        total_candidates = counts["orphan"] + counts["resolved"] + counts["unmatched_pre_audit"]
        summary = (
            f"orphan={counts['orphan']} resolved={counts['resolved']} "
            f"unmatched_pre_audit={counts['unmatched_pre_audit']} keep={counts['keep']} "
            f"total_candidates={total_candidates}"
        )
        if args.apply:
            print(summary)
            print(f"removed={removed}")
        else:
            print(summary)
        return 0
    finally:
        policy.STALE_DAYS = old_stale_days


if __name__ == "__main__":
    raise SystemExit(main())
