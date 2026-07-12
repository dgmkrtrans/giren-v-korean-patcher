#!/usr/bin/env python3
"""Shared Korean text renderer for PSP texture text patterns."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import unicodedata
import urllib.request
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont


Color = tuple[int, int, int, int]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = PROJECT_ROOT / "assets"
FONT_DIR = PROJECT_ROOT / "assets" / "fonts" / "pixel"
FONT_EXTENSIONS = {".otf", ".ttc", ".ttf", ".woff", ".woff2"}
LOWER_LEFT_CELL_PUNCTUATION = {",", "."}
LOWER_LEFT_CELL_PADDING = 3


@dataclass(frozen=True)
class FontChoice:
    aliases: tuple[str, ...]
    path: Path
    url: str
    default_size: int
    note: str


KNOWN_FONTS = (
    FontChoice(
        aliases=("tangba12", "tangba", "lowres"),
        path=FONT_DIR / "Tangba12.woff2",
        url="https://cdn.jsdelivr.net/gh/projectnoonnu/2601-4@1.1/Tangba12.woff2",
        default_size=12,
        note="12px Korean pixel font, good for low-resolution UI.",
    ),
    FontChoice(
        aliases=("dunggeunmo", "dunggeunmo-fixedsys", "rounded-fixedsys"),
        path=FONT_DIR / "DungGeunMo.woff",
        url="https://cdn.jsdelivr.net/gh/projectnoonnu/noonfonts_six@1.2/DungGeunMo.woff",
        default_size=16,
        note="DungGeunMo + Fixedsys pixel font.",
    ),
)


@dataclass(frozen=True)
class TextureSpec:
    width: int
    height: int
    background: Color
    palette: list[Color]
    text_palette: list[Color]
    max_cols: int
    max_lines: int
    cell_size: int


@dataclass(frozen=True)
class RenderPlan:
    lines: list[str]
    font_size: int
    overflow: bool
    too_wide: bool
    font_reduced: bool


@dataclass
class RenderResult:
    status: str
    output: str
    target: Path
    width: int = 0
    height: int = 0
    font: str = ""
    font_size: int = 0
    line_count: int = 0
    max_lines: int = 0
    source_png_bytes: int = 0
    generated_png_bytes: int = 0
    png_storage: str = ""
    pattern_id: str = ""
    dialogue_line_lengths: str = ""
    message: str = ""
    text_palette: list[Color] = field(default_factory=list)


@dataclass(frozen=True)
class PatternTarget:
    target: bool | None
    pattern_id: str


@dataclass(frozen=True)
class RendererDefaults:
    description: str
    out_root: str
    font: str = "assets/fonts/NanumMyeongjoExtraBold.ttf"
    font_size: int | None = 15
    min_font_size: int = 8
    cell_size: int = 16
    layout: str = "cell"
    align: str = "left"
    text_color_limit: int = 3
    pattern_csv: str = "textures_pattern/text_texture_patterns.csv"
    pattern_target_column: str = "target_white_transparent_renderer"
    target_verified_group: str = ""
    background_mode: str = "transparent"
    metric_fallback: bool = True
    preserve_newlines: bool = False
    no_wrap: bool = False
    force_cell_grid: bool = False
    max_render_lines: int = 0
    max_line_chars: int = 0
    expand_height: bool = False
    half_cell_chars: str = ""
    render_split_chars: str = ""
    pad_half_cell_first_line: bool = False
    x_padding: int = 0
    right_padding: int = 0
    y_adjust: int = 0
    min_transparent_ratio: float = 0.45
    min_neutral_ratio: float = 0.90
    min_max_luma: float = 180.0


@dataclass(frozen=True)
class RowRangeFilter:
    ranges: tuple[tuple[int, int], ...]

    @classmethod
    def parse(cls, value: str | None) -> "RowRangeFilter | None":
        text = (value or "").strip()
        if not text:
            return None
        ranges: list[tuple[int, int]] = []
        for raw_part in text.replace("~", "-").split(","):
            part = raw_part.strip()
            if not part:
                continue
            if "-" in part:
                start_text, end_text = [chunk.strip() for chunk in part.split("-", 1)]
            else:
                start_text = end_text = part
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"invalid row range token: {part}")
            start = int(start_text)
            end = int(end_text)
            if start <= 0 or end <= 0 or start > end:
                raise ValueError(f"invalid row range bounds: {part}")
            ranges.append((start, end))
        if not ranges:
            return None
        return cls(tuple(ranges))

    def contains(self, row_number: int) -> bool:
        return any(start <= row_number <= end for start, end in self.ranges)


def parse_dialogue_line_lengths(value: object) -> tuple[int, ...]:
    if value is None:
        return ()
    text = str(value).strip()
    if not text:
        return ()
    for separator in (";", "|", "/", " "):
        text = text.replace(separator, ",")
    lengths: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        number = int(part, 0)
        if number <= 0 or number > 0xFF:
            raise ValueError(f"dialogue line length out of byte range: {number}")
        lengths.append(number)
    return tuple(lengths)


def format_dialogue_line_lengths(lengths: Iterable[int]) -> str:
    return ",".join(str(length) for length in lengths)


def manifest_record_key(record: dict) -> tuple[str, str, str]:
    return (
        str(record.get("source", "")).replace("\\", "/"),
        str(record.get("offset", "")),
        str(record.get("output", "")).replace("\\", "/"),
    )


def parse_args(defaults: RendererDefaults) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=defaults.description
    )
    parser.add_argument("--csv", default="textures_static/manifest.csv")
    parser.add_argument("--textures-root", default="textures_static")
    parser.add_argument("--out-root", default=defaults.out_root)
    parser.add_argument("--pattern-csv", default=defaults.pattern_csv)
    parser.add_argument("--ignore-pattern-csv", action="store_true")
    parser.add_argument("--pattern-target-column", default=defaults.pattern_target_column)
    parser.add_argument("--target-verified-group", default=defaults.target_verified_group)
    parser.add_argument(
        "--background-mode",
        choices=("transparent", "opaque-most-frequent"),
        default=defaults.background_mode,
    )
    parser.add_argument("--output-column", default="output")
    parser.add_argument("--text-column", default="korean")
    parser.add_argument("--only", help="Render outputs containing this value.")
    parser.add_argument(
        "--rows",
        "--row-range",
        dest="rows",
        help="CSV data row range, for example: 10-40,150,300-500.",
    )
    parser.add_argument("--limit", type=int, help="Stop after this many generated PNGs.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Overwrite PNGs under --textures-root.")
    parser.add_argument(
        "--font",
        default=defaults.font,
        help="Pixel font alias, font file path, or 'auto' to use the first font found under assets.",
    )
    parser.add_argument("--font-index", type=int, default=0)
    parser.add_argument("--font-size", type=int, default=defaults.font_size, help="Requested pixel font size.")
    parser.add_argument("--min-font-size", type=int, default=defaults.min_font_size)
    parser.add_argument("--list-fonts", action="store_true")
    parser.add_argument("--download-fonts", action="store_true")
    parser.add_argument("--cell-size", type=int, default=defaults.cell_size)
    parser.add_argument("--x-padding", type=int, default=defaults.x_padding)
    parser.add_argument("--right-padding", type=int, default=defaults.right_padding)
    parser.add_argument("--y-adjust", type=int, default=defaults.y_adjust)
    parser.add_argument(
        "--align",
        choices=("left", "center", "right"),
        default=defaults.align,
    )
    parser.add_argument(
        "--layout",
        choices=("cell", "measure"),
        default=defaults.layout,
        help="cell keeps the PSP 16px text grid; measure uses actual font widths.",
    )
    parser.add_argument("--text-color-limit", type=int, choices=(1, 2, 3), default=defaults.text_color_limit)
    parser.add_argument(
        "--preserve-newlines",
        dest="preserve_newlines",
        action="store_true",
        default=defaults.preserve_newlines,
        help="Preserve CSV Korean text line breaks before wrapping.",
    )
    parser.add_argument("--ignore-newlines", dest="preserve_newlines", action="store_false")
    parser.add_argument("--no-wrap", dest="no_wrap", action="store_true", default=defaults.no_wrap)
    parser.add_argument("--wrap", dest="no_wrap", action="store_false")
    parser.add_argument("--max-render-lines", type=int, default=defaults.max_render_lines)
    parser.add_argument("--max-line-chars", type=int, default=defaults.max_line_chars)
    parser.add_argument(
        "--expand-height",
        dest="expand_height",
        action="store_true",
        default=defaults.expand_height,
        help="Grow the output canvas to fit rendered cell rows.",
    )
    parser.add_argument("--no-expand-height", dest="expand_height", action="store_false")
    parser.add_argument("--half-cell-chars", default=defaults.half_cell_chars)
    parser.add_argument("--render-split-chars", default=defaults.render_split_chars)
    parser.add_argument(
        "--pad-half-cell-first-line",
        action="store_true",
        default=defaults.pad_half_cell_first_line,
    )
    parser.add_argument("--report", help="Optional render report CSV.")
    parser.add_argument("--no-copy-manifest", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="Print ok/warning render progress and counts.")
    parser.add_argument("--min-transparent-ratio", type=float, default=defaults.min_transparent_ratio)
    parser.add_argument("--min-neutral-ratio", type=float, default=defaults.min_neutral_ratio)
    parser.add_argument("--min-max-luma", type=float, default=defaults.min_max_luma)
    parser.add_argument("--metric-fallback", dest="metric_fallback", action="store_true", default=defaults.metric_fallback)
    parser.add_argument("--no-metric-fallback", dest="metric_fallback", action="store_false")
    return parser.parse_args()


def discovered_font_paths() -> list[Path]:
    if not ASSETS_DIR.exists():
        return []
    seen: set[Path] = set()
    fonts: list[Path] = []
    for path in sorted(ASSETS_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in FONT_EXTENSIONS:
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        fonts.append(path)
    return fonts


def relative_font_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def list_fonts() -> None:
    print("auto")
    print("  path: first installed known font, otherwise first font under assets")
    print("  status: available" if discovered_font_paths() else "  status: no asset fonts found")
    for choice in KNOWN_FONTS:
        aliases = ", ".join(choice.aliases)
        exists = "installed" if choice.path.exists() else "missing"
        print(f"{choice.aliases[0]} ({aliases})")
        print(f"  path: {choice.path}")
        print(f"  status: {exists}")
        print(f"  default-size: {choice.default_size}")
        print(f"  note: {choice.note}")
    known_paths = {choice.path.resolve() for choice in KNOWN_FONTS}
    asset_fonts = [path for path in discovered_font_paths() if path.resolve() not in known_paths]
    if asset_fonts:
        print("asset fonts")
    for path in asset_fonts:
        print(f"  {relative_font_path(path)}")


def download_fonts() -> None:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    for choice in KNOWN_FONTS:
        if choice.path.exists():
            print(f"font exists: {choice.path}")
            continue
        print(f"downloading {choice.aliases[0]} -> {choice.path}")
        urllib.request.urlretrieve(choice.url, choice.path)


def known_font_by_alias(value: str) -> FontChoice | None:
    normalized = value.lower().strip()
    for choice in KNOWN_FONTS:
        if normalized in choice.aliases:
            return choice
    return None


def auto_font() -> tuple[Path, FontChoice | None]:
    for choice in KNOWN_FONTS:
        if choice.path.exists():
            return choice.path, choice
    fonts = discovered_font_paths()
    if fonts:
        return fonts[0], None
    raise FileNotFoundError(f"no fonts found under {ASSETS_DIR}")


def discovered_font_by_name(value: str) -> Path | None:
    normalized = value.lower().strip()
    for path in discovered_font_paths():
        aliases = {
            path.name.lower(),
            path.stem.lower(),
            relative_font_path(path).lower(),
        }
        if normalized in aliases:
            return path
    return None


def resolve_font(value: str) -> tuple[Path, FontChoice | None]:
    if value.lower().strip() == "auto":
        return auto_font()

    choice = known_font_by_alias(value)
    if choice:
        if choice.path.exists():
            return choice.path, choice
        asset_font = discovered_font_by_name(value)
        if asset_font:
            return asset_font, None
        return auto_font()

    asset_font = discovered_font_by_name(value)
    if asset_font:
        return asset_font, None

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"font not found: {path}")
    return path, None


@lru_cache(maxsize=64)
def load_font_cached(font_path: str, size: int, font_index: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(font_path, size=size, index=font_index)
    except TypeError:
        return ImageFont.truetype(font_path, size=size)


def load_font(font_path: Path, size: int, font_index: int) -> ImageFont.FreeTypeFont:
    return load_font_cached(str(font_path), size, font_index)


def luma(color: Color) -> float:
    r, g, b, _a = color
    return 0.299 * r + 0.587 * g + 0.114 * b


def saturation(color: Color) -> int:
    return max(color[:3]) - min(color[:3])


def rgba_hex(color: Color) -> str:
    return "#{:02x}{:02x}{:02x}{:02x}".format(*color)


def unique_colors(image: Image.Image) -> list[tuple[int, Color]]:
    colors = image.getcolors(maxcolors=image.width * image.height + 1)
    if colors is not None:
        return [(count, color) for count, color in colors]
    counts: dict[Color, int] = {}
    for color in image.getdata():
        counts[color] = counts.get(color, 0) + 1
    return [(count, color) for color, count in counts.items()]


def target_metrics(image: Image.Image) -> dict[str, float | int]:
    colors = unique_colors(image)
    total = image.width * image.height
    alpha0 = sum(count for count, color in colors if color[3] == 0)
    opaque = [(count, color) for count, color in colors if color[3] > 0]
    opaque_pixels = max(0, total - alpha0)
    neutral = sum(count for count, color in opaque if saturation(color) <= 36)
    max_l = max((luma(color) for _count, color in opaque), default=0.0)
    return {
        "total": total,
        "alpha0": alpha0,
        "opaque": opaque_pixels,
        "transparent_ratio": alpha0 / total if total else 0.0,
        "neutral_ratio": neutral / opaque_pixels if opaque_pixels else 0.0,
        "max_luma": max_l,
    }


def is_target_texture(image: Image.Image, args: argparse.Namespace) -> bool:
    metrics = target_metrics(image)
    if metrics["alpha0"] == metrics["total"]:
        return False
    return (
        metrics["transparent_ratio"] >= args.min_transparent_ratio
        and metrics["neutral_ratio"] >= args.min_neutral_ratio
        and metrics["max_luma"] >= args.min_max_luma
    )


def choose_background(colors: list[tuple[int, Color]], mode: str) -> Color:
    if mode == "opaque-most-frequent":
        opaque = [(count, color) for count, color in colors if color[3] > 0]
        if opaque:
            return max(opaque, key=lambda item: item[0])[1]
        if colors:
            return max(colors, key=lambda item: item[0])[1]
        return (0, 0, 0, 255)

    transparent = [(count, color) for count, color in colors if color[3] == 0]
    if transparent:
        return max(transparent, key=lambda item: item[0])[1]
    return (0, 0, 0, 0)


def choose_text_palette(
    colors: list[tuple[int, Color]],
    background: Color,
    limit: int,
) -> list[Color]:
    foreground = [
        (count, color)
        for count, color in colors
        if color[3] > 0 and color != background and saturation(color) <= 36
    ]
    if not foreground:
        raise ValueError("no neutral foreground colors found")

    high = max(foreground, key=lambda item: (luma(item[1]), item[0]))[1]
    chosen = [high]
    if limit >= 2:
        low = min(foreground, key=lambda item: (luma(item[1]), -item[0]))[1]
        if low not in chosen:
            chosen.append(low)
    if limit >= 3:
        lo = min(luma(color) for _count, color in foreground)
        hi = luma(high)
        target = lo + (hi - lo) * 0.55
        mid = min(
            foreground,
            key=lambda item: (
                item[1] in chosen,
                abs(luma(item[1]) - target),
                -item[0],
            ),
        )[1]
        if mid not in chosen:
            chosen.insert(1, mid)

    for _count, color in sorted(foreground, key=lambda item: item[0], reverse=True):
        if len(chosen) >= limit:
            break
        if color not in chosen:
            chosen.append(color)
    return chosen[:limit]


def inspect_texture(path: Path, args: argparse.Namespace) -> TextureSpec:
    with Image.open(path) as raw:
        image = raw.convert("RGBA")
    colors = unique_colors(image)
    background = choose_background(colors, args.background_mode)
    palette = sorted({color for _count, color in colors}, key=lambda color: (color[3], luma(color), color))
    return TextureSpec(
        width=image.width,
        height=image.height,
        background=background,
        palette=palette,
        text_palette=choose_text_palette(colors, background, args.text_color_limit),
        max_cols=max(1, (image.width - args.x_padding - args.right_padding) // args.cell_size),
        max_lines=max(1, image.height // args.cell_size),
        cell_size=args.cell_size,
    )


def normalize_text(value: str, preserve_newlines: bool) -> list[str]:
    text = unicodedata.normalize("NFC", value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if preserve_newlines:
        return [line for line in text.split("\n") if line]
    text = text.replace("\n", "")
    return [text] if text else []


def half_cell_chars_from_args(args: argparse.Namespace) -> set[str]:
    return set(getattr(args, "half_cell_chars", "") or "")


def render_split_chars_from_args(args: argparse.Namespace) -> set[str]:
    return set(getattr(args, "render_split_chars", "") or "")


def can_split_render_char(char: str, half_cell_chars: set[str], split_chars: set[str]) -> bool:
    if char in half_cell_chars:
        return False
    return "*" in split_chars or char in split_chars


def char_cell_units(char: str, half_cell_chars: set[str]) -> int:
    return 1 if char in half_cell_chars else 2


def line_cell_units(line: str, half_cell_chars: set[str]) -> int:
    if not half_cell_chars:
        return len(line) * 2
    return sum(char_cell_units(char, half_cell_chars) for char in line)


def split_token_to_cells(
    token: str,
    max_cols: int,
    half_cell_chars: set[str] | None = None,
    split_chars: set[str] | None = None,
) -> list[str]:
    half_cell_chars = half_cell_chars or set()
    split_chars = split_chars or set()
    if not half_cell_chars:
        return [token[index : index + max_cols] for index in range(0, len(token), max_cols)] or [""]

    max_units = max_cols * 2
    lines: list[str] = []
    current = ""
    current_units = 0
    for char in token:
        units = char_cell_units(char, half_cell_chars)
        if current and current_units + units > max_units:
            if can_split_render_char(char, half_cell_chars, split_chars) and current_units == max_units - 1:
                current += char
                lines.append(current)
                current = ""
                current_units = 1
                continue
            lines.append(current)
            current = char
            current_units = units
            continue
        current += char
        current_units += units
    if current:
        lines.append(current)
    elif current_units > 0:
        lines.append("")
    return lines or [""]


def truncate_line_to_cell_units(line: str, max_units: int, half_cell_chars: set[str]) -> str:
    if max_units <= 0:
        return line
    if not half_cell_chars:
        return line[: max_units // 2]

    current = ""
    current_units = 0
    for char in line:
        units = char_cell_units(char, half_cell_chars)
        if current_units + units > max_units:
            break
        current += char
        current_units += units
    return current


def pad_first_half_cell_line(lines: list[str], half_cell_chars: set[str]) -> list[str]:
    if len(lines) != 2 or not half_cell_chars:
        return lines
    if line_cell_units(lines[0], half_cell_chars) % 2 == 0:
        return lines
    return [f"{lines[0]} ", lines[1]]


def logical_cell_length_for_manifest(line: str, half_cell_chars: set[str]) -> int:
    units = line_cell_units(line.rstrip(), half_cell_chars)
    return (units + 1) // 2


def split_line_by_cell_limit(line: str, max_cells: int, half_cell_chars: set[str]) -> list[str]:
    if max_cells <= 0:
        return [line] if line else []
    max_units = max_cells * 2
    lines: list[str] = []
    current = ""
    current_units = 0
    for char in line:
        units = char_cell_units(char, half_cell_chars)
        if current and current_units + units > max_units:
            lines.append(current)
            current = char
            current_units = units
            continue
        current += char
        current_units += units
    if current:
        lines.append(current)
    return lines


def opening_logical_line_lengths(
    text: str,
    half_cell_chars: set[str],
    max_cells: int = 26,
    max_lines: int = 2,
) -> str:
    logical_lines: list[str] = []
    for paragraph in normalize_text(text, preserve_newlines=True):
        logical_lines.extend(split_line_by_cell_limit(paragraph, max_cells, half_cell_chars))
    lengths = [
        logical_cell_length_for_manifest(line, half_cell_chars)
        for line in logical_lines[:max_lines]
    ]
    return format_dialogue_line_lengths(length for length in lengths if length > 0)


def validate_opening_logical_line_lengths(value: str, max_cells: int = 26, max_lines: int = 2) -> None:
    lengths = parse_dialogue_line_lengths(value)
    if len(lengths) > max_lines:
        raise ValueError(f"opening logical line count exceeds {max_lines}: {value}")
    too_wide = [length for length in lengths if length > max_cells]
    if too_wide:
        raise ValueError(f"opening logical line length exceeds {max_cells}: {value}")


def split_tail_index_for_line(
    line: str,
    max_units: int,
    half_cell_chars: set[str],
    split_chars: set[str],
    prefix_units: int = 0,
) -> int | None:
    if max_units <= 0 or not line or not split_chars:
        return None
    char = line[-1]
    if not can_split_render_char(char, half_cell_chars, split_chars):
        return None
    if prefix_units + line_cell_units(line[:-1], half_cell_chars) != max_units - 1:
        return None
    return len(line) - 1


def current_row_units(line: str, half_cell_chars: set[str], split_index: int | None) -> int:
    units = line_cell_units(line, half_cell_chars)
    return units - 1 if split_index is not None else units


def wrap_cells(
    text: str,
    max_cols: int,
    preserve_newlines: bool,
    wrap: bool,
    half_cell_chars: set[str] | None = None,
    split_chars: set[str] | None = None,
) -> list[str]:
    half_cell_chars = half_cell_chars or set()
    split_chars = split_chars or set()
    paragraphs = normalize_text(text, preserve_newlines=preserve_newlines)
    if not wrap:
        return paragraphs
    lines: list[str] = []
    for paragraph in paragraphs:
        lines.extend(split_token_to_cells(paragraph, max_cols, half_cell_chars, split_chars))
    return lines


def text_width(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont, text: str) -> int:
    if not text:
        return 0
    left, _top, right, _bottom = draw.textbbox((0, 0), text, font=font)
    return right - left


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
        for char in paragraph:
            candidate = current + char
            if not current or text_width(draw, font, candidate) <= max_width:
                current = candidate
                continue
            lines.append(current)
            current = char
        if current:
            lines.append(current)
    return lines


def forced_lines_from_lengths(
    text: str,
    line_lengths: tuple[int, ...],
    preserve_newlines: bool,
) -> tuple[list[str], list[str]]:
    if not line_lengths:
        return [], []

    normalized_lines = normalize_text(text, preserve_newlines=True)
    messages: list[str] = []
    if len(normalized_lines) == len(line_lengths) and all(
        len(line) == length for line, length in zip(normalized_lines, line_lengths)
    ):
        return normalized_lines, messages

    linear_text = "".join(normalized_lines)
    lines: list[str] = []
    cursor = 0
    for length in line_lengths:
        lines.append(linear_text[cursor : cursor + length])
        cursor += length
    if cursor < len(linear_text):
        lines.append(linear_text[cursor:])
        messages.append("CSV line control shorter than text")
    elif cursor > len(linear_text):
        messages.append("CSV line control longer than text")
    return [line for line in lines if line], messages


def continuous_text(value: str) -> str:
    return "".join(normalize_text(value, preserve_newlines=True))


def split_line_by_limit(line: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        return [line] if line else []
    return [line[index : index + max_chars] for index in range(0, len(line), max_chars)] or []


def logical_dialogue_line_lengths(
    text: str,
    max_chars: int,
    max_lines: int,
    rendered_chars: int,
) -> str:
    source_lines = normalize_text(text, preserve_newlines=True)
    lines: list[str] = []
    if len(source_lines) > 1:
        for line in source_lines:
            lines.extend(split_line_by_limit(line, max_chars))
    else:
        lines = split_line_by_limit("".join(source_lines), max_chars)

    if max_lines > 0:
        lines = lines[:max_lines]

    remaining = rendered_chars
    lengths: list[int] = []
    for line in lines:
        if remaining <= 0:
            break
        length = min(len(line), remaining)
        if length > 0:
            lengths.append(length)
            remaining -= length
    return format_dialogue_line_lengths(lengths)


@lru_cache(maxsize=128)
def font_fits_cell_cached(font_path: str, size: int, font_index: int, cell_size: int) -> bool:
    font = load_font(font_path, size, font_index)
    scratch = Image.new("L", (1, 1), 0)
    draw = ImageDraw.Draw(scratch)
    left, top, right, bottom = draw.textbbox((0, 0), "가힣A", font=font)
    return right - left <= cell_size * 3 and bottom - top <= cell_size + 2


def font_fits_cell(font_path: Path, size: int, font_index: int, cell_size: int) -> bool:
    return font_fits_cell_cached(str(font_path), size, font_index, cell_size)


def make_render_plan(
    text: str,
    spec: TextureSpec,
    args: argparse.Namespace,
    font_path: Path,
    requested_size: int,
    line_lengths: tuple[int, ...] = (),
) -> RenderPlan:
    max_lines = spec.max_lines
    if args.layout == "cell":
        size = requested_size
        half_cell_chars = half_cell_chars_from_args(args)
        split_chars = render_split_chars_from_args(args)
        if args.max_render_lines > 0 and not half_cell_chars:
            max_lines = min(max_lines, args.max_render_lines)
        max_cols = spec.max_cols
        max_units = max_cols * 2
        lines = wrap_cells(
            text,
            max_cols=max_cols,
            preserve_newlines=args.preserve_newlines,
            wrap=getattr(args, "force_cell_grid", False) or not args.no_wrap,
            half_cell_chars=half_cell_chars,
            split_chars=split_chars,
        )
        if getattr(args, "expand_height", False):
            max_lines = len(lines)
            if args.max_render_lines > 0:
                max_lines = min(max_lines, args.max_render_lines)
        overflow = len(lines) > max_lines
        too_wide = False
        fitted_lines: list[str] = []
        carry_units = 0
        selected_lines = lines[:max_lines]
        for line_index, line in enumerate(selected_lines):
            split_index = split_tail_index_for_line(line, max_units, half_cell_chars, split_chars, carry_units)
            if split_index is not None and line_index + 1 >= len(selected_lines):
                split_index = None
            if carry_units + current_row_units(line, half_cell_chars, split_index) > max_units:
                too_wide = True
                fitted_lines.append(truncate_line_to_cell_units(line, max_units, half_cell_chars))
                carry_units = 0
            else:
                fitted_lines.append(line)
                carry_units = 1 if split_index is not None else 0
        lines = fitted_lines
        return RenderPlan(
            lines=lines,
            font_size=size,
            overflow=overflow,
            too_wide=too_wide,
            font_reduced=False,
        )

    if args.max_render_lines > 0:
        max_lines = min(max_lines, args.max_render_lines)

    min_size = min(requested_size, args.min_font_size)
    scratch = Image.new("L", (1, 1), 0)
    draw = ImageDraw.Draw(scratch)
    fallback_lines: list[str] = []
    fallback_size = min_size
    fallback_too_wide = False

    for size in range(requested_size, min_size - 1, -1):
        font = load_font(font_path, size, args.font_index)
        max_width = max(1, spec.width - args.x_padding - args.right_padding)
        lines = wrap_measured(
            text,
            max_width=max_width,
            preserve_newlines=args.preserve_newlines,
            wrap=not args.no_wrap,
            draw=draw,
            font=font,
        )
        if getattr(args, "expand_height", False):
            max_lines = len(lines)
            if args.max_render_lines > 0:
                max_lines = min(max_lines, args.max_render_lines)
        too_wide = any(text_width(draw, font, line) > max_width for line in lines)
        fallback_lines = lines
        fallback_size = size
        fallback_too_wide = too_wide
        if lines and len(lines) <= max_lines and not too_wide:
            return RenderPlan(
                lines=lines,
                font_size=size,
                overflow=False,
                too_wide=False,
                font_reduced=size != requested_size,
            )

    return RenderPlan(
        lines=fallback_lines[:max_lines],
        font_size=fallback_size,
        overflow=len(fallback_lines) > max_lines,
        too_wide=fallback_too_wide,
        font_reduced=fallback_size != requested_size,
    )


def draw_mask(
    spec: TextureSpec,
    plan: RenderPlan,
    args: argparse.Namespace,
    font_path: Path,
) -> Image.Image:
    mask = Image.new("L", (spec.width, spec.height), 0)
    draw = ImageDraw.Draw(mask)
    font = load_font(font_path, plan.font_size, args.font_index)

    if args.layout == "cell":
        half_cell_chars = half_cell_chars_from_args(args)
        split_chars = render_split_chars_from_args(args)
        max_units = spec.max_cols * 2
        split_indices: list[int | None] = []
        prefix_units = [0 for _line in plan.lines]
        for row_index, line in enumerate(plan.lines):
            split_index = split_tail_index_for_line(
                line,
                max_units,
                half_cell_chars,
                split_chars,
                prefix_units[row_index],
            )
            if split_index is not None and row_index + 1 >= len(plan.lines):
                split_index = None
            split_indices.append(split_index)
            if split_index is not None:
                prefix_units[row_index + 1] += 1

        def split_cell_halves(char: str) -> tuple[Image.Image, Image.Image]:
            left, top, right, bottom = draw.textbbox((0, 0), char, font=font)
            glyph_w = right - left
            glyph_h = bottom - top
            cell = Image.new("L", (spec.cell_size, spec.cell_size), 0)
            cell_draw = ImageDraw.Draw(cell)
            x = (spec.cell_size - glyph_w) // 2 - left
            y = (spec.cell_size - glyph_h) // 2 - top + args.y_adjust
            cell_draw.text((x, y), char, font=font, fill=255)
            half_width = spec.cell_size // 2
            return (
                cell.crop((0, 0, half_width, spec.cell_size)),
                cell.crop((half_width, 0, spec.cell_size, spec.cell_size)),
            )

        def render_cell_glyph(char: str, cell_width: int) -> Image.Image:
            left, top, right, bottom = draw.textbbox((0, 0), char, font=font)
            glyph_w = right - left
            glyph_h = bottom - top
            cell = Image.new("L", (cell_width, spec.cell_size), 0)
            cell_draw = ImageDraw.Draw(cell)
            if char in LOWER_LEFT_CELL_PUNCTUATION:
                x = LOWER_LEFT_CELL_PADDING - left
                y = spec.cell_size - LOWER_LEFT_CELL_PADDING - glyph_h - top + args.y_adjust
            else:
                x = (cell_width - glyph_w) // 2 - left
                y = (spec.cell_size - glyph_h) // 2 - top + args.y_adjust
            cell_draw.text((x, y), char, font=font, fill=255)
            return cell

        for row_index, line in enumerate(plan.lines):
            split_index = split_indices[row_index]
            line_units = prefix_units[row_index] + current_row_units(line, half_cell_chars, split_index)
            line_width = line_units * spec.cell_size // 2
            if args.align == "right":
                base_x = spec.width - args.right_padding - line_width
            elif args.align == "center":
                base_x = (spec.width - line_width) // 2
            else:
                base_x = args.x_padding
            base_x = max(0, min(base_x, max(0, spec.width - line_width)))
            row_top = row_index * spec.cell_size
            if prefix_units[row_index] and row_index > 0:
                previous_split = split_indices[row_index - 1]
                if previous_split is not None:
                    _left_half, right_half = split_cell_halves(plan.lines[row_index - 1][previous_split])
                    mask.paste(right_half, (base_x, row_top), right_half)
            cell_left = base_x + prefix_units[row_index] * spec.cell_size // 2
            for char_index, char in enumerate(line):
                cell_width = char_cell_units(char, half_cell_chars) * spec.cell_size // 2
                if char == " ":
                    cell_left += cell_width
                    continue
                if char_index == split_index:
                    left_half, _right_half = split_cell_halves(char)
                    mask.paste(left_half, (cell_left, row_top), left_half)
                    cell_left += spec.cell_size // 2
                    continue
                cell = render_cell_glyph(char, cell_width)
                mask.paste(cell, (cell_left, row_top), cell)
                cell_left += cell_width
        return mask

    row_height = spec.cell_size
    for row_index, line in enumerate(plan.lines):
        left, top, right, bottom = draw.textbbox((0, 0), line, font=font)
        glyph_w = right - left
        glyph_h = bottom - top
        if args.align == "right":
            x = spec.width - args.right_padding - glyph_w - left
        elif args.align == "center":
            x = (spec.width - glyph_w) // 2 - left
        else:
            x = args.x_padding - left
        y = row_index * row_height + (row_height - glyph_h) // 2 - top + args.y_adjust
        draw.text((x, y), line, font=font, fill=255)
    return mask


def threshold_mask(mask: Image.Image, low: int, high: int) -> Image.Image:
    return mask.point(lambda value: 255 if low <= value <= high else 0)


def full_text_palette(spec: TextureSpec) -> list[Color]:
    """Return all original neutral text grays, darkest -> brightest.

    This replaces the old 3-tone quantization. Both transparent-background
    and black-background renderers now use the full source text palette by
    default, which matches sample 1 (fullpalette_linear).
    """
    shades = [
        color
        for color in spec.palette
        if color != spec.background and color[3] > 0 and saturation(color) <= 36
    ]
    return sorted(shades, key=lambda color: (luma(color), color))


def linear_coverage_edges(band_count: int) -> list[int]:
    if band_count <= 0:
        return []
    return [max(1, round((index + 1) * 255 / band_count)) for index in range(band_count)]


def mask_to_texture(mask: Image.Image, spec: TextureSpec) -> Image.Image:
    image = Image.new("RGBA", (spec.width, spec.height), spec.background)
    shades = full_text_palette(spec)
    if not shades:
        return image

    previous_high = 0
    for high, color in zip(linear_coverage_edges(len(shades)), shades):
        low = previous_high + 1
        previous_high = high
        image.paste(color, mask=threshold_mask(mask, low, high))
    return image


def expanded_spec_for_plan(spec: TextureSpec, plan: RenderPlan, args: argparse.Namespace) -> TextureSpec:
    if not getattr(args, "expand_height", False):
        return spec
    required_height = max(spec.height, len(plan.lines) * spec.cell_size)
    if required_height % spec.cell_size:
        required_height = ((required_height + spec.cell_size - 1) // spec.cell_size) * spec.cell_size
    if required_height == spec.height:
        return spec
    return replace(
        spec,
        height=required_height,
        max_lines=max(1, required_height // spec.cell_size),
    )


def validate_palette(image: Image.Image, palette: Iterable[Color]) -> list[Color]:
    allowed = set(palette)
    return sorted({color for color in image.getdata() if color not in allowed})


def used_palette_colors(image: Image.Image) -> list[Color]:
    return [
        color
        for _count, color in sorted(
            unique_colors(image),
            key=lambda item: (item[1][3] == 255, -item[0], item[1]),
        )
    ]


def encode_rgba_png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.info.clear()
    image.save(out, format="PNG", optimize=True, compress_level=9)
    return out.getvalue()


def encode_indexed_png(image: Image.Image) -> bytes | None:
    colors = used_palette_colors(image)
    if len(colors) > 256:
        return None
    color_to_index = {color: index for index, color in enumerate(colors)}
    indexed = Image.new("P", image.size)
    indexed.putdata([color_to_index[color] for color in image.getdata()])
    palette: list[int] = []
    alpha: list[int] = []
    for red, green, blue, value in colors:
        palette.extend((red, green, blue))
        alpha.append(value)
    indexed.putpalette(palette)
    while alpha and alpha[-1] == 255:
        alpha.pop()
    if alpha:
        indexed.info["transparency"] = bytes(alpha)
    out = io.BytesIO()
    indexed.save(out, format="PNG", optimize=True, compress_level=9)
    return out.getvalue()


def encode_optimized_png(image: Image.Image) -> tuple[bytes, str]:
    rgba = encode_rgba_png(image)
    indexed = encode_indexed_png(image)
    if indexed is not None and len(indexed) < len(rgba):
        return indexed, "indexed"
    return rgba, "rgba"


def save_png(image: Image.Image, path: Path) -> tuple[int, str]:
    data, storage = encode_optimized_png(image)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data), storage


def render_one(
    output: str,
    text: str,
    line_lengths: tuple[int, ...],
    source_path: Path,
    target_path: Path,
    args: argparse.Namespace,
    font_path: Path,
    font_label: str,
    font_size: int,
    pattern_id: str,
) -> RenderResult:
    source_bytes = source_path.stat().st_size
    spec = inspect_texture(source_path, args)
    half_cell_chars = half_cell_chars_from_args(args)
    logical_lines = normalize_text(text, preserve_newlines=True)
    render_logical_lines = logical_lines
    if half_cell_chars:
        if args.max_render_lines > 0:
            render_logical_lines = render_logical_lines[: args.max_render_lines]
        if args.max_line_chars > 0:
            max_logical_units = args.max_line_chars * 2
            render_logical_lines = [
                truncate_line_to_cell_units(line, max_logical_units, half_cell_chars)
                for line in render_logical_lines
            ]
    if getattr(args, "pad_half_cell_first_line", False):
        render_logical_lines = pad_first_half_cell_line(render_logical_lines, half_cell_chars)
    if getattr(args, "force_cell_grid", False):
        render_text = "".join(render_logical_lines) if half_cell_chars else continuous_text(text)
    else:
        render_text = text
    if not normalize_text(render_text, args.preserve_newlines):
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=spec.width,
            height=spec.height,
            source_png_bytes=source_bytes,
            pattern_id=pattern_id,
            message="empty text",
        )
    plan = make_render_plan(render_text, spec, args, font_path, font_size, line_lengths=line_lengths)
    if not plan.lines:
        return RenderResult(
            status="skipped",
            output=output,
            target=target_path,
            width=spec.width,
            height=spec.height,
            source_png_bytes=source_bytes,
            pattern_id=pattern_id,
            message="empty render plan",
        )

    spec = expanded_spec_for_plan(spec, plan, args)
    mask = draw_mask(spec, plan, args, font_path)
    image = mask_to_texture(mask, spec)
    invalid = validate_palette(image, spec.palette)
    if invalid:
        sample = ", ".join(rgba_hex(color) for color in invalid[:4])
        raise ValueError(f"generated colors not present in source palette: {sample}")

    messages: list[str] = []
    if plan.overflow:
        messages.append(f"overflow: rendered first {len(plan.lines)} lines")
    if plan.too_wide:
        messages.append("line wider than texture")
    if half_cell_chars and args.max_render_lines > 0 and len(logical_lines) > args.max_render_lines:
        messages.append(f"logical line count {len(logical_lines)}/{args.max_render_lines}")
    if half_cell_chars and args.max_line_chars > 0:
        limit_units = args.max_line_chars * 2
        wide_lines = [
            index + 1
            for index, line in enumerate(logical_lines)
            if line_cell_units(line, half_cell_chars) > limit_units
        ]
        if wide_lines:
            messages.append(f"logical line over {args.max_line_chars} cells: {','.join(str(index) for index in wide_lines)}")
    if plan.font_reduced:
        messages.append(f"font reduced to {plan.font_size}")

    rendered_chars = sum(len(line) for line in plan.lines)
    is_opening_text = (getattr(args, "target_verified_group", "") or "").strip() == "각 세력 오프닝"
    if half_cell_chars:
        if is_opening_text:
            dialogue_line_lengths = opening_logical_line_lengths(text, half_cell_chars)
        elif getattr(args, "expand_height", False):
            dialogue_line_lengths = format_dialogue_line_lengths(
                length
                for length in (
                    logical_cell_length_for_manifest(line, half_cell_chars)
                    for line in render_logical_lines
                )
                if length > 0
            )
        else:
            max_logical_lines = args.max_render_lines if args.max_render_lines > 0 else len(render_logical_lines)
            dialogue_length_lines = render_logical_lines[:max_logical_lines]
            dialogue_line_lengths = format_dialogue_line_lengths(
                length
                for length in (
                    logical_cell_length_for_manifest(line, half_cell_chars)
                    for line in dialogue_length_lines
                )
                if length > 0
            )
    elif getattr(args, "force_cell_grid", False):
        dialogue_line_lengths = logical_dialogue_line_lengths(
            text,
            max_chars=args.max_line_chars,
            max_lines=args.max_render_lines,
            rendered_chars=rendered_chars,
        )
    else:
        dialogue_line_lengths = format_dialogue_line_lengths(
            len(line)
            for line in plan.lines
            if line
        )
    if is_opening_text:
        validate_opening_logical_line_lengths(dialogue_line_lengths)

    if args.dry_run:
        data, storage = encode_optimized_png(image)
        generated_bytes = len(data)
    else:
        generated_bytes, storage = save_png(image, target_path)

    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=spec.width,
        height=spec.height,
        font=font_label,
        font_size=plan.font_size,
        line_count=len(plan.lines),
        max_lines=spec.max_lines if half_cell_chars else (
            min(spec.max_lines, args.max_render_lines) if args.max_render_lines > 0 else spec.max_lines
        ),
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        png_storage=storage,
        pattern_id=pattern_id,
        dialogue_line_lengths=dialogue_line_lengths,
        message="; ".join(messages),
        text_palette=spec.text_palette,
    )


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_pattern_targets(path: Path, target_column: str) -> dict[str, PatternTarget]:
    if not path.exists():
        return {}
    targets: dict[str, PatternTarget] = {}
    for row in read_rows(path):
        output = (row.get("output") or "").strip().replace("\\", "/")
        if not output:
            continue
        target: bool | None = None
        if target_column:
            target = (row.get(target_column) or "").lower() == "yes"
        targets[output] = PatternTarget(target=target, pattern_id=row.get("pattern_id") or "")
    return targets


def matches_only(output: str, only: str | None) -> bool:
    if not only:
        return True
    needle = only.replace("\\", "/")
    return output == needle or output.endswith(needle) or needle in output


def matches_verified_group(value: str, target: str) -> bool:
    return value.strip() == target.strip()


def target_for(output: str, args: argparse.Namespace) -> Path:
    if args.apply:
        return Path(args.textures_root) / output
    return Path(args.out_root) / output


def write_report(path: Path, results: list[RenderResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "status",
        "output",
        "target",
        "pattern_id",
        "width",
        "height",
        "font",
        "font_size",
        "line_count",
        "max_lines",
        "source_png_bytes",
        "generated_png_bytes",
        "png_delta_bytes",
        "png_storage",
        "dialogue_line_lengths",
        "text_palette",
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
                    "pattern_id": result.pattern_id,
                    "width": result.width,
                    "height": result.height,
                    "font": result.font,
                    "font_size": result.font_size,
                    "line_count": result.line_count,
                    "max_lines": result.max_lines,
                    "source_png_bytes": result.source_png_bytes,
                    "generated_png_bytes": result.generated_png_bytes,
                    "png_delta_bytes": result.generated_png_bytes - result.source_png_bytes
                    if result.generated_png_bytes and result.source_png_bytes
                    else 0,
                    "png_storage": result.png_storage,
                    "dialogue_line_lengths": result.dialogue_line_lengths,
                    "text_palette": " ".join(rgba_hex(color) for color in result.text_palette),
                    "message": result.message,
                }
            )


def merge_manifest_records(existing: list[dict], incoming: list[dict]) -> list[dict]:
    merged = [dict(record) for record in existing]
    positions = {manifest_record_key(record): index for index, record in enumerate(merged)}
    for record in incoming:
        copied = dict(record)
        key = manifest_record_key(copied)
        if key in positions:
            merged[positions[key]] = copied
        else:
            positions[key] = len(merged)
            merged.append(copied)
    return merged


def record_allows_rendered_line_lengths(record: dict) -> bool:
    if parse_dialogue_line_lengths(record.get("dialogue_line_lengths", "")):
        return True
    try:
        return int(record.get("dialogue_line_control_offset") or 0) > 0
    except ValueError:
        return False


def rendered_lengths_for_record(
    record: dict,
    output: str,
    row_line_lengths: dict[tuple[str, str, str], str],
    output_line_lengths: dict[str, str],
    rendered_line_lengths: dict[str, str],
) -> str:
    if record_allows_rendered_line_lengths(record):
        lengths = rendered_line_lengths.get(output, "")
        if lengths:
            return lengths
    return row_line_lengths.get(manifest_record_key(record)) or output_line_lengths.get(output, "")


def apply_rendered_dimensions(record: dict, output: str, dimensions: dict[str, tuple[int, int]]) -> None:
    size = dimensions.get(output)
    if not size:
        return
    width, height = size
    record["width"] = width
    record["height"] = height
    record["storage_width"] = width
    record["storage_height"] = height
    record["output_crop_x"] = 0
    record["output_crop_y"] = 0
    record["output_crop_width"] = width
    record["output_crop_height"] = height


def merged_fieldnames(existing: Iterable[str], incoming: Iterable[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for name in list(existing) + list(incoming):
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def write_filtered_manifests(
    textures_root: Path,
    out_root: Path,
    csv_path: Path,
    rows: list[dict[str, str]],
    output_column: str,
    rendered: set[str],
    rendered_line_lengths: dict[str, str],
    rendered_dimensions: dict[str, tuple[int, int]],
) -> list[Path]:
    if not rendered:
        return []
    out_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    row_line_lengths: dict[tuple[str, str, str], str] = {}
    output_line_lengths: dict[str, str] = {}
    for row in rows:
        output = (row.get(output_column) or "").strip().replace("\\", "/")
        lengths = format_dialogue_line_lengths(
            parse_dialogue_line_lengths(row.get("dialogue_line_lengths", ""))
        )
        if not output or not lengths:
            continue
        row_line_lengths[manifest_record_key(row)] = lengths
        output_line_lengths.setdefault(output, lengths)

    manifest_json = textures_root / "manifest.json"
    if manifest_json.exists():
        records = json.loads(manifest_json.read_text(encoding="utf-8"))
        incoming = []
        for record in records:
            output = str(record.get("output", "")).replace("\\", "/")
            if output not in rendered:
                continue
            copied = dict(record)
            lengths = rendered_lengths_for_record(
                record,
                output,
                row_line_lengths,
                output_line_lengths,
                rendered_line_lengths,
            )
            if lengths:
                copied["dialogue_line_lengths"] = lengths
                copied["dialogue_line_count"] = len(lengths.split(","))
            apply_rendered_dimensions(copied, output, rendered_dimensions)
            incoming.append(copied)
        target = out_root / "manifest.json"
        existing = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
        merged = merge_manifest_records(existing, incoming)
        target.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(target)

    if rows:
        target = out_root / "manifest.csv"
        existing_rows: list[dict[str, str]] = []
        existing_fieldnames: list[str] = []
        if target.exists():
            with target.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                existing_fieldnames = list(reader.fieldnames or [])
                existing_rows = list(reader)
        fieldnames = merged_fieldnames(existing_fieldnames, rows[0].keys())
        for field in ("dialogue_line_count", "dialogue_line_lengths"):
            if field not in fieldnames:
                fieldnames.append(field)
        incoming_rows: list[dict[str, str]] = []
        for row in rows:
            output = (row.get(output_column) or "").strip().replace("\\", "/")
            if output in rendered:
                copied = dict(row)
                lengths = rendered_lengths_for_record(
                    row,
                    output,
                    row_line_lengths,
                    output_line_lengths,
                    rendered_line_lengths,
                )
                if lengths:
                    copied["dialogue_line_lengths"] = lengths
                    copied["dialogue_line_count"] = str(len(lengths.split(",")))
                apply_rendered_dimensions(copied, output, rendered_dimensions)
                incoming_rows.append(copied)
        merged_rows = merge_manifest_records(existing_rows, incoming_rows)
        fieldnames = merged_fieldnames(fieldnames, (key for row in merged_rows for key in row.keys()))
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(merged_rows)
        written.append(target)

    return written


TRANSPARENT_DEFAULTS = RendererDefaults(
    description="Render Korean PNGs for TP01 transparent white/gray dialogue textures using fullpalette_linear by default.",
    out_root="textures_translated",
    font="assets/fonts/NanumMyeongjoExtraBold.ttf",
    font_size=15,
    min_font_size=8,
    cell_size=16,
    layout="cell",
    align="left",
    text_color_limit=3,
    pattern_target_column="target_white_transparent_renderer",
    target_verified_group="대사들",
    background_mode="transparent",
    metric_fallback=True,
    preserve_newlines=True,
    force_cell_grid=True,
    max_line_chars=21,
    expand_height=True,
)


BLACK_BACKGROUND_DEFAULTS = RendererDefaults(
    description="Render Korean PNGs for black-background faction opening text textures using fullpalette_linear by default.",
    out_root="textures_translated",
    font="assets/fonts/NanumMyeongjoExtraBold.ttf",
    font_size=15,
    min_font_size=15,
    cell_size=16,
    layout="cell",
    align="left",
    text_color_limit=3,
    pattern_target_column="",
    target_verified_group="각 세력 오프닝",
    background_mode="opaque-most-frequent",
    metric_fallback=False,
    preserve_newlines=True,
    no_wrap=True,
    force_cell_grid=True,
    max_render_lines=0,
    max_line_chars=0,
    expand_height=True,
    half_cell_chars=" .,!，．！ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvxyz1234567890",
    render_split_chars="*",
    pad_half_cell_first_line=True,
)


def main(defaults: RendererDefaults | None = None) -> int:
    defaults = defaults or TRANSPARENT_DEFAULTS
    args = parse_args(defaults)
    if args.list_fonts:
        list_fonts()
        return 0
    if args.download_fonts:
        download_fonts()

    if args.cell_size <= 0:
        print("Error: --cell-size must be positive", file=sys.stderr)
        return 2
    if args.min_font_size <= 0:
        print("Error: --min-font-size must be positive", file=sys.stderr)
        return 2
    if args.max_render_lines < 0:
        print("Error: --max-render-lines cannot be negative", file=sys.stderr)
        return 2
    if args.max_line_chars < 0:
        print("Error: --max-line-chars cannot be negative", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit <= 0:
        print("Error: --limit must be positive", file=sys.stderr)
        return 2
    if defaults.force_cell_grid:
        args.force_cell_grid = True
        args.layout = "cell"
        args.min_font_size = args.font_size or defaults.font_size or args.min_font_size
    else:
        args.force_cell_grid = False
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
    requested_font_size = args.font_size or (font_choice.default_size if font_choice else 12)
    font_label = font_choice.aliases[0] if font_choice else str(font_path)

    csv_path = Path(args.csv)
    textures_root = Path(args.textures_root)
    rows = read_rows(csv_path)
    if rows and args.output_column not in rows[0]:
        print(f"Error: missing CSV column: {args.output_column}", file=sys.stderr)
        return 2
    if rows and args.text_column not in rows[0]:
        print(f"Error: missing CSV column: {args.text_column}", file=sys.stderr)
        return 2

    pattern_targets = {} if args.ignore_pattern_csv else load_pattern_targets(
        Path(args.pattern_csv),
        args.pattern_target_column,
    )
    if args.verbose:
        if pattern_targets:
            target_count = sum(1 for target in pattern_targets.values() if target.target)
            if args.pattern_target_column:
                print(f"Pattern targets loaded: {target_count}")
            else:
                print(f"Pattern metadata loaded: {len(pattern_targets)}")
        else:
            print("Pattern CSV not used.")
        print(f"Using pixel font: {font_label} ({font_path}) size={requested_font_size}")
        print(f"Output mode: {'apply to source textures' if args.apply else args.out_root}")
        if args.target_verified_group:
            print(f"Target verified_group: {args.target_verified_group}")
        if row_filter:
            print(f"CSV row range: {args.rows}")

    results: list[RenderResult] = []
    rendered: set[str] = set()
    rendered_line_lengths: dict[str, str] = {}
    rendered_dimensions: dict[str, tuple[int, int]] = {}
    seen_text: dict[str, str] = {}
    failures = 0
    skipped_not_target = 0
    duplicate_rows = 0

    for row_number, row in enumerate(rows, 1):
        output = (row.get(args.output_column) or "").strip().replace("\\", "/")
        if not output:
            continue
        pattern_target = pattern_targets.get(output)
        in_target_scope = True
        if args.target_verified_group and not matches_verified_group(
            row.get("verified_group") or "",
            args.target_verified_group,
        ):
            in_target_scope = False
        if pattern_target and pattern_target.target is False:
            in_target_scope = False
        if row_filter and not row_filter.contains(row_number):
            continue
        if not matches_only(output, args.only):
            continue
        if not in_target_scope:
            skipped_not_target += 1
            continue
        source_path = textures_root / output
        pattern_id = pattern_target.pattern_id if pattern_target else ""
        if not source_path.exists():
            failures += 1
            results.append(
                RenderResult(
                    status="error",
                    output=output,
                    target=target_for(output, args),
                    pattern_id=pattern_id,
                    message=f"source image not found: {source_path}",
                )
            )
            if args.strict:
                break
            continue
        if (
            not args.target_verified_group
            and (not pattern_target or pattern_target.target is None)
            and args.metric_fallback
        ):
            with Image.open(source_path) as raw:
                if not is_target_texture(raw.convert("RGBA"), args):
                    skipped_not_target += 1
                    continue
        elif (
            not args.target_verified_group
            and (not pattern_target or pattern_target.target is None)
            and not args.metric_fallback
        ):
            skipped_not_target += 1
            continue
        text = row.get(args.text_column) or ""
        previous = seen_text.get(output)
        normalized_text = normalize_text(text, args.preserve_newlines)
        render_text = "\n".join(normalized_text) if normalized_text else ""
        if previous is not None:
            if previous != render_text and render_text:
                failures += 1
                message = "duplicate output has different text"
                print(f"[error] {output}: {message}", file=sys.stderr)
                results.append(
                    RenderResult(
                        status="error",
                        output=output,
                        target=target_for(output, args),
                        pattern_id=pattern_id,
                        message=message,
                    )
                )
                if args.strict:
                    break
            duplicate_rows += 1
            continue
        seen_text[output] = render_text

        try:
            render_args = args
            result = render_one(
                output=output,
                text=text,
                line_lengths=(),
                source_path=source_path,
                target_path=target_for(output, render_args),
                args=render_args,
                font_path=font_path,
                font_label=font_label,
                font_size=requested_font_size,
                pattern_id=pattern_id,
            )
        except Exception as exc:
            failures += 1
            result = RenderResult(
                status="error",
                output=output,
                target=target_for(output, args),
                pattern_id=pattern_id,
                message=str(exc),
            )
            print(f"[error] {output}: {exc}")
            if args.strict:
                results.append(result)
                break

        results.append(result)
        if result.status in {"ok", "warning"}:
            rendered.add(output)
            if result.dialogue_line_lengths:
                rendered_line_lengths[output] = result.dialogue_line_lengths
            rendered_dimensions[output] = (result.width, result.height)
            if args.verbose:
                print(f"[{result.status}] {output} {result.width}x{result.height} lines={result.line_count}/{result.max_lines} {result.message}")
            if args.limit and len(rendered) >= args.limit:
                break
        elif result.status == "skipped" and args.verbose:
            print(f"[skipped] {output}: {result.message}")

    if args.report:
        write_report(Path(args.report), results)
        print(f"Report: {args.report}")

    if not args.dry_run and not args.apply and not args.no_copy_manifest:
        written = write_filtered_manifests(
            textures_root=textures_root,
            out_root=Path(args.out_root),
            csv_path=csv_path,
            rows=rows,
            output_column=args.output_column,
            rendered=rendered,
            rendered_line_lengths=rendered_line_lengths,
            rendered_dimensions=rendered_dimensions,
        )
        for path in written:
            print(f"Wrote {path}")

    ok = sum(1 for result in results if result.status == "ok")
    warnings = sum(1 for result in results if result.status == "warning")
    skipped = sum(1 for result in results if result.status == "skipped")
    errors = sum(1 for result in results if result.status == "error")
    if args.verbose:
        print(
            f"Done. rendered={len(rendered)} ok={ok} warning={warnings} "
            f"skipped_empty={skipped} skipped_not_target={skipped_not_target} "
            f"duplicate_rows={duplicate_rows} errors={errors}"
        )
    else:
        print(
            f"Done. rendered={len(rendered)} skipped_empty={skipped} "
            f"skipped_not_target={skipped_not_target} duplicate_rows={duplicate_rows} "
            f"errors={errors}"
        )
    if failures or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
