#!/usr/bin/env python3
"""Merge the public Korean-only translations into a locally generated dictionary."""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

from public_translation_keys import fonttile_translation_key


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV header not found: {path}")
        return list(reader.fieldnames), list(reader)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--translations",
        type=Path,
        default=Path("patch_data/fonttile_translations.csv"),
    )
    parser.add_argument(
        "--dictionary",
        type=Path,
        default=Path("results/fonttile_text_dictionary.csv"),
    )
    args = parser.parse_args()

    translation_headers, translation_rows = read_csv(args.translations)
    dictionary_headers, dictionary_rows = read_csv(args.dictionary)
    if not {"translation_key", "korean"}.issubset(translation_headers):
        raise ValueError(f"{args.translations}: translation_key and korean are required")
    if "original" not in dictionary_headers:
        raise ValueError(f"{args.dictionary}: original column is required")
    if "translation" not in dictionary_headers:
        dictionary_headers.append("translation")

    translations: dict[str, str] = {}
    for line_number, row in enumerate(translation_rows, start=2):
        key = (row.get("translation_key") or "").strip()
        korean = row.get("korean") or ""
        if len(key) != 64:
            raise ValueError(f"{args.translations}:{line_number}: invalid translation_key")
        if key in translations:
            raise ValueError(f"{args.translations}:{line_number}: duplicate translation_key")
        translations[key] = korean

    matched: set[str] = set()
    for row in dictionary_rows:
        key = fonttile_translation_key(row.get("original") or "")
        if key in translations:
            row["translation"] = translations[key]
            matched.add(key)
    missing = sorted(set(translations) - matched)
    if missing:
        raise ValueError(
            f"{len(missing)} fonttile translation keys are missing from the generated dictionary; "
            f"first: {missing[0]}"
        )

    temp_path = args.dictionary.with_suffix(args.dictionary.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=dictionary_headers, lineterminator="\n")
        writer.writeheader()
        writer.writerows(dictionary_rows)
    os.replace(temp_path, args.dictionary)
    print(f"merged {len(translations)} Korean fonttile translations into {args.dictionary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
