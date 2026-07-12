from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import threading
import unicodedata
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from ..core.config import (
    CSV_PAGE_SIZE,
    DEFAULT_KOREAN_TEXTURES_ROOT,
    DEFAULT_TEXTURES_ROOT,
    DIALOGUE_LINE_CONTROL_OFFSET_COLUMNS,
    DIALOGUE_LINE_COUNT_COLUMNS,
    DIALOGUE_LINE_LENGTHS_COLUMNS,
    FILE_COLUMNS,
    JAPANESE_COLUMNS,
    KOREAN_COLUMNS,
    ROLE_ADMIN,
    ROOT,
)
from ..core.db import DB_BACKEND, DB_ERRORS, connect_db, db_lock, execute, report_db_error
from ..core.utils import clean_text, now_ts, resolve_project_path, root_relative


csv_index_cache: dict[str, dict[str, Any]] = {}
csv_index_lock = threading.Lock()
csv_write_lock = threading.Lock()
SHA1_COLUMNS = ("sha1",)
EDITABLE_FIELDS = ("japanese", "korean", "dialogueLineLengths")
LINE_LENGTH_TARGET_GROUPS = {"대사들", "각 세력 오프닝"}
TEXT_CAPACITY_TARGET_GROUPS = {"각 세력 오프닝"}
PER_LINE_LIMITS = {"대사들": 21, "각 세력 오프닝": 26}
MAX_LINE_COUNTS = {"각 세력 오프닝": 2}
HALF_CELL_CHARS = {"각 세력 오프닝": set(" .,!，．！，．！ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvxyz1234567890")}


def find_translation_csv(folder: str) -> Path:
    target = resolve_project_path(folder, "textures_static")
    if target.is_file():
        if target.suffix.lower() != ".csv":
            raise ValueError("CSV 파일 또는 CSV가 있는 폴더를 지정하세요.")
        return target
    if not target.exists():
        raise ValueError(f"폴더가 없습니다: {root_relative(target)}")
    preferred = target / "manifest.csv"
    if preferred.exists():
        return preferred
    csv_files = sorted(target.glob("*.csv"))
    if not csv_files:
        raise ValueError(f"CSV 파일이 없습니다: {root_relative(target)}")
    return csv_files[0]


