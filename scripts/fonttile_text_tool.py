#!/usr/bin/env python3
from __future__ import annotations

"""Dump and patch fixed-slot strings rendered through the small font tile.

The normal dump path is intentionally strict: EBOOT strings come from the
relocation-backed pointer tables and known inline .data record table identified
by scripts/tile_text/analyze_relocated_string_tables.py.  Archive 0 DAT/MRG
matches include EBOOT-derived exact duplicates plus strings backed by detected
archive-side MRG child record tables.

The small font renderer accepts single-byte CP932/JIS X 0201 style text:
ASCII plus halfwidth kana.  Patches keep every file size unchanged.  Oversized
pointer-table strings can be relocated, while oversized fixed slots can hold a
three-byte marker resolved through a mapped EBOOT string pool.
"""

"""
How to use
1. dump and generate dictionary
.venv/bin/python scripts/fonttile_text_tool.py dump --output results/fonttile_text_slots.csv
.venv/bin/python scripts/fonttile_text_tool.py dictionary results/fonttile_text_slots.csv --output results/fonttile_text_dictionary.csv
.venv/bin/python scripts/apply_fonttile_translations.py

2. edit dictionary

3. 
.venv/bin/python scripts/fonttile_text_tool.py fill results/fonttile_text_slots.csv results/fonttile_text_dictionary.csv --output results/fonttile_text_slots.filled.csv

rm -rf work/fonttile_patch
mkdir -p work/fonttile_patch

cp -R unpacked_mkd work/fonttile_patch/unpacked_mkd
.venv/bin/python scripts/fonttile_text_tool.py apply results/fonttile_text_slots.filled.csv --out-root work/fonttile_patch


.venv/bin/python scripts/rebuild_mkd.py \
  --archives 0 \
  --unpacked work/fonttile_patch/unpacked_mkd \
  --out rebuilt_mkd \
  --no-reuse-unchanged \
  --optimal-sd0

.venv/bin/python scripts/import_iso_files.py \
  --iso game-patched.iso \
  --file PSP_GAME/SYSDIR/EBOOT.BIN=work/fonttile_patch/results/ULJS00178_EBOOT.BIN
.venv/bin/python scripts/import_mkd.py --iso game-patched.iso --mkd-dir rebuilt_mkd
"""


import argparse
import csv
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_eboot_assets import parse_elf_sections, va_to_offset  # noqa: E402
from tile_text.analyze_relocated_string_tables import (  # noqa: E402
    Elf32,
    add_capacity,
    decode_cp932 as decode_eboot_cp932,
    display_runs_from_runs,
    has_japanese,
    runs_from_relocated_rodata_pointers,
)


FONT_BYTES = set(range(0x20, 0x7F)) | set(range(0xA1, 0xE0))
CANDIDATE_SUFFIXES = {".dat", ".mrg"}
MRG_MAGIC = b"MRG\x00"
SMALL_FONT_LOOKUP_OFFSET = 0x160C6C
SMALL_FONT_CELL_COLUMNS = 32
SMALL_FONT_BASE_ROWS = 5
SMALL_FONT_MAX_ROWS = 8
SMALL_FONT_CELL_COUNT = SMALL_FONT_CELL_COLUMNS * SMALL_FONT_MAX_ROWS
SMALL_FONT_MAX_GLYPH_INDEX = 0xFE
SMALL_FONT_STOCK_LOOKUP_ENTRIES = 0x95
SMALL_FONT_EXPANDED_LOOKUP_OFFSET = 0x15A840
SMALL_FONT_EXPANDED_LOOKUP_LOW_ENTRIES = 0x5F
SMALL_FONT_EXPANDED_LOOKUP_ENTRIES = SMALL_FONT_EXPANDED_LOOKUP_LOW_ENTRIES + 0x80
SMALL_FONT_STOCK_LOOKUP_BYTES = bytes.fromhex(
    "2e2f30310088862c2d890102030405060708090a0b0c0d0e0f8f878b908c8d8e"
    "101112131415161718191a1b1c1d1e1f202122232425262728292a912b2c2d8a"
    "6c6d6e6f707172737475767778797a7b7c7d7e7f808182838485323334353637"
    "38393a3b3c033d3e3f404142434445464748494a4b4c4d4e4f505152535455"
    "565758595a5b5c5d5e5f606162636465666768696a6b"
)
SMALL_FONT_EXPANDED_LOOKUP_BASE_PATCHES = (
    (0x19EE0, 0x2455646C, 0x24550040),
    (0x1A078, 0x2454646C, 0x24540040),
)
SMALL_FONT_EXPANDED_HIGH_LOAD_PATCHES = (
    (0x19FC8, 0x9050FFB5, 0x9050FFDF),
    (0x1A160, 0x9050FFB5, 0x9050FFDF),
)
SMALL_FONT_COMBINING_MARK_RANGE_PATCHES = (
    ("width", 0x19E64, 0x24A20000, 0x19E74, 0x2C420000),
    ("render-r1", 0x19EF0, 0x24620000, 0x19EF8, 0x2C420000),
    ("render-r2", 0x1A084, 0x24620000, 0x1A08C, 0x2C420000),
)
SMALL_FONT_CELL_SIZE = 8
DALMOORI_GRID_FONT_SIZE = 64
DALMOORI_GRID_SAMPLE_STEP = 8
SMALL_FONT_TILE_GLYPHS = (
    "%",
    "+",
    ",",
    "-",
    ".",
    "/",
    "0",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
    "G",
    "H",
    "I",
    "J",
    "K",
    "L",
    "M",
    "N",
    "O",
    "P",
    "Q",
    "R",
    "S",
    "T",
    "U",
    "V",
    "W",
    "X",
    "Y",
    "Z",
    "[",
    "]",
    "α",
    "β",
    "Ⅱ",
    "改",
    "型",
    "Ⅲ",
    "・",
    "ヲ",
    "ァ",
    "ィ",
    "ゥ",
    "ェ",
    "ォ",
    "ャ",
    "ュ",
    "ョ",
    "ッ",
    "ア",
    "イ",
    "ウ",
    "エ",
    "オ",
    "カ",
    "キ",
    "ク",
    "ケ",
    "コ",
    "サ",
    "シ",
    "ス",
    "セ",
    "ソ",
    "タ",
    "チ",
    "ツ",
    "テ",
    "ト",
    "ナ",
    "ニ",
    "ヌ",
    "ネ",
    "ノ",
    "ハ",
    "ヒ",
    "フ",
    "ヘ",
    "ホ",
    "マ",
    "ミ",
    "ム",
    "メ",
    "モ",
    "ヤ",
    "ユ",
    "ヨ",
    "ラ",
    "リ",
    "ル",
    "レ",
    "ロ",
    "ワ",
    "ン",
    "゛",
    "゜",
    "a",
    "b",
    "c",
    "d",
    "e",
    "f",
    "g",
    "h",
    "i",
    "j",
    "k",
    "l",
    "m",
    "n",
    "o",
    "p",
    "q",
    "r",
    "s",
    "t",
    "u",
    "v",
    "w",
    "x",
    "y",
    "z",
    "ν",
    '"-f"',
    "ｖ",
    "三",
    "ｂ",
    "ｄ",
    "ｅ",
    "ｉ",
    "ｔ",
    "開",
    "発",
    '"+f"',
)
TX_MAGIC = b"TX\x00\x00"
PL_MAGIC = b"PL\x00\x00"
PSET_MAGIC = b"PSET"
PTNSET_MAGIC = b"PTN\x00SET\x00"
CMP0_MAGIC = b"CMP0"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ARCHIVE_TABLE_SKIP_MAGICS = (
    MRG_MAGIC,
    TX_MAGIC,
    PL_MAGIC,
    PSET_MAGIC,
    PTNSET_MAGIC,
    CMP0_MAGIC,
    PNG_MAGIC,
)
DEFAULT_PATHS = [
    Path("results/ULJS00178_EBOOT.BIN"),
    Path("unpacked_mkd/unpacked_0"),
]
DEFAULT_EBOOT = Path("results/ULJS00178_EBOOT.BIN")

# UTF-8 strings used by the save-data/title UI.  These are separate from the
# single-byte small-font strings handled by dump/fill/apply below.
EBOOT_UTF8_TRANSLATIONS = (
    (0x12B624, "正常終了しました。", "정상종료했습니다."),
    (0x12B640, "処理が中断されました", "처리가 중단됐습니다"),
    (
        0x12B660,
        "メモリースティック デュオ™ アクセス中にスリープしたため、処理を中断しました。",
        "메모리 스틱 접근 중 슬립하여 중단했습니다.",
    ),
    (0x12B80C, "ギレンの野望　アクシズの脅威Ｖ", "기렌의 야망　액시즈의 위협 V"),
    (0x12B864, "システムファイル", "시스템 파일"),
    (0x12BC64, "連邦編　", "연방편　"),
    (0x12C064, "ジオン編　", "지온편　"),
    (0x12C464, "ティターンズ編　", "티탄즈편　"),
    (0x12C864, "ティターンズ・シロッコ編　", "티탄즈 시로코편　"),
    (0x12CC64, "正統ジオン編　", "정통 지온편　"),
    (0x12D064, "新生ジオン編　", "신생 지온편　"),
    (0x12D464, "ネオ・ジオン・キャスバル編　", "네오 지온 캐스발편　"),
    (0x12D864, "エゥーゴ編　", "에우고편　"),
    (0x12DC64, "アクシズ編　", "액시즈편　"),
    (0x12E064, "デラーズ・フリート編　", "데라즈 플리트편　"),
    (0x12E464, "エゥーゴ・クワトロ編　", "에우고 콰트로편　"),
    (0x12E864, "アクシズ・グレミー編　", "액시즈 그레미편　"),
    (0x12EC64, "ネオ・ジオン・シャア編　", "네오 지온 샤아편　"),
    (0x12F064, "テム・レイ編　", "템 레이편　"),
    (0x12F464, "システムデータ", "시스템 데이터"),
    (0x12F47C, "コンティニューデータ", "컨티뉴 데이터"),
    (0x12F49C, "セーブデータ", "저장 데이터"),
)


@dataclass(frozen=True)
class EbootFixedStringTable:
    region: str
    record_start: int
    record_count: int
    record_stride: int
    field_offset: int
    slot_size: int


DEFAULT_SMALL_FONT_TILE_PNG = Path(
    "textures_static/text/000542-7c454b44299a_256x40_000002e6_header_0.png"
)
DEFAULT_SMALL_FONT_TILE_OUTPUT = Path(
    "textures_translated/text/000542-7c454b44299a_256x40_000002e6_header_0.png"
)
DEFAULT_DALMOORI_FONT = Path("assets/fonts/dalmoori.ttf")
DEFAULT_ALL_KOREAN_FONTTILE_PNG = Path(
    "scripts/tile_text/merge_text/all_korean_fonttile.png"
)
DEFAULT_ALL_KOREAN_FONTTILE_MAP = Path(
    "scripts/tile_text/merge_text/all_korean_fonttile_map.csv"
)
EBOOT_STRUCTURED_RODATA_TABLES = (
    # Code 0x1a7b4/0xad1c0 builds VMA 0x001329a4 and indexes
    # index * 0x18.  The display name lives at record +0x09.
    EbootFixedStringTable(
        region=".rodata:weapon_records_name",
        record_start=0x132A64,
        record_count=0x533,
        record_stride=0x18,
        field_offset=0x09,
        slot_size=0x0F,
    ),
    # Code 0x1b71c returns index * 0x34 + VMA 0x0013a814; callers
    # at 0x28ef4/0x37ac4/0x668e8 use the same development record base.
    EbootFixedStringTable(
        region=".rodata:development_records_name",
        record_start=0x13A8D0,
        record_count=0x15F,
        record_stride=0x34,
        field_offset=0x04,
        slot_size=0x17,
    ),
)
EBOOT_DIRECT_RODATA_STRINGS = (
    (".rodata:direct_unit_status_text", 0x140FB4, 0x08),
    (".rodata:direct_unit_status_text", 0x140FC8, 0x08),
    (".rodata:direct_unit_status_text", 0x140FD0, 0x08),
    (".rodata:direct_unit_status_text", 0x140FD8, 0x08),
    (".rodata:direct_unit_status_text", 0x140FE0, 0x08),
    (".rodata:direct_unit_status_text", 0x140FE8, 0x08),
    (".rodata:direct_ui_literal", 0x1429D4, 0x08),
    (".rodata:direct_ui_literal", 0x146920, 0x14),
    (".rodata:direct_ui_literal", 0x146934, 0x08),
)
EBOOT_DATA_UNIT_SHORT_NAME_MIN2_OFFSETS = {
    0x16192C,  # ﾈﾓ
    0x16A48C,  # ﾈﾓ
    0x16B6FC,  # ﾈﾛ; reported record-side location 0x16B6EC
    0x16B74C,  # ﾈﾛ
}
EBOOT_ASCII_RELOCATED_DISPLAY_RUNS = (
    ("NAME", "TYPE", "RADER", "HPMAX", "SPEED", "MOVE", "ENMAX"),
    ("NAME", "SHIKIN", "SHIGEN"),
    (
        "NO",
        "WEAPON",
        "CHARA",
        "AREA",
        "TYPE",
        "HP",
        "EN",
        "HPMAX",
        "ENMAX",
        "RADER",
        "SPEED",
        "MOVE",
    ),
    ("NO", "SIKIN", "SIGEN", "NAME", "RADER", "HPMAX", "SPEED", "MOVE", "ENMAX"),
)
RELOCATED_EXTERNAL_POOL_MARGIN = 16
RELOCATED_EXTERNAL_POOL_RANGES = (
    (0x12BC70, 0x12C064),
    (0x12C073, 0x12C464),
    (0x12C47C, 0x12C864),
    (0x12C88B, 0x12CC64),
    (0x12CC79, 0x12D064),
    (0x12D079, 0x12D464),
    (0x12D48E, 0x12D864),
    (0x12D876, 0x12DC64),
    (0x12DC76, 0x12E064),
    (0x12E085, 0x12E464),
    (0x12E485, 0x12E864),
    (0x12E885, 0x12EC64),
    (0x12EC88, 0x12F064),
)
# Fixed-slot strings that do not have a pointer table use a three-byte marker
# and are resolved by tiny trampolines in the three small-font entry points.
# PSP main executables are loaded at this fixed base; the project save-state
# verifier uses the same address when matching the loaded ELF bytes.
PSP_EBOOT_LOAD_BASE = 0x08804000
INDIRECT_STRING_MARKER = 0x1F
INDIRECT_STRING_MARKER_SIZE = 3
INDIRECT_STRING_POOL_BASE_OFFSET = (
    RELOCATED_EXTERNAL_POOL_RANGES[0][0] + RELOCATED_EXTERNAL_POOL_MARGIN
)
INDIRECT_STRING_ENTRY_PATCHES = (
    # kind, file offset, original first two words
    ("width", 0x19E58, (0x90850000, 0x10A0000A)),
    ("render-r1", 0x19E90, (0x27BDFFD0, 0xAFBE0020)),
    # The first render-r2 pair includes an ELF relocation at 0x1a020, so hook
    # after its initial stack adjustment/save instead.
    ("render-r2", 0x1A024, (0x311E00FF, 0xAFB7002C)),
)
KANA_BASE_BYTES = set(range(0xA6, 0xDE)) - {0xB0}
KANA_BODY_BYTES = KANA_BASE_BYTES | {0xB0}
KANA_MARK_BYTES = {0xDE, 0xDF}
KANA_PUNCT_BYTES = set(range(0xA1, 0xA6))
TEXT_ASCII_BYTES = set(range(0x20, 0x7F))
ARCHIVE_BAD_ASCII_BYTES = set(b"%'&()<>@\\^_`{|}~;?")
# The EBOOT lookup table maps some ASCII/kana-punctuation bytes to project
# font-tile glyphs that differ from a normal CP932 decode.  These byte aliases
# are byte-accurate against results/ULJS00178_EBOOT.BIN @0x160c6c, and match
# the glyph order documented in scripts/tile_text/fonttile_hint.md.
DISPLAY_BYTE_ALIASES = {
    0x21: "Ⅱ",
    0x22: "改",
    0x23: "型",
    0x24: "Ⅲ",
    0x26: "ｖ",
    0x27: "ν",
    0x28: "α",
    0x29: "β",
    0x2A: "三",
    0x3A: "開",
    0x3B: '"-f"',
    0x3C: "ｄ",
    0x3D: "発",
    0x3E: "ｅ",
    0x3F: "ｉ",
    0x40: "ｔ",
    0x5C: '"+f"',
    0x5E: "α",
    0x5F: "β",
    0x60: "ｂ",
    0x7B: "・",
    0x7C: "ヲ",
    0x7D: "ァ",
    0x7E: "ィ",
    0xA1: "w",
    0xA2: "x",
    0xA3: "y",
    0xA4: "z",
}
DISPLAY_SEQUENCE_ALIASES = {
    b":=": "開発",
}
DISPLAY_TEXT_ALIASES = {
    bytes([byte]).decode("cp932"): glyph
    for byte, glyph in DISPLAY_BYTE_ALIASES.items()
    if 0x20 <= byte < 0x7F
}
ARCHIVE_ALLOWED_TRAILING_ASCII = {
    b"I",
    b"II",
    b"III",
    b"IV",
    b"V",
    b"VI",
    b"VII",
    b"VIII",
    b"IX",
    b"X",
    b"L",
    b"R",
    b"M",
    b"MA",
    b"MS",
    b"ML",
    b"ZZ",
}
MIPS_OPS = {
    0x02,
    0x03,
    0x04,
    0x05,
    0x06,
    0x07,
    0x08,
    0x09,
    0x0A,
    0x0B,
    0x0C,
    0x0D,
    0x0E,
    0x0F,
    0x10,
    0x11,
    0x12,
    0x20,
    0x21,
    0x23,
    0x24,
    0x25,
    0x28,
    0x29,
    0x2B,
    0x31,
    0x39,
}
MIPS_SPECIAL_FUNCTS = {
    0x00,
    0x02,
    0x03,
    0x08,
    0x09,
    0x10,
    0x12,
    0x18,
    0x19,
    0x1A,
    0x1B,
    0x20,
    0x21,
    0x22,
    0x23,
    0x24,
    0x25,
    0x26,
    0x27,
    0x2A,
    0x2B,
}
MIPS_REGIMM_RT = {0x00, 0x01, 0x10, 0x11}

KOREAN_FONT_SOURCE_TEXTS_PATH = (
    Path(__file__).resolve().parent / "tile_text" / "korean_font_source_texts.json"
)
KOREAN_SOURCE_MARK_TYPES = ("demark", "mark")


@dataclass(frozen=True)
class KoreanFontSourceEntry:
    text: str
    mark_type: str


def load_korean_font_source_texts(
    path: Path | None = None,
) -> tuple[KoreanFontSourceEntry, ...]:
    texts_path = path or KOREAN_FONT_SOURCE_TEXTS_PATH
    data = json.loads(texts_path.read_text(encoding="utf-8"))
    groups: list[tuple[str, object]]
    if isinstance(data, list):
        groups = [("demark", data)]
    elif isinstance(data, dict):
        unknown_keys = sorted(set(data) - set(KOREAN_SOURCE_MARK_TYPES))
        if unknown_keys:
            raise ValueError(
                f"{texts_path}: unknown mark group(s): {', '.join(unknown_keys)}"
            )
        groups = [(mark_type, data.get(mark_type, [])) for mark_type in KOREAN_SOURCE_MARK_TYPES]
    else:
        raise ValueError(f"{texts_path}: expected a JSON array or mark/demark object")

    result: list[KoreanFontSourceEntry] = []
    for mark_type, raw_entries in groups:
        if not isinstance(raw_entries, list):
            raise ValueError(f"{texts_path}: {mark_type} must be a JSON array")
        for index, entry in enumerate(raw_entries):
            if not isinstance(entry, str):
                raise ValueError(f"{texts_path}: {mark_type}[{index}] must be a string")
            result.append(KoreanFontSourceEntry(entry, mark_type))
    return tuple(result)


