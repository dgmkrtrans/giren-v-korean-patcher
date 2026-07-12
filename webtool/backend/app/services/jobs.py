from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..core.config import (
    DEFAULT_KOREAN_TEXTURES_ROOT,
    DEFAULT_TEXTURES_ROOT,
    JOB_RETENTION_LIMIT,
    JOB_SAVE_INTERVAL_SECONDS,
    PYTHON,
    ROOT,
)
from ..core.db import DB_BACKEND, DB_ERRORS, connect_db, database_summary, db_lock, execute, report_db_error
from ..core.utils import as_bool, clean_text, quote
from ..models import Job, jobs, jobs_lock


FONT_EXTENSIONS = {".otf", ".ttc", ".ttf", ".woff", ".woff2"}
KNOWN_FONT_CHOICES = (
    {
        "value": "tangba12",
        "label": "tangba12",
        "path": "assets/fonts/pixel/Tangba12.woff2",
        "defaultSize": 12,
    },
    {
        "value": "dunggeunmo",
        "label": "dunggeunmo",
        "path": "assets/fonts/pixel/DungGeunMo.woff",
        "defaultSize": 16,
    },
)


def load_jobs_from_db() -> None:
    try:
        with db_lock, connect_db() as conn:
            rows = execute(conn, "SELECT * FROM jobs ORDER BY started_at DESC LIMIT 100").fetchall()
    except DB_ERRORS as exc:
        report_db_error("load_jobs_from_db failed", exc)
        return
    with jobs_lock:
        jobs.clear()
        for row in rows:
            jobs[row["id"]] = Job(
                id=row["id"],
                title=row["title"],
                command=json.loads(row["command_json"]),
                cwd=row["cwd"],
                status=row["status"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                returncode=row["returncode"],
                output=json.loads(row["output_json"]),
            )


def trim_finished_jobs_locked() -> None:
    if len(jobs) <= JOB_RETENTION_LIMIT:
        return
    ordered = sorted(jobs.values(), key=lambda item: item.started_at, reverse=True)
    keep = {job.id for job in ordered[:JOB_RETENTION_LIMIT]}
    for job_id, job in list(jobs.items()):
        if job_id in keep or job.status == "running":
            continue
        jobs.pop(job_id, None)


def save_job(job: Job) -> None:
    try:
        with db_lock, connect_db() as conn:
            sql = (
                """
                INSERT INTO jobs (id, title, command_json, cwd, status, started_at, finished_at, returncode, output_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    title = VALUES(title),
                    command_json = VALUES(command_json),
                    cwd = VALUES(cwd),
                    status = VALUES(status),
                    started_at = VALUES(started_at),
                    finished_at = VALUES(finished_at),
                    returncode = VALUES(returncode),
                    output_json = VALUES(output_json)
                """
                if DB_BACKEND in {"mysql", "mysql+pymysql"}
                else
                """
                INSERT INTO jobs (id, title, command_json, cwd, status, started_at, finished_at, returncode, output_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    command_json = excluded.command_json,
                    cwd = excluded.cwd,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    returncode = excluded.returncode,
                    output_json = excluded.output_json
                """
            )
            execute(
                conn,
                sql,
                (
                    job.id,
                    job.title,
                    json.dumps(job.command, ensure_ascii=False),
                    job.cwd,
                    job.status,
                    job.started_at,
                    job.finished_at,
                    job.returncode,
                    json.dumps(job.output, ensure_ascii=False),
                ),
            )
    except DB_ERRORS as exc:
        report_db_error(f"save_job failed for {job.id}", exc)


def add_optional(args: list[str], flag: str, value: Any) -> None:
    text = clean_text(value)
    if text:
        args.extend([flag, text])


def build_command(action: str, payload: dict[str, Any]) -> tuple[str, list[str]]:
    if action == "extract-all":
        return "표준 MKD 해체", ["bash", "scripts/extract_all_mkd.sh"]
    if action == "extract-mkd9":
        return "ZZZPSP9 해체", ["bash", "scripts/extract_mkd9_sd0.sh"]
    if action == "dump-static":
        args = [PYTHON, "scripts/dump_static_cmp0_textures.py"]
        add_optional(args, "--source", payload.get("source", "unpacked_mkd"))
        add_optional(args, "--out", payload.get("out", "textures_static"))
        add_optional(args, "--max-files", payload.get("maxFiles"))
        for key, flag in (
            ("all", "--all"),
            ("categories", "--categories"),
            ("noDedupe", "--no-dedupe"),
            ("skipRawPng", "--skip-raw-png"),
            ("clean", "--clean"),
        ):
            if as_bool(payload.get(key)):
                args.append(flag)
        return "정적 텍스처 덤프", args
    if action == "render-all-categories":
        args = [PYTHON, "scripts/txt_gen/render_all_categories.py"]
        for flag, value in (
            ("--csv", payload.get("csv", "textures_static/manifest.csv")),
            ("--textures-root", payload.get("texturesRoot", DEFAULT_TEXTURES_ROOT)),
            ("--out-root", payload.get("outRoot", DEFAULT_KOREAN_TEXTURES_ROOT)),
            ("--rows", payload.get("rowRange") or payload.get("rows")),
            ("--limit", payload.get("limit")),
        ):
            add_optional(args, flag, value)
        if as_bool(payload.get("dryRun")):
            args.append("--dry-run")
        if as_bool(payload.get("strict")):
            args.append("--strict")
        if as_bool(payload.get("apply")):
            args.append("--apply")
        if as_bool(payload.get("noCopyManifest")):
            args.append("--no-copy-manifest")
        return "전체 렌더", args
    if action == "render-ui-text-fit":
        args = [PYTHON, "scripts/txt_gen/ui_text_fit_renderer.py"]
        for flag, value in (
            ("--csv", payload.get("csv", "textures_static/manifest.csv")),
            ("--textures-root", payload.get("texturesRoot", DEFAULT_TEXTURES_ROOT)),
            ("--out-root", payload.get("outRoot", DEFAULT_KOREAN_TEXTURES_ROOT)),
            ("--rows", payload.get("rowRange") or payload.get("rows")),
            ("--font", payload.get("font", "assets/fonts/NanumMyeongjoBold.ttf")),
            ("--font-size", payload.get("fontSize", "16")),
            ("--min-font-size", payload.get("minFontSize", "7")),
            ("--x-padding", payload.get("xPadding", "0")),
            ("--y-padding", payload.get("yPadding", "0")),
            ("--line-spacing", payload.get("lineSpacing", "0")),
            ("--align", payload.get("align", "center")),
            ("--valign", payload.get("valign", "center")),
            ("--min-x-scale", payload.get("minXScale", "0.01")),
            ("--min-y-scale", payload.get("minYScale", "0.01")),
            ("--report", payload.get("report")),
        ):
            add_optional(args, flag, value)
        args.extend(
            [
                "--output-column",
                "output",
                "--text-column",
                "korean",
                "--font-index",
                "0",
            ]
        )
        add_optional(args, "--target-verified-group", payload.get("targetVerifiedGroup"))
        if as_bool(payload.get("preserveNewlines")):
            args.append("--preserve-newlines")
        if as_bool(payload.get("noWrap")):
            args.append("--no-wrap")
        if as_bool(payload.get("noScale")):
            args.append("--no-scale")
        if as_bool(payload.get("dryRun")):
            args.append("--dry-run")
        if as_bool(payload.get("strict")):
            args.append("--strict")
        if as_bool(payload.get("apply")):
            args.append("--apply")
        if as_bool(payload.get("noCopyManifest")):
            args.append("--no-copy-manifest")
        target_group = clean_text(payload.get("targetVerifiedGroup"))
        return f"{target_group or '고정 UI 텍스트'} 렌더", args
    if action in {"render-textures", "render-white-transparent"}:
        args = [PYTHON, "scripts/txt_gen/white_letter_transparent.py"]
        for flag, value in (
            ("--csv", payload.get("csv", "textures_static/manifest.csv")),
            ("--textures-root", payload.get("texturesRoot", DEFAULT_TEXTURES_ROOT)),
            ("--out-root", payload.get("outRoot", DEFAULT_KOREAN_TEXTURES_ROOT)),
            ("--rows", payload.get("rowRange") or payload.get("rows")),
            ("--font", payload.get("font", "assets/fonts/NanumMyeongjoBold.ttf")),
            ("--font-size", payload.get("fontSize", "15")),
            ("--cell-size", payload.get("cellSize", "16")),
            ("--text-color-limit", payload.get("textColorLimit", "3")),
        ):
            add_optional(args, flag, value)
        args.extend(
            [
                "--pattern-csv",
                "textures_pattern/text_texture_patterns.csv",
                "--output-column",
                "output",
                "--text-column",
                "korean",
                "--font-index",
                "0",
                "--min-font-size",
                "15",
                "--layout",
                "cell",
                "--align",
                "left",
                "--x-padding",
                "0",
                "--right-padding",
                "0",
                "--y-adjust",
                "0",
                "--min-transparent-ratio",
                "0.45",
                "--min-neutral-ratio",
                "0.90",
                "--min-max-luma",
                "180",
                "--max-line-chars",
                "21",
            ]
        )
        return "대사들 렌더", args
    if action == "render-white-black-background":
        args = [PYTHON, "scripts/txt_gen/white_letter_black_background.py"]
        for flag, value in (
            ("--csv", payload.get("csv", "textures_static/manifest.csv")),
            ("--textures-root", payload.get("texturesRoot", DEFAULT_TEXTURES_ROOT)),
            ("--out-root", payload.get("outRoot", DEFAULT_KOREAN_TEXTURES_ROOT)),
            ("--rows", payload.get("rowRange") or payload.get("rows")),
            ("--font", payload.get("font", "assets/fonts/NanumMyeongjoBold.ttf")),
            ("--font-size", payload.get("fontSize", "15")),
            ("--cell-size", payload.get("cellSize", "16")),
            ("--text-color-limit", payload.get("textColorLimit", "3")),
        ):
            add_optional(args, flag, value)
        args.extend(
            [
                "--pattern-csv",
                "textures_pattern/text_texture_patterns.csv",
                "--output-column",
                "output",
                "--text-column",
                "korean",
                "--font-index",
                "0",
                "--min-font-size",
                "15",
                "--layout",
                "cell",
                "--align",
                "left",
                "--x-padding",
                "0",
                "--right-padding",
                "0",
                "--y-adjust",
                "0",
                "--target-verified-group",
                "각 세력 오프닝",
                "--background-mode",
                "opaque-most-frequent",
                "--preserve-newlines",
                "--no-wrap",
                "--max-render-lines",
                "2",
                "--max-line-chars",
                "26",
            ]
        )
        return "각 세력 오프닝 렌더", args
    if action == "rebuild-mkd":
        args = [PYTHON, "scripts/rebuild_mkd.py"]
        for flag, value in (
            ("--original-dir", payload.get("originalDir", "ExtractedISO/PSP_GAME/USRDIR")),
            ("--unpacked", payload.get("unpacked", "unpacked_mkd")),
            ("--out", payload.get("out", "rebuilt_mkd")),
            ("--archives", payload.get("archives")),
            ("--apply-textures", payload.get("applyTextures", DEFAULT_KOREAN_TEXTURES_ROOT)),
            ("--write-staged-unpacked", payload.get("writeStagedUnpacked")),
        ):
            add_optional(args, flag, value)
        for key, flag in (
            ("forceReencodeTextures", "--force-reencode-textures"),
            ("noReuseUnchanged", "--no-reuse-unchanged"),
            ("relayout", "--relayout"),
            ("verify", "--verify"),
            ("optimalSd0", "--optimal-sd0"),
        ):
            if as_bool(payload.get(key)):
                args.append(flag)
        return "MKD 리빌드", args
    if action == "import-mkd":
        args = [PYTHON, "scripts/import_mkd.py"]
        add_optional(args, "--iso", payload.get("iso", "game-patched.iso"))
        add_optional(args, "--mkd-dir", payload.get("mkdDir", "rebuilt_mkd"))
        return "ISO MKD 주입", args
    if action == "one-click-build":
        rebuild_flags = " --optimal-sd0" if as_bool(payload.get("optimalSd0")) else ""
        return (
            "리빌드 후 ISO 주입",
            [
                "bash",
                "-lc",
                f"{quote(PYTHON)} scripts/rebuild_mkd.py --apply-textures {quote(clean_text(payload.get('texturesDir'), DEFAULT_KOREAN_TEXTURES_ROOT))}{rebuild_flags} "
                f"&& {quote(PYTHON)} scripts/import_mkd.py --iso {quote(clean_text(payload.get('iso'), 'game-patched.iso'))}",
            ],
        )
    if action == "fonttile-fill-slots":
        return (
            "EBOOT 슬롯채우기",
            [
                "bash",
                "-lc",
                "python scripts/fonttile_text_tool.py render-korean-tile "
                "--map-output results/fonttile_korean_glyph_map.csv "
                "&& python scripts/fonttile_text_tool.py dictionary "
                "results/fonttile_text_slots.csv "
                "--output results/fonttile_text_dictionary.csv "
                "&& python scripts/apply_fonttile_translations.py "
                "--translations patch_data/fonttile_translations.csv "
                "--dictionary results/fonttile_text_dictionary.csv "
                "&& python scripts/fonttile_text_tool.py fill "
                "results/fonttile_text_slots.csv "
                "results/fonttile_text_dictionary.csv "
                "--output results/fonttile_text_slots.filled.csv",
            ],
        )
    raise ValueError(f"unknown action: {action}")


def start_job(action: str, payload: dict[str, Any]) -> Job:
    title, command = build_command(action, payload)
    job = Job(id=uuid.uuid4().hex[:10], title=title, command=command, cwd=str(ROOT))
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )
    job.process = process
    with jobs_lock:
        jobs[job.id] = job
        trim_finished_jobs_locked()
    save_job(job)
    threading.Thread(target=collect_output, args=(job,), daemon=True).start()
    return job


