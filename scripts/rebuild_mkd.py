#!/usr/bin/env python3
"""Rebuild Gihren PSP MKD archives from extracted files.

The archive layer is a stream of 0x800-aligned records.  Most records are SD0
compressed; archive 0 also contains RIFF/WAV passthrough records, and archive 9
has a raw PNG tail after its SD0 records.  This script can also apply the
`textures_static/manifest.json` PNG view back into TX segments before rebuilding.
"""

from __future__ import annotations

import argparse
from array import array
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image

import dump_static_textures as texture_dump


ALIGNMENT = 0x800
SD0_MAGIC = b"SD0\x00"
RIFF_MAGIC = b"RIFF"
MRG_MAGIC = b"MRG\x00"
PSET_MAGIC = b"PSET"
TX_MAGIC = b"TX\x00\x00"
PL_MAGIC = b"PL\x00\x00"
CMP0_MAGIC = b"CMP0"
EXPANDABLE_FONTTILE_SOURCE = "unpacked_1/000002e6.mrg"
Sd0Token = tuple[str, int, int, int]
SD0_FAST_GREEDY_MATCH_CANDIDATES = 64
SD0_OPTIMAL_MATCH_CANDIDATE_STEPS = (16, 64, 256)
SCRIPT_DIR = Path(__file__).resolve().parent
SD0_FAST_CODEC_SOURCE = SCRIPT_DIR / "sd0_fast_codec.cpp"
SD0_FAST_CODEC_BINARY = Path(os.environ.get("GIREN_SD0_FAST_CODEC", "/tmp/giren_sd0_fast_codec"))
_SD0_FAST_CODEC_READY: Path | None | bool = None


@dataclass(frozen=True)
class MkdEntry:
    index: int
    kind: str
    offset: int
    stored_size: int
    unpacked_size: int
    next_offset: int


@dataclass
class TexturePatchSet:
    records_by_source: dict[str, list[dict]]
    skipped_unchanged: int = 0
    selected: int = 0


@dataclass
class RebuildStats:
    archive: int
    entries: int = 0
    reused: int = 0
    recompressed: int = 0
    riff: int = 0
    texture_records: int = 0
    texture_sources: int = 0
    changed_files: int = 0
    identical: bool | None = None


@dataclass(frozen=True)
class EntryPatchTask:
    archive: int
    entry_index: int
    kind: str
    source_path: str
    rel_source: str
    stored_size: int
    envelope_size: int
    records: list[dict]
    textures_root: str
    reuse_unchanged: bool
    use_optimal_sd0: bool
    use_optimal_cmp0: bool


@dataclass
class EntryPatchResult:
    entry_index: int
    chunk: bytes | None
    reused: int = 0
    recompressed: int = 0
    riff: int = 0
    texture_records: int = 0
    texture_sources: int = 0
    changed_files: int = 0


@dataclass(frozen=True)
class PsetCmp0Block:
    index: int
    start: int
    total: int
    cmp0_offset: int
    cmp0_stored_size: int
    cmp0_pl_gap: bytes
    pl_offset: int
    pl_size: int
    padding_after: bytes


@dataclass(frozen=True)
class StandalonePsetLayout:
    resource_table_offset: int
    resource_offset_base: int
    resource_starts: list[int]
    pl_table_pointer_offset: int
    pl_table_start: int


@dataclass(frozen=True)
class MrgChild:
    start: int
    total: int
    padding_after: bytes


_WORKER_IMAGE_CACHE: dict[str, tuple[int, int, bytes]] = {}


def read_u32(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def align(value: int, boundary: int = ALIGNMENT) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def parse_archive_list(value: str) -> list[int]:
    archives: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise argparse.ArgumentTypeError(f"invalid archive range: {part}")
            archives.update(range(start, end + 1))
        else:
            archives.add(int(part))
    return sorted(archives)


def parse_jobs(value: str) -> int:
    text = value.strip().lower()
    if text == "auto":
        return 0
    try:
        jobs = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid jobs value: {value}") from exc
    if jobs < 1:
        raise argparse.ArgumentTypeError("--jobs must be a positive integer or 'auto'")
    return jobs


def resolve_jobs(jobs: int) -> int:
    if jobs == 0:
        return max(1, os.cpu_count() or 1)
    return jobs


def infer_archives(unpacked_root: Path) -> list[int]:
    archives: list[int] = []
    for child in sorted(unpacked_root.glob("unpacked_*")):
        if not child.is_dir():
            continue
        suffix = child.name.rsplit("_", 1)[-1]
        if suffix.isdigit():
            archives.append(int(suffix))
    return archives


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


def output_digest(width: int, height: int, rgba_bytes: bytes) -> str:
    return hashlib.sha1(
        width.to_bytes(2, "little") + height.to_bytes(2, "little") + rgba_bytes
    ).hexdigest()


def load_rgba_bytes(path: Path) -> tuple[int, int, bytes]:
    with Image.open(path) as image:
        converted = image.convert("RGBA")
        return converted.width, converted.height, converted.tobytes()


def manifest_record_key(record: dict) -> tuple[str, str, str]:
    return (
        str(record.get("source", "")).replace("\\", "/"),
        str(record.get("offset", "")),
        str(record.get("output", "")).replace("\\", "/"),
    )


def optional_manifest_int(record: dict, field: str) -> int | None:
    value = record.get(field)
    if value is None or value == "":
        return None
    return int(value)


def effective_pl_segment_for_record(
    *,
    rel_source: str,
    record: dict,
    tx_segment: bytes,
    tx_offset: int,
    pl_segment: bytes,
    palette_offset: int,
) -> bytes:
    tx_record = texture_dump.Segment(
        path=str(record.get("tree_path", "")),
        offset=tx_offset,
        data=tx_segment,
        parent="",
        index=0,
    )
    pl_record = texture_dump.Segment(
        path=f"{tx_record.path}/pl",
        offset=palette_offset,
        data=pl_segment,
        parent="",
        index=0,
    )
    return texture_dump.database_detail_rebuild_palette_override(
        rel_source,
        tx_record,
        pl_record,
    ).data


def patch_effective_pl_segment(
    data: bytearray,
    *,
    palette_offset: int,
    original_pl_segment: bytes,
    effective_pl_segment: bytes,
) -> None:
    if effective_pl_segment == original_pl_segment:
        return
    if len(effective_pl_segment) != len(original_pl_segment):
        raise ValueError("effective PL override changed segment size")
    data[palette_offset : palette_offset + len(original_pl_segment)] = effective_pl_segment


def parse_patch_dialogue_line_lengths(value: object) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value]
    else:
        text = str(value).strip()
        if not text:
            return ()
        for separator in (";", "|", "/", " "):
            text = text.replace(separator, ",")
        parts = [part.strip() for part in text.split(",") if part.strip()]

    lengths: list[int] = []
    for part in parts:
        number = int(part, 0)
        if number == 0:
            continue
        if number < 0 or number > 0xFF:
            raise ValueError(f"dialogue line length out of byte range: {number}")
        lengths.append(number)
    return tuple(lengths)


def normalized_dialogue_line_lengths(value: object) -> str:
    lengths = parse_patch_dialogue_line_lengths(value)
    return ",".join(str(length) for length in lengths)


def decode_cmp0_tx(data: bytes | bytearray, offset: int) -> tuple[bytes, int, int]:
    if offset < 0 or offset + 12 > len(data):
        raise ValueError(f"CMP0 offset is out of range: {offset}")
    if data[offset : offset + 4] != CMP0_MAGIC:
        raise ValueError(f"target offset is not a CMP0 block: 0x{offset:x}")

    stored_size = read_u32(data, offset + 4)
    unpacked_size = read_u32(data, offset + 8)
    if stored_size < 12 or offset + stored_size > len(data):
        raise ValueError(f"invalid CMP0 stored size at 0x{offset:x}: 0x{stored_size:x}")
    if unpacked_size <= 0:
        raise ValueError(f"invalid CMP0 unpacked size at 0x{offset:x}: 0x{unpacked_size:x}")

    cursor = offset + 12
    end = offset + stored_size
    output = bytearray()
    while cursor < end and len(output) < unpacked_size:
        zero_count = data[cursor]
        cursor += 1
        output.extend(b"\x00" * zero_count)
        if len(output) >= unpacked_size:
            break

        if cursor >= end:
            raise ValueError(f"CMP0 literal count is missing at 0x{offset:x}")
        literal_count = data[cursor]
        cursor += 1
        if cursor + literal_count > end:
            raise ValueError(f"CMP0 literal run overruns block at 0x{offset:x}")
        output.extend(data[cursor : cursor + literal_count])
        cursor += literal_count

    if len(output) != unpacked_size:
        raise ValueError(
            f"CMP0 output size mismatch at 0x{offset:x}: {len(output)} != {unpacked_size}"
        )
    if not output.startswith(TX_MAGIC):
        raise ValueError(f"CMP0 decoded payload is not TX at 0x{offset:x}")
    tx_size = read_u32(output, 4)
    if tx_size != unpacked_size:
        raise ValueError(
            f"CMP0 decoded TX size mismatch at 0x{offset:x}: {tx_size} != {unpacked_size}"
        )
    return bytes(output), stored_size, unpacked_size


def cmp0_literal_run_end(data: bytes, start: int) -> int:
    position = start
    limit = min(len(data), start + 255)
    while position < limit:
        if data[position] != 0:
            position += 1
            continue

        zero_run = 0
        while (
            position + zero_run < len(data)
            and zero_run < 3
            and data[position + zero_run] == 0
        ):
            zero_run += 1
        if zero_run >= 3:
            break
        if position + zero_run > limit:
            return limit
        position += zero_run
    return position


def encode_cmp0_payload(data: bytes) -> bytes:
    output = bytearray()
    position = 0
    while position < len(data):
        zero_count = 0
        while (
            position + zero_count < len(data)
            and zero_count < 255
            and data[position + zero_count] == 0
        ):
            zero_count += 1
        output.append(zero_count)
        position += zero_count
        if position >= len(data):
            break

        literal_start = position
        position = cmp0_literal_run_end(data, literal_start)
        literal_count = position - literal_start
        if literal_count > 255:
            raise AssertionError(f"CMP0 literal run exceeded 255 bytes: {literal_count}")
        output.append(literal_count)
        output.extend(data[literal_start:position])
    return bytes(output)


def cmp0_zero_runs(data: bytes) -> list[int]:
    runs = [0] * (len(data) + 1)
    for position in range(len(data) - 1, -1, -1):
        if data[position] == 0:
            runs[position] = runs[position + 1] + 1
    return runs


def optimal_cmp0_payload(data: bytes) -> bytes:
    data_length = len(data)
    zero_runs = cmp0_zero_runs(data)
    costs = [0] * (data_length + 1)
    choices = [0] * data_length

    for position in range(data_length - 1, -1, -1):
        zero_count = min(zero_runs[position], 255)
        literal_start = position + zero_count
        if literal_start >= data_length:
            costs[position] = 1
            choices[position] = -1
            continue

        min_literal = 0 if zero_count else 1
        literal_limit = min(data_length, literal_start + 255)
        best_cost: int | None = None
        best_length = min_literal
        for literal_end in range(literal_start + min_literal, literal_limit + 1):
            literal_length = literal_end - literal_start
            cost = 2 + literal_length + costs[literal_end]
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_length = literal_length

        if best_cost is None:
            raise AssertionError(f"CMP0 optimal encoder found no choice at {position}")
        costs[position] = best_cost
        choices[position] = best_length

    output = bytearray()
    position = 0
    while position < data_length:
        zero_count = min(zero_runs[position], 255)
        output.append(zero_count)
        position += zero_count
        if position >= data_length:
            break

        literal_count = choices[position - zero_count]
        if literal_count < 0:
            raise AssertionError("CMP0 optimal encoder selected a final run too early")
        output.append(literal_count)
        output.extend(data[position : position + literal_count])
        position += literal_count
    return bytes(output)


def cmp0_best_payload(data: bytes, use_optimal: bool) -> bytes:
    payload = encode_cmp0_payload(data)
    if use_optimal:
        optimal_payload = optimal_cmp0_payload(data)
        if len(optimal_payload) < len(payload):
            payload = optimal_payload
    return payload


def cmp0_record_from_payload(tx_segment: bytes, payload: bytes, final_size: int) -> bytes:
    actual_size = 12 + len(payload)
    if final_size < actual_size:
        raise ValueError(
            f"CMP0 record is {actual_size} bytes, larger than original "
            f"{final_size} byte slot"
        )
    output = bytearray(CMP0_MAGIC)
    output.extend(final_size.to_bytes(4, "little"))
    output.extend(len(tx_segment).to_bytes(4, "little"))
    output.extend(payload)
    output.extend(b"\x00" * (final_size - len(output)))
    return bytes(output)


