from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import uvicorn

from .core.config import ROOT
from .core.db import DB_BACKEND, database_summary
from .main import app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gihren Korean Project Tool web server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true", help="Reload server code during backend development.")
    parser.add_argument("--no-supervisor", action="store_true", help="Run uvicorn directly without restart supervision.")
    parser.add_argument("--health-interval", type=float, default=5.0, help="Seconds between supervisor health checks.")
    parser.add_argument("--health-timeout", type=float, default=2.0, help="Seconds to wait for one health check.")
    parser.add_argument("--health-failures", type=int, default=3, help="Consecutive failed health checks before restart.")
    parser.add_argument("--restart-delay", type=float, default=1.0, help="Seconds to wait before restarting a failed server.")
    return parser.parse_args()


def run_server(args: argparse.Namespace) -> int:
    print(f"Gihren web tool: http://{args.host}:{args.port}")
    print(f"Project root: {ROOT}")
    print(f"Database: {DB_BACKEND} ({database_summary()})")
    uvicorn.run(
        "webtool.backend.app.main:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def health_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host.strip("[]")


def health_url(args: argparse.Namespace) -> str:
    host = health_host(args.host)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{args.port}/api/health"


def server_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "webtool" / "server.py"),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--no-supervisor",
    ]


def terminate_process(process: subprocess.Popen[bytes], timeout: float = 8.0) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=timeout)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def health_check(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (OSError, urllib.error.URLError):
        return False


def supervise(args: argparse.Namespace) -> int:
    url = health_url(args)
    command = server_command(args)
    print(f"Gihren web tool supervisor: http://{args.host}:{args.port}")
    print(f"Project root: {ROOT}")
    print(f"Database: {DB_BACKEND} ({database_summary()})")
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def stop_supervisor(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_supervisor)
    while True:
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            print(f"[webtool supervisor] started server pid={process.pid}")
            failures = 0
            while process.poll() is None:
                time.sleep(args.health_interval)
                if health_check(url, args.health_timeout):
                    failures = 0
                    continue
                failures += 1
                print(f"[webtool supervisor] health check failed ({failures}/{args.health_failures})")
                if failures >= args.health_failures:
                    print("[webtool supervisor] restarting unresponsive server")
                    terminate_process(process)
                    break
            returncode = process.poll()
            if returncode == 0:
                signal.signal(signal.SIGTERM, previous_sigterm)
                return 0
            print(f"[webtool supervisor] server exited with code {returncode}; restarting")
            time.sleep(args.restart_delay)
        except KeyboardInterrupt:
            if process is not None:
                terminate_process(process)
            signal.signal(signal.SIGTERM, previous_sigterm)
            return 130


def main() -> int:
    args = parse_args()
    if args.reload or args.no_supervisor:
        return run_server(args)
    return supervise(args)
