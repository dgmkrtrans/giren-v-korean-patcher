from __future__ import annotations

import json
import time
from typing import Any

from ..core.db import DB_BACKEND, DB_ERRORS, connect_db, db_lock, decode_state_rows, execute, memory_app_state, report_db_error, state_lock

STATE_ERRORS = DB_ERRORS + (json.JSONDecodeError,)


def get_app_state(user_id: str = "") -> dict[str, Any]:
    try:
        with db_lock, connect_db() as conn:
            if user_id:
                rows = execute(conn, "SELECT `key`, value FROM ui_state WHERE user_id = ?", (user_id,)).fetchall()
            else:
                rows = execute(conn, "SELECT `key`, value FROM app_state").fetchall()
        state = decode_state_rows(rows)
        with state_lock:
            memory_app_state.clear()
            memory_app_state.update(state)
        return state
    except STATE_ERRORS as exc:
        report_db_error("get_app_state failed; using in-memory state", exc)
        with state_lock:
            return dict(memory_app_state)


def set_app_state(payload: dict[str, Any], user_id: str = "") -> dict[str, Any]:
    allowed = {"activeTab", "selectedJobId", "forms", "translationPageSize"}
    filtered = {key: value for key, value in payload.items() if key in allowed}
    with state_lock:
        memory_app_state.update(filtered)
    now = time.time()
    try:
        with db_lock, connect_db() as conn:
            for key, value in filtered.items():
                if user_id:
                    sql = (
                        """
                        INSERT INTO ui_state (user_id, `key`, value, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = VALUES(updated_at)
                        """
                        if DB_BACKEND in {"mysql", "mysql+pymysql"}
                        else
                        """
                        INSERT INTO ui_state (user_id, `key`, value, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(user_id, `key`) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """
                    )
                    execute(
                        conn,
                        sql,
                        (user_id, key, json.dumps(value, ensure_ascii=False), now),
                    )
                else:
                    sql = (
                        """
                        INSERT INTO app_state (`key`, value, updated_at)
                        VALUES (?, ?, ?)
                        ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = VALUES(updated_at)
                        """
                        if DB_BACKEND in {"mysql", "mysql+pymysql"}
                        else
                        """
                        INSERT INTO app_state (`key`, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(`key`) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """
                    )
                    execute(
                        conn,
                        sql,
                        (key, json.dumps(value, ensure_ascii=False), now),
                    )
    except DB_ERRORS as exc:
        report_db_error("set_app_state failed; kept in memory", exc)
    with state_lock:
        return dict(memory_app_state)


def get_notice() -> dict[str, str]:
    try:
        with db_lock, connect_db() as conn:
            row = execute(conn, "SELECT value FROM app_state WHERE `key` = ?", ("notice",)).fetchone()
        if row is None:
            return {"notice": ""}
        return {"notice": str(json.loads(row["value"]) or "")}
    except STATE_ERRORS as exc:
        report_db_error("get_notice failed; using in-memory state", exc)
        with state_lock:
            return {"notice": str(memory_app_state.get("notice") or "")}


def set_notice(value: Any) -> dict[str, str]:
    notice = str(value or "")
    with state_lock:
        memory_app_state["notice"] = notice
    now = time.time()
    try:
        with db_lock, connect_db() as conn:
            sql = (
                """
                INSERT INTO app_state (`key`, value, updated_at)
                VALUES (?, ?, ?)
                ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = VALUES(updated_at)
                """
                if DB_BACKEND in {"mysql", "mysql+pymysql"}
                else
                """
                INSERT INTO app_state (`key`, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(`key`) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """
            )
            execute(conn, sql, ("notice", json.dumps(notice, ensure_ascii=False), now))
    except DB_ERRORS as exc:
        report_db_error("set_notice failed; kept in memory", exc)
    return {"notice": notice}


def delete_notice() -> dict[str, str]:
    with state_lock:
        memory_app_state.pop("notice", None)
    try:
        with db_lock, connect_db() as conn:
            execute(conn, "DELETE FROM app_state WHERE `key` = ?", ("notice",))
    except DB_ERRORS as exc:
        report_db_error("delete_notice failed; cleared in memory", exc)
    return {"notice": ""}