def encode_cmp0_tx(
    tx_segment: bytes,
    stored_size: int | None = None,
    use_optimal: bool = False,
) -> bytes:
    if not tx_segment.startswith(TX_MAGIC):
        raise ValueError("CMP0 payload is not a TX segment")
    tx_size = read_u32(tx_segment, 4)
    if tx_size != len(tx_segment):
        raise ValueError(f"TX segment size mismatch: {tx_size} != {len(tx_segment)}")

    payload = encode_cmp0_payload(tx_segment)
    actual_size = 12 + len(payload)
    final_size = stored_size if stored_size is not None else actual_size
    if final_size < actual_size:
        if stored_size is None or not use_optimal:
            raise ValueError(
                f"CMP0 record is {actual_size} bytes, larger than original "
                f"{final_size} byte slot"
            )
        greedy_error_message = (
            f"CMP0 record is {actual_size} bytes, larger than original "
            f"{final_size} byte slot"
        )
        payload = optimal_cmp0_payload(tx_segment)
        actual_size = 12 + len(payload)
        if final_size < actual_size:
            raise ValueError(
                f"CMP0 record is {actual_size} bytes, larger than original "
                f"{final_size} byte slot; greedy fallback also failed: "
                f"{greedy_error_message}"
            )

    return cmp0_record_from_payload(tx_segment, payload, final_size)


def load_manifest_csv_overlays(textures_root: Path) -> dict[tuple[str, str, str], dict]:
    csv_path = textures_root / "manifest.csv"
    if not csv_path.exists():
        return {}

    overlays: dict[tuple[str, str, str], dict] = {}
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            overlays[manifest_record_key(row)] = row
    return overlays


def load_texture_patch_set(
    textures_root: Path | None,
    force_reencode: bool,
    archives: set[int] | None,
) -> TexturePatchSet:
    if textures_root is None:
        return TexturePatchSet(records_by_source={})

    manifest_path = textures_root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing texture manifest: {manifest_path}")

    records = json.loads(manifest_path.read_text(encoding="utf-8"))
    csv_overlays = load_manifest_csv_overlays(textures_root)
    patch_set = TexturePatchSet(records_by_source={})
    digest_cache: dict[str, str] = {}

    for record in records:
        key = manifest_record_key(record)
        overlay = csv_overlays.get(key)
        if overlay:
            for field, value in overlay.items():
                if field is not None and value is not None:
                    record[field] = value
            if "dialogue_line_lengths" in overlay:
                after_lengths = normalized_dialogue_line_lengths(
                    record.get("dialogue_line_lengths", "")
                )
                record["dialogue_line_lengths"] = after_lengths
                record["dialogue_line_count"] = len(after_lengths.split(",")) if after_lengths else 0
                if after_lengths:
                    record["__dialogue_line_control_requested"] = True

        if record.get("category") == "raw_png":
            continue
        source = str(record.get("source", "")).replace("\\", "/")
        if not source:
            continue
        if archives is not None:
            archive_text = source.split("/", 1)[0].replace("unpacked_", "")
            if archive_text.isdigit() and int(archive_text) not in archives:
                continue

        output_rel = str(record.get("output", ""))
        output_path = textures_root / output_rel
        if not output_path.exists():
            raise FileNotFoundError(f"missing texture PNG: {output_path}")

        if not force_reencode:
            digest = digest_cache.get(output_rel)
            if digest is None:
                width, height, rgba = load_rgba_bytes(output_path)
                digest = output_digest(width, height, rgba)
                digest_cache[output_rel] = digest
            if digest == record.get("sha1"):
                if not record.get("__dialogue_line_control_requested"):
                    patch_set.skipped_unchanged += 1
                    continue

        patch_set.records_by_source.setdefault(source, []).append(record)
        patch_set.selected += 1

    for source_records in patch_set.records_by_source.values():
        source_records.sort(key=lambda item: int(item["offset"]))
    return patch_set


def palette_key(color: tuple[int, int, int, int]) -> int:
    r, g, b, a = color
    return (r << 24) | (g << 16) | (b << 8) | a


def pixel_key(rgba: bytes, offset: int) -> int:
    return (
        (rgba[offset] << 24)
        | (rgba[offset + 1] << 16)
        | (rgba[offset + 2] << 8)
        | rgba[offset + 3]
    )


def pixel_is_empty(rgba: bytes, offset: int) -> bool:
    return rgba[offset + 3] == 0 or max(rgba[offset], rgba[offset + 1], rgba[offset + 2]) <= 4


def tilemap_from_record(data: bytes | bytearray, record: dict) -> texture_dump.TileMap | None:
    layout = str(record.get("layout", "linear"))
    if not layout.startswith("tilemap_"):
        return None
    offset = int(record.get("layout_offset", 0))
    magic = {
        "tilemap_mp16": texture_dump.MP16_MAGIC,
        "tilemap_mp20": texture_dump.MP20_MAGIC,
    }.get(layout)
    if magic is None:
        raise ValueError(f"unsupported texture layout: {layout}")
    tilemap = texture_dump.valid_tilemap_at(bytes(data), offset, magic)
    if tilemap is None:
        raise ValueError(f"missing {layout} block at 0x{offset:x}")
    return tilemap