KOREAN_FONT_SOURCE_ENTRIES = load_korean_font_source_texts()
KOREAN_FONT_SOURCE_TEXTS = tuple(entry.text for entry in KOREAN_FONT_SOURCE_ENTRIES)
KOREAN_SOURCE_HAS_MARKS = any(entry.mark_type == "mark" for entry in KOREAN_FONT_SOURCE_ENTRIES)


def stock_small_font_lookup_index(byte: int) -> int | None:
    # The EBOOT renderer indexes the table as byte-0x21 for non-negative bytes
    # and byte-0x4b for signed-negative bytes.  The stock table is only known
    # through offset 0x94, matching ASCII/JIS X 0201 halfwidth kana coverage.
    if 0x21 <= byte <= 0x7F:
        return byte - 0x21
    if 0x80 <= byte <= 0xDF:
        return byte - 0x4B
    return None


def small_font_lookup_index(byte: int) -> int | None:
    # Patched renderer policy: printable/control stock bytes keep their own
    # low table entries, and high bytes get independent entries after them.
    if 0x21 <= byte <= 0xFF:
        return byte - 0x21
    return None


# Legacy one-byte Hangul used safe high bytes for the dakuten/handakuten tiles.
# Combining font mode uses the original 0xde/0xdf bytes as mark bytes instead.
KOREAN_LEGACY_REPLACEABLE_BYTES = (
    tuple(range(0xA6, 0xB0)) + tuple(range(0xB1, 0xDE)) + (0xA1, 0xA2)
)
KOREAN_COMBINING_REPLACEABLE_BYTES = tuple(range(0xA6, 0xB0)) + tuple(range(0xB1, 0xE0))
KOREAN_REPLACEABLE_BYTES = (
    KOREAN_COMBINING_REPLACEABLE_BYTES
    if KOREAN_SOURCE_HAS_MARKS
    else KOREAN_LEGACY_REPLACEABLE_BYTES
)
KOREAN_REPLACEABLE_TILE_INDICES = tuple(range(51, 108))
KOREAN_ALIAS_BYTES = (0x23, 0x3A, 0x3D)
KOREAN_ALIAS_TILE_INDICES = (48, 143, 144)
KOREAN_EMPTY_BYTES = tuple(range(0x80, 0x8E))
KOREAN_EMPTY_TILE_INDICES = tuple(range(146, 160))
KOREAN_FIXED_GLYPH_ALIASES: dict[str, tuple[int, int, str, int]] = {}
KOREAN_EXTENDED_PRIMARY_BYTES = tuple(range(0x80, 0xA5)) + tuple(range(0xE0, 0xFF))


def korean_extended_candidate_bytes() -> tuple[int, ...]:
    reserved = (
        set(KOREAN_REPLACEABLE_BYTES)
        | set(KOREAN_EMPTY_BYTES)
        | {alias[0] for alias in KOREAN_FIXED_GLYPH_ALIASES.values()}
        | {0xFF}
    )
    return tuple(byte for byte in KOREAN_EXTENDED_PRIMARY_BYTES if byte not in reserved)


KOREAN_EXTENDED_BYTES = korean_extended_candidate_bytes()
KOREAN_EXTENDED_TILE_INDICES = tuple(range(160, SMALL_FONT_MAX_GLYPH_INDEX + 1))
DISPLAY_SINGLE_BYTE_BY_GLYPH: dict[str, int] = {}
for _byte, _glyph in DISPLAY_BYTE_ALIASES.items():
    if len(_glyph) == 1:
        if _byte >= 0x80 and 0x21 <= ord(_glyph) <= 0x7E:
            continue
        DISPLAY_SINGLE_BYTE_BY_GLYPH.setdefault(_glyph, _byte)
DISPLAY_TEXT_BYTE_SEQUENCES = {
    '"-f"': bytes([0x3B]),
    '"+f"': bytes([0x5C]),
}


@dataclass(frozen=True)
class Region:
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class StringSlot:
    path: Path
    offset: int
    span: int
    max_bytes: int
    raw: bytes
    text: str
    region: str


@dataclass(frozen=True)
class ApplyRow:
    row_index: int
    source_path: Path
    offset: int
    span: int
    max_bytes: int
    original_hex: str
    translation: str
    encoded: bytes
    region: str


@dataclass(frozen=True)
class RelocatedDisplayTarget:
    offset: int
    vma: int
    span: int
    raw: bytes
    text: str
    table_ids: tuple[int, ...]
    pointer_file_offsets: tuple[int, ...]


@dataclass(frozen=True)
class RelocatedDisplayRelayoutStats:
    clusters: int
    rows: int
    pointers: int
    forced_truncated_bytes: int
    externalized_rows: int = 0
    externalized_bytes: int = 0


@dataclass(frozen=True)
class IndirectStringPoolStats:
    rows: int
    unique_payloads: int
    payload_bytes: int
    stub_bytes: int


@dataclass(frozen=True)
class RelocatedExternalPayload:
    offset: int
    payload: bytes


@dataclass(frozen=True)
class KoreanGlyphMapping:
    glyph: str
    mark_type: str
    byte: int
    tile_index: int
    source: str


def unique_chars(texts: tuple[str, ...]) -> tuple[str, ...]:
    chars: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for char in text:
            if char in seen:
                continue
            seen.add(char)
            chars.append(char)
    return tuple(chars)


def unique_char_mark_types(
    entries: tuple[KoreanFontSourceEntry, ...],
) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for entry in entries:
        for char in entry.text:
            existing = seen.get(char)
            if existing is not None:
                if existing != entry.mark_type:
                    raise ValueError(
                        f"{KOREAN_FONT_SOURCE_TEXTS_PATH}: glyph {char!r} appears as "
                        f"both {existing!r} and {entry.mark_type!r}"
                    )
                continue
            seen[char] = entry.mark_type
            result.append((char, entry.mark_type))
    return tuple(result)


