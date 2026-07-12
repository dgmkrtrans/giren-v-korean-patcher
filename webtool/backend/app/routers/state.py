from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends

from ..core.config import ROLE_ADMIN
from ..services.auth import require_role, require_user
from ..services.jobs import project_status
from ..services.state import delete_notice, get_app_state, get_notice, set_app_state, set_notice


router = APIRouter(prefix="/api", tags=["state"])


@router.get("/status")
def api_status(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return project_status()


@router.get("/state")
def api_state(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return get_app_state(user["id"])


@router.post("/state")
def api_save_state(payload: dict[str, Any] = Body(default_factory=dict), user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return set_app_state(payload, user["id"])


@router.get("/notice")
def api_notice(user: dict[str, Any] = Depends(require_user)) -> dict[str, str]:
    return get_notice()


@router.post("/notice")
def api_save_notice(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, str]:
    return set_notice(payload.get("notice", ""))


@router.delete("/notice")
def api_delete_notice(user: dict[str, Any] = Depends(require_role(ROLE_ADMIN))) -> dict[str, str]:
    return delete_notice()
