#!/usr/bin/env python3
"""Render Korean faction opening title textures from the translation CSV."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

from text_renderer import (
    PROJECT_ROOT,
    RowRangeFilter,
    merge_manifest_records,
    merged_fieldnames,
    read_rows,
    resolve_font,
)
from ui_text_fit_renderer import (
    color_to_first_index,
    inspect_source_png,
    luma,
    palette_distance,
    palette_from_manifest_record,
    palette_indexed_png_bytes,
    snap_rgba_to_palette,
)

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import dump_static_textures as texture_dump  # noqa: E402


Color = tuple[int, int, int, int]
TARGET_VERIFIED_GROUP = "각 세력 오프닝타이틀"


@dataclass
class RenderResult:
    status: str
    output: str
    target: Path
    width: int = 0
    height: int = 0
    source_png_bytes: int = 0
    generated_png_bytes: int = 0
    changed_pixels: int = 0
    line_count: int = 0
    message: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render Korean PNGs for faction opening title textures."
    )
    parser.add_argument("--csv", default="textures_static/manifest.csv")
    parser.add_argument("--textures-root", default="textures_static")
    parser.add_argument("--out-root", default="textures_translated")
    parser.add_argument("--unpacked-root", default="unpacked_mkd")
    parser.add_argument("--output-column", default="output")
    parser.add_argument("--text-column", default="korean")
    parser.add_argument("--target-verified-group", default=TARGET_VERIFIED_GROUP)
    parser.add_argument("--only", help="Render outputs containing this value.")
    parser.add_argument("--rows", "--row-range", dest="rows")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Overwrite PNGs under --textures-root.")
    parser.add_argument("--no-copy-manifest", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--font", default="assets/fonts/NanumMyeongjoExtraBold.ttf")
    parser.add_argument("--font-index", type=int, default=0)
    parser.add_argument("--font-size", type=int, default=80)
    parser.add_argument("--max-width", type=int, default=430)
    parser.add_argument("--y-offset", type=int, default=0)
    return parser.parse_args()


def target_for(output: str, args: argparse.Namespace) -> Path:
    if args.apply:
        return Path(args.textures_root) / output
    return Path(args.out_root) / output


def matches_only(output: str, only: str | None) -> bool:
    if not only:
        return True
    needle = only.replace("\\", "/")
    return output == needle or output.endswith(needle) or needle in output


def normalize_lines(value: str) -> list[str]:
    text = unicodedata.normalize("NFC", value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


def title_parts(value: str) -> tuple[str, str, str, list[str]]:
    lines = normalize_lines(value)
    messages: list[str] = []
    if not lines:
        return "", "", "", messages
    if len(lines) == 1:
        return "", lines[0], "", messages
    if len(lines) == 2:
        return "", lines[0], lines[1], messages
    if len(lines) == 3:
        return lines[0], lines[1], lines[2], messages
    messages.append(f"extra lines merged into bottom text: {len(lines)}")
    return lines[0], lines[1], " ".join(lines[2:]), messages


def nearest_palette_color(color: Color, palette: Iterable[Color]) -> Color:
    opaque = [candidate for candidate in palette if candidate[3] > 0]
    candidates = opaque or list(palette)
    if not candidates:
        return color
    return min(candidates, key=lambda candidate: palette_distance(color, candidate))


def sorted_text_palette(palette: Iterable[Color], background: Color) -> list[Color]:
    colors = [
        color
        for color in dict.fromkeys(palette)
        if color[3] > 0 and color != background
    ]
    if not colors:
        return [(255, 255, 255, 255)]
    return sorted(colors, key=lambda color: (luma(color), color))


def palette_ramp(palette: Iterable[Color], background: Color) -> list[Color]:
    colors = sorted_text_palette(palette, background)
    if len(colors) <= 1:
        return colors
    dark = colors[max(0, round((len(colors) - 1) * 0.12))]
    mid = colors[max(0, round((len(colors) - 1) * 0.56))]
    light = colors[-1]
    highlight = colors[max(0, round((len(colors) - 1) * 0.86))]
    return [dark, mid, highlight, light]


def _vertical_gradient(size: tuple[int, int], stops: list[tuple[float, Color]]) -> Image.Image:
    w, h = size
    stops = sorted(stops, key=lambda stop: stop[0])
    grad = Image.new("RGBA", (1, h))
    px = grad.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        for index in range(len(stops) - 1):
            t0, c0 = stops[index]
            t1, c1 = stops[index + 1]
            if t0 <= t <= t1:
                factor = (t - t0) / max(t1 - t0, 1e-6)
                px[0, y] = tuple(
                    int(c0[channel] + (c1[channel] - c0[channel]) * factor)
                    for channel in range(4)
                )
                break
        else:
            px[0, y] = stops[-1][1]
    return grad.resize((w, h))


def trim_alpha(image: Image.Image, margin: int = 2) -> Image.Image:
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    left, top, right, bottom = bbox
    left = max(0, left - margin)
    top = max(0, top - margin)
    right = min(image.width, right + margin)
    bottom = min(image.height, bottom + margin)
    return image.crop((left, top, right, bottom))


def render_chrome_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    palette: list[Color],
    background: Color,
    depth: int,
    padding: int = 8,
) -> Image.Image:
    probe = ImageDraw.Draw(Image.new("L", (4, 4)))
    bbox = probe.textbbox((0, 0), text, font=font)
    tw = max(1, bbox[2] - bbox[0])
    th = max(1, bbox[3] - bbox[1])

    ext = depth
    w = tw + padding * 2 + ext
    h = th + padding * 2 + ext
    ox = padding - bbox[0]
    oy = padding - bbox[1]

    ramp = palette_ramp(palette, background)
    dark = ramp[0]
    mid = ramp[min(1, len(ramp) - 1)]
    high = ramp[min(2, len(ramp) - 1)]
    light = ramp[-1]

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for step in range(depth, 0, -1):
        shade_target = int(48 + (110 - 48) * ((depth - step) / max(depth, 1)))
        shade = nearest_palette_color((shade_target, shade_target, shade_target, 255), palette)
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text(
            (ox + step, oy + step),
            text,
            font=font,
            fill=shade,
        )
        out.alpha_composite(layer)

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((ox, oy), text, font=font, fill=255)
    grad = _vertical_gradient(
        (w, h),
        [
            (0.00, light),
            (0.24, high),
            (0.54, mid),
            (1.00, dark),
        ],
    )
    face = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    face.paste(grad, (0, 0), mask)
    out.alpha_composite(face)
    return trim_alpha(out, margin=2)


def fit_line(image: Image.Image, max_width: int) -> tuple[Image.Image, float]:
    if image.width <= max_width:
        return image, 1.0
    scale = max_width / max(1, image.width)
    new_size = (max(1, max_width), max(1, round(image.height * scale)))
    return image.resize(new_size, Image.Resampling.LANCZOS), scale


def pixel_is_empty_color(color: Color) -> bool:
    red, green, blue, alpha = color
    return alpha == 0 or max(red, green, blue) <= 4


def active_tile_count(image: Image.Image, tile_width: int, tile_height: int) -> int:
    pixels = image.convert("RGBA").load()
    count = 0
    for tile_top in range(0, image.height, tile_height):
        for tile_left in range(0, image.width, tile_width):
            active = False
            for y in range(tile_top, min(image.height, tile_top + tile_height)):
                for x in range(tile_left, min(image.width, tile_left + tile_width)):
                    if not pixel_is_empty_color(pixels[x, y]):
                        active = True
                        break
                if active:
                    break
            if active:
                count += 1
    return count


def tile_budget(row: dict[str, str], unpacked_root: Path) -> tuple[int, int, int] | None:
    layout = (row.get("layout") or "").strip()
    magic = {
        "tilemap_mp16": texture_dump.MP16_MAGIC,
        "tilemap_mp20": texture_dump.MP20_MAGIC,
    }.get(layout)
    if magic is None:
        return None
    try:
        offset = int(row.get("layout_offset") or "0", 0)
        storage_width = int(row.get("storage_width") or "0", 0)
        storage_height = int(row.get("storage_height") or "0", 0)
    except ValueError:
        return None
    source_path = unpacked_root / (row.get("source") or "")
    if not source_path.exists():
        return None
    tilemap = texture_dump.valid_tilemap_at(source_path.read_bytes(), offset, magic)
    if tilemap is None:
        return None
    atlas_width = tilemap.atlas_width_tiles or storage_width // tilemap.tile_width
    atlas_height = tilemap.atlas_height_tiles or storage_height // tilemap.tile_height
    if atlas_width <= 0 or atlas_height <= 0:
        return None
    return tilemap.tile_width, tilemap.tile_height, max(1, atlas_width * atlas_height - 1)


def fit_to_tile_budget(
    image: Image.Image,
    background: Color,
    budget: tuple[int, int, int] | None,
) -> tuple[Image.Image, str]:
    if budget is None:
        return image, ""
    tile_width, tile_height, max_tiles = budget
    current_tiles = active_tile_count(image, tile_width, tile_height)
    if current_tiles <= max_tiles:
        return image, ""

    content_mask = Image.new("L", image.size, 0)
    mask_pixels = content_mask.load()
    image_pixels = image.convert("RGBA").load()
    for y in range(image.height):
        for x in range(image.width):
            if not pixel_is_empty_color(image_pixels[x, y]):
                mask_pixels[x, y] = 255
    bbox = content_mask.getbbox()
    if bbox is None:
        return image, ""

    left, top, right, bottom = bbox
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    content = image.crop(bbox)
    scale = min(0.98, (max_tiles / max(1, current_tiles)) ** 0.5)
    for _attempt in range(12):
        new_width = max(1, round(content.width * scale))
        new_height = max(1, round(content.height * scale))
        resized = content.resize((new_width, new_height), Image.Resampling.LANCZOS)
        candidate = Image.new("RGBA", image.size, background)
        paste_x = round(center_x - new_width / 2)
        paste_y = round(center_y - new_height / 2)
        candidate.alpha_composite(resized, (paste_x, paste_y))
        candidate_tiles = active_tile_count(candidate, tile_width, tile_height)
        if candidate_tiles <= max_tiles:
            return candidate, f"tile budget scaled x/y={scale:.3f} ({current_tiles}->{candidate_tiles}/{max_tiles})"
        scale *= 0.96
    return image, f"tile budget still over {current_tiles}/{max_tiles}"


def paste_centered(canvas: Image.Image, image: Image.Image, y: int) -> None:
    x = (canvas.width - image.width) // 2
    canvas.alpha_composite(image, (x, y))


def render_title(
    text: str,
    font_path: Path,
    palette: list[Color],
    background: Color,
    font_size: int = 80,
    canvas_size: tuple[int, int] = (480, 272),
    y_offset: int = 0,
    top_text: str = "",
    bottom_text: str = "",
    font_index: int = 0,
    max_width: int = 430,
) -> tuple[Image.Image, list[str]]:
    main_font = ImageFont.truetype(str(font_path), font_size, index=font_index)
    top_font = ImageFont.truetype(str(font_path), max(1, int(font_size * 0.42)), index=font_index)
    bottom_font = ImageFont.truetype(str(font_path), max(1, int(font_size * 0.55)), index=font_index)

    messages: list[str] = []
    parts: list[tuple[str, Image.Image]] = []
    if top_text:
        parts.append(("top", render_chrome_text(top_text, top_font, palette, background, depth=4)))
    parts.append(("main", render_chrome_text(text, main_font, palette, background, depth=7)))
    if bottom_text:
        parts.append(("bottom", render_chrome_text(bottom_text, bottom_font, palette, background, depth=5)))

    fitted: list[tuple[str, Image.Image]] = []
    for role, image in parts:
        max_line_width = max_width if role == "main" else min(canvas_size[0] - 32, max_width)
        resized, scale = fit_line(image, max_line_width)
        if scale < 1.0:
            messages.append(f"{role} scaled x/y={scale:.3f}")
        fitted.append((role, resized))

    gap = max(-2, round(font_size * -0.025))
    total_h = sum(image.height for _role, image in fitted) + gap * max(0, len(fitted) - 1)
    y = (canvas_size[1] - total_h) // 2 + y_offset

    canvas = Image.new("RGBA", canvas_size, background)
    for _role, image in fitted:
        paste_centered(canvas, image, y)
        y += image.height + gap
    return canvas, messages


def save_palette_png(image: Image.Image, palette: list[Color], color_to_index: dict[Color, int], path: Path) -> int:
    data = palette_indexed_png_bytes(image, palette, color_to_index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def render_one(row: dict[str, str], args: argparse.Namespace, font_path: Path) -> RenderResult:
    output = (row.get(args.output_column) or "").strip().replace("\\", "/")
    source_path = Path(args.textures_root) / output
    target_path = target_for(output, args)
    source_bytes = source_path.stat().st_size
    manifest_palette = palette_from_manifest_record(row, Path(args.unpacked_root))
    source = inspect_source_png(
        source_path,
        background_source="top_left",
        manifest_palette=None,
    )
    if manifest_palette:
        palette = list(manifest_palette)
        color_to_index = color_to_first_index(palette)
        background = palette[0]
    else:
        palette = source.colors
        color_to_index = source.color_to_index
        background = source.background
    top_text, text, bottom_text, messages = title_parts(row.get(args.text_column) or "")
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
    image, render_messages = render_title(
        text=text,
        font_path=font_path,
        palette=palette,
        background=background,
        font_size=args.font_size,
        canvas_size=(source.width, source.height),
        y_offset=args.y_offset,
        top_text=top_text,
        bottom_text=bottom_text,
        font_index=args.font_index,
        max_width=min(args.max_width, max(1, source.width - 32)),
    )
    messages.extend(render_messages)
    image, budget_message = fit_to_tile_budget(
        image,
        background,
        tile_budget(row, Path(args.unpacked_root)),
    )
    if budget_message:
        if budget_message.startswith("tile budget still over"):
            raise ValueError(budget_message)
        messages.append(budget_message)
    image, changed_pixels = snap_rgba_to_palette(image, palette)

    if args.dry_run:
        generated_bytes = len(palette_indexed_png_bytes(image, palette, color_to_index))
    else:
        generated_bytes = save_palette_png(image, palette, color_to_index, target_path)

    line_count = len([part for part in (top_text, text, bottom_text) if part])
    return RenderResult(
        status="warning" if messages else "ok",
        output=output,
        target=target_path,
        width=source.width,
        height=source.height,
        source_png_bytes=source_bytes,
        generated_png_bytes=generated_bytes,
        changed_pixels=changed_pixels,
        line_count=line_count,
        message="; ".join(messages),
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

    incoming_rows = [
        dict(row)
        for row in rows
        if (row.get(output_column) or "").strip().replace("\\", "/") in rendered
    ]
    if incoming_rows:
        target = out_root / "manifest.csv"
        existing_rows: list[dict[str, str]] = []
        existing_fieldnames: list[str] = []
        if target.exists():
            with target.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                existing_fieldnames = list(reader.fieldnames or [])
                existing_rows = list(reader)
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
        "line_count",
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
                    "line_count": result.line_count,
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
    if args.font_size <= 0:
        print("Error: --font-size must be positive", file=sys.stderr)
        return 2
    if args.max_width <= 0:
        print("Error: --max-width must be positive", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit <= 0:
        print("Error: --limit must be positive", file=sys.stderr)
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
    font_label = font_choice.aliases[0] if font_choice else str(
        font_path.relative_to(PROJECT_ROOT) if font_path.is_relative_to(PROJECT_ROOT) else font_path
    )

    rows = read_rows(Path(args.csv))
    if rows and args.output_column not in rows[0]:
        print(f"Error: missing CSV column: {args.output_column}", file=sys.stderr)
        return 2
    if rows and args.text_column not in rows[0]:
        print(f"Error: missing CSV column: {args.text_column}", file=sys.stderr)
        return 2

    if args.verbose:
        print(f"Target verified_group: {args.target_verified_group}")
        print(f"Using title font: {font_label} ({font_path}) size={args.font_size}")
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
        if (row.get("verified_group") or "").strip() != args.target_verified_group:
            skipped_not_target += 1
            continue
        source_path = Path(args.textures_root) / output
        if not source_path.exists():
            failures += 1
            result = RenderResult(
                status="error",
                output=output,
                target=target_for(output, args),
                message=f"source image not found: {source_path}",
            )
            results.append(result)
            if args.strict:
                break
            continue

        render_text = "\n".join(normalize_lines(row.get(args.text_column) or ""))
        previous = seen_text.get(output)
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
                        message=message,
                    )
                )
                if args.strict:
                    break
            duplicate_rows += 1
            continue
        seen_text[output] = render_text

        try:
            result = render_one(row, args, font_path)
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
        if result.status in {"ok", "warning"}:
            rendered.add(output)
            if args.verbose:
                print(
                    f"[{result.status}] {output} {result.width}x{result.height} "
                    f"lines={result.line_count} {result.message}"
                )
            if args.limit and len(rendered) >= args.limit:
                break
        elif result.status == "skipped" and args.verbose:
            print(f"[skipped] {output}: {result.message}")

    if args.report:
        write_report(Path(args.report), results)
        print(f"Report: {args.report}")

    if not args.dry_run and not args.apply and not args.no_copy_manifest:
        written = write_filtered_manifests(
            Path(args.textures_root),
            Path(args.out_root),
            rows,
            args.output_column,
            rendered,
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
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
