from __future__ import annotations

import errno
import mimetypes
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core.config import FD_HEALTH_WARN_RATIO, FRONTEND_DIST, allowed_hosts
from .core.db import DB_BACKEND, database_summary, init_db
from .routers import auth, fonttile, graphics, jobs, state, translation, users
from .services.jobs import load_jobs_from_db, terminate_running_jobs


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet, noimageindex",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

frontend_file_cache: dict[str, tuple[int, int, bytes, str]] = {}
frontend_file_cache_lock = threading.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    load_jobs_from_db()
    try:
        yield
    finally:
        terminate_running_jobs()


def create_app() -> FastAPI:
    app = FastAPI(title="Gihren Korean Project Tool", lifespan=lifespan)
    hosts = allowed_hosts()
    if hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_exception_handler(ValueError, value_error_handler)
    app.add_exception_handler(LookupError, lookup_error_handler)
    app.add_exception_handler(HTTPException, webtool_http_exception_handler)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(jobs.router)
    app.include_router(state.router)
    app.include_router(translation.router)
    app.include_router(fonttile.router)
    app.include_router(graphics.router)
    mount_health(app)
    mount_frontend(app)
    return app


class SecurityHeadersMiddleware:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def send_with_headers(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    headers.setdefault(key, value)
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except OSError as exc:
            if exc.errno in {errno.EMFILE, errno.ENFILE} and not response_started:
                response = JSONResponse(
                    {"error": "server file descriptor limit reached"},
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
                for key, value in SECURITY_HEADERS.items():
                    response.headers.setdefault(key, value)
                await response(scope, receive, send)
                return
            raise


async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)


async def lookup_error_handler(request: Request, exc: LookupError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=status.HTTP_404_NOT_FOUND)


async def webtool_http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, str):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    return await http_exception_handler(request, exc)


def open_file_count() -> int | None:
    for fd_dir in (Path("/proc/self/fd"), Path("/dev/fd")):
        try:
            return len(list(fd_dir.iterdir()))
        except OSError:
            continue
    return None


def open_file_limit() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    soft_limit, _hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    return int(soft_limit) if soft_limit > 0 else None


def process_health() -> dict[str, Any]:
    fd_count = open_file_count()
    fd_limit = open_file_limit()
    fd_ratio = (fd_count / fd_limit) if fd_count is not None and fd_limit else None
    ok = fd_ratio is None or fd_ratio < FD_HEALTH_WARN_RATIO
    return {
        "ok": ok,
        "databaseBackend": DB_BACKEND,
        "database": database_summary(),
        "openFiles": fd_count,
        "openFileLimit": fd_limit,
        "openFileRatio": fd_ratio,
    }


def mount_health(app: FastAPI) -> None:
    @app.get("/api/health", include_in_schema=False)
    def api_health() -> JSONResponse:
        health = process_health()
        return JSONResponse(
            health,
            status_code=status.HTTP_200_OK if health["ok"] else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


def cached_frontend_file(path: Path) -> Response:
    stat = path.stat()
    cache_key = str(path.resolve())
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    with frontend_file_cache_lock:
        cached = frontend_file_cache.get(cache_key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            _mtime_ns, _size, content, cached_media_type = cached
            return Response(content, media_type=cached_media_type)
        content = path.read_bytes()
        frontend_file_cache[cache_key] = (stat.st_mtime_ns, stat.st_size, content, media_type)
    return Response(content, media_type=media_type)


def mount_frontend(app: FastAPI) -> None:
    if (FRONTEND_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend_app(full_path: str) -> Response:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        target = (FRONTEND_DIST / full_path).resolve()
        try:
            target.relative_to(FRONTEND_DIST.resolve())
        except ValueError:
            target = FRONTEND_DIST / "index.html"
        if target.is_file():
            if target.name == "index.html":
                return cached_frontend_file(target)
            return FileResponse(target)
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return cached_frontend_file(index)
        return Response(
            "<!doctype html><meta charset='utf-8'><title>Webtool</title>"
            "<body><h1>Frontend build is missing</h1>"
            "<p>Run <code>cd webtool/frontend && npm install && npm run build</code>, "
            "or use the Vite dev server with <code>npm run dev</code>.</p></body>",
            media_type="text/html; charset=utf-8",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


app = create_app()
