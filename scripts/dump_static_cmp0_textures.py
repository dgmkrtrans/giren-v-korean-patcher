#!/usr/bin/env python3
"""Dump static TX/PL textures and PSET CMP0-compressed TX textures together.

This is a convenience wrapper around ``dump_static_textures.py`` and
``dump_cmp0_textures.py``.  It keeps the static dumper's manifest schema, then
appends decoded CMP0 textures from PSET ranges into the same output tree.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import dump_cmp0_textures as cmp0_dump
import dump_static_textures as static_dump


# Integrated static + CMP0 target list.  Keep the existing static text targets
# here, then append CMP0 ordinal ranges as they are identified.
TEXT_TARGET_RULES: tuple[static_dump.TextTargetRule, ...] = static_dump.TEXT_TARGET_RULES


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="unpacked_mkd", help="directory or file containing extracted resources")
    parser.add_argument("--out", default="textures_static_cmp0", help="output directory for decoded PNGs")
    parser.add_argument(
        "--categories",
        action="store_true",
        help="split the default verified text dump into docs/task.md category folders",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="dump every discovered texture using heuristic text/ui/graphics folders",
    )
    parser.add_argument("--max-files", type=int, default=None, help="only scan the first N static resource files")
    parser.add_argument("--no-dedupe", action="store_true", help="write every decoded static texture instance")
    parser.add_argument(
        "--suffix-only",
        action="store_true",
        help="only scan .mrg/.pse files for normal static TX/PL resources",
    )
    parser.add_argument(
        "--include-loose",
        action="store_true",
        help="also try nearest-palette decoding for valid TX records outside recognized containers",
    )
    parser.add_argument("--skip-raw-png", action="store_true", help="do not copy already-embedded PNG files")
    parser.add_argument(
        "--skip-cmp0",
        action="store_true",
        help="only run the normal static TX/PL dumper",
    )
    parser.add_argument(
        "--cmp0-list",
        type=Path,
        default=None,
        help="optional newline-delimited file list, relative to --source, for CMP0 scanning",
    )
    parser.add_argument("--clean", action="store_true", help="remove the output directory before dumping")
    return parser.parse_args()


def cmp0_to_static_record(
    record: cmp0_dump.Cmp0Record,
    ordinal: int,
    verified_group: str = "",
) -> static_dump.TextureRecord:
    return static_dump.TextureRecord(
        source=record.source,
        tree_path=f"/cmp0/pset/{record.pset_index}/{record.cmp0_index}",
        offset=record.cmp0_offset if record.cmp0_offset is not None else record.offset,
        width=record.width,
        height=record.height,
        palette_colors=record.palette_colors,
        bpp=record.bpp,
        category=record.category,
        verified_group=verified_group,
        ordinal=ordinal,
        pattern="generic",
        palette_profile=record.palette_profile,
        palette_order=record.palette_order,
        palette_offset=record.palette_offset,
        storage_width=record.width,
        storage_height=record.height,
        layout="linear_cmp0",
        layout_offset=0,
        sha1=record.sha1,
        output=record.output,
        duplicate=False,
        alpha_pixels=record.alpha_pixels,
        opaque_pixels=record.opaque_pixels,
        bbox=record.bbox,
        output_group="",
        output_group_part=0,
        output_group_parts=1,
        output_crop_x=0,
        output_crop_y=0,
        output_crop_width=record.width,
        output_crop_height=record.height,
        output_clear_rects="",
        dialogue_line_control_offset=0,
        dialogue_line_count=0,
        dialogue_line_lengths="",
        dialogue_speaker_id="",
    )


def cmp0_tree_path(record: cmp0_dump.Cmp0Record) -> str:
    return f"/cmp0/pset/{record.pset_index}/{record.cmp0_index}"


def cmp0_verified_text_group(record: cmp0_dump.Cmp0Record, ordinal: int) -> str | None:
    tree_path = cmp0_tree_path(record)
    for rule in TEXT_TARGET_RULES:
        if static_dump.ordinal_in_ranges(ordinal, rule.ranges) and static_dump.predicate_matches(
            rule.predicate,
            tree_path,
            record.width,
            record.height,
        ):
            return rule.name
    return None


def remove_empty_parent_dirs(path: Path, stop: Path) -> None:
    current = path.parent
    while current != stop and current != current.parent:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def count_raw_pngs(source_root: Path) -> int:
    if source_root.is_file():
        candidates = [source_root] if source_root.suffix.lower() == ".png" else []
    else:
        candidates = sorted(source_root.rglob("*.png"))
    return sum(1 for path in candidates if path.read_bytes().startswith(static_dump.PNG_MAGIC))


def keep_cmp0_text_targets(
    out_root: Path,
    records: list[cmp0_dump.Cmp0Record],
    start_ordinal: int,
    categorized_text: bool,
) -> list[tuple[cmp0_dump.Cmp0Record, int, str]]:
    selected: list[tuple[cmp0_dump.Cmp0Record, int, str]] = []
    for ordinal, record in enumerate(records, start=start_ordinal):
        group = cmp0_verified_text_group(record, ordinal)
        output_path = out_root / record.output
        if group is None:
            if output_path.exists():
                output_path.unlink()
                remove_empty_parent_dirs(output_path, out_root)
            continue

        category = static_dump.safe_category_name(group) if categorized_text else "text"
        old_output = Path(record.output)
        new_output = Path(category) / old_output.name
        new_path = out_root / new_output
        if old_output != new_output:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists():
                output_path.rename(new_path)
                remove_empty_parent_dirs(output_path, out_root)
        record.category = category
        record.output = str(new_output)
        selected.append((record, ordinal, group))
    return selected


def add_cmp0_read_order_prefixes(
    out_root: Path,
    records: list[cmp0_dump.Cmp0Record],
    start_ordinal: int,
) -> None:
    for ordinal, record in enumerate(records, start=start_ordinal):
        output = Path(record.output)
        if output.name.startswith(f"{ordinal:06d}-"):
            continue

        old_path = out_root / output
        new_output = output.with_name(f"{ordinal:06d}-{output.name}")
        new_path = out_root / new_output
        if old_path != new_path:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(new_path)
        record.output = str(new_output)


def main() -> None:
    args = parse_args()
    source_root = Path(args.source)
    out_root = Path(args.out)

    if not source_root.exists():
        raise SystemExit(f"source directory does not exist: {source_root}")
    if args.clean and out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    static_dump.TEXT_TARGET_RULES = TEXT_TARGET_RULES
    static_records = static_dump.dump_mrg_textures(
        source_root=source_root,
        out_root=out_root,
        max_files=args.max_files,
        dedupe=not args.no_dedupe,
        dump_all=args.all,
        categorized_text=args.categories,
        scan_all_files=not args.suffix_only,
        include_loose=args.include_loose,
    )
    static_all_next_ordinal = static_dump.LAST_DISCOVERY_NEXT_ORDINAL
    if args.all and not args.skip_raw_png:
        static_dump.copy_raw_pngs(source_root, out_root, static_records, {"raw_png"})

    cmp0_records: list[cmp0_dump.Cmp0Record] = []
    if not args.skip_cmp0:
        cmp0_files = cmp0_dump.input_files(source_root, args.cmp0_list)
        cmp0_records = cmp0_dump.dump_cmp0_textures(
            source_root,
            cmp0_files,
            out_root,
            include_plain_tx=False,
        )

    if args.all:
        next_ordinal = max((record.ordinal for record in static_records), default=0) + 1
    else:
        next_ordinal = static_all_next_ordinal
        if not args.skip_raw_png:
            next_ordinal += count_raw_pngs(source_root)
    add_cmp0_read_order_prefixes(out_root, cmp0_records, next_ordinal)
    combined_records = list(static_records)
    if args.all:
        selected_cmp0_records = [
            (cmp0_record, next_ordinal + index, "")
            for index, cmp0_record in enumerate(cmp0_records)
        ]
    else:
        selected_cmp0_records = keep_cmp0_text_targets(
            out_root,
            cmp0_records,
            next_ordinal,
            categorized_text=args.categories,
        )
    for cmp0_record, ordinal, verified_group in selected_cmp0_records:
        combined_records.append(cmp0_to_static_record(cmp0_record, ordinal, verified_group))

    static_dump.write_manifest(out_root, combined_records)

    unique = sum(1 for record in combined_records if not record.duplicate)
    print(f"Decoded static texture instances: {len(static_records)}")
    print(f"Decoded CMP0 texture instances: {len(cmp0_records)}")
    if not args.all:
        print(f"Selected CMP0 text targets: {len(selected_cmp0_records)}")
    print(f"Combined texture instances: {len(combined_records)}")
    print(f"Unique PNG outputs: {unique}")
    print(f"Output directory: {out_root}")
    print(f"Manifest: {out_root / 'manifest.csv'}")


if __name__ == "__main__":
    main()
