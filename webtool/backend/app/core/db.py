from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # SQLite-only local runs still work until requirements are installed.
    pymysql = None  # type: ignore[assignment]
    DictCursor = None  # type: ignore[assignment]

from .config import DATABASE_URL, DB_CONNECT_RETRIES, DB_PATH, DB_TIMEOUT_SECONDS


db_lock = threading.Lock()
state_lock = threading.Lock()
memory_app_state: dict[str, Any] = {}
last_db_error: tuple[str, str] | None = None
DB_BACKEND = urlparse(DATABASE_URL).scheme.lower() if "://" in DATABASE_URL else "sqlite"
if pymysql is None:
    DB_ERRORS = (OSError, sqlite3.Error)
    DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
else:
    DB_ERRORS = (OSError, sqlite3.Error, pymysql.MySQLError)
    DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError, pymysql.err.IntegrityError)


def mysql_connect_kwargs() -> dict[str, Any]:
    if pymysql is None:
        raise RuntimeError("PyMySQL is required for WEBTOOL_DATABASE_URL=mysql://...")
    parsed = urlparse(DATABASE_URL)
    query = dict(parse_qsl(parsed.query))
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": parsed.path.lstrip("/") or None,
        "charset": query.get("charset", "utf8mb4"),
        "connect_timeout": int(query.get("connect_timeout", DB_TIMEOUT_SECONDS)),
        "read_timeout": int(query.get("read_timeout", DB_TIMEOUT_SECONDS)),
        "write_timeout": int(query.get("write_timeout", DB_TIMEOUT_SECONDS)),
        "autocommit": True,
        "cursorclass": DictCursor,
    }


def sqlite_path() -> Path:
    if DATABASE_URL.startswith("sqlite:///"):
        path = urlparse(DATABASE_URL).path
        if path.startswith("//"):
            path = path[1:]
        return Path(path)
    return DB_PATH


def database_summary() -> str:
    if DB_BACKEND in {"mysql", "mysql+pymysql"}:
        parsed = urlparse(DATABASE_URL)
        username = unquote(parsed.username or "")
        auth = f"{username}:***@" if username else ""
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return parsed._replace(netloc=f"{auth}{host}").geturl()
    return str(sqlite_path())