def load_all_korean_fonttile_map(
    path: Path = DEFAULT_ALL_KOREAN_FONTTILE_MAP,
) -> dict[str, tuple[int, int]]:
    if not path.exists():
        raise SystemExit(f"all Korean fonttile map not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required_fields = {"glyph", "src_x", "src_y"}
        missing = required_fields - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(
                f"{path}: missing required column(s): {', '.join(sorted(missing))}"
            )
        glyph_sources: dict[str, tuple[int, int]] = {}
        duplicate_glyphs: list[str] = []
        for row_index, row in enumerate(reader, start=2):
            glyph = row.get("glyph") or ""
            if not glyph:
                raise SystemExit(f"{path}:{row_index}: glyph must not be empty")
            if glyph in glyph_sources:
                duplicate_glyphs.append(glyph)
                continue
            try:
                src_x = int(row.get("src_x") or "")
                src_y = int(row.get("src_y") or "")
            except ValueError as exc:
                raise SystemExit(f"{path}:{row_index}: src_x/src_y must be integers") from exc
            glyph_sources[glyph] = (src_x, src_y)
    if duplicate_glyphs:
        labels = ", ".join(duplicate_glyphs)
        raise SystemExit(f"{path}: duplicate glyph mapping(s): {labels}")
    return glyph_sources


def validate_korean_font_source_in_all_fonttile_map(
    chars: tuple[str, ...],
    *,
    map_path: Path = DEFAULT_ALL_KOREAN_FONTTILE_MAP,
) -> None:
    glyph_sources = load_all_korean_fonttile_map(map_path)
    missing_glyphs = [char for char in chars if char not in glyph_sources]
    if missing_glyphs:
        missing_text = "".join(missing_glyphs)
        raise SystemExit(f"{map_path}: missing glyph(s) from Korean source: {missing_text}")


KOREAN_SOURCE_UNIQUE_CHAR_MARK_TYPES = unique_char_mark_types(KOREAN_FONT_SOURCE_ENTRIES)
KOREAN_SOURCE_UNIQUE_CHARS = tuple(
    char for char, _mark_type in KOREAN_SOURCE_UNIQUE_CHAR_MARK_TYPES
)
KOREAN_SOURCE_MARK_TYPE_BY_CHAR = dict(KOREAN_SOURCE_UNIQUE_CHAR_MARK_TYPES)
validate_korean_font_source_in_all_fonttile_map(KOREAN_SOURCE_UNIQUE_CHARS)
KOREAN_SOURCE_CHAR_SET = set(KOREAN_SOURCE_UNIQUE_CHARS)
KOREAN_FONT_CHARS = tuple(
    char
    for char in KOREAN_SOURCE_UNIQUE_CHARS
    if char not in KOREAN_FIXED_GLYPH_ALIASES
)


def korean_glyph_slots() -> tuple[tuple[int, int, str], ...]:
    slots: list[tuple[int, int, str]] = []
    slots.extend(
        (byte, tile_index, "kana")
        for byte, tile_index in zip(KOREAN_REPLACEABLE_BYTES, KOREAN_REPLACEABLE_TILE_INDICES)
    )
    slots.extend(
        (byte, tile_index, "alias")
        for byte, tile_index in zip(KOREAN_ALIAS_BYTES, KOREAN_ALIAS_TILE_INDICES)
    )
    slots.extend(
        (byte, tile_index, "empty")
        for byte, tile_index in zip(KOREAN_EMPTY_BYTES, KOREAN_EMPTY_TILE_INDICES)
    )
    slots.extend(
        (byte, tile_index, "extended")
        for byte, tile_index in zip(KOREAN_EXTENDED_BYTES, KOREAN_EXTENDED_TILE_INDICES)
    )
    return tuple(slots)


def korean_auto_combining_mark_range_for_count(count: int) -> tuple[int, int] | None:
    if count <= 0:
        return None
    end_byte = 0xFE
    start_byte = end_byte - count + 1
    if start_byte < 0x80:
        raise RuntimeError(
            f"Korean mark glyph list needs {count} bytes, but only high-byte combining "
            "ranges are supported"
        )
    return start_byte, end_byte


def korean_index_byte_sort_key(byte: int) -> int:
    # The game's name index compares string bytes as signed chars, so custom
    # high bytes sort in 0x80..0xfe order before positive low-byte aliases.
    return byte if byte >= 0x80 else byte + 0x100


def korean_glyph_mappings() -> tuple[KoreanGlyphMapping, ...]:
    slots = korean_glyph_slots()
    glyphs_by_mark_type = {
        mark_type: tuple(
            glyph
            for glyph, glyph_mark_type in KOREAN_SOURCE_UNIQUE_CHAR_MARK_TYPES
            if glyph_mark_type == mark_type and glyph not in KOREAN_FIXED_GLYPH_ALIASES
        )
        for mark_type in KOREAN_SOURCE_MARK_TYPES
    }

    if not KOREAN_SOURCE_HAS_MARKS:
        if len(KOREAN_FONT_CHARS) > len(slots):
            overflow_chars = "".join(KOREAN_FONT_CHARS[len(slots) :])
            raise RuntimeError(
                f"Korean glyph list has {len(KOREAN_FONT_CHARS)} chars but only {len(slots)} "
                f"slots; overflow chars: {overflow_chars}"
            )
        mappings: list[KoreanGlyphMapping] = []
        slot_iter = iter(slots)
        for glyph in KOREAN_SOURCE_UNIQUE_CHARS:
            if glyph in KOREAN_FIXED_GLYPH_ALIASES:
                continue
            byte, tile_index, source = next(slot_iter)
            mark_type = KOREAN_SOURCE_MARK_TYPE_BY_CHAR.get(glyph, "demark")
            mappings.append(KoreanGlyphMapping(glyph, mark_type, byte, tile_index, source))
        return tuple(mappings)

    mark_range = korean_auto_combining_mark_range_for_count(len(glyphs_by_mark_type["mark"]))
    assert mark_range is not None
    mark_start, mark_end = mark_range
    mark_bytes = set(range(mark_start, mark_end + 1))
    mark_slots = tuple(slot for slot in slots if slot[0] in mark_bytes)
    demark_slots = tuple(
        sorted(
            (slot for slot in slots if slot[0] not in mark_bytes),
            key=lambda slot: korean_index_byte_sort_key(slot[0]),
        )
    )
    available_mark_bytes = {byte for byte, _tile_index, _source in mark_slots}
    missing_mark_bytes = sorted(mark_bytes - available_mark_bytes)
    if missing_mark_bytes:
        missing = ", ".join(f"0x{byte:02x}" for byte in missing_mark_bytes)
        raise RuntimeError(f"Korean mark byte range has no glyph slot(s): {missing}")

    if len(glyphs_by_mark_type["mark"]) > len(mark_slots):
        overflow_chars = "".join(glyphs_by_mark_type["mark"][len(mark_slots) :])
        raise RuntimeError(
            f"Korean mark glyph list has {len(glyphs_by_mark_type['mark'])} chars but "
            f"only {len(mark_slots)} mark slots; overflow chars: {overflow_chars}"
        )
    if len(glyphs_by_mark_type["demark"]) > len(demark_slots):
        overflow_chars = "".join(glyphs_by_mark_type["demark"][len(demark_slots) :])
        raise RuntimeError(
            f"Korean demark glyph list has {len(glyphs_by_mark_type['demark'])} chars but "
            f"only {len(demark_slots)} demark slots; overflow chars: {overflow_chars}"
        )

    mapping_by_char: dict[str, KoreanGlyphMapping] = {}
    for glyph, (byte, tile_index, source) in zip(glyphs_by_mark_type["demark"], demark_slots):
        mapping_by_char[glyph] = KoreanGlyphMapping(glyph, "demark", byte, tile_index, source)
    for glyph, (byte, tile_index, source) in zip(glyphs_by_mark_type["mark"], mark_slots):
        mapping_by_char[glyph] = KoreanGlyphMapping(glyph, "mark", byte, tile_index, source)

    return tuple(
        mapping_by_char[glyph]
        for glyph in KOREAN_SOURCE_UNIQUE_CHARS
        if glyph not in KOREAN_FIXED_GLYPH_ALIASES
    )


KOREAN_SLOT_GLYPH_MAPPINGS = korean_glyph_mappings()
KOREAN_FIXED_ALIAS_MAPPINGS = tuple(
    KoreanGlyphMapping(
        glyph,
        KOREAN_SOURCE_MARK_TYPE_BY_CHAR.get(glyph, "demark"),
        byte,
        tile_index,
        f"fixed:{stock_glyph}",
    )
    for glyph, (byte, tile_index, stock_glyph, _source_tile_index) in KOREAN_FIXED_GLYPH_ALIASES.items()
)
KOREAN_GLYPH_MAPPING_BY_CHAR = {
    mapping.glyph: mapping for mapping in KOREAN_SLOT_GLYPH_MAPPINGS
}
KOREAN_FIXED_ALIAS_MAPPING_BY_CHAR = {
    mapping.glyph: mapping for mapping in KOREAN_FIXED_ALIAS_MAPPINGS
}
KOREAN_ALL_GLYPH_MAPPINGS = tuple(
    KOREAN_FIXED_ALIAS_MAPPING_BY_CHAR.get(char) or KOREAN_GLYPH_MAPPING_BY_CHAR[char]
    for char in KOREAN_SOURCE_UNIQUE_CHARS
) + tuple(
    mapping
    for mapping in KOREAN_FIXED_ALIAS_MAPPINGS
    if mapping.glyph not in KOREAN_SOURCE_CHAR_SET
)


def korean_combining_mark_range_from_mappings(
    mappings: tuple[KoreanGlyphMapping, ...],
) -> tuple[int, int] | None:
    mark_bytes = sorted({mapping.byte for mapping in mappings if mapping.mark_type == "mark"})
    if not mark_bytes:
        return None
    start_byte = mark_bytes[0]
    end_byte = mark_bytes[-1]
    expected = set(range(start_byte, end_byte + 1))
    missing = sorted(expected - set(mark_bytes))
    if missing:
        missing_text = ", ".join(f"0x{byte:02x}" for byte in missing)
        raise RuntimeError(
            "Korean mark glyph bytes must form one contiguous range for the current "
            f"EBOOT patcher; missing {missing_text}"
        )
    return start_byte, end_byte


KOREAN_COMBINING_MARK_RANGE = korean_combining_mark_range_from_mappings(
    KOREAN_ALL_GLYPH_MAPPINGS
)
KOREAN_GLYPH_MAPPINGS = tuple(
    mapping
    for mapping in KOREAN_ALL_GLYPH_MAPPINGS
    if mapping.source.startswith("fixed:")
    or mapping.source in {"kana", "alias", "empty", "extended"}
)
KOREAN_FONT_BYTE_BY_CHAR = {
    mapping.glyph: mapping.byte for mapping in KOREAN_ALL_GLYPH_MAPPINGS
}
KOREAN_GLYPH_MAPPING_BY_BYTE = {
    mapping.byte: mapping for mapping in KOREAN_GLYPH_MAPPINGS
}
KOREAN_CUSTOM_TILE_INDICES = {mapping.tile_index for mapping in KOREAN_GLYPH_MAPPINGS}
KOREAN_CUSTOM_GLYPH_BY_TILE_INDEX = {
    mapping.tile_index: mapping.glyph for mapping in KOREAN_GLYPH_MAPPINGS
}


HANGUL_SYLLABLE_BASE = 0xAC00
HANGUL_SYLLABLE_END = 0xD7A3
HANGUL_JUNGSEONG_COUNT = 21
HANGUL_JONGSEONG_COUNT = 28
HANGUL_CHO_JUNG_BLOCK = HANGUL_JUNGSEONG_COUNT * HANGUL_JONGSEONG_COUNT
HANGUL_CHOSEONG = (
    "ㄱ",
    "ㄲ",
    "ㄴ",
    "ㄷ",
    "ㄸ",
    "ㄹ",
    "ㅁ",
    "ㅂ",
    "ㅃ",
    "ㅅ",
    "ㅆ",
    "ㅇ",
    "ㅈ",
    "ㅉ",
    "ㅊ",
    "ㅋ",
    "ㅌ",
    "ㅍ",
    "ㅎ",
)
HANGUL_JUNGSEONG = (
    "ㅏ",
    "ㅐ",
    "ㅑ",
    "ㅒ",
    "ㅓ",
    "ㅔ",
    "ㅕ",
    "ㅖ",
    "ㅗ",
    "ㅘ",
    "ㅙ",
    "ㅚ",
    "ㅛ",
    "ㅜ",
    "ㅝ",
    "ㅞ",
    "ㅟ",
    "ㅠ",
    "ㅡ",
    "ㅢ",
    "ㅣ",
)
HANGUL_JONGSEONG = (
    "",
    "ㄱ",
    "ㄲ",
    "ㄳ",
    "ㄴ",
    "ㄵ",
    "ㄶ",
    "ㄷ",
    "ㄹ",
    "ㄺ",
    "ㄻ",
    "ㄼ",
    "ㄽ",
    "ㄾ",
    "ㄿ",
    "ㅀ",
    "ㅁ",
    "ㅂ",
    "ㅄ",
    "ㅅ",
    "ㅆ",
    "ㅇ",
    "ㅈ",
    "ㅊ",
    "ㅋ",
    "ㅌ",
    "ㅍ",
    "ㅎ",
)
HANGUL_SIDE_VOWELS = frozenset("ㅏㅐㅑㅒㅓㅔㅕㅖㅣ")
HANGUL_BOTTOM_VOWELS = frozenset("ㅗㅛㅜㅠㅡ")
HANGUL_COMPLEX_VOWELS = frozenset("ㅘㅙㅚㅝㅞㅟㅢ")
KOREAN_COMPONENT_INITIAL_LAYOUT_FALLBACKS = {
    ("side", False): ("side",),
    ("bottom", False): ("bottom", "side"),
    ("complex", False): ("complex", "side", "bottom"),
    ("side", True): ("side", "complex"),
    ("bottom", True): ("bottom", "side", "complex"),
    ("complex", True): ("complex", "bottom", "side"),
}
KOREAN_COMPONENT_FINAL_ROW_PRIORITIES = {
    "side": (0, 1, 2),
    "bottom": (1, 0, 2),
    "complex": (2, 1, 0),
}


@dataclass(frozen=True)
class HangulSyllableParts:
    choseong: str
    jungseong: str
    jongseong: str


@dataclass(frozen=True)
class InitialComponentRow:
    layout: str
    has_final: bool
    components: dict[str, str]


def decompose_hangul_syllable(char: str) -> HangulSyllableParts | None:
    if len(char) != 1:
        return None
    codepoint = ord(char)
    if not HANGUL_SYLLABLE_BASE <= codepoint <= HANGUL_SYLLABLE_END:
        return None
    offset = codepoint - HANGUL_SYLLABLE_BASE
    choseong_index = offset // HANGUL_CHO_JUNG_BLOCK
    jungseong_index = (offset % HANGUL_CHO_JUNG_BLOCK) // HANGUL_JONGSEONG_COUNT
    jongseong_index = offset % HANGUL_JONGSEONG_COUNT
    return HangulSyllableParts(
        HANGUL_CHOSEONG[choseong_index],
        HANGUL_JUNGSEONG[jungseong_index],
        HANGUL_JONGSEONG[jongseong_index],
    )


def hangul_vowel_layout(jungseong: str) -> str:
    if jungseong in HANGUL_SIDE_VOWELS:
        return "side"
    if jungseong in HANGUL_BOTTOM_VOWELS:
        return "bottom"
    if jungseong in HANGUL_COMPLEX_VOWELS:
        return "complex"
    raise ValueError(f"지원하지 않는 한글 중성: {jungseong}")


def korean_source_rows(mark_type: str) -> tuple[str, ...]:
    return tuple(entry.text for entry in KOREAN_FONT_SOURCE_ENTRIES if entry.mark_type == mark_type)


def build_initial_component_rows() -> tuple[InitialComponentRow, ...]:
    # The source order controls byte/tile allocation, so authors may group
    # demark glyphs by choseong (가나다 order) instead of by render layout.
    # Rebuild the semantic rows from each glyph's Unicode decomposition rather
    # than assigning meaning to JSON array boundaries.
    rows_by_condition: dict[tuple[str, bool], dict[str, str]] = {}
    for row_index, text in enumerate(korean_source_rows("demark")):
        if not text:
            raise ValueError(
                f"{KOREAN_FONT_SOURCE_TEXTS_PATH}: demark[{row_index}]가 비어 있습니다"
            )
        for glyph in text:
            parts = decompose_hangul_syllable(glyph)
            if parts is None:
                raise ValueError(
                    f"{KOREAN_FONT_SOURCE_TEXTS_PATH}: demark[{row_index}] "
                    f"{glyph!r}는 한글 음절이어야 합니다"
                )
            glyph_layout = hangul_vowel_layout(parts.jungseong)
            glyph_has_final = bool(parts.jongseong)
            condition = (glyph_layout, glyph_has_final)
            row = rows_by_condition.setdefault(condition, {})
            previous = row.get(parts.choseong)
            if previous is not None:
                raise ValueError(
                    f"{KOREAN_FONT_SOURCE_TEXTS_PATH}: "
                    f"{glyph_layout}/{'있음' if glyph_has_final else '없음'} "
                    f"초성 {parts.choseong} component가 중복됩니다: "
                    f"{previous!r}, demark[{row_index}]의 {glyph!r}"
                )
            row[parts.choseong] = glyph
    return tuple(
        InitialComponentRow(layout, has_final, components)
        for (layout, has_final), components in rows_by_condition.items()
    )


def build_vowel_component_map(mark_row_index: int) -> dict[str, str]:
    mark_rows = korean_source_rows("mark")
    if mark_row_index >= len(mark_rows):
        return {}
    result: dict[str, str] = {}
    for glyph in mark_rows[mark_row_index]:
        parts = decompose_hangul_syllable(glyph)
        if parts is None:
            raise ValueError(
                f"{KOREAN_FONT_SOURCE_TEXTS_PATH}: mark[{mark_row_index}] "
                f"{glyph!r}는 한글 음절이어야 합니다"
            )
        previous = result.get(parts.jungseong)
        if previous is not None:
            raise ValueError(
                f"{KOREAN_FONT_SOURCE_TEXTS_PATH}: mark[{mark_row_index}] "
                f"중성 {parts.jungseong} component가 중복됩니다: {previous!r}, {glyph!r}"
            )
        result[parts.jungseong] = glyph
    return result


def build_final_component_rows() -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    for row_index, text in enumerate(korean_source_rows("mark")[2:], start=2):
        row: dict[str, str] = {}
        for glyph in text:
            parts = decompose_hangul_syllable(glyph)
            if parts is None:
                raise ValueError(
                    f"{KOREAN_FONT_SOURCE_TEXTS_PATH}: mark[{row_index}] "
                    f"{glyph!r}는 한글 음절이어야 합니다"
                )
            if not parts.jongseong:
                raise ValueError(
                    f"{KOREAN_FONT_SOURCE_TEXTS_PATH}: mark[{row_index}] "
                    f"{glyph!r}에는 종성 component가 없습니다"
                )
            previous = row.get(parts.jongseong)
            if previous is not None:
                raise ValueError(
                    f"{KOREAN_FONT_SOURCE_TEXTS_PATH}: mark[{row_index}] "
                    f"종성 {parts.jongseong} component가 중복됩니다: {previous!r}, {glyph!r}"
                )
            row[parts.jongseong] = glyph
        rows.append(row)
    return tuple(rows)


KOREAN_INITIAL_COMPONENT_ROWS = build_initial_component_rows()
KOREAN_VOWEL_COMPONENTS_NO_FINAL = build_vowel_component_map(0)
KOREAN_VOWEL_COMPONENTS_WITH_FINAL = build_vowel_component_map(1)
KOREAN_FINAL_COMPONENT_ROWS = build_final_component_rows()


def component_glyph_byte(glyph: str, *, label: str) -> int:
    byte = KOREAN_FONT_BYTE_BY_CHAR.get(glyph)
    if byte is None:
        raise ValueError(f"{label} component glyph {glyph!r}에 배정된 byte가 없습니다")
    return byte


def select_initial_component_glyph(parts: HangulSyllableParts, layout: str) -> str:
    has_final = bool(parts.jongseong)
    layout_fallbacks = KOREAN_COMPONENT_INITIAL_LAYOUT_FALLBACKS[(layout, has_final)]
    for candidate_layout in layout_fallbacks:
        for row in KOREAN_INITIAL_COMPONENT_ROWS:
            if row.layout != candidate_layout or row.has_final != has_final:
                continue
            glyph = row.components.get(parts.choseong)
            if glyph is not None:
                return glyph
    raise ValueError(
        f"초성 {parts.choseong} / layout={layout} / "
        f"종성={'있음' if has_final else '없음'} component가 없습니다"
    )


def select_vowel_component_glyph(parts: HangulSyllableParts) -> str:
    if parts.jongseong:
        glyph = KOREAN_VOWEL_COMPONENTS_WITH_FINAL.get(parts.jungseong)
        if glyph is not None:
            return glyph
    glyph = KOREAN_VOWEL_COMPONENTS_NO_FINAL.get(parts.jungseong)
    if glyph is not None:
        return glyph
    raise ValueError(
        f"중성 {parts.jungseong} "
        f"({'종성 있음' if parts.jongseong else '종성 없음'}) component가 없습니다"
    )


def select_final_component_glyph(parts: HangulSyllableParts, layout: str) -> str:
    if not parts.jongseong:
        raise ValueError("종성이 없는 음절에 종성 component를 요청했습니다")
    if not KOREAN_FINAL_COMPONENT_ROWS:
        raise ValueError("종성 component row가 없습니다")
    for row_index in KOREAN_COMPONENT_FINAL_ROW_PRIORITIES[layout]:
        if row_index >= len(KOREAN_FINAL_COMPONENT_ROWS):
            continue
        glyph = KOREAN_FINAL_COMPONENT_ROWS[row_index].get(parts.jongseong)
        if glyph is not None:
            return glyph
    for row in KOREAN_FINAL_COMPONENT_ROWS:
        glyph = row.get(parts.jongseong)
        if glyph is not None:
            return glyph
    raise ValueError(f"종성 {parts.jongseong} component가 없습니다")


def encode_hangul_component_char(char: str) -> bytes | None:
    if not KOREAN_SOURCE_HAS_MARKS:
        return None
    parts = decompose_hangul_syllable(char)
    if parts is None:
        return None
    layout = hangul_vowel_layout(parts.jungseong)
    initial_glyph = select_initial_component_glyph(parts, layout)
    vowel_glyph = select_vowel_component_glyph(parts)
    encoded = bytearray(
        [
            component_glyph_byte(initial_glyph, label="초성"),
            component_glyph_byte(vowel_glyph, label="중성"),
        ]
    )
    if parts.jongseong:
        final_glyph = select_final_component_glyph(parts, layout)
        encoded.append(component_glyph_byte(final_glyph, label="종성"))
    return bytes(encoded)


def is_elf(data: bytes) -> bool:
    return data.startswith(b"\x7fELF") and len(data) >= 0x34


def scan_regions(path: Path, data: bytes) -> list[Region]:
    if not is_elf(data):
        return [Region("whole", 0, len(data))]

    sections = parse_elf_sections(data)
    regions: list[Region] = []
    for section in sections:
        if section.type_id == 8 or section.size <= 0:
            continue
        if section.name.startswith((".rodata", ".data")):
            regions.append(Region(section.name, section.offset, section.offset + section.size))
    return regions


def decode_cp932(raw: bytes) -> str:
    text: list[str] = []
    cursor = 0
    while cursor < len(raw):
        for sequence, replacement in DISPLAY_SEQUENCE_ALIASES.items():
            if raw.startswith(sequence, cursor):
                text.append(replacement)
                cursor += len(sequence)
                break
        else:
            byte = raw[cursor]
            text.append(
                DISPLAY_BYTE_ALIASES.get(
                    byte,
                    bytes([byte]).decode("cp932", errors="replace"),
                )
            )
            cursor += 1
    return "".join(text)


LEGACY_TEXT_REPLACEMENTS = str.maketrans(DISPLAY_TEXT_ALIASES)


def normalize_legacy_display_text(text: str) -> str:
    return text.replace(":=", "開発").translate(LEGACY_TEXT_REPLACEMENTS)


def small_font_lookup_offset(byte: int) -> int | None:
    index = small_font_lookup_index(byte)
    if index is None or index >= SMALL_FONT_EXPANDED_LOOKUP_ENTRIES:
        return None
    return SMALL_FONT_EXPANDED_LOOKUP_OFFSET + index


def stock_small_font_lookup_offset(byte: int) -> int | None:
    index = stock_small_font_lookup_index(byte)
    if index is None or index >= SMALL_FONT_STOCK_LOOKUP_ENTRIES:
        return None
    return SMALL_FONT_LOOKUP_OFFSET + index


def small_font_tile_glyph(index: int) -> str:
    if 0 <= index < len(SMALL_FONT_TILE_GLYPHS):
        return SMALL_FONT_TILE_GLYPHS[index]
    return ""


def font_byte_label(byte: int) -> str:
    if 0x21 <= byte <= 0x7E:
        return chr(byte)
    if byte == 0x7F:
        return "<7f>"
    if 0x80 <= byte <= 0xA4:
        return f"<{byte:02x}>"
    if 0xE0 <= byte <= 0xFF:
        return f"<{byte:02x}>"
    return bytes([byte]).decode("cp932", errors="replace")


def read_u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def read_u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def patched_lookup_tile_index_for_byte(byte: int) -> int | None:
    mapping = KOREAN_GLYPH_MAPPING_BY_BYTE.get(byte)
    if mapping is not None:
        return mapping.tile_index
    stock_index = stock_small_font_lookup_index(byte)
    if stock_index is None or stock_index >= len(SMALL_FONT_STOCK_LOOKUP_BYTES):
        return 0xFF if 0xE0 <= byte <= 0xFF else None
    return SMALL_FONT_STOCK_LOOKUP_BYTES[stock_index]


def validate_literal_font_byte(char: str, byte: int, *, alias: bool = False) -> None:
    tile_index = patched_lookup_tile_index_for_byte(byte)
    if tile_index not in KOREAN_CUSTOM_TILE_INDICES:
        return
    glyph = KOREAN_CUSTOM_GLYPH_BY_TILE_INDEX.get(tile_index, "?")
    label = "literal alias" if alias else "literal"
    raise ValueError(
        f"{label} {char!r} byte 0x{byte:02x}가 한글 glyph "
        f"{glyph!r} tile {tile_index}와 충돌"
    )


def encode_font_char(char: str) -> bytes:
    try:
        component_raw = encode_hangul_component_char(char)
    except ValueError as exc:
        raise ValueError(f"조합식 한글로 인코딩할 수 없는 문자 {char!r}: {exc}") from exc
    if component_raw is not None:
        return component_raw

    mapped = KOREAN_FONT_BYTE_BY_CHAR.get(char)
    if mapped is not None:
        return bytes([mapped])
    alias_byte = DISPLAY_SINGLE_BYTE_BY_GLYPH.get(char)
    if alias_byte is not None:
        validate_literal_font_byte(char, alias_byte, alias=True)
        return bytes([alias_byte])
    try:
        raw = char.encode("cp932")
    except UnicodeEncodeError as exc:
        raise ValueError(f"작은 폰트 한글 매핑/CP932로 인코딩할 수 없는 문자: {char}") from exc
    for byte in raw:
        validate_literal_font_byte(char, byte)
    bad = [byte for byte in raw if byte not in FONT_BYTES]
    if bad:
        rendered = " ".join(f"0x{byte:02x}" for byte in bad[:8])
        raise ValueError(f"작은 폰트 표에 없는 바이트 포함: {rendered}")
    return raw


def encode_font_text(text: str) -> bytes:
    raw = bytearray()
    cursor = 0
    while cursor < len(text):
        for marker, marker_raw in DISPLAY_TEXT_BYTE_SEQUENCES.items():
            if text.startswith(marker, cursor):
                raw.extend(marker_raw)
                cursor += len(marker)
                break
        else:
            raw.extend(encode_font_char(text[cursor]))
            cursor += 1
    return bytes(raw)


def truncate_font_text_to_bytes(text: str, max_bytes: int) -> str:
    if max_bytes < 0:
        raise ValueError(f"invalid max_bytes: {max_bytes}")
    used = 0
    chars: list[str] = []
    cursor = 0
    while cursor < len(text):
        marker_match: tuple[str, bytes] | None = None
        for marker, marker_raw in DISPLAY_TEXT_BYTE_SEQUENCES.items():
            if text.startswith(marker, cursor):
                marker_match = (marker, marker_raw)
                break
        if marker_match is not None:
            marker, raw = marker_match
            if used + len(raw) > max_bytes:
                break
            chars.append(marker)
            used += len(raw)
            cursor += len(marker)
            continue

        char = text[cursor]
        raw = encode_font_char(char)
        if used + len(raw) > max_bytes:
            break
        chars.append(char)
        used += len(raw)
        cursor += 1
    return "".join(chars)


def iter_candidate_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                sorted(
                    p
                    for p in path.rglob("*")
                    if p.is_file() and p.suffix.lower() in CANDIDATE_SUFFIXES
                )
            )
        else:
            raise FileNotFoundError(path)
    return files


def stride_bytes(width: int, bpp: int) -> int:
    return (width * bpp + 7) // 8


def find_magic_offsets(data: bytes, magic: bytes) -> list[int]:
    offsets: list[int] = []
    cursor = 0
    while True:
        offset = data.find(magic, cursor)
        if offset < 0:
            return offsets
        offsets.append(offset)
        cursor = offset + 1


