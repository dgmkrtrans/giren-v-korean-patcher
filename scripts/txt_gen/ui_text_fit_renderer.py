#!/usr/bin/env python3
"""Render Korean UI text by fitting natural font glyphs into source PNG bounds."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from text_renderer import (
    PROJECT_ROOT,
    RowRangeFilter,
    load_font,
    merge_manifest_records,
    merged_fieldnames,
    read_rows,
    resolve_font,
)

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import dump_static_textures as texture_dump  # noqa: E402
import faction_select_phrase_generator  # noqa: E402


Color = tuple[int, int, int, int]


@dataclass(frozen=True)
class SourcePalette:
    width: int
    height: int
    colors: list[Color]
    background: Color
    foreground: Color
    color_to_index: dict[Color, int]


@dataclass(frozen=True)
class FitPlan:
    lines: list[str]
    font_size: int
    x_scale: float
    y_scale: float
    natural_width: int
    natural_height: int
    rendered_width: int
    rendered_height: int
    font_reduced: bool
    scaled: bool


@dataclass(frozen=True)
class TextStyle:
    font_role: str
    font_size: float
    align: str = "left"
    valign: str = "center"
    x_padding: int = 0
    x_padding_left: int | None = None
    x_padding_right: int | None = None
    y_padding: int = 0
    line_spacing: int = 0
    preserve_newlines: bool = True
    wrap: bool = True
    line_index: int | None = None
    shrink_overflow_x: bool = False
    shrink_overflow_y: bool = False
    outline_width: int = 0
    shadow_offset: tuple[int, int] | None = None
    cell_split: str = ""
    cell_width: int = 0
    cell_height: int = 0
    max_render_width: int = 0
    bracketed_font_size_delta: float = 0


@dataclass(frozen=True)
class GroupRule:
    name: str
    styles: tuple[TextStyle, ...]
    background_source: str = "palette_index_0"

    def styles_for(self, row: dict[str, str], source: SourcePalette) -> tuple[TextStyle, ...]:
        return self.styles


@dataclass(frozen=True)
class SystemMessageRule(GroupRule):
    pass


@dataclass(frozen=True)
class GameManualRule(GroupRule):
    pass


@dataclass(frozen=True)
class UnitOfficerCountRule(GroupRule):
    pass


@dataclass(frozen=True)
class BudgetInputRule(GroupRule):
    pass


@dataclass(frozen=True)
class UI16FlatRule(GroupRule):
    pass


@dataclass(frozen=True)
class BattleDisplayRule(GroupRule):
    pass


@dataclass(frozen=True)
class EndingTextRule(GroupRule):
    pass


@dataclass(frozen=True)
class FactionSelectPromptRule(GroupRule):
    pass


@dataclass(frozen=True)
class FactionSelectPhraseRule(GroupRule):
    pass


@dataclass(frozen=True)
class JoinLeaveRule(GroupRule):
    pass


@dataclass(frozen=True)
class DatabaseRule(GroupRule):
    def styles_for(self, row: dict[str, str], source: SourcePalette) -> tuple[TextStyle, ...]:
        del row
        if source.height == 25:
            return (
                TextStyle(
                    font_role="nanum_gothic_extrabold",
                    font_size=19,
                    align="right",
                    valign="center",
                    x_padding_right=10,
                    bracketed_font_size_delta=-4,
                    preserve_newlines=False,
                    wrap=False,
                ),
            )
        if source.height == 16:
            return (
                TextStyle(
                    font_role="nanum_gothic_extrabold",
                    font_size=16,
                    align="right",
                    valign="center",
                    preserve_newlines=False,
                    wrap=False,
                    shrink_overflow_x=True,
                ),
            )
        return (
            TextStyle(
                font_role="nanum_myeongjo_extrabold",
                font_size=13,
                align="left",
                valign="top",
                preserve_newlines=True,
                wrap=True,
                shrink_overflow_x=True,
            ),
        )


@dataclass(frozen=True)
class UI14Rule(GroupRule):
    def styles_for(self, row: dict[str, str], source: SourcePalette) -> tuple[TextStyle, ...]:
        del source
        ordinal = row_ordinal(row)
        korean = (row.get("korean") or "").strip()
        font_size = 10 if korean == "생산" or ordinal == 50075 else 11
        return (
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=font_size,
                align="center",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        )


@dataclass(frozen=True)
class UI17LeftRule(GroupRule):
    def styles_for(self, row: dict[str, str], source: SourcePalette) -> tuple[TextStyle, ...]:
        del source
        outline_width = 1 if row.get("_ui17_left_outline") == "1" else 0
        return (
            TextStyle(
                font_role="nanum_gothic_bold",
                font_size=17,
                align="left",
                valign="center",
                preserve_newlines=False,
                wrap=False,
                shrink_overflow_x=True,
                outline_width=outline_width,
            ),
        )


FIXED_GROUP_RULES: dict[str, GroupRule] = {
    "적 연산중": GroupRule(
        name="적 연산중",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=27,
                align="left",
                valign="center",
                preserve_newlines=False,
                wrap=False,
                outline_width=1,
            ),
        ),
    ),
    "UI(승격)": GroupRule(
        name="UI(승격)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=21,
                align="left",
                valign="center",
                preserve_newlines=False,
                wrap=False,
                shadow_offset=(1, 1),
                shrink_overflow_x=True
            ),
        ),
    ),
    "UI(20)": GroupRule(
        name="UI(20)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_bold",
                font_size=19,
                align="left",
                valign="center",
                preserve_newlines=False,
                wrap=False,
                shrink_overflow_x=True,
            ),
        ),
    ),
    "UI(17/wy/중앙)": GroupRule(
        name="UI(17/wy/중앙)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_bold",
                font_size=17,
                align="center",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "UI(17/w/오)": GroupRule(
        name="UI(17/w/오)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_bold",
                font_size=17,
                align="center",
                valign="center",
                preserve_newlines=False,
                shrink_overflow_x=True,
                wrap=False,
                outline_width=1,
            ),
        ),
    ),
    "UI(17/좌)": UI17LeftRule(
        name="UI(17/좌)",
        styles=(),
    ),
    "UI(16/좌)": GroupRule(
        name="UI(16/좌)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_bold",
                font_size=17,
                align="left",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "UI(15)": GroupRule(
        name="UI(15)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=14,
                align="center",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "UI(14)": UI14Rule(
        name="UI(14)",
        styles=(),
    ),
    "UI(자금자원14)": GroupRule(
        name="UI(자금자원14)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=11,
                align="center",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "UI(유닛적성)": GroupRule(
        name="UI(유닛적성)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=13,
                align="left",
                valign="center",
                preserve_newlines=False,
                wrap=False,
                cell_split="words",
                cell_width=17,
            ),
        ),
    ),
    "UI(유닛스테이터스)": GroupRule(
        name="UI(유닛스테이터스)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=13,
                align="left",
                valign="center",
                preserve_newlines=True,
                wrap=False,
                cell_split="lines",
                cell_height=15,
            ),
        ),
    ),
    "칭호": GroupRule(
        name="칭호",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=14,
                align="center",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "지역이름": GroupRule(
        name="지역이름",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=21,
                align="left",
                valign="top",
                x_padding=11,
                y_padding=0,
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "지도자 이름": GroupRule(
        name="지도자 이름",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=13.5,
                align="center",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "세력 이름(23)": GroupRule(
        name="세력 이름(23)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=19,
                align="center",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "세력 이름(22)": GroupRule(
        name="세력 이름(22)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=19,
                align="center",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "세력 이름(16/납작)": GroupRule(
        name="세력 이름(16/납작)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=19,
                align="center",
                valign="center",
                preserve_newlines=False,
                wrap=False,
                shrink_overflow_y=True,
            ),
        ),
    ),
    "세력 이름_지도자 이름": GroupRule(
        name="세력 이름_지도자 이름",
        styles=(
            TextStyle(
                font_role="nanum_gothic_bold",
                font_size=18,
                align="center",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        ),
        background_source="top_left",
    ),
    "메모리카드": GroupRule(
        name="메모리카드",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=14,
                align="left",
                valign="left",
                preserve_newlines=True,
                wrap=False,
                shrink_overflow_y=True,
                shrink_overflow_x=True,
            ),
        ),
    ),
    "도감(DATABASE)": DatabaseRule(name="도감(DATABASE)", styles=()),
    "개발설명": GroupRule(
        name="개발설명",
        styles=(
            TextStyle(
                font_role="nanum_myeongjo_extrabold",
                font_size=13,
                align="left",
                valign="top",
                preserve_newlines=True,
                wrap=True,
            ),
        ),
    ),
    "예산투입": BudgetInputRule(name="예산투입", styles=()),
    "개발이름": GroupRule(
        name="개발이름",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=19,
                align="left",
                valign="center",
                x_padding_left=10,
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "기체 스테이터스 이름": GroupRule(
        name="기체 스테이터스 이름",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=19,
                align="left",
                valign="top",
                x_padding_left=10,
                preserve_newlines=False,
                wrap=False,
                line_index=0,
                shrink_overflow_x=True,
            ),
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=11,
                align="left",
                valign="bottom",
                x_padding_left=89,
                preserve_newlines=False,
                wrap=False,
                line_index=1,
                shrink_overflow_x=True,
                max_render_width=63,
            ),
        ),
    ),
    "유닛 스테이터스 이름": GroupRule(
        name="유닛 스테이터스 이름",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=19,
                align="left",
                valign="center",
                preserve_newlines=False,
                wrap=False,
                shrink_overflow_x=True,
            ),
        ),
    ),
    "특별플랜": GroupRule(
        name="특별플랜",
        styles=(
            TextStyle(
                font_role="nanum_gothic_bold",
                font_size=14,
                align="justify",
                valign="center",
                preserve_newlines=False,
                wrap=False,
                shrink_overflow_x=True,
                outline_width=1,
            ),
        ),
    ),
    "UI(17/bw/중앙)": GroupRule(
        name="UI(17/bw/중앙)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_bold",
                font_size=16,
                align="justify",
                valign="center",
                preserve_newlines=False,
                wrap=False,
                outline_width=1,
            ),
        ),
    ),
    "UI(17g)": GroupRule(
        name="UI(17g)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_bold",
                font_size=15,
                align="left",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "UI(외,명)": GroupRule(
        name="UI(외,명)",
        styles=(
            TextStyle(
                font_role="nanum_gothic_extrabold",
                font_size=10,
                align="left",
                valign="center",
                preserve_newlines=False,
                wrap=False,
            ),
        ),
    ),
    "시스템 메시지": SystemMessageRule(name="시스템 메시지", styles=()),
    "게임내 메뉴얼": GameManualRule(name="게임내 메뉴얼", styles=()),
    "부대수,사관수": UnitOfficerCountRule(name="부대수,사관수", styles=()),
    "UI(16/납작)": UI16FlatRule(name="UI(16/납작)", styles=()),
    "전투중표시": BattleDisplayRule(name="전투중표시", styles=()),
    "엔딩 텍스트": EndingTextRule(name="엔딩 텍스트", styles=()),
    "세력선택해주세요": FactionSelectPromptRule(name="세력선택해주세요", styles=()),
    "세력선택 문구": FactionSelectPhraseRule(name="세력선택 문구", styles=()),
    "인원합류탈퇴": JoinLeaveRule(name="인원합류탈퇴", styles=()),
}


GROUP_ALIASES: dict[str, str] = {
    "UI(17/오)": "UI(17/w/오)",
    "세력이름(23)": "세력 이름(23)",
    "세력이름(22)": "세력 이름(22)",
    "세력이름(16/납작)": "세력 이름(16/납작)",
    "세력이름_지도자이름": "세력 이름_지도자 이름",
}


NANUM_GOTHIC_TTC = Path(
    "/System/Library/AssetsV2/com_apple_MobileAsset_Font8/"
    "7a0b5c0f3c1d41c4c52a33343496c9c65ad52c50.asset/"
    "AssetData/NanumGothic.ttc"
)


FONT_ROLE_CANDIDATES: dict[str, tuple[tuple[Path, int, str], ...]] = {
    "nanum_gothic_regular": (
        (PROJECT_ROOT / "assets/fonts/NanumGothic.ttf", 0, "Nanum Gothic Regular"),
        (PROJECT_ROOT / "assets/fonts/NanumGothic-Regular.ttf", 0, "Nanum Gothic Regular"),
        (NANUM_GOTHIC_TTC, 0, "Nanum Gothic Regular"),
        (PROJECT_ROOT / "assets/fonts/NanumMyeongjoBold.ttf", 0, "NanumMyeongjoBold fallback"),
    ),
    "nanum_gothic_bold": (
        (PROJECT_ROOT / "assets/fonts/NanumGothicBold.ttf", 0, "Nanum Gothic Bold"),
        (PROJECT_ROOT / "assets/fonts/NanumGothic-Bold.ttf", 0, "Nanum Gothic Bold"),
        (NANUM_GOTHIC_TTC, 1, "Nanum Gothic Bold"),
        (PROJECT_ROOT / "assets/fonts/NanumMyeongjoBold.ttf", 0, "NanumMyeongjoBold fallback"),
    ),
    "nanum_gothic_extrabold": (
        (PROJECT_ROOT / "assets/fonts/NanumGothicExtraBold.ttf", 0, "Nanum Gothic ExtraBold"),
        (PROJECT_ROOT / "assets/fonts/NanumGothic-ExtraBold.ttf", 0, "Nanum Gothic ExtraBold"),
        (NANUM_GOTHIC_TTC, 2, "Nanum Gothic ExtraBold"),
        (PROJECT_ROOT / "assets/fonts/NanumMyeongjoBold.ttf", 0, "NanumMyeongjoBold fallback"),
    ),
    "nanum_myeongjo_extrabold": (
        (PROJECT_ROOT / "assets/fonts/NanumMyeongjoExtraBold.ttf", 0, "Nanum Myeongjo ExtraBold"),
        (PROJECT_ROOT / "assets/fonts/NanumMyeongjoBold.ttf", 0, "Nanum Myeongjo Bold fallback"),
        (PROJECT_ROOT / "assets/fonts/NanumGothicExtraBold.ttf", 0, "Nanum Gothic ExtraBold fallback"),
    ),
}


@dataclass
class RenderResult:
    status: str
    output: str
    target: Path
    width: int = 0
    height: int = 0
    font: str = ""
    font_size: float = 0
    line_count: int = 0
    x_scale: float = 1.0
    y_scale: float = 1.0
    changed_pixels: int = 0
    source_png_bytes: int = 0
    generated_png_bytes: int = 0
    message: str = ""


@dataclass(frozen=True)
class TextLineSlot:
    x: int
    y: int
    width: int
    height: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render Korean UI text with natural font metrics, then snap to the source PNG palette."
    )
    parser.add_argument("--csv", default="textures_static/manifest.csv")
    parser.add_argument("--textures-root", default="textures_static")
    parser.add_argument("--unpacked-root", default="unpacked_mkd")
    parser.add_argument("--out-root", default="textures_translated")
    parser.add_argument("--output-column", default="output")
    parser.add_argument("--text-column", default="korean")
    parser.add_argument("--target-verified-group", default="")
    parser.add_argument("--only", help="Render outputs containing this value.")
    parser.add_argument("--rows", "--row-range", dest="rows")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Overwrite PNGs under --textures-root.")
    parser.add_argument("--font", default="assets/fonts/NanumMyeongjoBold.ttf")
    parser.add_argument("--font-index", type=int, default=0)
    parser.add_argument("--font-size", type=int, default=16)
    parser.add_argument("--min-font-size", type=int, default=7)
    parser.add_argument("--x-padding", type=int, default=0)
    parser.add_argument("--y-padding", type=int, default=0)
    parser.add_argument("--line-spacing", type=int, default=0)
    parser.add_argument("--align", choices=("left", "center", "right"), default="center")
    parser.add_argument("--valign", choices=("top", "center", "bottom"), default="center")
    parser.add_argument("--preserve-newlines", action="store_true")
    parser.add_argument("--no-wrap", action="store_true")
    parser.add_argument(
        "--no-scale",
        action="store_true",
        help="Do not shrink rendered text after the minimum font size is reached.",
    )
    parser.add_argument("--min-x-scale", type=float, default=0.01)
    parser.add_argument("--min-y-scale", type=float, default=0.01)
    parser.add_argument("--report", help="Optional render report CSV.")
    parser.add_argument("--no-copy-manifest", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="Print ok/warning render progress and counts.")
    return parser.parse_args()


def normalize_text(value: str, preserve_newlines: bool) -> list[str]:
    text = unicodedata.normalize("NFC", value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if preserve_newlines:
        return [line for line in text.split("\n") if line]
    text = text.replace("\n", "")
    return [text] if text else []


def text_bbox(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont, text: str) -> tuple[int, int, int, int]:
    if not text:
        return (0, 0, 0, 0)
    return draw.textbbox((0, 0), text, font=font)


def text_width(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont, text: str) -> int:
    left, _top, right, _bottom = text_bbox(draw, font, text)
    return right - left


def font_line_height(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont) -> int:
    left, top, right, bottom = text_bbox(draw, font, "가힣Ay")
    del left, right
    return max(1, bottom - top)


def wrap_measured(
    text: str,
    max_width: int,
    preserve_newlines: bool,
    wrap: bool,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
) -> list[str]:
    paragraphs = normalize_text(text, preserve_newlines=preserve_newlines)
    if not wrap:
        return paragraphs
    lines: list[str] = []
    for paragraph in paragraphs:
        current = ""
        skip_auto_line_leading_whitespace = False
        for char in paragraph:
            if skip_auto_line_leading_whitespace:
                if char.isspace():
                    continue
                skip_auto_line_leading_whitespace = False
            candidate = current + char
            if not current or text_width(draw, font, candidate) <= max_width:
                current = candidate
                continue
            lines.append(current)
            if char.isspace():
                current = ""
                skip_auto_line_leading_whitespace = True
            else:
                current = char
        if current:
            lines.append(current)
    return lines


def palette_key(color: Color) -> int:
    red, green, blue, alpha = color
    return (red << 24) | (green << 16) | (blue << 8) | alpha


def palette_distance(a: Color, b: Color) -> int:
    return sum((a[index] - b[index]) ** 2 for index in range(4))


def unique_colors(image: Image.Image) -> list[tuple[int, Color]]:
    colors = image.getcolors(maxcolors=image.width * image.height + 1)
    if colors is not None:
        return [(count, color) for count, color in colors]
    counts: dict[Color, int] = {}
    for color in image.getdata():
        counts[color] = counts.get(color, 0) + 1
    return [(count, color) for color, count in counts.items()]


def luma(color: Color) -> float:
    red, green, blue, _alpha = color
    return 0.299 * red + 0.587 * green + 0.114 * blue


def is_blackish(color: Color) -> bool:
    red, green, blue, alpha = color
    return alpha > 0 and max(red, green, blue) <= 96 and luma(color) <= 80


def most_common_blackish(colors_with_counts: list[tuple[int, Color]]) -> Color | None:
    candidates = [
        (count, color)
        for count, color in colors_with_counts
        if is_blackish(color)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], -luma(item[1])))[1]


def source_palette_from_indexed(raw: Image.Image) -> tuple[list[Color], dict[Color, int]] | None:
    if raw.mode != "P":
        return None
    raw_palette = raw.getpalette()
    if not raw_palette:
        return None
    transparency = raw.info.get("transparency")
    alpha = [255] * 256
    if isinstance(transparency, int):
        if 0 <= transparency < len(alpha):
            alpha[transparency] = 0
    elif isinstance(transparency, bytes):
        for index, value in enumerate(transparency[:256]):
            alpha[index] = value

    colors: list[Color] = []
    for index in range(256):
        base = index * 3
        if base + 2 >= len(raw_palette):
            colors.append((0, 0, 0, 0))
            continue
        colors.append(
            (
                raw_palette[base],
                raw_palette[base + 1],
                raw_palette[base + 2],
                alpha[index],
            )
        )

    index_counts = raw.getcolors(maxcolors=raw.width * raw.height + 1) or []
    preferred: dict[Color, tuple[int, int]] = {}
    for count, index in index_counts:
        color = colors[index]
        previous = preferred.get(color)
        if previous is None or count > previous[0]:
            preferred[color] = (count, index)
    color_to_index = {color: index for color, (_count, index) in preferred.items()}
    for index, color in enumerate(colors):
        color_to_index.setdefault(color, index)
    return colors, color_to_index


def palette_from_manifest_record(row: dict[str, str], unpacked_root: Path) -> list[Color] | None:
    source = (row.get("source") or "").strip()
    tx_offset = (row.get("offset") or "").strip()
    palette_offset = (row.get("palette_offset") or "").strip()
    if not source or not tx_offset or not palette_offset:
        return None
    source_path = unpacked_root / source
    if not source_path.exists():
        return None
    try:
        tx_start = int(tx_offset, 0)
        offset = int(palette_offset, 0)
    except ValueError:
        return None
    blob = source_path.read_bytes()
    if tx_start < 0 or tx_start + 12 > len(blob) or blob[tx_start : tx_start + 4] != texture_dump.TX_MAGIC:
        return None
    tx_size = texture_dump.read_u32(blob, tx_start + 4)
    if tx_start + tx_size > len(blob):
        return None
    if offset < 0 or offset + 12 > len(blob) or blob[offset : offset + 4] != texture_dump.PL_MAGIC:
        return None
    size = texture_dump.read_u32(blob, offset + 4)
    if offset + size > len(blob):
        return None
    tx_segment = texture_dump.Segment(
        path=(row.get("tree_path") or ""),
        offset=tx_start,
        data=blob[tx_start : tx_start + tx_size],
        parent="",
        index=0,
    )
    pl_segment = texture_dump.Segment(
        path=f"{tx_segment.path}/pl",
        offset=offset,
        data=blob[offset : offset + size],
        parent="",
        index=0,
    )
    effective_pl = texture_dump.database_detail_rebuild_palette_override(
        source,
        tx_segment,
        pl_segment,
    ).data
    return texture_dump.parse_palette(
        effective_pl,
        palette_order=(row.get("palette_order") or "linear").strip() or "linear",
    )


def color_to_first_index(colors: list[Color]) -> dict[Color, int]:
    color_to_index: dict[Color, int] = {}
    for index, color in enumerate(colors):
        color_to_index.setdefault(color, index)
    return color_to_index


def inspect_source_png(
    path: Path,
    background_source: str = "palette_index_0",
    manifest_palette: list[Color] | None = None,
) -> SourcePalette:
    with Image.open(path) as raw:
        indexed_palette = source_palette_from_indexed(raw)
        image = raw.convert("RGBA")
    colors_with_counts = unique_colors(image)
    if not colors_with_counts:
        raise ValueError("source PNG has no colors")
    top_left = image.getpixel((0, 0))
    if background_source == "top_left":
        background = top_left
    elif background_source == "blackish":
        background = most_common_blackish(colors_with_counts) or top_left
    elif background_source == "palette_index_0":
        background = manifest_palette[0] if manifest_palette else top_left
    else:
        raise ValueError(f"unknown background source: {background_source}")
    if manifest_palette:
        colors = list(manifest_palette)
        color_to_index = color_to_first_index(colors)
    elif indexed_palette:
        colors, color_to_index = indexed_palette
    else:
        colors = [background] + [
            color for _count, color in colors_with_counts if color != background
        ]
        color_to_index = {color: index for index, color in enumerate(colors)}
    foreground_candidates = [
        (count, color)
        for count, color in colors_with_counts
        if color[3] > 0 and color != background
    ]
    if foreground_candidates:
        foreground = max(foreground_candidates, key=lambda item: (luma(item[1]), item[0]))[1]
    else:
        foreground = max(colors_with_counts, key=lambda item: luma(item[1]))[1]
    return SourcePalette(
        width=image.width,
        height=image.height,
        colors=colors,
        background=background,
        foreground=foreground,
        color_to_index=color_to_index,
    )


def snap_rgba_to_palette(image: Image.Image, palette: Iterable[Color]) -> tuple[Image.Image, int]:
    target_palette = list(palette)
    if not target_palette:
        return image, 0
    palette_keys = {palette_key(color) for color in target_palette}
    transparent = next((color for color in target_palette if color[3] == 0), target_palette[0])
    opaque_palette = [color for color in target_palette if color[3] != 0] or target_palette
    snapped = image.convert("RGBA")
    pixels = snapped.load()
    changed = 0
    for y in range(snapped.height):
        for x in range(snapped.width):
            color = pixels[x, y]
            if palette_key(color) in palette_keys:
                continue
            replacement = transparent if color[3] == 0 else min(
                opaque_palette,
                key=lambda item: palette_distance(color, item),
            )
            pixels[x, y] = replacement
            changed += 1
    return snapped, changed


def render_lines_mask(
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    line_spacing: int,
) -> tuple[Image.Image, int, int]:
    scratch = Image.new("L", (1, 1), 0)
    draw = ImageDraw.Draw(scratch)
    boxes = [text_bbox(draw, font, line) for line in lines]
    line_height = max(
        font_line_height(draw, font),
        max((bottom - top for _left, top, _right, bottom in boxes), default=1),
    )
    widths = [right - left for left, _top, right, _bottom in boxes]
    width = max(1, max(widths, default=1))
    height = max(1, line_height * len(lines) + line_spacing * max(0, len(lines) - 1))
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    y = 0
    for line, box in zip(lines, boxes):
        left, top, right, bottom = box
        del right, bottom
        draw.text((-left, y - top), line, font=font, fill=255)
        y += line_height + line_spacing
    bbox = mask.getbbox()
    if bbox is None:
        return Image.new("L", (1, 1), 0), 1, 1
    cropped = mask.crop(bbox)
    return cropped, cropped.width, cropped.height


def make_fit_plan(
    text: str,
    width: int,
    height: int,
    args: argparse.Namespace,
    font_path: Path,
) -> FitPlan:
    available_width = max(1, width - args.x_padding * 2)
    available_height = max(1, height - args.y_padding * 2)
    scratch = Image.new("L", (1, 1), 0)
    draw = ImageDraw.Draw(scratch)
    fallback_lines: list[str] = []
    fallback_size = args.min_font_size
    fallback_width = 1
    fallback_height = 1

    for size in range(args.font_size, args.min_font_size - 1, -1):
        font = load_font(font_path, size, args.font_index)
        lines = wrap_measured(
            text,
            max_width=available_width,
            preserve_newlines=args.preserve_newlines,
            wrap=not args.no_wrap,
            draw=draw,
            font=font,
        )
        if not lines:
            continue
        _mask, natural_width, natural_height = render_lines_mask(lines, font, args.line_spacing)
        fallback_lines = lines
        fallback_size = size
        fallback_width = natural_width
        fallback_height = natural_height
        if natural_width <= available_width and natural_height <= available_height:
            return FitPlan(
                lines=lines,
                font_size=size,
                x_scale=1.0,
                y_scale=1.0,
                natural_width=natural_width,
                natural_height=natural_height,
                rendered_width=natural_width,
                rendered_height=natural_height,
                font_reduced=size != args.font_size,
                scaled=False,
            )

    if args.no_scale:
        x_scale = y_scale = 1.0
    else:
        x_scale = min(1.0, available_width / max(1, fallback_width))
        y_scale = min(1.0, available_height / max(1, fallback_height))
        x_scale = max(args.min_x_scale, x_scale)
        y_scale = max(args.min_y_scale, y_scale)
    return FitPlan(
        lines=fallback_lines,
        font_size=fallback_size,
        x_scale=x_scale,
        y_scale=y_scale,
        natural_width=fallback_width,
        natural_height=fallback_height,
        rendered_width=max(1, round(fallback_width * x_scale)),
        rendered_height=max(1, round(fallback_height * y_scale)),
        font_reduced=fallback_size != args.font_size,
        scaled=x_scale != 1.0 or y_scale != 1.0,
    )


def paste_position(
    canvas_width: int,
    canvas_height: int,
    image_width: int,
    image_height: int,
    args: argparse.Namespace,
) -> tuple[int, int]:
    if args.align == "left":
        x = args.x_padding
    elif args.align == "right":
        x = canvas_width - args.x_padding - image_width
    else:
        x = (canvas_width - image_width) // 2
    if args.valign == "top":
        y = args.y_padding
    elif args.valign == "bottom":
        y = canvas_height - args.y_padding - image_height
    else:
        y = (canvas_height - image_height) // 2
    return max(0, x), max(0, y)


def paste_position_for_style(
    canvas_width: int,
    canvas_height: int,
    image_width: int,
    image_height: int,
    style: TextStyle,
) -> tuple[int, int]:
    left_padding, right_padding = horizontal_paddings(style)
    if style.align == "left" or style.align == "justify":
        x = left_padding
    elif style.align == "right":
        x = canvas_width - right_padding - image_width
    else:
        x = left_padding + (canvas_width - left_padding - right_padding - image_width) // 2
    if style.valign == "top":
        y = style.y_padding
    elif style.valign == "bottom":
        y = canvas_height - style.y_padding - image_height
    else:
        y = (canvas_height - image_height) // 2
    return max(0, x), max(0, y)


def horizontal_paddings(style: TextStyle) -> tuple[int, int]:
    left = style.x_padding if style.x_padding_left is None else style.x_padding_left
    right = style.x_padding if style.x_padding_right is None else style.x_padding_right
    return max(0, left), max(0, right)


def fixed_font_for(style: TextStyle) -> tuple[Path, int, str]:
    for path, index, label in FONT_ROLE_CANDIDATES.get(style.font_role, ()):
        if path.exists():
            return path, index, label
    fallback = PROJECT_ROOT / "assets/fonts/NanumMyeongjoBold.ttf"
    return fallback, 0, "NanumMyeongjoBold fallback"


def fixed_font_render_size(font_size: float) -> tuple[int, float]:
    fraction = Fraction(str(font_size)).limit_denominator(8)
    render_size = max(1, int(fraction.numerator))
    downsample_scale = 1.0 / max(1, fraction.denominator)
    return render_size, downsample_scale


def split_text_for_style(text: str, style: TextStyle) -> str:
    normalized = unicodedata.normalize("NFC", text or "")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    if style.line_index is None:
        return normalized
    lines = [line for line in normalized.split("\n") if line]
    if style.line_index >= len(lines):
        return ""
    return lines[style.line_index]


def render_justified_mask(
    line: str,
    font: ImageFont.FreeTypeFont,
    target_width: int,
    valign: str = "center",
) -> tuple[Image.Image, int, int]:
    scratch = Image.new("L", (1, 1), 0)
    draw = ImageDraw.Draw(scratch)
    chars = [char for char in line if char]
    if not chars:
        return Image.new("L", (1, 1), 0), 1, 1
    line_height = font_line_height(draw, font)
    boxes = [text_bbox(draw, font, char) for char in chars]
    widths = [max(1, right - left) for left, _top, right, _bottom in boxes]
    natural_width = sum(widths)
    width = max(1, natural_width if natural_width >= target_width else target_width)
    mask = Image.new("L", (width, line_height), 0)
    draw = ImageDraw.Draw(mask)
    if len(chars) == 1 or natural_width >= width:
        x = max(0, (width - natural_width) // 2)
        gaps = [0.0] * max(0, len(chars) - 1)
    else:
        x = 0
        gap = (width - natural_width) / (len(chars) - 1)
        gaps = [gap] * (len(chars) - 1)
    current_x = float(x)
    for index, (char, box, char_width) in enumerate(zip(chars, boxes, widths)):
        left, top, _right, bottom = box
        char_height = max(1, bottom - top)
        if valign == "bottom":
            y = line_height - char_height
        elif valign == "top":
            y = 0
        else:
            y = (line_height - char_height) // 2
        draw.text((round(current_x) - left, y - top), char, font=font, fill=255)
        current_x += char_width
        if index < len(gaps):
            current_x += gaps[index]
    bbox = mask.getbbox()
    if bbox is None:
        return Image.new("L", (1, 1), 0), 1, 1
    cropped = mask.crop((0, bbox[1], mask.width, bbox[3]))
    return cropped, cropped.width, cropped.height


def render_cell_text_mask(
    text: str,
    font: ImageFont.FreeTypeFont,
    style: TextStyle,
    available_width: int,
    available_height: int,
    downsample_scale: float,
) -> tuple[Image.Image, int]:
    render_width = max(1, round(available_width / downsample_scale))
    render_height = max(1, round(available_height / downsample_scale))
    if style.cell_split == "words":
        cells = normalize_text(text, preserve_newlines=False)[0].split()
    elif style.cell_split == "lines":
        cells = normalize_text(text, preserve_newlines=True)
    else:
        return Image.new("L", (1, 1), 0), 0
    if not cells:
        return Image.new("L", (1, 1), 0), 0

    cell_width = max(1, round((style.cell_width or available_width) / downsample_scale))
    cell_height = max(1, round((style.cell_height or available_height) / downsample_scale))
    mask = Image.new("L", (render_width, render_height), 0)
    for index, cell_text in enumerate(cells):
        cell_mask, _cell_natural_width, _cell_natural_height = render_lines_mask(
            [cell_text],
            font,
            0,
        )
        max_width = cell_width if style.cell_width else render_width
        max_height = cell_height if style.cell_height else render_height
        if cell_mask.width > max_width:
            cell_mask = cell_mask.resize(
                (max_width, cell_mask.height),
                Image.Resampling.LANCZOS,
            )
        if cell_mask.height > max_height:
            cell_mask = cell_mask.resize(
                (cell_mask.width, max_height),
                Image.Resampling.LANCZOS,
            )
        if style.cell_split == "words":
            x = index * cell_width
            y = max(0, (render_height - cell_mask.height) // 2)
        else:
            x = 0
            y = index * cell_height + max(0, (cell_height - cell_mask.height) // 2)
        if x >= render_width or y >= render_height:
            continue
        clipped = cell_mask.crop(
            (
                0,
                0,
                min(cell_mask.width, render_width - x),
                min(cell_mask.height, render_height - y),
            )
        )
        mask.paste(clipped, (x, y), mask=clipped)

    if downsample_scale != 1.0:
        mask = mask.resize(
            (
                max(1, round(mask.width * downsample_scale)),
                max(1, round(mask.height * downsample_scale)),
            ),
            Image.Resampling.LANCZOS,
        )
    return mask, len(cells)


def bracketed_text_spans(text: str) -> list[tuple[str, bool]]:
    spans: list[tuple[str, bool]] = []
    position = 0
    for match in re.finditer(r"\[[^\[\]]*\]|\([^()]*\)", text):
        if match.start() > position:
            spans.append((text[position : match.start()], False))
        spans.append((match.group(0), True))
        position = match.end()
    if position < len(text):
        spans.append((text[position:], False))
    return spans


def render_bracketed_inline_mask(
    text: str,
    font_path: Path,
    font_index: int,
    render_font_size: int,
    downsample_scale: float,
    font_size_delta: float,
) -> Image.Image | None:
    spans = bracketed_text_spans(text)
    if not any(is_bracketed for _span, is_bracketed in spans):
        return None

    bracketed_render_size = max(
        1,
        round(render_font_size + font_size_delta / downsample_scale),
    )
    fonts = {
        False: load_font(font_path, render_font_size, font_index),
        True: load_font(font_path, bracketed_render_size, font_index),
    }
    scratch = Image.new("L", (1, 1), 0)
    draw = ImageDraw.Draw(scratch)
    baseline = max(font.getmetrics()[0] for font in fonts.values())
    x = 0.0
    positioned: list[tuple[float, str, ImageFont.FreeTypeFont]] = []
    boxes: list[tuple[int, int, int, int]] = []
    for span, is_bracketed in spans:
        if not span:
            continue
        font = fonts[is_bracketed]
        positioned.append((x, span, font))
        boxes.append(draw.textbbox((x, baseline), span, font=font, anchor="ls"))
        x += draw.textlength(span, font=font)

    if not boxes:
        return Image.new("L", (1, 1), 0)
    left = math.floor(min(box[0] for box in boxes))
    top = math.floor(min(box[1] for box in boxes))
    right = math.ceil(max(box[2] for box in boxes))
    bottom = math.ceil(max(box[3] for box in boxes))
    mask = Image.new("L", (max(1, right - left), max(1, bottom - top)), 0)
    draw = ImageDraw.Draw(mask)
    for span_x, span, font in positioned:
        draw.text((span_x - left, baseline - top), span, font=font, fill=255, anchor="ls")
    bbox = mask.getbbox()
    if bbox is not None:
        mask = mask.crop(bbox)
    if downsample_scale != 1.0:
        mask = mask.resize(
            (
                max(1, round(mask.width * downsample_scale)),
                max(1, round(mask.height * downsample_scale)),
            ),
            Image.Resampling.LANCZOS,
        )
    return mask


def render_style_mask(
    text: str,
    style: TextStyle,
    width: int,
    height: int,
) -> tuple[Image.Image, str, float, int, int]:
    font_path, font_index, font_label = fixed_font_for(style)
    render_font_size, downsample_scale = fixed_font_render_size(style.font_size)
    font = load_font(font_path, render_font_size, font_index)
    outline_width = max(0, style.outline_width)
    left_padding, right_padding = horizontal_paddings(style)
    available_width = max(1, width - left_padding - right_padding - outline_width * 2)
    selected_text = split_text_for_style(text, style)
    if not selected_text.strip():
        return Image.new("L", (1, 1), 0), font_label, style.font_size, 0, 0
    available_height = max(1, height - style.y_padding * 2 - outline_width * 2)
    if style.cell_split:
        mask, cell_count = render_cell_text_mask(
            selected_text,
            font,
            style,
            available_width,
            available_height,
            downsample_scale,
        )
        return mask, font_label, style.font_size, cell_count, max(mask.width, mask.height)
    if style.align == "justify":
        line = normalize_text(selected_text, preserve_newlines=False)[0]
        render_width = max(1, round(available_width / downsample_scale))
        mask, natural_width, natural_height = render_justified_mask(
            line,
            font,
            render_width,
            style.valign,
        )
        if downsample_scale != 1.0:
            mask = mask.resize(
                (
                    max(1, round(mask.width * downsample_scale)),
                    max(1, round(mask.height * downsample_scale)),
                ),
                Image.Resampling.LANCZOS,
            )
            natural_width = mask.width
            natural_height = mask.height
        return mask, font_label, style.font_size, natural_width, natural_height

    if style.bracketed_font_size_delta:
        inline_text = normalize_text(selected_text, preserve_newlines=False)[0]
        mask = render_bracketed_inline_mask(
            inline_text,
            font_path,
            font_index,
            render_font_size,
            downsample_scale,
            style.bracketed_font_size_delta,
        )
        if mask is not None:
            return mask, font_label, style.font_size, 1, max(mask.width, mask.height)

    scratch = Image.new("L", (1, 1), 0)
    draw = ImageDraw.Draw(scratch)
    lines = wrap_measured(
        selected_text,
        max_width=max(1, round(available_width / downsample_scale)),
        preserve_newlines=style.preserve_newlines,
        wrap=style.wrap,
        draw=draw,
        font=font,
    )
    mask, natural_width, natural_height = render_lines_mask(lines, font, style.line_spacing)
    if downsample_scale != 1.0:
        mask = mask.resize(
            (
                max(1, round(mask.width * downsample_scale)),
                max(1, round(mask.height * downsample_scale)),
            ),
            Image.Resampling.LANCZOS,
        )
        natural_width = mask.width
        natural_height = mask.height
    return mask, font_label, style.font_size, len(lines), max(natural_width, natural_height)


def fixed_rule_for_group(group: str) -> GroupRule | None:
    return FIXED_GROUP_RULES.get(GROUP_ALIASES.get(group, group))


def canonical_group(group: str) -> str:
    return GROUP_ALIASES.get(group, group)


def row_ordinal(row: dict[str, str]) -> int | None:
    try:
        return int(row.get("ordinal") or "", 0)
    except ValueError:
        return None


def mark_ui17_left_outline_rows(rows: list[dict[str, str]], limit: int = 6) -> None:
    ranked: list[tuple[int, int, dict[str, str]]] = []
    for index, row in enumerate(rows):
        if canonical_group((row.get("verified_group") or "").strip()) != "UI(17/좌)":
            continue
        ordinal = row_ordinal(row)
        if ordinal is None:
            continue
        ranked.append((ordinal, index, row))

    outlined = {id(row) for _ordinal, _index, row in sorted(ranked)[:limit]}
    for row in rows:
        if id(row) in outlined:
            row["_ui17_left_outline"] = "1"
        else:
            row.pop("_ui17_left_outline", None)


def text_effect_color(source: SourcePalette) -> Color:
    candidates = [
        color
        for color in source.colors
        if color[3] > 0 and color != source.foreground
    ]
    blackish = [color for color in candidates if is_blackish(color)]
    if blackish:
        return min(blackish, key=lambda color: (luma(color), color))
    if candidates:
        return min(candidates, key=lambda color: (luma(color), color))
    if source.background[3] > 0:
        return source.background
    if source.foreground[3] > 0:
        return source.foreground
    return (0, 0, 0, 255)


def text_outline_color(source: SourcePalette) -> Color | None:
    candidates = [
        color
        for color in dict.fromkeys(source.colors)
        if color[3] > 0
        and color != source.background
        and color != source.foreground
    ]
    blackish = [color for color in candidates if is_blackish(color)]
    if blackish:
        return min(blackish, key=lambda color: (luma(color), color))
    if candidates:
        return min(candidates, key=lambda color: (luma(color), color))
    return None


def paste_masked_color(
    canvas: Image.Image,
    mask: Image.Image,
    color: Color,
    xy: tuple[int, int],
) -> None:
    x, y = xy
    left = max(0, -x)
    top = max(0, -y)
    right = min(mask.width, canvas.width - x)
    bottom = min(mask.height, canvas.height - y)
    if right <= left or bottom <= top:
        return
    cropped_mask = mask.crop((left, top, right, bottom))
    layer = Image.new("RGBA", cropped_mask.size, color)
    canvas.paste(layer, (x + left, y + top), mask=cropped_mask)


def rgb_hex_color(value: str) -> Color:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"expected RRGGBB color: {value}")
    return (
        int(value[0:2], 16),
        int(value[2:4], 16),
        int(value[4:6], 16),
        255,
    )


def relative_box(
    width: int,
    height: int,
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> tuple[int, int, int, int]:
    x0 = width + left if left < 0 else left
    y0 = height + top if top < 0 else top
    x1 = width + right if right < 0 else right
    y1 = height + bottom if bottom < 0 else bottom
    x0 = min(max(0, x0), width)
    y0 = min(max(0, y0), height)
    x1 = min(max(0, x1), width)
    y1 = min(max(0, y1), height)
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return x0, y0, x1, y1


def normalize_preserved_lines(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFC", text or "")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n")


def wrap_preserving_linebreaks(
    lines: list[str],
    max_width: int,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
) -> list[str]:
    wrapped: list[str] = []
    for line in lines:
        if line == "":
            wrapped.append("")
            continue
        current = ""
        for char in line:
            candidate = current + char
            if not current or text_width(draw, font, candidate) <= max_width:
                current = candidate
                continue
            wrapped.append(current)
            current = char
        wrapped.append(current)
    return wrapped


def render_area_text_mask(
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    width: int,
    height: int,
    line_spacing: int = 0,
    blank_line_height_scale: float = 1.0,
) -> tuple[Image.Image, int, bool]:
    mask = Image.new("L", (max(1, width), max(1, height)), 0)
    draw = ImageDraw.Draw(mask)
    line_height = font_line_height(draw, font)
    y = 0
    for line in lines:
        if line:
            left, top, _right, _bottom = text_bbox(draw, font, line)
            draw.text((-left, y - top), line, font=font, fill=255)
            y += line_height + line_spacing
            continue
        y += max(1, round(line_height * blank_line_height_scale)) + line_spacing
    overflow = y - line_spacing > height if lines else False
    return mask, len(lines), overflow


def render_system_message_title_mask(
    title: str,
    font: ImageFont.FreeTypeFont,
    width: int,
    height: int,
) -> tuple[Image.Image, float]:
    mask, _natural_width, _natural_height = render_lines_mask([title], font, 0)
    x_scale = 1.0
    if mask.width > width:
        x_scale = width / max(1, mask.width)
        mask = mask.resize((max(1, width), mask.height), Image.Resampling.LANCZOS)
    if mask.height > height:
        top = max(0, (mask.height - height) // 2)
        mask = mask.crop((0, top, mask.width, top + height))
    return mask, x_scale


def render_shrink_to_width_line_mask(
    line: str,
    font: ImageFont.FreeTypeFont,
    width: int,
    height: int,
) -> tuple[Image.Image, float]:
    mask, _natural_width, _natural_height = render_lines_mask([line], font, 0)
    x_scale = 1.0
    if mask.width > width:
        x_scale = width / max(1, mask.width)
        mask = mask.resize((max(1, width), mask.height), Image.Resampling.LANCZOS)
    if mask.height > height:
        top = max(0, (mask.height - height) // 2)
        mask = mask.crop((0, top, mask.width, top + height))
    return mask, x_scale


def split_system_message_text(text: str) -> tuple[str, list[str]]:
    lines = normalize_preserved_lines(text)
    title = lines[0].strip() if lines else ""
    if title == "MESSAGE":
        return "", lines[1:]
    return title, lines[1:]


def split_title_content_text(text: str, skip_marker_title: str | None = None) -> tuple[str, list[str]]:
    lines = normalize_preserved_lines(text)
    title = lines[0].strip() if lines else ""
    content_lines = lines[1:]
    if skip_marker_title is not None and title == skip_marker_title:
        title = ""
    while content_lines and content_lines[0] == "":
        content_lines.pop(0)
    return title, content_lines


def brightest_opaque_color(image: Image.Image) -> Color:
    colors = unique_colors(image.convert("RGBA"))
    opaque = [color for _count, color in colors if color[3] > 0]
    if not opaque:
        return (255, 255, 255, 255)
    return max(opaque, key=lambda color: (luma(color), color))


def brightest_opaque_color_in_boxes(
    image: Image.Image,
    boxes: Iterable[tuple[int, int, int, int]],
) -> Color:
    brightest: Color | None = None
    for box in boxes:
        cropped = image.crop(box)
        for _count, color in unique_colors(cropped):
            if color[3] == 0:
                continue
            if brightest is None or (luma(color), color) > (luma(brightest), brightest):
                brightest = color
    return brightest or (255, 255, 255, 255)


def brightest_whiteish_opaque_color_in_boxes(
    image: Image.Image,
    boxes: Iterable[tuple[int, int, int, int]],
) -> Color:
    candidates: list[Color] = []
    for box in boxes:
        cropped = image.crop(box)
        for _count, color in unique_colors(cropped):
            red, green, blue, alpha = color
            if alpha == 0:
                continue
            if min(red, green, blue) >= 128 and max(red, green, blue) - min(red, green, blue) <= 48:
                candidates.append(color)
    if candidates:
        return max(candidates, key=lambda color: (luma(color), color))
    return brightest_opaque_color_in_boxes(image, boxes)


def darkest_opaque_color_in_boxes(
    image: Image.Image,
    boxes: Iterable[tuple[int, int, int, int]],
) -> Color:
    darkest: Color | None = None
    for box in boxes:
        cropped = image.crop(box)
        for _count, color in unique_colors(cropped):
            if color[3] == 0:
                continue
            if darkest is None or (luma(color), color) < (luma(darkest), darkest):
                darkest = color
    return darkest or (0, 0, 0, 255)


def colors_in_boxes(
    image: Image.Image,
    boxes: Iterable[tuple[int, int, int, int]],
) -> list[Color]:
    seen: set[Color] = set()
    colors: list[Color] = []
    for box in boxes:
        cropped = image.crop(box)
        for _count, color in unique_colors(cropped):
            if color in seen:
                continue
            seen.add(color)
            colors.append(color)
    return colors


def snap_rgba_boxes_to_palette(
    image: Image.Image,
    boxes: Iterable[tuple[int, int, int, int]],
    palette: Iterable[Color],
) -> tuple[Image.Image, int]:
    target_palette = list(palette)
    if not target_palette:
        return image, 0
    palette_keys = {palette_key(color) for color in target_palette}
    transparent = next((color for color in target_palette if color[3] == 0), target_palette[0])
    opaque_palette = [color for color in target_palette if color[3] != 0] or target_palette
    snapped = image.convert("RGBA")
    pixels = snapped.load()
    changed = 0
    for box in boxes:
        x0, y0, x1, y1 = relative_box(snapped.width, snapped.height, *box)
        for y in range(y0, y1):
            for x in range(x0, x1):
                color = pixels[x, y]
                if palette_key(color) in palette_keys:
                    continue
                replacement = transparent if color[3] == 0 else min(
                    opaque_palette,
                    key=lambda item: palette_distance(color, item),
                )
                pixels[x, y] = replacement
                changed += 1
    return snapped, changed


def outline_mask(mask: Image.Image, width: int) -> Image.Image:
    if width <= 0:
        return Image.new("L", mask.size, 0)
    padded = Image.new("L", (mask.width + width * 2, mask.height + width * 2), 0)
    padded.paste(mask, (width, width))
    expanded = padded.filter(ImageFilter.MaxFilter(width * 2 + 1))
    outline = ImageChops.subtract(expanded, padded)
    return outline.point(lambda value: 0 if value < 12 else min(255, round(value * 1.35)))


def alpha_bbox(image: Image.Image) -> tuple[int, int, int, int]:
    return image.getchannel("A").getbbox() or (0, 0, image.width, image.height)


def bright_text_line_slots(image: Image.Image) -> list[TextLineSlot]:
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    rows: list[tuple[int, int, int]] = []
    for y in range(rgba.height):
        xs = [
            x
            for x in range(rgba.width)
            if pixels[x, y][3] > 0 and luma(pixels[x, y]) > 128
        ]
        if xs:
            rows.append((y, min(xs), max(xs) + 1))

    if not rows:
        left, top, right, bottom = alpha_bbox(rgba)
        return [TextLineSlot(left, top, max(1, right - left), max(1, bottom - top))]

    grouped: list[tuple[int, int, int, int]] = []
    start_y = prev_y = rows[0][0]
    group_lefts = [rows[0][1]]
    group_rights = [rows[0][2]]
    for y, left, right in rows[1:]:
        if y == prev_y + 1:
            prev_y = y
            group_lefts.append(left)
            group_rights.append(right)
            continue
        grouped.append((start_y, prev_y + 1, min(group_lefts), max(group_rights)))
        start_y = prev_y = y
        group_lefts = [left]
        group_rights = [right]
    grouped.append((start_y, prev_y + 1, min(group_lefts), max(group_rights)))

    slots: list[TextLineSlot] = []
    for top, bottom, bright_left, bright_right in grouped:
        alpha_xs = [
            x
            for y in range(top, bottom)
            for x in range(rgba.width)
            if pixels[x, y][3] > 0
        ]
        if alpha_xs:
            left = min(alpha_xs)
            right = max(alpha_xs) + 1
        else:
            left = bright_left
            right = bright_right
        slots.append(TextLineSlot(left, top, max(1, right - left), max(1, bottom - top)))
    return slots


def clear_existing_alpha_to_background(image: Image.Image, background: Color) -> Image.Image:
    cleared = image.convert("RGBA")
    alpha = cleared.getchannel("A")
    background_layer = Image.new("RGBA", cleared.size, background)
    cleared.paste(background_layer, (0, 0), alpha)
    return cleared


def paste_masked_color_clipped_to_alpha(
    canvas: Image.Image,
    mask: Image.Image,
    color: Color,
    xy: tuple[int, int],
    clip_alpha: Image.Image,
) -> None:
    x, y = xy
    left = max(0, -x)
    top = max(0, -y)
    right = min(mask.width, canvas.width - x, clip_alpha.width - x)
    bottom = min(mask.height, canvas.height - y, clip_alpha.height - y)
    if right <= left or bottom <= top:
        return
    cropped_mask = mask.crop((left, top, right, bottom))
    clip = clip_alpha.crop((x + left, y + top, x + right, y + bottom))
    combined_mask = ImageChops.multiply(cropped_mask, clip)
    if combined_mask.getbbox() is None:
        return
    layer = Image.new("RGBA", combined_mask.size, color)
    canvas.paste(layer, (x + left, y + top), mask=combined_mask)


def split_ending_text(text: str) -> tuple[str, list[str]]:
    lines = normalize_preserved_lines(text)
    for index, line in enumerate(lines):
        if line.strip():
            return line.strip(), lines[index + 1 :]
    return "", []


def split_line_for_slot(
    text: str,
    max_width: int,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
) -> tuple[str, str]:
    if text_width(draw, font, text) <= max_width:
        return text, ""

    best = 1
    for index in range(1, len(text) + 1):
        candidate = text[:index]
        if text_width(draw, font, candidate) <= max_width:
            best = index
            continue
        break

    prefix = text[:best]
    break_at = max(prefix.rfind(" "), prefix.rfind("\u3000"))
    if break_at > 0 and break_at >= max(1, best // 3):
        return text[:break_at].rstrip(), text[break_at + 1 :].lstrip()
    return prefix.rstrip(), text[best:].lstrip()


def wrap_ending_lines_to_slots(
    lines: list[str],
    slots: list[TextLineSlot],
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
) -> tuple[list[str], int]:
    wrapped: list[str] = []
    overflow = 0
    for source_line in lines:
        remaining = source_line.strip()
        if not remaining:
            continue
        while remaining:
            if len(wrapped) >= len(slots):
                overflow += 1
                break
            slot = slots[len(wrapped)]
            current, remaining = split_line_for_slot(remaining, slot.width, draw, font)
            if current:
                wrapped.append(current)
            elif remaining:
                wrapped.append(remaining[:1])
                remaining = remaining[1:].lstrip()
    return wrapped, overflow


def paste_ending_line(
    image: Image.Image,
    clip_alpha: Image.Image,
    line: str,
    slot: TextLineSlot,
    font: ImageFont.FreeTypeFont,
    color: Color,
    align: str,
    outline_color: Color | None = None,
    outline_width: int = 0,
) -> tuple[float, float]:
    outline_width = max(0, outline_width)
    mask, _natural_width, _natural_height = render_lines_mask([line], font, 0)
    x_scale = 1.0
    y_scale = 1.0
    available_width = max(1, slot.width - outline_width * 2)
    # Ending slots are often 15px tall; keep the glyph body in-slot and let outline clip.
    available_height = max(1, slot.height)
    if mask.width > available_width:
        x_scale = available_width / max(1, mask.width)
        mask = mask.resize((available_width, mask.height), Image.Resampling.LANCZOS)
    if mask.height > available_height:
        y_scale = available_height / max(1, mask.height)
        mask = mask.resize((mask.width, available_height), Image.Resampling.LANCZOS)
    footprint_width = mask.width + outline_width * 2
    if align == "center":
        footprint_x = slot.x + max(0, (slot.width - footprint_width) // 2)
    elif align == "right":
        footprint_x = slot.x + max(0, slot.width - footprint_width)
    else:
        footprint_x = slot.x
    y = slot.y + max(0, (slot.height - mask.height) // 2)
    x = footprint_x + outline_width
    if outline_color is not None and outline_width > 0:
        paste_masked_color_clipped_to_alpha(
            image,
            outline_mask(mask, outline_width),
            outline_color,
            (x - outline_width, y - outline_width),
            clip_alpha,
        )
    paste_masked_color_clipped_to_alpha(image, mask, color, (x, y), clip_alpha)
    return x_scale, y_scale


def render_one_ending_text(
    row: dict[str, str],
    args: argparse.Namespace,
    rule: EndingTextRule,
) -> RenderResult:
    del rule
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    with Image.open(source_path) as raw:
        source_image = raw.convert("RGBA")

    image = clear_existing_alpha_to_background(source_image, source.background)
    clip_alpha = source_image.getchannel("A")
    slots = bright_text_line_slots(source_image)

    font_path, font_index, font_label = fixed_font_for(
        TextStyle(font_role="nanum_myeongjo_extrabold", font_size=14)
    )
    font = load_font(font_path, 14, font_index)
    title, content_source_lines = split_ending_text(row.get(args.text_column) or "")

    messages: list[str] = []
    x_scales: list[float] = []
    y_scales: list[float] = []
    line_count = 0
    scratch = Image.new("L", (1, 1), 0)
    draw = ImageDraw.Draw(scratch)

    if title and slots:
        x_scale, y_scale = paste_ending_line(
            image,
            clip_alpha,
            title,
            slots[0],
            font,
            source.foreground,
            "center",
            outline_color=text_outline_color(source),
            outline_width=1,
        )
        x_scales.append(x_scale)
        y_scales.append(y_scale)
        line_count += 1

    content_slots = slots[1:] if title else slots
    content_lines, overflow = wrap_ending_lines_to_slots(
        content_source_lines,
        content_slots,
        draw,
        font,
    )
    if overflow:
        messages.append(f"content overflow lines={overflow}")
    if len(content_lines) < len([line for line in content_source_lines if line.strip()]):
        messages.append("content truncated")
    if line_count + len(content_lines) != len(slots):
        messages.append(f"line slots {line_count + len(content_lines)}/{len(slots)}")

    for line, slot in zip(content_lines, content_slots):
        x_scale, y_scale = paste_ending_line(
            image,
            clip_alpha,
            line,
            slot,
            font,
            source.foreground,
            "left",
            outline_color=text_outline_color(source),
            outline_width=1,
        )
        x_scales.append(x_scale)
        y_scales.append(y_scale)
        line_count += 1
        if x_scale != 1.0:
            messages.append(f"scaled x={x_scale:.3f}")
        if y_scale < 0.85:
            messages.append(f"scaled y={y_scale:.3f}")

    if line_count == 0:
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=source.width,
            height=source.height,
            source_png_bytes=source_bytes,
            message="empty text",
        )

    image, changed_pixels = snap_rgba_to_palette(image, source.colors)
    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    scaled = [scale for scale in x_scales if scale != 1.0]
    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font=font_label,
        font_size=14,
        line_count=line_count,
        x_scale=min(x_scales, default=1.0),
        y_scale=min(y_scales, default=1.0),
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="; ".join(dict.fromkeys(messages + ([f"scaled_lines={len(scaled)}"] if scaled else []))),
    )


def render_one_system_message(
    row: dict[str, str],
    args: argparse.Namespace,
    rule: SystemMessageRule,
) -> RenderResult:
    del rule
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    with Image.open(source_path) as raw:
        image = raw.convert("RGBA")

    title_box = relative_box(source.width, source.height, 12, 9, -8, 23)
    content_box = relative_box(source.width, source.height, 11, 31, -9, -11)
    title_background = rgb_hex_color("39394a")
    content_background = rgb_hex_color("101820")
    text_color = rgb_hex_color("ffffff")
    image.paste(content_background, content_box)

    font_path, font_index, font_label = fixed_font_for(
        TextStyle(font_role="nanum_myeongjo_extrabold", font_size=14)
    )
    title_font = load_font(font_path, 14, font_index)
    content_font = load_font(font_path, 14, font_index)
    title, content_source_lines = split_system_message_text(row.get(args.text_column) or "")

    messages: list[str] = []
    line_count = 0
    x_scale = 1.0
    title_width = max(1, title_box[2] - title_box[0])
    title_height = max(1, title_box[3] - title_box[1])
    if title:
        image.paste(title_background, title_box)
        title_mask, x_scale = render_system_message_title_mask(
            title,
            title_font,
            title_width,
            title_height,
        )
        if x_scale != 1.0:
            messages.append(f"title scaled x={x_scale:.3f}")
        title_x = title_box[0] + max(0, (title_width - title_mask.width) // 2)
        title_y = title_box[1] + max(0, (title_height - title_mask.height) // 2)
        paste_masked_color(image, title_mask, text_color, (title_x, title_y))
        line_count += 1

    content_width = max(1, content_box[2] - content_box[0])
    content_height = max(1, content_box[3] - content_box[1])
    if any(line != "" for line in content_source_lines):
        scratch = Image.new("L", (1, 1), 0)
        draw = ImageDraw.Draw(scratch)
        content_lines = wrap_preserving_linebreaks(
            content_source_lines,
            content_width,
            draw,
            content_font,
        )
        content_mask, content_line_count, overflow = render_area_text_mask(
            content_lines,
            content_font,
            content_width,
            content_height,
            blank_line_height_scale=0.5,
        )
        if overflow:
            messages.append(f"content overflow lines={content_line_count}")
        paste_masked_color(image, content_mask, text_color, (content_box[0], content_box[1]))
        line_count += content_line_count

    if line_count == 0:
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=source.width,
            height=source.height,
            source_png_bytes=source_bytes,
            message="empty text",
        )

    image, changed_pixels = snap_rgba_to_palette(image, source.colors)
    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font=font_label,
        font_size=15,
        line_count=line_count,
        x_scale=x_scale,
        y_scale=1.0,
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="; ".join(messages),
    )


def render_one_game_manual(
    row: dict[str, str],
    args: argparse.Namespace,
    rule: GameManualRule,
) -> RenderResult:
    del rule
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    with Image.open(source_path) as raw:
        image = raw.convert("RGBA")

    title_box = (11, 13, 320, 59)
    title_erase_box = (11, 13, 317, 59)
    title_extra_erase_box = (314, 17, 321, 61)
    content_box = (246, 64, 467, 229)
    text_boxes = (title_box, content_box)
    text_area_palette = colors_in_boxes(image, text_boxes)
    background = image.getpixel((8, 14))
    text_color = brightest_opaque_color_in_boxes(image, text_boxes)
    image.paste(background, title_erase_box)
    image.paste(background, title_extra_erase_box)
    image.paste(background, content_box)

    font_path, font_index, font_label = fixed_font_for(
        TextStyle(font_role="nanum_myeongjo_extrabold", font_size=40)
    )
    title_font = load_font(font_path, 40, font_index)
    content_font = load_font(font_path, 12, font_index)
    title, content_source_lines = split_title_content_text(row.get(args.text_column) or "")

    messages: list[str] = []
    line_count = 0
    x_scale = 1.0
    title_width = max(1, title_box[2] - title_box[0])
    title_height = max(1, title_box[3] - title_box[1])
    if title:
        title_mask, x_scale = render_system_message_title_mask(
            title,
            title_font,
            title_width,
            title_height,
        )
        if x_scale != 1.0:
            messages.append(f"title scaled x={x_scale:.3f}")
        title_x = title_box[0]
        title_y = title_box[1] + max(0, (title_height - title_mask.height) // 2)
        paste_masked_color(image, title_mask, text_color, (title_x, title_y))
        line_count += 1

    content_width = max(1, content_box[2] - content_box[0])
    content_height = max(1, content_box[3] - content_box[1])
    if any(line != "" for line in content_source_lines):
        scratch = Image.new("L", (1, 1), 0)
        draw = ImageDraw.Draw(scratch)
        content_lines = wrap_preserving_linebreaks(
            content_source_lines,
            content_width,
            draw,
            content_font,
        )
        content_mask, content_line_count, overflow = render_area_text_mask(
            content_lines,
            content_font,
            content_width,
            content_height,
        )
        if overflow:
            messages.append(f"content overflow lines={content_line_count}")
        paste_masked_color(image, content_mask, text_color, (content_box[0], content_box[1]))
        line_count += content_line_count

    if line_count == 0:
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=source.width,
            height=source.height,
            source_png_bytes=source_bytes,
            message="empty text",
        )

    image, text_area_changed_pixels = snap_rgba_boxes_to_palette(
        image,
        text_boxes,
        text_area_palette,
    )
    image, changed_pixels = snap_rgba_to_palette(image, source.colors)
    changed_pixels += text_area_changed_pixels
    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font=font_label,
        font_size=40,
        line_count=line_count,
        x_scale=x_scale,
        y_scale=1.0,
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="; ".join(messages),
    )


def render_one_unit_officer_count(
    row: dict[str, str],
    args: argparse.Namespace,
    rule: UnitOfficerCountRule,
) -> RenderResult:
    del rule
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    with Image.open(source_path) as raw:
        image = raw.convert("RGBA")

    boxes = (
        (19, 21, 19 + 57, 21 + 18),
        (19, 40, 19 + 57, 40 + 18),
    )
    lines = normalize_preserved_lines(row.get(args.text_column) or "")
    font_path, font_index, font_label = fixed_font_for(
        TextStyle(font_role="nanum_gothic_extrabold", font_size=15)
    )
    font = load_font(font_path, 15, font_index)

    messages: list[str] = []
    line_count = 0
    changed_pixels = 0
    for index, box in enumerate(boxes):
        if index >= len(lines) or not lines[index].strip():
            continue
        box_palette = colors_in_boxes(image, (box,))
        background = darkest_opaque_color_in_boxes(image, (box,))
        text_color = brightest_opaque_color_in_boxes(image, (box,))
        image.paste(background, box)

        box_width = max(1, box[2] - box[0])
        box_height = max(1, box[3] - box[1])
        mask, _natural_width, _natural_height = render_lines_mask([lines[index]], font, 0)
        if mask.width > box_width or mask.height > box_height:
            messages.append(f"line {index + 1} overflow {mask.width}x{mask.height}")
        x = box[0] + (box_width - mask.width) // 2
        y = box[1] + (box_height - mask.height) // 2
        paste_masked_color(image, mask, text_color, (x, y))
        image, box_changed_pixels = snap_rgba_boxes_to_palette(image, (box,), box_palette)
        changed_pixels += box_changed_pixels
        line_count += 1

    if line_count == 0:
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=source.width,
            height=source.height,
            source_png_bytes=source_bytes,
            message="empty text",
        )

    image, full_changed_pixels = snap_rgba_to_palette(image, source.colors)
    changed_pixels += full_changed_pixels
    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font=font_label,
        font_size=15,
        line_count=line_count,
        x_scale=1.0,
        y_scale=1.0,
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="; ".join(messages),
    )


def render_one_budget_input(
    row: dict[str, str],
    args: argparse.Namespace,
    rule: BudgetInputRule,
) -> RenderResult:
    del rule
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    with Image.open(source_path) as raw:
        image = raw.convert("RGBA")

    lines = [
        line.strip()
        for line in normalize_preserved_lines(row.get(args.text_column) or "")
        if line.strip()
    ]
    if not lines:
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=source.width,
            height=source.height,
            source_png_bytes=source_bytes,
            message="empty text",
        )

    messages: list[str] = []
    if len(lines) != 2:
        messages.append(f"expected 2 lines, got {len(lines)}")
    lines = lines[:2]

    text_boxes = tuple(
        relative_box(source.width, source.height, *box)
        for box in (
            (18, 0, 18 + 106, 0 + 16),
            (18, 23, 18 + 106, 23 + 16),
        )
    )
    background = image.getpixel((0, 0))
    font_path, font_index, font_label = fixed_font_for(
        TextStyle(font_role="nanum_gothic_extrabold", font_size=14)
    )
    font = load_font(font_path, 14, font_index)

    changed_pixels = 0
    for index, (line, box) in enumerate(zip(lines, text_boxes), 1):
        box_palette = colors_in_boxes(image, (box,))
        text_color = brightest_opaque_color_in_boxes(image, (box,))
        image.paste(background, box)

        box_width = max(1, box[2] - box[0])
        box_height = max(1, box[3] - box[1])
        mask, _natural_width, _natural_height = render_lines_mask([line], font, 0)
        if mask.width > box_width or mask.height > box_height:
            messages.append(f"line {index} overflow {mask.width}x{mask.height}")
        if mask.width > box_width:
            mask = mask.crop((0, 0, box_width, mask.height))
        if mask.height > box_height:
            top = max(0, (mask.height - box_height) // 2)
            mask = mask.crop((0, top, mask.width, top + box_height))

        x = box[0]
        y = box[1] + max(0, (box_height - mask.height) // 2)
        paste_masked_color(image, mask, text_color, (x, y))
        image, box_changed_pixels = snap_rgba_boxes_to_palette(image, (box,), box_palette)
        changed_pixels += box_changed_pixels

    image, full_changed_pixels = snap_rgba_to_palette(image, source.colors)
    changed_pixels += full_changed_pixels
    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font=font_label,
        font_size=14,
        line_count=len(lines),
        x_scale=1.0,
        y_scale=1.0,
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="; ".join(messages),
    )


def render_one_ui16_flat(
    row: dict[str, str],
    args: argparse.Namespace,
    rule: UI16FlatRule,
) -> RenderResult:
    del rule
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    with Image.open(source_path) as raw:
        image = raw.convert("RGBA")

    font_path, font_index, font_label = fixed_font_for(
        TextStyle(font_role="nanum_gothic_bold", font_size=15)
    )
    font = load_font(font_path, 15, font_index)
    lines = [line for line in normalize_preserved_lines(row.get(args.text_column) or "") if line.strip()]
    messages: list[str] = []
    x_scale = 1.0

    if len(lines) == 3:
        boxes = (
            (47, 0, 47 + 71, 0 + 18),
            (47, 24, 47 + 71, 24 + 18),
            (47, 46, 47 + 71, 46 + 18),
        )
        text_boxes = tuple(relative_box(source.width, source.height, *box) for box in boxes)
        text_area_palette = colors_in_boxes(image, text_boxes)
        background = image.getpixel((0, 0))
        text_color = brightest_opaque_color_in_boxes(image, text_boxes)
        for box in text_boxes:
            image.paste(background, box)
        for index, (line, box) in enumerate(zip(lines, text_boxes), 1):
            box_width = max(1, box[2] - box[0])
            box_height = max(1, box[3] - box[1])
            mask, line_x_scale = render_system_message_title_mask(
                line,
                font,
                box_width,
                box_height,
            )
            if line_x_scale != 1.0:
                messages.append(f"line {index} scaled x={line_x_scale:.3f}")
            x_scale = min(x_scale, line_x_scale)
            x = box[0]
            y = box[1] + max(0, (box_height - mask.height) // 2)
            paste_masked_color(image, mask, text_color, (x, y))
    else:
        line = "".join(lines)
        if not line.strip():
            return RenderResult(
                status="skipped",
                output=output,
                target=target_path,
                width=source.width,
                height=source.height,
                source_png_bytes=source_bytes,
                message="empty text",
            )
        image = Image.new("RGBA", (source.width, source.height), source.background)
        mask, _natural_width, _natural_height = render_lines_mask([line], font, 0)
        if mask.width > source.width:
            x_scale = source.width / max(1, mask.width)
            mask = mask.resize((source.width, mask.height), Image.Resampling.LANCZOS)
            messages.append(f"scaled x={x_scale:.3f}")
        if mask.height > source.height:
            top = max(0, (mask.height - source.height) // 2)
            mask = mask.crop((0, top, mask.width, top + source.height))
            messages.append(f"cropped y={mask.height}")
        x = (source.width - mask.width) // 2
        y = (source.height - mask.height) // 2
        paste_masked_color(image, mask, source.foreground, (x, y))

    if len(lines) == 3:
        image, text_area_changed_pixels = snap_rgba_boxes_to_palette(
            image,
            text_boxes,
            text_area_palette,
        )
    else:
        text_area_changed_pixels = 0
    image, changed_pixels = snap_rgba_to_palette(image, source.colors)
    changed_pixels += text_area_changed_pixels
    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font=font_label,
        font_size=15,
        line_count=3 if len(lines) == 3 else 1,
        x_scale=x_scale,
        y_scale=1.0,
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="; ".join(messages),
    )


def render_one_battle_display(
    row: dict[str, str],
    args: argparse.Namespace,
    rule: BattleDisplayRule,
) -> RenderResult:
    del rule
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    with Image.open(source_path) as raw:
        image = raw.convert("RGBA")

    text = unicodedata.normalize("NFC", row.get(args.text_column) or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "").strip()
    if not text:
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=source.width,
            height=source.height,
            source_png_bytes=source_bytes,
            message="empty text",
        )

    text_box = relative_box(source.width, source.height, 11, 8, 11 + 183, 8 + 19)
    text_area_palette = colors_in_boxes(image, (text_box,))
    background = image.getpixel((11, 8))
    text_color = brightest_opaque_color_in_boxes(image, (text_box,))
    image.paste(background, text_box)

    font_path, font_index, font_label = fixed_font_for(
        TextStyle(font_role="nanum_gothic_extrabold", font_size=16)
    )
    font = load_font(font_path, 16, font_index)
    box_width = max(1, text_box[2] - text_box[0])
    box_height = max(1, text_box[3] - text_box[1])
    mask, x_scale = render_system_message_title_mask(text, font, box_width, box_height)
    messages: list[str] = []
    if x_scale != 1.0:
        messages.append(f"scaled x={x_scale:.3f}")
    x = text_box[0]
    y = text_box[1] + max(0, (box_height - mask.height) // 2)
    paste_masked_color(image, mask, text_color, (x, y))

    image, text_area_changed_pixels = snap_rgba_boxes_to_palette(
        image,
        (text_box,),
        text_area_palette,
    )
    image, changed_pixels = snap_rgba_to_palette(image, source.colors)
    changed_pixels += text_area_changed_pixels
    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font=font_label,
        font_size=16,
        line_count=1,
        x_scale=x_scale,
        y_scale=1.0,
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="; ".join(messages),
    )


def render_one_faction_select_prompt(
    row: dict[str, str],
    args: argparse.Namespace,
    rule: FactionSelectPromptRule,
) -> RenderResult:
    del rule
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    with Image.open(source_path) as raw:
        source_image = raw.convert("RGBA")

    text = unicodedata.normalize("NFC", row.get(args.text_column) or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "").strip()
    if not text:
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=source.width,
            height=source.height,
            source_png_bytes=source_bytes,
            message="empty text",
        )

    background = source_image.getpixel((0, 0))
    text_color = source.foreground
    image = Image.new("RGBA", (source.width, source.height), background)

    font_path, font_index, font_label = fixed_font_for(
        TextStyle(font_role="nanum_gothic_extrabold", font_size=20)
    )
    font = load_font(font_path, 20, font_index)
    mask, x_scale = render_shrink_to_width_line_mask(text, font, source.width, source.height)
    y = max(0, (source.height - mask.height) // 2)
    paste_masked_color(image, mask, text_color, (0, y))

    visible_palette = colors_in_boxes(source_image, ((0, 0, source.width, source.height),))
    image, local_changed_pixels = snap_rgba_boxes_to_palette(
        image,
        ((0, 0, source.width, source.height),),
        visible_palette,
    )
    image, changed_pixels = snap_rgba_to_palette(image, source.colors)
    changed_pixels += local_changed_pixels
    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    return RenderResult(
        status="ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font=font_label,
        font_size=20,
        line_count=1,
        x_scale=x_scale,
        y_scale=1.0,
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="",
    )


def faction_select_erase_bbox_message(box: tuple[int, int, int, int] | None) -> str:
    if box is None:
        return ""
    return ",".join(str(value) for value in box)


def faction_select_ordinal(row: dict[str, str]) -> int | None:
    try:
        return int(row.get("ordinal") or "", 0)
    except ValueError:
        return None


def faction_select_shared_donor_images(
    row: dict[str, str],
    args: argparse.Namespace,
) -> list[Image.Image]:
    ordinal = faction_select_ordinal(row)
    group = faction_select_phrase_generator.shared_background_group_for_ordinal(ordinal)
    if not group:
        return []

    rows = read_rows(Path(args.csv))
    outputs_by_ordinal: dict[int, str] = {}
    for candidate in rows:
        if (candidate.get("verified_group") or "").strip() != "세력선택 문구":
            continue
        candidate_ordinal = faction_select_ordinal(candidate)
        if candidate_ordinal in group and candidate_ordinal != ordinal:
            output = (candidate.get(args.output_column) or "").strip().replace("\\", "/")
            if output:
                outputs_by_ordinal[candidate_ordinal] = output

    donors: list[Image.Image] = []
    for donor_ordinal in group:
        if donor_ordinal == ordinal:
            continue
        output = outputs_by_ordinal.get(donor_ordinal)
        if not output:
            continue
        donor_path = Path(args.textures_root) / output
        if not donor_path.exists():
            continue
        with Image.open(donor_path) as raw:
            donors.append(raw.convert("RGBA"))
    return donors


def render_one_faction_select_phrase(
    row: dict[str, str],
    args: argparse.Namespace,
    rule: FactionSelectPhraseRule,
) -> RenderResult:
    del rule
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        background_source="top_left",
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    with Image.open(source_path) as raw:
        source_image = raw.convert("RGBA")

    text = unicodedata.normalize("NFC", row.get(args.text_column) or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "").strip()
    if not text:
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=source.width,
            height=source.height,
            source_png_bytes=source_bytes,
            message="empty text",
        )

    ordinal = faction_select_ordinal(row)
    erased = faction_select_phrase_generator.erase_japanese_text_with_shared_background(
        source_image,
        ordinal,
        faction_select_shared_donor_images(row, args),
    )
    image = erased.image
    box_left, box_top, box_right, box_bottom = relative_box(
        source.width,
        source.height,
        0,
        39,
        480,
        87,
    )
    box_width = max(1, box_right - box_left)
    box_height = max(1, box_bottom - box_top)

    font_path, font_index, font_label = fixed_font_for(
        TextStyle(font_role="nanum_gothic_extrabold", font_size=42)
    )
    font = load_font(font_path, 42, font_index)
    mask, _natural_width, _natural_height = render_lines_mask([text], font, 0)
    x_scale = 1.0
    y_scale = 1.0
    scaled = False
    messages = [
        f"erase_bbox={faction_select_erase_bbox_message(erased.mask_bbox)}",
        f"erase_pixels={erased.erase_pixels}",
    ]
    if erased.shared_background_pixels:
        messages.append(f"shared_bg_pixels={erased.shared_background_pixels}")
    if mask.width > box_width or mask.height > box_height:
        scale = min(box_width / max(1, mask.width), box_height / max(1, mask.height))
        mask = mask.resize(
            (max(1, round(mask.width * scale)), max(1, round(mask.height * scale))),
            Image.Resampling.LANCZOS,
        )
        x_scale = y_scale = scale
        scaled = True
        messages.append(f"scaled x/y={scale:.3f}")

    x = box_left + (box_width - mask.width) // 2
    y = box_top + (box_height - mask.height) // 2
    paste_masked_color(image, mask, text_effect_color(source), (x + 2, y + 2))
    paste_masked_color(image, mask, source.foreground, (x, y))

    image, _snapped_pixels = snap_rgba_to_palette(image, source.colors)
    changed_pixels = faction_select_phrase_generator.count_changed(source_image, image)
    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    return RenderResult(
        status="warning" if scaled else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font=font_label,
        font_size=42,
        line_count=1,
        x_scale=x_scale,
        y_scale=y_scale,
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="; ".join(messages),
    )


def join_leave_boxes_for_line_count(line_count: int) -> tuple[tuple[int, int, int, int], ...]:
    if line_count <= 1:
        origins = ((100, 144),)
    elif line_count == 2:
        origins = ((42, 144), (154, 144))
    else:
        origins = ((8, 144), (100, 144), (192, 144))
    return tuple((x, y, x + 84, y + 19) for x, y in origins)


def render_one_join_leave(
    row: dict[str, str],
    args: argparse.Namespace,
    rule: JoinLeaveRule,
) -> RenderResult:
    del rule
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    with Image.open(source_path) as raw:
        image = raw.convert("RGBA")

    lines = [line.strip() for line in normalize_preserved_lines(row.get(args.text_column) or "") if line.strip()]
    if not lines:
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=source.width,
            height=source.height,
            source_png_bytes=source_bytes,
            message="empty text",
        )

    messages: list[str] = []
    if len(lines) > 3:
        messages.append(f"truncated lines={len(lines)}")
        lines = lines[:3]

    text_boxes = tuple(relative_box(source.width, source.height, *box) for box in join_leave_boxes_for_line_count(len(lines)))
    text_area_palette = colors_in_boxes(image, text_boxes)
    background = image.getpixel((text_boxes[0][0], text_boxes[0][1]))
    text_color = brightest_whiteish_opaque_color_in_boxes(image, text_boxes)
    for box in text_boxes:
        image.paste(background, box)

    font_path, font_index, font_label = fixed_font_for(
        TextStyle(font_role="nanum_myeongjo_extrabold", font_size=16)
    )
    font = load_font(font_path, 16, font_index)
    x_scales: list[float] = []
    for index, (line, box) in enumerate(zip(lines, text_boxes), 1):
        box_width = max(1, box[2] - box[0])
        box_height = max(1, box[3] - box[1])
        mask, x_scale = render_shrink_to_width_line_mask(line, font, box_width, box_height)
        x_scales.append(x_scale)
        x = box[0] + max(0, (box_width - mask.width) // 2)
        y = box[1] + max(0, (box_height - mask.height) // 2)
        paste_masked_color(image, mask, text_color, (x, y))

    image, text_area_changed_pixels = snap_rgba_boxes_to_palette(
        image,
        text_boxes,
        text_area_palette,
    )
    image, changed_pixels = snap_rgba_to_palette(image, source.colors)
    changed_pixels += text_area_changed_pixels
    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font=font_label,
        font_size=16,
        line_count=len(lines),
        x_scale=min(x_scales, default=1.0),
        y_scale=1.0,
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="; ".join(dict.fromkeys(messages)),
    )


def render_one_fixed(
    row: dict[str, str],
    args: argparse.Namespace,
    rule: GroupRule,
) -> RenderResult:
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        background_source=rule.background_source,
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    text = row.get(args.text_column) or ""
    image = Image.new("RGBA", (source.width, source.height), source.background)
    messages: list[str] = []
    font_labels: list[str] = []
    font_sizes: list[float] = []
    line_count = 0
    x_scales: list[float] = []
    y_scales: list[float] = []

    for style in rule.styles_for(row, source):
        mask, font_label, font_size, lines, extent = render_style_mask(
            text,
            style,
            source.width,
            source.height,
        )
        if lines == 0:
            continue
        font_labels.append(font_label)
        font_sizes.append(font_size)
        line_count += lines
        outline_width = max(0, style.outline_width)
        left_padding, right_padding = horizontal_paddings(style)
        available_width = max(1, source.width - left_padding - right_padding - outline_width * 2)
        footprint_available_width = max(1, source.width - left_padding - right_padding)
        max_render_width = max(1, style.max_render_width - outline_width * 2) if style.max_render_width else 0
        scale_width = min(available_width, max_render_width) if max_render_width else available_width
        available_height = max(1, source.height - style.y_padding * 2 - outline_width * 2)
        footprint_available_height = max(1, source.height - style.y_padding * 2)
        x_scale = 1.0
        if style.shrink_overflow_x and mask.width > scale_width:
            x_scale = scale_width / max(1, mask.width)
            mask = mask.resize((scale_width, mask.height), Image.Resampling.LANCZOS)
            messages.append(f"scaled x={x_scale:.3f}")
        x_scales.append(x_scale)
        y_scale = 1.0
        if style.shrink_overflow_y and mask.height > available_height:
            y_scale = available_height / max(1, mask.height)
            mask = mask.resize((mask.width, available_height), Image.Resampling.LANCZOS)
            messages.append(f"scaled y={y_scale:.3f}")
        y_scales.append(y_scale)
        footprint_width = mask.width + outline_width * 2
        footprint_height = mask.height + outline_width * 2
        if footprint_width > footprint_available_width or footprint_height > footprint_available_height:
            messages.append(f"overflow {footprint_width}x{footprint_height}")
        footprint_x, footprint_y = paste_position_for_style(
            source.width,
            source.height,
            footprint_width,
            footprint_height,
            style,
        )
        x = footprint_x + outline_width
        y = footprint_y + outline_width
        effect_color = text_effect_color(source)
        if style.shadow_offset:
            shadow_x, shadow_y = style.shadow_offset
            paste_masked_color(image, mask, effect_color, (x + shadow_x, y + shadow_y))
        outline_color = text_outline_color(source) if style.outline_width > 0 else None
        if outline_color is not None:
            expanded_mask = outline_mask(mask, style.outline_width)
            paste_masked_color(
                image,
                expanded_mask,
                outline_color,
                (footprint_x, footprint_y),
            )
        paste_masked_color(image, mask, source.foreground, (x, y))
        del extent

    if line_count == 0:
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=source.width,
            height=source.height,
            source_png_bytes=source_bytes,
            message="empty text",
        )

    image, changed_pixels = snap_rgba_to_palette(image, source.colors)
    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font="+".join(dict.fromkeys(font_labels)),
        font_size=max(font_sizes, default=0),
        line_count=line_count,
        x_scale=min(x_scales, default=1.0),
        y_scale=min(y_scales, default=1.0),
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="; ".join(messages),
    )


def palette_indexed_png_bytes(
    image: Image.Image,
    palette: list[Color],
    color_to_index: dict[Color, int] | None = None,
) -> bytes:
    color_to_index = dict(color_to_index or {})
    if color_to_index:
        max_index = max(color_to_index.values(), default=-1)
        colors = list(palette[: max(0, max_index) + 1])
        while len(colors) <= max_index:
            colors.append((0, 0, 0, 0))
        for color, index in color_to_index.items():
            if index >= len(colors):
                colors.extend([(0, 0, 0, 0)] * (index + 1 - len(colors)))
            colors[index] = color
    else:
        colors = list(dict.fromkeys(palette))
    used = {color for color in image.getdata()}
    for color in sorted(used, key=lambda item: (item[3], luma(item), item)):
        if color not in color_to_index and color not in colors:
            colors.append(color)
        color_to_index.setdefault(color, colors.index(color))
    if len(colors) > 256:
        raise ValueError(f"too many palette colors for indexed PNG: {len(colors)}")
    for index, color in enumerate(colors):
        color_to_index.setdefault(color, index)
    indexed = Image.new("P", image.size)
    indexed.putdata([color_to_index[color] for color in image.getdata()])
    raw_palette: list[int] = []
    alpha: list[int] = []
    for red, green, blue, value in colors:
        raw_palette.extend((red, green, blue))
        alpha.append(value)
    indexed.putpalette(raw_palette)
    while alpha and alpha[-1] == 255:
        alpha.pop()
    if alpha:
        indexed.info["transparency"] = bytes(alpha)
    out = io.BytesIO()
    indexed.save(out, format="PNG", optimize=True, compress_level=9)
    return out.getvalue()


def save_png(
    image: Image.Image,
    palette: list[Color],
    path: Path,
    color_to_index: dict[Color, int] | None = None,
) -> int:
    data = palette_indexed_png_bytes(image, palette, color_to_index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def target_for(output: str, args: argparse.Namespace) -> Path:
    if args.apply:
        return Path(args.textures_root) / output
    return Path(args.out_root) / output


def matches_only(output: str, only: str | None) -> bool:
    if not only:
        return True
    needle = only.replace("\\", "/")
    return output == needle or output.endswith(needle) or needle in output


def render_one(
    row: dict[str, str],
    args: argparse.Namespace,
    font_path: Path,
    font_label: str,
) -> RenderResult:
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    source = inspect_source_png(
        source_path,
        manifest_palette=palette_from_manifest_record(row, Path(args.unpacked_root)),
    )
    text = row.get(args.text_column) or ""
    plan = make_fit_plan(text, source.width, source.height, args, font_path)
    font = load_font(font_path, plan.font_size, args.font_index)
    mask, natural_width, natural_height = render_lines_mask(plan.lines, font, args.line_spacing)
    del natural_width, natural_height
    if plan.rendered_width != mask.width or plan.rendered_height != mask.height:
        mask = mask.resize((plan.rendered_width, plan.rendered_height), Image.Resampling.LANCZOS)

    image = Image.new("RGBA", (source.width, source.height), source.background)
    text_layer = Image.new("RGBA", mask.size, source.foreground)
    x, y = paste_position(source.width, source.height, mask.width, mask.height, args)
    image.paste(text_layer, (x, y), mask=mask)
    image, changed_pixels = snap_rgba_to_palette(image, source.colors)

    messages: list[str] = []
    if plan.font_reduced:
        messages.append(f"font reduced to {plan.font_size}")
    if plan.scaled:
        messages.append(f"scaled x={plan.x_scale:.3f} y={plan.y_scale:.3f}")

    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, source.colors, source.color_to_index))
    else:
        generated_bytes = save_png(image, source.colors, target_path, source.color_to_index)

    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        font=font_label,
        font_size=plan.font_size,
        line_count=len(plan.lines),
        x_scale=plan.x_scale,
        y_scale=plan.y_scale,
        changed_pixels=changed_pixels,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        message="; ".join(messages),
    )


def manifest_record_key(record: dict) -> tuple[str, str, str]:
    return (
        str(record.get("source", "")).replace("\\", "/"),
        str(record.get("offset", "")),
        str(record.get("output", "")).replace("\\", "/"),
    )


def write_filtered_manifests(
    textures_root: Path,
    out_root: Path,
    rows: list[dict[str, str]],
    output_column: str,
    rendered: set[str],
) -> list[Path]:
    if not rendered:
        return []
    out_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    manifest_json = textures_root / "manifest.json"
    if manifest_json.exists():
        records = json.loads(manifest_json.read_text(encoding="utf-8"))
        incoming = [
            dict(record)
            for record in records
            if str(record.get("output", "")).replace("\\", "/") in rendered
        ]
        target = out_root / "manifest.json"
        existing = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
        merged = merge_manifest_records(existing, incoming)
        target.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(target)

    target = out_root / "manifest.csv"
    existing_rows: list[dict[str, str]] = []
    existing_fieldnames: list[str] = []
    if target.exists():
        with target.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            existing_fieldnames = list(reader.fieldnames or [])
            existing_rows = list(reader)
    incoming_rows = [
        dict(row)
        for row in rows
        if (row.get(output_column) or "").strip().replace("\\", "/") in rendered
    ]
    if incoming_rows:
        fieldnames = merged_fieldnames(existing_fieldnames, incoming_rows[0].keys())
        merged_rows = merge_manifest_records(existing_rows, incoming_rows)
        fieldnames = merged_fieldnames(fieldnames, (key for row in merged_rows for key in row.keys()))
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(merged_rows)
        written.append(target)
    return written


def write_report(path: Path, results: list[RenderResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "status",
        "output",
        "target",
        "width",
        "height",
        "font",
        "font_size",
        "line_count",
        "x_scale",
        "y_scale",
        "changed_pixels",
        "source_png_bytes",
        "generated_png_bytes",
        "png_delta_bytes",
        "message",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "status": result.status,
                    "output": result.output,
                    "target": result.target.as_posix(),
                    "width": result.width,
                    "height": result.height,
                    "font": result.font,
                    "font_size": result.font_size,
                    "line_count": result.line_count,
                    "x_scale": f"{result.x_scale:.3f}",
                    "y_scale": f"{result.y_scale:.3f}",
                    "changed_pixels": result.changed_pixels,
                    "source_png_bytes": result.source_png_bytes,
                    "generated_png_bytes": result.generated_png_bytes,
                    "png_delta_bytes": result.generated_png_bytes - result.source_png_bytes
                    if result.generated_png_bytes and result.source_png_bytes
                    else 0,
                    "message": result.message,
                }
            )


def main() -> int:
    args = parse_args()
    if args.font_size <= 0 or args.min_font_size <= 0:
        print("Error: font sizes must be positive", file=sys.stderr)
        return 2
    if args.min_font_size > args.font_size:
        print("Error: --min-font-size cannot exceed --font-size", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit <= 0:
        print("Error: --limit must be positive", file=sys.stderr)
        return 2
    if args.min_x_scale <= 0 or args.min_y_scale <= 0:
        print("Error: minimum scale values must be positive", file=sys.stderr)
        return 2
    try:
        row_filter = RowRangeFilter.parse(args.rows)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    try:
        font_path, font_choice = resolve_font(args.font)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    font_label = font_choice.aliases[0] if font_choice else str(font_path.relative_to(PROJECT_ROOT) if font_path.is_relative_to(PROJECT_ROOT) else font_path)

    rows = read_rows(Path(args.csv))
    mark_ui17_left_outline_rows(rows)
    if rows and args.output_column not in rows[0]:
        print(f"Error: missing CSV column: {args.output_column}", file=sys.stderr)
        return 2
    if rows and args.text_column not in rows[0]:
        print(f"Error: missing CSV column: {args.text_column}", file=sys.stderr)
        return 2

    target_groups = {canonical_group(args.target_verified_group)} if args.target_verified_group else set(FIXED_GROUP_RULES)
    if args.verbose:
        print(f"Target verified_group: {', '.join(sorted(target_groups))}")
        if target_groups and target_groups.issubset(FIXED_GROUP_RULES):
            print("Using fixed verified_group render rules")
        else:
            print(f"Using natural font: {font_label} ({font_path}) size={args.font_size}")
        print(f"Output mode: {'apply to source textures' if args.apply else args.out_root}")

    results: list[RenderResult] = []
    rendered: set[str] = set()
    seen_text: dict[str, str] = {}
    failures = 0
    skipped_not_target = 0
    duplicate_rows = 0

    for row_number, row in enumerate(rows, 1):
        if row_filter and not row_filter.contains(row_number):
            continue
        output = (row.get(args.output_column) or "").strip().replace("\\", "/")
        if not output or not matches_only(output, args.only):
            continue
        group = canonical_group((row.get("verified_group") or "").strip())
        if target_groups and group not in target_groups:
            skipped_not_target += 1
            continue
        source_path = Path(args.textures_root) / output
        if not source_path.exists():
            failures += 1
            results.append(
                RenderResult(
                    status="error",
                    output=output,
                    target=target_for(output, args),
                    message=f"source image not found: {source_path}",
                )
            )
            if args.strict:
                break
            continue
        text = row.get(args.text_column) or ""
        render_text = unicodedata.normalize("NFC", text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not render_text:
            results.append(
                RenderResult(
                    status="skipped",
                    output=output,
                    target=target_for(output, args),
                    message="empty text",
                )
            )
            continue
        previous = seen_text.get(output)
        if previous is not None:
            if previous != render_text:
                failures += 1
                message = "duplicate output has different text"
                print(f"[error] {output}: {message}", file=sys.stderr)
                results.append(
                    RenderResult(
                        status="error",
                        output=output,
                        target=target_for(output, args),
                        message=message,
                    )
                )
                if args.strict:
                    break
            duplicate_rows += 1
            continue
        seen_text[output] = render_text

        try:
            rule = fixed_rule_for_group(group)
            if isinstance(rule, SystemMessageRule):
                result = render_one_system_message(row, args, rule)
            elif isinstance(rule, GameManualRule):
                result = render_one_game_manual(row, args, rule)
            elif isinstance(rule, UnitOfficerCountRule):
                result = render_one_unit_officer_count(row, args, rule)
            elif isinstance(rule, BudgetInputRule):
                result = render_one_budget_input(row, args, rule)
            elif isinstance(rule, UI16FlatRule):
                result = render_one_ui16_flat(row, args, rule)
            elif isinstance(rule, BattleDisplayRule):
                result = render_one_battle_display(row, args, rule)
            elif isinstance(rule, EndingTextRule):
                result = render_one_ending_text(row, args, rule)
            elif isinstance(rule, FactionSelectPromptRule):
                result = render_one_faction_select_prompt(row, args, rule)
            elif isinstance(rule, FactionSelectPhraseRule):
                result = render_one_faction_select_phrase(row, args, rule)
            elif isinstance(rule, JoinLeaveRule):
                result = render_one_join_leave(row, args, rule)
            elif rule:
                result = render_one_fixed(row, args, rule)
            else:
                result = render_one(row, args, font_path, font_label)
        except Exception as exc:
            failures += 1
            result = RenderResult(
                status="error",
                output=output,
                target=target_for(output, args),
                message=str(exc),
            )
            print(f"[error] {output}: {exc}", file=sys.stderr)
            if args.strict:
                results.append(result)
                break
        results.append(result)
        if result.status != "error":
            rendered.add(output)
            if args.verbose:
                print(
                    f"[{result.status}] {output} -> {result.target} "
                    f"{result.width}x{result.height} size={result.font_size} "
                    f"scale={result.x_scale:.3f},{result.y_scale:.3f}"
                )
        if args.limit and len(rendered) >= args.limit:
            break

    if args.report:
        write_report(Path(args.report), results)
        print(f"Report written: {args.report}")

    if rendered and not args.no_copy_manifest and not args.dry_run and not args.apply:
        written = write_filtered_manifests(
            Path(args.textures_root),
            Path(args.out_root),
            rows,
            args.output_column,
            rendered,
        )
        for path in written:
            print(f"Manifest written: {path}")

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    if args.verbose:
        print(
            "Summary: "
            + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
            + f", skipped_not_target={skipped_not_target}, duplicate_rows={duplicate_rows}, failures={failures}"
        )
    else:
        rendered_count = counts.get("ok", 0) + counts.get("warning", 0)
        skipped_count = counts.get("skipped", 0)
        error_count = counts.get("error", 0)
        print(
            f"Summary: rendered={rendered_count}, skipped={skipped_count}, "
            f"skipped_not_target={skipped_not_target}, duplicate_rows={duplicate_rows}, "
            f"errors={error_count}, failures={failures}"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