def mapped_storage_xy(
    x: int,
    y: int,
    tilemap: texture_dump.TileMap,
    storage_width: int,
    storage_height: int,
) -> tuple[int, int] | None:
    tile_x = x // tilemap.tile_width
    tile_y = y // tilemap.tile_height
    if tile_x >= tilemap.width_tiles or tile_y >= tilemap.height_tiles:
        return None

    entry_index = tile_y * tilemap.width_tiles + tile_x
    entry = tilemap.entries[entry_index]
    if texture_dump.tilemap_entry_is_blank(tilemap, entry_index, entry):
        return None

    atlas_width_tiles = tilemap.atlas_width_tiles or storage_width // tilemap.tile_width
    source_tile = entry
    storage_x = (source_tile % atlas_width_tiles) * tilemap.tile_width + x % tilemap.tile_width
    storage_y = (source_tile // atlas_width_tiles) * tilemap.tile_height + y % tilemap.tile_height
    if storage_x >= storage_width or storage_y >= storage_height:
        return None
    return storage_x, storage_y


def tile_has_visible_pixels(
    rgba: bytes,
    width: int,
    height: int,
    left: int,
    top: int,
    tile_width: int,
    tile_height: int,
) -> bool:
    right = min(width, left + tile_width)
    bottom = min(height, top + tile_height)
    for y in range(top, bottom):
        row_offset = y * width * 4
        for x in range(left, right):
            if not pixel_is_empty(rgba, row_offset + x * 4):
                return True
    return False


def retile_opening_title_if_needed(
    data: bytearray,
    record: dict,
    tilemap: texture_dump.TileMap | None,
    rgba: bytes,
    width: int,
    height: int,
    storage_width: int,
    storage_height: int,
) -> texture_dump.TileMap | None:
    if tilemap is None or (record.get("verified_group") or "") != "각 세력 오프닝타이틀":
        return tilemap

    atlas_width_tiles = tilemap.atlas_width_tiles or storage_width // tilemap.tile_width
    atlas_height_tiles = tilemap.atlas_height_tiles or storage_height // tilemap.tile_height
    if atlas_width_tiles <= 0 or atlas_height_tiles <= 0:
        return tilemap
    total_storage_tiles = atlas_width_tiles * atlas_height_tiles
    blank_entry = tilemap.blank_entries[0] if tilemap.blank_entries else 0
    reserved_blank_entries = set(tilemap.blank_entries)
    if tilemap.zero_is_blank:
        reserved_blank_entries.add(0)

    entries = list(tilemap.entries)
    used_entries: set[int] = set()
    needs_allocation: list[int] = []
    changed = False

    for index, entry in enumerate(entries):
        tile_x = index % tilemap.width_tiles
        tile_y = index // tilemap.width_tiles
        active = tile_has_visible_pixels(
            rgba,
            width,
            height,
            tile_x * tilemap.tile_width,
            tile_y * tilemap.tile_height,
            tilemap.tile_width,
            tilemap.tile_height,
        )
        is_blank = texture_dump.tilemap_entry_is_blank(tilemap, index, entry)
        if active:
            if (
                is_blank
                or entry in used_entries
                or entry < 0
                or entry >= total_storage_tiles
            ):
                needs_allocation.append(index)
            else:
                used_entries.add(entry)
            continue
        if not is_blank:
            entries[index] = blank_entry
            changed = True

    free_entries = [
        entry
        for entry in range(total_storage_tiles)
        if entry not in used_entries and entry not in reserved_blank_entries
    ]
    if len(free_entries) < len(needs_allocation):
        raise ValueError(
            "not enough free tilemap storage tiles for opening title "
            f"({len(needs_allocation)} needed, {len(free_entries)} free)"
        )
    for index, entry in zip(needs_allocation, free_entries):
        entries[index] = entry
        used_entries.add(entry)
        changed = True

    if not changed:
        return tilemap

    if tilemap.kind == "MP16":
        entries_offset = tilemap.offset + 12
    elif tilemap.kind == "MP20":
        entries_offset = tilemap.offset + 20
    else:
        return tilemap
    for index, entry in enumerate(entries):
        data[entries_offset + index * 2 : entries_offset + index * 2 + 2] = entry.to_bytes(2, "little")

    new_entries = tuple(entries)
    blank_indices = texture_dump.infer_tilemap_blank_indices(
        new_entries,
        tilemap.width_tiles,
        tilemap.height_tiles,
    )
    blank_entries = tuple(sorted({new_entries[index] for index in blank_indices}))
    return texture_dump.TileMap(
        kind=tilemap.kind,
        offset=tilemap.offset,
        width_tiles=tilemap.width_tiles,
        height_tiles=tilemap.height_tiles,
        tile_width=tilemap.tile_width,
        tile_height=tilemap.tile_height,
        atlas_width_tiles=tilemap.atlas_width_tiles,
        atlas_height_tiles=tilemap.atlas_height_tiles,
        entries=new_entries,
        zero_is_blank=0 in blank_entries,
        blank_entries=blank_entries,
        blank_indices=blank_indices,
    )


def encode_png_into_tx(
    tx_segment: bytes,
    pl_segment: bytes,
    width: int,
    height: int,
    rgba: bytes,
    palette_order: str,
    layout: str = "linear",
    tilemap: texture_dump.TileMap | None = None,
) -> bytes:
    if not tx_segment.startswith(TX_MAGIC):
        raise ValueError("target offset is not a TX segment")
    if not pl_segment.startswith(PL_MAGIC):
        raise ValueError("palette offset is not a PL segment")

    tx_width = texture_dump.read_u16(tx_segment, 8)
    tx_height = texture_dump.read_u16(tx_segment, 10)
    if layout == "linear":
        expected_dimensions = (tx_width, tx_height)
    elif layout == "crop_480":
        expected_dimensions = (min(480, tx_width), tx_height)
    elif layout.startswith("tilemap_"):
        if tilemap is None:
            raise ValueError(f"{layout} requires a tilemap")
        expected_dimensions = (
            tilemap.width_tiles * tilemap.tile_width,
            tilemap.height_tiles * tilemap.tile_height,
        )
    else:
        raise ValueError(f"unsupported texture layout: {layout}")

    if (width, height) != expected_dimensions:
        raise ValueError(
            f"PNG dimensions {width}x{height} do not match {layout} "
            f"{expected_dimensions[0]}x{expected_dimensions[1]}"
        )

    palette = texture_dump.parse_palette(pl_segment, palette_order=palette_order)
    if len(palette) <= 16:
        bpp = 4
    elif len(palette) <= 256:
        bpp = 8
    else:
        raise ValueError(f"unsupported palette size: {len(palette)}")

    storage_stride = texture_dump._stride_bytes(tx_width, bpp)
    storage_pixel_size = storage_stride * tx_height
    if len(tx_segment) < 12 + storage_pixel_size:
        raise ValueError("TX segment is too short for its storage dimensions")

    stride = storage_stride
    pixel_size = stride * height
    if layout == "linear" and pixel_size != storage_pixel_size:
        raise ValueError(
            f"{layout} pixel size {pixel_size} does not match TX storage size {storage_pixel_size}"
        )
    if layout == "linear" and len(tx_segment) < 12 + pixel_size:
        raise ValueError("TX segment is too short for its dimensions")

    pixel_data = bytearray(tx_segment[12 : 12 + storage_pixel_size])
    color_to_index: dict[int, int] = {}
    transparent_index: int | None = None
    for index, color in enumerate(palette):
        key = palette_key(color)
        color_to_index.setdefault(key, index)
        if color[3] == 0 and transparent_index is None:
            transparent_index = index

    missing: dict[int, int] = {}
    for y in range(height):
        for x in range(width):
            rgba_offset = (y * width + x) * 4
            key = pixel_key(rgba, rgba_offset)

            if layout.startswith("tilemap_"):
                storage_xy = mapped_storage_xy(x, y, tilemap, tx_width, tx_height)
                if storage_xy is None:
                    if pixel_is_empty(rgba, rgba_offset):
                        continue
                    missing[key] = missing.get(key, 0) + 1
                    continue
                storage_x, storage_y = storage_xy
                row_start = storage_y * storage_stride
                pixel_x = storage_x
            else:
                row_start = y * stride
                pixel_x = x

            if bpp == 4:
                byte_offset = row_start + pixel_x // 2
                original_index = (
                    pixel_data[byte_offset] & 0x0F
                    if pixel_x % 2 == 0
                    else (pixel_data[byte_offset] >> 4) & 0x0F
                )
            else:
                byte_offset = row_start + pixel_x
                original_index = pixel_data[byte_offset]

            if original_index < len(palette) and palette_key(palette[original_index]) == key:
                encoded_index = original_index
            else:
                encoded_index = color_to_index.get(key)
                if encoded_index is None and rgba[rgba_offset + 3] == 0:
                    encoded_index = transparent_index
                if encoded_index is None:
                    missing[key] = missing.get(key, 0) + 1
                    continue

            if bpp == 4:
                if encoded_index > 0x0F:
                    raise ValueError(f"palette index does not fit in 4bpp: {encoded_index}")
                if pixel_x % 2 == 0:
                    pixel_data[byte_offset] = (pixel_data[byte_offset] & 0xF0) | encoded_index
                else:
                    pixel_data[byte_offset] = (pixel_data[byte_offset] & 0x0F) | (
                        encoded_index << 4
                    )
            else:
                pixel_data[byte_offset] = encoded_index

    if missing:
        samples = ", ".join(f"0x{key:08x}({count})" for key, count in list(missing.items())[:8])
        raise ValueError(f"PNG uses colors not present in the target PL palette: {samples}")

    return tx_segment[:12] + bytes(pixel_data) + tx_segment[12 + storage_pixel_size :]


def build_linear_tx_segment(
    *,
    width: int,
    height: int,
    rgba: bytes,
    pl_segment: bytes,
    palette_order: str,
) -> bytes:
    palette = texture_dump.parse_palette(pl_segment, palette_order=palette_order)
    if len(palette) <= 16:
        bpp = 4
    elif len(palette) <= 256:
        bpp = 8
    else:
        raise ValueError(f"unsupported palette size: {len(palette)}")
    stride = texture_dump._stride_bytes(width, bpp)
    tx_size = 12 + stride * height
    tx_segment = (
        TX_MAGIC
        + tx_size.to_bytes(4, "little")
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
        + (b"\0" * (stride * height))
    )
    return encode_png_into_tx(
        tx_segment=tx_segment,
        pl_segment=pl_segment,
        width=width,
        height=height,
        rgba=rgba,
        palette_order=palette_order,
    )


def maybe_patch_expanded_fonttile_mrg(
    *,
    rel_source: str,
    data: bytes | bytearray,
    tx_offset: int,
    palette_offset: int,
    width: int,
    height: int,
    rgba: bytes,
    palette_order: str,
) -> bytes | None:
    if rel_source != EXPANDABLE_FONTTILE_SOURCE:
        return None
    if data[:4] != MRG_MAGIC:
        return None
    if tx_offset + 12 > len(data) or data[tx_offset : tx_offset + 4] != TX_MAGIC:
        return None
    if palette_offset < 4 or palette_offset + 12 > len(data):
        return None

    old_tx_size = read_u32(data, tx_offset + 4)
    old_width = texture_dump.read_u16(data, tx_offset + 8)
    old_height = texture_dump.read_u16(data, tx_offset + 10)
    if width != old_width or height <= old_height:
        return None

    pl_size = read_u32(data, palette_offset + 4)
    if palette_offset + pl_size > len(data):
        raise ValueError(f"{rel_source}: PL segment exceeds source size")
    pl_segment = bytes(data[palette_offset : palette_offset + pl_size])
    if not pl_segment.startswith(PL_MAGIC):
        return None

    pl_prefix = bytes(data[palette_offset - 4 : palette_offset])
    if len(pl_prefix) != 4 or int.from_bytes(pl_prefix, "little") != pl_size:
        raise ValueError(f"{rel_source}: unexpected fonttile PL prefix at 0x{palette_offset - 4:x}")

    table_end = read_u32(data, 0x0C)
    if tx_offset != table_end:
        raise ValueError(f"{rel_source}: expandable fonttile TX is not at table_end")
    if read_u32(data, 0x08) < 2 or len(data) < 0x18:
        raise ValueError(f"{rel_source}: unexpected fonttile MRG table")
    if read_u32(data, 0x10) != palette_offset or read_u32(data, 0x14) != old_tx_size:
        raise ValueError(f"{rel_source}: unexpected fonttile MRG table values")

    new_tx = build_linear_tx_segment(
        width=width,
        height=height,
        rgba=rgba,
        pl_segment=pl_segment,
        palette_order=palette_order,
    )
    new_palette_offset = tx_offset + len(new_tx) + len(pl_prefix)
    new_data = bytearray()
    new_data.extend(data[:tx_offset])
    new_data.extend(new_tx)
    new_data.extend(pl_prefix)
    new_data.extend(pl_segment)
    new_data.extend(data[palette_offset + pl_size :])

    new_data[0x04:0x08] = len(new_data).to_bytes(4, "little")
    new_data[0x10:0x14] = new_palette_offset.to_bytes(4, "little")
    new_data[0x14:0x18] = len(new_tx).to_bytes(4, "little")
    return bytes(new_data)


def maybe_patch_expanded_linear_mrg(
    *,
    rel_source: str,
    data: bytes | bytearray,
    record: dict,
    tx_offset: int,
    palette_offset: int,
    width: int,
    height: int,
    rgba: bytes,
    pl_segment: bytes,
    palette_order: str,
    layout: str,
) -> bytes | None:
    if layout != "linear":
        return None
    if not data.startswith(MRG_MAGIC):
        return None
    if tx_offset < 0 or tx_offset + 12 > len(data) or data[tx_offset : tx_offset + 4] != TX_MAGIC:
        return None
    if palette_offset < 0 or palette_offset + 12 > len(data) or not pl_segment.startswith(PL_MAGIC):
        return None

    old_tx_size = read_u32(data, tx_offset + 4)
    old_width = texture_dump.read_u16(data, tx_offset + 8)
    old_height = texture_dump.read_u16(data, tx_offset + 10)
    old_tx_end = tx_offset + old_tx_size
    if old_tx_size <= 12 or old_tx_end > len(data):
        return None
    if width != old_width or height <= old_height:
        return None
    if palette_offset < old_tx_end:
        return None

    new_tx = build_linear_tx_segment(
        width=width,
        height=height,
        rgba=rgba,
        pl_segment=pl_segment,
        palette_order=palette_order,
    )
    delta = len(new_tx) - old_tx_size
    if delta <= 0:
        return None

    new_data = bytearray()
    new_data.extend(data[:tx_offset])
    new_data.extend(new_tx)
    new_data.extend(data[old_tx_end:])

    for offset in range(0, tx_offset + 1):
        if data[offset : offset + 4] != MRG_MAGIC or offset + 16 > len(data):
            continue
        total = read_u32(data, offset + 4)
        count = read_u32(data, offset + 8)
        table_end = read_u32(data, offset + 12)
        if total <= 0 or offset + total > len(data):
            continue
        if not (offset < tx_offset and old_tx_end <= offset + total):
            continue
        if count > 0x10000 or table_end != 16 + count * 4 or offset + table_end > len(data):
            raise ValueError(f"{rel_source}: unexpected MRG table while expanding TX")

        put_u32(new_data, offset + 4, total + delta)
        for index in range(count):
            table_offset = offset + 16 + index * 4
            value = read_u32(data, table_offset)
            absolute_value = offset + value
            if old_tx_end <= absolute_value < offset + total:
                put_u32(new_data, table_offset, value + delta)

    for field in ("palette_offset", "dialogue_line_control_offset", "cmp0_offset", "layout_offset"):
        value = optional_manifest_int(record, field)
        if value is not None and value >= old_tx_end:
            record[field] = value + delta
    record["height"] = height
    record["storage_height"] = height
    record["output_crop_height"] = height
    return bytes(new_data)


def record_rgba_view(
    rel_source: str,
    output_rel: str,
    width: int,
    height: int,
    rgba: bytes,
    record: dict,
) -> tuple[int, int, bytes]:
    crop_x = int(record.get("output_crop_x", 0) or 0)
    crop_y = int(record.get("output_crop_y", 0) or 0)
    crop_width = int(record.get("output_crop_width", 0) or width)
    crop_height = int(record.get("output_crop_height", 0) or height)
    clear_rects = parse_output_clear_rects(record.get("output_clear_rects", ""))
    if (
        crop_x == 0
        and crop_y == 0
        and crop_width == width
        and crop_height == height
        and not clear_rects
    ):
        return width, height, rgba
    if (
        crop_x < 0
        or crop_y < 0
        or crop_width <= 0
        or crop_height <= 0
        or crop_x + crop_width > width
        or crop_y + crop_height > height
    ):
        raise ValueError(
            f"{rel_source}: output crop for {output_rel} is outside PNG bounds "
            f"{crop_x},{crop_y},{crop_width},{crop_height} in {width}x{height}"
        )

    image = Image.frombytes("RGBA", (width, height), rgba)
    cropped = image.crop((crop_x, crop_y, crop_x + crop_width, crop_y + crop_height))
    cropped_rgba = bytearray(cropped.tobytes())
    for clear_x, clear_y, clear_width, clear_height in clear_rects:
        if clear_width <= 0 or clear_height <= 0:
            continue
        left = max(0, clear_x)
        top = max(0, clear_y)
        right = min(cropped.width, clear_x + clear_width)
        bottom = min(cropped.height, clear_y + clear_height)
        for y in range(top, bottom):
            row_start = (y * cropped.width + left) * 4
            row_end = (y * cropped.width + right) * 4
            cropped_rgba[row_start:row_end] = b"\x00\x00\x00\xff" * (right - left)
    return cropped.width, cropped.height, bytes(cropped_rgba)


def parse_output_clear_rects(value: object) -> list[tuple[int, int, int, int]]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid output_clear_rects: {value}") from exc
    if not isinstance(value, list):
        raise ValueError(f"invalid output_clear_rects: {value!r}")
    rects: list[tuple[int, int, int, int]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            raise ValueError(f"invalid output_clear_rects item: {item!r}")
        rects.append(tuple(int(part) for part in item))
    return rects


def patch_dialogue_line_control(patched: bytearray, record: dict, rel_source: str) -> tuple[int, int] | None:
    lengths = parse_patch_dialogue_line_lengths(record.get("dialogue_line_lengths", ""))
    if not lengths:
        return None

    palette_offset = int(record["palette_offset"])
    control = texture_dump.detect_dialogue_line_control(patched, palette_offset)
    if control is None:
        raise ValueError(f"{rel_source}: dialogue line control table was not found")

    manifest_offset = int(record.get("dialogue_line_control_offset", 0) or 0)
    if manifest_offset and manifest_offset != control.offset:
        raise ValueError(
            f"{rel_source}: manifest dialogue control offset 0x{manifest_offset:x} "
            f"does not match detected offset 0x{control.offset:x}"
        )

    if lengths == control.line_lengths and len(lengths) == control.line_count:
        return None

    new_info_size = texture_dump.dialogue_line_info_size(len(lengths))
    delta = new_info_size - control.line_info_size
    if delta > 0:
        insert_offset = control.phoneme_length_offset
        patched[insert_offset:insert_offset] = b"\x00" * delta
        update_mrg_tables_after_insert(patched, insert_offset, delta, rel_source)
    else:
        insert_offset = 0

    patched[control.offset : control.offset + 4] = (len(lengths) + 1).to_bytes(4, "little")
    patched[control.offset + 4] = len(lengths)
    lengths_start = control.offset + 5
    lengths_end = control.offset + 4 + new_info_size
    patched[lengths_start:lengths_end] = bytes(lengths) + b"\x00" * (
        lengths_end - lengths_start - len(lengths)
    )
    return (insert_offset, delta) if delta > 0 else None


def put_u32(buffer: bytearray, offset: int, value: int) -> None:
    buffer[offset : offset + 4] = int(value).to_bytes(4, "little")


def shift_manifest_offsets(records: list[dict], start_index: int, insert_offset: int, delta: int) -> None:
    if delta == 0:
        return
    for record in records[start_index:]:
        for field in ("offset", "palette_offset", "dialogue_line_control_offset", "cmp0_offset", "layout_offset"):
            value = optional_manifest_int(record, field)
            if value is not None and value >= insert_offset:
                record[field] = value + delta


def update_mrg_tables_after_insert(buffer: bytearray, insert_offset: int, delta: int, rel_source: str) -> None:
    if delta <= 0:
        return
    for offset in range(0, min(insert_offset + 1, len(buffer) - 15)):
        if buffer[offset : offset + 4] != MRG_MAGIC:
            continue
        total = read_u32(buffer, offset + 4)
        count = read_u32(buffer, offset + 8)
        table_end = read_u32(buffer, offset + 12)
        old_end = offset + total
        if total <= 0 or count > 0x10000 or old_end < insert_offset or old_end > len(buffer):
            continue
        if not (offset < insert_offset <= old_end):
            continue
        if table_end != 16 + count * 4 or offset + table_end > len(buffer):
            raise ValueError(f"{rel_source}: unexpected MRG table while expanding data")

        put_u32(buffer, offset + 4, total + delta)
        for index in range(count):
            table_offset = offset + 16 + index * 4
            value = read_u32(buffer, table_offset)
            absolute_value = offset + value
            if insert_offset <= absolute_value < old_end:
                put_u32(buffer, table_offset, value + delta)


def cmp0_slot_overflow_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        "CMP0 record is" in message
        and "larger than original" in message
        and "byte slot" in message
    )


def sd0_slot_overflow_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        "compressed SD0 record is" in message
        and "larger than original" in message
        and "byte slot" in message
    )


