from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import ROOT


def now_ts() -> float:
    import time

    return time.time()


def as_bool(value: Any) -> bool:
    return value is True or value == "true" or value == "on" or value == "1"


def clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def root_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def resolve_project_path(value: str, default: str = "") -> Path:
    text = clean_text(value, default)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    resolved = path.resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise ValueError("프로젝트 폴더 내부 경로만 사용할 수 있습니다.") from exc
    return resolved
