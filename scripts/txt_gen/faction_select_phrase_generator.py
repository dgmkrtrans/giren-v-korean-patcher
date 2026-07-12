#!/usr/bin/env python3
"""Erase Japanese text from faction-select phrase textures.

This is the first stage for the ``세력선택 문구`` renderer.  It detects the
white Japanese title near the top guide row, expands the mask enough to cover
anti-aliasing and the dark drop shadow, then fills the area from surrounding
background pixels.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from PIL import Image

from text_renderer import merge_manifest_records, merged_fieldnames, read_rows


Color = tuple[int, int, int, int]
Point = tuple[int, int]
Box = tuple[int, int, int, int]

TARGET_VERIFIED_GROUP = "세력선택 문구"
DEFAULT_TEST_ORDINAL = 6717
DEFAULT_SEARCH_RECT = (0, 43, 480, 87)
PARTIAL_SHARED_BACKGROUND_GROUP = frozenset((6718, 6726))
SHARED_BACKGROUND_GROUPS = (
    (6718, 6726),
    (6719, 6720, 6725, 6727, 6730),
    (6721, 6722),
    (6723, 6724),
)
SHARED_BACKGROUND_BY_ORDINAL = {
    ordinal: group
    for group in SHARED_BACKGROUND_GROUPS
    for ordinal in group
}
NEIGHBORS_8 = (
    (-1, -1),
    (0, -1),
    (1, -1),
    (-1, 0),
    (1, 0),
    (-1, 1),
    (0, 1),
    (1, 1),
)


@dataclass
class EraseResult:
    status: str
    output: str
    target: Path
    width: int = 0
    height: int = 0
    title_white_pixels: int = 0
    erase_pixels: int = 0
    changed_pixels: int = 0
    mask_bbox: str = ""
    message: str = ""


@dataclass(frozen=True)
class EraseOptions:
    search_rect: Box = DEFAULT_SEARCH_RECT
    seed_box: Box | None = None
    detect_y_pad: int = 0
    white_min: int = 170
    white_luma: int = 190
    white_chroma: int = 70
    min_component_pixels: int = 3
    min_title_component_pixels: int = 12
    min_title_component_height: int = 8
    title_cluster_slop_y: int = 14
    shadow_radius: int = 4
    inpaint_method: str = "telea"
    inpaint_radius: float = 3.0
    palette_snap_sample_margin: int = 8


@dataclass(frozen=True)
class ErasedImage:
    image: Image.Image
    title_white_pixels: int
    erase_pixels: int
    changed_pixels: int
    mask_bbox: Box | None
    shared_background_pixels: int = 0
    erase_mask: frozenset[Point] = field(default_factory=frozenset, repr=False, compare=False)


def parse_box(value: str) -> Box:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("box must be x0,y0,x1,y1")
    try:
        x0, y0, x1, y1 = (int(part, 0) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if x1 <= x0 or y1 <= y0:
        raise argparse.ArgumentTypeError("box right/bottom must be greater than left/top")
    return x0, y0, x1, y1


def parse_rect(value: str) -> Box:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("rect must be x,y,width,height")
    try:
        x, y, width, height = (int(part, 0) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("rect width/height must be positive")
    return x, y, x + width, y + height


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Erase Japanese text from faction-select phrase PNGs."
    )
    parser.add_argument("--csv", default="textures_static/manifest.csv")
    parser.add_argument("--textures-root", default="textures_static")
    parser.add_argument("--out-root", default="textures_translated")
    parser.add_argument("--output-column", default="output")
    parser.add_argument("--target-verified-group", default=TARGET_VERIFIED_GROUP)
    parser.add_argument("--only", help="Render outputs containing this value.")
    parser.add_argument(
        "--only-ordinal",
        type=int,
        default=DEFAULT_TEST_ORDINAL,
        help="Only process this manifest ordinal. Defaults to the 6717 test image.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all rows in the target group instead of the 6717 test image.",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Overwrite PNGs under --textures-root.")
    parser.add_argument("--no-copy-manifest", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--debug-mask", help="Write the erase mask for the first rendered row.")
    parser.add_argument(
        "--search-rect",
        type=parse_rect,
        default=DEFAULT_SEARCH_RECT,
        help="Japanese text search rectangle as x,y,width,height.",
    )
    parser.add_argument(
        "--seed-box",
        type=parse_box,
        help="Deprecated x0,y0,x1,y1 alias. Prefer --search-rect.",
    )
    parser.add_argument(
        "--detect-y-pad",
        type=int,
        default=0,
        help="Extra vertical search padding around --search-rect.",
    )
    parser.add_argument("--white-min", type=int, default=170)
    parser.add_argument("--white-luma", type=int, default=190)
    parser.add_argument("--white-chroma", type=int, default=70)
    parser.add_argument("--min-component-pixels", type=int, default=3)
    parser.add_argument("--min-title-component-pixels", type=int, default=12)
    parser.add_argument("--min-title-component-height", type=int, default=8)
    parser.add_argument("--title-cluster-slop-y", type=int, default=14)
    parser.add_argument("--shadow-radius", type=int, default=4)
    parser.add_argument(
        "--inpaint-method",
        choices=("telea", "directional"),
        default="telea",
        help="telea uses OpenCV inpainting; directional uses the local 8-neighbor filler.",
    )
    parser.add_argument("--inpaint-radius", type=float, default=3.0)
    parser.add_argument("--palette-snap-sample-margin", type=int, default=8)
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


def clamp_box(box: Box, width: int, height: int) -> Box:
    x0, y0, x1, y1 = box
    return max(0, x0), max(0, y0), min(width, x1), min(height, y1)


def expand_box(box: Box, amount: int, width: int, height: int) -> Box:
    x0, y0, x1, y1 = box
    return clamp_box((x0 - amount, y0 - amount, x1 + amount, y1 + amount), width, height)


def point_bbox(points: Iterable[Point]) -> Box | None:
    xs: list[int] = []
    ys: list[int] = []
    for x, y in points:
        xs.append(x)
        ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def box_to_string(box: Box | None) -> str:
    if box is None:
        return ""
    return ",".join(str(value) for value in box)


def luma(color: Color) -> float:
    red, green, blue, _alpha = color
    return red * 0.299 + green * 0.587 + blue * 0.114


def is_white_text_candidate(color: Color, args: argparse.Namespace) -> bool:
    red, green, blue, alpha = color
    if alpha == 0:
        return False
    return (
        min(red, green, blue) >= args.white_min
        and luma(color) >= args.white_luma
        and max(red, green, blue) - min(red, green, blue) <= args.white_chroma
    )


def components(points: set[Point]) -> list[set[Point]]:
    remaining = set(points)
    found: list[set[Point]] = []
    while remaining:
        start = remaining.pop()
        comp = {start}
        queue: deque[Point] = deque([start])
        while queue:
            x, y = queue.popleft()
            for dx, dy in NEIGHBORS_8:
                neighbor = (x + dx, y + dy)
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    comp.add(neighbor)
                    queue.append(neighbor)
        found.append(comp)
    return found


def detect_white_title_mask(image: Image.Image, args: argparse.Namespace) -> set[Point]:
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    base_box = args.seed_box if args.seed_box else args.search_rect
    area_x0, area_y0, area_x1, area_y1 = clamp_box(base_box, width, height)
    detect_box = clamp_box(
        (
            area_x0,
            area_y0 - args.detect_y_pad,
            area_x1,
            area_y1 + args.detect_y_pad,
        ),
        width,
        height,
    )
    x0, y0, x1, y1 = detect_box
    bright: set[Point] = set()
    for y in range(y0, y1):
        for x in range(x0, x1):
            if is_white_text_candidate(pixels[x, y], args):
                bright.add((x, y))

    bright_components = components(bright)
    strong: list[set[Point]] = []
    weak: list[set[Point]] = []
    for comp in bright_components:
        if len(comp) < args.min_component_pixels:
            continue
        bbox = point_bbox(comp)
        if bbox is None:
            continue
        _left, top, _right, bottom = bbox
        comp_height = bottom - top
        if (
            len(comp) >= args.min_title_component_pixels
            and comp_height >= args.min_title_component_height
        ):
            strong.append(comp)
        else:
            weak.append(comp)

    if not strong:
        return set()

    clustered: list[list[set[Point]]] = []
    for comp in sorted(
        strong,
        key=lambda item: sum(y for _x, y in item) / max(1, len(item)),
    ):
        center_y = sum(y for _x, y in comp) / len(comp)
        for cluster in clustered:
            cluster_points = [point for item in cluster for point in item]
            cluster_center_y = sum(y for _x, y in cluster_points) / len(cluster_points)
            if abs(center_y - cluster_center_y) <= args.title_cluster_slop_y:
                cluster.append(comp)
                break
        else:
            clustered.append([comp])

    title_cluster = max(
        clustered,
        key=lambda cluster: (
            sum(len(comp) for comp in cluster),
            -min(y for comp in cluster for _x, y in comp),
        ),
    )
    first_pass = set().union(*title_cluster)
    first_bbox = point_bbox(first_pass)
    if first_bbox is None:
        return first_pass

    bx0, by0, bx1, by1 = expand_box(first_bbox, 8, width, height)
    for comp in weak + [comp for comp in strong if comp not in title_cluster]:
        if len(comp) < args.min_component_pixels:
            continue
        bbox = point_bbox(comp)
        if bbox is None:
            continue
        left, top, right, bottom = bbox
        overlaps_x = right > bx0 and left < bx1
        near_vertical = bottom > by0 and top < by1 + 3
        if overlaps_x and near_vertical:
            first_pass.update(comp)
    return first_pass


def dilate(points: set[Point], radius: int, width: int, height: int) -> set[Point]:
    if radius <= 0 or not points:
        return set(points)
    out: set[Point] = set()
    offsets: list[Point] = []
    radius_sq = radius * radius
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy <= radius_sq:
                offsets.append((dx, dy))
    for x, y in points:
        for dx, dy in offsets:
            nx = x + dx
            ny = y + dy
            if 0 <= nx < width and 0 <= ny < height:
                out.add((nx, ny))
    return out


def unique_nontext_colors(
    image: Image.Image,
    mask: set[Point],
    sample_box: Box,
    args: argparse.Namespace,
) -> list[Color]:
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    x0, y0, x1, y1 = clamp_box(sample_box, rgba.width, rgba.height)
    colors: list[Color] = []
    seen: set[Color] = set()
    for y in range(y0, y1):
        for x in range(x0, x1):
            if (x, y) in mask:
                continue
            color = pixels[x, y]
            if color[3] == 0 or is_white_text_candidate(color, args):
                continue
            if color not in seen:
                seen.add(color)
                colors.append(color)
    return colors


def nearest_color(color: Color, palette: list[Color]) -> Color:
    if not palette:
        return color
    red, green, blue, alpha = color
    return min(
        palette,
        key=lambda candidate: (
            (candidate[0] - red) * (candidate[0] - red)
            + (candidate[1] - green) * (candidate[1] - green)
            + (candidate[2] - blue) * (candidate[2] - blue)
            + ((candidate[3] - alpha) * (candidate[3] - alpha)) // 4
        ),
    )


def averaged_color(samples: list[Color]) -> Color:
    count = len(samples)
    return (
        round(sum(color[0] for color in samples) / count),
        round(sum(color[1] for color in samples) / count),
        round(sum(color[2] for color in samples) / count),
        round(sum(color[3] for color in samples) / count),
    )


def fill_mask_from_neighbors(image: Image.Image, mask: set[Point], allowed_colors: list[Color]) -> Image.Image:
    out = image.convert("RGBA").copy()
    pixels = out.load()
    width, height = out.size
    remaining = set(mask)
    while remaining:
        updates: dict[Point, Color] = {}
        for x, y in tuple(remaining):
            samples: list[Color] = []
            for dx, dy in NEIGHBORS_8:
                nx = x + dx
                ny = y + dy
                if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in remaining:
                    samples.append(pixels[nx, ny])
            if samples:
                updates[(x, y)] = nearest_color(averaged_color(samples), allowed_colors)

        if not updates:
            # Extremely unlikely for this tight title mask, but keep the script
            # total: fall back to the closest unmasked ray sample.
            for x, y in tuple(remaining):
                samples = []
                for dx, dy in NEIGHBORS_8:
                    for distance in range(1, max(width, height)):
                        nx = x + dx * distance
                        ny = y + dy * distance
                        if not (0 <= nx < width and 0 <= ny < height):
                            break
                        if (nx, ny) not in remaining:
                            samples.append(pixels[nx, ny])
                            break
                if samples:
                    updates[(x, y)] = nearest_color(averaged_color(samples), allowed_colors)
            if not updates:
                break

        for point, color in updates.items():
            pixels[point] = color
            remaining.remove(point)
    return out


def fill_mask_with_telea(image: Image.Image, mask: set[Point], radius: float) -> Image.Image:
    try:
        import cv2
        import numpy as np
    except Exception as exc:  # pragma: no cover - depends on local optional package state.
        raise RuntimeError("OpenCV/numpy are required for --inpaint-method telea") from exc

    source = image.convert("RGBA")
    rgb = np.array(source.convert("RGB"))
    mask_image = Image.new("L", source.size, 0)
    mask_pixels = mask_image.load()
    for point in mask:
        mask_pixels[point] = 255
    inpainted_rgb = cv2.inpaint(rgb, np.array(mask_image), radius, cv2.INPAINT_TELEA)
    alpha = np.array(source.getchannel("A"))
    out = np.dstack([inpainted_rgb, alpha])
    return Image.fromarray(out)


def count_changed(before: Image.Image, after: Image.Image) -> int:
    left = before.convert("RGBA").load()
    right = after.convert("RGBA").load()
    width, height = before.size
    changed = 0
    for y in range(height):
        for x in range(width):
            if left[x, y] != right[x, y]:
                changed += 1
    return changed


def write_mask(path: Path, size: tuple[int, int], mask: set[Point]) -> None:
    out = Image.new("L", size, 0)
    pixels = out.load()
    for point in mask:
        pixels[point] = 255
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(path)


def shared_background_group_for_ordinal(ordinal: int | None) -> tuple[int, ...] | None:
    if ordinal is None:
        return None
    return SHARED_BACKGROUND_BY_ORDINAL.get(ordinal)


def shared_background_allows_point(group: tuple[int, ...], x: int, width: int) -> bool:
    if frozenset(group) == PARTIAL_SHARED_BACKGROUND_GROUP:
        return x >= width // 2
    return True


def median_color(samples: list[Color]) -> Color:
    if not samples:
        return (0, 0, 0, 0)
    mid = len(samples) // 2
    return tuple(sorted(color[channel] for color in samples)[mid] for channel in range(4))  # type: ignore[return-value]


def erase_japanese_text(image: Image.Image, options: EraseOptions | None = None) -> ErasedImage:
    options = options or EraseOptions()
    source = image.convert("RGBA")
    white_mask = detect_white_title_mask(source, options)
    erase_mask = dilate(white_mask, options.shadow_radius, source.width, source.height)
    mask_bbox = point_bbox(erase_mask)
    if not erase_mask or mask_bbox is None:
        raise ValueError("no white Japanese title pixels detected")

    sample_box = expand_box(
        mask_bbox,
        options.palette_snap_sample_margin,
        source.width,
        source.height,
    )
    if options.inpaint_method == "telea":
        erased = fill_mask_with_telea(source, erase_mask, options.inpaint_radius)
    elif options.inpaint_method == "directional":
        allowed_colors = unique_nontext_colors(source, erase_mask, sample_box, options)
        if not allowed_colors:
            allowed_colors = unique_nontext_colors(
                source,
                erase_mask,
                (0, 0, source.width, source.height),
                options,
            )
        erased = fill_mask_from_neighbors(source, erase_mask, allowed_colors)
    else:
        raise ValueError(f"unknown inpaint method: {options.inpaint_method}")
    return ErasedImage(
        image=erased,
        title_white_pixels=len(white_mask),
        erase_pixels=len(erase_mask),
        changed_pixels=count_changed(source, erased),
        mask_bbox=mask_bbox,
        erase_mask=frozenset(erase_mask),
    )


def erase_japanese_text_with_shared_background(
    image: Image.Image,
    ordinal: int | None,
    donor_images: Iterable[Image.Image],
    options: EraseOptions | None = None,
) -> ErasedImage:
    options = options or EraseOptions()
    erased = erase_japanese_text(image, options)
    group = shared_background_group_for_ordinal(ordinal)
    if not group:
        return erased

    source = image.convert("RGBA")
    donor_records: list[tuple[Image.Image, set[Point]]] = []
    for donor_image in donor_images:
        donor = donor_image.convert("RGBA")
        if donor.size != source.size:
            continue
        donor_white_mask = detect_white_title_mask(donor, options)
        donor_mask = dilate(donor_white_mask, options.shadow_radius, donor.width, donor.height)
        donor_records.append((donor, donor_mask))
    if not donor_records:
        return erased

    out = erased.image.convert("RGBA").copy()
    out_pixels = out.load()
    donor_pixels = [(donor.load(), mask) for donor, mask in donor_records]
    replaced = 0
    for x, y in erased.erase_mask:
        if not shared_background_allows_point(group, x, source.width):
            continue
        samples: list[Color] = []
        for pixels, donor_mask in donor_pixels:
            if (x, y) in donor_mask:
                continue
            color = pixels[x, y]
            if color[3] == 0 or is_white_text_candidate(color, options):
                continue
            samples.append(color)
        if not samples:
            continue
        out_pixels[x, y] = median_color(samples)
        replaced += 1

    if replaced == 0:
        return erased
    return ErasedImage(
        image=out,
        title_white_pixels=erased.title_white_pixels,
        erase_pixels=erased.erase_pixels,
        changed_pixels=count_changed(source, out),
        mask_bbox=erased.mask_bbox,
        shared_background_pixels=replaced,
        erase_mask=erased.erase_mask,
    )


def erase_one(source_path: Path, target_path: Path, args: argparse.Namespace) -> EraseResult:
    source = Image.open(source_path).convert("RGBA")
    try:
        erased_result = erase_japanese_text(source, args)
    except ValueError as exc:
        return EraseResult(
            status="error",
            output="",
            target=target_path,
            width=source.width,
            height=source.height,
            message=str(exc),
        )
    if not args.dry_run:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        erased_result.image.save(target_path)
    if args.debug_mask:
        write_mask(
            Path(args.debug_mask),
            source.size,
            dilate(
                detect_white_title_mask(source, args),
                args.shadow_radius,
                source.width,
                source.height,
            ),
        )
    return EraseResult(
        status="ok",
        output="",
        target=target_path,
        width=source.width,
        height=source.height,
        title_white_pixels=erased_result.title_white_pixels,
        erase_pixels=erased_result.erase_pixels,
        changed_pixels=erased_result.changed_pixels,
        mask_bbox=box_to_string(erased_result.mask_bbox),
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


def write_report(path: Path, results: list[EraseResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "status",
        "output",
        "target",
        "width",
        "height",
        "title_white_pixels",
        "erase_pixels",
        "changed_pixels",
        "mask_bbox",
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
                    "title_white_pixels": result.title_white_pixels,
                    "erase_pixels": result.erase_pixels,
                    "changed_pixels": result.changed_pixels,
                    "mask_bbox": result.mask_bbox,
                    "message": result.message,
                }
            )


def main() -> int:
    args = parse_args()
    if args.detect_y_pad < 0 or args.title_cluster_slop_y < 0 or args.shadow_radius < 0:
        print("Error: y padding, cluster slop, and shadow radius must be non-negative")
        return 2
    if args.limit is not None and args.limit <= 0:
        print("Error: --limit must be positive")
        return 2

    rows = read_rows(Path(args.csv))
    if rows and args.output_column not in rows[0]:
        print(f"Error: missing CSV column: {args.output_column}")
        return 2

    results: list[EraseResult] = []
    rendered: set[str] = set()
    failures = 0
    skipped_not_target = 0

    only_ordinal = None if args.all else args.only_ordinal
    for row in rows:
        output = (row.get(args.output_column) or "").strip().replace("\\", "/")
        if not output or not matches_only(output, args.only):
            continue
        if (row.get("verified_group") or "").strip() != args.target_verified_group:
            skipped_not_target += 1
            continue
        if only_ordinal is not None:
            try:
                ordinal = int(row.get("ordinal") or "-1", 0)
            except ValueError:
                ordinal = -1
            if ordinal != only_ordinal:
                continue

        source_path = Path(args.textures_root) / output
        target_path = target_for(output, args)
        if not source_path.exists():
            failures += 1
            result = EraseResult(
                status="error",
                output=output,
                target=target_path,
                message=f"source image not found: {source_path}",
            )
            results.append(result)
            if args.strict:
                break
            continue

        try:
            result = erase_one(source_path, target_path, args)
            result.output = output
        except Exception as exc:
            failures += 1
            result = EraseResult(status="error", output=output, target=target_path, message=str(exc))
            print(f"[error] {output}: {exc}")
            if args.strict:
                results.append(result)
                break

        results.append(result)
        if result.status == "ok":
            rendered.add(output)
            if args.verbose:
                print(
                    f"[ok] {output} mask={result.erase_pixels} "
                    f"changed={result.changed_pixels} bbox={result.mask_bbox}"
                )
            if args.limit and len(rendered) >= args.limit:
                break
        elif result.status == "error":
            failures += 1
            print(f"[error] {output}: {result.message}")
            if args.strict:
                break

    if args.report:
        write_report(Path(args.report), results)
        print(f"Report: {args.report}")

    if not args.dry_run and not args.apply and not args.no_copy_manifest:
        for path in write_filtered_manifests(
            Path(args.textures_root),
            Path(args.out_root),
            rows,
            args.output_column,
            rendered,
        ):
            print(f"Wrote {path}")

    errors = sum(1 for result in results if result.status == "error")
    print(
        f"Done. erased={len(rendered)} skipped_not_target={skipped_not_target} "
        f"errors={errors}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