def mrg_child_declared_size(data: bytes, offset: int) -> int | None:
    if offset + 8 > len(data):
        return None
    magic = data[offset : offset + 4]
    if magic in (MRG_MAGIC, TX_MAGIC, PL_MAGIC, CMP0_MAGIC):
        return read_u32(data, offset + 4)
    if magic == PSET_MAGIC:
        if offset + 12 > len(data):
            return None
        return read_u32(data, offset + 8)
    return None


def mrg_child_magic_at(data: bytes, offset: int) -> bool:
    return data[offset : offset + 4] in (
        MRG_MAGIC,
        PSET_MAGIC,
        TX_MAGIC,
        PL_MAGIC,
        CMP0_MAGIC,
    )


def parse_standalone_pset_layout(data: bytes) -> StandalonePsetLayout | None:
    if not data.startswith(PSET_MAGIC) or len(data) < 0x100:
        return None
    total = read_u32(data, 8)
    if total != len(data):
        return None

    # This PSET2 flavor stores the resource count twice at 0x0c/0x0d,
    # then points to a small table header whose +0x0c area is a u32
    # resource-offset table.  Entries are relative to the second table word.
    if data[0x0D] == data[0x0C] and data[0x0C] != 0:
        resource_count = data[0x0C]
        pointer_table_offset = align(0x10 + resource_count, 4)
        if pointer_table_offset + 8 > len(data):
            return None
        pl_table_pointer_offset = pointer_table_offset
        table_header_offset = read_u32(data, pointer_table_offset + 4)
    else:
        resource_count = read_u32(data, 0x0C)
        if resource_count <= 0 or resource_count > 4096:
            return None
        pl_table_pointer_offset = 0x10
        table_header_offset = read_u32(data, 0x14)
    resource_table_offset = table_header_offset + 12
    resource_offset_base = resource_table_offset + 4
    table_end = resource_table_offset + resource_count * 4
    if (
        table_header_offset <= 0
        or resource_table_offset < table_header_offset
        or table_end > len(data)
    ):
        return None

    resource_starts = [
        read_u32(data, resource_table_offset + index * 4) + resource_offset_base
        for index in range(resource_count)
    ]
    if resource_starts != sorted(resource_starts):
        return None
    if resource_starts[0] < table_end or resource_starts[-1] >= len(data):
        return None
    if any(data[offset : offset + 4] not in (TX_MAGIC, CMP0_MAGIC) for offset in resource_starts):
        return None

    pl_table_start = read_u32(data, pl_table_pointer_offset) + 12
    if pl_table_start <= resource_starts[-1] or pl_table_start >= len(data):
        return None
    pl_offsets = [
        offset
        for offset in texture_dump.find_magic_offsets(data, PL_MAGIC, pl_table_start, len(data))
        if texture_dump.valid_pl_at(data, offset, len(data))
    ]
    if not pl_offsets:
        return None

    first_pl_offset = pl_offsets[0]
    if (
        pl_table_start > first_pl_offset
        or (first_pl_offset - pl_table_start) % 4 != 0
    ):
        return None
    pl_table_count = (first_pl_offset - pl_table_start) // 4
    if pl_table_count != len(pl_offsets):
        return None
    for index, pl_offset in enumerate(pl_offsets):
        table_value = read_u32(data, pl_table_start + index * 4)
        if table_value + pl_table_start != pl_offset:
            return None

    return StandalonePsetLayout(
        resource_table_offset=resource_table_offset,
        resource_offset_base=resource_offset_base,
        resource_starts=resource_starts,
        pl_table_pointer_offset=pl_table_pointer_offset,
        pl_table_start=pl_table_start,
    )


def root_pset_cmp0_blocks(data: bytes) -> tuple[list[int], list[PsetCmp0Block]] | None:
    if not data.startswith(MRG_MAGIC) or len(data) < 16:
        return None
    total = read_u32(data, 4)
    count = read_u32(data, 8)
    table_end = read_u32(data, 12)
    if total != len(data) or count < 1 or table_end != 16 + count * 4:
        return None

    table_values = [read_u32(data, 16 + index * 4) for index in range(count)]
    starts = [table_end]
    starts.extend(
        value
        for value in table_values
        if table_end <= value < total and data[value : value + 4] == PSET_MAGIC
    )
    starts = sorted(dict.fromkeys(starts))
    if not starts:
        return None

    blocks: list[PsetCmp0Block] = []
    for index, start in enumerate(starts):
        if start + 48 > len(data) or data[start : start + 4] != PSET_MAGIC:
            return None
        pset_total = read_u32(data, start + 8)
        if pset_total < 60 or start + pset_total > len(data):
            return None
        if read_u32(data, start + 16) != pset_total - 60:
            return None

        # This PSET2 flavor stores one CMP0 at +0x30, then a 4-byte field,
        # then the PL segment through the declared PSET total.  The gap is
        # not always zero and affects runtime rendering state, so preserve it.
        cmp0_offset = start + 48
        if data[cmp0_offset : cmp0_offset + 4] != CMP0_MAGIC:
            return None
        cmp0_stored_size = read_u32(data, cmp0_offset + 4)
        pl_offset = cmp0_offset + cmp0_stored_size + 4
        if (
            pl_offset + 12 > start + pset_total
            or data[pl_offset : pl_offset + 4] != PL_MAGIC
        ):
            return None
        pl_size = read_u32(data, pl_offset + 4)
        if pl_offset + pl_size != start + pset_total:
            return None

        next_start = starts[index + 1] if index + 1 < len(starts) else total
        if next_start < start + pset_total:
            return None
        padding_after = data[start + pset_total : next_start]
        blocks.append(
            PsetCmp0Block(
                index=index,
                start=start,
                total=pset_total,
                cmp0_offset=cmp0_offset,
                cmp0_stored_size=cmp0_stored_size,
                cmp0_pl_gap=bytes(data[cmp0_offset + cmp0_stored_size : pl_offset]),
                pl_offset=pl_offset,
                pl_size=pl_size,
                padding_after=padding_after,
            )
        )
    return table_values, blocks


def cmp0_record_for_relayout(
    tx_segment: bytes,
    original_stored_size: int,
    use_optimal_cmp0: bool,
) -> bytes:
    payload = cmp0_best_payload(tx_segment, use_optimal_cmp0)
    actual_size = 12 + len(payload)
    final_size = (
        original_stored_size
        if actual_size <= original_stored_size
        else align(actual_size, 4)
    )
    return cmp0_record_from_payload(tx_segment, payload, final_size)


def patch_standalone_pset_cmp0_relayout(
    rel_source: str,
    data: bytes,
    records: list[dict],
    textures_root: Path,
    image_cache: dict[str, tuple[int, int, bytes]],
    use_optimal_cmp0: bool,
) -> bytes | None:
    layout = parse_standalone_pset_layout(data)
    if layout is None:
        return None

    records_by_offset: dict[int, dict] = {}
    for record in records:
        if parse_patch_dialogue_line_lengths(record.get("dialogue_line_lengths", "")):
            return None
        tx_offset = int(record["offset"])
        if tx_offset in records_by_offset:
            return None
        encode_layout = str(record.get("layout", "linear"))
        if encode_layout.endswith("_cmp0"):
            encode_layout = encode_layout[:-5]
        if not encode_layout:
            encode_layout = "linear"
        if encode_layout != "linear":
            return None
        records_by_offset[tx_offset] = record

    resource_starts = layout.resource_starts
    resource_start_set = set(resource_starts)
    if not records_by_offset or any(offset not in resource_start_set for offset in records_by_offset):
        return None

    output = bytearray(data[: resource_starts[0]])
    new_starts: list[int] = []
    for index, start in enumerate(resource_starts):
        old_end = resource_starts[index + 1] if index + 1 < len(resource_starts) else layout.pl_table_start
        if old_end <= start:
            return None

        new_starts.append(len(output))
        record = records_by_offset.get(start)
        if record is None:
            output.extend(data[start:old_end])
            continue

        output_rel = str(record["output"])
        cached = image_cache.get(output_rel)
        if cached is None:
            cached = load_rgba_bytes(textures_root / output_rel)
            image_cache[output_rel] = cached
        full_width, full_height, full_rgba = cached
        width, height, rgba = record_rgba_view(
            rel_source=rel_source,
            output_rel=output_rel,
            width=full_width,
            height=full_height,
            rgba=full_rgba,
            record=record,
        )

        palette_offset = int(record["palette_offset"])
        if palette_offset < 0 or palette_offset + 12 > len(data):
            return None
        if data[palette_offset : palette_offset + 4] != PL_MAGIC:
            return None
        pl_size = read_u32(data, palette_offset + 4)
        pl_segment = bytes(data[palette_offset : palette_offset + pl_size])

        magic = data[start : start + 4]
        if magic == CMP0_MAGIC:
            tx_segment, cmp0_stored_size, _cmp0_unpacked_size = decode_cmp0_tx(data, start)
            old_payload_end = start + cmp0_stored_size
            if old_payload_end > old_end:
                return None
            new_tx = encode_png_into_tx(
                tx_segment=tx_segment,
                pl_segment=pl_segment,
                width=width,
                height=height,
                rgba=rgba,
                palette_order=str(record.get("palette_order", "linear")),
                layout="linear",
                tilemap=None,
            )
            if len(new_tx) != len(tx_segment):
                raise ValueError(f"{rel_source}: re-encoded CMP0 TX size changed")
            output.extend(
                cmp0_record_for_relayout(
                    new_tx,
                    original_stored_size=cmp0_stored_size,
                    use_optimal_cmp0=use_optimal_cmp0,
                )
            )
            output.extend(data[old_payload_end:old_end])
        elif magic == TX_MAGIC:
            tx_size = read_u32(data, start + 4)
            old_payload_end = start + tx_size
            if old_payload_end > old_end:
                return None
            tx_segment = bytes(data[start:old_payload_end])
            new_tx = encode_png_into_tx(
                tx_segment=tx_segment,
                pl_segment=pl_segment,
                width=width,
                height=height,
                rgba=rgba,
                palette_order=str(record.get("palette_order", "linear")),
                layout="linear",
                tilemap=None,
            )
            if len(new_tx) != tx_size:
                raise ValueError(f"{rel_source}: re-encoded TX size changed")
            output.extend(new_tx)
            output.extend(data[old_payload_end:old_end])
        else:
            return None

    new_pl_table_start = len(output)
    output.extend(data[layout.pl_table_start :])
    put_u32(output, 8, len(output))
    put_u32(output, layout.pl_table_pointer_offset, new_pl_table_start - 12)
    for index, new_start in enumerate(new_starts):
        put_u32(
            output,
            layout.resource_table_offset + index * 4,
            new_start - layout.resource_offset_base,
        )
    return bytes(output)


def mrg_pset_children(data: bytes) -> tuple[list[int], list[MrgChild]] | None:
    if not data.startswith(MRG_MAGIC) or len(data) < 16:
        return None
    total = read_u32(data, 4)
    count = read_u32(data, 8)
    table_end = read_u32(data, 12)
    if total != len(data) or count < 1 or table_end != 16 + count * 4:
        return None

    table_values = [read_u32(data, 16 + index * 4) for index in range(count)]
    starts: list[int] = []
    if table_end < total and mrg_child_magic_at(data, table_end):
        starts.append(table_end)
    starts.extend(
        value
        for value in table_values
        if table_end <= value < total and mrg_child_magic_at(data, value)
    )
    starts = sorted(dict.fromkeys(starts))
    if not starts:
        return None

    children: list[MrgChild] = []
    for index, start in enumerate(starts):
        child_total = mrg_child_declared_size(data, start)
        if child_total is None or child_total <= 0:
            return None
        next_start = starts[index + 1] if index + 1 < len(starts) else total
        if start + child_total > next_start:
            return None
        children.append(
            MrgChild(
                start=start,
                total=child_total,
                padding_after=data[start + child_total : next_start],
            )
        )
    return table_values, children


