#!/usr/bin/env python3
# usage
# python scripts/dump_static_textures.py --source unpacked_mkd --output output
"""
Statically dump localization-target texture assets from the extracted Gihren PSP resources.

This does not use PPSSPP runtime texture dumping.  It walks the already
unpacked resource tree, parses MRG containers, standalone PSET/PSE resources,
and PTNSET pattern blocks, decodes TX + PL texture/palette pairs, and writes
PNGs for the visually verified text-bearing assets listed in docs/task.md.
Use --all for a full discovery dump with the older heuristic text/ui/graphics
grouping, and --categories to split the verified text dump into review folders.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import struct
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image


MRG_MAGIC = b"MRG\x00"
TX_MAGIC = b"TX\x00\x00"
PL_MAGIC = b"PL\x00\x00"
PSET_MAGIC = b"PSET"
PTNSET_MAGIC = b"PTN\x00SET\x00"
CMP0_MAGIC = b"CMP0"
MP16_MAGIC = b"MP16"
MP20_MAGIC = b"MP20"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
KNOWN_SEGMENT_MAGICS = (MRG_MAGIC, TX_MAGIC, PL_MAGIC, PSET_MAGIC, PTNSET_MAGIC, CMP0_MAGIC, PNG_MAGIC)
TEXT_PATTERN_IDS = {f"p{index:02d}" for index in range(1, 9)}
CLUT4_INTERLEAVED_ORDER = [0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15]
RESOURCE_SUFFIXES = (".mrg", ".pse")
RESOURCE_SCAN_MAGICS = (MRG_MAGIC, TX_MAGIC, PL_MAGIC, PSET_MAGIC, PTNSET_MAGIC, CMP0_MAGIC, PNG_MAGIC)
LAST_DISCOVERY_NEXT_ORDINAL = 1
DATABASE_BLUE_DETAIL_START = 0x487
DATABASE_BLUE_DETAIL_END = 0x6C9
DATABASE_GRAY_DETAIL_PL_VALUES = (
    0x0000,
    0xF39C,
    0xEB5A,
    0xE739,
    0xE318,
    0xDEF7,
    0xD6B5,
    0xD294,
    0xCE73,
    0xC631,
    0xBDEF,
    0xB5AD,
    0xAD6B,
    0xA94A,
    0xA108,
    0x94A5,
)


@dataclass(frozen=True)
class TextTargetRule:
    name: str
    ranges: tuple[tuple[int, int], ...]
    predicate: str = "all"


@dataclass(frozen=True)
class TileMap:
    kind: str
    offset: int
    width_tiles: int
    height_tiles: int
    tile_width: int
    tile_height: int
    atlas_width_tiles: int
    atlas_height_tiles: int
    entries: tuple[int, ...]
    zero_is_blank: bool
    blank_entries: tuple[int, ...] = ()
    blank_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class DialogueLineControl:
    offset: int
    line_count: int
    line_lengths: tuple[int, ...]
    line_info_size: int
    phoneme_length_offset: int
    phoneme_length: int


TEXT_TARGET_RULES = (
    TextTargetRule("UI(20)", (
        (49791, 49880),
        )),
    TextTargetRule("UI(자금자원14)", (
        (49891, 49892),
        )),
    TextTargetRule("UI(17g)", (
        (49914, 49935),
        )),
    TextTargetRule("UI(17/bw/중앙)", (
        (49940, 50003),
    )),
    TextTargetRule("UI(17/w/오)", (
        (50010, 50065),
    )),
    TextTargetRule("UI(17/wy/중앙)", (
        (50067, 50071),
    )),
    TextTargetRule("UI(유닛스테이터스)", (
        (50074, 50074),
    )),
    TextTargetRule("UI(유닛적성)", (
        (50076, 50076),
    )),
    TextTargetRule("UI(14)", (
        (50072, 50072),
        (50075, 50075),
        (50077, 50078),
        (50095,50103)
    )),
    TextTargetRule("UI(15)", (
        (50080, 50092),
        (50111, 50113)
    )),
    TextTargetRule("UI(16/중앙)", (
        (50111, 50113),
    )),
    TextTargetRule("UI(16/좌)", (
        (50115, 50119),
        (50134, 50138),
        (51381, 51381),
    )),
    TextTargetRule("UI(17/좌)", (
        (50121, 50127),
        (50004, 50009),
    )),
    TextTargetRule("UI(외,명)", (
        (50140, 50140),
    )),
    TextTargetRule("UI(승격)", (
        (50141, 50141),
    )),
    TextTargetRule("UI(외,명)", (
        (50142, 50142),
    )),
    TextTargetRule("UI(16/납작)", (
        (51335, 51337),
    )),

       

    TextTargetRule("세력 이름_지도자 이름", ((259, 272),)),
    TextTargetRule("작은폰트", ((542, 542),)),
    TextTargetRule("칭호", ((598, 604),)),
    TextTargetRule("개발이름", ((644, 2257),), "development_name"),
    TextTargetRule("적 연산중", ((52780, 52780),)),
    TextTargetRule("유닛 스테이터스 이름", ((2259, 2792),), "unit_status_name"),
    TextTargetRule("기체 스테이터스 이름", ((2794, 4387),), "machine_status_name"),
    TextTargetRule("메인 타이틀", ((4392, 4392),)),
    TextTargetRule("도감(DATABASE)", ((4408, 6455),)),
    TextTargetRule("부대수,사관수", ((6457, 6457),)),
    TextTargetRule("지역이름", ((6462, 6662),), "area_name"),
    TextTargetRule("지도자 이름", ((6664, 6678),)),
    TextTargetRule("세력선택해주세요", ((6683, 6683),)),
    TextTargetRule("세력선택 문구", ((6716, 6730),)),
    TextTargetRule("세력 이름(22)", ((51345, 51358),)),
    TextTargetRule("세력 이름(16/납작)", ((51359, 51373),)),
    TextTargetRule("세력 이름(23)", ((52785, 52795),)),
    TextTargetRule("전투중표시", ((53156, 53157),)),
    TextTargetRule("대사들", (
        (6747, 11583),
        (12076, 12230),
        (14563, 14574),
    )),
    TextTargetRule("각 세력 오프닝", ((11584, 12075),)),
    TextTargetRule("게임내 메뉴얼", ((13891, 13923),)),
    TextTargetRule("시스템 메시지", ((13924, 14182),)),
    TextTargetRule("진행", ((14575, 14575),)),
    TextTargetRule("엔딩 텍스트", ((14576, 14704),)),
    TextTargetRule("각 세력 오프닝타이틀", ((14922, 14939),)),
    TextTargetRule("메모리카드", ((53327, 53360),)),
    TextTargetRule("특별플랜", ((51383, 51491),)),
    TextTargetRule("인원합류탈퇴", ((13749, 13862),)),
    #TextTargetRule("추가텍스트", (
    #    # (14962, 14963), # 이건 ps2용임
    #    (49803, 49892),
    #    (49903, 49904),
    #    (49926, 49947),
    #    
    #    
    #    (51347, 51349),
    #    (51393, 51393),
    #    (51396, 51398),
    #    )),
    TextTargetRule("예산투입", ((51304, 51306),)),
        
    TextTargetRule("개발설명", (
        (50146, 50189),
        (50192, 50201),
        (50204, 50221),
        (50224, 50225),
        (50228, 50259),
        (50264, 50265),
        (50270, 50271),
        (50274, 50277),
        (50280, 50281),
        (50284, 50285),
        (50288, 50289),
        (50292, 50293),
        (50296, 50297),
        (50300, 50301),
        (50304, 50305),
        (50308, 50311),
        (50318, 50319),
        (50322, 50323),
        (50326, 50327),
        (50330, 50331),
        (50336, 50337),
        (50340, 50343),
        (50352, 50357),
        (50360, 50361),
        (50364, 50381),
        (50386, 50387),
        (50392, 50393),
        (50398, 50409),
        (50412, 50423),
        (50428, 50435),
        (50438, 50449),
        (50456, 50459),
        (50466, 50471),
        (50474, 50479),
        (50486, 50489),
        (50496, 50511),
        (50514, 50519),
        (50522, 50539),
        (50542, 50585),
        (50590, 50595),
        (50600, 50601),
        (50604, 50605),
        (50612, 50637),
        (50640, 50641),
        (50646, 50655),
        (50662, 50691),
        (50694, 50695),
        (50698, 50699),
        (50702, 50703),
        (50706, 50741),
        (50744, 50755),
        (50758, 50759),
        (50762, 50763),
        (50766, 50767),
        (50770, 50771),
        (50774, 50777),
        (50780, 50783),
        (50786, 50791),
        (50794, 50795),
        (50798, 50803),
        (50810, 50813),
        (50820, 50821),
        (50824, 50825),
        (50828, 50829),
        (50832, 50837),
        (50840, 50841),
        (50846, 50847),
        (50850, 50869),
        (50872, 50873),
        (50876, 50877),
        (50884, 50895),
        (50898, 50905),
        (50908, 50909),
        (50912, 50913),
        (50918, 50919),
        (50922, 50925),
        (50928, 50929),
        (50934, 50939),
        (50942, 50971),
        (50974, 50975),
        (51098, 51099),
        (51106, 51109),
        (51112, 51113),
        (51122, 51125),
        (51128, 51129),
        (51132, 51133),
        (51136, 51141),
        (51148, 51151),
        (51154, 51155),
        (51158, 51159),
        (51162, 51163),
        (51166, 51169),
        (51172, 51173),
        (51176, 51177),
        (51180, 51181),
        (51184, 51185),
        (51188, 51189),
        (51192, 51193),
        (51196, 51197),
        (51200, 51201),
        (51204, 51205),
        (51208, 51209),
        (51212, 51213),
        (51216, 51219),
        (51222, 51223),
        (51226, 51229),
        (51232, 51235),
        (51238, 51239),
        (51242, 51243),
        (51246, 51247),
        (51250, 51251),
        (51254, 51255),
        (51258, 51259),
        (51262, 51263),
        (51266, 51267),
        (51270, 51289),
        (51292, 51293),
        (51296, 51297),
        (51300, 51301),
        (51307, 51313),
        )),
)
TILEMAP_AFTER_GROUPS = {"메인 타이틀", "각 세력 오프닝타이틀"}
TILEMAP_BEFORE_GROUPS = {"엔딩 텍스트"}
TILEMAP_GROUPS = TILEMAP_AFTER_GROUPS | TILEMAP_BEFORE_GROUPS


def target_rule_ranges(name: str) -> tuple[tuple[int, int], ...]:
    return tuple(
        range_pair
        for rule in TEXT_TARGET_RULES
        if rule.name == name
        for range_pair in rule.ranges
    )


def triplet_ranges(ranges: tuple[tuple[int, int], ...], *, label: str) -> tuple[tuple[int, int], ...]:
    combined: list[tuple[int, int]] = []
    for start, end in ranges:
        count = end - start + 1
        if count % 3:
            raise ValueError(f"{label} range {start}-{end} is not divisible into 3-part groups")
        combined.extend((ordinal, ordinal + 2) for ordinal in range(start, end + 1, 3))
    return tuple(combined)


ENDING_TEXT_COMBINED_RANGES = triplet_ranges(
    target_rule_ranges("엔딩 텍스트"),
    label="엔딩 텍스트",
)
ENDING_TEXT_COMBINED_RANGE_BY_ORDINAL = {
    ordinal: (start, end)
    for start, end in ENDING_TEXT_COMBINED_RANGES
    for ordinal in range(start, end + 1)
}


@dataclass
class Segment:
    path: str
    offset: int
    data: bytes
    parent: str
    index: int


@dataclass
class TextureRecord:
    source: str
    tree_path: str
    offset: int
    width: int
    height: int
    palette_colors: int
    bpp: int
    category: str
    verified_group: str
    ordinal: int
    pattern: str
    palette_profile: str
    palette_order: str
    palette_offset: int
    storage_width: int
    storage_height: int
    layout: str
    layout_offset: int
    sha1: str
    output: str
    duplicate: bool
    alpha_pixels: int
    opaque_pixels: int
    bbox: str
    output_group: str
    output_group_part: int
    output_group_parts: int
    output_crop_x: int
    output_crop_y: int
    output_crop_width: int
    output_crop_height: int
    output_clear_rects: str
    dialogue_line_control_offset: int
    dialogue_line_count: int
    dialogue_line_lengths: str
    dialogue_speaker_id: str


@dataclass
class TextureDraft:
    source: str
    tree_path: str
    offset: int
    palette_colors: int
    bpp: int
    category: str
    verified_group: str
    ordinal: int
    pattern: str
    palette_order: str
    palette_offset: int
    storage_width: int
    storage_height: int
    layout: str
    layout_offset: int
    dialogue_line_control_offset: int
    dialogue_line_count: int
    dialogue_line_lengths: str
    dialogue_speaker_id: str
    filename_stem: str
    image: Image.Image
    dedupe_key: str


def read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def read_u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def aligned4(value: int) -> int:
    return (value + 3) & ~3


def dialogue_line_info_size(line_count: int) -> int:
    return aligned4(1 + line_count)


def parse_dialogue_line_lengths(value: object) -> tuple[int, ...]:
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
        if number <= 0 or number > 0xFF:
            raise ValueError(f"dialogue line length out of byte range: {number}")
        lengths.append(number)
    return tuple(lengths)


def detect_dialogue_line_control(blob: bytes | bytearray, palette_offset: int) -> DialogueLineControl | None:
    if palette_offset < 0 or palette_offset + 8 > len(blob):
        return None
    if bytes(blob[palette_offset : palette_offset + 4]) != PL_MAGIC:
        return None

    palette_size = read_u32(blob, palette_offset + 4)
    control_offset = palette_offset + palette_size
    if control_offset + 12 > len(blob):
        return None

    entry_count = read_u32(blob, control_offset)
    line_count = blob[control_offset + 4]
    if line_count <= 0 or line_count > 64 or entry_count != line_count + 1:
        return None

    info_size = dialogue_line_info_size(line_count)
    phoneme_length_offset = control_offset + 4 + info_size
    if phoneme_length_offset + 4 > len(blob):
        return None

    lengths_start = control_offset + 5
    lengths_end = lengths_start + line_count
    line_lengths = tuple(int(value) for value in blob[lengths_start:lengths_end])
    if any(length <= 0 for length in line_lengths):
        return None

    padding_start = lengths_end
    padding_end = phoneme_length_offset
    if any(blob[padding_start:padding_end]):
        return None

    phoneme_length = read_u32(blob, phoneme_length_offset)
    phoneme_start = phoneme_length_offset + 4
    phoneme_end = phoneme_start + phoneme_length
    if phoneme_length <= 0 or phoneme_end > len(blob):
        return None
    if phoneme_length < 2 or bytes(blob[phoneme_end - 2 : phoneme_end]) != b"\r\n":
        return None

    aligned_phoneme_end = aligned4(phoneme_end)
    if aligned_phoneme_end > len(blob):
        return None
    if any(blob[phoneme_end:aligned_phoneme_end]):
        return None

    return DialogueLineControl(
        offset=control_offset,
        line_count=line_count,
        line_lengths=line_lengths,
        line_info_size=info_size,
        phoneme_length_offset=phoneme_length_offset,
        phoneme_length=phoneme_length,
    )


def mrg_leading_metadata(blob: bytes, base: int) -> bytes:
    if blob[base : base + 4] != MRG_MAGIC or base + 16 > len(blob):
        return b""

    table_end = read_u32(blob, base + 12)
    children = parse_mrg_children(blob, base)
    if not children:
        return b""

    first_child_offset = min(segment.offset for segment in children)
    metadata_start = base + table_end
    if metadata_start > first_child_offset:
        return b""
    return blob[metadata_start:first_child_offset]


def dialogue_speaker_id_from_metadata(metadata: bytes) -> str:
    if len(metadata) < 2:
        return ""
    speaker_id = read_u16(metadata, 0)
    if speaker_id == 0xFFFF:
        return ""
    return str(speaker_id)


def dialogue_speaker_ids_by_parent_path(blob: bytes) -> dict[str, str]:
    speaker_ids: dict[str, str] = {}
    if not blob.startswith(MRG_MAGIC):
        return speaker_ids

    def visit(base: int, parent_path: str = "") -> None:
        for segment in parse_mrg_children(blob, base, parent_path):
            if not segment.data.startswith(MRG_MAGIC):
                continue
            speaker_ids[segment.path] = dialogue_speaker_id_from_metadata(
                mrg_leading_metadata(blob, segment.offset)
            )
            visit(segment.offset, segment.path)

    speaker_ids["/"] = dialogue_speaker_id_from_metadata(mrg_leading_metadata(blob, 0))
    visit(0)
    return speaker_ids


def safe_slug(value: str) -> str:
    return (
        value.strip("/")
        .replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
        .replace(" ", "_")
        or "root"
    )


def safe_category_name(value: str) -> str:
    return value.strip("/").replace("/", "_").replace("\\", "_").replace(":", "_") or "text"


def trailing_texture_index(value: str | None) -> str:
    if not value:
        return ""
    value = value.replace("\\", "/").rstrip("/")
    if "/" in value and "." not in value.rsplit("/", 1)[-1]:
        return value.rsplit("/", 1)[-1]
    stem = Path(value).stem
    parts = stem.split("_")
    return parts[-1] if parts else ""


def starts_with_known_segment(blob: bytes, offset: int) -> bool:
    return any(blob[offset : offset + len(magic)] == magic for magic in KNOWN_SEGMENT_MAGICS)


def parse_mrg_children(blob: bytes, base: int = 0, parent_path: str = "") -> list[Segment]:
    if blob[base : base + 4] != MRG_MAGIC or base + 16 > len(blob):
        return []

    total = read_u32(blob, base + 4)
    count = read_u32(blob, base + 8)
    table_end = read_u32(blob, base + 12)
    if total < 16 or count < 1 or table_end < 16 or base + total > len(blob):
        return []

    if table_end != 16 + count * 4:
        return []

    table_values = [read_u32(blob, base + 16 + i * 4) for i in range(count)]
    candidate_offsets = {table_end}
    candidate_offsets.update(value for value in table_values if table_end <= value < total)
    table_path_indices = {
        value: index
        for index, value in enumerate(
            value
            for value in table_values
            if table_end < value < total and starts_with_known_segment(blob, base + value)
        )
    }
    offsets = [
        value
        for value in sorted(candidate_offsets)
        if starts_with_known_segment(blob, base + value)
    ]
    if not offsets:
        return []

    segments: list[Segment] = []
    for index, (start, end) in enumerate(zip(offsets, offsets[1:] + [total])):
        if end < start:
            continue
        path_index = table_path_indices.get(start, index)
        path_part = str(path_index) if start in table_path_indices else f"header_{index}"
        tree_path = f"{parent_path}/{path_part}" if parent_path else f"/{path_part}"
        segments.append(
            Segment(
                path=tree_path,
                offset=base + start,
                data=blob[base + start : base + end],
                parent=parent_path or "/",
                index=path_index,
            )
        )
    return segments


def walk_mrg(blob: bytes, base: int = 0, parent_path: str = "") -> Iterable[Segment]:
    for segment in parse_mrg_children(blob, base, parent_path):
        yield segment
        if segment.data.startswith(MRG_MAGIC):
            yield from walk_mrg(blob, segment.offset, segment.path)


def palette_555_to_rgba(value: int, transparent_index: bool) -> tuple[int, int, int, int]:
    # The observed PL palettes are 15-bit little-endian BGR/RGB grayscale values.
    # Palette entry 0 often uses 0x8000 and is transparent for text strips.
    r = (value & 0x1F) * 255 // 31
    g = ((value >> 5) & 0x1F) * 255 // 31
    b = ((value >> 10) & 0x1F) * 255 // 31
    a = 0 if transparent_index else 255
    return (r, g, b, a)


def palette_profile_from_rgba(colors: list[tuple[int, int, int, int]]) -> str:
    visible = [rgba for rgba in colors if rgba[3] > 0 and max(rgba[:3]) > 24]
    if not visible:
        return "black"

    gray = sum(1 for r, g, b, _ in visible if max(r, g, b) - min(r, g, b) <= 5)
    blue = sum(1 for r, g, b, _ in visible if b >= g >= r and b - r >= 16)
    yellow = sum(1 for r, g, b, _ in visible if r >= g >= b and r - b >= 24 and g - b >= 16)

    if gray / len(visible) >= 0.85:
        return "gray"
    if blue / len(visible) >= 0.45:
        return "blue"
    if yellow / len(visible) >= 0.45:
        return "yellow"
    return "color"


def raw_palette_profile(pl_segment: bytes) -> str:
    if not pl_segment.startswith(PL_MAGIC) or len(pl_segment) < 14:
        return "unknown"
    color_count = read_u32(pl_segment, 8)
    if color_count <= 0 or color_count > 256 or len(pl_segment) < 12 + color_count * 2:
        return "unknown"
    raw_values = [read_u16(pl_segment, 12 + i * 2) for i in range(color_count)]
    transparent_zero = bool(raw_values and raw_values[0] == 0x8000)
    return palette_profile_from_rgba(
        [
            palette_555_to_rgba(value, transparent_zero and index == 0)
            for index, value in enumerate(raw_values)
        ]
    )


def make_pl_segment(raw_values: Iterable[int]) -> bytes:
    values = tuple(raw_values)
    payload = bytearray(PL_MAGIC)
    payload.extend((12 + len(values) * 2).to_bytes(4, "little"))
    payload.extend(len(values).to_bytes(4, "little"))
    for value in values:
        payload.extend(int(value).to_bytes(2, "little"))
    return bytes(payload)


def rgba_to_555(r: int, g: int, b: int) -> int:
    red = round(max(0, min(255, r)) * 31 / 255)
    green = round(max(0, min(255, g)) * 31 / 255)
    blue = round(max(0, min(255, b)) * 31 / 255)
    return red | (green << 5) | (blue << 10)


def luma_pl_segment(pl_segment: bytes, palette_order: str = "linear") -> bytes:
    raw_values: list[int] = []
    for index, (r, g, b, a) in enumerate(parse_palette(pl_segment, palette_order=palette_order)):
        if a == 0 and index == 0:
            raw_values.append(0x8000)
            continue
        luma = round(0.299 * r + 0.587 * g + 0.114 * b)
        raw_values.append(rgba_to_555(luma, luma, luma))
    return make_pl_segment(raw_values)


DATABASE_GRAY_DETAIL_PL = make_pl_segment(DATABASE_GRAY_DETAIL_PL_VALUES)


def color_distance(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> int:
    return sum((first[index] - second[index]) ** 2 for index in range(3))


def database_status_detail_pl(pl_segment: bytes) -> bytes:
    luma_palette = parse_palette(luma_pl_segment(pl_segment))
    gray_palette = parse_palette(DATABASE_GRAY_DETAIL_PL)
    gray_values = DATABASE_GRAY_DETAIL_PL_VALUES
    raw_values: list[int] = []
    for color in luma_palette:
        if color[3] == 0:
            raw_values.append(0x8000)
            continue
        nearest_index, _nearest_color = min(
            enumerate(gray_palette),
            key=lambda item: color_distance(color, item[1]),
        )
        raw_values.append(gray_values[nearest_index])
    return make_pl_segment(raw_values)


def source_mrg_index(source: str | Path) -> tuple[int | None, int | None]:
    path = Path(str(source).replace("\\", "/"))
    archive: int | None = None
    for part in path.parts:
        if part.startswith("unpacked_") and part.rsplit("_", 1)[-1].isdigit():
            archive = int(part.rsplit("_", 1)[-1])
    try:
        index = int(path.stem, 16)
    except ValueError:
        index = None
    return archive, index


def is_database_blue_detail_palette(
    rel_source: str | Path,
    tx_segment: Segment,
    pl_segment: Segment,
) -> bool:
    archive, source_index = source_mrg_index(rel_source)
    if source_index is None:
        return False
    if archive is not None and archive != 2:
        return False
    if not (DATABASE_BLUE_DETAIL_START <= source_index <= DATABASE_BLUE_DETAIL_END):
        return False
    if not tx_segment.data.startswith(TX_MAGIC) or len(tx_segment.data) < 12:
        return False
    width = read_u16(tx_segment.data, 8)
    height = read_u16(tx_segment.data, 10)
    if width in {372, 373} and height == 25:
        return False
    suffix = trailing_texture_index(tx_segment.path)
    if suffix not in {"2", "3", "4", "5"}:
        return False
    return raw_palette_profile(pl_segment.data) == "blue"


def database_detail_palette_override(
    rel_source: str | Path,
    tx_segment: Segment,
    pl_segment: Segment,
) -> Segment:
    if not is_database_blue_detail_palette(rel_source, tx_segment, pl_segment):
        return pl_segment
    suffix = trailing_texture_index(tx_segment.path)
    data = database_status_detail_pl(pl_segment.data) if suffix == "2" else DATABASE_GRAY_DETAIL_PL
    return Segment(
        path=f"{pl_segment.path}/database_luma_detail_override",
        offset=pl_segment.offset,
        data=data,
        parent=pl_segment.parent,
        index=pl_segment.index,
    )


def database_detail_rebuild_palette_override(
    rel_source: str | Path,
    tx_segment: Segment,
    pl_segment: Segment,
) -> Segment:
    if not is_database_blue_detail_palette(rel_source, tx_segment, pl_segment):
        return pl_segment
    return Segment(
        path=f"{pl_segment.path}/database_gray_detail_rebuild_override",
        offset=pl_segment.offset,
        data=DATABASE_GRAY_DETAIL_PL,
        parent=pl_segment.parent,
        index=pl_segment.index,
    )


def texture_dedupe_key(rel_source: str | Path, tx_segment: Segment, image_digest_value: str) -> str:
    archive, source_index = source_mrg_index(rel_source)
    if (
        (archive is None or archive == 2)
        and source_index is not None
        and DATABASE_BLUE_DETAIL_START <= source_index <= DATABASE_BLUE_DETAIL_END
        and trailing_texture_index(tx_segment.path) == "3"
    ):
        return f"database_after5115_path3:{image_digest_value}"
    return image_digest_value


def image_palette_profile(image: Image.Image) -> str:
    colors = image.convert("RGBA").getcolors(maxcolors=1_000_000)
    if not colors:
        return "unknown"
    return palette_profile_from_rgba([rgba for _count, rgba in colors])


def parse_palette(pl_segment: bytes, palette_order: str = "linear") -> list[tuple[int, int, int, int]]:
    if not pl_segment.startswith(PL_MAGIC) or len(pl_segment) < 14:
        raise ValueError("not a PL segment")

    color_count = read_u32(pl_segment, 8)
    if color_count <= 0 or color_count > 256:
        raise ValueError(f"unsupported palette color count: {color_count}")
    if len(pl_segment) < 12 + color_count * 2:
        raise ValueError("palette segment is truncated")

    raw_values = [read_u16(pl_segment, 12 + i * 2) for i in range(color_count)]
    transparent_8000 = palette_order == "transparent_8000"
    if palette_order == "clut4_interleaved":
        if color_count != 16:
            raise ValueError("clut4_interleaved palette order requires 16 colors")
        raw_values = [raw_values[index] for index in CLUT4_INTERLEAVED_ORDER]
    elif palette_order not in {"linear", "transparent_8000"}:
        raise ValueError(f"unsupported palette order: {palette_order}")
    transparent_zero = bool(raw_values and raw_values[0] == 0x8000)
    return [
        palette_555_to_rgba(
            value,
            (transparent_zero and index == 0) or (transparent_8000 and value == 0x8000),
        )
        for index, value in enumerate(raw_values)
    ]


def choose_pixel_data(tx_segment: bytes, needed: int) -> bytes:
    # TX records use a 12-byte header:
    #   magic(4), segment_size(4), width(2), height(2)
    # Many records then have 4 trailing alignment bytes.  Starting at 16 skips
    # the first four pixel bytes, which clips eight pixels from 4bpp text.
    if len(tx_segment) - 12 >= needed:
        return tx_segment[12 : 12 + needed]
    if len(tx_segment) - 16 >= needed:
        return tx_segment[16 : 16 + needed]
    raise ValueError("TX segment is shorter than the declared dimensions")


def _stride_bytes(width: int, bpp: int) -> int:
    """Compute the row stride in bytes, rounded up to an even byte boundary.

    PSP TX pixel data pads each row so that the byte count per row is even.
    For 4bpp textures with an odd number of raw bytes per row (e.g. width=373
    -> ceil(373/2)=187 bytes), this adds 1 padding byte per row.  Without
    accounting for this, pixels drift by the padding amount each row,
    producing a characteristic skewed / tilted image.
    """
    raw = math.ceil(width * bpp / 8)
    return (raw + 1) & ~1  # round up to even


def decode_tx_pl_dimensions(
    tx_segment: bytes,
    pl_segment: bytes,
    width: int,
    height: int,
    palette_order: str = "linear",
) -> tuple[Image.Image, int, int]:
    if not tx_segment.startswith(TX_MAGIC) or len(tx_segment) < 12:
        raise ValueError("not a TX segment")

    if width <= 0 or height <= 0:
        raise ValueError(f"invalid texture dimensions: {width}x{height}")

    palette = parse_palette(pl_segment, palette_order=palette_order)
    if len(palette) <= 16:
        bpp = 4
    elif len(palette) <= 256:
        bpp = 8
    else:
        raise ValueError(f"unsupported palette size: {len(palette)}")

    stride = _stride_bytes(width, bpp)
    total_needed = stride * height
    pixel_data = choose_pixel_data(tx_segment, total_needed)

    # Decode each row using the full stride, then keep only 'width' pixels.
    # The stride may be wider than the display width due to byte-alignment
    # padding (e.g. 373 pixels at 4bpp → 187 raw bytes → stride 188 bytes
    # = 376 decoded pixels per row, of which only the first 373 are valid).
    indices: list[int] = []
    for row in range(height):
        row_start = row * stride
        row_bytes = pixel_data[row_start : row_start + stride]
        if bpp == 4:
            row_indices: list[int] = []
            for byte in row_bytes:
                row_indices.append(byte & 0x0F)
                row_indices.append((byte >> 4) & 0x0F)
            indices.extend(row_indices[:width])
        else:
            indices.extend(row_bytes[:width])

    image = Image.new("RGBA", (width, height))
    pixels = [palette[index] if index < len(palette) else (255, 0, 255, 255) for index in indices]
    image.putdata(pixels)
    return image, len(palette), bpp


def decode_tx_pl(
    tx_segment: bytes,
    pl_segment: bytes,
    palette_order: str = "linear",
) -> tuple[Image.Image, int, int]:
    width = read_u16(tx_segment, 8)
    height = read_u16(tx_segment, 10)
    return decode_tx_pl_dimensions(
        tx_segment,
        pl_segment,
        width=width,
        height=height,
        palette_order=palette_order,
    )


def palette_is_grayscale(image: Image.Image) -> bool:
    colors = image.getcolors(maxcolors=257)
    if colors is None:
        return False
    visible = [rgba for _, rgba in colors if rgba[3] > 0]
    if not visible:
        return True
    grayish = sum(1 for r, g, b, _ in visible if max(r, g, b) - min(r, g, b) <= 4)
    return grayish / len(visible) >= 0.85


def infer_pattern(width: int, height: int, tree_path: str, palette_profile: str) -> str:
    if any(prefix in tree_path for prefix in ("/pset/", "/ptnset/", "/loose/")):
        suffix = ""
    else:
        suffix = trailing_texture_index(tree_path)
    if width == 160 and height >= 160:
        return "p01"
    if width == 160 and 112 <= height <= 152:
        return "p02"
    if width == 240 and height >= 96:
        return "p03"
    if width == 373 and height == 25:
        return "p04"
    if height == 16 and suffix == "5" and palette_profile in {"blue", "gray"}:
        return "p05"
    if height == 16 and suffix == "2" and palette_profile == "gray":
        return "p06"
    if width == 372 and height == 25:
        return "p07"
    if width == 240 and height in (32, 48):
        return "p08"
    return "generic"


def palette_order_for_pattern(pattern: str, palette_profile: str) -> str:
    if pattern in {"p02", "p05"} and palette_profile == "blue":
        return "clut4_interleaved"
    return "linear"


def classify_texture(image: Image.Image, palette_colors: int, bpp: int, pattern: str = "generic") -> str:
    if pattern in TEXT_PATTERN_IDS:
        return "text"

    width, height = image.size
    grayscale = palette_is_grayscale(image)
    bbox = image.getbbox()
    visible_area = 0
    if bbox:
        visible_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])

    if grayscale and palette_colors <= 16:
        if width >= 80 and height <= 96:
            return "text"
        if width >= 128 and height >= 96 and visible_area <= width * height * 0.65:
            return "text"
        return "ui"

    if bpp >= 8 or palette_colors > 16:
        return "graphics"

    return "ui"


def image_stats(image: Image.Image) -> tuple[int, int, str]:
    alpha = image.getchannel("A")
    alpha_pixels = sum(1 for value in alpha.getdata() if value == 0)
    opaque_pixels = image.width * image.height - alpha_pixels
    bbox = image.getbbox()
    bbox_text = "" if bbox is None else ",".join(str(value) for value in bbox)
    return alpha_pixels, opaque_pixels, bbox_text


def image_digest(image: Image.Image) -> str:
    return hashlib.sha1(
        image.size[0].to_bytes(2, "little")
        + image.size[1].to_bytes(2, "little")
        + image.tobytes()
    ).hexdigest()


def visible_mask_rows(image: Image.Image) -> list[bytes]:
    rgba = image.convert("RGBA").tobytes()
    width, height = image.size
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray(width)
        row_start = y * width * 4
        for x in range(width):
            offset = row_start + x * 4
            if max(rgba[offset], rgba[offset + 1], rgba[offset + 2]) > 40:
                row[x] = 1
        rows.append(bytes(row))
    return rows


def visible_row_clusters(image: Image.Image) -> list[tuple[int, int, int, int]]:
    rows = visible_mask_rows(image)
    clusters: list[tuple[int, int, int, int]] = []
    start: int | None = None
    pixel_count = 0
    for y, row in enumerate(rows):
        row_pixels = row.count(1)
        if row_pixels and start is None:
            start = y
            pixel_count = 0
        if start is not None:
            pixel_count += row_pixels
        if start is not None and (not row_pixels or y == len(rows) - 1):
            end = y if not row_pixels else y + 1
            clusters.append((start, end, end - start, pixel_count))
            start = None
            pixel_count = 0
    return clusters


def visible_overlap_offset(
    previous: Image.Image,
    current: Image.Image,
    max_overlap: int = 128,
    min_visible_pixels: int = 300,
) -> int:
    previous_rows = visible_mask_rows(previous)
    current_rows = visible_mask_rows(current)
    previous_height = previous.height
    best_offset = previous_height
    best_visible = 0
    for offset in range(max(0, previous_height - max_overlap), previous_height):
        overlap = previous_height - offset
        if overlap > current.height:
            continue
        previous_overlap = previous_rows[offset:]
        current_overlap = current_rows[:overlap]
        if previous_overlap != current_overlap:
            continue
        visible = sum(row.count(1) for row in previous_overlap)
        if visible >= min_visible_pixels and visible > best_visible:
            best_offset = offset
            best_visible = visible
    return best_offset


def continuation_stitch_offset(
    previous: Image.Image,
    current: Image.Image,
    edge_margin: int = 64,
    partial_height: int = 16,
    top_margin: int = 4,
    max_overlap: int = 48,
) -> int | None:
    previous_clusters = visible_row_clusters(previous)
    current_clusters = visible_row_clusters(current)
    if not previous_clusters or not current_clusters:
        return None

    _prev_start, prev_end, prev_height, _prev_pixels = previous_clusters[-1]
    curr_start, _curr_end, curr_height, _curr_pixels = current_clusters[0]
    if previous.height - prev_end > edge_margin or curr_start > top_margin:
        return None
    if prev_height > partial_height and curr_height > 6:
        return None
    if curr_height > partial_height and prev_height > 6:
        return None

    offset = prev_end - curr_start
    if offset <= 0 or offset >= previous.height:
        return None
    if previous.height - offset > max_overlap:
        return None
    return offset


def ending_text_stitch_offset(previous: Image.Image, current: Image.Image) -> tuple[int, bool]:
    exact_offset = visible_overlap_offset(previous, current)
    if exact_offset < previous.height:
        return exact_offset, False

    continuation_offset = continuation_stitch_offset(previous, current)
    if continuation_offset is not None:
        return continuation_offset, True

    return previous.height, False


def bright_edge_padding(image: Image.Image, padding: int = 8) -> tuple[int, int, int, int]:
    rgba = image.convert("RGBA").tobytes()
    width, height = image.size

    def is_bright(x: int, y: int) -> bool:
        offset = (y * width + x) * 4
        return max(rgba[offset], rgba[offset + 1], rgba[offset + 2]) > 40

    top = any(is_bright(x, 0) for x in range(width))
    bottom = any(is_bright(x, height - 1) for x in range(width))
    left = any(is_bright(0, y) for y in range(height))
    right = any(is_bright(width - 1, y) for y in range(height))
    return (
        padding if left else 0,
        padding if top else 0,
        padding if right else 0,
        padding if bottom else 0,
    )


def add_padding(
    image: Image.Image,
    padding: tuple[int, int, int, int],
) -> Image.Image:
    left, top, right, bottom = padding
    if not any(padding):
        return image
    output = Image.new("RGBA", (image.width + left + right, image.height + top + bottom))
    output.alpha_composite(image, (left, top))
    return output


def predicate_matches(
    predicate: str,
    tree_path: str,
    width: int,
    height: int,
) -> bool:
    if predicate == "all":
        return True
    if predicate == "development_name":
        return tree_path == "/2/0" and (width, height) == (306, 27)
    if predicate == "unit_status_name":
        return tree_path == "/2" and (width, height) == (174, 19)
    if predicate == "machine_status_name":
        return tree_path == "/2" and (width, height) == (151, 35)
    if predicate == "area_name":
        return tree_path == "/header_0/0" and (width, height) == (250, 25)
    raise AssertionError(f"unknown text target predicate: {predicate}")


def ordinal_in_ranges(ordinal: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= ordinal <= end for start, end in ranges)


def verified_text_group(
    ordinal: int,
    tree_path: str,
    width: int,
    height: int,
) -> str | None:
    for rule in TEXT_TARGET_RULES:
        if ordinal_in_ranges(ordinal, rule.ranges) and predicate_matches(
            rule.predicate,
            tree_path,
            width,
            height,
        ):
            return rule.name
    return None


def valid_tilemap_at(blob: bytes, offset: int, magic: bytes) -> TileMap | None:
    if offset < 0 or offset + 12 > len(blob) or blob[offset : offset + 4] != magic:
        return None
    size = read_u32(blob, offset + 4)
    if size < 12 or offset + size > len(blob):
        return None

    if magic == MP16_MAGIC:
        width_tiles = read_u16(blob, offset + 8)
        height_tiles = read_u16(blob, offset + 10)
        tile_width = tile_height = 16
        entries_offset = offset + 12
        expected_size = 12 + width_tiles * height_tiles * 2
        atlas_width_tiles = 0
        atlas_height_tiles = 0
    elif magic == MP20_MAGIC:
        if size < 20:
            return None
        width_tiles = read_u16(blob, offset + 8)
        height_tiles = read_u16(blob, offset + 10)
        atlas_width_tiles = read_u16(blob, offset + 12)
        atlas_height_tiles = read_u16(blob, offset + 14)
        tile_width = read_u16(blob, offset + 16)
        tile_height = read_u16(blob, offset + 18)
        entries_offset = offset + 20
        expected_size = 20 + width_tiles * height_tiles * 2
    else:
        return None

    if (
        width_tiles <= 0
        or height_tiles <= 0
        or tile_width <= 0
        or tile_height <= 0
        or expected_size > size
    ):
        return None

    entries = tuple(
        read_u16(blob, entries_offset + index * 2)
        for index in range(width_tiles * height_tiles)
    )
    blank_indices = infer_tilemap_blank_indices(entries, width_tiles, height_tiles)
    blank_entries = tuple(sorted({entries[index] for index in blank_indices}))
    zero_is_blank = 0 in blank_entries
    return TileMap(
        kind=magic.decode("ascii"),
        offset=offset,
        width_tiles=width_tiles,
        height_tiles=height_tiles,
        tile_width=tile_width,
        tile_height=tile_height,
        atlas_width_tiles=atlas_width_tiles,
        atlas_height_tiles=atlas_height_tiles,
        entries=entries,
        zero_is_blank=zero_is_blank,
        blank_entries=blank_entries,
        blank_indices=blank_indices,
    )


def infer_tilemap_blank_indices(
    entries: tuple[int, ...],
    width_tiles: int,
    height_tiles: int,
) -> tuple[int, ...]:
    scores: Counter[int] = Counter()
    if entries.count(0) > 1:
        scores[0] += entries.count(0) * 4

    for row_index in range(height_tiles):
        row = entries[row_index * width_tiles : (row_index + 1) * width_tiles]
        if len(set(row)) == 1:
            scores[row[0]] += width_tiles * 3

        trailing_value = row[-1]
        trailing_count = 0
        for entry in reversed(row):
            if entry != trailing_value:
                break
            trailing_count += 1
        if trailing_count >= 2:
            scores[trailing_value] += trailing_count * 2

        run_value: int | None = None
        run_count = 0
        for entry in row + (None,):
            if entry == run_value:
                run_count += 1
                continue
            if run_value is not None and run_count >= 4:
                scores[run_value] += run_count
            run_value = entry
            run_count = 1

    if not scores:
        return ()

    entry, score = scores.most_common(1)[0]
    if score < max(4, width_tiles):
        return ()
    if entry == 0:
        return tuple(index for index, value in enumerate(entries) if value == entry)

    first_index = entries.index(entry)
    return tuple(
        index
        for index, value in enumerate(entries)
        if value == entry and index != first_index
    )


def tilemap_entry_is_blank(tilemap: TileMap, entry_index: int, entry: int) -> bool:
    return entry_index in tilemap.blank_indices or (
        tilemap.zero_is_blank and entry == 0 and not tilemap.blank_indices
    )


def tilemap_after_tx(blob: bytes, tx_segment: Segment) -> TileMap | None:
    if len(tx_segment.data) < 8:
        return None
    tx_size = read_u32(tx_segment.data, 4)
    tx_end = tx_segment.offset + tx_size
    for offset in range(tx_end, min(len(blob), tx_end + 16)):
        for magic in (MP16_MAGIC, MP20_MAGIC):
            tilemap = valid_tilemap_at(blob, offset, magic)
            if tilemap is not None:
                return tilemap
    return None


def tilemap_before_tx(blob: bytes, tx_segment: Segment) -> TileMap | None:
    best: TileMap | None = None
    for offset in range(max(0, tx_segment.offset - 2048), tx_segment.offset):
        for magic in (MP16_MAGIC, MP20_MAGIC):
            tilemap = valid_tilemap_at(blob, offset, magic)
            if tilemap is None:
                continue
            end = offset + read_u32(blob, offset + 4)
            if end <= tx_segment.offset and tx_segment.offset - end <= 16:
                best = tilemap
    return best


def tilemap_near_tx(blob: bytes, tx_segment: Segment) -> TileMap | None:
    return tilemap_after_tx(blob, tx_segment) or tilemap_before_tx(blob, tx_segment)


def apply_tilemap(image: Image.Image, tilemap: TileMap) -> Image.Image:
    atlas_width_tiles = tilemap.atlas_width_tiles or image.width // tilemap.tile_width
    atlas_height_tiles = tilemap.atlas_height_tiles or image.height // tilemap.tile_height
    if atlas_width_tiles <= 0 or atlas_height_tiles <= 0:
        raise ValueError("invalid tilemap atlas dimensions")

    output = Image.new(
        "RGBA",
        (
            tilemap.width_tiles * tilemap.tile_width,
            tilemap.height_tiles * tilemap.tile_height,
        ),
    )
    for entry_index, entry in enumerate(tilemap.entries):
        if tilemap_entry_is_blank(tilemap, entry_index, entry):
            continue
        source_tile = entry
        source_x = (source_tile % atlas_width_tiles) * tilemap.tile_width
        source_y = (source_tile // atlas_width_tiles) * tilemap.tile_height
        if source_x + tilemap.tile_width > image.width or source_y + tilemap.tile_height > image.height:
            continue
        dest_x = (entry_index % tilemap.width_tiles) * tilemap.tile_width
        dest_y = (entry_index // tilemap.width_tiles) * tilemap.tile_height
        output.alpha_composite(
            image.crop(
                (
                    source_x,
                    source_y,
                    source_x + tilemap.tile_width,
                    source_y + tilemap.tile_height,
                )
            ),
            (dest_x, dest_y),
        )
    return output


def apply_verified_layout(
    group: str,
    raw_image: Image.Image,
    tx_segment: Segment,
    pl_segment: Segment,
    palette_order: str,
    blob: bytes,
) -> tuple[Image.Image, str, int]:
    if group in TILEMAP_GROUPS:
        if group in TILEMAP_BEFORE_GROUPS:
            tilemap = tilemap_before_tx(blob, tx_segment)
        elif group in TILEMAP_AFTER_GROUPS:
            tilemap = tilemap_after_tx(blob, tx_segment)
        else:
            tilemap = tilemap_near_tx(blob, tx_segment)
        if tilemap is not None:
            return apply_tilemap(raw_image, tilemap), f"tilemap_{tilemap.kind.lower()}", tilemap.offset

    return raw_image, "linear", 0


def find_palette_for_tx(children: list[Segment], tx_index: int) -> Segment | None:
    """Return the sibling palette that applies to a TX segment.

    Some MRG groups store textures as TX, PL, TX, TX..., where the first PL is
    shared by every following TX in that group.  Older versions only searched
    forward from each TX, so those shared-palette textures were skipped.
    """
    tx_segment = children[tx_index]
    width = read_u16(tx_segment.data, 8) if len(tx_segment.data) >= 12 else 0
    height = read_u16(tx_segment.data, 10) if len(tx_segment.data) >= 12 else 0

    previous_palette = next(
        (
            candidate
            for candidate in reversed(children[:tx_index])
            if candidate.data.startswith(PL_MAGIC)
        ),
        None,
    )
    next_palette = next(
        (
            candidate
            for candidate in children[tx_index + 1 :]
            if candidate.data.startswith(PL_MAGIC)
        ),
        None,
    )

    # The first title/name strip in these resource MRGs has two possible PLs:
    # a leading yellow PL at table_end and a later shared blue/white PL.  Both
    # 372x25 and 373x25 DATABASE name strips use the leading yellow PL; the
    # later PL applies to the following smaller status/detail strips.
    if width in {372, 373} and height == 25 and previous_palette is not None:
        return previous_palette
    if width == 373 and height == 25 and next_palette is not None:
        return next_palette

    if previous_palette is not None:
        return previous_palette

    return next_palette


def valid_tx_at(blob: bytes, offset: int, limit: int | None = None) -> bool:
    limit = len(blob) if limit is None else limit
    if offset + 12 > limit or not blob[offset : offset + 4] == TX_MAGIC:
        return False
    segment_size = read_u32(blob, offset + 4)
    width = read_u16(blob, offset + 8)
    height = read_u16(blob, offset + 10)
    if segment_size < 12 or offset + segment_size > limit:
        return False
    if not (8 <= width <= 1024 and 5 <= height <= 1024):
        return False
    return segment_size >= 12 + _stride_bytes(width, 4) * height


def valid_cmp0_tx_at(blob: bytes, offset: int, limit: int | None = None) -> bool:
    limit = len(blob) if limit is None else limit
    if offset + 12 > limit or not blob[offset : offset + 4] == CMP0_MAGIC:
        return False
    stored_size = read_u32(blob, offset + 4)
    unpacked_size = read_u32(blob, offset + 8)
    if stored_size < 12 or unpacked_size < 12 or offset + stored_size > limit:
        return False

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
            return False
        literal_count = blob[cursor]
        cursor += 1
        if cursor + literal_count > end:
            return False
        output.extend(blob[cursor : cursor + literal_count])
        cursor += literal_count

    if len(output) != unpacked_size or not output.startswith(TX_MAGIC):
        return False
    tx_size = read_u32(output, 4)
    width = read_u16(output, 8)
    height = read_u16(output, 10)
    return tx_size == unpacked_size and 1 <= width <= 1024 and 1 <= height <= 1024


def offset_in_ranges(offset: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in ranges)


def valid_pl_at(blob: bytes, offset: int, limit: int | None = None) -> bool:
    limit = len(blob) if limit is None else limit
    if offset + 12 > limit or not blob[offset : offset + 4] == PL_MAGIC:
        return False
    segment_size = read_u32(blob, offset + 4)
    color_count = read_u32(blob, offset + 8)
    if color_count <= 0 or color_count > 256:
        return False
    minimum_size = 12 + color_count * 2
    return minimum_size <= segment_size and offset + segment_size <= limit


def declared_segment(blob: bytes, offset: int, path: str, parent: str, index: int) -> Segment:
    segment_size = read_u32(blob, offset + 4) if offset + 8 <= len(blob) else 0
    end = offset + segment_size if segment_size >= 8 and offset + segment_size <= len(blob) else len(blob)
    return Segment(path=path, offset=offset, data=blob[offset:end], parent=parent, index=index)


def find_magic_offsets(blob: bytes, magic: bytes, start: int = 0, end: int | None = None) -> list[int]:
    end = len(blob) if end is None else end
    offsets: list[int] = []
    cursor = start
    while True:
        offset = blob.find(magic, cursor, end)
        if offset < 0:
            return offsets
        offsets.append(offset)
        cursor = offset + 1


def pset_ranges(blob: bytes) -> Iterable[tuple[int, int]]:
    for offset in find_magic_offsets(blob, PSET_MAGIC):
        if offset + 12 > len(blob):
            continue
        total = read_u32(blob, offset + 8)
        if total >= 12 and offset + total <= len(blob):
            yield offset, offset + total


def ptnset_ranges(blob: bytes) -> Iterable[tuple[int, int]]:
    for offset in find_magic_offsets(blob, PTNSET_MAGIC):
        if offset + 12 > len(blob):
            continue
        total = read_u32(blob, offset + 8)
        if total >= 12 and offset + total <= len(blob):
            yield offset, offset + total


def choose_scanned_palette(resource_index: int, resource_offsets: list[int], pl_offsets: list[int]) -> int | None:
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


def iter_range_scanned_tx_pl_pairs(
    blob: bytes,
    used_tx_offsets: set[int],
    ranges: Iterable[tuple[int, int]],
    path_prefix: str,
) -> Iterable[tuple[Segment, Segment]]:
    for range_index, (start, end) in enumerate(ranges):
        cmp0_offsets = [
            offset
            for offset in find_magic_offsets(blob, CMP0_MAGIC, start, end)
            if valid_cmp0_tx_at(blob, offset, end)
        ]
        cmp0_ranges = [(offset, offset + read_u32(blob, offset + 4)) for offset in cmp0_offsets]
        tx_offsets = [
            offset
            for offset in find_magic_offsets(blob, TX_MAGIC, start, end)
            if (
                offset not in used_tx_offsets
                and not offset_in_ranges(offset, cmp0_ranges)
                and valid_tx_at(blob, offset, end)
            )
        ]
        resource_offsets = sorted(
            tx_offsets + cmp0_offsets
        )
        resource_index_by_offset = {offset: index for index, offset in enumerate(resource_offsets)}
        pl_offsets = [
            offset
            for offset in find_magic_offsets(blob, PL_MAGIC, start, end)
            if valid_pl_at(blob, offset, end)
        ]
        if not tx_offsets or not pl_offsets:
            continue

        for tx_index, tx_offset in enumerate(tx_offsets):
            pl_offset = choose_scanned_palette(resource_index_by_offset[tx_offset], resource_offsets, pl_offsets)
            if pl_offset is None:
                continue
            tx_path = f"/{path_prefix}/{range_index}/{tx_index}"
            pl_path = f"/{path_prefix}/{range_index}/pl/{tx_index}"
            parent = f"/{path_prefix}/{range_index}"
            yield (
                declared_segment(blob, tx_offset, tx_path, parent, tx_index),
                declared_segment(blob, pl_offset, pl_path, parent, tx_index),
            )
            used_tx_offsets.add(tx_offset)


def iter_loose_tx_pl_pairs(blob: bytes, used_tx_offsets: set[int]) -> Iterable[tuple[Segment, Segment]]:
    tx_offsets = [
        offset
        for offset in find_magic_offsets(blob, TX_MAGIC)
        if offset not in used_tx_offsets and valid_tx_at(blob, offset)
    ]
    pl_offsets = [
        offset
        for offset in find_magic_offsets(blob, PL_MAGIC)
        if valid_pl_at(blob, offset)
    ]
    if not tx_offsets or not pl_offsets:
        return

    for tx_index, tx_offset in enumerate(tx_offsets):
        pl_offset = choose_scanned_palette(tx_index, tx_offsets, pl_offsets)
        if pl_offset is None:
            continue
        tx_path = f"/loose/{tx_index}"
        pl_path = f"/loose/pl/{tx_index}"
        yield (
            declared_segment(blob, tx_offset, tx_path, "/loose", tx_index),
            declared_segment(blob, pl_offset, pl_path, "/loose", tx_index),
        )
        used_tx_offsets.add(tx_offset)


def iter_tx_pl_pairs(blob: bytes, include_loose: bool = False) -> Iterable[tuple[Segment, Segment]]:
    used_tx_offsets: set[int] = set()

    def visit(base: int, parent_path: str = "") -> Iterable[tuple[Segment, Segment]]:
        children = parse_mrg_children(blob, base, parent_path)
        for index, segment in enumerate(children):
            if segment.data.startswith(TX_MAGIC):
                palette = find_palette_for_tx(children, index)
                if palette is not None:
                    used_tx_offsets.add(segment.offset)
                    yield segment, palette
            if segment.data.startswith(MRG_MAGIC):
                yield from visit(segment.offset, segment.path)

    if blob.startswith(MRG_MAGIC):
        yield from visit(0)
    yield from iter_range_scanned_tx_pl_pairs(blob, used_tx_offsets, pset_ranges(blob), "pset")
    yield from iter_range_scanned_tx_pl_pairs(blob, used_tx_offsets, ptnset_ranges(blob), "ptnset")
    if include_loose:
        yield from iter_loose_tx_pl_pairs(blob, used_tx_offsets)


def known_good_palette_override(
    blob: bytes,
    rel_source: Path,
    tx_segment: Segment,
    pl_segment: Segment,
) -> Segment:
    source = str(rel_source).replace("\\", "/")
    if (
        source.endswith("unpacked_2/00000719.mrg")
        or source == "00000719.mrg"
    ) and tx_segment.path == "/pset/0/19":
        # This is the lower "勢力を選択してください" half of the FORCE SELECT title.
        # The adjacent TX at /pset/0/18 and this TX share the gray title PL; the
        # index-matched PL collapses the glyph body to black and is not suitable
        # for translation round-trip.
        offset = 0x80D4
        if (
            offset + 12 <= len(blob)
            and blob[offset : offset + 4] == PL_MAGIC
            and valid_pl_at(blob, offset, len(blob))
        ):
            return declared_segment(blob, offset, "/pset/0/pl/18", "/pset/0", 18)
    return database_detail_palette_override(rel_source, tx_segment, pl_segment)


def verified_palette_override(
    blob: bytes,
    rel_source: Path,
    verified_group: str,
    tx_segment: Segment,
    pl_segment: Segment,
) -> Segment:
    archive, source_index = source_mrg_index(rel_source)
    if verified_group == "게임내 메뉴얼" and archive == 8 and source_index is not None:
        if 0x69F <= source_index <= 0x6AE and tx_segment.data.startswith(TX_MAGIC):
            width = read_u16(tx_segment.data, 8)
            height = read_u16(tx_segment.data, 10)
            if (width, height) == (480, 272):
                tx_offsets: list[int] = []
                offset = 0
                while True:
                    offset = blob.find(TX_MAGIC, offset)
                    if offset < 0:
                        break
                    if valid_tx_at(blob, offset, len(blob)):
                        candidate = declared_segment(blob, offset, "/manual/tx", "/", 0)
                        if len(candidate.data) >= 12 and (
                            read_u16(candidate.data, 8),
                            read_u16(candidate.data, 10),
                        ) == (480, 272):
                            tx_offsets.append(offset)
                    offset += 4

                pl_offsets: list[int] = []
                offset = 0
                while True:
                    offset = blob.find(PL_MAGIC, offset)
                    if offset < 0:
                        break
                    if valid_pl_at(blob, offset, len(blob)):
                        pl_offsets.append(offset)
                    offset += 4

                try:
                    tx_order = tx_offsets.index(tx_segment.offset)
                except ValueError:
                    return pl_segment
                if tx_order < len(pl_offsets):
                    palette_offset = pl_offsets[tx_order]
                    return declared_segment(
                        blob,
                        palette_offset,
                        f"/manual/pl/{tx_order}",
                        "/",
                        tx_order,
                    )
    return pl_segment


def contains_resource_magic(path: Path) -> bool:
    blob = path.read_bytes()
    return any(magic in blob for magic in RESOURCE_SCAN_MAGICS)


def discover_resource_files(source_root: Path, scan_all_files: bool) -> list[Path]:
    if source_root.is_file():
        if source_root.suffix.lower() in RESOURCE_SUFFIXES:
            return [source_root]
        if scan_all_files and contains_resource_magic(source_root):
            return [source_root]
        return []

    resource_files = sorted(
        path
        for suffix in RESOURCE_SUFFIXES
        for path in source_root.rglob(f"*{suffix}")
    )
    if not scan_all_files:
        return resource_files

    seen = set(resource_files)
    extra_files = [
        path
        for path in sorted(source_root.rglob("*"))
        if path.is_file() and path not in seen and contains_resource_magic(path)
    ]
    return resource_files + extra_files


def dump_mrg_textures(
    source_root: Path,
    out_root: Path,
    max_files: int | None,
    dedupe: bool,
    dump_all: bool,
    categorized_text: bool,
    scan_all_files: bool,
    include_loose: bool,
) -> list[TextureRecord]:
    global LAST_DISCOVERY_NEXT_ORDINAL
    LAST_DISCOVERY_NEXT_ORDINAL = 1
    records: list[TextureRecord] = []
    full_seen: dict[str, int] = {}
    selected_seen: dict[str, str] = {}
    pending_combined: dict[int, list[TextureDraft]] = {}
    output_index = 1

    def write_output(
        category: str,
        ordinal: int,
        image: Image.Image,
        filename_stem: str,
        dedupe_key: str,
    ) -> tuple[str, str, bool]:
        digest = image_digest(image)
        duplicate = dedupe and dedupe_key in selected_seen
        if duplicate:
            return selected_seen[dedupe_key], digest, True

        filename = (
            f"{ordinal:06d}-{digest[:12]}_{image.width}x{image.height}_"
            f"{filename_stem}.png"
        )
        output_rel = str(Path(category) / filename)
        output_path = out_root / output_rel
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path)
        selected_seen[dedupe_key] = output_rel
        return output_rel, digest, False

    def append_record(
        draft: TextureDraft,
        output_image: Image.Image,
        output_rel: str,
        digest: str,
        duplicate: bool,
        output_group: str,
        output_group_part: int,
        output_group_parts: int,
        crop: tuple[int, int, int, int],
        clear_rects: list[tuple[int, int, int, int]] | None = None,
    ) -> None:
        profile = image_palette_profile(output_image)
        alpha_pixels, opaque_pixels, bbox = image_stats(output_image)
        crop_x, crop_y, crop_width, crop_height = crop
        records.append(
            TextureRecord(
                source=draft.source,
                tree_path=draft.tree_path,
                offset=draft.offset,
                width=output_image.width,
                height=output_image.height,
                palette_colors=draft.palette_colors,
                bpp=draft.bpp,
                category=draft.category,
                verified_group=draft.verified_group,
                ordinal=draft.ordinal,
                pattern=draft.pattern,
                palette_profile=profile,
                palette_order=draft.palette_order,
                palette_offset=draft.palette_offset,
                storage_width=draft.storage_width,
                storage_height=draft.storage_height,
                layout=draft.layout,
                layout_offset=draft.layout_offset,
                sha1=digest,
                output=output_rel,
                duplicate=duplicate,
                alpha_pixels=alpha_pixels,
                opaque_pixels=opaque_pixels,
                bbox=bbox,
                output_group=output_group,
                output_group_part=output_group_part,
                output_group_parts=output_group_parts,
                output_crop_x=crop_x,
                output_crop_y=crop_y,
                output_crop_width=crop_width,
                output_crop_height=crop_height,
                output_clear_rects=json.dumps(clear_rects or [], separators=(",", ":")),
                dialogue_line_control_offset=draft.dialogue_line_control_offset,
                dialogue_line_count=draft.dialogue_line_count,
                dialogue_line_lengths=draft.dialogue_line_lengths,
                dialogue_speaker_id=draft.dialogue_speaker_id,
            )
        )

    def emit_single(draft: TextureDraft) -> None:
        output_rel, digest, duplicate = write_output(
            draft.category,
            draft.ordinal,
            draft.image,
            draft.filename_stem,
            draft.dedupe_key,
        )
        append_record(
            draft=draft,
            output_image=draft.image,
            output_rel=output_rel,
            digest=digest,
            duplicate=duplicate,
            output_group="",
            output_group_part=0,
            output_group_parts=1,
            crop=(0, 0, draft.image.width, draft.image.height),
            clear_rects=[],
        )

    def emit_combined(start_ordinal: int, drafts: list[TextureDraft]) -> None:
        expected = [start_ordinal + index for index in range(3)]
        if len(drafts) != 3 or [draft.ordinal for draft in drafts] != expected:
            for draft in drafts:
                emit_single(draft)
            return

        placements = [0]
        clear_rects: list[list[tuple[int, int, int, int]]] = [[], [], []]
        for previous, current in zip(drafts, drafts[1:]):
            offset, clear_previous_overlap = ending_text_stitch_offset(
                previous.image,
                current.image,
            )
            if clear_previous_overlap and offset < previous.image.height:
                clear_rects[len(placements) - 1].append(
                    (0, offset, previous.image.width, previous.image.height - offset)
                )
            placements.append(placements[-1] + offset)

        width = max(draft.image.width for draft in drafts)
        height = max(y + draft.image.height for y, draft in zip(placements, drafts))
        output_image = Image.new("RGBA", (width, height))
        crops: list[tuple[int, int, int, int]] = []
        for cursor_y, draft in zip(placements, drafts):
            output_image.alpha_composite(draft.image, (0, cursor_y))
            crops.append((0, cursor_y, draft.image.width, draft.image.height))
        pad_left, pad_top, _pad_right, _pad_bottom = bright_edge_padding(output_image)
        output_image = add_padding(
            output_image,
            (pad_left, pad_top, _pad_right, _pad_bottom),
        )
        if pad_left or pad_top:
            crops = [
                (x + pad_left, y + pad_top, width, height)
                for x, y, width, height in crops
            ]

        group_id = f"ending_text_{start_ordinal:06d}_{start_ordinal + 2:06d}"
        filename_stem = f"{drafts[0].filename_stem}_to_{start_ordinal + 2:06d}"
        output_rel, digest, already_duplicate = write_output(
            drafts[0].category,
            start_ordinal,
            output_image,
            filename_stem,
            image_digest(output_image),
        )
        for index, (draft, crop) in enumerate(zip(drafts, crops), start=1):
            append_record(
                draft=draft,
                output_image=output_image,
                output_rel=output_rel,
                digest=digest,
                duplicate=already_duplicate or index > 1,
                output_group=group_id,
                output_group_part=index,
                output_group_parts=3,
                crop=crop,
                clear_rects=clear_rects[index - 1],
            )

    def flush_pending_before(ordinal: int) -> None:
        for start_ordinal in sorted(list(pending_combined)):
            if start_ordinal + 2 < ordinal:
                emit_combined(start_ordinal, pending_combined.pop(start_ordinal))

    def maybe_emit(draft: TextureDraft) -> None:
        flush_pending_before(draft.ordinal)
        combined_range = (
            ENDING_TEXT_COMBINED_RANGE_BY_ORDINAL.get(draft.ordinal)
            if draft.verified_group == "엔딩 텍스트"
            else None
        )
        if combined_range is None:
            emit_single(draft)
            return

        start_ordinal, _end_ordinal = combined_range
        pending = pending_combined.setdefault(start_ordinal, [])
        pending.append(draft)
        if len(pending) == 3:
            emit_combined(start_ordinal, pending_combined.pop(start_ordinal))

    resource_files = discover_resource_files(source_root, scan_all_files=scan_all_files)
    if max_files is not None:
        resource_files = resource_files[:max_files]

    rel_base = source_root.parent if source_root.is_file() else source_root
    for resource_path in resource_files:
        blob = resource_path.read_bytes()
        speaker_ids = dialogue_speaker_ids_by_parent_path(blob)
        rel_source = resource_path.relative_to(rel_base)
        for tx_segment, pl_segment in iter_tx_pl_pairs(blob, include_loose=include_loose):
            pl_segment = known_good_palette_override(blob, rel_source, tx_segment, pl_segment)
            raw_profile = raw_palette_profile(pl_segment.data)
            width = read_u16(tx_segment.data, 8) if len(tx_segment.data) >= 12 else 0
            height = read_u16(tx_segment.data, 10) if len(tx_segment.data) >= 12 else 0
            pattern = infer_pattern(width, height, tx_segment.path, raw_profile)
            palette_order = palette_order_for_pattern(pattern, raw_profile)
            try:
                image, palette_colors, bpp = decode_tx_pl(
                    tx_segment.data,
                    pl_segment.data,
                    palette_order=palette_order,
                )
            except ValueError:
                continue

            full_digest = image_digest(image)
            full_dedupe_key = texture_dedupe_key(rel_source, tx_segment, full_digest)
            selected_dedupe_key = full_dedupe_key
            if dedupe and full_dedupe_key in full_seen:
                ordinal = full_seen[full_dedupe_key]
            else:
                ordinal = output_index
                if dedupe:
                    full_seen[full_dedupe_key] = ordinal
                output_index += 1

            raw_category = classify_texture(image, palette_colors, bpp, pattern=pattern)
            verified_group = ""
            layout = "linear"
            layout_offset = 0
            category = raw_category
            output_image = image
            line_control = detect_dialogue_line_control(blob, pl_segment.offset)
            dialogue_line_control_offset = line_control.offset if line_control else 0
            dialogue_line_count = line_control.line_count if line_control else 0
            dialogue_line_lengths = (
                ",".join(str(length) for length in line_control.line_lengths)
                if line_control
                else ""
            )
            dialogue_speaker_id = (
                speaker_ids.get(tx_segment.parent, "") if line_control else ""
            )

            if dump_all:
                pass
            else:
                group = verified_text_group(ordinal, tx_segment.path, width, height)
                if group is None:
                    continue
                verified_group = group
                category = safe_category_name(group) if categorized_text else "text"
                selected_pl_segment = verified_palette_override(
                    blob,
                    rel_source,
                    verified_group,
                    tx_segment,
                    pl_segment,
                )
                if selected_pl_segment.offset != pl_segment.offset or selected_pl_segment.data != pl_segment.data:
                    selected_raw_profile = raw_palette_profile(selected_pl_segment.data)
                    selected_pattern = infer_pattern(
                        width,
                        height,
                        tx_segment.path,
                        selected_raw_profile,
                    )
                    selected_palette_order = palette_order_for_pattern(
                        selected_pattern,
                        selected_raw_profile,
                    )
                    try:
                        image, palette_colors, bpp = decode_tx_pl(
                            tx_segment.data,
                            selected_pl_segment.data,
                            palette_order=selected_palette_order,
                        )
                    except ValueError:
                        continue
                    pl_segment = selected_pl_segment
                    raw_profile = selected_raw_profile
                    pattern = selected_pattern
                    palette_order = selected_palette_order
                    selected_dedupe_key = texture_dedupe_key(
                        rel_source,
                        tx_segment,
                        image_digest(image),
                    )
                output_image, layout, layout_offset = apply_verified_layout(
                    group=group,
                    raw_image=image,
                    tx_segment=tx_segment,
                    pl_segment=pl_segment,
                    palette_order=palette_order,
                    blob=blob,
                )
                if verified_group == "게임내 메뉴얼":
                    selected_dedupe_key = (
                        f"manual:{rel_source}:{tx_segment.path}:"
                        f"{tx_segment.offset}:{pl_segment.offset}:{image_digest(output_image)}"
                    )

            maybe_emit(
                TextureDraft(
                    source=str(rel_source),
                    tree_path=tx_segment.path,
                    offset=tx_segment.offset,
                    palette_colors=palette_colors,
                    bpp=bpp,
                    category=category,
                    verified_group=verified_group,
                    ordinal=ordinal,
                    pattern=pattern,
                    palette_order=palette_order,
                    palette_offset=pl_segment.offset,
                    storage_width=width,
                    storage_height=height,
                    layout=layout,
                    layout_offset=layout_offset,
                    dialogue_line_control_offset=dialogue_line_control_offset,
                    dialogue_line_count=dialogue_line_count,
                    dialogue_line_lengths=dialogue_line_lengths,
                    dialogue_speaker_id=dialogue_speaker_id,
                    filename_stem=f"{resource_path.stem}_{safe_slug(tx_segment.path)}",
                    image=output_image,
                    dedupe_key=selected_dedupe_key,
                )
            )

    for start_ordinal in sorted(list(pending_combined)):
        emit_combined(start_ordinal, pending_combined.pop(start_ordinal))

    LAST_DISCOVERY_NEXT_ORDINAL = output_index
    return records


def copy_raw_pngs(
    source_root: Path,
    out_root: Path,
    records: list[TextureRecord],
    categories: set[str],
) -> None:
    if "raw_png" not in categories:
        return

    raw_dir = out_root / "raw_png"
    output_index = sum(1 for record in records if not record.duplicate) + 1
    for png_path in sorted(source_root.rglob("*.png")):
        if not png_path.read_bytes().startswith(PNG_MAGIC):
            continue
        rel_source = png_path.relative_to(source_root)
        with Image.open(png_path) as image:
            converted = image.convert("RGBA")
            profile = image_palette_profile(converted)
            pattern = infer_pattern(converted.width, converted.height, png_path.name, profile)
            digest = hashlib.sha1(
                converted.size[0].to_bytes(2, "little")
                + converted.size[1].to_bytes(2, "little")
                + converted.tobytes()
            ).hexdigest()
            category = "raw_png"
            filename = f"{output_index:06d}-{digest[:12]}_{converted.width}x{converted.height}_{png_path.stem}.png"
            output_index += 1
            output_rel = str(Path(category) / filename)
            output_path = out_root / output_rel
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if not output_path.exists():
                shutil.copy2(png_path, output_path)
            alpha_pixels, opaque_pixels, bbox = image_stats(converted)
            records.append(
                TextureRecord(
                    source=str(rel_source),
                    tree_path="/",
                    offset=0,
                    width=converted.width,
                    height=converted.height,
                    palette_colors=0,
                    bpp=0,
                    category=category,
                    verified_group="",
                    ordinal=output_index - 1,
                    pattern=pattern,
                    palette_profile=profile,
                    palette_order="n/a",
                    palette_offset=0,
                    storage_width=converted.width,
                    storage_height=converted.height,
                    layout="linear",
                    layout_offset=0,
                    sha1=digest,
                    output=output_rel,
                    duplicate=False,
                    alpha_pixels=alpha_pixels,
                    opaque_pixels=opaque_pixels,
                    bbox=bbox,
                    output_group="",
                    output_group_part=0,
                    output_group_parts=1,
                    output_crop_x=0,
                    output_crop_y=0,
                    output_crop_width=converted.width,
                    output_crop_height=converted.height,
                    output_clear_rects="",
                    dialogue_line_control_offset=0,
                    dialogue_line_count=0,
                    dialogue_line_lengths="",
                    dialogue_speaker_id="",
                )
            )


def write_manifest(out_root: Path, records: list[TextureRecord]) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    json_path = out_root / "manifest.json"
    csv_path = out_root / "manifest.csv"
    summary_path = out_root / "SUMMARY.md"

    with json_path.open("w", encoding="utf-8") as fp:
        json.dump([asdict(record) for record in records], fp, ensure_ascii=False, indent=2)

    with csv_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(asdict(records[0]).keys()) if records else [])
        if records:
            writer.writeheader()
            for record in records:
                writer.writerow(asdict(record))

    counts: dict[str, int] = {}
    unique_counts: dict[str, int] = {}
    for record in records:
        counts[record.category] = counts.get(record.category, 0) + 1
        if not record.duplicate:
            unique_counts[record.category] = unique_counts.get(record.category, 0) + 1

    lines = [
        "# Static Texture Dump Summary",
        "",
        f"- Total texture instances: {len(records)}",
        f"- Unique PNG outputs: {sum(unique_counts.values())}",
        "",
        "| Category | Instances | Unique PNGs |",
        "| :--- | ---: | ---: |",
    ]
    for category in sorted(counts):
        lines.append(f"| {category} | {counts[category]} | {unique_counts.get(category, 0)} |")
    lines.append("")
    lines.append("Generated by `scripts/dump_static_textures.py` from static resource files, not PPSSPP runtime dumps.")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="unpacked_mkd", help="directory or file containing extracted resources")
    parser.add_argument("--out", default="textures_static", help="output directory for decoded PNGs")
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
    parser.add_argument("--max-files", type=int, default=None, help="only scan the first N resource files")
    parser.add_argument("--no-dedupe", action="store_true", help="write every decoded texture instance")
    parser.add_argument(
        "--suffix-only",
        action="store_true",
        help="only scan .mrg/.pse files instead of also probing other files with TX/PL/PSET/PTNSET magics",
    )
    parser.add_argument(
        "--include-loose",
        action="store_true",
        help="also try nearest-palette decoding for valid TX records outside recognized containers",
    )
    parser.add_argument("--skip-raw-png", action="store_true", help="do not copy already-embedded PNG files")
    parser.add_argument("--clean", action="store_true", help="remove the output directory before dumping")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_root = Path(args.source)
    out_root = Path(args.out)

    if not source_root.exists():
        raise SystemExit(f"source directory does not exist: {source_root}")
    if args.clean and out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    records = dump_mrg_textures(
        source_root=source_root,
        out_root=out_root,
        max_files=args.max_files,
        dedupe=not args.no_dedupe,
        dump_all=args.all,
        categorized_text=args.categories,
        scan_all_files=not args.suffix_only,
        include_loose=args.include_loose,
    )
    if args.all and not args.skip_raw_png:
        copy_raw_pngs(source_root, out_root, records, {"raw_png"})
    write_manifest(out_root, records)

    unique = sum(1 for record in records if not record.duplicate)
    print(f"Decoded texture instances: {len(records)}")
    print(f"Unique PNG outputs: {unique}")
    print(f"Output directory: {out_root}")
    print(f"Manifest: {out_root / 'manifest.csv'}")


if __name__ == "__main__":
    main()
