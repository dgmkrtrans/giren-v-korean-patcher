#!/usr/bin/env python3
"""Dump PSET textures including CMP0-compressed TX records.

Some PSET2 resources store TX records inside CMP0 blocks instead of using
plain ``TX\0\0`` children.  CMP0 is a zero-run RLE wrapper whose decoded
payload is a normal TX segment.  Mixed PSET ranges must pair palettes in the
combined TX/CMP0 read order, because the PL table usually lives after both
kinds of texture records.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw

import dump_static_textures as texture_dump


CMP0_MAGIC = b"CMP0"
PSET_MAGIC = b"PSET"


@dataclass
class Cmp0Record:
    source: str
    pset_index: int
    cmp0_index: int
    resource_index: int
    resource_kind: str
    offset: int
    tx_offset: int
    cmp0_offset: int | None
    cmp0_stored_size: int | None
    cmp0_unpacked_size: int | None
    tx_size: int
    width: int
    height: int
    palette_offset: int
    palette_colors: int
    bpp: int
    category: str
    palette_profile: str
    palette_order: str
    sha1: str
    alpha_pixels: int
    opaque_pixels: int
    bbox: str
    output: str


def read_u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def decode_cmp0_tx(blob: bytes, offset: int) -> tuple[bytes, int, int]:
    if blob[offset : offset + 4] != CMP0_MAGIC or offset + 12 > len(blob):
        raise ValueError(f"not a CMP0 block at 0x{offset:x}")

    stored_size = read_u32(blob, offset + 4)
    unpacked_size = read_u32(blob, offset + 8)
    if stored_size < 12 or offset + stored_size > len(blob):
        raise ValueError(f"invalid CMP0 stored size at 0x{offset:x}: 0x{stored_size:x}")
    if unpacked_size <= 0:
        raise ValueError(f"invalid CMP0 unpacked size at 0x{offset:x}: 0x{unpacked_size:x}")

    cursor = offset + 12
    end = offset + stored_size
    output = bytearray()
    while cursor < end and len(output) < unpacked_size:
        zero_count = blob[cursor]
        cursor += 1
        output.extend(b"\x00" * zero_count)
        if len(output) >= unpacked_size:
            break

        if cursor >= end:
            raise ValueError(f"CMP0 literal count is missing at 0x{offset:x}")
        literal_count = blob[cursor]
        cursor += 1
        if cursor + literal_count > end:
            raise ValueError(f"CMP0 literal run overruns block at 0x{offset:x}")
        output.extend(blob[cursor : cursor + literal_count])
        cursor += literal_count

    if len(output) != unpacked_size:
        raise ValueError(
            f"CMP0 output size mismatch at 0x{offset:x}: {len(output)} != {unpacked_size}"
        )
    if not output.startswith(texture_dump.TX_MAGIC):
        raise ValueError(f"CMP0 decoded payload is not TX at 0x{offset:x}")
    tx_size = read_u32(output, 4)
    if tx_size != unpacked_size:
        raise ValueError(f"CMP0 decoded TX size mismatch at 0x{offset:x}: {tx_size} != {unpacked_size}")
    return bytes(output), stored_size, unpacked_size


def pset_ranges(blob: bytes) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while True:
        offset = blob.find(PSET_MAGIC, cursor)
        if offset < 0:
            return ranges
        cursor = offset + 1
        if offset + 12 > len(blob):
            continue
        total = read_u32(blob, offset + 8)
        if total >= 12 and offset + total <= len(blob):
            ranges.append((offset, offset + total))


def find_offsets(blob: bytes, magic: bytes, start: int, end: int) -> list[int]:
    offsets: list[int] = []
    cursor = start
    while True:
        offset = blob.find(magic, cursor, end)
        if offset < 0:
            return offsets
        offsets.append(offset)
        cursor = offset + 1


def choose_palette(resource_index: int, resource_offsets: list[int], pl_offsets: list[int]) -> int | None:
    if not pl_offsets:
        return None
    if len(pl_offsets) > 1 and resource_offsets and pl_offsets[0] > resource_offsets[-1]:
        return pl_offsets[min(resource_index, len(pl_offsets) - 1)]

    target_offset = resource_offsets[resource_index]
    previous = [offset for offset in pl_offsets if offset < target_offset]
    if previous:
        return previous[-1]
    following = [offset for offset in pl_offsets if offset > target_offset]
    if following:
        return following[0]
    return pl_offsets[0]


def source_archive_index(rel_source: str) -> tuple[int | None, int | None]:
    parts = rel_source.replace("\\", "/").split("/")
    archive: int | None = None
    for part in parts:
        if part.startswith("unpacked_"):
            try:
                archive = int(part.removeprefix("unpacked_"))
            except ValueError:
                archive = None
            break
    try:
        source_index = int(Path(parts[-1]).stem, 16)
    except ValueError:
        source_index = None
    return archive, source_index


def known_good_palette_offset(
    rel_source: str,
    pset_index: int,
    resource_index: int,
    resource_kind: str,
    width: int,
    height: int,
    palette_offset: int,
    pl_offsets: list[int],
) -> int:
    archive, source_index = source_archive_index(rel_source)
    if (
        archive == 2
        and source_index == 0x11
        and pset_index == 0
        and resource_kind == "cmp0"
        and resource_index in {0, 1, 2}
        and height == 39
        and width in {123, 124}
        and pl_offsets
    ):
        return pl_offsets[0]
    return palette_offset


def known_good_palette_order(
    rel_source: str,
    resource_kind: str,
    palette_order: str,
) -> str:
    archive, source_index = source_archive_index(rel_source)
    if archive == 9 and source_index == 0x130 and resource_kind == "cmp0":
        return "transparent_8000"
    return palette_order


def used_palette_indices(tx_segment: bytes, palette_colors: int) -> set[int]:
    width = texture_dump.read_u16(tx_segment, 8)
    height = texture_dump.read_u16(tx_segment, 10)
    bpp = 4 if palette_colors <= 16 else 8
    stride = texture_dump._stride_bytes(width, bpp)
    pixel_data = texture_dump.choose_pixel_data(tx_segment, stride * height)

    used: set[int] = set()
    for row in range(height):
        row_start = row * stride
        row_bytes = pixel_data[row_start : row_start + stride]
        if bpp == 4:
            pixel_x = 0
            for byte in row_bytes:
                for index in (byte & 0x0F, (byte >> 4) & 0x0F):
                    if pixel_x >= width:
                        break
                    used.add(index)
                    pixel_x += 1
        else:
            used.update(row_bytes[:width])
    return used


def palette_collides_on_used_indices(
    tx_segment: bytes,
    pl_segment: bytes,
    palette_order: str,
) -> bool:
    palette = texture_dump.parse_palette(pl_segment, palette_order=palette_order)
    used = used_palette_indices(tx_segment, len(palette))
    seen: dict[tuple[int, int, int, int], int] = {}
    for index in sorted(used):
        if index >= len(palette):
            continue
        color = palette[index]
        previous = seen.setdefault(color, index)
        if previous != index:
            return True
    return False


def roundtrip_safe_palette(
    blob: bytes,
    tx_segment: bytes,
    palette_offset: int,
    pl_offsets: list[int],
    palette_order: str,
) -> int:
    pl_segment = declared_segment(blob, palette_offset).data
    try:
        if not palette_collides_on_used_indices(tx_segment, pl_segment, palette_order):
            return palette_offset
    except ValueError:
        return palette_offset

    try:
        palette_index = pl_offsets.index(palette_offset)
    except ValueError:
        return palette_offset

    candidates = list(reversed(pl_offsets[:palette_index])) + pl_offsets[palette_index + 1 :]
    for candidate_offset in candidates:
        candidate_segment = declared_segment(blob, candidate_offset).data
        try:
            if not palette_collides_on_used_indices(tx_segment, candidate_segment, palette_order):
                return candidate_offset
        except ValueError:
            continue
    return palette_offset


def declared_segment(blob: bytes, offset: int) -> texture_dump.Segment:
    size = read_u32(blob, offset + 4)
    return texture_dump.Segment(path="", offset=offset, data=blob[offset : offset + size], parent="", index=0)


def offset_in_ranges(offset: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in ranges)


def input_files(source_root: Path, list_path: Path | None) -> list[Path]:
    if list_path is not None:
        return [
            source_root / line.strip()
            for line in list_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    if source_root.is_file():
        return [source_root]
    return sorted(path for path in source_root.rglob("*") if path.is_file() and CMP0_MAGIC in path.read_bytes())


def safe_source_stem(path: Path, source_root: Path) -> str:
    if path == source_root:
        return path.stem.replace(".", "_")
    try:
        rel = path.relative_to(source_root)
    except ValueError:
        rel = path
    return str(rel).replace("/", "_").replace("\\", "_").replace(".", "_")


def dump_cmp0_textures(
    source_root: Path,
    files: list[Path],
    out_root: Path,
    include_plain_tx: bool = True,
) -> list[Cmp0Record]:
    records: list[Cmp0Record] = []
    for path in files:
        if not path.exists():
            print(f"missing input: {path}", file=sys.stderr)
            continue
        blob = path.read_bytes()
        if path == source_root:
            rel_source = path.name
        else:
            try:
                rel_source = str(path.relative_to(source_root))
            except ValueError:
                rel_source = str(path)

        for pset_index, (start, end) in enumerate(pset_ranges(blob)):
            cmp_offsets = find_offsets(blob, CMP0_MAGIC, start, end)
            tx_by_offset: dict[int, tuple[bytes, int, int]] = {}
            for cmp_offset in cmp_offsets:
                try:
                    tx_by_offset[cmp_offset] = decode_cmp0_tx(blob, cmp_offset)
                except ValueError as exc:
                    print(f"{rel_source}: skip CMP0 0x{cmp_offset:x}: {exc}", file=sys.stderr)

            valid_cmp_offsets = [offset for offset in cmp_offsets if offset in tx_by_offset]
            cmp_ranges = [
                (cmp_offset, cmp_offset + tx_by_offset[cmp_offset][1])
                for cmp_offset in valid_cmp_offsets
            ]
            plain_tx_offsets = [
                offset
                for offset in texture_dump.find_magic_offsets(blob, texture_dump.TX_MAGIC, start, end)
                if not offset_in_ranges(offset, cmp_ranges) and texture_dump.valid_tx_at(blob, offset, end)
            ]
            resources: list[tuple[int, str]] = sorted(
                [(offset, "tx") for offset in plain_tx_offsets]
                + [(offset, "cmp0") for offset in valid_cmp_offsets]
            )
            resource_offsets = [offset for offset, _kind in resources]
            pl_offsets = [
                offset
                for offset in texture_dump.find_magic_offsets(blob, texture_dump.PL_MAGIC, start, end)
                if texture_dump.valid_pl_at(blob, offset, end)
            ]

            cmp0_seen = 0
            for resource_index, (resource_offset, resource_kind) in enumerate(resources):
                is_cmp0 = resource_kind == "cmp0"
                cmp0_index = cmp0_seen if is_cmp0 else -1
                if is_cmp0:
                    cmp0_seen += 1
                if not is_cmp0 and not include_plain_tx:
                    continue

                palette_offset = choose_palette(resource_index, resource_offsets, pl_offsets)
                if palette_offset is None:
                    print(
                        f"{rel_source}: skip {resource_kind} 0x{resource_offset:x}: no PL in PSET",
                        file=sys.stderr,
                    )
                    continue

                if is_cmp0:
                    tx_segment, stored_size, unpacked_size = tx_by_offset[resource_offset]
                    cmp0_offset: int | None = resource_offset
                    tx_offset = resource_offset
                else:
                    stored_size = None
                    unpacked_size = None
                    cmp0_offset = None
                    tx_offset = resource_offset
                    tx_segment = declared_segment(blob, resource_offset).data

                width = texture_dump.read_u16(tx_segment, 8)
                height = texture_dump.read_u16(tx_segment, 10)
                palette_offset = known_good_palette_offset(
                    rel_source,
                    pset_index,
                    resource_index,
                    resource_kind,
                    width,
                    height,
                    palette_offset,
                    pl_offsets,
                )
                base_pl_segment = declared_segment(blob, palette_offset)
                base_profile = texture_dump.raw_palette_profile(base_pl_segment.data)
                base_pattern = texture_dump.infer_pattern(
                    width,
                    height,
                    f"/{resource_kind}/{pset_index}/{resource_index}",
                    base_profile,
                )
                base_palette_order = texture_dump.palette_order_for_pattern(base_pattern, base_profile)
                base_palette_order = known_good_palette_order(
                    rel_source,
                    resource_kind,
                    base_palette_order,
                )
                palette_offset = roundtrip_safe_palette(
                    blob,
                    tx_segment,
                    palette_offset,
                    pl_offsets,
                    base_palette_order,
                )
                pl_segment = declared_segment(blob, palette_offset)
                raw_profile = texture_dump.raw_palette_profile(pl_segment.data)
                pattern = texture_dump.infer_pattern(
                    width,
                    height,
                    f"/{resource_kind}/{pset_index}/{resource_index}",
                    raw_profile,
                )
                palette_order = texture_dump.palette_order_for_pattern(pattern, raw_profile)
                palette_order = known_good_palette_order(
                    rel_source,
                    resource_kind,
                    palette_order,
                )

                try:
                    image, palette_colors, bpp = texture_dump.decode_tx_pl(
                        tx_segment,
                        pl_segment.data,
                        palette_order=palette_order,
                    )
                except ValueError as exc:
                    print(f"{rel_source}: skip {resource_kind} 0x{resource_offset:x}: {exc}", file=sys.stderr)
                    continue

                digest = texture_dump.image_digest(image)
                category = texture_dump.classify_texture(image, palette_colors, bpp, pattern)
                kind_label = "cmp" if is_cmp0 else "tx"
                filename = (
                    f"{safe_source_stem(path, source_root)}_pset{pset_index:03d}_"
                    f"res{resource_index:03d}_{kind_label}{max(cmp0_index, 0):03d}_"
                    f"off{resource_offset:06x}_{width}x{height}_{bpp}bpp_{digest[:12]}.png"
                )
                output = Path(category) / filename
                output_path = out_root / output
                output_path.parent.mkdir(parents=True, exist_ok=True)
                image.save(output_path)

                alpha_pixels, opaque_pixels, bbox = texture_dump.image_stats(image)
                records.append(
                    Cmp0Record(
                        source=rel_source,
                        pset_index=pset_index,
                        cmp0_index=cmp0_index,
                        resource_index=resource_index,
                        resource_kind=resource_kind,
                        offset=resource_offset,
                        tx_offset=tx_offset,
                        cmp0_offset=cmp0_offset,
                        cmp0_stored_size=stored_size,
                        cmp0_unpacked_size=unpacked_size,
                        tx_size=len(tx_segment),
                        width=width,
                        height=height,
                        palette_offset=palette_offset,
                        palette_colors=palette_colors,
                        bpp=bpp,
                        category=category,
                        palette_profile=texture_dump.image_palette_profile(image),
                        palette_order=palette_order,
                        sha1=digest,
                        alpha_pixels=alpha_pixels,
                        opaque_pixels=opaque_pixels,
                        bbox=bbox,
                        output=str(output),
                    )
                )
    return records


def write_manifest(out_root: Path, records: list[Cmp0Record]) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    rows = [asdict(record) for record in records]
    with (out_root / "manifest.json").open("w", encoding="utf-8") as fp:
        json.dump(rows, fp, ensure_ascii=False, indent=2)
    with (out_root / "manifest.csv").open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)


def write_contact_sheet(out_root: Path, records: list[Cmp0Record], name: str, category: str | None) -> None:
    selected = [record for record in records if category is None or record.category == category]
    if not selected:
        return
    tile_width = 520 if category == "ui" else 220
    tile_height = 70 if category == "ui" else 84
    columns = 2 if category == "ui" else 4
    thumbs: list[Image.Image] = []
    for index, record in enumerate(selected, start=1):
        image = Image.open(out_root / record.output).convert("RGBA")
        tile = Image.new("RGBA", (tile_width, tile_height), (26, 26, 26, 255))
        scale = min((tile_width - 8) / image.width, (tile_height - 26) / image.height, 3.0)
        resized = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.NEAREST,
        )
        tile.alpha_composite(resized, ((tile_width - resized.width) // 2, 2))
        draw = ImageDraw.Draw(tile)
        draw.text(
            (3, tile_height - 22),
            f"{index:03d} {Path(record.source).name} {record.width}x{record.height}",
            fill=(255, 255, 255, 255),
        )
        thumbs.append(tile)

    rows = math.ceil(len(thumbs) / columns)
    sheet = Image.new("RGBA", (columns * tile_width, rows * tile_height), (18, 18, 18, 255))
    for index, thumb in enumerate(thumbs):
        sheet.alpha_composite(thumb, ((index % columns) * tile_width, (index // columns) * tile_height))
    sheet.save(out_root / name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("unpacked_mkd"))
    parser.add_argument("--list", dest="list_path", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("results/find_un_decrypted/cmp0_textures"))
    parser.add_argument("--cmp0-only", action="store_true", help="dump only CMP0-wrapped TX records")
    parser.add_argument("--clean", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.clean and args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True, exist_ok=True)

    files = input_files(args.source, args.list_path)
    records = dump_cmp0_textures(args.source, files, args.out, include_plain_tx=not args.cmp0_only)
    write_manifest(args.out, records)
    write_contact_sheet(args.out, records, "contact_sheet.png", None)
    write_contact_sheet(args.out, records, "contact_sheet_ui.png", "ui")

    counts = Counter(record.category for record in records)
    kinds = Counter(record.resource_kind for record in records)
    print(f"Decoded PSET textures: {len(records)}")
    print(f"Resource kinds: {dict(kinds)}")
    print(f"Categories: {dict(counts)}")
    print(f"Output directory: {args.out}")


if __name__ == "__main__":
    main()