def fixed_size_resource_ranges(data: bytes, magic: bytes, *, size_offset: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for offset in find_magic_offsets(data, magic):
        if offset + size_offset + 4 > len(data):
            continue
        size = read_u32(data, offset + size_offset)
        if size >= size_offset + 4 and offset + size <= len(data):
            ranges.append((offset, offset + size))
    return ranges


def tx_ranges(data: bytes) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for offset in find_magic_offsets(data, TX_MAGIC):
        if offset + 12 > len(data):
            continue
        size = read_u32(data, offset + 4)
        width = read_u16(data, offset + 8)
        height = read_u16(data, offset + 10)
        if (
            size >= 12
            and offset + size <= len(data)
            and 1 <= width <= 1024
            and 1 <= height <= 1024
            and size >= 12 + stride_bytes(width, 4) * height
        ):
            ranges.append((offset, offset + size))
    return ranges


def pl_ranges(data: bytes) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for offset in find_magic_offsets(data, PL_MAGIC):
        if offset + 12 > len(data):
            continue
        size = read_u32(data, offset + 4)
        color_count = read_u32(data, offset + 8)
        if (
            0 < color_count <= 256
            and size >= 12 + color_count * 2
            and offset + size <= len(data)
        ):
            ranges.append((offset, offset + size))
    return ranges


def resource_exclusion_ranges(data: bytes) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    ranges.extend(tx_ranges(data))
    ranges.extend(pl_ranges(data))
    ranges.extend(fixed_size_resource_ranges(data, CMP0_MAGIC, size_offset=4))
    ranges.extend((offset, len(data)) for offset in find_magic_offsets(data, PNG_MAGIC))
    if not ranges:
        return []

    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            old_start, old_end = merged[-1]
            merged[-1] = (old_start, max(old_end, end))
    return merged


def excluded_range_end(ranges: list[tuple[int, int]], offset: int) -> int | None:
    for start, end in ranges:
        if offset < start:
            return None
        if start <= offset < end:
            return end
    return None


def has_kana_base(raw: bytes) -> bool:
    return any(byte in KANA_BASE_BYTES for byte in raw)


def looks_like_ascii_name_with_middle_dot(raw: bytes) -> bool:
    return (
        0xA5 in raw
        and any(0x30 <= byte <= 0x39 or 0x41 <= byte <= 0x5A or 0x61 <= byte <= 0x7A for byte in raw)
        and all(byte in TEXT_ASCII_BYTES or byte == 0xA5 for byte in raw)
    )


def has_bad_kana_marks(raw: bytes) -> bool:
    for index, byte in enumerate(raw):
        if byte not in KANA_MARK_BYTES:
            continue
        if index == 0 or raw[index - 1] not in KANA_BASE_BYTES:
            return True
    return False


def has_bad_ascii_mix(raw: bytes) -> bool:
    return any(0x20 <= byte < 0x7F and byte not in TEXT_ASCII_BYTES for byte in raw)


def has_lowercase_noise(raw: bytes) -> bool:
    if raw.startswith((b"Ex-", b"Ez-")) or looks_like_ascii_name_with_middle_dot(raw):
        return False
    return any(0x61 <= byte <= 0x7A for byte in raw)


def has_repeated_kana_noise(raw: bytes) -> bool:
    previous = None
    count = 0
    for byte in raw:
        if byte == previous and byte in KANA_BASE_BYTES:
            count += 1
            if count >= 3:
                return True
        else:
            previous = byte
            count = 1
    return False


def trailing_ascii_group(raw: bytes) -> bytes:
    end = len(raw)
    start = end
    while start > 0 and 0x41 <= raw[start - 1] <= 0x5A:
        start -= 1
    return raw[start:end]


def has_bad_archive_ascii_tail(raw: bytes) -> bool:
    compact = raw.strip(b" ")
    suffix = trailing_ascii_group(compact)
    if not suffix:
        return False
    if len(suffix) == len(compact):
        return False
    return suffix not in ARCHIVE_ALLOWED_TRAILING_ASCII


def first_kana_start(raw: bytes) -> int | None:
    for index, byte in enumerate(raw):
        if byte in KANA_BASE_BYTES:
            return index
    return None


def text_candidate_start(raw: bytes, include_ascii: bool) -> int | None:
    first_non_space = 0
    while first_non_space < len(raw) and raw[first_non_space] == 0x20:
        first_non_space += 1

    stripped = raw[first_non_space:]
    if not stripped:
        return None
    if include_ascii and looks_like_text_candidate(raw, include_ascii=True):
        return 0
    if looks_like_text_candidate(raw, include_ascii=False):
        return 0

    kana_index = first_kana_start(raw)
    if kana_index is None:
        return None

    suffix = raw[kana_index:]
    if looks_like_text_candidate(suffix, include_ascii=False):
        if first_non_space == kana_index:
            return first_non_space if first_non_space else 0
        return kana_index
    return None


def looks_like_text_candidate(raw: bytes, include_ascii: bool) -> bool:
    if not raw:
        return False
    if has_bad_ascii_mix(raw) or has_bad_kana_marks(raw):
        return False

    kana_base_count = sum(1 for byte in raw if byte in KANA_BASE_BYTES)
    kana_body_count = sum(1 for byte in raw if byte in KANA_BODY_BYTES or byte in KANA_MARK_BYTES)
    ascii_count = sum(1 for byte in raw if 0x20 <= byte < 0x7F)

    if kana_base_count:
        return ascii_count <= max(6, kana_body_count * 2)
    if looks_like_ascii_name_with_middle_dot(raw):
        return True
    return include_ascii and ascii_count == len(raw)


def looks_like_strong_archive_text(raw: bytes) -> bool:
    compact = raw.strip(b" ")
    if len(compact) < 4:
        return False
    if any(0x20 <= byte < 0x7F and byte in ARCHIVE_BAD_ASCII_BYTES for byte in compact):
        return False
    if has_bad_ascii_mix(compact) or has_bad_kana_marks(compact):
        return False
    if has_lowercase_noise(compact) or has_repeated_kana_noise(compact):
        return False
    if has_bad_archive_ascii_tail(compact):
        return False

    kana_base_count = sum(1 for byte in compact if byte in KANA_BASE_BYTES)
    kanaish_count = sum(1 for byte in compact if byte >= 0xA1)
    ascii_count = sum(1 for byte in compact if 0x20 <= byte < 0x7F)
    if kana_base_count < 3:
        return False
    if kanaish_count < max(3, len(compact) // 2):
        return False
    if ascii_count and kana_base_count < 4 and len(compact) < 6:
        return False
    return True


EBOOT_STRUCTURED_DISPLAY_ALIAS_BYTES = {
    0x21,  # II
    0x22,  # 改
    0x23,  # 型
    0x24,  # III
    0x27,  # ν
    0x28,  # α
    0x29,  # β
    0x2A,  # 三
    0x3A,  # 開
    0x3D,  # 発
    0x5E,  # α
    0x5F,  # β
}


def looks_like_structured_eboot_text(raw: bytes) -> bool:
    compact = raw.strip(b" ")
    if not compact:
        return False
    if any(byte not in FONT_BYTES for byte in compact):
        return False
    if has_bad_kana_marks(compact):
        return False
    if any(byte in KANA_BODY_BYTES or byte in KANA_MARK_BYTES for byte in compact):
        return True
    if b":=" in compact:
        return True
    return any(byte in EBOOT_STRUCTURED_DISPLAY_ALIAS_BYTES for byte in compact)


def fixed_slot_raw(data: bytes, offset: int, slot_size: int) -> bytes | None:
    if offset < 0 or offset + slot_size > len(data):
        return None
    slot = data[offset : offset + slot_size]
    nul = slot.find(b"\0")
    if nul < 0:
        return None
    raw = slot[:nul]
    if not looks_like_structured_eboot_text(raw):
        return None
    return raw


def slot_zero_count(slot: StringSlot) -> int:
    return slot.span - len(slot.raw)


def looks_like_textual_record_start(raw: bytes) -> bool:
    compact = raw.strip(b" ")
    if not compact:
        return False
    return compact[0] in KANA_BASE_BYTES or looks_like_ascii_name_with_middle_dot(compact)


def looks_like_record_slot_text(raw: bytes) -> bool:
    compact = raw.strip(b" ")
    if len(compact) < 3:
        return False
    if has_bad_ascii_mix(compact) or has_bad_kana_marks(compact):
        return False
    if has_lowercase_noise(compact) or has_repeated_kana_noise(compact):
        return False
    if any(0x20 <= byte < 0x7F and byte in ARCHIVE_BAD_ASCII_BYTES for byte in compact):
        return False
    if not looks_like_textual_record_start(compact):
        return False

    kana_base_count = sum(1 for byte in compact if byte in KANA_BASE_BYTES)
    kanaish_count = sum(1 for byte in compact if byte >= 0xA1)
    ascii_count = sum(1 for byte in compact if 0x20 <= byte < 0x7F)
    if kana_base_count < 2 or kanaish_count < 2:
        return False
    if ascii_count and 0x20 <= compact[0] < 0x7F:
        return False
    return True


def has_record_slot_context(slot: StringSlot, data: bytes) -> bool:
    if slot_zero_count(slot) < 2 or slot.max_bytes < 5:
        return False
    if not looks_like_record_slot_text(slot.raw):
        return False
    if slot.offset > 0 and data[slot.offset - 1] != 0:
        return False
    return True


def elf_pointer_target_offsets(data: bytes) -> set[int]:
    if not is_elf(data):
        return set()

    sections = parse_elf_sections(data)
    targets: set[int] = set()
    for section in sections:
        if section.type_id == 8 or section.size <= 0:
            continue
        end = min(section.offset + section.size, len(data) - 3)
        for offset in range(section.offset, end, 4):
            target = va_to_offset(sections, read_u32(data, offset))
            if target is not None:
                targets.add(target)
    return targets


def is_trusted_source_slot(
    slot: StringSlot,
    data: bytes,
    pointer_targets: set[int],
) -> bool:
    if slot.offset in pointer_targets:
        return True
    if slot.region.startswith(".rodata"):
        return True
    if slot.region.startswith(".data") and has_record_slot_context(slot, data):
        return True
    return looks_like_strong_archive_text(slot.raw)


def filter_default_slots(slots: list[StringSlot]) -> list[StringSlot]:
    data_cache: dict[Path, bytes] = {}
    pointer_cache: dict[Path, set[int]] = {}

    def data_for(path: Path) -> bytes:
        if path not in data_cache:
            data_cache[path] = path.read_bytes()
        return data_cache[path]

    def pointer_targets_for(path: Path) -> set[int]:
        if path not in pointer_cache:
            pointer_cache[path] = elf_pointer_target_offsets(data_for(path))
        return pointer_cache[path]

    trusted_texts = {
        slot.text
        for slot in slots
        if slot.region != "whole"
        and is_trusted_source_slot(slot, data_for(slot.path), pointer_targets_for(slot.path))
    }
    filtered: list[StringSlot] = []
    for slot in slots:
        data = data_for(slot.path)
        if (
            (
                slot.region != "whole"
                and is_trusted_source_slot(slot, data, pointer_targets_for(slot.path))
            )
            or slot.text in trusted_texts
            or looks_like_strong_archive_text(slot.raw)
            or (slot.path.suffix.lower() == ".mrg" and has_record_slot_context(slot, data))
        ):
            filtered.append(slot)
    return filtered


def plausible_mips_word(word: int) -> bool:
    if word == 0:
        return False

    opcode = word >> 26
    if opcode == 0:
        return (word & 0x3F) in MIPS_SPECIAL_FUNCTS
    if opcode == 1:
        return ((word >> 16) & 0x1F) in MIPS_REGIMM_RT
    return opcode in MIPS_OPS


def overlaps_mips_instruction_stream(data: bytes, start: int, end: int) -> bool:
    if end - start < 4:
        return False

    for alignment in range(4):
        first = start - ((start - alignment) % 4) - 8
        last = end + 8
        total = 0
        plausible = 0
        overlap_plausible = 0

        for word_start in range(first, last, 4):
            if word_start < 0 or word_start + 4 > len(data):
                continue
            word = int.from_bytes(data[word_start : word_start + 4], "little")
            if word == 0:
                continue
            total += 1
            is_plausible = plausible_mips_word(word)
            if is_plausible:
                plausible += 1
                if word_start < end and word_start + 4 > start:
                    overlap_plausible += 1

        if total >= 4 and plausible / total >= 0.75 and overlap_plausible >= 2:
            return True
    return False


def scan_slots_in_regions(
    path: Path,
    data: bytes,
    regions: list[Region],
    *,
    min_bytes: int,
    max_span: int,
    include_ascii: bool,
    raw_scan: bool,
    excluded_ranges: list[tuple[int, int]] | None = None,
) -> list[StringSlot]:
    slots: list[StringSlot] = []
    seen: set[tuple[int, int]] = set()
    if excluded_ranges is None:
        excluded_ranges = [] if raw_scan else resource_exclusion_ranges(data)

    for region in regions:
        cursor = region.start
        while cursor < region.end:
            excluded_end = excluded_range_end(excluded_ranges, cursor)
            if excluded_end is not None:
                cursor = min(excluded_end, region.end)
                continue

            if data[cursor] not in FONT_BYTES:
                cursor += 1
                continue

            start = cursor
            while cursor < region.end and data[cursor] in FONT_BYTES:
                excluded_end = excluded_range_end(excluded_ranges, cursor)
                if excluded_end is not None:
                    break
                cursor += 1
            raw = data[start:cursor]

            zero_end = cursor
            while zero_end < region.end and data[zero_end] == 0:
                zero_end += 1

            zero_count = zero_end - cursor
            candidate_delta = 0 if raw_scan else text_candidate_start(raw, include_ascii)
            if candidate_delta is None:
                cursor = max(cursor, zero_end, start + 1)
                continue

            candidate_start = start + candidate_delta
            candidate_raw = data[candidate_start:cursor]
            span = zero_end - candidate_start
            previous_ok = (
                candidate_start == region.start
                or candidate_delta > 0
                or data[candidate_start - 1] not in FONT_BYTES
            )
            has_kana = any(byte >= 0xA1 for byte in candidate_raw)
            key = (candidate_start, span)

            if (
                key not in seen
                and previous_ok
                and len(candidate_raw) >= min_bytes
                and zero_count > 0
                and 2 <= span <= max_span
                and (include_ascii or has_kana)
                and (
                    raw_scan
                    or not overlaps_mips_instruction_stream(data, candidate_start, cursor)
                )
            ):
                seen.add(key)
                slots.append(
                    StringSlot(
                        path=path,
                        offset=candidate_start,
                        span=span,
                        max_bytes=span - 1,
                        raw=candidate_raw,
                        text=decode_cp932(candidate_raw),
                        region=region.name,
                    )
                )

            cursor = max(cursor, zero_end, start + 1)

    return slots


def scan_slots(
    path: Path,
    *,
    min_bytes: int,
    max_span: int,
    include_ascii: bool,
    raw_scan: bool,
) -> list[StringSlot]:
    data = path.read_bytes()
    return scan_slots_in_regions(
        path,
        data,
        scan_regions(path, data),
        min_bytes=min_bytes,
        max_span=max_span,
        include_ascii=include_ascii,
        raw_scan=raw_scan,
    )


def add_unique_slot(slots: list[StringSlot], seen: set[int], slot: StringSlot) -> None:
    if slot.offset in seen:
        return
    seen.add(slot.offset)
    slots.append(slot)


def exact_span_from_capacity(data: bytes, offset: int, raw: bytes, capacity: object) -> int:
    raw_end = offset + len(raw)
    if (
        isinstance(capacity, int)
        and capacity >= len(raw)
        and offset + capacity < len(data)
        and data[raw_end] == 0
    ):
        limit = offset + capacity + 1
        cursor = raw_end + 1
        while cursor < limit and data[cursor] == 0:
            cursor += 1
        return cursor - offset
    return len(raw) + 1


def all_relocated_rodata_pointer_runs(elf: Elf32) -> list[list[dict[str, object]]]:
    rodata_names = {".rodata", ".rodata.0001", ".rodata.0002", ".rodata.0003"}
    entries: list[dict[str, object]] = []
    for rel in elf.relocations():
        if rel.target_section != ".data" or rel.r_type != 2:
            continue
        pointed_sec = elf.section_for_vma(rel.value)
        if not pointed_sec or pointed_sec.name not in rodata_names:
            continue
        target_off = elf.vma_to_off(rel.value)
        decoded = decode_eboot_cp932(elf.data, target_off)
        if not decoded:
            continue
        raw, text = decoded
        entries.append(
            {
                "pointer_vma": rel.target_vma,
                "pointer_file_off": rel.target_file_off,
                "reloc_record_off": rel.record_off,
                "raw_pointer_value": rel.value,
                "target_vma": rel.value,
                "target_file_off": target_off,
                "target_section": pointed_sec.name,
                "text_raw": raw,
                "text": text,
                "has_japanese": has_japanese(text),
            }
        )
    entries.sort(key=lambda entry: int(entry["pointer_vma"]))

    runs: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    for entry in entries:
        if current and int(entry["pointer_vma"]) != int(current[-1]["pointer_vma"]) + 4:
            runs.append(current)
            current = []
        current.append(entry)
    if current:
        runs.append(current)
    return runs


def ascii_relocated_display_runs(
    runs: list[list[dict[str, object]]],
) -> list[list[dict[str, object]]]:
    allowed = set(EBOOT_ASCII_RELOCATED_DISPLAY_RUNS)
    return [
        run
        for run in runs
        if tuple(str(entry["text"]) for entry in run) in allowed
    ]


def eboot_relocated_display_table_runs(
    elf: Elf32,
) -> list[tuple[int, str, list[dict[str, object]]]]:
    japanese_runs = runs_from_relocated_rodata_pointers(elf)
    for run in japanese_runs:
        add_capacity(run)
    table_runs = display_runs_from_runs(japanese_runs)

    ascii_runs = ascii_relocated_display_runs(all_relocated_rodata_pointer_runs(elf))
    for run in ascii_runs:
        add_capacity(run)

    numbered: list[tuple[int, str, list[dict[str, object]]]] = []
    for table_id, run in enumerate(table_runs, start=1):
        numbered.append((table_id, "relocated_display_table", run))
    for table_id, run in enumerate(ascii_runs, start=len(numbered) + 1):
        numbered.append((table_id, "relocated_ascii_label_table", run))
    return numbered


def relocated_display_region(target_section: str, table_kind: str, table_id: int) -> str:
    return f"{target_section}:{table_kind}_{table_id}"


def collect_exact_eboot_slots(
    eboot: Path,
    *,
    min_bytes: int,
    max_span: int,
) -> list[StringSlot]:
    elf = Elf32(eboot)
    slots: list[StringSlot] = []
    seen: set[int] = set()

    def add_slot(
        offset: int,
        span: int,
        max_bytes: int,
        raw: bytes,
        region: str,
        *,
        min_len: int | None = None,
    ) -> None:
        effective_min_len = (
            min_len
            if min_len is not None
            else 2 if relocated_display_table_id(region) is not None else min_bytes
        )
        if len(raw) < effective_min_len:
            return
        if any(byte not in FONT_BYTES for byte in raw):
            return
        if offset < 0 or offset + len(raw) >= len(elf.data):
            return
        if elf.data[offset : offset + len(raw)] != raw:
            return
        if elf.data[offset + len(raw)] != 0:
            return
        if span <= len(raw) or offset + span > len(elf.data):
            span = len(raw) + 1
            max_bytes = len(raw)
        add_unique_slot(
            slots,
            seen,
            StringSlot(
                path=eboot,
                offset=offset,
                span=span,
                max_bytes=max_bytes,
                raw=raw,
                text=decode_cp932(raw),
                region=region,
            ),
        )

    for table_id, table_kind, run in eboot_relocated_display_table_runs(elf):
        for entry in run:
            raw = entry["text_raw"]
            span = exact_span_from_capacity(
                elf.data,
                entry["target_file_off"],
                raw,
                entry["max_bytes_before_next_target"],
            )
            add_slot(
                entry["target_file_off"],
                span,
                span - 1,
                raw,
                relocated_display_region(str(entry["target_section"]), table_kind, table_id),
            )

    # Additional .rodata strings whose table bases are built directly by code
    # rather than by relocated .data pointers.
    for table in EBOOT_STRUCTURED_RODATA_TABLES:
        for index in range(table.record_count):
            offset = table.record_start + index * table.record_stride + table.field_offset
            raw = fixed_slot_raw(elf.data, offset, table.slot_size)
            if raw is None:
                continue
            add_slot(
                offset,
                table.slot_size,
                table.slot_size - 1,
                raw,
                table.region,
            )

    for region, offset, slot_size in EBOOT_DIRECT_RODATA_STRINGS:
        raw = fixed_slot_raw(elf.data, offset, slot_size)
        if raw is None:
            continue
        add_slot(
            offset,
            slot_size,
            slot_size - 1,
            raw,
            region,
        )

    # Known .data inline unit short-name table from the relocated-table analysis.
    record_start_vma = 0x00161600
    record_stride = 0x50
    record_count = 579
    field_offset = 0x0C
    span_bytes = 0x0D
    max_text_bytes = 0x0C
    for index in range(record_count):
        field_vma = record_start_vma + index * record_stride + field_offset
        field_off = elf.vma_to_off(field_vma)
        if field_off is None:
            continue
        slot_raw = elf.data[field_off : field_off + span_bytes]
        nul = slot_raw.find(b"\0")
        if nul < 0:
            continue
        raw = slot_raw[:nul]
        add_slot(
            field_off,
            span_bytes,
            max_text_bytes,
            raw,
            "data:data_unit_records_short_name",
            min_len=2 if field_off in EBOOT_DATA_UNIT_SHORT_NAME_MIN2_OFFSETS else None,
        )

    slots.sort(key=lambda slot: slot.offset)
    return slots


def scan_exact_allowed_slots(
    path: Path,
    *,
    allowed_raws: set[bytes],
    min_bytes: int,
    max_span: int,
) -> list[StringSlot]:
    data = path.read_bytes()
    slots: list[StringSlot] = []
    seen: set[int] = set()
    if not allowed_raws:
        return slots
    excluded_ranges = resource_exclusion_ranges(data)

    for region in scan_regions(path, data):
        cursor = region.start
        while cursor < region.end:
            excluded_end = excluded_range_end(excluded_ranges, cursor)
            if excluded_end is not None:
                cursor = min(excluded_end, region.end)
                continue

            if data[cursor] not in FONT_BYTES:
                cursor += 1
                continue

            start = cursor
            while cursor < region.end and data[cursor] in FONT_BYTES:
                excluded_end = excluded_range_end(excluded_ranges, cursor)
                if excluded_end is not None:
                    break
                cursor += 1
            raw = data[start:cursor]

            zero_end = cursor
            while zero_end < region.end and data[zero_end] == 0:
                zero_end += 1

            zero_span = zero_end - start
            span = min(zero_span, max_span)
            previous_ok = start == region.start or data[start - 1] not in FONT_BYTES
            if (
                len(raw) >= min(min_bytes, 2)
                and raw in allowed_raws
                and cursor < region.end
                and data[cursor] == 0
                and previous_ok
                and zero_span >= len(raw) + 1
                and 2 <= span <= max_span
            ):
                add_unique_slot(
                    slots,
                    seen,
                    StringSlot(
                        path=path,
                        offset=start,
                        span=span,
                        max_bytes=span - 1,
                        raw=raw,
                        text=decode_cp932(raw),
                        region=region.name,
                    ),
                )

            cursor = max(cursor, zero_end, start + 1)

    return slots


def mrg_child_record_regions(data: bytes, *, base: int = 0, prefix: str = "mrg") -> list[Region]:
    if base + 12 > len(data) or data[base : base + 4] != MRG_MAGIC:
        return []

    total = read_u32(data, base + 4)
    count = read_u32(data, base + 8)
    if (
        total < 12
        or base + total > len(data)
        or count <= 0
        or count > 4096
        or base + 12 + count * 4 > base + total
    ):
        return []

    table_end = base + 12 + count * 4
    starts = [base + read_u32(data, base + 12 + index * 4) for index in range(count)]
    if starts != sorted(set(starts)) or starts[0] < table_end or starts[-1] >= base + total:
        return []

    regions: list[Region] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else base + total
        if end <= start:
            continue
        child_prefix = f"{prefix}_child_{index}"
        if data[start : start + 4] == MRG_MAGIC:
            regions.extend(mrg_child_record_regions(data, base=start, prefix=child_prefix))
            continue
        if data[start : start + 8].startswith(ARCHIVE_TABLE_SKIP_MAGICS):
            continue
        regions.append(Region(child_prefix, start, end))
    return regions


def looks_like_archive_record_text(raw: bytes) -> bool:
    compact = raw.strip(b" ")
    if len(compact) < 3:
        return False
    if has_bad_ascii_mix(compact) or has_bad_kana_marks(compact):
        return False
    if has_lowercase_noise(compact) or has_repeated_kana_noise(compact):
        return False
    if any(0x20 <= byte < 0x7F and byte in ARCHIVE_BAD_ASCII_BYTES for byte in compact):
        return False

    kana_base_count = sum(1 for byte in compact if byte in KANA_BASE_BYTES)
    kanaish_count = sum(1 for byte in compact if byte >= 0xA1)
    return kana_base_count >= 2 and kanaish_count >= 2


def archive_record_run_is_valid(slots: list[StringSlot], stride: int) -> bool:
    if len(slots) < 4 or not (8 <= stride <= 0x200):
        return False
    span_counts: dict[int, int] = {}
    for slot in slots:
        span_counts[slot.span] = span_counts.get(slot.span, 0) + 1
    common_span_count = max(span_counts.values())
    if common_span_count * 5 < len(slots) * 4:
        return False

    kanaish_slots = sum(1 for slot in slots if any(byte >= 0xA1 for byte in slot.raw))
    return kanaish_slots == len(slots)


def collect_archive_table_slots(
    path: Path,
    *,
    min_bytes: int,
    max_span: int,
) -> list[StringSlot]:
    data = path.read_bytes()
    regions = mrg_child_record_regions(data)
    if not regions:
        return []

    candidates = scan_slots_in_regions(
        path,
        data,
        regions,
        min_bytes=min_bytes,
        max_span=max_span,
        include_ascii=False,
        raw_scan=True,
        excluded_ranges=resource_exclusion_ranges(data),
    )
    candidates = [
        slot
        for slot in candidates
        if looks_like_archive_record_text(slot.raw)
    ]

    slots: list[StringSlot] = []
    seen: set[int] = set()
    by_region: dict[str, list[StringSlot]] = {}
    for slot in candidates:
        by_region.setdefault(slot.region, []).append(slot)

    for region, region_slots in by_region.items():
        ordered = sorted(region_slots, key=lambda slot: slot.offset)
        index = 0
        while index + 1 < len(ordered):
            stride = ordered[index + 1].offset - ordered[index].offset
            if not (8 <= stride <= 0x200):
                index += 1
                continue

            end_index = index + 1
            while (
                end_index + 1 < len(ordered)
                and ordered[end_index + 1].offset - ordered[end_index].offset == stride
            ):
                end_index += 1

            run = ordered[index : end_index + 1]
            if archive_record_run_is_valid(run, stride):
                for slot in run:
                    add_unique_slot(
                        slots,
                        seen,
                        StringSlot(
                            path=slot.path,
                            offset=slot.offset,
                            span=slot.span,
                            max_bytes=slot.max_bytes,
                            raw=slot.raw,
                            text=slot.text,
                            region=f"{region}:record_stride_0x{stride:x}",
                        ),
                    )
            index = max(index + 1, end_index)

    slots.sort(key=lambda slot: slot.offset)
    return slots


def dedupe_slots(slots: list[StringSlot]) -> list[StringSlot]:
    out: list[StringSlot] = []
    seen: set[tuple[Path, int]] = set()
    for slot in slots:
        key = (slot.path, slot.offset)
        if key in seen:
            continue
        seen.add(key)
        out.append(slot)
    return out


def same_path(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


def dump_csv(args: argparse.Namespace) -> None:
    paths = [Path(value) for value in args.paths] if args.paths else DEFAULT_PATHS
    files = iter_candidate_files(paths)
    slots: list[StringSlot] = []
    if args.strict_filter and not args.raw:
        raise SystemExit("--strict-filter는 --raw 조사 모드에서만 사용할 수 있습니다")
    if args.raw:
        for path in files:
            slots.extend(
                scan_slots(
                    path,
                    min_bytes=args.min_bytes,
                    max_span=args.max_span,
                    include_ascii=args.include_ascii,
                    raw_scan=True,
                )
            )
        if args.strict_filter:
            slots = filter_default_slots(slots)
    else:
        eboot_slots = collect_exact_eboot_slots(
            args.eboot,
            min_bytes=args.min_bytes,
            max_span=args.max_span,
        )
        allowed_raws = {slot.raw for slot in eboot_slots}
        for path in files:
            if same_path(path, args.eboot):
                slots.extend(eboot_slots)
            else:
                archive_slots = [] if args.no_archive_tables else collect_archive_table_slots(
                    path,
                    min_bytes=args.min_bytes,
                    max_span=args.max_span,
                )
                exact_slots = scan_exact_allowed_slots(
                    path,
                    allowed_raws=allowed_raws,
                    min_bytes=args.min_bytes,
                    max_span=args.max_span,
                )
                slots.extend(dedupe_slots(archive_slots + exact_slots))

    slots.sort(key=lambda slot: (slot.path.as_posix(), slot.offset, slot.region))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "path",
                "region",
                "offset_hex",
                "span",
                "max_bytes",
                "original",
                "original_hex",
                "translation",
                "sha1",
            ],
        )
        writer.writeheader()
        for slot in slots:
            writer.writerow(
                {
                    "path": slot.path.as_posix(),
                    "region": slot.region,
                    "offset_hex": f"0x{slot.offset:x}",
                    "span": slot.span,
                    "max_bytes": slot.max_bytes,
                    "original": slot.text,
                    "original_hex": slot.raw.hex(),
                    "translation": "",
                    "sha1": hashlib.sha1(slot.raw).hexdigest()[:12],
                }
            )
    print(f"wrote {len(slots)} slots to {args.output}")


def dictionary_csv(args: argparse.Namespace) -> None:
    previous_translations: dict[str, str] = {}
    if args.keep_translations and args.output.exists():
        with args.output.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                translation = (row.get("translation") or "").strip()
                if translation:
                    original = row["original"]
                    previous_translations[original] = translation
                    previous_translations.setdefault(
                        normalize_legacy_display_text(original), translation
                    )

    groups: dict[str, dict[str, object]] = {}
    with args.slots.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            original = row["original"]
            group = groups.setdefault(
                original,
                {
                    "original": original,
                    "count": 0,
                    "min_max_bytes": int(row["max_bytes"]),
                    "max_max_bytes": int(row["max_bytes"]),
                    "relayout_offset_table_count": 0,
                    "samples": [],
                    "translation": previous_translations.get(original, ""),
                },
            )
            group["count"] = int(group["count"]) + 1
            group["min_max_bytes"] = min(int(group["min_max_bytes"]), int(row["max_bytes"]))
            group["max_max_bytes"] = max(int(group["max_max_bytes"]), int(row["max_bytes"]))
            if relocated_display_table_id(row.get("region", "")) is not None:
                group["relayout_offset_table_count"] = (
                    int(group["relayout_offset_table_count"]) + 1
                )
            samples = group["samples"]
            assert isinstance(samples, list)
            if len(samples) < args.samples:
                samples.append(f"{row['path']}@{row['offset_hex']}")
    dropped_translations: list[str] = []
    for group in groups.values():
        translation = str(group["translation"]).strip()
        if not translation:
            continue
        original = str(group["original"])
        try:
            encode_font_text(translation)
        except ValueError as exc:
            group["translation"] = ""
            dropped_translations.append(f"{original} -> {translation}: {exc}")

    rows = sorted(groups.values(), key=lambda item: (str(item["original"])))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "original",
                "translation",
                "max_max_bytes",
                "count",
                "min_max_bytes",
                "relayout_offset_table_count",
                "samples",
            ],
        )
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["samples"] = " | ".join(out["samples"])
            writer.writerow(out)
    print(f"wrote {len(rows)} unique strings to {args.output}")
    if dropped_translations:
        print(
            f"cleared {len(dropped_translations)} translations that cannot be encoded",
            file=sys.stderr,
        )
        for item in dropped_translations[:20]:
            print(f"  {item}", file=sys.stderr)