def patch_mrg_standalone_pset_relayout(
    rel_source: str,
    data: bytes,
    records: list[dict],
    textures_root: Path,
    image_cache: dict[str, tuple[int, int, bytes]],
    use_optimal_cmp0: bool,
) -> bytes | None:
    parsed = mrg_pset_children(data)
    if parsed is None:
        return None
    table_values, children = parsed

    child_records: dict[int, list[dict]] = {}
    for record in records:
        tx_offset = int(record["offset"])
        for child_index, child in enumerate(children):
            start = child.start
            total = child.total
            if start <= tx_offset < start + total:
                adjusted = dict(record)
                adjusted["offset"] = tx_offset - start
                palette_offset = optional_manifest_int(record, "palette_offset")
                if palette_offset is not None:
                    if not start <= palette_offset < start + total:
                        return None
                    adjusted["palette_offset"] = palette_offset - start
                cmp0_offset = optional_manifest_int(record, "cmp0_offset")
                if cmp0_offset is not None:
                    if not start <= cmp0_offset < start + total:
                        return None
                    adjusted["cmp0_offset"] = cmp0_offset - start
                child_records.setdefault(child_index, []).append(adjusted)
                break
        else:
            return None

    if not child_records:
        return None

    output = bytearray(data[: children[0].start])
    old_to_new_start: dict[int, int] = {}
    new_child_sizes: dict[int, int] = {}
    for child_index, child in enumerate(children):
        start = child.start
        total = child.total
        old_to_new_start[start] = len(output)
        child_data = data[start : start + total]
        records_for_child = child_records.get(child_index)
        if records_for_child:
            if data[start : start + 4] != PSET_MAGIC:
                return None
            new_child = patch_standalone_pset_cmp0_relayout(
                rel_source,
                child_data,
                records_for_child,
                textures_root,
                image_cache,
                use_optimal_cmp0,
            )
            if new_child is None:
                return None
        else:
            new_child = child_data
        new_child_sizes[start] = len(new_child)
        output.extend(new_child)
        output.extend(child.padding_after)

    first_start = children[0].start
    first_old_total = children[0].total
    for index, value in enumerate(table_values):
        table_offset = 16 + index * 4
        if value in old_to_new_start:
            put_u32(output, table_offset, old_to_new_start[value])
        elif value == first_old_total and data[value : value + 4] != PSET_MAGIC:
            put_u32(output, table_offset, new_child_sizes[first_start])
    put_u32(output, 4, len(output))
    return bytes(output)


def patch_mrg_cmp0_pset_relayout(
    rel_source: str,
    data: bytes,
    records: list[dict],
    textures_root: Path,
    image_cache: dict[str, tuple[int, int, bytes]],
    use_optimal_cmp0: bool,
) -> bytes | None:
    parsed = root_pset_cmp0_blocks(data)
    if parsed is None:
        return None
    table_values, blocks = parsed

    records_by_offset: dict[int, dict] = {}
    for record in records:
        if parse_patch_dialogue_line_lengths(record.get("dialogue_line_lengths", "")):
            return None
        tx_offset = int(record["offset"])
        if tx_offset in records_by_offset:
            return None
        layout = str(record.get("layout", "linear"))
        encode_layout = layout[:-5] if layout.endswith("_cmp0") else layout
        if encode_layout != "linear":
            return None
        records_by_offset[tx_offset] = record

    cmp0_offsets = {block.cmp0_offset for block in blocks}
    if not records_by_offset or any(offset not in cmp0_offsets for offset in records_by_offset):
        return None

    new_blocks: list[bytes] = []
    old_to_new_start: dict[int, int] = {}
    output = bytearray(data[: read_u32(data, 12)])
    for block in blocks:
        old_to_new_start[block.start] = len(output)
        record = records_by_offset.get(block.cmp0_offset)
        if record is None:
            new_block = data[block.start : block.start + block.total]
        else:
            output_rel = str(record["output"])
            cached = image_cache.get(output_rel)
            if cached is None:
                cached = load_rgba_bytes(textures_root / output_rel)
                image_cache[output_rel] = cached
            full_width, full_height, full_rgba = cached
            width, height, rgba = record_rgba_view(
                rel_source=rel_source,
                output_rel=output_rel,
                width=full_width,
                height=full_height,
                rgba=full_rgba,
                record=record,
            )

            palette_offset = int(record["palette_offset"])
            if palette_offset < 0 or palette_offset + 12 > len(data):
                return None
            if data[palette_offset : palette_offset + 4] != PL_MAGIC:
                return None
            pl_size = read_u32(data, palette_offset + 4)
            pl_segment = bytes(data[palette_offset : palette_offset + pl_size])

            tx_segment, cmp0_stored_size, _cmp0_unpacked_size = decode_cmp0_tx(
                data, block.cmp0_offset
            )
            new_tx = encode_png_into_tx(
                tx_segment=tx_segment,
                pl_segment=pl_segment,
                width=width,
                height=height,
                rgba=rgba,
                palette_order=str(record.get("palette_order", "linear")),
                layout="linear",
                tilemap=None,
            )
            if len(new_tx) != len(tx_segment):
                raise ValueError(f"{rel_source}: re-encoded CMP0 TX size changed")

            new_cmp0 = cmp0_record_for_relayout(
                new_tx,
                original_stored_size=cmp0_stored_size,
                use_optimal_cmp0=use_optimal_cmp0,
            )
            header = bytearray(data[block.start : block.cmp0_offset])
            new_total = len(header) + len(new_cmp0) + len(block.cmp0_pl_gap) + block.pl_size
            put_u32(header, 8, new_total)
            put_u32(header, 16, new_total - 60)
            new_block = (
                bytes(header)
                + new_cmp0
                + block.cmp0_pl_gap
                + data[block.pl_offset : block.pl_offset + block.pl_size]
            )
            if len(new_block) != new_total:
                raise AssertionError(f"{rel_source}: relayout PSET size mismatch")

        new_blocks.append(new_block)
        output.extend(new_block)
        output.extend(block.padding_after)

    new_first_total = len(new_blocks[0])
    old_first_total = blocks[0].total
    for index, value in enumerate(table_values):
        table_offset = 16 + index * 4
        if value in old_to_new_start:
            put_u32(output, table_offset, old_to_new_start[value])
        elif value == old_first_total and data[value : value + 4] != PSET_MAGIC:
            # Some MRGs store a leading table_end child and keep its PSET total
            # as the final table value rather than as a normal child offset.
            put_u32(output, table_offset, new_first_total)
    put_u32(output, 4, len(output))
    return bytes(output)


def record_targets_cmp0(data: bytes, record: dict) -> bool:
    layout = str(record.get("layout", ""))
    resource_kind = str(record.get("resource_kind", "")).lower()
    if layout.endswith("_cmp0") or resource_kind == "cmp0":
        return True
    tx_offset = int(record["offset"])
    return 0 <= tx_offset <= len(data) - 4 and data[tx_offset : tx_offset + 4] == CMP0_MAGIC


def patch_mrg_data_preserve_then_cmp0_relayout(
    rel_source: str,
    data: bytes,
    records: list[dict],
    textures_root: Path,
    image_cache: dict[str, tuple[int, int, bytes]],
    use_optimal_cmp0: bool,
) -> bytes | None:
    cmp0_records = [record for record in records if record_targets_cmp0(data, record)]
    preserve_records = [record for record in records if not record_targets_cmp0(data, record)]
    if not cmp0_records or not preserve_records:
        return None

    try:
        preserved = patch_mrg_data_preserve(
            rel_source,
            data,
            preserve_records,
            textures_root,
            image_cache,
            use_optimal_cmp0,
        )
    except ValueError:
        return None

    relayout = patch_standalone_pset_cmp0_relayout(
        rel_source,
        preserved,
        cmp0_records,
        textures_root,
        image_cache,
        use_optimal_cmp0,
    )
    if relayout is not None:
        return relayout
    relayout = patch_mrg_standalone_pset_relayout(
        rel_source,
        preserved,
        cmp0_records,
        textures_root,
        image_cache,
        use_optimal_cmp0,
    )
    if relayout is not None:
        return relayout
    return patch_mrg_cmp0_pset_relayout(
        rel_source,
        preserved,
        cmp0_records,
        textures_root,
        image_cache,
        use_optimal_cmp0,
    )


def patch_mrg_data_preserve(
    rel_source: str,
    data: bytes,
    records: list[dict],
    textures_root: Path,
    image_cache: dict[str, tuple[int, int, bytes]],
    use_optimal_cmp0: bool = False,
) -> bytes:
    patched = bytearray(data)
    for record_index, record in enumerate(records):
        output_rel = str(record["output"])
        cached = image_cache.get(output_rel)
        if cached is None:
            cached = load_rgba_bytes(textures_root / output_rel)
            image_cache[output_rel] = cached
        full_width, full_height, full_rgba = cached
        width, height, rgba = record_rgba_view(
            rel_source=rel_source,
            output_rel=output_rel,
            width=full_width,
            height=full_height,
            rgba=full_rgba,
            record=record,
        )

        tx_offset = int(record["offset"])
        palette_offset = int(record["palette_offset"])
        if tx_offset < 0 or tx_offset + 12 > len(patched):
            raise ValueError(f"{rel_source}: TX offset is out of range: {tx_offset}")
        if palette_offset < 0 or palette_offset + 12 > len(patched):
            raise ValueError(f"{rel_source}: PL offset is out of range: {palette_offset}")

        resource_kind = str(record.get("resource_kind", "")).lower()
        cmp0_offset = optional_manifest_int(record, "cmp0_offset")
        if cmp0_offset is not None and cmp0_offset != tx_offset:
            raise ValueError(
                f"{rel_source}: manifest cmp0_offset 0x{cmp0_offset:x} "
                f"does not match patch offset 0x{tx_offset:x}"
            )
        if resource_kind == "cmp0" and patched[tx_offset : tx_offset + 4] != CMP0_MAGIC:
            raise ValueError(f"{rel_source}: manifest CMP0 record does not point to CMP0 at 0x{tx_offset:x}")
        if resource_kind == "tx" and patched[tx_offset : tx_offset + 4] != TX_MAGIC:
            raise ValueError(f"{rel_source}: manifest TX record does not point to TX at 0x{tx_offset:x}")

        pl_size = read_u32(patched, palette_offset + 4)
        pl_segment = bytes(patched[palette_offset : palette_offset + pl_size])
        layout = str(record.get("layout", "linear"))
        is_cmp0 = patched[tx_offset : tx_offset + 4] == CMP0_MAGIC or layout.endswith("_cmp0")
        encode_layout = layout[:-5] if layout.endswith("_cmp0") else layout
        if not encode_layout:
            encode_layout = "linear"
        layout_record = record
        if encode_layout != layout:
            layout_record = dict(record)
            layout_record["layout"] = encode_layout
        tilemap = tilemap_from_record(patched, layout_record)

        expanded_fonttile = maybe_patch_expanded_fonttile_mrg(
            rel_source=rel_source,
            data=patched,
            tx_offset=tx_offset,
            palette_offset=palette_offset,
            width=width,
            height=height,
            rgba=rgba,
            palette_order=str(record.get("palette_order", "linear")),
        )
        if expanded_fonttile is not None:
            patched = bytearray(expanded_fonttile)
            shifted = patch_dialogue_line_control(patched, record, rel_source)
            if shifted is not None:
                shift_manifest_offsets(records, record_index + 1, *shifted)
            continue

        if is_cmp0:
            tx_segment, cmp0_stored_size, _cmp0_unpacked_size = decode_cmp0_tx(patched, tx_offset)
            effective_pl_segment = effective_pl_segment_for_record(
                rel_source=rel_source,
                record=record,
                tx_segment=tx_segment,
                tx_offset=tx_offset,
                pl_segment=pl_segment,
                palette_offset=palette_offset,
            )
            patch_effective_pl_segment(
                patched,
                palette_offset=palette_offset,
                original_pl_segment=pl_segment,
                effective_pl_segment=effective_pl_segment,
            )
            pl_segment = effective_pl_segment
            tilemap = retile_opening_title_if_needed(
                patched,
                layout_record,
                tilemap,
                rgba,
                width,
                height,
                texture_dump.read_u16(tx_segment, 8),
                texture_dump.read_u16(tx_segment, 10),
            )
            new_tx = encode_png_into_tx(
                tx_segment=tx_segment,
                pl_segment=pl_segment,
                width=width,
                height=height,
                rgba=rgba,
                palette_order=str(record.get("palette_order", "linear")),
                layout=encode_layout,
                tilemap=tilemap,
            )
            if len(new_tx) != len(tx_segment):
                raise ValueError(f"{rel_source}: re-encoded CMP0 TX size changed")
            new_cmp0 = encode_cmp0_tx(
                new_tx,
                stored_size=cmp0_stored_size,
                use_optimal=use_optimal_cmp0,
            )
            if len(new_cmp0) != cmp0_stored_size:
                raise ValueError(f"{rel_source}: re-encoded CMP0 size changed")
            patched[tx_offset : tx_offset + cmp0_stored_size] = new_cmp0
            shifted = patch_dialogue_line_control(patched, record, rel_source)
            if shifted is not None:
                shift_manifest_offsets(records, record_index + 1, *shifted)
            continue

        if patched[tx_offset : tx_offset + 4] != TX_MAGIC:
            raise ValueError(f"{rel_source}: target offset is not TX/CMP0: 0x{tx_offset:x}")
        tx_size = read_u32(patched, tx_offset + 4)
        tx_segment = bytes(patched[tx_offset : tx_offset + tx_size])
        effective_pl_segment = effective_pl_segment_for_record(
            rel_source=rel_source,
            record=record,
            tx_segment=tx_segment,
            tx_offset=tx_offset,
            pl_segment=pl_segment,
            palette_offset=palette_offset,
        )
        patch_effective_pl_segment(
            patched,
            palette_offset=palette_offset,
            original_pl_segment=pl_segment,
            effective_pl_segment=effective_pl_segment,
        )
        pl_segment = effective_pl_segment
        tilemap = retile_opening_title_if_needed(
            patched,
            layout_record,
            tilemap,
            rgba,
            width,
            height,
            texture_dump.read_u16(tx_segment, 8),
            texture_dump.read_u16(tx_segment, 10),
        )
        expanded_linear = maybe_patch_expanded_linear_mrg(
            rel_source=rel_source,
            data=patched,
            record=record,
            tx_offset=tx_offset,
            palette_offset=palette_offset,
            width=width,
            height=height,
            rgba=rgba,
            pl_segment=pl_segment,
            palette_order=str(record.get("palette_order", "linear")),
            layout=encode_layout,
        )
        if expanded_linear is not None:
            delta = len(expanded_linear) - len(patched)
            if delta > 0:
                shift_manifest_offsets(records, record_index + 1, tx_offset + tx_size, delta)
            patched = bytearray(expanded_linear)
            shifted = patch_dialogue_line_control(patched, record, rel_source)
            if shifted is not None:
                shift_manifest_offsets(records, record_index + 1, *shifted)
            continue
        new_tx = encode_png_into_tx(
            tx_segment=tx_segment,
            pl_segment=pl_segment,
            width=width,
            height=height,
            rgba=rgba,
            palette_order=str(record.get("palette_order", "linear")),
            layout=encode_layout,
            tilemap=tilemap,
        )
        if len(new_tx) != tx_size:
            raise ValueError(f"{rel_source}: re-encoded TX size changed")
        patched[tx_offset : tx_offset + tx_size] = new_tx
        shifted = patch_dialogue_line_control(patched, record, rel_source)
        if shifted is not None:
            shift_manifest_offsets(records, record_index + 1, *shifted)

    return bytes(patched)


