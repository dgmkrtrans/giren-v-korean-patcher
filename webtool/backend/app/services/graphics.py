from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from PIL import Image

from ..core.config import (
    CSV_PAGE_SIZE,
    DEFAULT_KOREAN_TEXTURES_ROOT,
    DEFAULT_TEXTURES_ROOT,
    FILE_COLUMNS,
    KOREAN_COLUMNS,
    ROOT,
    ROLE_ADMIN,
)
from ..core.db import DB_BACKEND, DB_ERRORS, connect_db, db_lock, execute, report_db_error
from ..core.utils import clean_text, now_ts, resolve_project_path, root_relative
from .translation import (
    cell,
    csv_identity,
    csv_record_index,
    find_column,
    find_translation_csv,
    resolve_image_path,
    unique_texture_records,
)


MAX_UPLOAD_BYTES = 15 * 1024 * 1024
PENDING_UPLOAD_ROOT = "webtool_data/graphics_uploads/pending"
_rebuild_module: Any | None = None
_rebuild_module_lock = threading.Lock()
graphics_file_lock = threading.Lock()


def palette_key(color: tuple[int, int, int, int]) -> int:
    r, g, b, a = color
    return (r << 24) | (g << 16) | (b << 8) | a


def palette_distance(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    return sum((a[index] - b[index]) ** 2 for index in range(4))


def parse_row_ranges(value: str, total_rows: int) -> list[int]:
    text = clean_text(value)
    if not text:
        return []
    numbers: set[int] = set()
    tokens = [part for part in re.split(r"[\s,;]+", text.replace("~", "-")) if part]
    for token in tokens:
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"행 범위를 해석할 수 없습니다: {token}")
            start = int(start_text)
            end = int(end_text)
            if start > end:
                start, end = end, start
            for number in range(start, end + 1):
                if number < 1 or number > total_rows:
                    raise ValueError(f"CSV 행 번호가 범위를 벗어났습니다: {number}")
                numbers.add(number)
            continue
        if not token.isdigit():
            raise ValueError(f"CSV 행 번호를 해석할 수 없습니다: {token}")
        number = int(token)
        if number < 1 or number > total_rows:
            raise ValueError(f"CSV 행 번호가 범위를 벗어났습니다: {number}")
        numbers.add(number)
    return sorted(numbers)


def format_row_ranges(numbers: list[int] | set[int]) -> str:
    ordered = sorted({int(number) for number in numbers})
    if not ordered:
        return ""
    ranges: list[str] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(ranges)


def safe_rel_path(value: str) -> str:
    text = clean_text(value).replace("\\", "/")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ValueError("상대 PNG 경로만 사용할 수 있습니다.")
    return str(path)


def approved_output_rel(output_rel: str) -> str:
    return f"text/{Path(safe_rel_path(output_rel)).name}"


def output_path(root_value: str, rel_path: str) -> Path:
    root = resolve_project_path(root_value, DEFAULT_KOREAN_TEXTURES_ROOT)
    target = (root / safe_rel_path(rel_path)).resolve()
    target.relative_to(root.resolve())
    return target


def graphic_image_url(folder: str, file_path: str, *, csv_folder: str = "") -> str:
    params = {"folder": folder, "path": file_path}
    if csv_folder:
        params["csvFolder"] = csv_folder
    return "/api/graphics/image?" + urlencode(params)


def pending_image_url(folder: str, row_number: int, user_id: str) -> str:
    return "/api/graphics/pending-image?" + urlencode(
        {"folder": folder, "rowNumber": str(row_number), "userId": user_id}
    )