def fill_csv(args: argparse.Namespace) -> None:
    with args.slots.open("r", encoding="utf-8", newline="") as source:
        slots_reader = csv.DictReader(source)
        if slots_reader.fieldnames is None:
            raise SystemExit("slots CSV에 헤더가 없습니다")
        slot_fieldnames = slots_reader.fieldnames
        slot_rows = list(slots_reader)

    translations: dict[str, str] = {}
    with args.dictionary.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required_fields = {"original", "translation"}
        if reader.fieldnames is None:
            raise SystemExit("dictionary CSV에 헤더가 없습니다")
        missing_fields = required_fields - set(reader.fieldnames)
        if missing_fields:
            raise SystemExit(
                "dictionary CSV에 필요한 컬럼이 없습니다: "
                + ", ".join(sorted(missing_fields))
            )
        errors: list[str] = []
        for row_index, row in enumerate(reader, start=2):
            translation = (row.get("translation") or "").strip()
            if translation:
                original = row["original"]
                try:
                    encode_font_text(translation)
                except ValueError as exc:
                    errors.append(f"dictionary row {row_index} {original} {translation}: {exc}")
                    continue
                translations[row["original"]] = translation
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            raise SystemExit(f"{len(errors)}개 dictionary 오류로 fill 중단")

    filled = 0
    oversized = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=slot_fieldnames)
        writer.writeheader()
        for row in slot_rows:
            if row["original"] in translations and not (row.get("translation") or "").strip():
                translation = translations[row["original"]]
                if len(encode_font_text(translation)) > int(row["max_bytes"]):
                    oversized += 1
                row["translation"] = translation
                filled += 1
            writer.writerow(row)
    print(f"filled {filled} slot translations in {args.output}")
    if oversized:
        print(
            f"preserved {oversized} oversized translations without truncation; "
            "apply them with --relocated-external-pool"
        )


def relocated_display_table_id(region: str) -> int | None:
    for marker in (":relocated_display_table_", ":relocated_ascii_label_table_"):
        if marker not in region:
            continue
        suffix = region.rsplit(marker, 1)[1]
        if not suffix.isdigit():
            return None
        return int(suffix)
    return None


def row_target_path(row: ApplyRow, args: argparse.Namespace) -> Path:
    return row.source_path if args.in_place else args.out_root / row.source_path


def prepare_target_bytes(source_path: Path, target_path: Path, args: argparse.Namespace) -> bytes:
    if args.in_place:
        return target_path.read_bytes()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)
    return target_path.read_bytes()


def collect_apply_rows(
    args: argparse.Namespace,
) -> tuple[list[ApplyRow], dict[Path, dict[int, ApplyRow]], list[ApplyRow]]:
    normal_rows: list[ApplyRow] = []
    relayout_rows_by_path: dict[Path, dict[int, ApplyRow]] = {}
    indirect_rows: list[ApplyRow] = []
    errors: list[str] = []

    with args.csv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SystemExit("slots CSV에 헤더가 없습니다")
        for row_index, row in enumerate(reader, start=2):
            translation = (row.get("translation") or "").strip()
            if not translation:
                continue

            try:
                source_path = Path(row["path"])
                offset = int(row["offset_hex"], 16)
                span = int(row["span"])
                max_bytes = int(row["max_bytes"])
                original_hex = row.get("original_hex", "")
                region = row.get("region", "")
                encoded = encode_font_text(translation)
            except (KeyError, ValueError) as exc:
                errors.append(f"row {row_index}: CSV 행을 해석할 수 없습니다: {exc}")
                continue

            apply_row = ApplyRow(
                row_index=row_index,
                source_path=source_path,
                offset=offset,
                span=span,
                max_bytes=max_bytes,
                original_hex=original_hex,
                translation=translation,
                encoded=encoded,
                region=region,
            )
            if args.relayout_offset_tables and relocated_display_table_id(region) is not None:
                by_offset = relayout_rows_by_path.setdefault(source_path, {})
                previous = by_offset.get(offset)
                if previous is not None and previous.encoded != encoded:
                    errors.append(
                        f"row {row_index} {source_path}@0x{offset:x}: "
                        f"같은 offset에 서로 다른 번역이 있습니다"
                    )
                else:
                    by_offset[offset] = apply_row
                continue

            if (
                len(apply_row.encoded) > max_bytes
                and args.relocated_external_pool
                and relocated_display_table_id(region) is None
            ):
                if max_bytes < INDIRECT_STRING_MARKER_SIZE:
                    errors.append(
                        f"row {row_index} {source_path}: {len(apply_row.encoded)}바이트라 "
                        f"슬롯 한계 {max_bytes}바이트를 초과하고 "
                        f"{INDIRECT_STRING_MARKER_SIZE}바이트 외부 참조 표식도 들어가지 않습니다"
                    )
                else:
                    indirect_rows.append(apply_row)
                continue

            if len(apply_row.encoded) > max_bytes and args.force_apply:
                apply_row = replace(apply_row, encoded=apply_row.encoded[:max_bytes])

            if len(apply_row.encoded) > max_bytes:
                hint = (
                    "; EBOOT relocated display 행은 기본 apply에서 확장 적용됨"
                    if relocated_display_table_id(region) is not None
                    else ""
                )
                errors.append(
                    f"row {row_index} {source_path}: "
                    f"{len(apply_row.encoded)}바이트라 슬롯 한계 {max_bytes}바이트를 초과{hint}"
                )
                continue
            normal_rows.append(apply_row)

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        raise SystemExit(f"{len(errors)}개 CSV 오류로 패치 중단")
    return normal_rows, relayout_rows_by_path, indirect_rows


def collect_relocated_display_targets(eboot: Path) -> tuple[Elf32, list[RelocatedDisplayTarget]]:
    elf = Elf32(eboot)

    mutable: dict[int, dict[str, object]] = {}
    for table_id, _table_kind, run in eboot_relocated_display_table_runs(elf):
        for entry in run:
            offset = int(entry["target_file_off"])
            raw = bytes(entry["text_raw"])
            span = exact_span_from_capacity(
                elf.data,
                offset,
                raw,
                entry["max_bytes_before_next_target"],
            )
            target = mutable.setdefault(
                offset,
                {
                    "vma": int(entry["target_vma"]),
                    "span": span,
                    "raw": raw,
                    "text": decode_cp932(raw),
                    "table_ids": set(),
                    "pointer_file_offsets": set(),
                },
            )
            if target["raw"] != raw:
                raise ValueError(
                    f"{eboot}: relocated display target 0x{offset:x} has conflicting bytes"
                )
            target["span"] = max(int(target["span"]), span)
            table_ids = target["table_ids"]
            pointer_offsets = target["pointer_file_offsets"]
            assert isinstance(table_ids, set)
            assert isinstance(pointer_offsets, set)
            table_ids.add(table_id)
            pointer_offsets.add(int(entry["pointer_file_off"]))

    target_vmas = {int(item["vma"]) for item in mutable.values()}
    for rel in elf.relocations():
        if rel.target_section != ".data" or rel.r_type != 2 or rel.value not in target_vmas:
            continue
        target_off = elf.vma_to_off(rel.value)
        if target_off not in mutable:
            continue
        pointer_offsets = mutable[target_off]["pointer_file_offsets"]
        assert isinstance(pointer_offsets, set)
        pointer_offsets.add(rel.target_file_off)

    targets: list[RelocatedDisplayTarget] = []
    for offset, item in sorted(mutable.items()):
        table_ids = item["table_ids"]
        pointer_offsets = item["pointer_file_offsets"]
        assert isinstance(table_ids, set)
        assert isinstance(pointer_offsets, set)
        targets.append(
            RelocatedDisplayTarget(
                offset=offset,
                vma=int(item["vma"]),
                span=int(item["span"]),
                raw=bytes(item["raw"]),
                text=str(item["text"]),
                table_ids=tuple(sorted(int(value) for value in table_ids)),
                pointer_file_offsets=tuple(sorted(int(value) for value in pointer_offsets)),
            )
        )
    return elf, targets


def relocated_display_clusters(
    data: bytes | bytearray,
    targets: list[RelocatedDisplayTarget],
) -> list[list[RelocatedDisplayTarget]]:
    if not targets:
        return []

    ordered = sorted(targets, key=lambda target: target.offset)
    clusters: list[list[RelocatedDisplayTarget]] = []
    current: list[RelocatedDisplayTarget] = [ordered[0]]
    current_end = ordered[0].offset + ordered[0].span
    for target in ordered[1:]:
        if target.offset > current_end and any(data[current_end : target.offset]):
            clusters.append(current)
            current = [target]
        else:
            current.append(target)
        current_end = max(current_end, target.offset + target.span)
    clusters.append(current)
    return clusters


RELOCATED_OVERFLOW_DETAIL_LIMIT = 40