def close_process_handles(process: subprocess.Popen[str]) -> None:
    if process.stdout:
        try:
            process.stdout.close()
        except OSError:
            pass


def collect_output(job: Job) -> None:
    assert job.process and job.process.stdout
    process = job.process
    last_saved_at = 0.0
    returncode = 1
    try:
        for line in process.stdout:
            with jobs_lock:
                job.output.append(line.rstrip("\n"))
                if len(job.output) > 3000:
                    job.output = job.output[-3000:]
            now = time.time()
            if now - last_saved_at >= JOB_SAVE_INTERVAL_SECONDS:
                save_job(job)
                last_saved_at = now
    finally:
        returncode = process.wait()
        close_process_handles(process)
    with jobs_lock:
        job.returncode = returncode
        if job.status != "cancelled":
            job.status = "done" if returncode == 0 else "failed"
        job.finished_at = time.time()
        job.process = None
        trim_finished_jobs_locked()
    save_job(job)


def cancel_job(job_id: str) -> bool:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or not job.process or job.status != "running":
        return False
    if hasattr(os, "killpg"):
        os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
    else:
        job.process.terminate()
    with jobs_lock:
        job.status = "cancelled"
        job.finished_at = time.time()
    save_job(job)
    return True


def terminate_running_jobs(timeout: float = 5.0) -> None:
    with jobs_lock:
        running = [job for job in jobs.values() if job.process and job.status == "running"]
        for job in running:
            job.status = "cancelled"
            job.finished_at = time.time()
    for job in running:
        assert job.process is not None
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
            else:
                job.process.terminate()
        except ProcessLookupError:
            pass
    deadline = time.time() + timeout
    for job in running:
        process = job.process
        if process is None:
            continue
        remaining = max(0.0, deadline - time.time())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        finally:
            close_process_handles(process)
            with jobs_lock:
                job.process = None
            save_job(job)


