#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "skills" / "_shared" / "weex_risk_review_core.py"
TARGETS = (
    REPO_ROOT / "skills" / "weex-trader-skill" / "scripts" / "weex_risk_review_core.py",
    REPO_ROOT / "skills" / "weex-analysis-skill" / "scripts" / "weex_risk_review_core.py",
)


def sync(*, check: bool) -> int:
    expected = SOURCE.read_text(encoding="utf-8")
    mismatched: list[Path] = []

    for path in TARGETS:
        if not path.exists() or path.read_text(encoding="utf-8") != expected:
            mismatched.append(path)

    if check:
        if mismatched:
            print("Risk review core is out of sync:")
            for path in mismatched:
                print(path.relative_to(REPO_ROOT).as_posix())
            return 1
        print("Risk review core is in sync.")
        return 0

    for path in mismatched:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
        print(f"Updated {path.relative_to(REPO_ROOT).as_posix()}")

    if not mismatched:
        print("Risk review core already in sync.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync the shared WEEX risk review core into each installable skill.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail instead of writing when vendored copies are out of sync.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return sync(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
