from __future__ import annotations

import os
import sys
from pathlib import Path


WEBTOOL_DIR = Path(__file__).resolve().parents[3]
ROOT = WEBTOOL_DIR.parent
FRONTEND_DIST = WEBTOOL_DIR / "frontend" / "dist"
DB_PATH = Path(os.environ.get("WEBTOOL_DB_PATH", WEBTOOL_DIR / "state.sqlite3"))
DATABASE_URL = os.environ.get("WEBTOOL_DATABASE_URL", f"sqlite:///{DB_PATH}")
PYTHON = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable

CSV_PAGE_SIZE = 15
JOB_LIST_LIMIT = 5
JOB_SAVE_INTERVAL_SECONDS = 0.5
DB_TIMEOUT_SECONDS = 30
DB_CONNECT_RETRIES = 5
JOB_RETENTION_LIMIT = int(os.environ.get("WEBTOOL_JOB_RETENTION_LIMIT", "100"))
FD_HEALTH_WARN_RATIO = float(os.environ.get("WEBTOOL_FD_HEALTH_WARN_RATIO", "0.85"))

SESSION_COOKIE = "gihren_webtool_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
PASSWORD_ITERATIONS = 260_000
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 10 * 60
EMAIL_VERIFICATION_TTL_SECONDS = 60 * 60 * 24
SIGNUP_ENABLED = os.environ.get("WEBTOOL_SIGNUP_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
PUBLIC_URL = os.environ.get("WEBTOOL_PUBLIC_URL", "").rstrip("/")
SMTP_HOST = os.environ.get("WEBTOOL_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("WEBTOOL_SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("WEBTOOL_SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("WEBTOOL_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("WEBTOOL_SMTP_FROM", SMTP_USERNAME)
SMTP_USE_TLS = os.environ.get("WEBTOOL_SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
SMTP_USE_SSL = os.environ.get("WEBTOOL_SMTP_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}

ROLE_VIEWER = "viewer"
ROLE_EDITOR = "editor"
ROLE_ADMIN = "admin"
ROLE_LEVELS = {ROLE_VIEWER: 1, ROLE_EDITOR: 2, ROLE_ADMIN: 3}

DEFAULT_TEXTURES_ROOT = "textures_static"
DEFAULT_KOREAN_TEXTURES_ROOT = "textures_translated"
FILE_COLUMNS = ("output", "파일명", "filename", "file", "path", "filepath", "file_path", "png", "image", "image_path")
JAPANESE_COLUMNS = ("japanese", "일본어", "ja", "jp")
KOREAN_COLUMNS = ("korean", "한국어", "ko", "kr")
DIALOGUE_LINE_CONTROL_OFFSET_COLUMNS = ("dialogue_line_control_offset",)
DIALOGUE_LINE_COUNT_COLUMNS = ("dialogue_line_count",)
DIALOGUE_LINE_LENGTHS_COLUMNS = ("dialogue_line_lengths",)


def allowed_hosts() -> list[str]:
    return [host.strip() for host in os.environ.get("WEBTOOL_ALLOWED_HOSTS", "").split(",") if host.strip()]


def use_secure_cookie() -> bool:
    return os.environ.get("WEBTOOL_SECURE_COOKIE", "false").lower() in {"1", "true", "yes", "on"}
