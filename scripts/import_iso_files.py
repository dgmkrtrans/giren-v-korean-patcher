#!/usr/bin/env python3
"""Import ISO9660 members or extracted raw PNGs into an ISO image in-place."""

from __future__ import annotations

import argparse
import mmap
import shutil
import sys
from pathlib import Path

from extract_mkd import PNG_MAGIC, align, png_length
from import_mkd import parse_iso9660


def parse_mapping(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("mapping must be ISO_PATH=LOCAL_PATH")
    iso_path, local_path = value.split("=", 1)
    iso_path = iso_path.strip().strip("/")
    if not iso_path:
        raise argparse.ArgumentTypeError("ISO_PATH is empty")
    return iso_path, Path(local_path)


def find_pngs(data: mmap.mmap) -> list[tuple[int, int]]:
    """Return (offset, byte length) for every complete PNG embedded in the ISO."""
    pngs: list[tuple[int, int]] = []
    cursor = 0
    while True:
        offset = data.find(PNG_MAGIC, cursor)
        if offset < 0:
            return pngs
        try:
            length = png_length(data, offset)
        except ValueError:
            cursor = offset + 1
            continue
        pngs.append((offset, length))
        cursor = offset + length


def translated_pngs(directory: Path) -> list[tuple[int, Path]]:
    if not directory.is_dir():
        raise ValueError(f"PNG directory not found: {directory}")
    results: list[tuple[int, Path]] = []
    seen: set[int] = set()
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".png":
            continue
        try:
            index = int(path.stem, 16)
        except ValueError as exc:
            raise ValueError(f"PNG filename must be an 8-digit hexadecimal index: {path.name}") from exc
        if len(path.stem) != 8 or index in seen:
            raise ValueError(f"invalid or duplicate PNG index: {path.name}")
        seen.add(index)
        results.append((index, path))
    if not results:
        raise ValueError(f"no indexed PNG files found in: {directory}")
    return results


def import_raw_pngs(iso, directory: Path) -> None:
    replacements = translated_pngs(directory)
    with mmap.mmap(iso.fileno(), 0, access=mmap.ACCESS_WRITE) as data:
        embedded = find_pngs(data)
        for index, local_path in replacements:
            if index >= len(embedded):
                raise ValueError(
                    f"PNG index {index:08x} is out of range; ISO contains {len(embedded)} PNGs"
                )
            offset, original_length = embedded[index]
            replacement = local_path.read_bytes()
            if not replacement.startswith(PNG_MAGIC):
                raise ValueError(f"not a PNG file: {local_path}")
            if png_length(replacement, 0) != len(replacement):
                raise ValueError(f"PNG has trailing data: {local_path}")
            if len(replacement) > original_length:
                raise ValueError(
                    f"replacement is larger than original for {local_path.name}: "
                    f"{len(replacement)} > {original_length} bytes"
                )

            slot_end = align(offset + original_length)
            if any(data[offset + original_length : slot_end]):
                raise ValueError(
                    f"non-zero data follows PNG {index:08x}; refusing to overwrite its padding"
                )
            print(
                f"Importing {local_path} -> raw PNG {index:08x} at ISO offset "
                f"0x{offset:x} ({len(replacement)}/{original_length} bytes)"
            )
            data[offset : offset + len(replacement)] = replacement
            data[offset + len(replacement) : slot_end] = b"\x00" * (slot_end - offset - len(replacement))
        data.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", default="game-patched.iso", help="ISO file to modify in-place")
    parser.add_argument(
        "--file",
        action="append",
        type=parse_mapping,
        metavar="ISO_PATH=LOCAL_PATH",
        help="file mapping, e.g. PSP_GAME/SYSDIR/BOOT.BIN=/tmp/BOOT.BIN",
    )
    parser.add_argument(
        "--raw-png-dir",
        type=Path,
        metavar="DIRECTORY",
        help=(
            "replace embedded PNGs named by ISO-wide hexadecimal order, "
            "e.g. iso_mkd/translated/00000003.png"
        ),
    )
    args = parser.parse_args()

    if not args.file and args.raw_png_dir is None:
        parser.error("at least one --file or --raw-png-dir is required")

    iso_path = Path(args.iso)
    if not iso_path.exists():
        print(f"Error: ISO file not found: {iso_path}", file=sys.stderr)
        return 1

    with iso_path.open("r+b") as iso:
        iso_files = parse_iso9660(iso)
        for iso_member, local_path in args.file or []:
            if iso_member not in iso_files:
                print(f"Error: {iso_member} not found in ISO", file=sys.stderr)
                return 1
            if not local_path.exists():
                print(f"Error: local file not found: {local_path}", file=sys.stderr)
                return 1

            entry = iso_files[iso_member]
            expected_size = entry["size"]
            actual_size = local_path.stat().st_size
            if actual_size != expected_size:
                print(
                    f"Error: size mismatch for {iso_member}: ISO expects {expected_size}, "
                    f"local file is {actual_size}",
                    file=sys.stderr,
                )
                return 1

            print(f"Importing {local_path} -> {iso_member} LBA {entry['lba']} ({actual_size} bytes)")
            iso.seek(entry["lba"] * 2048)
            with local_path.open("rb") as source:
                shutil.copyfileobj(source, iso, length=8 * 1024 * 1024)

        if args.raw_png_dir is not None:
            import_raw_pngs(iso, args.raw_png_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
