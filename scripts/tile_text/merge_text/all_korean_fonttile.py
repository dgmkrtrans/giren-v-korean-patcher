#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE_TEXTS = Path(__file__).resolve().with_name("all_merge_font_source_texts.json")
DEFAULT_OUTPUT = Path(__file__).resolve().with_name("all_korean_fonttile.png")
DEFAULT_MAP_OUTPUT = Path(__file__).resolve().with_name("all_korean_fonttile_map.csv")
DEFAULT_SMALL_FONT_TILE_PNG = (
    PROJECT_ROOT / "textures_static/text/000542-7c454b44299a_256x40_000002e6_header_0.png"
)
DEFAULT_DALMOORI_FONT = PROJECT_ROOT / "assets/fonts/dalmoori.ttf"
SMALL_FONT_CELL_COLUMNS = 32
SMALL_FONT_CELL_SIZE = 8
DALMOORI_GRID_FONT_SIZE = 64
DALMOORI_GRID_SAMPLE_STEP = 8
DEFAULT_WIDTH = SMALL_FONT_CELL_COLUMNS * SMALL_FONT_CELL_SIZE


@dataclass(frozen=True)
class GlyphPlacement:
    glyph: str
    tile_index: int
    source_text_index: int
    source_char_index: int

    @property
    def column(self) -> int:
        return self.tile_index % SMALL_FONT_CELL_COLUMNS

    @property
    def row(self) -> int:
        return self.tile_index // SMALL_FONT_CELL_COLUMNS

    @property
    def x(self) -> int:
        return self.column * SMALL_FONT_CELL_SIZE

    @property
    def y(self) -> int:
        return self.row * SMALL_FONT_CELL_SIZE


@dataclass(frozen=True)
class GlyphSourcePosition:
    text_index: int
    char_index: int


