from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status

from ..core.config import ROLE_ADMIN, ROLE_EDITOR
from ..services.auth import require_role, require_user
from ..services.fonttile import (
    approve_fonttile_request,
    delete_fonttile_request,
    fonttile_bulk_preview,
    fonttile_request_detail,
    list_fonttile_requests,
    read_fonttile_state,
    save_fonttile_state,
    submit_fonttile_bulk_request,
    submit_fonttile_dictionary_request,
)


router = APIRouter(prefix="/api/fonttile", tags=["fonttile"])


@router.get("")
def api_fonttile(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    try:
        return read_fonttile_state()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/save")
def api_fonttile_save(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return save_fonttile_state(payload)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/dictionary/requests")
def api_submit_fonttile_dictionary_request(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_EDITOR)),
) -> dict[str, Any]:
    try:
        return submit_fonttile_dictionary_request(payload.get("changes", []), user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/dictionary/requests")
def api_fonttile_dictionary_requests(
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return list_fonttile_requests("dictionary", user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/dictionary/requests/{request_id}")
def api_fonttile_dictionary_request_detail(
    request_id: str,
    page: int = 1,
    pageSize: int = 50,
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return fonttile_request_detail(request_id, "dictionary", page, pageSize)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/dictionary/requests/{request_id}/approve")
def api_approve_fonttile_dictionary_request(
    request_id: str,
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return approve_fonttile_request(request_id, "dictionary", user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/dictionary/requests/{request_id}")
def api_delete_fonttile_dictionary_request(
    request_id: str,
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return delete_fonttile_request(request_id, "dictionary")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/bulk/preview")
def api_fonttile_bulk_preview(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return fonttile_bulk_preview(
            "" if payload.get("targetText") is None else str(payload.get("targetText", "")),
            "" if payload.get("replacementText") is None else str(payload.get("replacementText", "")),
            int(payload.get("page") or 1),
            int(payload.get("pageSize") or 50),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/bulk/requests")
def api_submit_fonttile_bulk_request(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_EDITOR)),
) -> dict[str, Any]:
    try:
        return submit_fonttile_bulk_request(
            "" if payload.get("targetText") is None else str(payload.get("targetText", "")),
            "" if payload.get("replacementText") is None else str(payload.get("replacementText", "")),
            user,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/bulk/requests")
def api_fonttile_bulk_requests(
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return list_fonttile_requests("bulk", user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/bulk/requests/{request_id}")
def api_fonttile_bulk_request_detail(
    request_id: str,
    page: int = 1,
    pageSize: int = 50,
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return fonttile_request_detail(request_id, "bulk", page, pageSize)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/bulk/requests/{request_id}/approve")
def api_approve_fonttile_bulk_request(
    request_id: str,
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return approve_fonttile_request(request_id, "bulk", user)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/bulk/requests/{request_id}")
def api_delete_fonttile_bulk_request(
    request_id: str,
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        return delete_fonttile_request(request_id, "bulk")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
