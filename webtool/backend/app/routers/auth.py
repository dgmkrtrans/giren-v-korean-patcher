from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse

from ..core.config import PUBLIC_URL, ROLE_ADMIN, SESSION_COOKIE, SIGNUP_ENABLED
from ..services.auth import (
    EmailNotVerifiedError,
    authenticate_user,
    clear_login_failures,
    clear_session_cookie,
    create_session,
    create_user,
    delete_session,
    ensure_login_allowed,
    has_any_user,
    record_login_failure,
    require_user,
    set_session_cookie,
    signup_user,
    verify_email_token,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])


def first_header_value(value: str | None) -> str:
    return (value or "").split(",", 1)[0].strip()


def parse_forwarded_header(value: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in first_header_value(value).split(";"):
        key, separator, raw = part.strip().partition("=")
        if separator:
            result[key.lower()] = raw.strip().strip('"')
    return result


def cf_visitor_scheme(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ""
    scheme = parsed.get("scheme") if isinstance(parsed, dict) else ""
    return scheme if scheme in {"http", "https"} else ""


def is_private_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    text = hostname.lower().strip("[]")
    if text in {"localhost", "0.0.0.0"} or text.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def prefer_https_for_public_host(base_url: str) -> str:
    parsed = urlsplit(base_url if "://" in base_url else f"https://{base_url}")
    scheme = parsed.scheme or "https"
    if scheme == "http" and not is_private_host(parsed.hostname):
        scheme = "https"
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, path, "", "")).rstrip("/")


def public_signup_base_url(request: Request) -> str:
    if PUBLIC_URL:
        return prefer_https_for_public_host(PUBLIC_URL)
    forwarded = parse_forwarded_header(request.headers.get("forwarded"))
    host = (
        first_header_value(request.headers.get("x-forwarded-host"))
        or forwarded.get("host", "")
        or request.url.netloc
    )
    scheme = (
        first_header_value(request.headers.get("x-forwarded-proto"))
        or forwarded.get("proto", "")
        or cf_visitor_scheme(request.headers.get("cf-visitor"))
        or request.url.scheme
    )
    if scheme not in {"http", "https"}:
        scheme = "https"
    return prefer_https_for_public_host(f"{scheme}://{host}")


@router.get("/setup")
def auth_setup() -> dict[str, Any]:
    return {"needsSetup": not has_any_user(), "signupEnabled": SIGNUP_ENABLED}


@router.post("/bootstrap", status_code=status.HTTP_201_CREATED)
def auth_bootstrap(response: Response, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    if has_any_user():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="초기 관리자가 이미 생성되었습니다.")
    user = create_user(payload.get("username"), payload.get("password"), ROLE_ADMIN)
    token, expires_at = create_session(user["id"])
    set_session_cookie(response, token, expires_at)
    return {"user": user}


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def auth_signup(request: Request, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    if not SIGNUP_ENABLED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="회원가입이 비활성화되어 있습니다.")
    if not has_any_user():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="초기 관리자 생성 후 회원가입할 수 있습니다.")
    base_url = public_signup_base_url(request)
    result = signup_user(payload.get("username"), payload.get("email"), payload.get("password"), base_url)
    return {
        "ok": True,
        "mailSent": result["mailSent"],
        "devVerificationUrl": result.get("devVerificationUrl"),
    }


@router.get("/verify-email", response_class=HTMLResponse)
def auth_verify_email(token: str = "") -> HTMLResponse:
    user = verify_email_token(token)
    username = user["username"]
    return HTMLResponse(
        """
        <!doctype html>
        <meta charset="utf-8">
        <title>이메일 인증 완료</title>
        <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:32px;line-height:1.5">
          <h1>이메일 인증 완료</h1>
          <p>{username} 계정으로 로그인할 수 있습니다.</p>
          <p><a href="/">웹툴로 돌아가기</a></p>
        </body>
        """.format(username=username)
    )


@router.post("/login")
def auth_login(request: Request, response: Response, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    rate_key = ensure_login_allowed(payload.get("username"), request)
    try:
        user = authenticate_user(payload.get("username"), payload.get("password"))
    except EmailNotVerifiedError as exc:
        record_login_failure(rate_key)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError:
        user = None
    if user is None:
        record_login_failure(rate_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="사용자 이름 또는 비밀번호가 올바르지 않습니다.")
    clear_login_failures(rate_key)
    token, expires_at = create_session(user["id"])
    set_session_cookie(response, token, expires_at)
    return {"user": user}


@router.post("/logout")
def auth_logout(response: Response, session: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> dict[str, bool]:
    if session:
        delete_session(session)
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
def auth_me(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"user": user}