def one_line_text(text: str, *, limit: int = 96) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def format_relocated_overflow_error(
    source_path: Path,
    *,
    table_ids: list[int],
    start: int,
    end: int,
    capacity: int,
    required: int,
    payloads: list[tuple[RelocatedDisplayTarget, bytes]],
    rows_by_offset: dict[int, ApplyRow],
) -> str:
    original_required = sum(len(target.raw) + 1 for target, _payload in payloads)
    overflow = required - capacity
    delta_total = required - original_required
    translated_items: list[tuple[int, RelocatedDisplayTarget, ApplyRow]] = []
    unchanged_count = 0
    for order, (target, _payload) in enumerate(payloads):
        row = rows_by_offset.get(target.offset)
        if row is None:
            unchanged_count += 1
            continue
        translated_items.append((order, target, row))

    growth_items = sorted(
        translated_items,
        key=lambda item: (
            len(item[2].encoded) - len(item[1].raw),
            len(item[2].encoded),
        ),
        reverse=True,
    )
    positive_growth = [
        item for item in growth_items if len(item[2].encoded) > len(item[1].raw)
    ]
    neutral_or_shrink = len(growth_items) - len(positive_growth)
    shown = positive_growth[:RELOCATED_OVERFLOW_DETAIL_LIMIT]

    lines = [
        f"{source_path}: relocated display 문자열 풀 용량 초과",
        (
            f"  tables={table_ids} cluster=0x{start:x}-0x{end:x} "
            f"capacity={capacity}B required={required}B overflow={overflow}B"
        ),
        (
            f"  원문 기준 필요량={original_required}B, 번역 후 증가량={delta_total:+d}B, "
            f"번역 행={len(translated_items)}개, 원문 유지 행={unchanged_count}개"
        ),
        (
            f"  최소 {overflow}B 이상 줄여야 합니다. 아래는 원문보다 길어진 번역 "
            "중 증가량이 큰 순서입니다."
        ),
    ]
    if not shown:
        lines.append(
            "  원문보다 길어진 번역 행을 찾지 못했습니다. 클러스터 span/원본 분석을 확인하세요."
        )
        return "\n".join(lines)

    for _order, target, row in shown:
        original_len = len(target.raw)
        encoded_len = len(row.encoded)
        delta = encoded_len - original_len
        lines.append(
            "  "
            f"row {row.row_index} @0x{target.offset:x} "
            f"{original_len}B->{encoded_len}B ({delta:+d}B): "
            f"{one_line_text(target.text)} => {one_line_text(row.translation)}"
        )
    omitted_positive = len(positive_growth) - len(shown)
    if omitted_positive:
        lines.append(
            f"  ... 원문보다 길어진 행 {omitted_positive}개 더 있음 "
            f"(상위 {RELOCATED_OVERFLOW_DETAIL_LIMIT}개만 표시)"
        )
    if neutral_or_shrink:
        lines.append(f"  원문보다 같거나 짧은 번역 행 {neutral_or_shrink}개는 생략")
    return "\n".join(lines)


def force_truncate_relocated_payloads(
    rows_by_offset: dict[int, ApplyRow],
    payloads: list[tuple[RelocatedDisplayTarget, bytes]],
    *,
    capacity: int,
) -> int:
    required = sum(len(payload) + 1 for _target, payload in payloads)
    overflow = required - capacity
    if overflow <= 0:
        return 0

    candidates: list[tuple[int, int, int]] = []
    encoded_by_offset: dict[int, bytearray] = {}
    for order, (target, _payload) in enumerate(payloads):
        row = rows_by_offset.get(target.offset)
        if row is None or not row.encoded:
            continue
        growth = len(row.encoded) - len(target.raw)
        candidates.append((target.offset, growth, order))
        encoded_by_offset[target.offset] = bytearray(row.encoded)

    candidates.sort(
        key=lambda item: (item[1], len(encoded_by_offset[item[0]]), -item[2]),
        reverse=True,
    )
    if not candidates:
        raise ValueError(
            "force-apply 대상 번역 행이 없어 relocated display overflow를 줄일 수 없습니다"
        )

    remaining = overflow
    while remaining > 0:
        reduced_this_cycle = False
        for offset, _growth, _order in candidates:
            encoded = encoded_by_offset[offset]
            if not encoded:
                continue
            encoded.pop()
            remaining -= 1
            reduced_this_cycle = True
            if remaining == 0:
                break
        if not reduced_this_cycle:
            raise ValueError(
                f"force-apply로 relocated display overflow {overflow}B를 줄일 수 없습니다"
            )

    for offset, encoded in encoded_by_offset.items():
        row = rows_by_offset[offset]
        rows_by_offset[offset] = replace(row, encoded=bytes(encoded))
    return overflow


class RelocatedExternalPoolAllocator:
    def __init__(self, data: bytes | bytearray):
        self.ranges: list[list[int]] = []
        for start, end in RELOCATED_EXTERNAL_POOL_RANGES:
            if end > len(data):
                raise ValueError(
                    f"relocated external pool range 0x{start:x}-0x{end:x} exceeds file size"
                )
            if any(data[start:end]):
                raise ValueError(
                    f"relocated external pool range 0x{start:x}-0x{end:x} is not zero-filled"
                )
            usable_start = start + RELOCATED_EXTERNAL_POOL_MARGIN
            usable_end = end - RELOCATED_EXTERNAL_POOL_MARGIN
            if usable_start < usable_end:
                self.ranges.append([usable_start, usable_end, usable_start])
        if not self.ranges:
            raise ValueError("relocated external pool has no usable zero-filled ranges")

    def allocate(self, payload: bytes, *, alignment: int = 1) -> int:
        if alignment <= 0 or alignment & (alignment - 1):
            raise ValueError(f"external pool alignment must be a power of two: {alignment}")
        for item in self.ranges:
            _start, end, cursor = item
            aligned = (cursor + alignment - 1) & -alignment
            if aligned + len(payload) <= end:
                item[2] = aligned + len(payload)
                return aligned
        needed = len(payload)
        raise ValueError(f"relocated external pool has no room for {needed}B payload")


def mips_i(op: int, rs: int, rt: int, immediate: int) -> int:
    return (
        ((op & 0x3F) << 26)
        | ((rs & 0x1F) << 21)
        | ((rt & 0x1F) << 16)
        | (immediate & 0xFFFF)
    )


def mips_r(rs: int, rt: int, rd: int, shamt: int, funct: int) -> int:
    return (
        ((rs & 0x1F) << 21)
        | ((rt & 0x1F) << 16)
        | ((rd & 0x1F) << 11)
        | ((shamt & 0x1F) << 6)
        | (funct & 0x3F)
    )


def mips_j(runtime_address: int) -> int:
    if runtime_address & 3:
        raise ValueError(f"MIPS jump target is not word-aligned: 0x{runtime_address:x}")
    return (2 << 26) | ((runtime_address >> 2) & 0x03FFFFFF)


def runtime_address_for_eboot_offset(elf: Elf32, offset: int) -> int:
    vma = elf.off_to_vma(offset)
    if vma is None:
        raise ValueError(f"EBOOT file offset 0x{offset:x} is not in a mapped segment")
    return PSP_EBOOT_LOAD_BASE + vma


def indirect_string_stub_words(
    kind: str,
    *,
    entry_runtime: int,
    pool_runtime: int,
) -> tuple[int, ...]:
    # Registers used before the original prologue are limited to v0/v1/a0.
    # render-r2 receives additional arguments in t0-t2, so those must remain intact.
    words = [
        mips_i(0x24, 4, 2, 0),  # lbu v0,0(a0)
        mips_i(0x09, 0, 3, INDIRECT_STRING_MARKER),  # li v1,marker
        mips_i(0x05, 2, 3, 8),  # bne v0,v1,normal
        0,
        mips_i(0x24, 4, 2, 1),  # lbu v0,1(a0)
        mips_i(0x24, 4, 3, 2),  # lbu v1,2(a0)
        mips_r(0, 3, 3, 8, 0x00),  # sll v1,v1,8
        mips_r(2, 3, 2, 0, 0x25),  # or v0,v0,v1
        mips_i(0x0F, 0, 3, pool_runtime >> 16),  # lui v1,pool_hi
        mips_i(0x0D, 3, 3, pool_runtime),  # ori v1,v1,pool_lo
        mips_r(3, 2, 4, 0, 0x21),  # addu a0,v1,v0
    ]
    if kind == "width":
        words.extend(
            (
                mips_i(0x24, 4, 5, 0),  # lbu a1,0(a0)
                mips_i(0x05, 5, 0, 3),  # bnez a1,nonzero
                mips_r(0, 0, 6, 0, 0x21),  # move a2,zero (original delay slot)
                mips_j(entry_runtime + 0x30),  # original zero-string return path
                0,
                mips_j(entry_runtime + 0x0C),  # continue after original delay slot
                0,
            )
        )
    elif kind == "render-r1":
        words.extend((0x27BDFFD0, 0xAFBE0020, mips_j(entry_runtime + 8), 0))
    elif kind == "render-r2":
        words.extend((0x311E00FF, 0xAFB7002C, mips_j(entry_runtime + 8), 0))
    else:
        raise ValueError(f"unknown indirect string entry kind: {kind}")
    return tuple(words)


def words_to_little_endian(words: tuple[int, ...]) -> bytes:
    return b"".join(word.to_bytes(4, "little") for word in words)


def install_indirect_string_pool(
    eboot_path: Path,
    data: bytearray,
    allocator: RelocatedExternalPoolAllocator,
    rows: list[ApplyRow],
) -> tuple[list[ApplyRow], IndirectStringPoolStats]:
    elf = Elf32(eboot_path)
    relocation_offsets = {rel.target_file_off for rel in elf.relocations()}
    entry_offsets = {
        offset
        for _kind, entry_offset, _expected_words in INDIRECT_STRING_ENTRY_PATCHES
        for offset in (entry_offset, entry_offset + 4)
    }
    relocated_entries = sorted(entry_offsets & relocation_offsets)
    if relocated_entries:
        raise ValueError(
            "indirect string entry patch overlaps ELF relocations: "
            + ", ".join(f"0x{offset:x}" for offset in relocated_entries)
        )
    relocated_pool_offsets = sorted(
        offset
        for offset in relocation_offsets
        if any(start <= offset < end for start, end in RELOCATED_EXTERNAL_POOL_RANGES)
    )
    if relocated_pool_offsets:
        raise ValueError(
            "indirect string pool overlaps ELF relocations: "
            + ", ".join(f"0x{offset:x}" for offset in relocated_pool_offsets[:20])
        )
    pool_runtime = runtime_address_for_eboot_offset(elf, INDIRECT_STRING_POOL_BASE_OFFSET)
    stub_bytes = 0

    for kind, entry_offset, expected_words in INDIRECT_STRING_ENTRY_PATCHES:
        current_words = tuple(
            int.from_bytes(data[offset : offset + 4], "little")
            for offset in (entry_offset, entry_offset + 4)
        )
        if current_words != expected_words:
            raise ValueError(
                f"{eboot_path}@0x{entry_offset:x}: indirect string entry bytes differ "
                f"(current {[hex(word) for word in current_words]}, "
                f"expected {[hex(word) for word in expected_words]})"
            )
        entry_runtime = runtime_address_for_eboot_offset(elf, entry_offset)
        stub_words = indirect_string_stub_words(
            kind,
            entry_runtime=entry_runtime,
            pool_runtime=pool_runtime,
        )
        stub = words_to_little_endian(stub_words)
        stub_offset = allocator.allocate(stub, alignment=4)
        stub_runtime = runtime_address_for_eboot_offset(elf, stub_offset)
        data[stub_offset : stub_offset + len(stub)] = stub
        data[entry_offset : entry_offset + 8] = words_to_little_endian(
            (mips_j(stub_runtime), 0)
        )
        stub_bytes += len(stub)

    payload_offsets: dict[bytes, int] = {}
    indirect_rows: list[ApplyRow] = []
    payload_bytes = 0
    for row in rows:
        payload_offset = payload_offsets.get(row.encoded)
        if payload_offset is None:
            payload = row.encoded + b"\0"
            payload_offset = allocator.allocate(payload)
            relative = payload_offset - INDIRECT_STRING_POOL_BASE_OFFSET
            if not 0 <= relative <= 0xFFFF:
                raise ValueError(
                    f"indirect string payload 0x{payload_offset:x} is outside the "
                    "16-bit marker range"
                )
            data[payload_offset : payload_offset + len(payload)] = payload
            payload_offsets[row.encoded] = payload_offset
            payload_bytes += len(payload)
        relative = payload_offset - INDIRECT_STRING_POOL_BASE_OFFSET
        marker = bytes(
            (INDIRECT_STRING_MARKER, relative & 0xFF, (relative >> 8) & 0xFF)
        )
        indirect_rows.append(replace(row, encoded=marker))

    return indirect_rows, IndirectStringPoolStats(
        rows=len(rows),
        unique_payloads=len(payload_offsets),
        payload_bytes=payload_bytes,
        stub_bytes=stub_bytes,
    )


def choose_relocated_external_payloads(
    payloads: list[tuple[RelocatedDisplayTarget, bytes]],
    rows_by_offset: dict[int, ApplyRow],
    *,
    overflow: int,
) -> tuple[tuple[RelocatedDisplayTarget, bytes], ...]:
    candidates = tuple(
        (len(payload) + 1, order, target, payload)
        for order, (target, payload) in enumerate(payloads)
        if target.offset in rows_by_offset
    )
    if not candidates:
        raise ValueError("external pool로 옮길 relocated display 번역 행이 없습니다")

    dp: dict[int, tuple[tuple[int, int, RelocatedDisplayTarget, bytes], ...]] = {0: ()}
    for item in candidates:
        weight = item[0]
        for total, subset in list(dp.items()):
            next_total = total + weight
            next_subset = subset + (item,)
            if next_total not in dp or len(next_subset) < len(dp[next_total]):
                dp[next_total] = next_subset

    possible = [total for total in dp if total >= overflow]
    if not possible:
        raise ValueError(
            f"external pool 대상 행 전체로도 relocated display overflow {overflow}B를 "
            "줄일 수 없습니다"
        )
    best_total = min(possible, key=lambda total: (total, len(dp[total])))
    return tuple((target, payload) for _weight, _order, target, payload in dp[best_total])


def apply_relocated_display_relayout(
    source_path: Path,
    data: bytearray,
    rows_by_offset: dict[int, ApplyRow],
    *,
    force_apply: bool = False,
    external_pool: bool = False,
    external_allocator: RelocatedExternalPoolAllocator | None = None,
) -> RelocatedDisplayRelayoutStats:
    try:
        elf, targets = collect_relocated_display_targets(source_path)
    except Exception as exc:
        raise ValueError(f"{source_path}: relocated display table 분석 실패: {exc}") from exc

    targets_by_offset = {target.offset: target for target in targets}
    errors: list[str] = []
    for offset, row in rows_by_offset.items():
        target = targets_by_offset.get(offset)
        if target is None:
            errors.append(
                f"row {row.row_index} {source_path}@0x{offset:x}: "
                "relocated display table 대상 offset을 찾지 못했습니다"
            )
            continue
        if row.original_hex:
            original = bytes.fromhex(row.original_hex)
            current = bytes(data[offset : offset + len(original)])
            if current != original:
                errors.append(
                    f"row {row.row_index} {source_path}: 원본 바이트가 CSV와 다릅니다 "
                    f"(현재 {current.hex()}, CSV {row.original_hex})"
                )
        if row.original_hex and bytes.fromhex(row.original_hex) != target.raw:
            errors.append(
                f"row {row.row_index} {source_path}@0x{offset:x}: "
                "CSV original_hex와 분석된 relocated target byte가 다릅니다"
            )

    clusters_rewritten = 0
    pointer_rewrites = 0
    forced_truncated_bytes = 0
    external_payloads: dict[int, RelocatedExternalPayload] = {}
    if external_pool and external_allocator is None:
        external_allocator = RelocatedExternalPoolAllocator(data)
    for cluster in relocated_display_clusters(data, targets):
        if not any(target.offset in rows_by_offset for target in cluster):
            continue

        start = min(target.offset for target in cluster)
        end = max(target.offset + target.span for target in cluster)
        capacity = end - start
        payloads: list[tuple[RelocatedDisplayTarget, bytes]] = []
        required = 0
        for target in cluster:
            row = rows_by_offset.get(target.offset)
            payload = row.encoded if row is not None else target.raw
            payloads.append((target, payload))
            required += len(payload) + 1

        if required > capacity:
            if external_allocator is not None:
                overflow = required - capacity
                try:
                    selected = choose_relocated_external_payloads(
                        payloads,
                        rows_by_offset,
                        overflow=overflow,
                    )
                    for target, payload in selected:
                        external_offset = external_allocator.allocate(payload + b"\0")
                        external_payloads[target.offset] = RelocatedExternalPayload(
                            offset=external_offset,
                            payload=payload + b"\0",
                        )
                except ValueError as exc:
                    errors.append(f"{source_path}: {exc}")
                    continue
                payloads = [
                    (target, payload)
                    for target, payload in payloads
                    if target.offset not in external_payloads
                ]
                required = sum(len(payload) + 1 for _target, payload in payloads)

            if required > capacity and force_apply:
                try:
                    truncated = force_truncate_relocated_payloads(
                        rows_by_offset,
                        payloads,
                        capacity=capacity,
                    )
                except ValueError as exc:
                    errors.append(f"{source_path}: {exc}")
                    continue
                forced_truncated_bytes += truncated
                payloads = []
                required = 0
                for target in cluster:
                    row = rows_by_offset.get(target.offset)
                    payload = row.encoded if row is not None else target.raw
                    payloads.append((target, payload))
                    required += len(payload) + 1
                if required > capacity:
                    errors.append(
                        f"{source_path}: force-apply 후에도 relocated display "
                        f"cluster 0x{start:x}-0x{end:x} 필요량 {required}B가 "
                        f"한계 {capacity}B를 초과합니다"
                    )
                    continue
            elif required > capacity:
                table_ids = sorted(
                    {table_id for target in cluster for table_id in target.table_ids}
                )
                errors.append(
                    format_relocated_overflow_error(
                        source_path,
                        table_ids=table_ids,
                        start=start,
                        end=end,
                        capacity=capacity,
                        required=required,
                        payloads=payloads,
                        rows_by_offset=rows_by_offset,
                    )
                )
                continue

        for target in cluster:
            current = bytes(data[target.offset : target.offset + len(target.raw)])
            nul_ok = target.offset + len(target.raw) < len(data) and data[
                target.offset + len(target.raw)
            ] == 0
            if current != target.raw or not nul_ok:
                errors.append(
                    f"{source_path}@0x{target.offset:x}: relocated display 원본이 "
                    f"예상과 다릅니다 ({target.text})"
                )

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        raise ValueError(f"{len(errors)}개 relocated display relayout 오류")

    for cluster in relocated_display_clusters(data, targets):
        if not any(target.offset in rows_by_offset for target in cluster):
            continue

        start = min(target.offset for target in cluster)
        end = max(target.offset + target.span for target in cluster)
        cursor = start
        new_offsets: dict[int, int] = {}
        data[start:end] = b"\0" * (end - start)
        for target in cluster:
            external = external_payloads.get(target.offset)
            if external is not None:
                new_offsets[target.offset] = external.offset
                continue
            payload = (
                rows_by_offset[target.offset].encoded
                if target.offset in rows_by_offset
                else target.raw
            )
            new_offsets[target.offset] = cursor
            data[cursor : cursor + len(payload)] = payload
            cursor += len(payload)
            data[cursor] = 0
            cursor += 1

        for target in cluster:
            new_offset = new_offsets[target.offset]
            new_vma = elf.off_to_vma(new_offset)
            if new_vma is None:
                raise ValueError(f"{source_path}@0x{new_offset:x}: VMA 변환 실패")
            for pointer_offset in target.pointer_file_offsets:
                current = read_u32(data, pointer_offset)
                if current != target.vma:
                    raise ValueError(
                        f"{source_path}@0x{pointer_offset:x}: 포인터 현재값 "
                        f"0x{current:08x}이 예상값 0x{target.vma:08x}과 다릅니다"
                    )
                write_u32_at(data, pointer_offset, new_vma)
                pointer_rewrites += 1
        clusters_rewritten += 1

    for external in external_payloads.values():
        data[external.offset : external.offset + len(external.payload)] = external.payload

    return RelocatedDisplayRelayoutStats(
        clusters=clusters_rewritten,
        rows=len(rows_by_offset),
        pointers=pointer_rewrites,
        forced_truncated_bytes=forced_truncated_bytes,
        externalized_rows=len(external_payloads),
        externalized_bytes=sum(len(external.payload) for external in external_payloads.values()),
    )


