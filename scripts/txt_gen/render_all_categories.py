#!/usr/bin/env python3
"""Run every defined Korean text render category in sequence."""

from __future__ import annotations

import argparse
import subprocess
import sys


def add_optional(args: list[str], flag: str, value: object) -> None:
    text = str(value or "").strip()
    if text:
        args.extend([flag, text])


def add_common_args(command: list[str], args: argparse.Namespace) -> None:
    for flag, value in (
        ("--csv", args.csv),
        ("--textures-root", args.textures_root),
        ("--out-root", args.out_root),
        ("--rows", args.rows),
        ("--limit", args.limit),
    ):
        add_optional(command, flag, value)
    if args.dry_run:
        command.append("--dry-run")
    if args.strict:
        command.append("--strict")
    if args.apply:
        command.append("--apply")
    if args.no_copy_manifest:
        command.append("--no-copy-manifest")
    if args.verbose:
        command.append("--verbose")


def run_step(title: str, command: list[str]) -> int:
    print(f"\n=== {title} ===", flush=True)
    print(" ".join(command), flush=True)
    process = subprocess.Popen(command)
    return process.wait()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render every webtool Render tab category.")
    parser.add_argument("--csv", default="textures_static/manifest.csv")
    parser.add_argument("--textures-root", default="textures_static")
    parser.add_argument("--out-root", default="textures_translated")
    parser.add_argument("--rows", "--row-range", dest="rows")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-copy-manifest", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="Print ok/warning render progress.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    steps = (
        ("대사들 렌더", "scripts/txt_gen/white_letter_transparent.py", ()),
        ("각 세력 오프닝 렌더", "scripts/txt_gen/white_letter_black_background.py", ()),
        ("각 세력 오프닝타이틀 렌더", "scripts/txt_gen/episode_title_generator.py", ()),
        ("고정 UI 텍스트 전체 렌더", "scripts/txt_gen/ui_text_fit_renderer.py", ()),
    )
    for title, script, extra_args in steps:
        command = [sys.executable, script]
        add_common_args(command, args)
        command.extend(extra_args)
        returncode = run_step(title, command)
        if returncode != 0:
            print(f"Error: {title} failed with exit code {returncode}", file=sys.stderr)
            return returncode
    print("\nAll render categories completed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