def patch_mrg_data(
    rel_source: str,
    data: bytes,
    records: list[dict],
    textures_root: Path,
    image_cache: dict[str, tuple[int, int, bytes]],
    use_optimal_cmp0: bool = False,
) -> bytes:
    try:
        patched = patch_mrg_data_preserve(
            rel_source,
            data,
            records,
            textures_root,
            image_cache,
            use_optimal_cmp0,
        )
        return patched
    except ValueError as exc:
        if not cmp0_slot_overflow_error(exc):
            raise
        relayout = patch_standalone_pset_cmp0_relayout(
            rel_source,
            data,
            records,
            textures_root,
            image_cache,
            use_optimal_cmp0,
        )
        if relayout is not None:
            return relayout
        relayout = patch_mrg_standalone_pset_relayout(
            rel_source,
            data,
            records,
            textures_root,
            image_cache,
            use_optimal_cmp0,
        )
        if relayout is not None:
            return relayout
        relayout = patch_mrg_cmp0_pset_relayout(
            rel_source,
            data,
            records,
            textures_root,
            image_cache,
            use_optimal_cmp0,
        )
        if relayout is None:
            relayout = patch_mrg_data_preserve_then_cmp0_relayout(
                rel_source,
                data,
                records,
                textures_root,
                image_cache,
                use_optimal_cmp0,
            )
        if relayout is None:
            raise
        return relayout


def sd0_match_length(data: bytes, position: int, distance: int, max_length: int) -> int:
    length = 0
    match_position = position - distance
    while (
        length + 8 <= max_length
        and data[position + length : position + length + 8]
        == data[match_position + length : match_position + length + 8]
    ):
        length += 8
    while length < max_length and data[position + length] == data[match_position + length]:
        length += 1
    return length


def sd0_run_length(data: bytes, position: int, limit: int = 272) -> int:
    value = data[position]
    end = min(len(data), position + limit)
    length = 1
    while position + length < end and data[position + length] == value:
        length += 1
    return length


def sd0_best_match_rfind(
    data: bytes,
    position: int,
    max_candidates: int,
) -> tuple[int, int]:
    max_distance = min(4095, position)
    if max_distance <= 0 or position + 3 > len(data):
        return 0, 0

    max_length = min(271, len(data) - position)
    best_length = 0
    best_distance = 0

    for distance in range(1, min(2, max_distance) + 1):
        length = sd0_match_length(data, position, distance, max_length)
        if length >= 3 and length > best_length:
            best_length = length
            best_distance = distance
            if best_length == max_length:
                return best_length, best_distance

    start = position - max_distance
    key = data[position : position + 3]
    candidate = data.rfind(key, start, position)
    checked_candidates = 0
    while candidate >= start:
        distance = position - candidate
        if distance > 2:
            checked_candidates += 1
            if not (
                best_length >= 3
                and best_length < max_length
                and data[candidate + best_length] != data[position + best_length]
            ):
                length = sd0_match_length(data, position, distance, max_length)
                if length > best_length:
                    best_length = length
                    best_distance = distance
                    if length == max_length:
                        break
            if checked_candidates >= max_candidates:
                break
        candidate = data.rfind(key, start, candidate)

    return best_length, best_distance


class Sd0Compressor:
    def __init__(self, data: bytes, use_index: bool = True):
        self.data = data
        self.use_index = use_index
        self.positions: dict[bytes, list[int]] = {}
        self.indexed_until = 0

    def add_position(self, position: int) -> None:
        if not self.use_index:
            return
        if position + 3 <= len(self.data):
            self.positions.setdefault(self.data[position : position + 3], []).append(position)

    def add_range(self, start: int, end: int) -> None:
        if start != self.indexed_until:
            raise AssertionError("SD0 compressor index advanced out of order")
        for position in range(start, end):
            self.add_position(position)
        self.indexed_until = end

    def match_length(self, position: int, distance: int, max_length: int) -> int:
        return sd0_match_length(self.data, position, distance, max_length)

    def best_match(
        self,
        position: int,
        max_candidates: int | None = None,
    ) -> tuple[int, int]:
        if max_candidates is not None:
            return sd0_best_match_rfind(self.data, position, max_candidates)

        max_distance = min(4095, position)
        if max_distance <= 0 or position + 3 > len(self.data):
            return 0, 0

        max_length = min(271, len(self.data) - position)
        best_length = 0
        best_distance = 0

        for distance in range(1, min(2, max_distance) + 1):
            length = self.match_length(position, distance, max_length)
            if length >= 3 and length > best_length:
                best_length = length
                best_distance = distance
                if best_length == max_length:
                    return best_length, best_distance

        start = position - max_distance
        checked_candidates = 0
        for candidate in reversed(self.positions.get(self.data[position : position + 3], [])):
            if candidate < start:
                break
            distance = position - candidate
            if distance <= 2:
                continue
            checked_candidates += 1
            if (
                best_length >= 3
                and best_length < max_length
                and self.data[candidate + best_length] != self.data[position + best_length]
            ):
                if max_candidates is not None and checked_candidates >= max_candidates:
                    break
                continue
            length = self.match_length(position, distance, max_length)
            if length > best_length:
                best_length = length
                best_distance = distance
                if length == max_length:
                    break
            if max_candidates is not None and checked_candidates >= max_candidates:
                break

        return best_length, best_distance

    def run_length(self, position: int, limit: int = 272) -> int:
        return sd0_run_length(self.data, position, limit=limit)

    def choose_token(
        self,
        position: int,
        max_match_candidates: int | None = None,
    ) -> tuple[str, int, int] | None:
        full_run = self.run_length(position)
        rle_length = min(full_run, 18) if full_run >= 3 else 0
        match_length, distance = self.best_match(
            position,
            max_candidates=max_match_candidates,
        )

        if rle_length >= 3 and (match_length > 0 or full_run <= 36) and rle_length >= match_length:
            return "R", rle_length, self.data[position]
        if match_length >= 3:
            return "M", min(match_length, 271), distance
        return None

    def iter_tokens(
        self,
        max_match_candidates: int | None = None,
    ) -> Iterable[tuple[str, int, int, int]]:
        position = 0
        while position < len(self.data):
            token = self.choose_token(position, max_match_candidates=max_match_candidates)
            if token is not None:
                kind, length, value = token
                yield kind, length, value, position
                new_position = position + length
                self.add_range(position, new_position)
                position = new_position
                continue

            start = position
            position += 1
            self.add_range(start, position)
            while position < len(self.data) and position - start < 4113:
                if self.choose_token(position, max_match_candidates=max_match_candidates) is not None:
                    break
                self.add_range(position, position + 1)
                position += 1

            length = position - start
            if length >= 18:
                yield "B", length, 0, start
            else:
                for literal_position in range(start, position):
                    yield "L", 1, self.data[literal_position], literal_position


def token_payload_size(token: Sd0Token) -> int:
    kind, length, _value, _position = token
    if kind == "L":
        return 1
    if kind in {"R", "B"}:
        return 2 if kind == "R" else length + 2
    if kind == "M":
        return 3 if length >= 16 else 2
    raise AssertionError(f"unknown SD0 token: {kind}")


def encode_sd0_tokens(
    data: bytes,
    tokens: Iterable[Sd0Token],
    stored_size: int | None = None,
) -> bytes:
    output = bytearray(SD0_MAGIC + b"\x00\x00\x00\x00" + len(data).to_bytes(4, "little"))
    group: list[Sd0Token] = []

    def flush_group() -> None:
        if not group:
            return
        flags = 0
        payload = bytearray()
        for bit, (kind, length, value, position) in enumerate(group):
            if kind == "L":
                payload.append(value)
                continue

            flags |= 1 << bit
            if kind == "R":
                payload.extend((((length - 3) << 4) | 1, value))
            elif kind == "B":
                encoded_length = length - 18
                payload.extend((((encoded_length & 0x0F) << 4) | 2, encoded_length >> 4))
                payload.extend(data[position : position + length])
            elif kind == "M":
                distance = value
                if length >= 16:
                    payload.extend((((distance & 0x0F) << 4), distance >> 4, length - 16))
                else:
                    payload.extend((((distance & 0x0F) << 4) | length, distance >> 4))
            else:
                raise AssertionError(f"unknown SD0 token: {kind}")

        output.append(flags)
        output.extend(payload)
        group.clear()

    for token in tokens:
        group.append(token)
        if len(group) == 8:
            flush_group()
    flush_group()

    actual_size = align(len(output), 4)
    final_size = stored_size if stored_size is not None else actual_size
    if final_size < actual_size:
        raise ValueError(
            f"compressed SD0 record is {actual_size} bytes, larger than original "
            f"{final_size} byte slot"
        )
    if final_size % 4:
        raise ValueError(f"SD0 stored size must be 4-byte aligned: {final_size}")

    output[4:8] = final_size.to_bytes(4, "little")
    output.extend(b"\x00" * (final_size - len(output)))
    return bytes(output)


def greedy_sd0_tokens(
    data: bytes,
    max_match_candidates: int | None = None,
) -> list[Sd0Token]:
    compressor = Sd0Compressor(data, use_index=max_match_candidates is None)
    return list(compressor.iter_tokens(max_match_candidates=max_match_candidates))


def precompute_sd0_matches(
    data: bytes,
    max_match_candidates: int | None = None,
) -> tuple[list[tuple[int, int]], list[int]]:
    if max_match_candidates is not None:
        matches = []
        runs = []
        for position in range(len(data)):
            matches.append(sd0_best_match_rfind(data, position, max_match_candidates))
            runs.append(sd0_run_length(data, position))
        return matches, runs

    compressor = Sd0Compressor(data)
    matches: list[tuple[int, int]] = []
    runs: list[int] = []
    for position in range(len(data)):
        matches.append(compressor.best_match(position, max_candidates=max_match_candidates))
        runs.append(compressor.run_length(position))
        compressor.add_range(position, position + 1)
    return matches, runs