def load_rebuild_module() -> Any:
    global _rebuild_module
    with _rebuild_module_lock:
        if _rebuild_module is not None:
            return _rebuild_module
        scripts_dir = ROOT / "scripts"
        scripts_text = str(scripts_dir)
        if scripts_text not in sys.path:
            sys.path.insert(0, scripts_text)
        module_path = scripts_dir / "rebuild_mkd.py"
        spec = importlib.util.spec_from_file_location("_webtool_rebuild_mkd", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("rebuild_mkd.py를 불러올 수 없습니다.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _rebuild_module = module
        return module


def row_dict(row: list[str], header: list[str]) -> dict[str, str]:
    return {name: row[index] if index < len(row) else "" for index, name in enumerate(header)}


def manifest_key(source: Any, offset: Any, output: Any) -> tuple[str, str, str]:
    return (
        str(source or "").replace("\\", "/"),
        str(offset or ""),
        str(output or "").replace("\\", "/"),
    )


def manifest_key_from_record(record: dict[str, Any]) -> tuple[str, str, str]:
    return manifest_key(record.get("source"), record.get("offset"), record.get("output"))


def load_source_manifest_index(csv_path: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    manifest_path = csv_path.parent / "manifest.json"
    if not manifest_path.exists():
        return {}
    records = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"manifest.json 형식이 올바르지 않습니다: {root_relative(manifest_path)}")
    return {
        manifest_key_from_record(record): dict(record)
        for record in records
        if isinstance(record, dict)
    }


def load_source_manifest_records(csv_path: Path) -> list[dict[str, Any]]:
    manifest_path = csv_path.parent / "manifest.json"
    if not manifest_path.exists():
        return []
    records = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"manifest.json 형식이 올바르지 않습니다: {root_relative(manifest_path)}")
    return [dict(record) for record in records if isinstance(record, dict)]


def record_for_csv_row(row: list[str], header: list[str], manifest_index: dict[tuple[str, str, str], dict[str, Any]]) -> dict[str, Any]:
    values = row_dict(row, header)
    key = manifest_key(values.get("source"), values.get("offset"), values.get("output"))
    record = dict(manifest_index.get(key, values))
    for field, value in values.items():
        if value != "":
            record[field] = value
    if not record.get("source") or not record.get("offset") or not record.get("output"):
        raise ValueError("source/offset/output 컬럼이 필요합니다.")
    return record


def related_records_for_row(csv_path: Path, row: list[str], header: list[str]) -> list[dict[str, Any]]:
    manifest_index = load_source_manifest_index(csv_path)
    record = record_for_csv_row(row, header, manifest_index)
    output_rel = clean_text(record.get("output")).replace("\\", "/")
    records = [
        item
        for item in load_source_manifest_records(csv_path)
        if clean_text(item.get("output")).replace("\\", "/") == output_rel
    ]
    if not records:
        records = [record]
    return records


def load_graphic_targets(csv_path: Path) -> set[int]:
    try:
        with db_lock, connect_db() as conn:
            rows = execute(
                conn,
                "SELECT `row_number` FROM graphic_translation_targets WHERE csv_path = ? ORDER BY `row_number` ASC",
                (csv_identity(csv_path),),
            ).fetchall()
    except DB_ERRORS as exc:
        report_db_error("load_graphic_targets failed; showing no graphic targets", exc)
        return set()
    return {int(row["row_number"]) for row in rows}


def replace_graphic_targets(folder: str, row_ranges: str, user: dict[str, Any]) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    index = csv_record_index(csv_path)
    targets = parse_row_ranges(row_ranges, index["totalRows"])
    timestamp = now_ts()
    with db_lock, connect_db() as conn:
        execute(conn, "DELETE FROM graphic_translation_targets WHERE csv_path = ?", (csv_identity(csv_path),))
        for row_number in targets:
            execute(
                conn,
                """
                INSERT INTO graphic_translation_targets (csv_path, `row_number`, created_by, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (csv_identity(csv_path), row_number, user.get("id"), timestamp),
            )
    return {
        "ok": True,
        "csvPath": root_relative(csv_path),
        "targetRows": len(targets),
        "targetRowsText": format_row_ranges(targets),
    }


def load_upload_rows(csv_path: Path, *, row_numbers: set[int] | None = None, user_id: str | None = None) -> list[Any]:
    clauses = ["graphic_translation_uploads.csv_path = ?"]
    params: list[Any] = [csv_identity(csv_path)]
    if row_numbers:
        placeholders = ", ".join("?" for _ in row_numbers)
        clauses.append(f"graphic_translation_uploads.`row_number` IN ({placeholders})")
        params.extend(sorted(row_numbers))
    if user_id:
        clauses.append("graphic_translation_uploads.user_id = ?")
        params.append(user_id)
    sql = f"""
        SELECT graphic_translation_uploads.*, COALESCE(users.username, graphic_translation_uploads.user_id) AS username
        FROM graphic_translation_uploads
        LEFT JOIN users ON users.id = graphic_translation_uploads.user_id
        WHERE {' AND '.join(clauses)}
        ORDER BY graphic_translation_uploads.updated_at DESC
    """
    try:
        with db_lock, connect_db() as conn:
            return list(execute(conn, sql, params).fetchall())
    except DB_ERRORS as exc:
        report_db_error("load_graphic_uploads failed", exc)
        return []


def validation_from_row(row: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(row["validation_json"] or "{}")
    except json.JSONDecodeError:
        parsed = {}
    return {
        "ok": bool(parsed.get("ok")),
        "mode": clean_text(parsed.get("mode")),
        "warnings": [str(item) for item in parsed.get("warnings", [])],
        "errors": [str(item) for item in parsed.get("errors", [])],
        "details": parsed.get("details", []) if isinstance(parsed.get("details"), list) else [],
    }


def uploads_by_row(csv_path: Path, row_numbers: set[int], user_id: str) -> dict[int, Any]:
    return {int(row["row_number"]): row for row in load_upload_rows(csv_path, row_numbers=row_numbers, user_id=user_id)}


def upload_counts_by_row(csv_path: Path, row_numbers: set[int]) -> dict[int, int]:
    rows = load_upload_rows(csv_path, row_numbers=row_numbers)
    counts: dict[int, int] = {}
    for row in rows:
        row_number = int(row["row_number"])
        counts[row_number] = counts.get(row_number, 0) + 1
    return counts


def load_rebuild_manifest_keys(translated_root: str) -> set[tuple[str, str, str]]:
    manifest_path = resolve_project_path(translated_root, DEFAULT_KOREAN_TEXTURES_ROOT) / "manifest.json"
    if not manifest_path.exists():
        return set()
    try:
        records = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(records, list):
        return set()
    return {
        manifest_key_from_record(record)
        for record in records
        if isinstance(record, dict)
    }


def row_matches_search(row_number: int, row: list[str], header: list[str], query: str) -> bool:
    text = clean_text(query)
    if not text:
        return True
    if text.isdigit() and int(text) == row_number:
        return True
    haystack = "\n".join([str(row_number), *[cell(row, header, column) for column in header]])
    return text.lower() in haystack.lower()


def read_graphic_page(
    folder: str,
    page: int,
    show_images: bool,
    query: str = "",
    translated_root: str = DEFAULT_KOREAN_TEXTURES_ROOT,
    user_id: str | None = None,
) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    page = max(1, page)
    index = csv_record_index(csv_path)
    header: list[str] = index["header"]
    all_rows: list[list[str]] = index["rows"]
    file_col = find_column(header, FILE_COLUMNS)
    korean_col = find_column(header, KOREAN_COLUMNS)
    group_col = find_column(header, ("verified_group",))
    targets = load_graphic_targets(csv_path)
    manifest_index = load_source_manifest_index(csv_path)
    filtered = unique_texture_records([
        (row_number, all_rows[row_number - 1])
        for row_number in sorted(targets)
        if 1 <= row_number <= len(all_rows) and row_matches_search(row_number, all_rows[row_number - 1], header, query)
    ], header, file_col)
    total_rows = len(filtered)
    total_pages = max(1, (total_rows + CSV_PAGE_SIZE - 1) // CSV_PAGE_SIZE)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * CSV_PAGE_SIZE
    page_records = filtered[start : start + CSV_PAGE_SIZE]
    row_numbers = {row_number for row_number, _row in page_records}
    user_uploads = uploads_by_row(csv_path, row_numbers, user_id) if user_id else {}
    pending_counts = upload_counts_by_row(csv_path, row_numbers)
    manifest_keys = load_rebuild_manifest_keys(translated_root)
    translated_root_path = resolve_project_path(translated_root, DEFAULT_KOREAN_TEXTURES_ROOT)
    rows: list[dict[str, Any]] = []
    for row_number, row in page_records:
        file_path = cell(row, header, file_col)
        approved_rel = approved_output_rel(file_path)
        approved_path = translated_root_path / approved_rel
        record = record_for_csv_row(row, header, manifest_index)
        record_for_rebuild = {**record, "output": approved_rel}
        upload = user_uploads.get(row_number)
        item = {
            "rowNumber": row_number,
            "korean": cell(row, header, korean_col),
            "verifiedGroup": cell(row, header, group_col).strip(),
            "approvedExists": approved_path.exists(),
            "pendingUploadCount": pending_counts.get(row_number, 0),
            "rebuildTarget": manifest_key_from_record(record_for_rebuild) in manifest_keys,
        }
        if upload:
            item["myUpload"] = {
                "updatedAt": upload["updated_at"],
                "validation": validation_from_row(upload),
            }
        if show_images and file_path:
            item["imageUrl"] = graphic_image_url(folder, file_path, csv_folder=folder)
            if approved_path.exists():
                item["koreanImageUrl"] = graphic_image_url(translated_root, approved_rel)
        rows.append(item)
    return {
        "csvPath": root_relative(csv_path),
        "page": page,
        "pageSize": CSV_PAGE_SIZE,
        "totalRows": total_rows,
        "allRows": len(unique_texture_records(list(enumerate(all_rows, 1)), header, file_col)),
        "totalPages": total_pages,
        "rows": rows,
        "targetRows": len(targets),
        "targetRowsText": format_row_ranges(targets),
        "pendingUploadCount": sum(upload_counts_by_row(csv_path, targets).values()) if targets else 0,
        "translatedRoot": root_relative(translated_root_path),
    }


def pending_path_for_upload(csv_path: Path, row_number: int, user_id: str) -> Path:
    csv_hash = hashlib.sha1(csv_identity(csv_path).encode("utf-8")).hexdigest()[:16]
    return resolve_project_path(PENDING_UPLOAD_ROOT) / csv_hash / str(row_number) / f"{user_id}.png"


def decode_png_upload(content_base64: str) -> bytes:
    text = clean_text(content_base64)
    if "," in text and text.lower().startswith("data:"):
        text = text.split(",", 1)[1]
    try:
        data = base64.b64decode(text, validate=True)
    except binascii.Error as exc:
        raise ValueError("업로드 데이터를 base64로 해석할 수 없습니다.") from exc
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG 파일만 업로드할 수 있습니다.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("업로드 PNG가 너무 큽니다. 15MB 이하 파일만 허용합니다.")
    return data


def snap_png_to_target_palette(png_path: Path, rebuild: Any, pl_segment: bytes, palette_order: str) -> int:
    palette = rebuild.texture_dump.parse_palette(pl_segment, palette_order=palette_order)
    if not palette:
        return 0
    palette_by_key = {palette_key(color) for color in palette}
    transparent = next((color for color in palette if color[3] == 0), palette[0])
    opaque_palette = [color for color in palette if color[3] != 0] or palette
    changed = 0
    with Image.open(png_path) as source:
        image = source.convert("RGBA")
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            color = pixels[x, y]
            if palette_key(color) in palette_by_key:
                continue
            replacement = transparent if color[3] == 0 else min(opaque_palette, key=lambda item: palette_distance(color, item))
            pixels[x, y] = replacement
            changed += 1
    if changed:
        image.save(png_path, format="PNG")
    return changed


def snap_rgba_to_target_palette(
    width: int,
    height: int,
    rgba: bytes,
    rebuild: Any,
    pl_segment: bytes,
    palette_order: str,
) -> tuple[bytes, int]:
    palette = rebuild.texture_dump.parse_palette(pl_segment, palette_order=palette_order)
    if not palette:
        return rgba, 0
    palette_by_key = {palette_key(color) for color in palette}
    transparent = next((color for color in palette if color[3] == 0), palette[0])
    opaque_palette = [color for color in palette if color[3] != 0] or palette
    source = bytearray(rgba)
    changed = 0
    for offset in range(0, len(source), 4):
        color = (source[offset], source[offset + 1], source[offset + 2], source[offset + 3])
        if palette_key(color) in palette_by_key:
            continue
        replacement = transparent if color[3] == 0 else min(opaque_palette, key=lambda item: palette_distance(color, item))
        source[offset : offset + 4] = bytes(replacement)
        changed += 1
    return bytes(source), changed


def source_archive_entry(source_text: str) -> tuple[int, int]:
    normalized = source_text.replace("\\", "/")
    parts = normalized.split("/")
    if len(parts) != 2 or not parts[0].startswith("unpacked_"):
        raise ValueError(f"source에서 archive/entry를 해석할 수 없습니다: {source_text}")
    return int(parts[0].split("_", 1)[1]), int(Path(parts[1]).stem, 16)


def source_data_path(source_text: str) -> Path:
    source_path = resolve_project_path(source_text)
    if not source_path.exists() and not source_text.startswith("unpacked_mkd/"):
        source_path = resolve_project_path(f"unpacked_mkd/{source_text}")
    if not source_path.exists():
        raise FileNotFoundError("원본 그래픽 리소스를 찾을 수 없습니다.")
    return source_path


def png_record_view(png_path: Path, record: dict[str, Any], source_text: str, rebuild: Any) -> tuple[int, int, bytes]:
    full_width, full_height, full_rgba = rebuild.load_rgba_bytes(png_path)
    return rebuild.record_rgba_view(
        rel_source=source_text,
        output_rel=str(record.get("output", "")),
        width=full_width,
        height=full_height,
        rgba=full_rgba,
        record=record,
    )


def patch_data_with_png(
    png_path: Path,
    record: dict[str, Any],
    *,
    palette_mode: str,
    rebuild: Any,
) -> tuple[bytes, bytes, int]:
    source_text = clean_text(record.get("source"))
    if not source_text:
        raise ValueError("source가 비어 있습니다.")
    data = source_data_path(source_text).read_bytes()
    width, height, rgba = png_record_view(png_path, record, source_text, rebuild)
    tx_offset = int(record["offset"])
    palette_offset = int(record["palette_offset"])
    if tx_offset < 0 or tx_offset + 12 > len(data):
        raise ValueError("원본 텍스처 위치를 검증할 수 없습니다.")
    if palette_offset < 0 or palette_offset + 12 > len(data):
        raise ValueError("원본 팔레트 위치를 검증할 수 없습니다.")
    tx_size = rebuild.read_u32(data, tx_offset + 4)
    pl_size = rebuild.read_u32(data, palette_offset + 4)
    tx_segment = bytes(data[tx_offset : tx_offset + tx_size])
    pl_segment = bytes(data[palette_offset : palette_offset + pl_size])
    palette_order = str(record.get("palette_order", "linear"))
    snapped_pixels = 0
    if palette_mode == "snap":
        rgba, snapped_pixels = snap_rgba_to_target_palette(
            width,
            height,
            rgba,
            rebuild,
            pl_segment,
            palette_order,
        )
    layout = str(record.get("layout", "linear"))
    tilemap = rebuild.tilemap_from_record(bytearray(data), record)
    new_tx = rebuild.encode_png_into_tx(
        tx_segment=tx_segment,
        pl_segment=pl_segment,
        width=width,
        height=height,
        rgba=rgba,
        palette_order=palette_order,
        layout=layout,
        tilemap=tilemap,
    )
    patched = bytearray(data)
    patched[tx_offset : tx_offset + tx_size] = new_tx
    rebuild.patch_dialogue_line_control(patched, record, source_text)
    return data, bytes(patched), snapped_pixels


def snap_png_file_for_record(source_png: Path, destination_png: Path, record: dict[str, Any]) -> int:
    rebuild = load_rebuild_module()
    source_text = clean_text(record.get("source"))
    if not source_text:
        raise ValueError("source가 비어 있습니다.")
    data = source_data_path(source_text).read_bytes()
    palette_offset = int(record["palette_offset"])
    if palette_offset < 0 or palette_offset + 12 > len(data):
        raise ValueError("원본 팔레트 위치를 검증할 수 없습니다.")
    pl_size = rebuild.read_u32(data, palette_offset + 4)
    pl_segment = bytes(data[palette_offset : palette_offset + pl_size])
    shutil.copyfile(source_png, destination_png)
    return snap_png_to_target_palette(
        destination_png,
        rebuild,
        pl_segment,
        str(record.get("palette_order", "linear")),
    )


def sd0_slot_for_source(source_text: str, rebuild: Any) -> tuple[int, int, int]:
    archive, entry_index = source_archive_entry(source_text)
    archive_path = resolve_project_path(f"ExtractedISO/PSP_GAME/USRDIR/ZZZPSP{archive}.MKD")
    entries, _tail_offset = rebuild.parse_mkd(archive_path.read_bytes())
    if entry_index >= len(entries):
        raise ValueError(f"MKD 엔트리를 찾을 수 없습니다: {source_text}")
    entry = entries[entry_index]
    if entry.kind != "SD0":
        raise ValueError(f"SD0 엔트리가 아닙니다: {source_text}")
    return archive, entry_index, int(entry.stored_size)


def sd0_check_records(
    png_path: Path,
    records: list[dict[str, Any]],
    *,
    palette_mode: str,
    use_optimal: bool = True,
) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    details: list[dict[str, Any]] = []
    total_snapped = 0
    try:
        rebuild = load_rebuild_module()
        for record in records:
            source_text = clean_text(record.get("source"))
            if not source_text:
                raise ValueError("source가 비어 있습니다.")
            raw, patched, snapped_pixels = patch_data_with_png(
                png_path,
                record,
                palette_mode=palette_mode,
                rebuild=rebuild,
            )
            total_snapped += snapped_pixels
            archive, entry_index, slot_size = sd0_slot_for_source(source_text, rebuild)
            greedy_size = len(rebuild.compress_sd0(patched))
            optimal_size: int | None = None
            fits = greedy_size <= slot_size
            if not fits and use_optimal:
                optimal_size = len(rebuild.encode_sd0_tokens(patched, rebuild.optimal_sd0_tokens(patched)))
                fits = optimal_size <= slot_size
            changed_bytes = sum(1 for left, right in zip(raw, patched) if left != right)
            details.append(
                {
                    "source": source_text,
                    "archive": archive,
                    "entry": f"{entry_index:08x}",
                    "slotSize": slot_size,
                    "greedySize": greedy_size,
                    "optimalSize": optimal_size,
                    "fits": fits,
                    "changedBytes": changed_bytes,
                    "snappedPixels": snapped_pixels,
                }
            )
            if not fits:
                best_size = optimal_size or greedy_size
                errors.append(
                    f"{source_text}: SD0 {best_size:,}바이트가 원본 슬롯 {slot_size:,}바이트를 초과합니다."
                )
            elif optimal_size is not None:
                warnings.append(
                    f"{source_text}: greedy는 {greedy_size:,}/{slot_size:,}바이트로 초과, optimal은 {optimal_size:,}바이트로 통과"
                )
        if palette_mode == "snap" and total_snapped:
            warnings.append(f"팔레트가 변환 됐습니다. ({total_snapped}픽셀)")
        if not errors:
            warnings.append("SD0 압축 리빌드 가능")
    except Exception as exc:
        errors.append(str(exc))
    return {
        "ok": not errors,
        "mode": "palette" if palette_mode == "snap" else "compression",
        "warnings": warnings,
        "errors": errors,
        "details": details,
    }


def prepare_palette_check_candidate(png_path: Path, records: list[dict[str, Any]]) -> tuple[Path, int]:
    if not records:
        raise ValueError("검사할 manifest 레코드가 없습니다.")
    temp_file = tempfile.NamedTemporaryFile("wb", suffix=".png", delete=False)
    temp_path = Path(temp_file.name)
    temp_file.close()
    try:
        changed = snap_png_file_for_record(png_path, temp_path, records[0])
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    return temp_path, changed


def check_upload_png_against_records(
    png_path: Path,
    records: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    if mode == "palette":
        temp_path, changed = prepare_palette_check_candidate(png_path, records)
        try:
            validation = sd0_check_records(temp_path, records, palette_mode="strict")
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass
        validation["mode"] = "palette"
        if changed:
            validation.setdefault("warnings", []).insert(0, f"팔레트가 변환 됐습니다. ({changed}픽셀)")
        return validation
    return sd0_check_records(png_path, records, palette_mode="strict")


def validate_png_against_record(png_path: Path, record: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    try:
        rebuild = load_rebuild_module()
        source_text = clean_text(record.get("source"))
        if not source_text:
            raise ValueError("source가 비어 있습니다.")
        source_path = resolve_project_path(source_text)
        if not source_path.exists() and not source_text.startswith("unpacked_mkd/"):
            source_path = resolve_project_path(f"unpacked_mkd/{source_text}")
        if not source_path.exists():
            raise FileNotFoundError("원본 그래픽 리소스를 찾을 수 없습니다.")
        data = source_path.read_bytes()
        full_width, full_height, full_rgba = rebuild.load_rgba_bytes(png_path)
        digest = rebuild.output_digest(full_width, full_height, full_rgba)
        if digest == clean_text(record.get("sha1")):
            warnings.append("원본 일본어 이미지와 동일합니다.")
        width, height, rgba = rebuild.record_rgba_view(
            rel_source=source_text,
            output_rel=str(record.get("output", "")),
            width=full_width,
            height=full_height,
            rgba=full_rgba,
            record=record,
        )
        tx_offset = int(record["offset"])
        palette_offset = int(record["palette_offset"])
        if tx_offset < 0 or tx_offset + 12 > len(data):
            raise ValueError("원본 텍스처 위치를 검증할 수 없습니다.")
        if palette_offset < 0 or palette_offset + 12 > len(data):
            raise ValueError("원본 팔레트 위치를 검증할 수 없습니다.")
        tx_size = rebuild.read_u32(data, tx_offset + 4)
        pl_size = rebuild.read_u32(data, palette_offset + 4)
        tx_segment = bytes(data[tx_offset : tx_offset + tx_size])
        pl_segment = bytes(data[palette_offset : palette_offset + pl_size])
        layout = str(record.get("layout", "linear"))
        changed_pixels = snap_png_to_target_palette(
            png_path,
            rebuild,
            pl_segment,
            str(record.get("palette_order", "linear")),
        )
        if changed_pixels:
            warnings.append(f"팔레트가 변환 됐습니다. ({changed_pixels}픽셀)")
            full_width, full_height, full_rgba = rebuild.load_rgba_bytes(png_path)
            width, height, rgba = rebuild.record_rgba_view(
                rel_source=source_text,
                output_rel=str(record.get("output", "")),
                width=full_width,
                height=full_height,
                rgba=full_rgba,
                record=record,
            )
        tilemap = rebuild.tilemap_from_record(bytearray(data), record)
        rebuild.encode_png_into_tx(
            tx_segment=tx_segment,
            pl_segment=pl_segment,
            width=width,
            height=height,
            rgba=rgba,
            palette_order=str(record.get("palette_order", "linear")),
            layout=layout,
            tilemap=tilemap,
        )
    except Exception as exc:
        errors.append(str(exc))
    return {"ok": not errors, "warnings": warnings, "errors": errors}


def unchecked_upload_validation() -> dict[str, Any]:
    return {
        "ok": False,
        "mode": "unchecked",
        "warnings": ["관리자 압축체크 또는 팔레트체크가 필요합니다."],
        "errors": [],
        "details": [],
    }


def save_graphic_upload(folder: str, row_number: int, filename: str, content_base64: str, user: dict[str, Any]) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    index = csv_record_index(csv_path)
    if row_number < 1 or row_number > index["totalRows"]:
        raise ValueError("CSV 행 번호가 범위를 벗어났습니다.")
    targets = load_graphic_targets(csv_path)
    if row_number not in targets:
        raise ValueError("그래픽 작업 대상으로 지정되지 않은 행입니다.")
    header: list[str] = index["header"]
    row = index["rows"][row_number - 1]
    upload_bytes = decode_png_upload(content_base64)
    pending_path = pending_path_for_upload(csv_path, row_number, user["id"])
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=pending_path.parent, delete=False) as target:
        temp_name = target.name
        target.write(upload_bytes)
    try:
        with graphics_file_lock:
            os.replace(temp_name, pending_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    validation = unchecked_upload_validation()
    timestamp = now_ts()
    relative_pending = root_relative(pending_path)
    with db_lock, connect_db() as conn:
        existing = execute(
            conn,
            """
            SELECT created_at FROM graphic_translation_uploads
            WHERE csv_path = ? AND `row_number` = ? AND user_id = ?
            """,
            (csv_identity(csv_path), row_number, user["id"]),
        ).fetchone()
        created_at = existing["created_at"] if existing else timestamp
        params = (
            csv_identity(csv_path),
            row_number,
            user["id"],
            relative_pending,
            Path(clean_text(filename, "upload.png")).name,
            json.dumps(validation, ensure_ascii=False),
            created_at,
            timestamp,
        )
        if DB_BACKEND in {"mysql", "mysql+pymysql"}:
            execute(
                conn,
                """
                INSERT INTO graphic_translation_uploads
                    (csv_path, `row_number`, user_id, pending_path, original_filename, validation_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    pending_path = VALUES(pending_path),
                    original_filename = VALUES(original_filename),
                    validation_json = VALUES(validation_json),
                    updated_at = VALUES(updated_at)
                """,
                params,
            )
        else:
            execute(
                conn,
                """
                INSERT INTO graphic_translation_uploads
                    (csv_path, `row_number`, user_id, pending_path, original_filename, validation_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(csv_path, `row_number`, user_id) DO UPDATE SET
                    pending_path = excluded.pending_path,
                    original_filename = excluded.original_filename,
                    validation_json = excluded.validation_json,
                    updated_at = excluded.updated_at
                """,
                params,
            )
    return {"ok": True, "rowNumber": row_number, "validation": validation}


def check_graphic_upload(
    folder: str,
    row_number: int,
    user_id: str,
    mode: str,
) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    index = csv_record_index(csv_path)
    if row_number < 1 or row_number > index["totalRows"]:
        raise ValueError("CSV 행 번호가 범위를 벗어났습니다.")
    uploads = load_upload_rows(csv_path, row_numbers={row_number}, user_id=user_id)
    if not uploads:
        raise ValueError("업로드를 찾을 수 없습니다.")
    check_mode = "palette" if mode == "palette" else "compression"
    records = related_records_for_row(csv_path, index["rows"][row_number - 1], index["header"])
    pending_path = resolve_project_path(uploads[0]["pending_path"])
    validation = check_upload_png_against_records(pending_path, records, check_mode)
    timestamp = now_ts()
    with db_lock, connect_db() as conn:
        execute(
            conn,
            """
            UPDATE graphic_translation_uploads
            SET validation_json = ?, updated_at = ?
            WHERE csv_path = ? AND `row_number` = ? AND user_id = ?
            """,
            (
                json.dumps(validation, ensure_ascii=False),
                timestamp,
                csv_identity(csv_path),
                row_number,
                user_id,
            ),
        )
    return {"ok": True, "rowNumber": row_number, "userId": user_id, "validation": validation}


def upload_record_preview(
    folder: str,
    upload: Any,
    header: list[str],
    row: list[str] | None,
    translated_root: str,
) -> dict[str, Any]:
    row_number = int(upload["row_number"])
    file_col = find_column(header, FILE_COLUMNS)
    korean_col = find_column(header, KOREAN_COLUMNS)
    group_col = find_column(header, ("verified_group",))
    file_path = cell(row or [], header, file_col)
    approved_rel = approved_output_rel(file_path) if file_path else ""
    translated_root_path = resolve_project_path(translated_root, DEFAULT_KOREAN_TEXTURES_ROOT)
    validation = validation_from_row(upload)
    item = {
        "rowNumber": row_number,
        "userId": upload["user_id"],
        "username": upload["username"],
        "updatedAt": upload["updated_at"],
        "korean": cell(row or [], header, korean_col),
        "verifiedGroup": cell(row or [], header, group_col).strip(),
        "validation": validation,
        "canApprove": bool(validation.get("ok")),
        "pendingImageUrl": pending_image_url(folder, row_number, upload["user_id"]),
        "approvedExists": bool(approved_rel and (translated_root_path / approved_rel).exists()),
    }
    if row is not None and file_path:
        item["imageUrl"] = graphic_image_url(folder, file_path, csv_folder=folder)
        if item["approvedExists"]:
            item["koreanImageUrl"] = graphic_image_url(translated_root, approved_rel)
    return item


def list_graphic_uploads(folder: str, translated_root: str = DEFAULT_KOREAN_TEXTURES_ROOT) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    index = csv_record_index(csv_path)
    header: list[str] = index["header"]
    rows: list[list[str]] = index["rows"]
    uploads = load_upload_rows(csv_path)
    previews = [
        upload_record_preview(
            folder,
            upload,
            header,
            rows[int(upload["row_number"]) - 1] if 1 <= int(upload["row_number"]) <= len(rows) else None,
            translated_root,
        )
        for upload in uploads
    ]
    return {
        "csvPath": root_relative(csv_path),
        "total": len(previews),
        "valid": sum(1 for item in previews if item["canApprove"]),
        "invalid": sum(1 for item in previews if not item["canApprove"]),
        "uploads": previews,
    }


def remove_pending_files(rows: list[Any]) -> None:
    for row in rows:
        try:
            path = resolve_project_path(row["pending_path"])
            if path.exists():
                path.unlink()
        except OSError:
            pass


def selected_upload_keys(items: list[dict[str, Any]]) -> set[tuple[int, str]]:
    keys: set[tuple[int, str]] = set()
    for item in items:
        try:
            row_number = int(item.get("rowNumber"))
        except (TypeError, ValueError):
            continue
        user_id = clean_text(item.get("userId"))
        if row_number > 0 and user_id:
            keys.add((row_number, user_id))
    return keys


def approve_graphic_uploads(folder: str, items: list[dict[str, Any]], translated_root: str = DEFAULT_KOREAN_TEXTURES_ROOT) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    selected = selected_upload_keys(items)
    if not selected:
        raise ValueError("승인할 업로드를 선택하세요.")
    index = csv_record_index(csv_path)
    header: list[str] = index["header"]
    rows: list[list[str]] = index["rows"]
    file_col = find_column(header, FILE_COLUMNS)
    approved = 0
    skipped: list[dict[str, Any]] = []
    with graphics_file_lock:
        for row_number, user_id in selected:
            upload_rows = load_upload_rows(csv_path, row_numbers={row_number}, user_id=user_id)
            if not upload_rows:
                skipped.append({"rowNumber": row_number, "userId": user_id, "errors": ["업로드를 찾을 수 없습니다."]})
                continue
            upload = upload_rows[0]
            if row_number < 1 or row_number > len(rows):
                skipped.append({"rowNumber": row_number, "userId": user_id, "errors": ["CSV 행을 찾을 수 없습니다."]})
                continue
            row = rows[row_number - 1]
            pending_path = resolve_project_path(upload["pending_path"])
            validation = validation_from_row(upload)
            if not validation.get("ok"):
                messages = validation.get("errors") or validation.get("warnings") or ["압축체크 또는 팔레트체크를 통과해야 승인할 수 있습니다."]
                skipped.append({"rowNumber": row_number, "userId": user_id, "errors": messages})
                continue
            approved_rel = approved_output_rel(cell(row, header, file_col))
            destination = output_path(translated_root, approved_rel)
            destination.parent.mkdir(parents=True, exist_ok=True)
            validation_payload = json.loads(upload["validation_json"] or "{}")
            if validation_payload.get("mode") == "palette":
                records = related_records_for_row(csv_path, row, header)
                temp_file = tempfile.NamedTemporaryFile("wb", suffix=".png", dir=destination.parent, delete=False)
                temp_path = Path(temp_file.name)
                temp_file.close()
                try:
                    snap_png_file_for_record(pending_path, temp_path, records[0])
                    shutil.copyfile(temp_path, destination)
                finally:
                    if temp_path.exists():
                        temp_path.unlink()
            else:
                shutil.copyfile(pending_path, destination)
            all_row_uploads = load_upload_rows(csv_path, row_numbers={row_number})
            remove_pending_files(all_row_uploads)
            with db_lock, connect_db() as conn:
                execute(
                    conn,
                    "DELETE FROM graphic_translation_uploads WHERE csv_path = ? AND `row_number` = ?",
                    (csv_identity(csv_path), row_number),
                )
            approved += 1
    return {"ok": True, "approved": approved, "skipped": skipped, "translatedRoot": translated_root}


def discard_graphic_uploads(folder: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    selected = selected_upload_keys(items)
    deleted = 0
    with graphics_file_lock:
        for row_number, user_id in selected:
            rows = load_upload_rows(csv_path, row_numbers={row_number}, user_id=user_id)
            remove_pending_files(rows)
            with db_lock, connect_db() as conn:
                cursor = execute(
                    conn,
                    """
                    DELETE FROM graphic_translation_uploads
                    WHERE csv_path = ? AND `row_number` = ? AND user_id = ?
                    """,
                    (csv_identity(csv_path), row_number, user_id),
                )
                deleted += int(getattr(cursor, "rowcount", 0) or 0)
    return {"ok": True, "deleted": deleted}


def write_graphic_rebuild_manifest(folder: str, translated_root: str = DEFAULT_KOREAN_TEXTURES_ROOT) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    source_manifest = csv_path.parent / "manifest.json"
    if not source_manifest.exists():
        raise ValueError(f"원본 manifest.json이 없습니다: {root_relative(source_manifest)}")
    translated_root_path = resolve_project_path(translated_root, DEFAULT_KOREAN_TEXTURES_ROOT)
    records = json.loads(source_manifest.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("원본 manifest.json 형식이 올바르지 않습니다.")
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        output_rel = clean_text(record.get("output"))
        if not output_rel:
            continue
        candidates = [approved_output_rel(output_rel)]
        original_rel = safe_rel_path(output_rel)
        if original_rel not in candidates:
            candidates.append(original_rel)
        selected_rel = next((rel for rel in candidates if (translated_root_path / rel).exists()), "")
        if not selected_rel:
            continue
        next_record = dict(record)
        next_record["output"] = selected_rel
        key = manifest_key_from_record(next_record)
        if key in seen:
            continue
        seen.add(key)
        selected.append(next_record)
    translated_root_path.mkdir(parents=True, exist_ok=True)
    manifest_path = translated_root_path / "manifest.json"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=translated_root_path, delete=False) as target:
        temp_name = target.name
        json.dump(selected, target, ensure_ascii=False, indent=2)
        target.write("\n")
    os.replace(temp_name, manifest_path)
    return {
        "ok": True,
        "records": len(selected),
        "images": len({record["output"] for record in selected}),
    }


def resolve_graphic_image(folder: str, file_path: str, csv_folder: str = "") -> Path:
    csv_path = find_translation_csv(csv_folder) if clean_text(csv_folder) else None
    return resolve_image_path(folder, file_path, csv_path)


def resolve_pending_image(folder: str, row_number: int, user_id: str, current_user: dict[str, Any]) -> Path:
    csv_path = find_translation_csv(folder)
    if current_user.get("role") != ROLE_ADMIN and current_user.get("id") != user_id:
        raise PermissionError("다른 사용자의 임시 이미지를 볼 수 없습니다.")
    rows = load_upload_rows(csv_path, row_numbers={row_number}, user_id=user_id)
    if not rows:
        raise LookupError("업로드 이미지를 찾을 수 없습니다.")
    return resolve_project_path(rows[0]["pending_path"])
