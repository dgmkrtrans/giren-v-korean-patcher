#!/usr/bin/env python3
"""Merge the public Korean texture translations into a freshly dumped manifest."""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

from public_translation_keys import texture_translation_key

PATCH_COLUMNS = ("korean", "dialogue_line_lengths")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV header not found: {path}")
        return list(reader.fieldnames), list(reader)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--patch",
        type=Path,
        default=Path("patch_data/texture_translations.csv"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("textures_static/manifest.csv"),
    )
    args = parser.parse_args()

    patch_headers, patch_rows = read_csv(args.patch)
    manifest_headers, manifest_rows = read_csv(args.manifest)
    required_patch = {"translation_key", "korean"}
    required_manifest = {"source", "tree_path", "offset", "sha1"}
    missing_patch = sorted(required_patch - set(patch_headers))
    missing_manifest = sorted(required_manifest - set(manifest_headers))
    if missing_patch:
        raise ValueError(f"{args.patch}: missing columns: {', '.join(missing_patch)}")
    if missing_manifest:
        raise ValueError(f"{args.manifest}: missing columns: {', '.join(missing_manifest)}")

    patch_by_key: dict[str, dict[str, str]] = {}
    for line_number, row in enumerate(patch_rows, start=2):
        key = (row.get("translation_key") or "").strip()
        if len(key) != 64:
            raise ValueError(f"{args.patch}:{line_number}: invalid translation_key")
        if key in patch_by_key:
            raise ValueError(f"{args.patch}:{line_number}: duplicate patch key: {key}")
        patch_by_key[key] = row

    manifest_by_key: dict[str, dict[str, str]] = {}
    for line_number, row in enumerate(manifest_rows, start=2):
        key = texture_translation_key(row)
        if key in manifest_by_key:
            raise ValueError(
                f"{args.manifest}:{line_number}: duplicate generated translation key: {key}"
            )
        manifest_by_key[key] = row
    missing_keys = sorted(set(patch_by_key) - set(manifest_by_key))
    if missing_keys:
        raise ValueError(
            f"{len(missing_keys)} patch rows are missing from the generated manifest; "
            f"first: {missing_keys[0]}"
        )

    for column in PATCH_COLUMNS:
        if column not in manifest_headers:
            manifest_headers.append(column)
    for key, patch_row in patch_by_key.items():
        target = manifest_by_key[key]
        for column in PATCH_COLUMNS:
            if column in patch_headers:
                target[column] = patch_row.get(column, "")

    temp_path = args.manifest.with_suffix(args.manifest.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=manifest_headers, lineterminator="\n")
        writer.writeheader()
        writer.writerows(manifest_rows)
    os.replace(temp_path, args.manifest)
    print(f"merged {len(patch_rows)} texture translation rows into {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
