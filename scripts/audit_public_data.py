#!/usr/bin/env python3
"""Fail when public translation datasets contain source-text fields or invalid keys."""

from __future__ import annotations

import csv
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KEY_RE = re.compile(r"[0-9a-f]{64}")
FORBIDDEN_PATHS = (
    ROOT / "patch_data/fonttile_text_dictionary.csv",
    ROOT / "scripts/tile_text/part_auto_translation_map.json",
)
DATASETS = (
    (
        ROOT / "patch_data/texture_translations.csv",
        ("translation_key", "korean", "dialogue_line_lengths"),
        frozenset(),
    ),
    (
        ROOT / "patch_data/fonttile_translations.csv",
        ("translation_key", "korean"),
        frozenset({"･"}),
    ),
)


def audit_dataset(
    path: Path,
    expected_header: tuple[str, ...],
    allowed_japanese_chars: frozenset[str],
) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != expected_header:
            raise ValueError(
                f"{path}: expected header {expected_header}, got {reader.fieldnames}"
            )
        seen: set[str] = set()
        count = 0
        for line_number, row in enumerate(reader, start=2):
            key = row["translation_key"]
            if KEY_RE.fullmatch(key) is None:
                raise ValueError(f"{path}:{line_number}: invalid translation_key")
            if key in seen:
                raise ValueError(f"{path}:{line_number}: duplicate translation_key")
            if not row["korean"].strip():
                raise ValueError(f"{path}:{line_number}: empty Korean translation")
            disallowed = {
                char
                for char in row["korean"]
                if (
                    "\u3040" <= char <= "\u30ff"
                    or "\uff61" <= char <= "\uff9f"
                )
                and char not in allowed_japanese_chars
            }
            if disallowed:
                chars = "".join(sorted(disallowed))
                raise ValueError(
                    f"{path}:{line_number}: Japanese-script character(s): {chars}"
                )
            seen.add(key)
            count += 1
    return count


def main() -> int:
    leaked = [str(path.relative_to(ROOT)) for path in FORBIDDEN_PATHS if path.exists()]
    if leaked:
        raise SystemExit(f"forbidden source-text data found: {', '.join(leaked)}")
    for path, header, allowed_japanese_chars in DATASETS:
        count = audit_dataset(path, header, allowed_japanese_chars)
        print(f"ok: {path.relative_to(ROOT)} ({count} rows)")
    print("public translation data contains only approved hash/Korean fields")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