def collapse_literal_tokens(tokens: list[Sd0Token]) -> list[Sd0Token]:
    collapsed: list[Sd0Token] = []
    index = 0
    while index < len(tokens):
        kind, _length, _value, position = tokens[index]
        if kind != "L":
            collapsed.append(tokens[index])
            index += 1
            continue

        start = index
        while (
            index < len(tokens)
            and tokens[index][0] == "L"
            and tokens[index][3] == position + index - start
        ):
            index += 1

        literal_count = index - start
        offset = 0
        while literal_count - offset >= 18:
            chunk_length = min(4113, literal_count - offset)
            if literal_count - offset - chunk_length and literal_count - offset - chunk_length < 18:
                chunk_length -= 18
            collapsed.append(("B", chunk_length, 0, position + offset))
            offset += chunk_length
        while offset < literal_count:
            literal_position = position + offset
            collapsed.append(("L", 1, tokens[start + offset][2], literal_position))
            offset += 1

    return collapsed


def optimal_sd0_tokens(
    data: bytes,
    max_match_candidates: int | None = None,
) -> list[Sd0Token]:
    matches, runs = precompute_sd0_matches(data, max_match_candidates=max_match_candidates)
    data_length = len(data)
    state_count = (data_length + 1) * 8
    costs = [0] * state_count
    choice_kind = bytearray(data_length * 8)
    choice_length = array("H", [0]) * (data_length * 8)
    choice_value = array("H", [0]) * (data_length * 8)
    literal_block_windows: list[deque[tuple[int, int]]] = [deque() for _ in range(8)]

    for position in range(data_length - 1, -1, -1):
        literal_block_end = position + 18
        if literal_block_end <= data_length:
            for token_mod in range(8):
                value = literal_block_end + costs[literal_block_end * 8 + token_mod]
                window = literal_block_windows[token_mod]
                while window and window[-1][1] >= value:
                    window.pop()
                window.append((literal_block_end, value))
        literal_block_limit = position + 4113
        for window in literal_block_windows:
            while window and window[0][0] > literal_block_limit:
                window.popleft()

        state_base = position * 8
        byte_value = data[position]
        rle_limit = min(runs[position], 18)
        match_length, match_distance = matches[position]
        match_limit = min(match_length, 271)

        for token_mod in range(8):
            next_mod = (token_mod + 1) & 7
            flag_cost = 1 if token_mod == 0 else 0
            best_cost = flag_cost + 1 + costs[(position + 1) * 8 + next_mod]
            best_kind = 0
            best_length = 1
            best_value = byte_value

            if rle_limit >= 3:
                for length in range(3, rle_limit + 1):
                    cost = flag_cost + 2 + costs[(position + length) * 8 + next_mod]
                    if cost < best_cost:
                        best_cost = cost
                        best_kind = 1
                        best_length = length
                        best_value = byte_value

            if match_limit >= 3:
                for length in range(3, match_limit + 1):
                    payload_cost = 3 if length >= 16 else 2
                    cost = (
                        flag_cost
                        + payload_cost
                        + costs[(position + length) * 8 + next_mod]
                    )
                    if cost < best_cost:
                        best_cost = cost
                        best_kind = 2
                        best_length = length
                        best_value = match_distance

            literal_block_window = literal_block_windows[next_mod]
            if literal_block_window:
                literal_end, _window_cost = literal_block_window[0]
                literal_length = literal_end - position
                cost = (
                    flag_cost
                    + literal_length
                    + 2
                    + costs[literal_end * 8 + next_mod]
                )
                if cost < best_cost:
                    best_cost = cost
                    best_kind = 3
                    best_length = literal_length
                    best_value = 0

            state = state_base + token_mod
            costs[state] = best_cost
            choice_kind[state] = best_kind
            choice_length[state] = best_length
            choice_value[state] = best_value

    tokens: list[Sd0Token] = []
    position = 0
    token_mod = 0
    while position < data_length:
        state = position * 8 + token_mod
        kind_code = choice_kind[state]
        length = choice_length[state]
        value = choice_value[state]
        if kind_code == 0:
            token: Sd0Token = ("L", 1, value, position)
        elif kind_code == 1:
            token = ("R", length, value, position)
        elif kind_code == 2:
            token = ("M", length, value, position)
        elif kind_code == 3:
            token = ("B", length, 0, position)
        else:
            raise AssertionError(f"unknown SD0 token choice: {kind_code}")
        tokens.append(token)
        position += token[1]
        token_mod = (token_mod + 1) & 7

    return tokens


def ensure_sd0_fast_codec() -> Path | None:
    global _SD0_FAST_CODEC_READY

    if os.environ.get("GIREN_DISABLE_SD0_FAST_CODEC"):
        _SD0_FAST_CODEC_READY = False
        return None
    if isinstance(_SD0_FAST_CODEC_READY, Path):
        return _SD0_FAST_CODEC_READY
    if _SD0_FAST_CODEC_READY is False:
        return None

    binary = SD0_FAST_CODEC_BINARY
    try:
        if (
            binary.exists()
            and binary.stat().st_mtime >= SD0_FAST_CODEC_SOURCE.stat().st_mtime
        ):
            _SD0_FAST_CODEC_READY = binary
            return binary
    except OSError:
        pass

    compiler = shutil.which("clang++") or shutil.which("c++") or shutil.which("g++")
    if compiler is None or not SD0_FAST_CODEC_SOURCE.exists():
        _SD0_FAST_CODEC_READY = False
        return None

    binary.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            compiler,
            "-O3",
            "-std=c++17",
            str(SD0_FAST_CODEC_SOURCE),
            "-o",
            str(binary),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        details = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        print(f"SD0 native codec build failed; using Python compressor: {details}", file=sys.stderr)
        _SD0_FAST_CODEC_READY = False
        return None

    _SD0_FAST_CODEC_READY = binary
    return binary


