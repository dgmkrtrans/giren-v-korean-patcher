from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import uuid
from typing import Any

from fastapi import Cookie, Depends, HTTPException, Request, Response, status

from ..core.config import (
    LOGIN_MAX_ATTEMPTS,
    LOGIN_WINDOW_SECONDS,
    PASSWORD_ITERATIONS,
    EMAIL_VERIFICATION_TTL_SECONDS,
    ROLE_ADMIN,
    ROLE_EDITOR,
    ROLE_LEVELS,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    use_secure_cookie,
)
from ..core.db import DB_ERRORS, DB_INTEGRITY_ERRORS, connect_db, db_lock, execute, report_db_error
from ..core.utils import as_bool, clean_text, now_ts
from .email import send_verification_email


login_attempts: dict[str, list[float]] = {}
login_attempts_lock = threading.Lock()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailNotVerifiedError(ValueError):
    pass


def public_user(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "emailVerifiedAt": row["email_verified_at"],
        "role": row["role"],
        "isActive": bool(row["is_active"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "lastLoginAt": row["last_login_at"],
    }


def validate_role(role: str) -> str:
    if role not in ROLE_LEVELS:
        raise ValueError("알 수 없는 권한입니다.")
    return role


def normalize_username(username: Any) -> str:
    text = clean_text(username).lower()
    if not (3 <= len(text) <= 32):
        raise ValueError("사용자 이름은 3~32자여야 합니다.")
    if not all(char.isalnum() or char in {"_", "-", "."} for char in text):
        raise ValueError("사용자 이름에는 영문/숫자/._- 만 사용할 수 있습니다.")
    return text


def normalize_email(email: Any) -> str:
    text = clean_text(email).lower()
    if not (5 <= len(text) <= 191) or not EMAIL_RE.match(text):
        raise ValueError("올바른 이메일 주소를 입력하세요.")
    return text


def validate_password(password: Any) -> str:
    text = str(password or "")
    if len(text) < 4:
        raise ValueError("비밀번호는 4자 이상이어야 합니다.")
    return text


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_text, digest_text = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except Exception:
        return False
    return hmac.compare_digest(actual, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def has_any_user() -> bool:
    try:
        with db_lock, connect_db() as conn:
            row = execute(conn, "SELECT 1 FROM users WHERE is_active = 1 LIMIT 1").fetchone()
            return row is not None
    except DB_ERRORS as exc:
        report_db_error("has_any_user failed", exc)
        return True


def login_rate_key(username: Any, request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{clean_text(username).lower()}"


def ensure_login_allowed(username: Any, request: Request) -> str:
    key = login_rate_key(username, request)
    cutoff = now_ts() - LOGIN_WINDOW_SECONDS
    with login_attempts_lock:
        attempts = [item for item in login_attempts.get(key, []) if item >= cutoff]
        login_attempts[key] = attempts
        if len(attempts) >= LOGIN_MAX_ATTEMPTS:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.")
    return key


def record_login_failure(key: str) -> None:
    with login_attempts_lock:
        login_attempts.setdefault(key, []).append(now_ts())


def clear_login_failures(key: str) -> None:
    with login_attempts_lock:
        login_attempts.pop(key, None)


def create_user(
    username: Any,
    password: Any,
    role: str,
    *,
    active: bool = True,
    email: Any = None,
    email_verified: bool = True,
) -> dict[str, Any]:
    username_text = normalize_username(username)
    password_text = validate_password(password)
    role_text = validate_role(role)
    email_text = normalize_email(email) if clean_text(email) else None
    timestamp = now_ts()
    user_id = uuid.uuid4().hex
    try:
        with db_lock, connect_db() as conn:
            execute(
                conn,
                """
                INSERT INTO users (id, username, email, email_verified_at, password_hash, role, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username_text,
                    email_text,
                    timestamp if email_text and email_verified else None,
                    hash_password(password_text),
                    role_text,
                    int(active),
                    timestamp,
                    timestamp,
                ),
            )
            row = execute(conn, "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    except DB_INTEGRITY_ERRORS as exc:
        raise ValueError("이미 존재하는 사용자 이름 또는 이메일입니다.") from exc
    assert row is not None
    return public_user(row)


def verification_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/api/auth/verify-email?token={token}"


def create_email_verification_token(user_id: str, email: str) -> str:
    token = secrets.token_urlsafe(32)
    timestamp = now_ts()
    with db_lock, connect_db() as conn:
        execute(conn, "DELETE FROM email_verification_tokens WHERE user_id = ? AND used_at IS NULL", (user_id,))
        execute(
            conn,
            """
            INSERT INTO email_verification_tokens (token_hash, user_id, email, created_at, expires_at, used_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (token_hash(token), user_id, email, timestamp, timestamp + EMAIL_VERIFICATION_TTL_SECONDS),
        )
    return token


def signup_user(username: Any, email: Any, password: Any, base_url: str) -> dict[str, Any]:
    user = create_user(username, password, ROLE_EDITOR, active=True, email=email, email_verified=False)
    token = create_email_verification_token(user["id"], user["email"])
    url = verification_url(base_url, token)
    mail_sent = send_verification_email(user["email"], user["username"], url)
    result: dict[str, Any] = {"user": user, "mailSent": mail_sent}
    if not mail_sent:
        result["devVerificationUrl"] = url
    return result


def verify_email_token(token: Any) -> dict[str, Any]:
    token_text = clean_text(token)
    if not token_text:
        raise ValueError("인증 토큰이 없습니다.")
    timestamp = now_ts()
    with db_lock, connect_db() as conn:
        row = execute(
            conn,
            """
            SELECT * FROM email_verification_tokens
            WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
            """,
            (token_hash(token_text), timestamp),
        ).fetchone()
        if row is None:
            raise ValueError("이메일 인증 링크가 만료되었거나 올바르지 않습니다.")
        execute(
            conn,
            "UPDATE users SET email_verified_at = ?, updated_at = ? WHERE id = ? AND email = ?",
            (timestamp, timestamp, row["user_id"], row["email"]),
        )
        execute(conn, "UPDATE email_verification_tokens SET used_at = ? WHERE token_hash = ?", (timestamp, row["token_hash"]))
        user = execute(conn, "SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
    if user is None:
        raise LookupError("사용자를 찾을 수 없습니다.")
    return public_user(user)


def update_user_record(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    fields: list[str] = []
    values: list[Any] = []
    next_role = clean_text(payload.get("role")) if "role" in payload else None
    next_active = as_bool(payload.get("isActive")) if "isActive" in payload else None
    with db_lock, connect_db() as conn:
        current = execute(conn, "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if current is None:
            raise LookupError("사용자를 찾을 수 없습니다.")
        if current["role"] == ROLE_ADMIN and current["is_active"]:
            admin_count = execute(conn, "SELECT COUNT(*) AS count FROM users WHERE role = ? AND is_active = 1", (ROLE_ADMIN,)).fetchone()["count"]
            removes_admin = (next_role is not None and next_role != ROLE_ADMIN) or next_active is False
            if admin_count <= 1 and removes_admin:
                raise ValueError("마지막 활성 관리자는 비활성화하거나 권한을 낮출 수 없습니다.")
    if "role" in payload:
        fields.append("role = ?")
        values.append(validate_role(next_role or ""))
    if "isActive" in payload:
        fields.append("is_active = ?")
        values.append(1 if next_active else 0)
    if clean_text(payload.get("password")):
        fields.append("password_hash = ?")
        values.append(hash_password(validate_password(payload.get("password"))))
    if not fields:
        raise ValueError("변경할 항목이 없습니다.")
    fields.append("updated_at = ?")
    values.append(now_ts())
    values.append(user_id)
    with db_lock, connect_db() as conn:
        execute(conn, f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
        row = execute(conn, "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise LookupError("사용자를 찾을 수 없습니다.")
    return public_user(row)


def list_users() -> list[dict[str, Any]]:
    with db_lock, connect_db() as conn:
        rows = execute(conn, "SELECT * FROM users ORDER BY created_at ASC").fetchall()
    return [public_user(row) for row in rows]


def authenticate_user(username: Any, password: Any) -> dict[str, Any] | None:
    username_text = normalize_username(username)
    with db_lock, connect_db() as conn:
        row = execute(conn, "SELECT * FROM users WHERE username = ?", (username_text,)).fetchone()
        if row is None or not row["is_active"] or not verify_password(str(password or ""), row["password_hash"]):
            return None
        if row["email"] and not row["email_verified_at"]:
            raise EmailNotVerifiedError("이메일 인증 후 로그인할 수 있습니다.")
        timestamp = now_ts()
        execute(conn, "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?", (timestamp, timestamp, row["id"]))
        updated = execute(conn, "SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    assert updated is not None
    return public_user(updated)


def create_session(user_id: str) -> tuple[str, float]:
    token = secrets.token_urlsafe(32)
    timestamp = now_ts()
    expires_at = timestamp + SESSION_TTL_SECONDS
    with db_lock, connect_db() as conn:
        execute(
            conn,
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash(token), user_id, timestamp, expires_at, timestamp),
        )
    return token, expires_at


def delete_session(token: str) -> None:
    with db_lock, connect_db() as conn:
        execute(conn, "DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def user_from_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    timestamp = now_ts()
    with db_lock, connect_db() as conn:
        execute(conn, "DELETE FROM sessions WHERE expires_at <= ?", (timestamp,))
        row = execute(
            conn,
            """
            SELECT users.*
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.is_active = 1
            """,
            (token_hash(token), timestamp),
        ).fetchone()
        if row is None:
            return None
        execute(conn, "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (timestamp, token_hash(token)))
    return public_user(row)


def set_session_cookie(response: Response, token: str, expires_at: float) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max(0, int(expires_at - now_ts())),
        httponly=True,
        secure=use_secure_cookie(),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def require_user(session: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> dict[str, Any]:
    user = user_from_session(session)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다.")
    return user


def require_role(required: str):
    def dependency(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        if ROLE_LEVELS[user["role"]] < ROLE_LEVELS[required]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="권한이 부족합니다.")
        return user

    return dependency