def load_source_texts(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a JSON array")
    for index, entry in enumerate(data):
        if not isinstance(entry, str):
            raise SystemExit(f"{path}: entry {index} must be a string")
    return data


def duplicate_glyph_positions(texts: list[str]) -> dict[str, list[GlyphSourcePosition]]:
    positions_by_glyph: dict[str, list[GlyphSourcePosition]] = {}
    for text_index, text in enumerate(texts):
        for char_index, glyph in enumerate(text):
            positions_by_glyph.setdefault(glyph, []).append(
                GlyphSourcePosition(text_index=text_index, char_index=char_index)
            )
    return {
        glyph: positions
        for glyph, positions in positions_by_glyph.items()
        if len(positions) > 1
    }


def validate_no_duplicate_glyphs(texts: list[str], source_path: Path) -> None:
    duplicates = duplicate_glyph_positions(texts)
    if not duplicates:
        return

    print(f"{source_path}: duplicate glyphs found:", file=sys.stderr)
    for glyph, positions in duplicates.items():
        labels = ", ".join(
            f"text[{position.text_index}] char[{position.char_index}]"
            for position in positions
        )
        print(f"  {glyph} ({labels})", file=sys.stderr)
    raise SystemExit(f"{len(duplicates)} duplicate glyph(s) found")


def color_luma(color: tuple[int, int, int, int]) -> float:
    return (0.2126 * color[0]) + (0.7152 * color[1]) + (0.0722 * color[2])


def color_distance(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
) -> int:
    return sum((a - b) * (a - b) for a, b in zip(left, right))


def unique_rgba_colors(image: object) -> list[tuple[int, int, int, int]]:
    colors: set[tuple[int, int, int, int]] = set()
    pixels = image.convert("RGBA").load()
    for y in range(image.height):
        for x in range(image.width):
            colors.add(pixels[x, y])
    return list(colors)


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


def snap_rgba_to_colors(image: object, palette: list[tuple[int, int, int, int]]) -> object:
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


def render_pillow_bbox_glyph_mask(char: str, font: object) -> object:
    from PIL import Image, ImageDraw

    scratch = Image.new("L", (32, 32), 0)
    draw = ImageDraw.Draw(scratch)
    left, top, right, bottom = draw.textbbox((0, 0), char, font=font)
    width = max(1, right - left)
    height = max(1, bottom - top)
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.text((-left, -top), char, font=font, fill=255)
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


def glyph_placements(texts: list[str]) -> list[GlyphPlacement]:
    placements: list[GlyphPlacement] = []
    for text_index, text in enumerate(texts):
        for char_index, glyph in enumerate(text):
            placements.append(
                GlyphPlacement(
                    glyph=glyph,
                    tile_index=len(placements),
                    source_text_index=text_index,
                    source_char_index=char_index,
                )
            )
    return placements


def write_mapping_csv(path: Path, placements: list[GlyphPlacement]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "glyph",
                "codepoint",
                "tile_index",
                "tile_index_hex",
                "column",
                "row",
                "src_x",
                "src_y",
                "source_text_index",
                "source_char_index",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        for placement in placements:
            writer.writerow(
                {
                    "glyph": placement.glyph,
                    "codepoint": f"U+{ord(placement.glyph):04X}",
                    "tile_index": placement.tile_index,
                    "tile_index_hex": f"0x{placement.tile_index:02x}",
                    "column": placement.column,
                    "row": placement.row,
                    "src_x": placement.x,
                    "src_y": placement.y,
                    "source_text_index": placement.source_text_index,
                    "source_char_index": placement.source_char_index,
                }
            )


def render_all_korean_fonttile(args: argparse.Namespace) -> None:
    from PIL import Image, ImageFont

    if args.width <= 0 or args.width % SMALL_FONT_CELL_SIZE:
        raise SystemExit("--width must be a positive multiple of 8")
    if args.width != DEFAULT_WIDTH:
        raise SystemExit("this fonttile format uses a fixed 256px width")

    texts = load_source_texts(args.source_texts)
    validate_no_duplicate_glyphs(texts, args.source_texts)
    placements = glyph_placements(texts)
    if not placements:
        raise SystemExit("source text list does not contain any glyphs")

    font_path = args.font
    if not font_path.is_absolute():
        font_path = Path.cwd() / font_path
    if not font_path.exists():
        raise SystemExit(f"font not found: {font_path}")

    with Image.open(args.palette_source) as raw:
        palette_source = raw.convert("RGBA")
    palette = fonttile_source_palette(palette_source)
    background, foreground = fonttile_background_and_foreground(palette)

    rows = (len(placements) + SMALL_FONT_CELL_COLUMNS - 1) // SMALL_FONT_CELL_COLUMNS
    image = Image.new(
        "RGBA",
        (DEFAULT_WIDTH, rows * SMALL_FONT_CELL_SIZE),
        background,
    )

    font_size = (
        DALMOORI_GRID_FONT_SIZE if args.font_renderer == "dalmoori-grid" else args.font_size
    )
    font = ImageFont.truetype(str(font_path), size=font_size, index=args.font_index)

    for placement in placements:
        mask = render_glyph_mask(placement.glyph, font, args.font_renderer)
        paste_x = placement.x + max(0, (SMALL_FONT_CELL_SIZE - mask.width) // 2)
        paste_y = placement.y + max(0, (SMALL_FONT_CELL_SIZE - mask.height) // 2)
        layer = Image.new("RGBA", mask.size, foreground)
        image.paste(layer, (paste_x, paste_y), mask=mask)

    image = snap_rgba_to_colors(image, palette)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    print(f"rendered {len(placements)} glyphs ({SMALL_FONT_CELL_COLUMNS}x{rows}) to {args.output}")

    write_mapping_csv(args.map_output, placements)
    print(f"wrote glyph map to {args.map_output}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Render every glyph from all_merge_font_source_texts.json into a fresh "
            "256px-wide 8x8 fonttile image and write its tile-index map."
        )
    )
    parser.add_argument("--source-texts", type=Path, default=DEFAULT_SOURCE_TEXTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--map-output", type=Path, default=DEFAULT_MAP_OUTPUT)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--palette-source", type=Path, default=DEFAULT_SMALL_FONT_TILE_PNG)
    parser.add_argument("--font", type=Path, default=DEFAULT_DALMOORI_FONT)
    parser.add_argument("--font-index", type=int, default=0)
    parser.add_argument("--font-size", type=int, default=8)
    parser.add_argument(
        "--font-renderer",
        choices=("dalmoori-grid", "pillow-bbox"),
        default="dalmoori-grid",
    )
    args = parser.parse_args()
    render_all_korean_fonttile(args)


if __name__ == "__main__":
    main()
