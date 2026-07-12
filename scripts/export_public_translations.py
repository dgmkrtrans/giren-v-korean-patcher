#!/usr/bin/env python3
"""Export Korean-only public datasets from local manifests and dictionaries."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from public_translation_keys import fonttile_translation_key, texture_translation_key


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV header not found: {path}")
        return list(reader)


def export_textures(source: Path, output: Path) -> int:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for source_row in read_rows(source):
        korean = source_row.get("korean") or ""
        if not korean.strip():
            continue
        key = texture_translation_key(source_row)
        if key in seen:
            raise ValueError(f"duplicate texture translation key: {key}")
        seen.add(key)
        rows.append(
            {
                "translation_key": key,
                "korean": korean,
                "dialogue_line_lengths": source_row.get("dialogue_line_lengths") or "",
            }
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["translation_key", "korean", "dialogue_line_lengths"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def export_fonttile(source: Path, output: Path) -> int:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for source_row in read_rows(source):
        korean = source_row.get("translation") or ""
        if not korean.strip():
            continue
        key = fonttile_translation_key(source_row.get("original") or "")
        if key in seen:
            raise ValueError(f"duplicate fonttile translation key: {key}")
        seen.add(key)
        rows.append({"translation_key": key, "korean": korean})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["translation_key", "korean"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--texture-manifest", type=Path)
    parser.add_argument(
        "--texture-output",
        type=Path,
        default=Path("patch_data/texture_translations.csv"),
    )
    parser.add_argument("--fonttile-dictionary", type=Path)
    parser.add_argument(
        "--fonttile-output",
        type=Path,
        default=Path("patch_data/fonttile_translations.csv"),
    )
    args = parser.parse_args()
    if args.texture_manifest is None and args.fonttile_dictionary is None:
        parser.error("provide --texture-manifest and/or --fonttile-dictionary")
    if args.texture_manifest is not None:
        count = export_textures(args.texture_manifest, args.texture_output)
        print(f"exported {count} Korean-only texture translations to {args.texture_output}")
    if args.fonttile_dictionary is not None:
        count = export_fonttile(args.fonttile_dictionary, args.fonttile_output)
        print(f"exported {count} Korean-only fonttile translations to {args.fonttile_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
