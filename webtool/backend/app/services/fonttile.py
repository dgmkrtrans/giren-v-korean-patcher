from __future__ import annotations

import csv
import json
import threading
import uuid
from pathlib import Path
from typing import Any

from ..core.config import ROLE_ADMIN, ROOT
from ..core.db import connect_db, db_lock, execute
from ..core.utils import now_ts, root_relative
from .translation import write_csv_atomic


DICTIONARY_PATH = ROOT / "results/fonttile_text_dictionary.csv"
FONTTILE_PAGE_SIZE = 50
fonttile_csv_lock = threading.Lock()
FONT_BYTES = set(range(0x20, 0x7F)) | set(range(0xA1, 0xE0))
DISPLAY_TEXT_BYTE_SEQUENCES = {
    '"-f"': b"\x3b",
    '"+f"': b"\x5c",
}
DISPLAY_SINGLE_BYTE_GLYPHS = {
    "Ⅱ",
    "改",
    "型",
    "Ⅲ",
    "ｖ",
    "ν",
    "α",
    "β",
    "三",
    "開",
    "発",
    "ｂ",
    "ｄ",
    "ｅ",
    "ｉ",
    "ｔ",
    "・",
    "ヲ",
    "ァ",
    "ィ",
    "w",
    "x",
    "y",
    "z",
}


def is_korean_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        0xAC00 <= codepoint <= 0xD7A3
        or 0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
        or 0xA960 <= codepoint <= 0xA97F
        or 0xD7B0 <= codepoint <= 0xD7FF
    )


def fonttile_byte_length(text: str) -> tuple[int, list[str]]:
    length = 0
    errors: list[str] = []
    cursor = 0
    while cursor < len(text):
        matched = False
        for marker, raw in DISPLAY_TEXT_BYTE_SEQUENCES.items():
            if text.startswith(marker, cursor):
                length += len(raw)
                cursor += len(marker)
                matched = True
                break
        if matched:
            continue

        char = text[cursor]
        if is_korean_char(char):
            length += 1
            cursor += 1
            continue
        if char in DISPLAY_SINGLE_BYTE_GLYPHS:
            length += 1
            cursor += 1
            continue
        try:
            raw = char.encode("cp932")
        except UnicodeEncodeError:
            errors.append(f"인코딩 불가 문자: {char}")
            length += 1
            cursor += 1
            continue
        bad = [byte for byte in raw if byte not in FONT_BYTES]
        if bad:
            rendered = " ".join(f"0x{byte:02x}" for byte in bad[:8])
            errors.append(f"작은 폰트 바이트 범위 밖: {char}({rendered})")
        length += len(raw)
        cursor += 1
    return length, errors


def dictionary_rows() -> tuple[list[str], list[dict[str, str]]]:
    if not DICTIONARY_PATH.exists():
        raise FileNotFoundError(root_relative(DICTIONARY_PATH))
    with DICTIONARY_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("dictionary CSV에 헤더가 없습니다.")
        rows = [{field: str(row.get(field, "")) for field in reader.fieldnames} for row in reader]
    if "min_max_bytes" not in reader.fieldnames:
        raise ValueError("dictionary CSV에 min_max_bytes 컬럼이 필요합니다.")
    return list(reader.fieldnames), rows


