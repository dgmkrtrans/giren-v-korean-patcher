from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status

from ..core.config import JOB_LIST_LIMIT, ROLE_ADMIN, ROLE_EDITOR
from ..core.utils import clean_text
from ..models import jobs, jobs_lock
from ..services.auth import require_role, require_user
from ..services.jobs import build_command, cancel_job, discover_font_choices, job_snapshot, start_job


router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/jobs")
def api_jobs(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    with jobs_lock:
        return [
            job_snapshot(job)
            for job in sorted(jobs.values(), key=lambda item: item.started_at, reverse=True)[:JOB_LIST_LIMIT]
        ]


@router.get("/jobs/{job_id}")
def api_job(job_id: str, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        data = job_snapshot(job) if job else None
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return data


@router.post("/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str, user: dict[str, Any] = Depends(require_role(ROLE_EDITOR))) -> dict[str, bool]:
    if not cancel_job(job_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="running job not found")
    return {"ok": True}


@router.get("/fonts")
def api_fonts(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    return discover_font_choices()


@router.post("/run", status_code=status.HTTP_201_CREATED)
def api_run(payload: dict[str, Any] = Body(default_factory=dict), user: dict[str, Any] = Depends(require_role(ROLE_EDITOR))) -> dict[str, Any]:
    try:
        action = clean_text(payload.get("action"))
        if action == "fonttile-fill-slots" and user.get("role") != ROLE_ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
        job = start_job(action, payload)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return job_snapshot(job)


@router.post("/run/command")
def api_run_command(payload: dict[str, Any] = Body(default_factory=dict), user: dict[str, Any] = Depends(require_role(ROLE_EDITOR))) -> dict[str, Any]:
    try:
        action = clean_text(payload.get("action"))
        if action == "fonttile-fill-slots" and user.get("role") != ROLE_ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
        title, command = build_command(action, payload)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        "id": "command-preview",
        "title": title,
        "command": command,
        "cwd": "",
        "status": "command-only",
        "startedAt": None,
        "finishedAt": None,
        "returncode": None,
        "output": ["커맨드만 출력했습니다. 실제 실행은 하지 않았습니다."],
    }