def apply_csv(args: argparse.Namespace) -> None:
    if args.in_place and args.out_root is not None:
        raise SystemExit("--in-place와 --out-root는 같이 사용할 수 없습니다")
    if not args.in_place and args.out_root is None:
        raise SystemExit("패치 출력 위치로 --out-root를 지정하거나 --in-place를 사용하세요")

    patched_cache: dict[Path, bytearray] = {}
    original_sizes: dict[Path, int] = {}
    rows_applied = 0
    errors: list[str] = []
    relayout_stats: dict[Path, RelocatedDisplayRelayoutStats] = {}
    indirect_stats: IndirectStringPoolStats | None = None
    external_allocator: RelocatedExternalPoolAllocator | None = None
    normal_rows, relayout_rows_by_path, indirect_rows = collect_apply_rows(args)

    if indirect_rows:
        if not args.patch_korean_font_lookup:
            raise SystemExit(
                "고정 슬롯 외부 문자열은 --patch-korean-font-lookup과 함께 적용해야 합니다"
            )
        eboot_target_path = args.eboot if args.in_place else args.out_root / args.eboot
        if eboot_target_path not in patched_cache:
            raw_data = prepare_target_bytes(args.eboot, eboot_target_path, args)
            patched_cache[eboot_target_path] = bytearray(raw_data)
            original_sizes[eboot_target_path] = len(raw_data)
        try:
            external_allocator = RelocatedExternalPoolAllocator(
                patched_cache[eboot_target_path]
            )
            resolved_rows, indirect_stats = install_indirect_string_pool(
                args.eboot,
                patched_cache[eboot_target_path],
                external_allocator,
                indirect_rows,
            )
        except ValueError as exc:
            raise SystemExit(f"고정 슬롯 외부 문자열 풀 생성 실패: {exc}") from exc
        normal_rows.extend(resolved_rows)

    for row in normal_rows:
        target_path = row_target_path(row, args)
        if target_path not in patched_cache:
            raw_data = prepare_target_bytes(row.source_path, target_path, args)
            patched_cache[target_path] = bytearray(raw_data)
            original_sizes[target_path] = len(raw_data)
        data = patched_cache[target_path]

        current = bytes(data[row.offset : row.offset + len(bytes.fromhex(row.original_hex))])
        if args.zero_fill_span:
            payload = row.encoded + b"\0" * (row.span - len(row.encoded))
            write_len = row.span
        else:
            payload = row.encoded + b"\0"
            write_len = len(payload)
        if row.original_hex and current.hex() != row.original_hex:
            existing = bytes(data[row.offset : row.offset + write_len])
            if existing == payload:
                continue
            errors.append(
                f"row {row.row_index} {row.source_path}: 원본 바이트가 CSV와 다릅니다 "
                f"(현재 {current.hex()}, CSV {row.original_hex})"
            )
            continue

        data[row.offset : row.offset + write_len] = payload
        rows_applied += 1

    for source_path, rows_by_offset in relayout_rows_by_path.items():
        target_path = source_path if args.in_place else args.out_root / source_path
        if target_path not in patched_cache:
            raw_data = prepare_target_bytes(source_path, target_path, args)
            patched_cache[target_path] = bytearray(raw_data)
            original_sizes[target_path] = len(raw_data)
        try:
            stats = apply_relocated_display_relayout(
                source_path,
                patched_cache[target_path],
                rows_by_offset,
                force_apply=args.force_apply,
                external_pool=args.relocated_external_pool,
                external_allocator=(
                    external_allocator if source_path == args.eboot else None
                ),
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        relayout_stats[target_path] = stats
        rows_applied += stats.rows

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        raise SystemExit(f"{len(errors)}개 오류로 패치 중단")

    if args.patch_korean_font_lookup:
        target_path = args.eboot if args.in_place else args.out_root / args.eboot
        if target_path not in patched_cache:
            raw_data = prepare_target_bytes(args.eboot, target_path, args)
            patched_cache[target_path] = bytearray(raw_data)
            original_sizes[target_path] = len(raw_data)
        patched = patch_korean_font_lookup_data(patched_cache[target_path])
        print(f"patched {len(patched)} Korean small-font lookup changes in {target_path}")
        if not args.combining_mark_range:
            combining_patched, combining_range = patch_korean_combining_marks_data(
                patched_cache[target_path]
            )
            if combining_range is not None:
                start_byte, end_byte = combining_range
                print(
                    f"patched {len(combining_patched)} Korean combining mark changes "
                    f"for 0x{start_byte:02x}-0x{end_byte:02x} in {target_path}"
                )

    if args.combining_mark_range:
        try:
            start_byte, end_byte = parse_font_byte_range(args.combining_mark_range)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        target_path = args.eboot if args.in_place else args.out_root / args.eboot
        if target_path not in patched_cache:
            raw_data = prepare_target_bytes(args.eboot, target_path, args)
            patched_cache[target_path] = bytearray(raw_data)
            original_sizes[target_path] = len(raw_data)
        patched = patch_combining_mark_range_data(
            patched_cache[target_path],
            start_byte=start_byte,
            end_byte=end_byte,
        )
        print(
            f"patched {len(patched)} small-font combining mark changes "
            f"for 0x{start_byte:02x}-0x{end_byte:02x} in {target_path}"
        )

    size_errors = [
        f"{path}: 파일 크기가 {original_sizes[path]}에서 {len(data)}로 바뀌었습니다"
        for path, data in patched_cache.items()
        if len(data) != original_sizes[path]
    ]
    if size_errors:
        for error in size_errors:
            print(error, file=sys.stderr)
        raise SystemExit(f"{len(size_errors)}개 파일 크기 변경 오류로 패치 중단")

    for path, data in patched_cache.items():
        path.write_bytes(data)
    for path, stats in relayout_stats.items():
        forced = (
            f", force-truncated {stats.forced_truncated_bytes}B"
            if stats.forced_truncated_bytes
            else ""
        )
        externalized = (
            f", externalized {stats.externalized_rows} rows/{stats.externalized_bytes}B"
            if stats.externalized_rows
            else ""
        )
        print(
            f"relocated {stats.rows} translations in {stats.clusters} "
            f"EBOOT offset-table clusters and rewrote {stats.pointers} pointers in {path}"
            f"{forced}{externalized}"
        )
    if indirect_stats is not None:
        print(
            f"externalized {indirect_stats.rows} fixed-slot translations as "
            f"{indirect_stats.unique_payloads} unique full strings/"
            f"{indirect_stats.payload_bytes}B and installed "
            f"{indirect_stats.stub_bytes}B of small-font resolver stubs"
        )
    print(f"applied {rows_applied} translations to {len(patched_cache)} files")


def has_expanded_font_lookup_patch(data: bytes | bytearray) -> bool:
    for offset, _expected_old, expected_new in SMALL_FONT_EXPANDED_LOOKUP_BASE_PATCHES:
        if offset + 4 > len(data) or read_u32_at(data, offset) != expected_new:
            return False
    for offset, _expected_old, expected_new in SMALL_FONT_EXPANDED_HIGH_LOAD_PATCHES:
        if offset + 4 > len(data) or read_u32_at(data, offset) != expected_new:
            return False
    return True


def font_lookup_offset_for_byte(byte: int, *, expanded: bool) -> int | None:
    if expanded:
        return small_font_lookup_offset(byte)
    return stock_small_font_lookup_offset(byte)


def lookup_map_csv(args: argparse.Namespace) -> None:
    data = args.eboot.read_bytes()
    expanded = has_expanded_font_lookup_patch(data)
    refs_by_index: dict[int, list[str]] = {index: [] for index in range(SMALL_FONT_CELL_COUNT)}

    for kind, byte_range in (
        ("ascii", range(0x21, 0x80)),
        ("high", range(0x80, 0x100 if expanded else 0xE0)),
    ):
        for byte in byte_range:
            offset = font_lookup_offset_for_byte(byte, expanded=expanded)
            if offset is None:
                continue
            if offset >= len(data):
                raise SystemExit(f"{args.eboot} is too small for lookup offset 0x{offset:x}")
            glyph_index = data[offset]
            label = font_byte_label(byte)
            if glyph_index < SMALL_FONT_CELL_COUNT and glyph_index != 0xFF:
                refs_by_index[glyph_index].append(
                    f"{kind}:0x{byte:02x}({label})@0x{offset:x}"
                )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "tile_index_hex",
                "tile_index",
                "src_x",
                "src_y",
                "glyph",
                "byte_refs",
            ],
        )
        writer.writeheader()
        for index in range(SMALL_FONT_CELL_COUNT):
            writer.writerow(
                {
                    "tile_index_hex": f"0x{index:02x}",
                    "tile_index": index,
                    "src_x": (index & 0x1F) * 8,
                    "src_y": (index >> 5) * 8,
                    "glyph": small_font_tile_glyph(index),
                    "byte_refs": " | ".join(refs_by_index[index]),
                }
            )
    print(f"wrote {SMALL_FONT_CELL_COUNT} tile lookup rows to {args.output}")


def color_luma(color: tuple[int, int, int, int]) -> float:
    red, green, blue, _alpha = color
    return 0.299 * red + 0.587 * green + 0.114 * blue