def job_snapshot(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "title": job.title,
        "command": job.command,
        "cwd": job.cwd,
        "status": job.status,
        "startedAt": job.started_at,
        "finishedAt": job.finished_at,
        "returncode": job.returncode,
        "output": job.output,
    }


def project_status() -> dict[str, Any]:
    paths = [
        "ExtractedISO/PSP_GAME/USRDIR",
        "unpacked_mkd",
        "textures_static",
        "textures_translated",
        "textures_static/manifest.csv",
        "textures_pattern/text_texture_patterns.csv",
        "textures_translated/manifest.json",
        "textures_static/manifest.json",
        "rebuilt_mkd",
        "game-patched.iso",
    ]
    return {
        "root": str(ROOT),
        "python": PYTHON,
        "databaseBackend": DB_BACKEND,
        "database": database_summary(),
        "paths": [{"path": item, "exists": (ROOT / item).exists()} for item in paths],
    }


def relative_project_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def discover_asset_fonts() -> list[Path]:
    assets_dir = ROOT / "assets"
    if not assets_dir.exists():
        return []
    return sorted(
        path
        for path in assets_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in FONT_EXTENSIONS
    )


def discover_font_choices() -> list[dict[str, Any]]:
    asset_fonts = discover_asset_fonts()
    choices: list[dict[str, Any]] = [
        {
            "value": "auto",
            "label": "auto",
            "path": "",
            "exists": bool(asset_fonts),
            "defaultSize": 12,
        }
    ]
    for choice in KNOWN_FONT_CHOICES:
        choices.append({**choice, "exists": (ROOT / choice["path"]).exists()})

    known_paths = {(ROOT / choice["path"]).resolve() for choice in KNOWN_FONT_CHOICES}
    for path in asset_fonts:
        if path.resolve() in known_paths:
            continue
        rel_path = relative_project_path(path)
        choices.append(
            {
                "value": rel_path,
                "label": path.name,
                "path": rel_path,
                "exists": True,
                "defaultSize": 12,
            }
        )
    return choices
