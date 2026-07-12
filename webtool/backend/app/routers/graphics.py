from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import FileResponse

from ..core.config import DEFAULT_KOREAN_TEXTURES_ROOT, ROLE_ADMIN
from ..core.utils import clean_text
from ..services.auth import require_role, require_user
from ..services.graphics import (
    approve_graphic_uploads,
    check_graphic_upload,
    discard_graphic_uploads,
    list_graphic_uploads,
    read_graphic_page,
    replace_graphic_targets,
    resolve_graphic_image,
    resolve_pending_image,
    save_graphic_upload,
    write_graphic_rebuild_manifest,
)


router = APIRouter(prefix="/api/graphics", tags=["graphics"])


@router.get("")
def api_graphics(
    folder: str = "textures_static",
    page: int = 1,
    showImages: bool = True,
    q: str = "",
    translatedRoot: str = DEFAULT_KOREAN_TEXTURES_ROOT,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return read_graphic_page(folder, page, showImages, q, translatedRoot, user["id"])
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/targets")
def api_graphics_targets(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return replace_graphic_targets(
            clean_text(payload.get("folder"), "textures_static"),
            clean_text(payload.get("rowRanges")),
            user,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/upload")
def api_graphics_upload(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return save_graphic_upload(
            clean_text(payload.get("folder"), "textures_static"),
            int(payload.get("rowNumber") or 0),
            clean_text(payload.get("filename"), "upload.png"),
            clean_text(payload.get("contentBase64")),
            user,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/uploads")
def api_graphics_uploads(
    folder: str = "textures_static",
    translatedRoot: str = DEFAULT_KOREAN_TEXTURES_ROOT,
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return list_graphic_uploads(folder, translatedRoot)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/uploads/approve")
def api_graphics_approve_uploads(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return approve_graphic_uploads(
            clean_text(payload.get("folder"), "textures_static"),
            payload.get("items", []),
            clean_text(payload.get("translatedRoot"), DEFAULT_KOREAN_TEXTURES_ROOT),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/uploads/check")
def api_graphics_check_upload(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return check_graphic_upload(
            clean_text(payload.get("folder"), "textures_static"),
            int(payload.get("rowNumber") or 0),
            clean_text(payload.get("userId")),
            clean_text(payload.get("mode"), "compression"),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/uploads/discard")
def api_graphics_discard_uploads(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return discard_graphic_uploads(clean_text(payload.get("folder"), "textures_static"), payload.get("items", []))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/rebuild-manifest")
def api_graphics_rebuild_manifest(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return write_graphic_rebuild_manifest(
            clean_text(payload.get("folder"), "textures_static"),
            clean_text(payload.get("translatedRoot"), DEFAULT_KOREAN_TEXTURES_ROOT),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/image")
def api_graphics_image(
    folder: str = "textures_static",
    path: str = "",
    csvFolder: str = "",
    user: dict[str, Any] = Depends(require_user),
) -> FileResponse:
    try:
        resolved = resolve_graphic_image(folder, path, csvFolder)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="image not found")
    content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
    return FileResponse(resolved, media_type=content_type)


@router.get("/pending-image")
def api_graphics_pending_image(
    folder: str = "textures_static",
    rowNumber: int = 0,
    userId: str = "",
    user: dict[str, Any] = Depends(require_user),
) -> FileResponse:
    try:
        resolved = resolve_pending_image(folder, rowNumber, userId, user)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="image not found")
    return FileResponse(resolved, media_type="image/png")