def color_distance(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> int:
    return sum((left[index] - right[index]) ** 2 for index in range(4))


def unique_rgba_colors(image: object) -> list[tuple[int, int, int, int]]:
    colors = image.getcolors(maxcolors=image.width * image.height + 1)
    if colors is None:
        counts: dict[tuple[int, int, int, int], int] = {}
        for color in image.getdata():
            counts[color] = counts.get(color, 0) + 1
        return list(counts)
    return [color for _count, color in colors]


def snap_rgba_to_colors(image: object, palette: list[tuple[int, int, int, int]]) -> object:
    if not palette:
        return image
    palette_keys = set(palette)
    snapped = image.convert("RGBA")
    pixels = snapped.load()
    for y in range(snapped.height):
        for x in range(snapped.width):
            color = pixels[x, y]
            if color in palette_keys:
                continue
            pixels[x, y] = min(palette, key=lambda candidate: color_distance(color, candidate))
    return snapped


def fonttile_source_palette(image: object) -> list[tuple[int, int, int, int]]:
    colors = unique_rgba_colors(image.convert("RGBA"))
    return sorted(colors, key=lambda color: (color_luma(color), color))


def fonttile_background_and_foreground(
    palette: list[tuple[int, int, int, int]],
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    opaque = [color for color in palette if color[3] > 0] or palette
    if not opaque:
        return (0, 0, 0, 255), (255, 255, 255, 255)
    background = min(opaque, key=lambda color: (color_luma(color), color))
    foreground = max(opaque, key=lambda color: (color_luma(color), color))
    return background, foreground


def required_fonttile_height(mappings: tuple[KoreanGlyphMapping, ...]) -> int:
    if not mappings:
        return SMALL_FONT_CELL_SIZE * SMALL_FONT_BASE_ROWS
    max_tile_index = max(mapping.tile_index for mapping in mappings)
    rows = (max_tile_index // SMALL_FONT_CELL_COLUMNS) + 1
    return rows * SMALL_FONT_CELL_SIZE


def expand_fonttile_canvas_if_needed(
    image: object,
    *,
    required_height: int,
    background: tuple[int, int, int, int],
) -> object:
    if image.height >= required_height:
        return image
    from PIL import Image

    expanded = Image.new("RGBA", (image.width, required_height), background)
    expanded.paste(image, (0, 0))
    return expanded


def render_pillow_bbox_glyph_mask(char: str, font: object) -> object:
    from PIL import Image, ImageDraw

    scratch = Image.new("L", (32, 32), 0)
    draw = ImageDraw.Draw(scratch)
    bbox = draw.textbbox((0, 0), char, font=font)
    left, top, right, bottom = bbox
    width = max(1, right - left)
    height = max(1, bottom - top)
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.text((-left, -top), char, font=font, fill=255)
    '''if mask.width > 8 or mask.height > 8 :
        scale = min(8 / mask.width, 8 / mask.height)
        mask = mask.resize(
            (
                max(1, round(mask.width * scale)),
                max(1, round(mask.height * scale)),
            ),
            Image.Resampling.LANCZOS,
        )'''
    return mask


def render_dalmoori_grid_glyph_mask(char: str, font: object) -> object:
    from PIL import Image, ImageDraw

    source_size = SMALL_FONT_CELL_SIZE * DALMOORI_GRID_SAMPLE_STEP
    source = Image.new("L", (source_size, source_size), 0)
    draw = ImageDraw.Draw(source)
    draw.fontmode = "1"
    draw.text((0, 0), char, font=font, fill=255)

    mask = Image.new("L", (SMALL_FONT_CELL_SIZE, SMALL_FONT_CELL_SIZE), 0)
    for y in range(SMALL_FONT_CELL_SIZE):
        for x in range(SMALL_FONT_CELL_SIZE):
            sample_x = x * DALMOORI_GRID_SAMPLE_STEP + (DALMOORI_GRID_SAMPLE_STEP // 2)
            sample_y = y * DALMOORI_GRID_SAMPLE_STEP + (DALMOORI_GRID_SAMPLE_STEP // 2)
            if source.getpixel((sample_x, sample_y)):
                mask.putpixel((x, y), 255)
    return mask


def render_glyph_mask(char: str, font: object, renderer: str) -> object:
    if renderer == "dalmoori-grid":
        return render_dalmoori_grid_glyph_mask(char, font)
    if renderer == "pillow-bbox":
        return render_pillow_bbox_glyph_mask(char, font)
    raise ValueError(f"unknown glyph renderer: {renderer}")


def write_korean_glyph_map_csv(path: Path, mappings: tuple[KoreanGlyphMapping, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "glyph",
                "mark_type",
                "byte_hex",
                "byte_label",
                "tile_index",
                "src_x",
                "src_y",
                "source",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        for mapping in mappings:
            writer.writerow(
                {
                    "glyph": mapping.glyph,
                    "mark_type": mapping.mark_type,
                    "byte_hex": f"0x{mapping.byte:02x}",
                    "byte_label": font_byte_label(mapping.byte),
                    "tile_index": mapping.tile_index,
                    "src_x": (mapping.tile_index & 0x1F) * 8,
                    "src_y": (mapping.tile_index >> 5) * 8,
                    "source": mapping.source,
                }
            )


def relative_to_or_none(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def texture_manifest_key(record: dict) -> tuple[str, str, str]:
    return (
        str(record.get("source", "")).replace("\\", "/"),
        str(record.get("offset", "")),
        str(record.get("output", "")).replace("\\", "/"),
    )


def update_fonttile_record_dimensions(record: dict, width: int, height: int) -> None:
    for field, value in (
        ("width", width),
        ("height", height),
        ("storage_width", width),
        ("storage_height", height),
        ("output_crop_x", 0),
        ("output_crop_y", 0),
        ("output_crop_width", width),
        ("output_crop_height", height),
    ):
        if field in record:
            record[field] = value


def merged_fieldnames(existing: list[str], incoming: list[str]) -> list[str]:
    fieldnames = list(existing)
    for field in incoming:
        if field not in fieldnames:
            fieldnames.append(field)
    return fieldnames


def merge_fonttile_manifest_record(
    *,
    source_root: Path,
    out_root: Path,
    source_output_rel: str,
    target_output_rel: str,
    target_width: int | None = None,
    target_height: int | None = None,
) -> list[Path]:
    written: list[Path] = []

    source_json = source_root / "manifest.json"
    target_json = out_root / "manifest.json"
    if source_json.exists():
        records = json.loads(source_json.read_text(encoding="utf-8"))
        incoming = [
            dict(record)
            for record in records
            if str(record.get("output", "")).replace("\\", "/") == source_output_rel
        ]
        for record in incoming:
            record["output"] = target_output_rel
            if target_width is not None and target_height is not None:
                update_fonttile_record_dimensions(record, target_width, target_height)
        if incoming:
            target_records = (
                json.loads(target_json.read_text(encoding="utf-8"))
                if target_json.exists()
                else []
            )
            merged = {texture_manifest_key(record): record for record in target_records}
            for record in incoming:
                merged[texture_manifest_key(record)] = record
            out_root.mkdir(parents=True, exist_ok=True)
            target_json.write_text(
                json.dumps(list(merged.values()), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            written.append(target_json)

    source_csv = source_root / "manifest.csv"
    target_csv = out_root / "manifest.csv"
    if source_csv.exists():
        with source_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            source_fieldnames = list(reader.fieldnames or [])
            incoming_rows = [
                dict(row)
                for row in reader
                if (row.get("output") or "").replace("\\", "/") == source_output_rel
            ]
        for row in incoming_rows:
            row["output"] = target_output_rel
            if target_width is not None and target_height is not None:
                update_fonttile_record_dimensions(row, target_width, target_height)
        if incoming_rows:
            existing_rows: list[dict[str, str]] = []
            existing_fieldnames: list[str] = []
            if target_csv.exists():
                with target_csv.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    existing_fieldnames = list(reader.fieldnames or [])
                    existing_rows = list(reader)
            merged = {texture_manifest_key(row): row for row in existing_rows}
            for row in incoming_rows:
                merged[texture_manifest_key(row)] = row
            fieldnames = merged_fieldnames(existing_fieldnames, source_fieldnames)
            fieldnames = merged_fieldnames(
                fieldnames,
                [field for row in merged.values() for field in row.keys()],
            )
            out_root.mkdir(parents=True, exist_ok=True)
            with target_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
                writer.writeheader()
                writer.writerows(merged.values())
            written.append(target_csv)

    return written


def render_korean_fonttile(args: argparse.Namespace) -> None:
    from PIL import Image

    with Image.open(args.source) as raw:
        image = raw.convert("RGBA")
    palette = fonttile_source_palette(image)
    background, _foreground = fonttile_background_and_foreground(palette)
    image = expand_fonttile_canvas_if_needed(
        image,
        required_height=required_fonttile_height(KOREAN_GLYPH_MAPPINGS),
        background=background,
    )

    if not DEFAULT_ALL_KOREAN_FONTTILE_PNG.exists():
        raise SystemExit(f"all Korean fonttile image not found: {DEFAULT_ALL_KOREAN_FONTTILE_PNG}")
    glyph_sources = load_all_korean_fonttile_map()
    with Image.open(DEFAULT_ALL_KOREAN_FONTTILE_PNG) as raw_atlas:
        atlas = raw_atlas.convert("RGBA")

    missing_glyphs = sorted(
        {mapping.glyph for mapping in KOREAN_GLYPH_MAPPINGS if mapping.glyph not in glyph_sources}
    )
    if missing_glyphs:
        raise SystemExit(
            f"{DEFAULT_ALL_KOREAN_FONTTILE_MAP}: missing glyph(s): {''.join(missing_glyphs)}"
        )

    for mapping in KOREAN_GLYPH_MAPPINGS:
        x = (mapping.tile_index & 0x1F) * 8
        y = (mapping.tile_index >> 5) * 8
        for py in range(y, y + 8):
            for px in range(x, x + 8):
                image.putpixel((px, py), background)
        src_x, src_y = glyph_sources[mapping.glyph]
        if src_x < 0 or src_y < 0 or src_x + 8 > atlas.width or src_y + 8 > atlas.height:
            raise SystemExit(
                f"{DEFAULT_ALL_KOREAN_FONTTILE_MAP}: glyph {mapping.glyph!r} source "
                f"tile is outside {DEFAULT_ALL_KOREAN_FONTTILE_PNG}"
            )
        tile = atlas.crop((src_x, src_y, src_x + 8, src_y + 8))
        image.paste(tile, (x, y))

    image = snap_rgba_to_colors(image, palette)
    if args.map_output:
        write_korean_glyph_map_csv(args.map_output, KOREAN_ALL_GLYPH_MAPPINGS)
        print(f"wrote {len(KOREAN_ALL_GLYPH_MAPPINGS)} glyph mappings to {args.map_output}")
    if not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        image.save(args.output)
        print(f"rendered {len(KOREAN_GLYPH_MAPPINGS)} Korean glyphs to {args.output}")
    else:
        print(f"validated render of {len(KOREAN_GLYPH_MAPPINGS)} Korean glyphs")

    if args.no_copy_manifest or args.dry_run:
        return
    source_output_rel = relative_to_or_none(args.source, args.textures_root)
    target_output_rel = relative_to_or_none(args.output, args.out_root)
    if not source_output_rel or not target_output_rel:
        print("manifest copy skipped: source/output are not under their roots", file=sys.stderr)
        return
    written = merge_fonttile_manifest_record(
        source_root=args.textures_root,
        out_root=args.out_root,
        source_output_rel=source_output_rel,
        target_output_rel=target_output_rel,
        target_width=image.width,
        target_height=image.height,
    )
    for path in written:
        print(f"Manifest written: {path}")


def read_u32_at(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def write_u32_at(data: bytearray, offset: int, value: int) -> None:
    data[offset : offset + 4] = value.to_bytes(4, "little")


def patch_expected_word(
    data: bytearray,
    *,
    offset: int,
    expected_old: int,
    expected_new: int,
    label: str,
) -> str | None:
    if offset + 4 > len(data):
        raise ValueError(f"EBOOT too small for {label} patch offset 0x{offset:x}")
    current = read_u32_at(data, offset)
    if current == expected_new:
        return None
    if current != expected_old:
        raise ValueError(
            f"{label} patch expected 0x{expected_old:08x} at 0x{offset:x}, "
            f"found 0x{current:08x}"
        )
    write_u32_at(data, offset, expected_new)
    return f"{label} @0x{offset:x}: 0x{expected_old:08x}->0x{expected_new:08x}"


def parse_font_byte(value: str) -> int:
    text = value.strip().lower()
    if text.startswith("$"):
        text = "0x" + text[1:]
    base = 16 if text.startswith("0x") or any(char in "abcdef" for char in text) else 10
    try:
        byte = int(text, base)
    except ValueError as exc:
        raise ValueError(f"invalid byte value: {value!r}") from exc
    if not 0 <= byte <= 0xFF:
        raise ValueError(f"byte value out of range: {value!r}")
    return byte


def parse_font_byte_range(value: str) -> tuple[int, int]:
    text = value.strip()
    separator = ".." if ".." in text else "-"
    parts = text.split(separator, 1)
    if len(parts) == 1:
        start = end = parse_font_byte(parts[0])
    else:
        start = parse_font_byte(parts[0])
        end = parse_font_byte(parts[1])
    if start > end:
        raise ValueError(f"byte range start is after end: {value!r}")
    return start, end


def combining_mark_range_patch_values(start_byte: int, end_byte: int) -> tuple[int, int]:
    if start_byte == 0:
        raise ValueError("0x00은 문자열 종료 byte라 결합 범위에 넣을 수 없습니다")
    if not 0 <= start_byte <= end_byte <= 0xFF:
        raise ValueError(f"invalid combining range: 0x{start_byte:02x}-0x{end_byte:02x}")
    mark_count = end_byte - start_byte + 1
    if mark_count > 0xFF:
        raise ValueError("결합 범위는 최대 255개 byte까지만 지정할 수 있습니다")
    add_immediate = (-start_byte) & 0xFF
    return add_immediate, mark_count


def patch_i_type_immediate(
    data: bytearray,
    *,
    offset: int,
    expected_opcode: int,
    immediate: int,
    label: str,
) -> str | None:
    if offset + 4 > len(data):
        raise ValueError(f"EBOOT too small for {label} patch offset 0x{offset:x}")
    current = read_u32_at(data, offset)
    if current & 0xFFFF0000 != expected_opcode:
        raise ValueError(
            f"{label} patch expected opcode 0x{expected_opcode:08x} at 0x{offset:x}, "
            f"found 0x{current:08x}"
        )
    expected_new = expected_opcode | immediate
    if current == expected_new:
        return None
    write_u32_at(data, offset, expected_new)
    return f"{label} @0x{offset:x}: 0x{current:08x}->0x{expected_new:08x}"


def patch_combining_mark_range_data(
    data: bytearray,
    *,
    start_byte: int,
    end_byte: int,
) -> list[str]:
    add_immediate, mark_count = combining_mark_range_patch_values(start_byte, end_byte)
    patched: list[str] = []
    for label, add_offset, add_opcode, count_offset, count_opcode in (
        SMALL_FONT_COMBINING_MARK_RANGE_PATCHES
    ):
        add_patch = patch_i_type_immediate(
            data,
            offset=add_offset,
            expected_opcode=add_opcode,
            immediate=add_immediate,
            label=f"{label} combining-range addiu",
        )
        if add_patch:
            patched.append(add_patch)
        count_patch = patch_i_type_immediate(
            data,
            offset=count_offset,
            expected_opcode=count_opcode,
            immediate=mark_count,
            label=f"{label} combining-range sltiu",
        )
        if count_patch:
            patched.append(count_patch)
    return patched


def patch_korean_combining_marks_data(data: bytearray) -> tuple[list[str], tuple[int, int] | None]:
    if KOREAN_COMBINING_MARK_RANGE is None:
        return [], None
    start_byte, end_byte = KOREAN_COMBINING_MARK_RANGE
    patched = patch_combining_mark_range_data(
        data,
        start_byte=start_byte,
        end_byte=end_byte,
    )
    return patched, (start_byte, end_byte)


def build_expanded_font_lookup_table() -> bytearray:
    table = bytearray([0xFF] * SMALL_FONT_EXPANDED_LOOKUP_ENTRIES)
    for byte in range(0x21, 0x80):
        stock_index = stock_small_font_lookup_index(byte)
        expanded_index = small_font_lookup_index(byte)
        if stock_index is None or expanded_index is None:
            continue
        table[expanded_index] = SMALL_FONT_STOCK_LOOKUP_BYTES[stock_index]
    for byte in range(0x80, 0xE0):
        stock_index = stock_small_font_lookup_index(byte)
        expanded_index = small_font_lookup_index(byte)
        if stock_index is None or expanded_index is None:
            continue
        table[expanded_index] = SMALL_FONT_STOCK_LOOKUP_BYTES[stock_index]
    return table


def patch_korean_font_lookup_data(data: bytearray) -> list[str]:
    patched: list[str] = []
    for offset, expected_old, expected_new in SMALL_FONT_EXPANDED_LOOKUP_BASE_PATCHES:
        item = patch_expected_word(
            data,
            offset=offset,
            expected_old=expected_old,
            expected_new=expected_new,
            label="small-font lookup base",
        )
        if item:
            patched.append(item)
    for offset, expected_old, expected_new in SMALL_FONT_EXPANDED_HIGH_LOAD_PATCHES:
        item = patch_expected_word(
            data,
            offset=offset,
            expected_old=expected_old,
            expected_new=expected_new,
            label="small-font high-byte lookup",
        )
        if item:
            patched.append(item)

    if SMALL_FONT_EXPANDED_LOOKUP_OFFSET + SMALL_FONT_EXPANDED_LOOKUP_ENTRIES > len(data):
        raise ValueError(
            f"EBOOT too small for expanded lookup table at 0x{SMALL_FONT_EXPANDED_LOOKUP_OFFSET:x}"
        )
    table = build_expanded_font_lookup_table()
    for mapping in KOREAN_GLYPH_MAPPINGS:
        index = small_font_lookup_index(mapping.byte)
        if index is None or index >= len(table):
            raise ValueError(f"cannot map byte 0x{mapping.byte:02x} through expanded lookup")
        old = table[index]
        if old == mapping.tile_index:
            continue
        table[index] = mapping.tile_index
        patched.append(
            f"0x{mapping.byte:02x}({font_byte_label(mapping.byte)}) "
            f"{old}-> {mapping.tile_index} for {mapping.glyph}"
        )
    old_table = bytes(
        data[
            SMALL_FONT_EXPANDED_LOOKUP_OFFSET : SMALL_FONT_EXPANDED_LOOKUP_OFFSET
            + SMALL_FONT_EXPANDED_LOOKUP_ENTRIES
        ]
    )
    if old_table != bytes(table):
        data[
            SMALL_FONT_EXPANDED_LOOKUP_OFFSET : SMALL_FONT_EXPANDED_LOOKUP_OFFSET
            + SMALL_FONT_EXPANDED_LOOKUP_ENTRIES
        ] = table
        patched.append(
            f"expanded lookup table @0x{SMALL_FONT_EXPANDED_LOOKUP_OFFSET:x} "
            f"({SMALL_FONT_EXPANDED_LOOKUP_ENTRIES} bytes)"
        )
    return patched


def patch_korean_font_lookup(args: argparse.Namespace) -> None:
    if args.in_place and args.output is not None:
        raise SystemExit("--in-place와 --output은 같이 사용할 수 없습니다")
    if not args.in_place and args.output is None:
        raise SystemExit("패치 출력 위치로 --output을 지정하거나 --in-place를 사용하세요")
    data = bytearray(args.eboot.read_bytes())
    patched = patch_korean_font_lookup_data(data)
    combining_patched, combining_range = patch_korean_combining_marks_data(data)
    target = args.eboot if args.in_place else args.output
    assert target is not None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(f"patched {len(patched)} Korean small-font lookup changes in {target}")
    for item in patched:
        print(f"  {item}")
    if combining_range is not None:
        start_byte, end_byte = combining_range
        print(
            f"patched {len(combining_patched)} Korean combining mark changes "
            f"for 0x{start_byte:02x}-0x{end_byte:02x} in {target}"
        )
        for item in combining_patched:
            print(f"  {item}")


def patch_combining_marks(args: argparse.Namespace) -> None:
    if args.in_place and args.output is not None:
        raise SystemExit("--in-place와 --output은 같이 사용할 수 없습니다")
    if not args.dry_run and not args.in_place and args.output is None:
        raise SystemExit("패치 출력 위치로 --output을 지정하거나 --in-place를 사용하세요")
    try:
        start_byte, end_byte = parse_font_byte_range(args.range)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    data = bytearray(args.eboot.read_bytes())
    patched = patch_combining_mark_range_data(
        data,
        start_byte=start_byte,
        end_byte=end_byte,
    )
    print(
        f"small-font combining mark range: 0x{start_byte:02x}-0x{end_byte:02x} "
        f"({end_byte - start_byte + 1} bytes)"
    )
    if patched:
        for item in patched:
            print(f"  {item}")
    else:
        print("  no changes needed")
    if args.dry_run:
        return

    target = args.eboot if args.in_place else args.output
    assert target is not None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(f"patched small-font combining mark range in {target}")


def korean_glyph_map_csv(args: argparse.Namespace) -> None:
    write_korean_glyph_map_csv(args.output, KOREAN_ALL_GLYPH_MAPPINGS)
    print(f"wrote {len(KOREAN_ALL_GLYPH_MAPPINGS)} glyph mappings to {args.output}")


def patch_eboot_utf8(args: argparse.Namespace) -> None:
    if args.in_place and args.output is not None:
        raise SystemExit("--in-place와 --output은 같이 사용할 수 없습니다")
    if not args.in_place and args.output is None:
        raise SystemExit("패치 출력 위치로 --output을 지정하거나 --in-place를 사용하세요")

    original_data = args.eboot.read_bytes()
    data = bytearray(original_data)
    patched: list[tuple[int, str, str]] = []
    unchanged: list[tuple[int, str]] = []
    errors: list[str] = []
    for offset, original, translation in EBOOT_UTF8_TRANSLATIONS:
        source = original.encode("utf-8")
        target = translation.encode("utf-8")
        if len(target) > len(source):
            errors.append(
                f"{original!r}: 번역이 고정 슬롯을 {len(target) - len(source)}바이트 초과"
            )
            continue
        current = original_data[offset : offset + len(source) + 1]
        translated_slot = target + b"\0" + b"\0" * (len(source) - len(target))
        if current == translated_slot:
            unchanged.append((offset, translation))
            continue
        if current != source + b"\0":
            errors.append(
                f"0x{offset:x} {original!r}: 원본 바이트 불일치 "
                f"(현재 {current.hex()}, 예상 {(source + b'\0').hex()})"
            )
            continue
        data[offset : offset + len(source) + 1] = translated_slot
        patched.append((offset, original, translation))

    if errors:
        raise SystemExit("UTF-8 EBOOT 패치 검증 실패:\n  " + "\n  ".join(errors))
    if len(data) != len(original_data):
        raise SystemExit("UTF-8 EBOOT 패치가 파일 크기를 변경했습니다")

    target_path = args.eboot if args.in_place else args.output
    assert target_path is not None
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(data)
    print(f"patched {len(patched)} UTF-8 strings in {target_path}")
    for offset, original, translation in patched:
        print(f"  0x{offset:x}: {original} => {translation}")
    if unchanged:
        print(f"  already translated: {len(unchanged)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    utf8 = subparsers.add_parser(
        "utf8", help="patch known EBOOT UTF-8 title and scenario labels to Korean"
    )
    utf8.add_argument("--eboot", type=Path, default=DEFAULT_EBOOT)
    utf8.add_argument("--output", type=Path)
    utf8.add_argument("--in-place", action="store_true")
    utf8.set_defaults(func=patch_eboot_utf8)

    dump = subparsers.add_parser("dump", help="write fixed-slot strings to CSV")
    dump.add_argument("--output", type=Path, default=Path("results/fonttile_text_slots.csv"))
    dump.add_argument(
        "--eboot",
        type=Path,
        default=DEFAULT_EBOOT,
        help="decrypted EBOOT ELF used as the exact relocated-table source",
    )
    dump.add_argument("--min-bytes", type=int, default=3)
    dump.add_argument("--max-span", type=int, default=32)
    dump.add_argument(
        "--include-ascii",
        action="store_true",
        help="raw scan mode only: also include pure ASCII slots",
    )
    dump.add_argument(
        "--raw",
        action="store_true",
        help="investigation mode: use the old broad byte-run scan instead of exact EBOOT tables",
    )
    dump.add_argument(
        "--strict-filter",
        action="store_true",
        help="raw scan mode only: keep slots backed by old pointer/record/trusted-text heuristics",
    )
    dump.add_argument(
        "--no-archive-tables",
        action="store_true",
        help="default mode only: disable detected archive-side MRG record tables",
    )
    dump.add_argument("paths", nargs="*", help="files or directories to scan")
    dump.set_defaults(func=dump_csv)

    dictionary = subparsers.add_parser(
        "dictionary", help="create a unique-original translation dictionary from slots CSV"
    )
    dictionary.add_argument("slots", type=Path)
    dictionary.add_argument("--output", type=Path, default=Path("results/fonttile_text_dictionary.csv"))
    dictionary.add_argument("--samples", type=int, default=3)
    dictionary.add_argument(
        "--keep-translations",
        action="store_true",
        help="preserve existing translation values from the output CSV when regenerating",
    )
    dictionary.set_defaults(func=dictionary_csv)

    fill = subparsers.add_parser(
        "fill", help="copy dictionary translations back into every matching slot row"
    )
    fill.add_argument("slots", type=Path)
    fill.add_argument("dictionary", type=Path)
    fill.add_argument("--output", type=Path, default=Path("results/fonttile_text_slots.filled.csv"))
    fill.set_defaults(func=fill_csv)

    apply = subparsers.add_parser("apply", help="apply edited CSV translations")
    apply.add_argument("csv", type=Path)
    apply.add_argument("--out-root", type=Path)
    apply.add_argument("--in-place", action="store_true")
    apply.add_argument(
        "--patch-korean-font-lookup",
        action="store_true",
        help="also patch the EBOOT lookup/table needed by Korean glyph byte assignments",
    )
    apply.add_argument(
        "--combining-mark-range",
        help=(
            "override the Korean mark byte range used by the EBOOT small-font renderer; "
            "when omitted with --patch-korean-font-lookup, derive it from the Korean glyph map"
        ),
    )
    apply.add_argument(
        "--eboot",
        type=Path,
        default=DEFAULT_EBOOT,
        help="EBOOT to patch when EBOOT-level font patches are used",
    )
    apply.add_argument(
        "--zero-fill-span",
        action="store_true",
        help="legacy behavior: zero-fill the whole CSV span instead of writing one terminator",
    )
    apply.add_argument(
        "--force-apply",
        action="store_true",
        help=(
            "dangerous: when encoded translations exceed slot or relocated string-pool "
            "capacity, truncate encoded bytes round-robin from the largest overflows"
        ),
    )
    apply.add_argument(
        "--relocated-external-pool",
        action="store_true",
        help=(
            "preserve every oversized translation: repoint relocated display strings and "
            "encode fixed slots as three-byte references to known mapped .rodata padding"
        ),
    )
    apply.add_argument(
        "--relayout-offset-tables",
        "--adjust-offset-tables",
        action="store_true",
        default=True,
        help=argparse.SUPPRESS,
    )
    apply.add_argument(
        "--no-relayout-offset-tables",
        action="store_false",
        dest="relayout_offset_tables",
        help=(
            "investigation mode only: disable EBOOT relocated display string-pool "
            "relayout and enforce each row max_bytes"
        ),
    )
    apply.set_defaults(func=apply_csv)

    lookup_map = subparsers.add_parser(
        "lookup-map", help="write EBOOT small-font lookup table to a cell map CSV"
    )
    lookup_map.add_argument(
        "--eboot", type=Path, default=Path("results/ULJS00178_EBOOT.BIN")
    )
    lookup_map.add_argument(
        "--output", type=Path, default=Path("results/fonttile_lookup_map.csv")
    )
    lookup_map.set_defaults(func=lookup_map_csv)

    render_tile = subparsers.add_parser(
        "render-korean-tile",
        help="render the Korean small-font glyph set into the 8x8 font tile PNG",
    )
    render_tile.add_argument("--source", type=Path, default=DEFAULT_SMALL_FONT_TILE_PNG)
    render_tile.add_argument("--output", type=Path, default=DEFAULT_SMALL_FONT_TILE_OUTPUT)
    render_tile.add_argument("--textures-root", type=Path, default=Path("textures_static"))
    render_tile.add_argument("--out-root", type=Path, default=Path("textures_translated"))
    render_tile.add_argument("--map-output", type=Path)
    render_tile.add_argument("--dry-run", action="store_true")
    render_tile.add_argument("--no-copy-manifest", action="store_true")
    render_tile.set_defaults(func=render_korean_fonttile)

    glyph_map = subparsers.add_parser(
        "korean-glyph-map", help="write Korean glyph byte/tile assignments to CSV"
    )
    glyph_map.add_argument(
        "--output",
        type=Path,
        default=Path("results/fonttile_korean_glyph_map.csv"),
    )
    glyph_map.set_defaults(func=korean_glyph_map_csv)

    patch_lookup = subparsers.add_parser(
        "patch-korean-lookup",
        help="patch EBOOT lookup/table bytes and Korean mark range for small-font glyphs",
    )
    patch_lookup.add_argument("--eboot", type=Path, default=DEFAULT_EBOOT)
    patch_lookup.add_argument("--output", type=Path)
    patch_lookup.add_argument("--in-place", action="store_true")
    patch_lookup.set_defaults(func=patch_korean_font_lookup)

    patch_combining = subparsers.add_parser(
        "patch-combining-marks",
        help="patch EBOOT small-font bytes that render without advancing",
    )
    patch_combining.add_argument("--eboot", type=Path, default=DEFAULT_EBOOT)
    patch_combining.add_argument(
        "--range",
        default="0xde-0xdf",
        help="inclusive byte range to treat as combining marks, e.g. 0xd6-0xfe",
    )
    patch_combining.add_argument("--output", type=Path)
    patch_combining.add_argument("--in-place", action="store_true")
    patch_combining.add_argument("--dry-run", action="store_true")
    patch_combining.set_defaults(func=patch_combining_marks)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