def find_column(header: list[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in header:
            return candidate
    lowered = {name.lower(): name for name in header}
    for candidate in candidates:
        match = lowered.get(candidate.lower())
        if match:
            return match
    return None


def cell(row: list[str], header: list[str], column: str | None) -> str:
    if column is None:
        return ""
    try:
        index = header.index(column)
    except ValueError:
        return ""
    return row[index] if index < len(row) else ""


def parse_dialogue_line_lengths(value: str) -> list[int]:
    text = clean_text(value)
    if not text:
        return []
    for separator in (";", "|", "/", " "):
        text = text.replace(separator, ",")
    lengths: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        number = int(part, 0)
        if number <= 0 or number > 0xFF:
            raise ValueError(f"줄 길이는 1~255 범위여야 합니다: {number}")
        lengths.append(number)
    return lengths


def normalize_dialogue_line_lengths(value: str) -> str:
    return ",".join(str(length) for length in parse_dialogue_line_lengths(value))


def normalize_text_lines(value: str) -> list[str]:
    text = unicodedata.normalize("NFC", str(value or "")).replace("\r\n", "\n").replace("\r", "\n").replace("　", " ")
    lines = []
    for line in text.split("\n"):
        collapsed = " ".join(line.strip().split())
        if collapsed:
            lines.append(collapsed)
    return lines


def group_half_cell_chars(group: str) -> set[str]:
    return HALF_CELL_CHARS.get(group.strip(), set())


def line_cell_units(line: str, group: str) -> int:
    half_cell_chars = group_half_cell_chars(group)
    if not half_cell_chars:
        return len(line) * 2
    return sum(1 if char in half_cell_chars else 2 for char in line)


def truncate_line_to_cell_units(line: str, max_units: int, group: str) -> str:
    if max_units <= 0:
        return line
    half_cell_chars = group_half_cell_chars(group)
    if not half_cell_chars:
        return line[: max_units // 2]

    current = ""
    current_units = 0
    for char in line:
        units = 1 if char in half_cell_chars else 2
        if current_units + units > max_units:
            break
        current += char
        current_units += units
    return current


def logical_length_from_units(units: int) -> int | float:
    if units % 2 == 0:
        return units // 2
    return units / 2


def render_lines_for_lengths(value: str, group: str) -> list[str]:
    lines = normalize_text_lines(value)
    if group.strip() == "각 세력 오프닝":
        max_lines = MAX_LINE_COUNTS["각 세력 오프닝"]
        max_units = PER_LINE_LIMITS["각 세력 오프닝"] * 2
        lines = [
            truncate_line_to_cell_units(line, max_units, group)
            for line in lines[:max_lines]
        ]
    if group.strip() == "각 세력 오프닝" and len(lines) == 2 and line_cell_units(lines[0], group) % 2 == 1:
        lines = [f"{lines[0]} ", lines[1]]
    return lines


def logical_cell_length_for_manifest(line: str, group: str) -> int:
    units = line_cell_units(line, group)
    return (units + 1) // 2


def line_lengths_from_text(value: str, group: str = "") -> str:
    if group_half_cell_chars(group):
        return ",".join(str(logical_cell_length_for_manifest(line, group)) for line in render_lines_for_lengths(value, group))
    return ",".join(str(len(line)) for line in render_lines_for_lengths(value, group))


def normalized_text_length(value: str, group: str = "") -> int | float:
    lines = normalize_text_lines(value)
    if not group_half_cell_chars(group):
        return sum(len(line) for line in lines)
    return logical_length_from_units(sum(line_cell_units(line, group) for line in lines))


def parse_texture_dimensions(file_path: str) -> tuple[int, int]:
    name = Path(file_path or "").name
    stem = Path(name).stem
    for part in stem.split("_"):
        if "x" not in part:
            continue
        width_text, height_text = part.lower().split("x", 1)
        if width_text.isdigit() and height_text.isdigit():
            width = int(width_text)
            height = int(height_text)
            if width > 0 and height > 0:
                return width, height
    return 0, 0


def texture_text_capacity(width: int, height: int) -> int:
    if width <= 0 or height <= 0:
        return 0
    return (width // 16) * (height // 16)


def group_max_lines(group: str) -> int:
    return MAX_LINE_COUNTS.get(group.strip(), 0)


def group_text_capacity(width: int, height: int, group: str) -> int:
    line_limit = group_line_limit(group)
    max_lines = group_max_lines(group)
    if line_limit > 0 and max_lines > 0:
        return line_limit * max_lines
    return texture_text_capacity(width, height)


def group_line_limit(group: str) -> int:
    return PER_LINE_LIMITS.get(group.strip(), 0)


def line_overflow_amount(value: str, group: str) -> tuple[int, int | float]:
    limit = group_line_limit(group)
    if limit <= 0:
        return 0, 0
    lines = normalize_text_lines(value)
    if not lines:
        return limit, 0
    limit_units = limit * 2
    line_excess_units = max(max(0, line_cell_units(line, group) - limit_units) for line in lines)
    max_lines = group_max_lines(group)
    line_count_excess = max(0, len(lines) - max_lines) if max_lines > 0 else 0
    return limit, max(logical_length_from_units(line_excess_units), line_count_excess)


def display_number(file_path: str, fallback: int) -> str:
    name = Path(file_path).name
    for char_index, char in enumerate(name):
        if char.isdigit():
            end = char_index + 1
            while end < len(name) and name[end].isdigit():
                end += 1
            return name[char_index:end]
    return str(fallback)


def resolve_image_path(folder: str, file_path: str, csv_path: Path | None = None) -> Path:
    raw = clean_text(file_path)
    if not raw:
        raise ValueError("이미지 경로가 비어 있습니다.")
    candidate = Path(raw).expanduser()
    folder_root = resolve_project_path(folder, DEFAULT_TEXTURES_ROOT)
    bases = [folder_root, ROOT]
    if csv_path is not None:
        bases.insert(0, csv_path.parent)
    if candidate.is_absolute():
        bases = [Path("/")]
    for base in bases:
        resolved = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        try:
            resolved.relative_to(ROOT.resolve())
        except ValueError:
            continue
        if resolved.exists():
            return resolved
    base = csv_path.parent if not candidate.is_absolute() else Path("/")
    resolved = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    resolved.relative_to(ROOT.resolve())
    return resolved


def translation_image_url(folder: str, file_path: str) -> str:
    return "/api/translation/image?" + urlencode({"folder": folder, "path": file_path})


def translated_texture_rel_path(file_path: str) -> str:
    text = clean_text(file_path).replace("\\", "/")
    if not text:
        return ""
    path = Path(text)
    try:
        if path.is_absolute():
            relative = path.resolve().relative_to(ROOT.resolve())
            parts = relative.parts
        else:
            parts = path.parts
        if parts and parts[0] in {DEFAULT_TEXTURES_ROOT, DEFAULT_KOREAN_TEXTURES_ROOT}:
            parts = parts[1:]
        if not parts or ".." in parts:
            return ""
        rel_path = Path(*parts)
        target_root = resolve_project_path(DEFAULT_KOREAN_TEXTURES_ROOT, DEFAULT_KOREAN_TEXTURES_ROOT)
        target = (target_root / rel_path).resolve()
        target.relative_to(target_root.resolve())
    except ValueError:
        return ""
    return str(rel_path) if target.exists() and target.is_file() else ""


def translated_texture_image_url(file_path: str) -> str:
    rel_path = translated_texture_rel_path(file_path)
    return translation_image_url(DEFAULT_KOREAN_TEXTURES_ROOT, rel_path) if rel_path else ""


def csv_record_index(csv_path: Path) -> dict[str, Any]:
    stat = csv_path.stat()
    cache_key = str(csv_path.resolve())
    with csv_index_lock:
        cached = csv_index_cache.get(cache_key)
        if cached and cached["mtimeNs"] == stat.st_mtime_ns and cached["size"] == stat.st_size:
            return cached
    csv.field_size_limit(sys.maxsize)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        rows = list(reader)
    index = {
        "mtimeNs": stat.st_mtime_ns,
        "size": stat.st_size,
        "header": header,
        "rows": rows,
        "totalRows": len(rows),
    }
    with csv_index_lock:
        csv_index_cache[cache_key] = index
    return index


def csv_identity(csv_path: Path) -> str:
    return root_relative(csv_path)


def require_sha1_column(header: list[str]) -> str:
    sha1_col = find_column(header, SHA1_COLUMNS)
    if sha1_col is None:
        raise ValueError("3-way merge에는 sha1 컬럼이 필요합니다.")
    return sha1_col


def ensure_column(header: list[str], column: str) -> str:
    existing = find_column(header, (column,))
    if existing:
        return existing
    header.append(column)
    return column


def editable_columns(header: list[str], *, ensure: bool = False) -> dict[str, str | None]:
    japanese_col = find_column(header, JAPANESE_COLUMNS)
    korean_col = find_column(header, KOREAN_COLUMNS)
    line_count_col = find_column(header, DIALOGUE_LINE_COUNT_COLUMNS)
    line_lengths_col = find_column(header, DIALOGUE_LINE_LENGTHS_COLUMNS)
    if ensure:
        japanese_col = japanese_col or ensure_column(header, "japanese")
        korean_col = korean_col or ensure_column(header, "korean")
        line_count_col = line_count_col or ensure_column(header, "dialogue_line_count")
        line_lengths_col = line_lengths_col or ensure_column(header, "dialogue_line_lengths")
    return {
        "japanese": japanese_col,
        "korean": korean_col,
        "dialogueLineCount": line_count_col,
        "dialogueLineLengths": line_lengths_col,
    }


def ensure_row_width(row: list[str], header: list[str]) -> None:
    if len(row) < len(header):
        row.extend([""] * (len(header) - len(row)))


def editable_values(row: list[str], header: list[str], columns: dict[str, str | None]) -> dict[str, str]:
    return {
        "japanese": cell(row, header, columns.get("japanese")),
        "korean": cell(row, header, columns.get("korean")),
        "dialogueLineLengths": cell(row, header, columns.get("dialogueLineLengths")),
    }


def normalize_editable_payload(payload: dict[str, Any], fallback: dict[str, str], group: str) -> dict[str, str]:
    korean = str(payload.get("korean", fallback.get("korean", "")))
    if group in LINE_LENGTH_TARGET_GROUPS:
        line_lengths = line_lengths_from_text(korean, group)
    else:
        line_lengths = normalize_dialogue_line_lengths(str(payload.get("dialogueLineLengths", fallback.get("dialogueLineLengths", ""))))
    return {
        "japanese": str(payload.get("japanese", fallback.get("japanese", ""))),
        "korean": korean,
        "dialogueLineLengths": line_lengths,
    }


def json_dumps(value: dict[str, str]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads_object(value: str) -> dict[str, str]:
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        return {}
    return {field: str(parsed.get(field, "")) for field in EDITABLE_FIELDS}


def load_user_drafts(csv_path: Path, user_id: str | None) -> dict[str, dict[str, Any]]:
    if not user_id:
        return {}
    try:
        with db_lock, connect_db() as conn:
            rows = execute(
                conn,
                """
                SELECT translation_drafts.*, users.username
                FROM translation_drafts
                JOIN users ON users.id = translation_drafts.user_id
                WHERE csv_path = ? AND user_id = ?
                """,
                (csv_identity(csv_path), user_id),
            ).fetchall()
    except DB_ERRORS as exc:
        report_db_error("load_user_drafts failed; showing main CSV only", exc)
        return {}
    return {row["sha1"]: draft_record_from_row(row) for row in rows}


def load_translation_row_marks(csv_path: Path) -> dict[str, Any]:
    try:
        with db_lock, connect_db() as conn:
            pending_rows = execute(
                conn,
                """
                SELECT sha1, COUNT(*) AS draft_count
                FROM translation_drafts
                WHERE csv_path = ?
                GROUP BY sha1
                """,
                (csv_identity(csv_path),),
            ).fetchall()
            merged_rows = execute(
                conn,
                "SELECT sha1 FROM translation_row_marks WHERE csv_path = ?",
                (csv_identity(csv_path),),
            ).fetchall()
    except DB_ERRORS as exc:
        report_db_error("load_translation_row_marks failed; showing rows without modified marks", exc)
        return {"pendingDraftCounts": {}, "mergedSha1s": set()}
    return {
        "pendingDraftCounts": {row["sha1"]: int(row["draft_count"] or 0) for row in pending_rows},
        "mergedSha1s": {row["sha1"] for row in merged_rows},
    }


def mark_translation_rows_merged(conn: Any, csv_path: Path, sha1s: set[str]) -> None:
    if not sha1s:
        return
    timestamp = now_ts()
    for sha1 in sha1s:
        params = (csv_identity(csv_path), sha1, timestamp)
        if DB_BACKEND in {"mysql", "mysql+pymysql"}:
            execute(
                conn,
                """
                INSERT INTO translation_row_marks (csv_path, sha1, marked_at)
                VALUES (?, ?, ?)
                ON DUPLICATE KEY UPDATE marked_at = VALUES(marked_at)
                """,
                params,
            )
        else:
            execute(
                conn,
                """
                INSERT INTO translation_row_marks (csv_path, sha1, marked_at)
                VALUES (?, ?, ?)
                ON CONFLICT(csv_path, sha1) DO UPDATE SET marked_at = excluded.marked_at
                """,
                params,
            )


def draft_record_from_row(row: Any) -> dict[str, Any]:
    return {
        "csvPath": row["csv_path"],
        "sha1": row["sha1"],
        "userId": row["user_id"],
        "username": row["username"],
        "base": json_loads_object(row["base_json"]),
        "draft": json_loads_object(row["draft_json"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def translation_log_from_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "csvPath": row["csv_path"],
        "sha1": row["sha1"],
        "action": row["action"],
        "submittedUserId": row["submitted_user_id"],
        "submittedUsername": row["submitted_username"],
        "executorUserId": row["executor_user_id"],
        "executorUsername": row["executor_username"],
        "original": json_loads_object(row["original_json"]),
        "submitted": json_loads_object(row["submitted_json"]),
        "applied": json_loads_object(row["applied_json"]),
        "merged": bool(row["merged"]),
        "note": row["note"],
        "executedAt": row["executed_at"],
        "notifiedAt": row["notified_at"],
    }


def insert_translation_merge_log(
    conn: Any,
    csv_path: Path,
    record: dict[str, Any],
    *,
    action: str,
    merged: bool,
    executor: dict[str, Any],
    note: str = "",
    applied: dict[str, str] | None = None,
    executed_at: float | None = None,
) -> int:
    timestamp = executed_at or now_ts()
    cursor = execute(
        conn,
        """
        INSERT INTO translation_merge_logs
            (csv_path, sha1, action, submitted_user_id, submitted_username, executor_user_id, executor_username,
             original_json, submitted_json, applied_json, merged, note, executed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            csv_identity(csv_path),
            record["sha1"],
            action,
            record["userId"],
            record["username"],
            executor["id"],
            executor["username"],
            json_dumps(record["base"]),
            json_dumps(record["draft"]),
            json_dumps(applied or {}),
            1 if merged else 0,
            clean_text(note),
            timestamp,
        ),
    )
    return int(getattr(cursor, "lastrowid", 0) or 0)


def manifest_rows_by_sha1(rows: list[list[str]], header: list[str], sha1_col: str) -> dict[str, list[tuple[int, list[str]]]]:
    grouped: dict[str, list[tuple[int, list[str]]]] = defaultdict(list)
    for row_number, row in enumerate(rows, 1):
        sha1 = clean_text(cell(row, header, sha1_col))
        if sha1:
            grouped[sha1].append((row_number, row))
    return dict(grouped)


def merge_editable_values(
    base: dict[str, str],
    current: dict[str, str],
    draft: dict[str, str],
    *,
    force_conflicts: bool = False,
) -> tuple[dict[str, str], list[str], list[str]]:
    merged: dict[str, str] = {}
    changed_fields: list[str] = []
    conflict_fields: list[str] = []
    for field in EDITABLE_FIELDS:
        base_value = str(base.get(field, ""))
        current_value = str(current.get(field, ""))
        draft_value = str(draft.get(field, ""))
        if draft_value != base_value:
            changed_fields.append(field)
        if draft_value == base_value:
            merged[field] = current_value
        elif current_value == base_value or current_value == draft_value or force_conflicts:
            merged[field] = draft_value
        else:
            merged[field] = current_value
            conflict_fields.append(field)
    return merged, changed_fields, conflict_fields


def draft_status(base: dict[str, str], current: dict[str, str], draft: dict[str, str]) -> dict[str, Any]:
    _merged, changed_fields, conflict_fields = merge_editable_values(base, current, draft)
    already_applied = bool(changed_fields) and all(str(current.get(field, "")) == str(draft.get(field, "")) for field in changed_fields)
    return {
        "changedFields": changed_fields,
        "hasChanges": bool(changed_fields),
        "conflictFields": conflict_fields,
        "hasConflict": bool(conflict_fields),
        "alreadyApplied": already_applied,
        "mergeable": bool(changed_fields) and not conflict_fields,
    }


def draft_key(draft_record: dict[str, Any]) -> tuple[str, str]:
    return draft_record["sha1"], draft_record["userId"]


def changed_field_values(draft_record: dict[str, Any]) -> dict[str, str]:
    return {
        field: str(draft_record["draft"].get(field, ""))
        for field in EDITABLE_FIELDS
        if str(draft_record["draft"].get(field, "")) != str(draft_record["base"].get(field, ""))
    }


def draft_queue_conflicts(draft_records: list[dict[str, Any]]) -> dict[tuple[str, str], set[str]]:
    grouped: dict[tuple[str, str], list[tuple[tuple[str, str], str]]] = defaultdict(list)
    for record in draft_records:
        key = draft_key(record)
        for field, value in changed_field_values(record).items():
            grouped[(record["sha1"], field)].append((key, value))

    conflicts: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (_sha1, field), values in grouped.items():
        if len({value for _key, value in values}) <= 1:
            continue
        for key, _value in values:
            conflicts[key].add(field)
    return dict(conflicts)


def write_csv_atomic(csv_path: Path, header: list[str], rows: list[list[str]]) -> None:
    temp_name = ""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=csv_path.parent, delete=False) as target:
        temp_name = target.name
        writer = csv.writer(target, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    os.replace(temp_name, csv_path)
    with csv_index_lock:
        csv_index_cache.pop(str(csv_path.resolve()), None)


def set_row_editable_values(row: list[str], header: list[str], columns: dict[str, str | None], values: dict[str, str]) -> None:
    ensure_row_width(row, header)
    assert columns["japanese"] is not None
    assert columns["korean"] is not None
    assert columns["dialogueLineCount"] is not None
    assert columns["dialogueLineLengths"] is not None
    row[header.index(columns["japanese"])] = values.get("japanese", "")
    row[header.index(columns["korean"])] = values.get("korean", "")
    line_lengths = values.get("dialogueLineLengths", "")
    row[header.index(columns["dialogueLineLengths"])] = line_lengths
    row[header.index(columns["dialogueLineCount"])] = str(len(parse_dialogue_line_lengths(line_lengths))) if line_lengths else ""


def verified_group_label(value: str) -> str:
    return value.strip() or "미분류"


def row_matches_group(row: list[str], header: list[str], group_col: str | None, selected_group: str) -> bool:
    if not selected_group:
        return True
    return verified_group_label(cell(row, header, group_col)) == selected_group


def normalized_texture_key(file_path: str, row_number: int) -> str:
    text = clean_text(file_path).replace("\\", "/")
    if not text:
        return f"__row__:{row_number}"
    parts = [part for part in text.split("/") if part and part != "."]
    return "/".join(parts).casefold()


def unique_texture_records(records: list[tuple[int, list[str]]], header: list[str], file_col: str | None) -> list[tuple[int, list[str]]]:
    seen: set[str] = set()
    unique: list[tuple[int, list[str]]] = []
    for row_number, row in records:
        key = normalized_texture_key(cell(row, header, file_col), row_number)
        if key in seen:
            continue
        seen.add(key)
        unique.append((row_number, row))
    return unique


def read_translation_page(
    folder: str,
    page: int,
    show_images: bool,
    group: str = "",
    user_id: str | None = None,
    show_translated_images: bool = False,
) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    page_size = CSV_PAGE_SIZE
    if user_id:
        try:
            with db_lock, connect_db() as conn:
                row = execute(
                    conn,
                    "SELECT value FROM ui_state WHERE user_id = ? AND `key` = ?",
                    (user_id, "translationPageSize"),
                ).fetchone()
                if row:
                    page_size = int(json.loads(row["value"]))
        except Exception as exc:
            report_db_error("Failed to load user translationPageSize from ui_state", exc)

    page = max(1, page)
    index = csv_record_index(csv_path)
    all_rows: list[list[str]] = index["rows"]
    rows: list[dict[str, Any]] = []
    header: list[str] = index["header"]
    sha1_col = require_sha1_column(header)
    columns = editable_columns(header)
    line_control_offset_col = find_column(header, DIALOGUE_LINE_CONTROL_OFFSET_COLUMNS)
    file_col = find_column(header, FILE_COLUMNS)
    group_col = find_column(header, ("verified_group",))
    width_col = find_column(header, ("width", "storage_width", "output_crop_width"))
    height_col = find_column(header, ("height", "storage_height", "output_crop_height"))
    selected_group = clean_text(group)
    user_drafts = load_user_drafts(csv_path, user_id)
    row_marks = load_translation_row_marks(csv_path)
    pending_draft_counts = row_marks["pendingDraftCounts"]
    merged_sha1s = row_marks["mergedSha1s"]
    duplicate_counts = Counter(normalized_texture_key(cell(row, header, file_col), row_number) for row_number, row in enumerate(all_rows, 1))
    filtered_records: list[tuple[int, list[str]]] = []
    for row_number, row in enumerate(all_rows, 1):
        if row_matches_group(row, header, group_col, selected_group):
            filtered_records.append((row_number, row))
    filtered_records = unique_texture_records(filtered_records, header, file_col)
    unique_all_records = unique_texture_records(list(enumerate(all_rows, 1)), header, file_col)
    group_counts = Counter()
    group_translated = Counter()
    korean_col = columns.get("korean")
    for _row_number, row in unique_all_records:
        group_label = verified_group_label(cell(row, header, group_col))
        group_counts[group_label] += 1
        korean_val = cell(row, header, korean_col).strip() if korean_col else ""
        if korean_val:
            group_translated[group_label] += 1

    groups = [
        {
            "value": label,
            "label": label,
            "count": count,
            "translatedCount": group_translated[label]
        }
        for label, count in sorted(
            group_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    total_rows = len(filtered_records)
    total_pages = max(1, (total_rows + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    start_index = (page - 1) * page_size
    end_index = min(total_rows, start_index + page_size)
    overflow_count = 0
    first_overflow_page = 0
    line_overflow_count = 0
    first_line_overflow_page = 0
    for group_index, (row_number, row) in enumerate(filtered_records, 1):
        group = cell(row, header, group_col).strip()
        if group not in LINE_LENGTH_TARGET_GROUPS:
            continue
        sha1 = clean_text(cell(row, header, sha1_col))
        main_values = editable_values(row, header, columns)
        display_values = user_drafts.get(sha1, {}).get("draft", main_values)
        if group in TEXT_CAPACITY_TARGET_GROUPS:
            file_path = cell(row, header, file_col)
            width_text = cell(row, header, width_col).strip()
            height_text = cell(row, header, height_col).strip()
            width = int(width_text) if width_text.isdigit() else 0
            height = int(height_text) if height_text.isdigit() else 0
            if width <= 0 or height <= 0:
                width, height = parse_texture_dimensions(file_path)
            capacity = group_text_capacity(width, height, group)
            korean_length = normalized_text_length(display_values["korean"], group)
            if capacity > 0 and korean_length > capacity:
                overflow_count += 1
                if not first_overflow_page:
                    first_overflow_page = (group_index - 1) // page_size + 1
        _line_limit, line_excess = line_overflow_amount(display_values["korean"], group)
        if line_excess > 0:
            line_overflow_count += 1
            if not first_line_overflow_page:
                first_line_overflow_page = (group_index - 1) // page_size + 1
    page_records = filtered_records[start_index:end_index] if total_rows else []
    for row_number, row in page_records:
        file_path = cell(row, header, file_col)
        verified_group = cell(row, header, group_col).strip()
        width_text = cell(row, header, width_col).strip()
        height_text = cell(row, header, height_col).strip()
        width = int(width_text) if width_text.isdigit() else 0
        height = int(height_text) if height_text.isdigit() else 0
        if width <= 0 or height <= 0:
            width, height = parse_texture_dimensions(file_path)
        sha1 = clean_text(cell(row, header, sha1_col))
        main_values = editable_values(row, header, columns)
        draft_record = user_drafts.get(sha1)
        display_values = draft_record["draft"] if draft_record else main_values
        korean = display_values["korean"]
        korean_length = normalized_text_length(korean, verified_group)
        capacity = group_text_capacity(width, height, verified_group) if verified_group in TEXT_CAPACITY_TARGET_GROUPS else 0
        line_limit, line_excess = line_overflow_amount(korean, verified_group)
        status = draft_status(draft_record["base"], main_values, draft_record["draft"]) if draft_record else {}
        dialogue_line_lengths = display_values["dialogueLineLengths"]
        dialogue_line_count = (
            str(len(parse_dialogue_line_lengths(dialogue_line_lengths)))
            if draft_record and dialogue_line_lengths
            else cell(row, header, columns.get("dialogueLineCount"))
        )
        item = {
            "rowNumber": row_number,
            "filePath": file_path,
            "sha1": sha1,
            "duplicateCount": duplicate_counts.get(normalized_texture_key(file_path, row_number), 0),
            "japanese": display_values["japanese"],
            "korean": korean,
            "mainJapanese": main_values["japanese"],
            "mainKorean": main_values["korean"],
            "mainDialogueLineLengths": main_values["dialogueLineLengths"],
            "baseJapanese": draft_record["base"]["japanese"] if draft_record else main_values["japanese"],
            "baseKorean": draft_record["base"]["korean"] if draft_record else main_values["korean"],
            "baseDialogueLineLengths": draft_record["base"]["dialogueLineLengths"] if draft_record else main_values["dialogueLineLengths"],
            "verifiedGroup": verified_group,
            "dialogueLineControlOffset": cell(row, header, line_control_offset_col),
            "dialogueLineCount": dialogue_line_count,
            "dialogueLineLengths": dialogue_line_lengths,
            "hasDraft": bool(draft_record),
            "draftUpdatedAt": draft_record["updatedAt"] if draft_record else None,
            "draftChangedFields": status.get("changedFields", []),
            "draftConflictFields": status.get("conflictFields", []),
            "draftHasConflict": status.get("hasConflict", False),
            "draftAlreadyApplied": status.get("alreadyApplied", False),
            "pendingDraftCount": pending_draft_counts.get(sha1, 0),
            "mergedModified": sha1 in merged_sha1s,
            "isModified": pending_draft_counts.get(sha1, 0) > 0 or sha1 in merged_sha1s,
            "textureWidth": width,
            "textureHeight": height,
            "textCapacity": capacity,
            "koreanTextLength": korean_length,
            "textOverflow": capacity > 0 and korean_length > capacity,
            "lineLimit": line_limit,
            "lineOverflow": line_excess > 0,
        }
        if file_path:
            if show_images:
                item["imageUrl"] = translation_image_url(folder, file_path)
            if show_translated_images:
                item["koreanImageUrl"] = translated_texture_image_url(file_path)
        rows.append(item)
    return {
        "csvPath": root_relative(csv_path),
        "page": page,
        "pageSize": page_size,
        "totalRows": total_rows,
        "allRows": len(unique_all_records),
        "totalPages": total_pages,
        "rows": rows if page <= total_pages else [],
        "selectedGroup": selected_group,
        "groups": groups,
        "draftCount": len(user_drafts),
        "overflowCount": overflow_count,
        "firstOverflowPage": first_overflow_page,
        "lineOverflowCount": line_overflow_count,
        "firstLineOverflowPage": first_line_overflow_page,
    }


def search_translation(folder: str, query: str, group: str = "", user_id: str | None = None) -> dict[str, Any]:
    text = clean_text(query)
    if not text:
        return {"found": False, "error": "검색어를 입력하세요."}
    csv_path = find_translation_csv(folder)
    page_size = CSV_PAGE_SIZE
    if user_id:
        try:
            with db_lock, connect_db() as conn:
                row = execute(
                    conn,
                    "SELECT value FROM ui_state WHERE user_id = ? AND `key` = ?",
                    (user_id, "translationPageSize"),
                ).fetchone()
                if row:
                    page_size = int(json.loads(row["value"]))
        except Exception as exc:
            report_db_error("Failed to load user translationPageSize from ui_state", exc)

    index = csv_record_index(csv_path)
    header: list[str] = index["header"]
    sha1_col = require_sha1_column(header)
    columns = editable_columns(header)
    group_col = find_column(header, ("verified_group",))
    selected_group = clean_text(group)
    if columns["japanese"] is None and columns["korean"] is None:
        return {"found": False, "error": "일본어/한국어 컬럼이 없습니다."}
    user_drafts = load_user_drafts(csv_path, user_id)
    file_col = find_column(header, FILE_COLUMNS)
    filtered_records = unique_texture_records(
        [
            (row_number, row)
            for row_number, row in enumerate(index["rows"], 1)
            if row_matches_group(row, header, group_col, selected_group)
        ],
        header,
        file_col,
    )
    for matched_index, (row_number, row) in enumerate(filtered_records, 1):
        sha1 = clean_text(cell(row, header, sha1_col))
        values = user_drafts.get(sha1, {}).get("draft", editable_values(row, header, columns))
        haystack = "\n".join((values["japanese"], values["korean"]))
        if text in haystack:
            return {"found": True, "rowNumber": row_number, "page": (matched_index - 1) // page_size + 1}
    return {"found": False}


def clamp_page_size(value: Any, default: int = CSV_PAGE_SIZE, maximum: int = 500) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = default
    return max(1, min(maximum, size))


def normalize_bulk_groups(groups: Any) -> list[str]:
    if isinstance(groups, str):
        raw_groups = groups.split(",")
    elif isinstance(groups, (list, tuple, set)):
        raw_groups = groups
    else:
        raw_groups = []
    selected: list[str] = []
    for group in raw_groups:
        label = clean_text(group)
        if label and label not in selected:
            selected.append(label)
    return selected


def bulk_group_options_from_rows(rows: list[list[str]], header: list[str], group_col: str | None, korean_col: str | None) -> list[dict[str, Any]]:
    group_counts = Counter()
    group_translated = Counter()
    for row in rows:
        group_label = verified_group_label(cell(row, header, group_col))
        group_counts[group_label] += 1
        if korean_col and cell(row, header, korean_col).strip():
            group_translated[group_label] += 1
    return [
        {
            "value": label,
            "label": label,
            "count": count,
            "translatedCount": group_translated[label],
        }
        for label, count in sorted(group_counts.items(), key=lambda item: (item[0]))
    ]


def bulk_translation_options(folder: str) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    index = csv_record_index(csv_path)
    header: list[str] = index["header"]
    columns = editable_columns(header)
    group_col = find_column(header, ("verified_group",))
    return {
        "csvPath": root_relative(csv_path),
        "totalRows": len(index["rows"]),
        "groups": bulk_group_options_from_rows(index["rows"], header, group_col, columns.get("korean")),
    }


def decode_bulk_search_text(value: str) -> str:
    return str(value or "").replace("\\n", "\n")


def bulk_translation_rows(
    folder: str,
    target_text: str,
    replacement_text: str = "",
    groups: Any = None,
) -> tuple[Path, list[str], list[list[str]], list[dict[str, Any]], list[dict[str, Any]]]:
    search_text = decode_bulk_search_text(target_text)
    replacement_value = decode_bulk_search_text(replacement_text)
    if search_text == "":
        raise ValueError("대상문자를 입력하세요.")
    csv_path = find_translation_csv(folder)
    index = csv_record_index(csv_path)
    header: list[str] = index["header"]
    rows: list[list[str]] = index["rows"]
    columns = editable_columns(header)
    korean_col = columns.get("korean")
    if korean_col is None:
        raise ValueError("korean 컬럼이 없습니다.")
    sha1_col = require_sha1_column(header)
    japanese_col = columns.get("japanese")
    group_col = find_column(header, ("verified_group",))
    file_col = find_column(header, FILE_COLUMNS)
    selected_groups = set(normalize_bulk_groups(groups))
    matches: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, 1):
        group_label = verified_group_label(cell(row, header, group_col))
        if selected_groups and group_label not in selected_groups:
            continue
        korean = cell(row, header, korean_col)
        if search_text not in korean:
            continue
        matches.append({
            "rowNumber": row_number,
            "sha1": clean_text(cell(row, header, sha1_col)),
            "verifiedGroup": group_label,
            "filePath": cell(row, header, file_col),
            "japanese": cell(row, header, japanese_col),
            "korean": korean,
            "before": korean,
            "after": korean.replace(search_text, replacement_value),
        })
    return csv_path, header, rows, matches, bulk_group_options_from_rows(rows, header, group_col, korean_col)


def paginate_bulk_rows(rows: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = clamp_page_size(page_size)
    total_rows = len(rows)
    total_pages = max(1, (total_rows + page_size - 1) // page_size)
    page = min(page, total_pages)
    start_index = (page - 1) * page_size
    return {
        "page": page,
        "pageSize": page_size,
        "totalRows": total_rows,
        "totalPages": total_pages,
        "rows": rows[start_index:start_index + page_size],
    }


def bulk_translation_preview(
    folder: str,
    target_text: str,
    replacement_text: str = "",
    groups: Any = None,
    page: int = 1,
    page_size: int = CSV_PAGE_SIZE,
) -> dict[str, Any]:
    csv_path, _header, _rows, matches, group_options = bulk_translation_rows(folder, target_text, replacement_text, groups)
    return {
        "csvPath": root_relative(csv_path),
        "targetText": target_text,
        "replacementText": replacement_text,
        "selectedGroups": normalize_bulk_groups(groups),
        "groups": group_options,
        **paginate_bulk_rows(matches, page, page_size),
    }


def bulk_translation_search(
    folder: str,
    query: str,
    groups: Any = None,
    page: int = 1,
    page_size: int = CSV_PAGE_SIZE,
) -> dict[str, Any]:
    return bulk_translation_preview(folder, query, "", groups, page, page_size)


def bulk_request_from_row(row: Any, include_snapshot: bool = False) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "csvPath": row["csv_path"],
        "targetText": row["target_text"],
        "replacementText": row["replacement_text"],
        "groups": json.loads(row["groups_json"] or "[]"),
        "submittedUserId": row["submitted_user_id"],
        "submittedUsername": row["submitted_username"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "approvedAt": row["approved_at"],
        "approvedUserId": row["approved_user_id"],
        "approvedUsername": row["approved_username"],
    }
    if include_snapshot:
        parsed = json.loads(row["snapshot_json"] or "[]")
        item["snapshot"] = parsed if isinstance(parsed, list) else []
    return item


def submit_bulk_translation_request(folder: str, target_text: str, replacement_text: str, groups: Any, user: dict[str, Any]) -> dict[str, Any]:
    csv_path, _header, _rows, matches, _group_options = bulk_translation_rows(folder, target_text, replacement_text, groups)
    if not matches:
        raise ValueError("제출할 치환 대상이 없습니다.")
    snapshot = [
        {
            "rowNumber": item["rowNumber"],
            "sha1": item["sha1"],
            "verifiedGroup": item["verifiedGroup"],
            "filePath": item["filePath"],
            "japanese": item["japanese"],
            "korean": item["korean"],
        }
        for item in matches
    ]
    request_id = uuid.uuid4().hex
    timestamp = now_ts()
    with db_lock, connect_db() as conn:
        execute(
            conn,
            """
            INSERT INTO bulk_translation_requests
                (id, csv_path, target_text, replacement_text, groups_json, snapshot_json,
                 submitted_user_id, submitted_username, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                csv_identity(csv_path),
                target_text,
                replacement_text,
                json.dumps(normalize_bulk_groups(groups), ensure_ascii=False, separators=(",", ":")),
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                user["id"],
                user["username"],
                "pending",
                timestamp,
            ),
        )
    return {
        "ok": True,
        "id": request_id,
        "csvPath": root_relative(csv_path),
        "matchedRows": len(matches),
        "createdAt": timestamp,
    }


def list_bulk_translation_requests(folder: str, user: dict[str, Any]) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    params: tuple[Any, ...]
    if user.get("role") == ROLE_ADMIN:
        where_sql = "csv_path = ? AND status = 'pending'"
        params = (csv_identity(csv_path),)
    else:
        where_sql = "csv_path = ? AND status = 'pending' AND submitted_user_id = ?"
        params = (csv_identity(csv_path), user["id"])
    with db_lock, connect_db() as conn:
        rows = execute(
            conn,
            f"""
            SELECT *
            FROM bulk_translation_requests
            WHERE {where_sql}
            ORDER BY created_at ASC
            """,
            params,
        ).fetchall()
    requests = [bulk_request_from_row(row) for row in rows]
    return {
        "csvPath": root_relative(csv_path),
        "total": len(requests),
        "requests": requests,
    }


def delete_bulk_translation_request(request_id: str, user: dict[str, Any]) -> dict[str, Any]:
    with db_lock, connect_db() as conn:
        row = execute(conn, "SELECT * FROM bulk_translation_requests WHERE id = ? AND status = 'pending'", (request_id,)).fetchone()
        if row is None:
            raise ValueError("대기 중인 일괄수정 요청을 찾을 수 없습니다.")
        cursor = execute(conn, "DELETE FROM bulk_translation_requests WHERE id = ? AND status = 'pending'", (request_id,))
    return {
        "ok": True,
        "deleted": int(getattr(cursor, "rowcount", 0) or 0),
        "id": request_id,
    }


def load_bulk_translation_request(request_id: str) -> dict[str, Any]:
    with db_lock, connect_db() as conn:
        row = execute(conn, "SELECT * FROM bulk_translation_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise ValueError("일괄수정 요청을 찾을 수 없습니다.")
    return bulk_request_from_row(row, include_snapshot=True)


def bulk_request_validation_rows(request_record: dict[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
    csv_path = resolve_project_path(request_record["csvPath"], "textures_static")
    index = csv_record_index(csv_path)
    header: list[str] = index["header"]
    rows: list[list[str]] = index["rows"]
    columns = editable_columns(header)
    korean_col = columns.get("korean")
    if korean_col is None:
        raise ValueError("korean 컬럼이 없습니다.")
    sha1_col = require_sha1_column(header)
    target_text = decode_bulk_search_text(request_record["targetText"])
    replacement_text = decode_bulk_search_text(request_record["replacementText"])
    result_rows: list[dict[str, Any]] = []
    for snapshot in request_record.get("snapshot", []):
        row_number = int(snapshot.get("rowNumber") or 0)
        current_korean = ""
        current_sha1 = ""
        conflict_reason = ""
        if row_number <= 0 or row_number > len(rows):
            conflict_reason = "값이 변경됨"
        else:
            row = rows[row_number - 1]
            current_korean = cell(row, header, korean_col)
            current_sha1 = clean_text(cell(row, header, sha1_col))
            if current_sha1 != clean_text(snapshot.get("sha1")) or current_korean != str(snapshot.get("korean", "")):
                conflict_reason = "값이 변경됨"
            elif target_text not in current_korean:
                conflict_reason = "문자 사라짐"
        original = str(snapshot.get("korean", ""))
        result_rows.append({
            "rowNumber": row_number,
            "sha1": clean_text(snapshot.get("sha1")),
            "verifiedGroup": snapshot.get("verifiedGroup", ""),
            "filePath": snapshot.get("filePath", ""),
            "japanese": snapshot.get("japanese", ""),
            "korean": original,
            "before": original,
            "after": original.replace(target_text, replacement_text),
            "currentKorean": current_korean,
            "conflictReason": conflict_reason,
            "hasConflict": bool(conflict_reason),
        })
    return csv_path, result_rows


def bulk_translation_request_detail(request_id: str, page: int = 1, page_size: int = CSV_PAGE_SIZE) -> dict[str, Any]:
    request_record = load_bulk_translation_request(request_id)
    _csv_path, rows = bulk_request_validation_rows(request_record)
    conflicts = [row for row in rows if row["hasConflict"]]
    changed = [row for row in conflicts if row["conflictReason"] == "값이 변경됨"]
    missing = [row for row in conflicts if row["conflictReason"] == "문자 사라짐"]
    return {
        **request_record,
        "conflictRows": len(conflicts),
        "changedRows": len(changed),
        "missingTargetRows": len(missing),
        "canApprove": request_record["status"] == "pending" and not conflicts,
        **paginate_bulk_rows(rows, page, page_size),
    }


def approve_bulk_translation_request(request_id: str, user: dict[str, Any]) -> dict[str, Any]:
    request_record = load_bulk_translation_request(request_id)
    if request_record["status"] != "pending":
        raise ValueError("이미 승인된 요청입니다.")
    with csv_write_lock:
        csv_path = resolve_project_path(request_record["csvPath"], "textures_static")
        csv.field_size_limit(sys.maxsize)
        with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source)
            header = next(reader, [])
            rows = list(reader)
        columns = editable_columns(header, ensure=True)
        korean_col = columns.get("korean")
        sha1_col = require_sha1_column(header)
        assert korean_col is not None
        conflicts: list[dict[str, Any]] = []
        target_text = decode_bulk_search_text(request_record["targetText"])
        replacement_text = decode_bulk_search_text(request_record["replacementText"])
        changed_row_numbers: list[int] = []
        merged_sha1s: set[str] = set()
        for snapshot in request_record.get("snapshot", []):
            row_number = int(snapshot.get("rowNumber") or 0)
            conflict_reason = ""
            current_korean = ""
            if row_number <= 0 or row_number > len(rows):
                conflict_reason = "값이 변경됨"
            else:
                row = rows[row_number - 1]
                ensure_row_width(row, header)
                current_korean = cell(row, header, korean_col)
                current_sha1 = clean_text(cell(row, header, sha1_col))
                if current_sha1 != clean_text(snapshot.get("sha1")) or current_korean != str(snapshot.get("korean", "")):
                    conflict_reason = "값이 변경됨"
                elif target_text not in current_korean:
                    conflict_reason = "문자 사라짐"
            if conflict_reason:
                conflicts.append({
                    "rowNumber": row_number,
                    "sha1": clean_text(snapshot.get("sha1")),
                    "conflictReason": conflict_reason,
                    "currentKorean": current_korean,
                })
        if conflicts:
            return {
                "ok": False,
                "approved": False,
                "conflicts": conflicts,
                "conflictRows": len(conflicts),
            }
        for snapshot in request_record.get("snapshot", []):
            row_number = int(snapshot.get("rowNumber") or 0)
            row = rows[row_number - 1]
            row[header.index(korean_col)] = cell(row, header, korean_col).replace(target_text, replacement_text)
            changed_row_numbers.append(row_number)
            sha1 = clean_text(snapshot.get("sha1"))
            if sha1:
                merged_sha1s.add(sha1)
        if changed_row_numbers:
            write_csv_atomic(csv_path, header, rows)
        with db_lock, connect_db() as conn:
            mark_translation_rows_merged(conn, csv_path, merged_sha1s)
            execute(
                conn,
                """
                UPDATE bulk_translation_requests
                SET status = 'approved',
                    approved_at = ?,
                    approved_user_id = ?,
                    approved_username = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now_ts(), user["id"], user["username"], request_id),
            )
    return {
        "ok": True,
        "approved": True,
        "changedRows": len(changed_row_numbers),
        "csvPath": root_relative(csv_path),
    }


def save_translation_changes(folder: str, changes: list[dict[str, Any]], user: dict[str, Any]) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    index = csv_record_index(csv_path)
    header: list[str] = index["header"]
    rows: list[list[str]] = index["rows"]
    sha1_col = require_sha1_column(header)
    columns = editable_columns(header)
    group_col = find_column(header, ("verified_group",))
    rows_by_sha1 = manifest_rows_by_sha1(rows, header, sha1_col)
    by_row_number = {row_number: clean_text(cell(row, header, sha1_col)) for row_number, row in enumerate(rows, 1)}
    indexed: dict[str, dict[str, Any]] = {}
    for item in changes:
        sha1 = clean_text(item.get("sha1"))
        if not sha1:
            try:
                sha1 = by_row_number.get(int(item.get("rowNumber")), "")
            except (TypeError, ValueError):
                sha1 = ""
        if sha1 and sha1 in rows_by_sha1:
            indexed[sha1] = item
    if not indexed:
        return {"ok": True, "saved": 0, "savedDrafts": 0, "csvPath": root_relative(csv_path), "mode": "draft"}

    existing_drafts = load_user_drafts(csv_path, user["id"])
    timestamp = now_ts()
    saved_drafts = 0
    skipped_unchanged_korean = 0
    with db_lock, connect_db() as conn:
        for sha1, item in indexed.items():
            _row_number, row = rows_by_sha1[sha1][0]
            current_values = editable_values(row, header, columns)
            base_values = existing_drafts[sha1]["base"] if sha1 in existing_drafts else current_values
            group = cell(row, header, group_col).strip()
            draft_values = normalize_editable_payload(item, current_values, group)
            previous_korean = str(item["previousKorean"]) if "previousKorean" in item else str(base_values.get("korean", ""))
            if str(draft_values.get("korean", "")) == previous_korean:
                skipped_unchanged_korean += 1
                continue
            params = (
                csv_identity(csv_path),
                sha1,
                user["id"],
                json_dumps(base_values),
                json_dumps(draft_values),
                timestamp,
                timestamp,
            )
            if DB_BACKEND in {"mysql", "mysql+pymysql"}:
                execute(
                    conn,
                    """
                    INSERT INTO translation_drafts
                        (csv_path, sha1, user_id, base_json, draft_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        draft_json = VALUES(draft_json),
                        updated_at = VALUES(updated_at)
                    """,
                    params,
                )
            else:
                execute(
                    conn,
                    """
                    INSERT INTO translation_drafts
                        (csv_path, sha1, user_id, base_json, draft_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(csv_path, sha1, user_id) DO UPDATE SET
                        draft_json = excluded.draft_json,
                        updated_at = excluded.updated_at
                    """,
                    params,
                )
            saved_drafts += 1
    return {
        "ok": True,
        "saved": saved_drafts,
        "savedDrafts": saved_drafts,
        "skippedUnchangedKorean": skipped_unchanged_korean,
        "csvPath": root_relative(csv_path),
        "mode": "draft",
    }


def draft_preview(
    draft_record: dict[str, Any],
    queue_conflict_fields: set[str],
    folder: str,
    rows_by_sha1: dict[str, list[tuple[int, list[str]]]],
    header: list[str],
    columns: dict[str, str | None],
    file_col: str | None,
    group_col: str | None,
    width_col: str | None,
    height_col: str | None,
) -> dict[str, Any]:
    sha1 = draft_record["sha1"]
    row_entries = rows_by_sha1.get(sha1, [])
    missing_main = not row_entries
    row_number = row_entries[0][0] if row_entries else 0
    row = row_entries[0][1] if row_entries else []
    current = editable_values(row, header, columns) if row_entries else {"japanese": "", "korean": "", "dialogueLineLengths": ""}
    status = draft_status(draft_record["base"], current, draft_record["draft"]) if row_entries else {
        "changedFields": [],
        "hasChanges": False,
        "conflictFields": ["sha1"],
        "hasConflict": True,
        "alreadyApplied": False,
        "mergeable": False,
    }
    if queue_conflict_fields:
        conflict_fields = sorted(set(status["conflictFields"]) | queue_conflict_fields)
        status = {
            **status,
            "conflictFields": conflict_fields,
            "queueConflictFields": sorted(queue_conflict_fields),
            "hasConflict": True,
            "mergeable": False,
        }
    else:
        status = {**status, "queueConflictFields": []}
    file_path = cell(row, header, file_col) if row_entries else ""
    verified_group = cell(row, header, group_col).strip() if row_entries else ""
    width_text = cell(row, header, width_col).strip() if row_entries else ""
    height_text = cell(row, header, height_col).strip() if row_entries else ""
    width = int(width_text) if width_text.isdigit() else 0
    height = int(height_text) if height_text.isdigit() else 0
    if (width <= 0 or height <= 0) and file_path:
        width, height = parse_texture_dimensions(file_path)
    capacity = group_text_capacity(width, height, verified_group) if verified_group in TEXT_CAPACITY_TARGET_GROUPS else 0
    return {
        **draft_record,
        "rowNumber": row_number,
        "verifiedGroup": verified_group,
        "duplicateCount": len(row_entries),
        "imageUrl": translation_image_url(folder, file_path) if file_path else "",
        "current": current,
        "missingMain": missing_main,
        "textureWidth": width,
        "textureHeight": height,
        "textCapacity": capacity,
        **status,
    }


def list_translation_drafts(folder: str) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    index = csv_record_index(csv_path)
    header: list[str] = index["header"]
    sha1_col = require_sha1_column(header)
    columns = editable_columns(header)
    rows_by_sha1 = manifest_rows_by_sha1(index["rows"], header, sha1_col)
    file_col = find_column(header, FILE_COLUMNS)
    group_col = find_column(header, ("verified_group",))
    width_col = find_column(header, ("width", "storage_width", "output_crop_width"))
    height_col = find_column(header, ("height", "storage_height", "output_crop_height"))
    with db_lock, connect_db() as conn:
        draft_rows = execute(
            conn,
            """
            SELECT translation_drafts.*, users.username
            FROM translation_drafts
            JOIN users ON users.id = translation_drafts.user_id
            WHERE csv_path = ?
            ORDER BY updated_at DESC
            """,
            (csv_identity(csv_path),),
        ).fetchall()
    draft_records = [draft_record_from_row(row) for row in draft_rows]
    queue_conflicts = draft_queue_conflicts(draft_records)
    drafts = [
        draft_preview(record, queue_conflicts.get(draft_key(record), set()), folder, rows_by_sha1, header, columns, file_col, group_col, width_col, height_col)
        for record in draft_records
    ]
    missing_row_sort = len(index["rows"]) + 1
    drafts.sort(key=lambda item: item["rowNumber"] if item["rowNumber"] > 0 else missing_row_sort)
    return {
        "csvPath": root_relative(csv_path),
        "total": len(drafts),
        "mergeable": sum(1 for item in drafts if item["mergeable"]),
        "conflicts": sum(1 for item in drafts if item["hasConflict"]),
        "alreadyApplied": sum(1 for item in drafts if item["alreadyApplied"]),
        "drafts": drafts,
    }


def selected_draft_keys(items: list[dict[str, Any]]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for item in items:
        sha1 = clean_text(item.get("sha1"))
        user_id = clean_text(item.get("userId"))
        if sha1 and user_id:
            keys.add((sha1, user_id))
    return keys


def selected_draft_items(items: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        sha1 = clean_text(item.get("sha1"))
        user_id = clean_text(item.get("userId"))
        if sha1 and user_id:
            indexed[(sha1, user_id)] = item
    return indexed


def selected_item_note(selected_items: dict[tuple[str, str], dict[str, Any]], key: tuple[str, str], fallback: str = "") -> str:
    item = selected_items.get(key)
    if not item:
        return clean_text(fallback)
    return clean_text(item.get("note"), clean_text(fallback))


def draft_values_for_apply(record: dict[str, Any], selected_item: dict[str, Any] | None, group: str) -> dict[str, str]:
    override = selected_item.get("draft") if selected_item else None
    if not isinstance(override, dict):
        return record["draft"]
    payload = dict(record["draft"])
    for field in EDITABLE_FIELDS:
        if field in override:
            payload[field] = str(override.get(field, ""))
    return normalize_editable_payload(payload, record["draft"], group)


def apply_translation_drafts(
    folder: str,
    items: list[dict[str, Any]],
    *,
    force_conflicts: bool = False,
    user: dict[str, Any],
    note: str = "",
) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    selected = selected_draft_keys(items)
    selected_items = selected_draft_items(items)
    applied_keys: list[tuple[str, str]] = []
    conflict_items: list[dict[str, Any]] = []
    merged_sha1s: set[str] = set()
    skipped = 0
    changed_rows = 0
    with csv_write_lock:
        csv.field_size_limit(sys.maxsize)
        with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source)
            header = next(reader, [])
            rows = list(reader)
        sha1_col = require_sha1_column(header)
        columns = editable_columns(header, ensure=True)
        file_col = find_column(header, FILE_COLUMNS)
        group_col = find_column(header, ("verified_group",))
        for row in rows:
            ensure_row_width(row, header)
        rows_by_sha1 = manifest_rows_by_sha1(rows, header, sha1_col)
        with db_lock, connect_db() as conn:
            draft_rows = execute(
                conn,
                """
                SELECT translation_drafts.*, users.username
                FROM translation_drafts
                JOIN users ON users.id = translation_drafts.user_id
                WHERE csv_path = ?
                ORDER BY updated_at ASC
                """,
                (csv_identity(csv_path),),
            ).fetchall()
        draft_records = [draft_record_from_row(row) for row in draft_rows]
        queue_conflicts = draft_queue_conflicts(draft_records)
        for record in draft_records:
            key = draft_key(record)
            if selected and key not in selected:
                continue
            row_entries = rows_by_sha1.get(record["sha1"], [])
            if not row_entries:
                skipped += 1
                conflict_items.append({"sha1": record["sha1"], "userId": record["userId"], "username": record["username"], "conflictFields": ["sha1"]})
                continue
            queue_conflict_fields = queue_conflicts.get(key, set())
            if queue_conflict_fields and not force_conflicts:
                skipped += 1
                conflict_items.append({
                    "sha1": record["sha1"],
                    "userId": record["userId"],
                    "username": record["username"],
                    "conflictFields": sorted(queue_conflict_fields),
                    "queueConflictFields": sorted(queue_conflict_fields),
                })
                continue
            current = editable_values(row_entries[0][1], header, columns)
            group = cell(row_entries[0][1], header, group_col).strip()
            draft_values = draft_values_for_apply(record, selected_items.get(key), group)
            merged, changed_fields, conflict_fields = merge_editable_values(
                record["base"],
                current,
                draft_values,
                force_conflicts=force_conflicts,
            )
            if conflict_fields and not force_conflicts:
                skipped += 1
                conflict_items.append({
                    "sha1": record["sha1"],
                    "userId": record["userId"],
                    "username": record["username"],
                    "conflictFields": conflict_fields,
                })
                continue
            if changed_fields:
                for _row_number, target_row in row_entries:
                    set_row_editable_values(target_row, header, columns, merged)
                changed_rows += len(row_entries)
                merged_sha1s.add(record["sha1"])
            applied_keys.append(key)
        if applied_keys and changed_rows:
            write_csv_atomic(csv_path, header, rows)
        if applied_keys:
            with db_lock, connect_db() as conn:
                mark_translation_rows_merged(conn, csv_path, merged_sha1s)
                timestamp = now_ts()
                for sha1, user_id in applied_keys:
                    matching_record = next((record for record in draft_records if record["sha1"] == sha1 and record["userId"] == user_id), None)
                    if matching_record:
                        row_entries = rows_by_sha1.get(sha1, [])
                        group = cell(row_entries[0][1], header, group_col).strip() if row_entries else ""
                        applied_values = draft_values_for_apply(matching_record, selected_items.get((sha1, user_id)), group)
                        insert_translation_merge_log(
                            conn,
                            csv_path,
                            matching_record,
                            action="merged",
                            merged=True,
                            executor=user,
                            note=selected_item_note(selected_items, (sha1, user_id), note),
                            applied=applied_values,
                            executed_at=timestamp,
                        )
                    execute(
                        conn,
                        "DELETE FROM translation_drafts WHERE csv_path = ? AND sha1 = ? AND user_id = ?",
                        (csv_identity(csv_path), sha1, user_id),
                    )
    return {
        "ok": True,
        "csvPath": root_relative(csv_path),
        "applied": len(applied_keys),
        "changedRows": changed_rows,
        "skipped": skipped,
        "conflicts": conflict_items,
        "forceConflicts": force_conflicts,
    }


def discard_translation_drafts(folder: str, items: list[dict[str, Any]], user: dict[str, Any], note: str = "") -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    allow_any_user = user.get("role") == ROLE_ADMIN
    deleted = 0
    selected_items = selected_draft_items(items)
    with db_lock, connect_db() as conn:
        for item in items:
            sha1 = clean_text(item.get("sha1"))
            if not sha1:
                continue
            user_id = clean_text(item.get("userId")) if allow_any_user else user["id"]
            if not user_id:
                user_id = user["id"]
            draft_row = execute(
                conn,
                """
                SELECT translation_drafts.*, users.username
                FROM translation_drafts
                JOIN users ON users.id = translation_drafts.user_id
                WHERE csv_path = ? AND sha1 = ? AND user_id = ?
                """,
                (csv_identity(csv_path), sha1, user_id),
            ).fetchone()
            cursor = execute(
                conn,
                "DELETE FROM translation_drafts WHERE csv_path = ? AND sha1 = ? AND user_id = ?",
                (csv_identity(csv_path), sha1, user_id),
            )
            row_count = int(getattr(cursor, "rowcount", 0) or 0)
            deleted += row_count
            if row_count and draft_row is not None and allow_any_user and clean_text(item.get("userId")):
                insert_translation_merge_log(
                    conn,
                    csv_path,
                    draft_record_from_row(draft_row),
                    action="deleted",
                    merged=False,
                    executor=user,
                    note=selected_item_note(selected_items, (sha1, user_id), note),
                    applied={},
                )
    return {"ok": True, "deleted": deleted, "csvPath": root_relative(csv_path)}


def notification_record_from_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "userId": row["user_id"],
        "createdBy": row["created_by"],
        "createdAt": row["created_at"],
        "mergedCount": int(row["merged_count"] or 0),
        "deletedCount": int(row["deleted_count"] or 0),
    }


def list_translation_notifications(user: dict[str, Any]) -> dict[str, Any]:
    with db_lock, connect_db() as conn:
        rows = execute(
            conn,
            """
            SELECT *
            FROM translation_notifications
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user["id"],),
        ).fetchall()
    return {"notifications": [notification_record_from_row(row) for row in rows]}


def get_translation_notification(notification_id: str, user: dict[str, Any]) -> dict[str, Any]:
    with db_lock, connect_db() as conn:
        notification = execute(
            conn,
            "SELECT * FROM translation_notifications WHERE id = ?",
            (notification_id,),
        ).fetchone()
        if notification is None:
            raise ValueError("알림을 찾을 수 없습니다.")
        if notification["user_id"] != user["id"] and user.get("role") != ROLE_ADMIN:
            raise ValueError("알림을 볼 권한이 없습니다.")
        rows = execute(
            conn,
            """
            SELECT translation_merge_logs.*
            FROM translation_notification_items
            JOIN translation_merge_logs ON translation_merge_logs.id = translation_notification_items.log_id
            WHERE translation_notification_items.notification_id = ?
            ORDER BY translation_merge_logs.executed_at ASC, translation_merge_logs.id ASC
            """,
            (notification_id,),
        ).fetchall()
    return {
        "notification": notification_record_from_row(notification),
        "items": [translation_log_from_row(row) for row in rows],
    }


def delete_translation_notification(notification_id: str, user: dict[str, Any]) -> dict[str, Any]:
    with db_lock, connect_db() as conn:
        notification = execute(
            conn,
            "SELECT * FROM translation_notifications WHERE id = ?",
            (notification_id,),
        ).fetchone()
        if notification is None:
            raise ValueError("알림을 찾을 수 없습니다.")
        if notification["user_id"] != user["id"] and user.get("role") != ROLE_ADMIN:
            raise ValueError("알림을 삭제할 권한이 없습니다.")
        cursor = execute(
            conn,
            "DELETE FROM translation_notifications WHERE id = ?",
            (notification_id,),
        )
    return {"ok": True, "deleted": int(getattr(cursor, "rowcount", 0) or 0)}


def send_translation_notifications(user: dict[str, Any]) -> dict[str, Any]:
    timestamp = now_ts()
    notification_count = 0
    item_count = 0
    recipients: list[dict[str, Any]] = []
    with db_lock, connect_db() as conn:
        rows = execute(
            conn,
            """
            SELECT *
            FROM translation_merge_logs
            WHERE notified_at IS NULL
            ORDER BY executed_at ASC, id ASC
            """,
        ).fetchall()
        grouped: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            grouped[row["submitted_user_id"]].append(row)
        for user_id, user_rows in grouped.items():
            merged_count = sum(1 for row in user_rows if row["action"] == "merged")
            deleted_count = sum(1 for row in user_rows if row["action"] == "deleted")
            if merged_count + deleted_count <= 0:
                continue
            notification_id = uuid.uuid4().hex
            execute(
                conn,
                """
                INSERT INTO translation_notifications (id, user_id, created_by, created_at, merged_count, deleted_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (notification_id, user_id, user["id"], timestamp, merged_count, deleted_count),
            )
            for row in user_rows:
                execute(
                    conn,
                    "INSERT INTO translation_notification_items (notification_id, log_id) VALUES (?, ?)",
                    (notification_id, row["id"]),
                )
                execute(
                    conn,
                    "UPDATE translation_merge_logs SET notified_at = ? WHERE id = ?",
                    (timestamp, row["id"]),
                )
            notification_count += 1
            item_count += len(user_rows)
            recipients.append({
                "userId": user_id,
                "username": user_rows[0]["submitted_username"],
                "mergedCount": merged_count,
                "deletedCount": deleted_count,
            })
    return {
        "ok": True,
        "notifications": notification_count,
        "items": item_count,
        "recipients": recipients,
    }


def apply_korean_line_lengths(folder: str) -> dict[str, Any]:
    csv_path = find_translation_csv(folder)
    matched = 0
    changed = 0
    with csv_write_lock:
        csv.field_size_limit(sys.maxsize)
        with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source)
            header = next(reader, [])
            rows = list(reader)
        group_col = find_column(header, ("verified_group",))
        korean_col = find_column(header, KOREAN_COLUMNS)
        if group_col is None:
            raise ValueError("verified_group 컬럼이 없습니다.")
        if korean_col is None:
            raise ValueError("한국어 컬럼이 없습니다.")
        line_count_col = ensure_column(header, "dialogue_line_count")
        line_lengths_col = ensure_column(header, "dialogue_line_lengths")
        group_index = header.index(group_col)
        korean_index = header.index(korean_col)
        line_count_index = header.index(line_count_col)
        line_lengths_index = header.index(line_lengths_col)
        for row in rows:
            ensure_row_width(row, header)
            if row[group_index] in LINE_LENGTH_TARGET_GROUPS:
                matched += 1
                next_lengths = line_lengths_from_text(row[korean_index], row[group_index])
                next_count = str(len(parse_dialogue_line_lengths(next_lengths))) if next_lengths else ""
                if row[line_lengths_index] != next_lengths or row[line_count_index] != next_count:
                    changed += 1
                row[line_lengths_index] = next_lengths
                row[line_count_index] = next_count
        write_csv_atomic(csv_path, header, rows)
    return {"ok": True, "matched": matched, "changed": changed, "csvPath": root_relative(csv_path)}
