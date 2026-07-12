#!/usr/bin/env python3
"""Render translated labels onto the raw PNG tail extracted from ZZZPSP9.MKD."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FONT = PROJECT_ROOT / "assets/fonts/NanumMyeongjoExtraBold.ttf"
DEFAULT_CSV = PROJECT_ROOT / "iso_mkd/translate.csv"
DEFAULT_SOURCE = PROJECT_ROOT / "iso_mkd/_tail_png"
DEFAULT_OUTPUT = PROJECT_ROOT / "iso_mkd/translated"

BOX_WIDTH = 144
BOX_HEIGHT = 28
FONT_SIZE = 23


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT)
    parser.add_argument("--font-size", type=int, default=FONT_SIZE)
    return parser.parse_args()


def text_mask(text: str, font: ImageFont.FreeTypeFont) -> tuple[Image.Image, float]:
    """Render one line and horizontally compress the complete mask if needed."""
    probe = Image.new("L", (1, 1), 0)
    draw = ImageDraw.Draw(probe)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width = max(1, right - left)
    height = max(1, bottom - top)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).text((-left, -top), text, font=font, fill=255)

    x_scale = min(1.0, BOX_WIDTH / width)
    if x_scale < 1.0:
        mask = mask.resize((BOX_WIDTH, height), Image.Resampling.LANCZOS)
    return mask, x_scale


def read_translations(csv_path: Path) -> list[tuple[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, skipinitialspace=True)
        if reader.fieldnames is None or not {"filename", "translation"}.issubset(reader.fieldnames):
            raise ValueError("CSV must contain filename and translation columns")

        rows: list[tuple[str, str]] = []
        seen: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            filename = (row.get("filename") or "").strip()
            translation = (row.get("translation") or "").strip()
            if not filename or not translation:
                raise ValueError(f"line {line_number}: filename and translation are required")
            if Path(filename).name != filename or not filename.lower().endswith(".png"):
                raise ValueError(f"line {line_number}: invalid PNG filename: {filename!r}")
            if filename in seen:
                raise ValueError(f"line {line_number}: duplicate filename: {filename}")
            seen.add(filename)
            rows.append((filename, translation))
    return rows


def render_one(source_path: Path, output_path: Path, text: str, font: ImageFont.FreeTypeFont) -> float:
    with Image.open(source_path) as opened:
        opened.load()
        if opened.size != (144, 80):
            raise ValueError(f"expected a 144x80 PNG, got {opened.size}: {source_path}")
        image = opened.convert("RGB")

    # Pillow rectangles include their final coordinate. Using a box keeps the
    # requested (0, 0)~(144, 28) region half-open: x=0..143, y=0..27.
    image.paste((0, 0, 0), (0, 0, BOX_WIDTH, BOX_HEIGHT))
    mask, x_scale = text_mask(text, font)
    x = (BOX_WIDTH - mask.width) // 2
    y = (BOX_HEIGHT - mask.height) // 2
    image.paste((255, 255, 255), (x, y), mask)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return x_scale


def main() -> int:
    args = parse_args()
    try:
        if args.font_size <= 0:
            raise ValueError("--font-size must be positive")
        rows = read_translations(args.csv)
        font = ImageFont.truetype(str(args.font), size=args.font_size)
        missing = [name for name, _text in rows if not (args.source / name).is_file()]
        if missing:
            raise FileNotFoundError("source PNG not found: " + ", ".join(missing))

        for filename, translation in rows:
            scale = render_one(args.source / filename, args.out / filename, translation, font)
            suffix = f" (x-scale={scale:.3f})" if scale < 1.0 else ""
            print(f"{filename}: {translation}{suffix}")
        print(f"Rendered {len(rows)} PNGs into {args.out}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
