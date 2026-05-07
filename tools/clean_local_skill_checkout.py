#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


NOISE_DIR_NAMES = {"__pycache__", ".pytest_cache"}
NOISE_FILE_NAMES = {".DS_Store"}
NOISE_FILE_SUFFIXES = {".pyc"}
SKIP_DIR_NAMES = {".git"}


def find_noise(root: Path) -> list[Path]:
    matches: list[Path] = []

    for path in root.rglob("*"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.name in NOISE_DIR_NAMES and path.is_dir():
            matches.append(path)
            continue
        if path.name in NOISE_FILE_NAMES and path.is_file():
            matches.append(path)
            continue
        if path.suffix in NOISE_FILE_SUFFIXES and path.is_file():
            matches.append(path)

    return sorted(matches)


def remove_noise(paths: list[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove local checkout packaging noise before installing skills from a source repository checkout.",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root to clean. Defaults to the current directory.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for packaging noise without deleting anything.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    noise = find_noise(root)

    if args.check:
        if not noise:
            print(f"Checkout is clean: {root}")
            return 0
        print(f"Packaging noise detected under {root}:")
        for path in noise:
            print(path.relative_to(root).as_posix())
        return 1

    if not noise:
        print(f"Nothing to clean under {root}")
        return 0

    remove_noise(noise)
    print(f"Removed {len(noise)} packaging artifact(s) under {root}")
    for path in noise:
        print(path.relative_to(root).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