def compress_sd0_native(data: bytes, stored_size: int) -> bytes | None:
    codec = ensure_sd0_fast_codec()
    if codec is None:
        return None

    result = subprocess.run(
        [str(codec), "--stored-size", str(stored_size)],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 0:
        if len(result.stdout) != stored_size:
            raise ValueError(
                f"native SD0 compressor returned {len(result.stdout)} bytes, "
                f"expected {stored_size}"
            )
        return result.stdout

    message = result.stderr.decode("utf-8", errors="replace").strip()
    if not message:
        message = f"native SD0 compressor failed with exit code {result.returncode}"
    raise ValueError(message)


def compress_sd0(
    data: bytes,
    stored_size: int | None = None,
    use_optimal: bool = False,
) -> bytes:
    if len(data) >= 0x1000000:
        raise ValueError("SD0 calldll header only supports unpacked sizes below 16 MiB")

    native_error_message = ""
    if use_optimal and stored_size is not None:
        try:
            native_chunk = compress_sd0_native(data, stored_size)
        except ValueError as exc:
            native_error_message = str(exc)
        else:
            if native_chunk is not None:
                return native_chunk

    greedy_tokens = greedy_sd0_tokens(
        data,
        max_match_candidates=SD0_FAST_GREEDY_MATCH_CANDIDATES if use_optimal else None,
    )
    greedy_error_message = ""
    try:
        return encode_sd0_tokens(data, greedy_tokens, stored_size=stored_size)
    except ValueError as exc:
        if stored_size is None or not use_optimal:
            raise
        greedy_error_message = str(exc)

    optimal_error: ValueError | None = None
    for max_candidates in SD0_OPTIMAL_MATCH_CANDIDATE_STEPS:
        optimal_tokens = optimal_sd0_tokens(data, max_match_candidates=max_candidates)
        try:
            return encode_sd0_tokens(data, optimal_tokens, stored_size=stored_size)
        except ValueError as exc:
            optimal_error = exc

    if optimal_error is None:
        raise AssertionError("SD0 optimal candidate ladder did not run")
    raise ValueError(
        f"{optimal_error}; fast optimal fallback also failed after candidate caps "
        f"{SD0_OPTIMAL_MATCH_CANDIDATE_STEPS}; greedy fallback also failed: "
        f"{greedy_error_message}"
        + (f"; native fallback also failed: {native_error_message}" if native_error_message else "")
    ) from optimal_error


def entry_files(unpacked_dir: Path) -> dict[int, Path]:
    files: dict[int, Path] = {}
    for path in sorted(item for item in unpacked_dir.iterdir() if item.is_file()):
        try:
            index = int(path.stem, 16)
        except ValueError:
            continue
        if index in files:
            raise ValueError(f"duplicate extracted entry index {index:08x} in {unpacked_dir}")
        files[index] = path
    return files


def first_difference(left: bytes, right: bytes) -> int | None:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def rebuild_entry_patch_task(task: EntryPatchTask) -> EntryPatchResult:
    try:
        source_path = Path(task.source_path)
        raw_original = source_path.read_bytes()
        raw = patch_mrg_data(
            rel_source=task.rel_source,
            data=raw_original,
            records=task.records,
            textures_root=Path(task.textures_root),
            image_cache=_WORKER_IMAGE_CACHE,
            use_optimal_cmp0=task.use_optimal_cmp0,
        )
        changed = raw != raw_original

        if task.kind == "SD0":
            if task.reuse_unchanged and not changed:
                return EntryPatchResult(
                    entry_index=task.entry_index,
                    chunk=None,
                    reused=1,
                    texture_records=len(task.records),
                    texture_sources=1,
                )
            try:
                chunk = compress_sd0(
                    raw,
                    stored_size=task.stored_size,
                    use_optimal=task.use_optimal_sd0,
                )
            except ValueError as exc:
                if (
                    not sd0_slot_overflow_error(exc)
                    or task.envelope_size <= task.stored_size
                ):
                    raise
                chunk = compress_sd0(
                    raw,
                    stored_size=task.envelope_size,
                    use_optimal=task.use_optimal_sd0,
                )
            return EntryPatchResult(
                entry_index=task.entry_index,
                chunk=chunk,
                recompressed=1,
                texture_records=len(task.records),
                texture_sources=1,
                changed_files=1 if changed else 0,
            )

        if task.kind == "RIFF":
            if len(raw) != task.stored_size:
                raise ValueError(
                    f"RIFF size changed from {task.stored_size} to {len(raw)}; "
                    "RIFF relayout is not supported"
                )
            return EntryPatchResult(
                entry_index=task.entry_index,
                chunk=raw,
                riff=1,
                texture_records=len(task.records),
                texture_sources=1,
                changed_files=1 if changed else 0,
            )

        raise AssertionError(f"unknown entry kind: {task.kind}")
    except Exception as exc:
        raise type(exc)(
            f"archive {task.archive} entry {task.entry_index:08x} "
            f"({task.rel_source}): {exc}"
        ) from exc


def add_entry_result(stats: RebuildStats, result: EntryPatchResult) -> None:
    stats.reused += result.reused
    stats.recompressed += result.recompressed
    stats.riff += result.riff
    stats.texture_records += result.texture_records
    stats.texture_sources += result.texture_sources
    stats.changed_files += result.changed_files


def stop_executor_now(executor: ProcessPoolExecutor) -> None:
    for method_name in ("terminate_workers", "kill_workers"):
        method = getattr(executor, method_name, None)
        if method is not None:
            method()
            return
    executor.shutdown(wait=False, cancel_futures=True)


def rebuild_archive(
    archive: int,
    original_dir: Path,
    unpacked_root: Path,
    baseline_unpacked_root: Path | None,
    out_dir: Path,
    patch_set: TexturePatchSet,
    textures_root: Path | None,
    reuse_unchanged: bool,
    preserve_layout: bool,
    verify: bool,
    write_staged_unpacked: Path | None,
    use_optimal_sd0: bool,
    use_optimal_cmp0: bool,
    jobs: int,
    allow_basename_source_records: bool,
) -> RebuildStats:
    original_path = original_dir / f"ZZZPSP{archive}.MKD"
    unpacked_dir = unpacked_root / f"unpacked_{archive}"
    if not original_path.exists():
        raise FileNotFoundError(f"missing original MKD: {original_path}")
    if not unpacked_dir.exists():
        raise FileNotFoundError(f"missing unpacked directory: {unpacked_dir}")

    original = original_path.read_bytes()
    entries, tail_offset = parse_mkd(original)
    files = entry_files(unpacked_dir)
    if len(files) != len(entries):
        raise ValueError(
            f"archive {archive}: extracted file count {len(files)} does not match "
            f"MKD entry count {len(entries)}"
        )

    stats = RebuildStats(archive=archive, entries=len(entries))
    output = bytearray(original) if preserve_layout else bytearray()
    image_cache: dict[str, tuple[int, int, bytes]] = {}

    staged_archive_dir: Path | None = None
    if write_staged_unpacked is not None:
        staged_archive_dir = write_staged_unpacked / f"unpacked_{archive}"
        if staged_archive_dir.exists():
            shutil.rmtree(staged_archive_dir)
        staged_archive_dir.mkdir(parents=True)

    record_entries: list[tuple[MkdEntry, Path, str, list[dict]]] = []
    for entry in entries:
        source_path = files.get(entry.index)
        if source_path is None:
            raise FileNotFoundError(f"archive {archive}: missing extracted entry {entry.index:08x}")
        rel_source = source_path.relative_to(unpacked_root).as_posix()
        records = patch_set.records_by_source.get(rel_source, [])
        if not records and allow_basename_source_records:
            records = patch_set.records_by_source.get(source_path.name, [])
        if records:
            if textures_root is None:
                raise AssertionError("texture records selected without a textures root")
            record_entries.append((entry, source_path, rel_source, records))

    parallel_jobs = min(jobs, len(record_entries))
    use_parallel_entries = (
        parallel_jobs > 1
        and preserve_layout
        and reuse_unchanged
        and staged_archive_dir is None
        and textures_root is not None
        and baseline_unpacked_root is None
    )
    if use_parallel_entries:
        print(
            f"ZZZPSP{archive}.MKD: parallel texture rebuild "
            f"jobs={parallel_jobs} sources={len(record_entries)}"
        )
        record_entry_indices = {entry.index for entry, _path, _rel, _records in record_entries}
        for entry in entries:
            if entry.index in record_entry_indices:
                continue
            if entry.kind == "SD0":
                stats.reused += 1
            elif entry.kind == "RIFF":
                stats.riff += 1
            else:
                raise AssertionError(f"unknown entry kind: {entry.kind}")

        executor: ProcessPoolExecutor | None = ProcessPoolExecutor(max_workers=parallel_jobs)
        try:
            future_entries = {
                executor.submit(
                    rebuild_entry_patch_task,
                    EntryPatchTask(
                        archive=archive,
                        entry_index=entry.index,
                        kind=entry.kind,
                        source_path=str(source_path.resolve()),
                        rel_source=rel_source,
                        stored_size=entry.stored_size,
                        envelope_size=entry.next_offset - entry.offset,
                        records=records,
                        textures_root=str(textures_root.resolve()),
                        reuse_unchanged=reuse_unchanged,
                        use_optimal_sd0=use_optimal_sd0,
                        use_optimal_cmp0=use_optimal_cmp0,
                    ),
                ): (entry, source_path, rel_source, records)
                for entry, source_path, rel_source, records in record_entries
            }
            try:
                for future in as_completed(future_entries):
                    result = future.result()
                    add_entry_result(stats, result)
                    if result.chunk is None:
                        continue
                    entry = entries[result.entry_index]
                    envelope_size = entry.next_offset - entry.offset
                    if len(result.chunk) not in {entry.stored_size, envelope_size}:
                        raise AssertionError(
                            "preserved-layout chunks must fill the original stored size "
                            "or aligned record envelope"
                        )
                    output[entry.offset : entry.offset + len(result.chunk)] = result.chunk
            except BaseException:
                for future in future_entries:
                    future.cancel()
                stop_executor_now(executor)
                executor = None
                raise
        finally:
            if executor is not None:
                executor.shutdown()
    else:
        for entry in entries:
            source_path = files.get(entry.index)
            if source_path is None:
                raise FileNotFoundError(
                    f"archive {archive}: missing extracted entry {entry.index:08x}"
                )

            rel_source = source_path.relative_to(unpacked_root).as_posix()
            records = patch_set.records_by_source.get(rel_source, [])
            if not records and allow_basename_source_records:
                records = patch_set.records_by_source.get(source_path.name, [])
            original_chunk = original[entry.offset : entry.offset + entry.stored_size]
            raw_original: bytes | None = None
            source_changed_from_baseline = False
            reuse_original_chunk = reuse_unchanged and not records and staged_archive_dir is None
            if reuse_original_chunk and baseline_unpacked_root is not None:
                baseline_path = baseline_unpacked_root / rel_source
                if not baseline_path.exists():
                    raise FileNotFoundError(
                        f"archive {archive}: missing baseline entry {rel_source} "
                        f"under {baseline_unpacked_root}"
                    )
                raw_original = source_path.read_bytes()
                source_changed_from_baseline = raw_original != baseline_path.read_bytes()
                reuse_original_chunk = not source_changed_from_baseline

            if reuse_original_chunk:
                if entry.kind == "SD0":
                    chunk = original_chunk
                    stats.reused += 1
                elif entry.kind == "RIFF":
                    chunk = original_chunk
                    stats.riff += 1
                else:
                    raise AssertionError(f"unknown entry kind: {entry.kind}")
            else:
                if raw_original is None:
                    raw_original = source_path.read_bytes()
                raw = raw_original
                if records:
                    if textures_root is None:
                        raise AssertionError("texture records selected without a textures root")
                    try:
                        raw = patch_mrg_data(
                            rel_source,
                            raw,
                            records,
                            textures_root,
                            image_cache,
                            use_optimal_cmp0=use_optimal_cmp0,
                        )
                    except ValueError as exc:
                        raise ValueError(
                            f"archive {archive} entry {entry.index:08x} "
                            f"({rel_source}): {exc}"
                        ) from exc
                    stats.texture_records += len(records)
                    stats.texture_sources += 1

                if raw != raw_original or source_changed_from_baseline:
                    stats.changed_files += 1

                if staged_archive_dir is not None:
                    (staged_archive_dir / source_path.name).write_bytes(raw)

                if entry.kind == "SD0":
                    if reuse_unchanged and raw == raw_original and not source_changed_from_baseline:
                        chunk = original_chunk
                        stats.reused += 1
                    else:
                        try:
                            chunk = compress_sd0(
                                raw,
                                stored_size=entry.stored_size if preserve_layout else None,
                                use_optimal=use_optimal_sd0,
                            )
                        except ValueError as exc:
                            envelope_size = entry.next_offset - entry.offset
                            if (
                                preserve_layout
                                and sd0_slot_overflow_error(exc)
                                and envelope_size > entry.stored_size
                            ):
                                try:
                                    chunk = compress_sd0(
                                        raw,
                                        stored_size=envelope_size,
                                        use_optimal=use_optimal_sd0,
                                    )
                                except ValueError as envelope_exc:
                                    raise ValueError(
                                        f"archive {archive} entry {entry.index:08x} "
                                        f"({rel_source}): {envelope_exc}"
                                    ) from envelope_exc
                            else:
                                raise ValueError(
                                    f"archive {archive} entry {entry.index:08x} "
                                    f"({rel_source}): {exc}"
                                ) from exc
                        stats.recompressed += 1
                elif entry.kind == "RIFF":
                    stats.riff += 1
                    if len(raw) != entry.stored_size:
                        raise ValueError(
                            f"archive {archive} entry {entry.index:08x}: RIFF size changed from "
                            f"{entry.stored_size} to {len(raw)}; RIFF relayout is not supported"
                        )
                    chunk = raw
                else:
                    raise AssertionError(f"unknown entry kind: {entry.kind}")

            if preserve_layout:
                envelope_size = entry.next_offset - entry.offset
                if len(chunk) not in {entry.stored_size, envelope_size}:
                    raise AssertionError(
                        "preserved-layout chunks must fill the original stored size "
                        "or aligned record envelope"
                    )
                output[entry.offset : entry.offset + len(chunk)] = chunk
            else:
                padding = align(len(output)) - len(output)
                if padding:
                    output.extend(b"\x00" * padding)
                output.extend(chunk)

    if not preserve_layout and tail_offset is not None:
        padding = align(len(output)) - len(output)
        if padding:
            output.extend(b"\x00" * padding)
        output.extend(original[tail_offset:])

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / original_path.name
    out_path.write_bytes(output)

    if verify:
        rebuilt = bytes(output)
        stats.identical = rebuilt == original
        if not stats.identical:
            diff = first_difference(rebuilt, original)
            print(
                f"ZZZPSP{archive}.MKD: DIFF size={len(rebuilt)} original={len(original)} "
                f"first_diff=0x{diff:x}" if diff is not None else f"ZZZPSP{archive}.MKD: DIFF"
            )
        else:
            print(f"ZZZPSP{archive}.MKD: OK byte-identical")

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--original-dir",
        default="ExtractedISO/PSP_GAME/USRDIR",
        help="directory containing original ZZZPSP*.MKD files",
    )
    parser.add_argument(
        "--unpacked",
        default="unpacked_mkd",
        help="root containing unpacked_0, unpacked_1, ... extracted files",
    )
    parser.add_argument(
        "--baseline-unpacked",
        default=None,
        help=(
            "optional original unpacked root used to detect non-texture file changes; "
            "entries that match the baseline reuse their original compressed chunks"
        ),
    )
    parser.add_argument(
        "--out",
        default="rebuilt_mkd",
        help="directory where rebuilt ZZZPSP*.MKD files are written",
    )
    parser.add_argument(
        "--archives",
        default=None,
        help="comma-separated archives/ranges to rebuild, for example 0-8 or 0,2,9",
    )
    parser.add_argument(
        "--apply-textures",
        default=None,
        help="textures_static directory whose manifest/PNGs should be applied to MRG TX records",
    )
    parser.add_argument(
        "--force-reencode-textures",
        action="store_true",
        help="re-encode every manifest texture even when the PNG digest is unchanged",
    )
    parser.add_argument(
        "--no-reuse-unchanged",
        action="store_true",
        help="run the SD0 compressor even for extracted files that are byte-identical",
    )
    parser.add_argument(
        "--relayout",
        action="store_true",
        help="write records sequentially instead of preserving original offsets/slots",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="compare rebuilt MKD files with the originals and fail on differences",
    )
    parser.add_argument(
        "--write-staged-unpacked",
        default=None,
        help="optional directory where texture-applied extracted files are written",
    )
    parser.add_argument(
        "--optimal-sd0",
        action="store_true",
        help="try a slower optimal SD0 parse when greedy compression does not fit",
    )
    parser.add_argument(
        "--optimal-cmp0",
        action="store_true",
        help="try a slower optimal CMP0 zero/literal split when greedy compression does not fit",
    )
    parser.add_argument(
        "--jobs",
        default="auto",
        type=parse_jobs,
        help="parallel texture patch/recompression workers; use 1 to disable (default: auto)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    original_dir = Path(args.original_dir)
    unpacked_root = Path(args.unpacked)
    baseline_unpacked_root = Path(args.baseline_unpacked) if args.baseline_unpacked else None
    out_dir = Path(args.out)
    textures_root = Path(args.apply_textures) if args.apply_textures else None
    staged_root = Path(args.write_staged_unpacked) if args.write_staged_unpacked else None
    jobs = resolve_jobs(args.jobs)
    if args.optimal_sd0:
        ensure_sd0_fast_codec()

    if args.archives:
        archives = parse_archive_list(args.archives)
    else:
        archives = infer_archives(unpacked_root)
    if not archives:
        raise SystemExit("no archives selected")

    patch_set = load_texture_patch_set(
        textures_root=textures_root,
        force_reencode=args.force_reencode_textures,
        archives=set(archives),
    )

    if textures_root is not None:
        print(
            f"Texture records selected: {patch_set.selected} "
            f"(skipped unchanged: {patch_set.skipped_unchanged})"
        )

    all_stats: list[RebuildStats] = []
    failed = False
    for archive in archives:
        stats = rebuild_archive(
            archive=archive,
            original_dir=original_dir,
            unpacked_root=unpacked_root,
            baseline_unpacked_root=baseline_unpacked_root,
            out_dir=out_dir,
            patch_set=patch_set,
            textures_root=textures_root,
            reuse_unchanged=not args.no_reuse_unchanged,
            preserve_layout=not args.relayout,
            verify=args.verify,
            write_staged_unpacked=staged_root,
            use_optimal_sd0=args.optimal_sd0,
            use_optimal_cmp0=args.optimal_cmp0,
            jobs=jobs,
            allow_basename_source_records=len(archives) == 1,
        )
        all_stats.append(stats)
        if stats.identical is False:
            failed = True
        print(
            f"ZZZPSP{archive}.MKD: entries={stats.entries} reused={stats.reused} "
            f"recompressed={stats.recompressed} riff={stats.riff} "
            f"texture_records={stats.texture_records} changed_files={stats.changed_files}"
        )

    total_entries = sum(item.entries for item in all_stats)
    total_reused = sum(item.reused for item in all_stats)
    total_recompressed = sum(item.recompressed for item in all_stats)
    total_textures = sum(item.texture_records for item in all_stats)
    print(
        f"Done: archives={len(all_stats)} entries={total_entries} reused={total_reused} "
        f"recompressed={total_recompressed} texture_records={total_textures}"
    )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