def parse_int(value: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def build_dictionary_item(row_number: int, row: dict[str, str]) -> dict[str, Any]:
    translation = str(row.get("translation", ""))
    byte_length, byte_errors = fonttile_byte_length(translation)
    min_max_bytes = parse_int(row.get("min_max_bytes", "0"))
    return {
        "rowNumber": row_number,
        "original": row.get("original", ""),
        "translation": translation,
        "maxMaxBytes": parse_int(row.get("max_max_bytes", "0")),
        "count": parse_int(row.get("count", "0")),
        "minMaxBytes": min_max_bytes,
        "samples": row.get("samples", ""),
        "byteLength": byte_length,
        "byteErrors": byte_errors,
        "byteOverflow": bool(translation) and min_max_bytes > 0 and byte_length > min_max_bytes,
    }


def read_fonttile_state() -> dict[str, Any]:
    header, raw_dictionary_rows = dictionary_rows()
    dictionary_items = [
        build_dictionary_item(index, row)
        for index, row in enumerate(raw_dictionary_rows, start=1)
    ]
    return {
        "paths": {
            "dictionary": root_relative(DICTIONARY_PATH),
        },
        "dictionary": {
            "header": header,
            "totalRows": len(dictionary_items),
            "rows": dictionary_items,
        },
    }


def save_fonttile_state(payload: dict[str, Any]) -> dict[str, Any]:
    dictionary_payload = payload.get("dictionaryRows", [])
    if not isinstance(dictionary_payload, list):
        raise ValueError("dictionaryRows는 배열이어야 합니다.")

    header, rows = dictionary_rows()
    translation_by_original = {
        str(item.get("original", "")): str(item.get("translation", ""))
        for item in dictionary_payload
        if isinstance(item, dict)
    }
    if "translation" not in header:
        header.append("translation")
    for row in rows:
        original = row.get("original", "")
        if original in translation_by_original:
            row["translation"] = translation_by_original[original]

    write_csv_atomic(DICTIONARY_PATH, header, [[row.get(column, "") for column in header] for row in rows])
    return read_fonttile_state()


def request_from_row(row: Any, include_snapshot: bool = False) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "type": row["request_type"],
        "targetText": row["target_text"],
        "replacementText": row["replacement_text"],
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


def find_dictionary_row(rows: list[dict[str, str]], row_number: int, original: str) -> dict[str, str] | None:
    if row_number > 0 and row_number <= len(rows):
        row = rows[row_number - 1]
        if row.get("original", "") == original:
            return row
    for row in rows:
        if row.get("original", "") == original:
            return row
    return None


def normalize_dictionary_snapshot(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise ValueError("changes는 배열이어야 합니다.")
    _header, rows = dictionary_rows()
    snapshot: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row_number = parse_int(str(item.get("rowNumber", "0")))
        original = str(item.get("original", ""))
        submitted_translation = str(item.get("translation", ""))
        row = find_dictionary_row(rows, row_number, original)
        if row is None:
            raise ValueError(f"dictionary 행을 찾을 수 없습니다: {row_number} {original}")
        current_translation = str(row.get("translation", ""))
        base_translation = str(item.get("baseTranslation", current_translation))
        if current_translation != base_translation:
            raise ValueError(f"dictionary {row_number}행이 이미 변경되었습니다. 다시 읽은 뒤 제출하세요.")
        if current_translation == submitted_translation:
            continue
        snapshot.append({
            "rowNumber": row_number,
            "original": original,
            "baseTranslation": base_translation,
            "submittedTranslation": submitted_translation,
            "count": parse_int(row.get("count", "0")),
            "minMaxBytes": parse_int(row.get("min_max_bytes", "0")),
            "maxMaxBytes": parse_int(row.get("max_max_bytes", "0")),
            "samples": row.get("samples", ""),
        })
    if not snapshot:
        raise ValueError("제출할 변경사항이 없습니다.")
    return snapshot


def insert_fonttile_request(request_type: str, snapshot: list[dict[str, Any]], user: dict[str, Any], target_text: str = "", replacement_text: str = "") -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    timestamp = now_ts()
    with db_lock, connect_db() as conn:
        execute(
            conn,
            """
            INSERT INTO fonttile_translation_requests
                (id, request_type, target_text, replacement_text, snapshot_json,
                 submitted_user_id, submitted_username, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                request_type,
                target_text,
                replacement_text,
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                user["id"],
                user["username"],
                "pending",
                timestamp,
            ),
        )
    return {"ok": True, "id": request_id, "submittedRows": len(snapshot), "createdAt": timestamp}


def submit_fonttile_dictionary_request(changes: Any, user: dict[str, Any]) -> dict[str, Any]:
    return insert_fonttile_request("dictionary", normalize_dictionary_snapshot(changes), user)


def list_fonttile_requests(request_type: str, user: dict[str, Any]) -> dict[str, Any]:
    if request_type not in {"dictionary", "bulk"}:
        raise ValueError("요청 종류가 올바르지 않습니다.")
    if user.get("role") == ROLE_ADMIN:
        where_sql = "request_type = ? AND status = 'pending'"
        params: tuple[Any, ...] = (request_type,)
    else:
        where_sql = "request_type = ? AND status = 'pending' AND submitted_user_id = ?"
        params = (request_type, user["id"])
    with db_lock, connect_db() as conn:
        rows = execute(
            conn,
            f"SELECT * FROM fonttile_translation_requests WHERE {where_sql} ORDER BY created_at ASC",
            params,
        ).fetchall()
    requests = [request_from_row(row) for row in rows]
    return {"total": len(requests), "requests": requests}


def load_fonttile_request(request_id: str, request_type: str | None = None) -> dict[str, Any]:
    sql = "SELECT * FROM fonttile_translation_requests WHERE id = ?"
    params: tuple[Any, ...] = (request_id,)
    if request_type:
        sql += " AND request_type = ?"
        params = (request_id, request_type)
    with db_lock, connect_db() as conn:
        row = execute(conn, sql, params).fetchone()
    if row is None:
        raise ValueError("요청을 찾을 수 없습니다.")
    return request_from_row(row, include_snapshot=True)


def paginate_rows(rows: list[dict[str, Any]], page: int = 1, page_size: int = FONTTILE_PAGE_SIZE) -> dict[str, Any]:
    size = max(1, int(page_size or FONTTILE_PAGE_SIZE))
    total = len(rows)
    total_pages = max(1, (total + size - 1) // size)
    current = max(1, min(total_pages, int(page or 1)))
    start = (current - 1) * size
    return {
        "rows": rows[start:start + size],
        "page": current,
        "pageSize": size,
        "totalRows": total,
        "totalPages": total_pages,
    }


def request_validation_rows(request_record: dict[str, Any]) -> list[dict[str, Any]]:
    _header, rows = dictionary_rows()
    result: list[dict[str, Any]] = []
    for snapshot in request_record.get("snapshot", []):
        row_number = parse_int(str(snapshot.get("rowNumber", "0")))
        original = str(snapshot.get("original", ""))
        row = find_dictionary_row(rows, row_number, original)
        current_translation = str(row.get("translation", "")) if row else ""
        conflict_reason = ""
        if row is None:
            conflict_reason = "값이 변경됨"
        elif current_translation != str(snapshot.get("baseTranslation", "")):
            conflict_reason = "값이 변경됨"
        submitted_translation = str(snapshot.get("submittedTranslation", ""))
        byte_length, byte_errors = fonttile_byte_length(submitted_translation)
        min_max_bytes = parse_int(str(snapshot.get("minMaxBytes", "0")))
        result.append({
            **snapshot,
            "currentTranslation": current_translation,
            "byteLength": byte_length,
            "byteErrors": byte_errors,
            "byteOverflow": bool(submitted_translation) and min_max_bytes > 0 and byte_length > min_max_bytes,
            "conflictReason": conflict_reason,
            "hasConflict": bool(conflict_reason),
        })
    return result


def fonttile_request_detail(request_id: str, request_type: str, page: int = 1, page_size: int = FONTTILE_PAGE_SIZE) -> dict[str, Any]:
    request_record = load_fonttile_request(request_id, request_type)
    rows = request_validation_rows(request_record)
    conflicts = [row for row in rows if row["hasConflict"]]
    return {
        **request_record,
        "conflictRows": len(conflicts),
        "canApprove": request_record["status"] == "pending" and not conflicts,
        **paginate_rows(rows, page, page_size),
    }


def approve_fonttile_request(request_id: str, request_type: str, user: dict[str, Any]) -> dict[str, Any]:
    request_record = load_fonttile_request(request_id, request_type)
    if request_record["status"] != "pending":
        raise ValueError("이미 승인된 요청입니다.")
    with fonttile_csv_lock:
        header, rows = dictionary_rows()
        if "translation" not in header:
            header.append("translation")
        validation_rows = request_validation_rows(request_record)
        conflicts = [row for row in validation_rows if row["hasConflict"]]
        if conflicts:
            return {"ok": False, "approved": False, "conflictRows": len(conflicts), "conflicts": conflicts}
        changed = 0
        for item in validation_rows:
            row = find_dictionary_row(rows, parse_int(str(item.get("rowNumber", "0"))), str(item.get("original", "")))
            if row is None:
                continue
            row["translation"] = str(item.get("submittedTranslation", ""))
            changed += 1
        if changed:
            write_csv_atomic(DICTIONARY_PATH, header, [[row.get(column, "") for column in header] for row in rows])
        with db_lock, connect_db() as conn:
            execute(
                conn,
                """
                UPDATE fonttile_translation_requests
                SET status = 'approved',
                    approved_at = ?,
                    approved_user_id = ?,
                    approved_username = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now_ts(), user["id"], user["username"], request_id),
            )
    return {"ok": True, "approved": True, "changedRows": changed, "dictionary": root_relative(DICTIONARY_PATH)}


def delete_fonttile_request(request_id: str, request_type: str) -> dict[str, Any]:
    with db_lock, connect_db() as conn:
        cursor = execute(
            conn,
            "DELETE FROM fonttile_translation_requests WHERE id = ? AND request_type = ? AND status = 'pending'",
            (request_id, request_type),
        )
    return {"ok": True, "deleted": int(getattr(cursor, "rowcount", 0) or 0), "id": request_id}


def fonttile_bulk_preview(target_text: str, replacement_text: str, page: int = 1, page_size: int = FONTTILE_PAGE_SIZE) -> dict[str, Any]:
    if target_text == "":
        raise ValueError("대상문자를 입력하세요.")
    _header, rows = dictionary_rows()
    matches: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        translation = str(row.get("translation", ""))
        if target_text not in translation:
            continue
        after = translation.replace(target_text, replacement_text)
        matches.append({
            "rowNumber": index,
            "original": row.get("original", ""),
            "baseTranslation": translation,
            "submittedTranslation": after,
            "before": translation,
            "after": after,
            "count": parse_int(row.get("count", "0")),
            "minMaxBytes": parse_int(row.get("min_max_bytes", "0")),
            "maxMaxBytes": parse_int(row.get("max_max_bytes", "0")),
            "samples": row.get("samples", ""),
        })
    return {
        "targetText": target_text,
        "replacementText": replacement_text,
        **paginate_rows(matches, page, page_size),
    }


def submit_fonttile_bulk_request(target_text: str, replacement_text: str, user: dict[str, Any]) -> dict[str, Any]:
    preview = fonttile_bulk_preview(target_text, replacement_text, 1, 1_000_000)
    rows = preview["rows"]
    if not rows:
        raise ValueError("제출할 치환 대상이 없습니다.")
    snapshot = [
        {
            "rowNumber": row["rowNumber"],
            "original": row["original"],
            "baseTranslation": row["baseTranslation"],
            "submittedTranslation": row["submittedTranslation"],
            "count": row["count"],
            "minMaxBytes": row["minMaxBytes"],
            "maxMaxBytes": row["maxMaxBytes"],
            "samples": row["samples"],
        }
        for row in rows
    ]
    response = insert_fonttile_request("bulk", snapshot, user, target_text, replacement_text)
    response["matchedRows"] = len(snapshot)
    return response
