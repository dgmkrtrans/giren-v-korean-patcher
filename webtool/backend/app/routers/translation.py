from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import FileResponse

from ..core.config import ROLE_ADMIN, ROLE_EDITOR
from ..core.utils import clean_text
from ..services.auth import require_role, require_user
from ..services.translation import (
    apply_korean_line_lengths,
    approve_bulk_translation_request,
    bulk_translation_options,
    bulk_translation_preview,
    bulk_translation_request_detail,
    bulk_translation_search,
    apply_translation_drafts,
    delete_translation_notification,
    delete_bulk_translation_request,
    discard_translation_drafts,
    get_translation_notification,
    list_bulk_translation_requests,
    list_translation_drafts,
    list_translation_notifications,
    read_translation_page,
    resolve_image_path,
    save_translation_changes,
    search_translation,
    send_translation_notifications,
    submit_bulk_translation_request,
)


router = APIRouter(prefix="/api/translation", tags=["translation"])


@router.get("")
def api_translation(
    folder: str = "textures_static",
    page: int = 1,
    showImages: bool = False,
    showTranslatedImages: bool = False,
    group: str = "",
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return read_translation_page(folder, page, showImages, group, user["id"], showTranslatedImages)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/search")
def api_translation_search(
    folder: str = "textures_static",
    q: str = "",
    group: str = "",
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return search_translation(folder, q, group, user["id"])
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/bulk/options")
def api_bulk_translation_options(
    folder: str = "textures_static",
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return bulk_translation_options(folder)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/bulk/search")
def api_bulk_translation_search(
    folder: str = "textures_static",
    q: str = "",
    groups: str = "",
    page: int = 1,
    pageSize: int = 15,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return bulk_translation_search(folder, q, groups, page, pageSize)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/bulk/preview")
def api_bulk_translation_preview(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return bulk_translation_preview(
            clean_text(payload.get("folder"), "textures_static"),
            "" if payload.get("targetText") is None else str(payload.get("targetText", "")),
            "" if payload.get("replacementText") is None else str(payload.get("replacementText", "")),
            payload.get("groups", []),
            int(payload.get("page") or 1),
            int(payload.get("pageSize") or 15),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/bulk/requests")
def api_submit_bulk_translation_request(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_EDITOR)),
) -> dict[str, Any]:
    try:
        return submit_bulk_translation_request(
            clean_text(payload.get("folder"), "textures_static"),
            "" if payload.get("targetText") is None else str(payload.get("targetText", "")),
            "" if payload.get("replacementText") is None else str(payload.get("replacementText", "")),
            payload.get("groups", []),
            user,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/bulk/requests")
def api_bulk_translation_requests(
    folder: str = "textures_static",
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return list_bulk_translation_requests(folder, user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/bulk/requests/{request_id}")
def api_bulk_translation_request_detail(
    request_id: str,
    page: int = 1,
    pageSize: int = 15,
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return bulk_translation_request_detail(request_id, page, pageSize)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/bulk/requests/{request_id}/approve")
def api_approve_bulk_translation_request(
    request_id: str,
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return approve_bulk_translation_request(request_id, user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/bulk/requests/{request_id}")
def api_delete_bulk_translation_request(
    request_id: str,
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return delete_bulk_translation_request(request_id, user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/image")
def api_translation_image(
    folder: str = "textures_static",
    path: str = "",
    user: dict[str, Any] = Depends(require_user),
) -> FileResponse:
    try:
        resolved = resolve_image_path(folder, path)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="image not found")
    content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
    return FileResponse(resolved, media_type=content_type)


@router.post("/save")
def api_translation_save(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_EDITOR)),
) -> dict[str, Any]:
    try:
        return save_translation_changes(clean_text(payload.get("folder"), "textures_static"), payload.get("changes", []), user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/drafts")
def api_translation_drafts(
    folder: str = "textures_static",
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return list_translation_drafts(folder)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/drafts/apply")
def api_translation_apply_drafts(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return apply_translation_drafts(
            clean_text(payload.get("folder"), "textures_static"),
            payload.get("items", []),
            force_conflicts=bool(payload.get("forceConflicts")),
            user=user,
            note=clean_text(payload.get("note")),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/drafts/discard")
def api_translation_discard_drafts(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_EDITOR)),
) -> dict[str, Any]:
    try:
        return discard_translation_drafts(
            clean_text(payload.get("folder"), "textures_static"),
            payload.get("items", []),
            user,
            note=clean_text(payload.get("note")),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/notifications")
def api_translation_notifications(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    try:
        return list_translation_notifications(user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/notifications/{notification_id}")
def api_translation_notification_detail(
    notification_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return get_translation_notification(notification_id, user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/notifications/{notification_id}")
def api_delete_translation_notification(
    notification_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return delete_translation_notification(notification_id, user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/notifications/send")
def api_send_translation_notifications(user: dict[str, Any] = Depends(require_role(ROLE_ADMIN))) -> dict[str, Any]:
    try:
        return send_translation_notifications(user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/apply-korean-line-lengths")
def api_translation_apply_korean_line_lengths(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return apply_korean_line_lengths(clean_text(payload.get("folder"), "textures_static"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
