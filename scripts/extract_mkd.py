#!/usr/bin/env python3
"""Pure Python extractor for Gihren PSP MKD archives.

The original workflow used QuickBMS for SD0 decompression.  This script mirrors
the MKD stream parser used by rebuild_mkd.py, implements the inverse SD0 token
decoder in Python, and also exposes the raw PNG tail found in ZZZPSP9.MKD.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


ALIGNMENT = 0x800
SD0_MAGIC = b"SD0\x00"
RIFF_MAGIC = b"RIFF"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class MkdEntry:
    index: int
    kind: str
    offset: int
    stored_size: int
    unpacked_size: int
    next_offset: int


def read_u32(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def align(value: int, boundary: int = ALIGNMENT) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def parse_mkd(data: bytes) -> tuple[list[MkdEntry], int | None]:
    entries: list[MkdEntry] = []
    offset = 0
    while offset < len(data):
        magic = data[offset : offset + 4]
        if magic == SD0_MAGIC:
            stored_size = read_u32(data, offset + 4)
            unpacked_size = read_u32(data, offset + 8)
            if stored_size < 12 or offset + stored_size > len(data):
                raise ValueError(f"invalid SD0 size at 0x{offset:x}: {stored_size}")
            next_offset = align(offset + stored_size)
            entries.append(
                MkdEntry(
                    index=len(entries),
                    kind="SD0",
                    offset=offset,
                    stored_size=stored_size,
                    unpacked_size=unpacked_size,
                    next_offset=next_offset,
                )
            )
            offset = next_offset
            continue

        if magic == RIFF_MAGIC:
            stored_size = read_u32(data, offset + 4) + 8
            if stored_size < 8 or offset + stored_size > len(data):
                raise ValueError(f"invalid RIFF size at 0x{offset:x}: {stored_size}")
            next_offset = align(offset + stored_size)
            entries.append(
                MkdEntry(
                    index=len(entries),
                    kind="RIFF",
                    offset=offset,
                    stored_size=stored_size,
                    unpacked_size=stored_size,
                    next_offset=next_offset,
                )
            )
            offset = next_offset
            continue

        return entries, offset
    return entries, None


def decompress_sd0(chunk: bytes) -> bytes:
    if not chunk.startswith(SD0_MAGIC):
        raise ValueError("not an SD0 chunk")
    stored_size = read_u32(chunk, 4)
    output_size = read_u32(chunk, 8)
    if stored_size < 12 or stored_size > len(chunk):
        raise ValueError(f"invalid SD0 stored size: {stored_size}")

    source = memoryview(chunk)[:stored_size]
    output = bytearray()
    cursor = 12
    while len(output) < output_size:
        if cursor >= len(source):
            raise ValueError("SD0 input ended before output was complete")
        flags = source[cursor]
        cursor += 1
        for bit in range(8):
            if len(output) >= output_size:
                break
            compressed = flags & (1 << bit)
            if not compressed:
                output.append(source[cursor])
                cursor += 1
                continue

            control = source[cursor]
            cursor += 1
            kind = control & 0x0F
            if kind == 1:
                value = source[cursor]
                cursor += 1
                output.extend([value] * ((control >> 4) + 3))
                continue

            if kind == 2:
                high = source[cursor]
                cursor += 1
                length = ((control >> 4) | (high << 4)) + 18
                output.extend(source[cursor : cursor + length])
                cursor += length
                continue

            distance_low = control >> 4
            distance_high = source[cursor]
            cursor += 1
            distance = distance_low | (distance_high << 4)
            length = kind
            if kind == 0:
                length = source[cursor] + 16
                cursor += 1
            if distance <= 0 or distance > len(output):
                raise ValueError(f"invalid SD0 match distance: {distance}")
            for _ in range(length):
                output.append(output[-distance])

    if len(output) != output_size:
        raise ValueError(f"SD0 output size mismatch: {len(output)} != {output_size}")
    return bytes(output)


def extension_for_data(data: bytes, fallback_kind: str) -> str:
    if data.startswith(b"MRG\x00"):
        return ".mrg"
    if data.startswith(b"PSET"):
        return ".pse"
    if data.startswith(RIFF_MAGIC):
        return ".wav"
    if data.startswith(PNG_MAGIC):
        return ".png"
    if data.startswith(b"pBAV"):
        return ".pba"
    if data.startswith(b"pQES"):
        return ".pqe"
    if fallback_kind == "RIFF":
        return ".wav"
    return ".dat"


def png_length(data: bytes, offset: int) -> int:
    if data[offset : offset + len(PNG_MAGIC)] != PNG_MAGIC:
        raise ValueError(f"not a PNG at 0x{offset:x}")
    cursor = offset + len(PNG_MAGIC)
    while cursor + 12 <= len(data):
        length = int.from_bytes(data[cursor : cursor + 4], "big")
        chunk_type = data[cursor + 4 : cursor + 8]
        cursor += 12 + length
        if chunk_type == b"IEND":
            return cursor - offset
    raise ValueError(f"PNG at 0x{offset:x} has no IEND")


def extract_tail_pngs(tail: bytes, start_index: int, out_dir: Path) -> int:
    offsets: list[int] = []
    cursor = 0
    while True:
        offset = tail.find(PNG_MAGIC, cursor)
        if offset < 0:
            break
        offsets.append(offset)
        cursor = offset + 1

    for png_index, offset in enumerate(offsets):
        length = png_length(tail, offset)
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"{start_index + png_index:08x}.png"
        output.write_bytes(tail[offset : offset + length])
    return len(offsets)


def extract_mkd(
    mkd_path: Path,
    out_dir: Path,
    include_tail_pngs: bool = True,
    tail_dir_name: str = "_tail_png",
) -> None:
    data = mkd_path.read_bytes()
    entries, tail_offset = parse_mkd(data)
    out_dir.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    for entry in entries:
        chunk = data[entry.offset : entry.offset + entry.stored_size]
        if entry.kind == "SD0":
            raw = decompress_sd0(chunk)
        elif entry.kind == "RIFF":
            raw = chunk
        else:
            raise AssertionError(f"unknown entry kind: {entry.kind}")
        ext = extension_for_data(raw, entry.kind)
        counts[ext] = counts.get(ext, 0) + 1
        (out_dir / f"{entry.index:08x}{ext}").write_bytes(raw)

    tail_pngs = 0
    if include_tail_pngs and tail_offset is not None:
        tail_pngs = extract_tail_pngs(data[tail_offset:], len(entries), out_dir / tail_dir_name)
    print(
        f"{mkd_path.name}: entries={len(entries)} tail="
        f"{'none' if tail_offset is None else hex(tail_offset)} tail_pngs={tail_pngs} "
        f"outputs={sum(counts.values()) + tail_pngs} counts={counts}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mkd", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--skip-tail-pngs", action="store_true")
    parser.add_argument(
        "--tail-dir-name",
        default="_tail_png",
        help="subdirectory for raw tail PNGs so rebuild_mkd direct entry counts stay compatible",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    extract_mkd(
        args.mkd,
        args.out,
        include_tail_pngs=not args.skip_tail_pngs,
        tail_dir_name=args.tail_dir_name,
    )


if __name__ == "__main__":
    main()