def mysql_execute(conn: Any, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
    cursor = conn.cursor()
    cursor.execute(sql.replace("?", "%s"), params)
    return cursor


def connect_db() -> Any:
    if DB_BACKEND in {"mysql", "mysql+pymysql"}:
        last_error: Exception | None = None
        for attempt in range(DB_CONNECT_RETRIES):
            try:
                assert pymysql is not None
                return pymysql.connect(**mysql_connect_kwargs())
            except DB_ERRORS as exc:
                last_error = exc
                time.sleep(0.1 * (attempt + 1))
        assert last_error is not None
        raise last_error

    path = sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(DB_CONNECT_RETRIES):
        try:
            conn = sqlite3.connect(path, timeout=DB_TIMEOUT_SECONDS)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {DB_TIMEOUT_SECONDS * 1000}")
            return conn
        except sqlite3.OperationalError as exc:
            last_error = exc
            message = str(exc).lower()
            if "unable to open database file" not in message and "database is locked" not in message:
                raise
            time.sleep(0.05 * (attempt + 1))
    assert last_error is not None
    raise last_error


def execute(conn: Any, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
    if DB_BACKEND in {"mysql", "mysql+pymysql"}:
        return mysql_execute(conn, sql, params)
    return conn.execute(sql, params)


def report_db_error(action: str, exc: Exception) -> None:
    global last_db_error
    message = str(exc)
    current = (action, message)
    if current != last_db_error:
        print(f"[webtool db] {action}: {message}", file=sys.stderr)
        last_db_error = current


def init_db() -> None:
    try:
        with db_lock, connect_db() as conn:
            if DB_BACKEND in {"mysql", "mysql+pymysql"}:
                init_mysql_db(conn)
            else:
                init_sqlite_db(conn)
            execute(
                conn,
                "UPDATE jobs SET status = 'interrupted', finished_at = COALESCE(finished_at, ?) WHERE status = 'running'",
                (time.time(),),
            )
    except DB_ERRORS as exc:
        report_db_error("init_db failed; continuing with in-memory state", exc)


def init_sqlite_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            command_json TEXT NOT NULL,
            cwd TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            finished_at REAL,
            returncode INTEGER,
            output_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ui_state (
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (user_id, key)
        );
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            email_verified_at REAL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_login_at REAL
        );
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            email TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            used_at REAL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS translation_drafts (
            csv_path TEXT NOT NULL,
            sha1 TEXT NOT NULL,
            user_id TEXT NOT NULL,
            base_json TEXT NOT NULL,
            draft_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (csv_path, sha1, user_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS translation_row_marks (
            csv_path TEXT NOT NULL,
            sha1 TEXT NOT NULL,
            marked_at REAL NOT NULL,
            PRIMARY KEY (csv_path, sha1)
        );
        CREATE TABLE IF NOT EXISTS translation_merge_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            csv_path TEXT NOT NULL,
            sha1 TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('merged', 'deleted')),
            submitted_user_id TEXT NOT NULL,
            submitted_username TEXT NOT NULL,
            executor_user_id TEXT NOT NULL,
            executor_username TEXT NOT NULL,
            original_json TEXT NOT NULL,
            submitted_json TEXT NOT NULL,
            applied_json TEXT NOT NULL,
            merged INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            executed_at REAL NOT NULL,
            notified_at REAL
        );
        CREATE TABLE IF NOT EXISTS translation_notifications (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at REAL NOT NULL,
            merged_count INTEGER NOT NULL,
            deleted_count INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS translation_notification_items (
            notification_id TEXT NOT NULL,
            log_id INTEGER NOT NULL,
            PRIMARY KEY (notification_id, log_id),
            FOREIGN KEY (notification_id) REFERENCES translation_notifications(id) ON DELETE CASCADE,
            FOREIGN KEY (log_id) REFERENCES translation_merge_logs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS bulk_translation_requests (
            id TEXT PRIMARY KEY,
            csv_path TEXT NOT NULL,
            target_text TEXT NOT NULL,
            replacement_text TEXT NOT NULL,
            groups_json TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            submitted_user_id TEXT NOT NULL,
            submitted_username TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'approved')),
            created_at REAL NOT NULL,
            approved_at REAL,
            approved_user_id TEXT,
            approved_username TEXT,
            FOREIGN KEY (submitted_user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS fonttile_translation_requests (
            id TEXT PRIMARY KEY,
            request_type TEXT NOT NULL CHECK (request_type IN ('dictionary', 'bulk')),
            target_text TEXT NOT NULL DEFAULT '',
            replacement_text TEXT NOT NULL DEFAULT '',
            snapshot_json TEXT NOT NULL,
            submitted_user_id TEXT NOT NULL,
            submitted_username TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'approved')),
            created_at REAL NOT NULL,
            approved_at REAL,
            approved_user_id TEXT,
            approved_username TEXT,
            FOREIGN KEY (submitted_user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS graphic_translation_targets (
            csv_path TEXT NOT NULL,
            `row_number` INTEGER NOT NULL,
            created_by TEXT,
            created_at REAL NOT NULL,
            PRIMARY KEY (csv_path, `row_number`)
        );
        CREATE TABLE IF NOT EXISTS graphic_translation_uploads (
            csv_path TEXT NOT NULL,
            `row_number` INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            pending_path TEXT NOT NULL,
            original_filename TEXT,
            validation_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (csv_path, `row_number`, user_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_user_id ON email_verification_tokens(user_id);
        CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_expires_at ON email_verification_tokens(expires_at);
        CREATE INDEX IF NOT EXISTS idx_translation_drafts_user_id ON translation_drafts(user_id);
        CREATE INDEX IF NOT EXISTS idx_translation_drafts_updated_at ON translation_drafts(updated_at);
        CREATE INDEX IF NOT EXISTS idx_translation_merge_logs_user_id ON translation_merge_logs(submitted_user_id);
        CREATE INDEX IF NOT EXISTS idx_translation_merge_logs_notified_at ON translation_merge_logs(notified_at);
        CREATE INDEX IF NOT EXISTS idx_translation_notifications_user_id ON translation_notifications(user_id);
        CREATE INDEX IF NOT EXISTS idx_bulk_translation_requests_csv_status ON bulk_translation_requests(csv_path, status);
        CREATE INDEX IF NOT EXISTS idx_bulk_translation_requests_created_at ON bulk_translation_requests(created_at);
        CREATE INDEX IF NOT EXISTS idx_fonttile_translation_requests_type_status ON fonttile_translation_requests(request_type, status);
        CREATE INDEX IF NOT EXISTS idx_fonttile_translation_requests_created_at ON fonttile_translation_requests(created_at);
        CREATE INDEX IF NOT EXISTS idx_graphic_translation_uploads_user_id ON graphic_translation_uploads(user_id);
        CREATE INDEX IF NOT EXISTS idx_graphic_translation_uploads_updated_at ON graphic_translation_uploads(updated_at);
        """
    )
    migrate_sqlite_db(conn)


def sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate_sqlite_db(conn: sqlite3.Connection) -> None:
    columns = sqlite_columns(conn, "users")
    if "email" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "email_verified_at" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN email_verified_at REAL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")


def init_mysql_db(conn: Any) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS app_state (
            `key` VARCHAR(191) PRIMARY KEY,
            value LONGTEXT NOT NULL,
            updated_at DOUBLE NOT NULL
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id VARCHAR(64) PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            command_json LONGTEXT NOT NULL,
            cwd TEXT NOT NULL,
            status VARCHAR(32) NOT NULL,
            started_at DOUBLE NOT NULL,
            finished_at DOUBLE NULL,
            returncode INT NULL,
            output_json LONGTEXT NOT NULL
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS ui_state (
            user_id VARCHAR(64) NOT NULL,
            `key` VARCHAR(191) NOT NULL,
            value LONGTEXT NOT NULL,
            updated_at DOUBLE NOT NULL,
            PRIMARY KEY (user_id, `key`)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS users (
            id VARCHAR(64) PRIMARY KEY,
            username VARCHAR(191) NOT NULL UNIQUE,
            email VARCHAR(191) NULL UNIQUE,
            email_verified_at DOUBLE NULL,
            password_hash VARCHAR(255) NOT NULL,
            role ENUM('admin', 'editor', 'viewer') NOT NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            created_at DOUBLE NOT NULL,
            updated_at DOUBLE NOT NULL,
            last_login_at DOUBLE NULL
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            token_hash CHAR(64) PRIMARY KEY,
            user_id VARCHAR(64) NOT NULL,
            email VARCHAR(191) NOT NULL,
            created_at DOUBLE NOT NULL,
            expires_at DOUBLE NOT NULL,
            used_at DOUBLE NULL,
            CONSTRAINT fk_email_verification_tokens_user_id FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            INDEX idx_email_verification_tokens_user_id (user_id),
            INDEX idx_email_verification_tokens_expires_at (expires_at)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash CHAR(64) PRIMARY KEY,
            user_id VARCHAR(64) NOT NULL,
            created_at DOUBLE NOT NULL,
            expires_at DOUBLE NOT NULL,
            last_seen_at DOUBLE NOT NULL,
            CONSTRAINT fk_sessions_user_id FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            INDEX idx_sessions_user_id (user_id),
            INDEX idx_sessions_expires_at (expires_at)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS translation_drafts (
            csv_path VARCHAR(255) NOT NULL,
            sha1 CHAR(40) NOT NULL,
            user_id VARCHAR(64) NOT NULL,
            base_json LONGTEXT NOT NULL,
            draft_json LONGTEXT NOT NULL,
            created_at DOUBLE NOT NULL,
            updated_at DOUBLE NOT NULL,
            PRIMARY KEY (csv_path, sha1, user_id),
            CONSTRAINT fk_translation_drafts_user_id FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            INDEX idx_translation_drafts_user_id (user_id),
            INDEX idx_translation_drafts_updated_at (updated_at)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS translation_row_marks (
            csv_path VARCHAR(255) NOT NULL,
            sha1 CHAR(40) NOT NULL,
            marked_at DOUBLE NOT NULL,
            PRIMARY KEY (csv_path, sha1)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS translation_merge_logs (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            csv_path VARCHAR(255) NOT NULL,
            sha1 CHAR(40) NOT NULL,
            action ENUM('merged', 'deleted') NOT NULL,
            submitted_user_id VARCHAR(64) NOT NULL,
            submitted_username VARCHAR(191) NOT NULL,
            executor_user_id VARCHAR(64) NOT NULL,
            executor_username VARCHAR(191) NOT NULL,
            original_json LONGTEXT NOT NULL,
            submitted_json LONGTEXT NOT NULL,
            applied_json LONGTEXT NOT NULL,
            merged TINYINT(1) NOT NULL,
            note TEXT NOT NULL,
            executed_at DOUBLE NOT NULL,
            notified_at DOUBLE NULL,
            INDEX idx_translation_merge_logs_user_id (submitted_user_id),
            INDEX idx_translation_merge_logs_notified_at (notified_at)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS translation_notifications (
            id VARCHAR(64) PRIMARY KEY,
            user_id VARCHAR(64) NOT NULL,
            created_by VARCHAR(64) NOT NULL,
            created_at DOUBLE NOT NULL,
            merged_count INT NOT NULL,
            deleted_count INT NOT NULL,
            CONSTRAINT fk_translation_notifications_user_id FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            INDEX idx_translation_notifications_user_id (user_id)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS translation_notification_items (
            notification_id VARCHAR(64) NOT NULL,
            log_id BIGINT NOT NULL,
            PRIMARY KEY (notification_id, log_id),
            CONSTRAINT fk_translation_notification_items_notification_id FOREIGN KEY (notification_id) REFERENCES translation_notifications(id) ON DELETE CASCADE,
            CONSTRAINT fk_translation_notification_items_log_id FOREIGN KEY (log_id) REFERENCES translation_merge_logs(id) ON DELETE CASCADE
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS bulk_translation_requests (
            id VARCHAR(64) PRIMARY KEY,
            csv_path VARCHAR(255) NOT NULL,
            target_text TEXT NOT NULL,
            replacement_text TEXT NOT NULL,
            groups_json LONGTEXT NOT NULL,
            snapshot_json LONGTEXT NOT NULL,
            submitted_user_id VARCHAR(64) NOT NULL,
            submitted_username VARCHAR(191) NOT NULL,
            status ENUM('pending', 'approved') NOT NULL,
            created_at DOUBLE NOT NULL,
            approved_at DOUBLE NULL,
            approved_user_id VARCHAR(64) NULL,
            approved_username VARCHAR(191) NULL,
            CONSTRAINT fk_bulk_translation_requests_user_id FOREIGN KEY (submitted_user_id) REFERENCES users(id) ON DELETE CASCADE,
            INDEX idx_bulk_translation_requests_csv_status (csv_path, status),
            INDEX idx_bulk_translation_requests_created_at (created_at)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS fonttile_translation_requests (
            id VARCHAR(64) PRIMARY KEY,
            request_type ENUM('dictionary', 'bulk') NOT NULL,
            target_text TEXT NOT NULL,
            replacement_text TEXT NOT NULL,
            snapshot_json LONGTEXT NOT NULL,
            submitted_user_id VARCHAR(64) NOT NULL,
            submitted_username VARCHAR(191) NOT NULL,
            status ENUM('pending', 'approved') NOT NULL,
            created_at DOUBLE NOT NULL,
            approved_at DOUBLE NULL,
            approved_user_id VARCHAR(64) NULL,
            approved_username VARCHAR(191) NULL,
            CONSTRAINT fk_fonttile_translation_requests_user_id FOREIGN KEY (submitted_user_id) REFERENCES users(id) ON DELETE CASCADE,
            INDEX idx_fonttile_translation_requests_type_status (request_type, status),
            INDEX idx_fonttile_translation_requests_created_at (created_at)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS graphic_translation_targets (
            csv_path VARCHAR(255) NOT NULL,
            `row_number` INT NOT NULL,
            created_by VARCHAR(64) NULL,
            created_at DOUBLE NOT NULL,
            PRIMARY KEY (csv_path, `row_number`)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS graphic_translation_uploads (
            csv_path VARCHAR(255) NOT NULL,
            `row_number` INT NOT NULL,
            user_id VARCHAR(64) NOT NULL,
            pending_path TEXT NOT NULL,
            original_filename VARCHAR(255) NULL,
            validation_json LONGTEXT NOT NULL,
            created_at DOUBLE NOT NULL,
            updated_at DOUBLE NOT NULL,
            PRIMARY KEY (csv_path, `row_number`, user_id),
            CONSTRAINT fk_graphic_translation_uploads_user_id FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            INDEX idx_graphic_translation_uploads_user_id (user_id),
            INDEX idx_graphic_translation_uploads_updated_at (updated_at)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """,
    ]
    for statement in statements:
        execute(conn, statement)
    migrate_mysql_db(conn)


def mysql_columns(conn: Any, table: str) -> set[str]:
    rows = execute(conn, f"SHOW COLUMNS FROM {table}").fetchall()
    return {row["Field"] for row in rows}


def migrate_mysql_db(conn: Any) -> None:
    columns = mysql_columns(conn, "users")
    if "email" not in columns:
        execute(conn, "ALTER TABLE users ADD COLUMN email VARCHAR(191) NULL UNIQUE AFTER username")
    if "email_verified_at" not in columns:
        execute(conn, "ALTER TABLE users ADD COLUMN email_verified_at DOUBLE NULL AFTER email")


def decode_state_rows(rows: list[Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for row in rows:
        state[row["key"]] = json.loads(row["value"])
    return state
