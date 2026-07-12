from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, status

from ..core.config import ROLE_ADMIN, ROLE_VIEWER
from ..core.utils import clean_text
from ..services.auth import create_user, list_users, require_role, update_user_record


router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
def api_list_users(user: dict[str, Any] = Depends(require_role(ROLE_ADMIN))) -> list[dict[str, Any]]:
    return list_users()


@router.post("", status_code=status.HTTP_201_CREATED)
def api_create_user(
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    return create_user(payload.get("username"), payload.get("password"), clean_text(payload.get("role"), ROLE_VIEWER))


@router.patch("/{user_id}")
def api_update_user(
    user_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    user: dict[str, Any] = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, Any]:
    return update_user_record(user_id, payload)
