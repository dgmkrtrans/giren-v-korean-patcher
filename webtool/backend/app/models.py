from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Job:
    id: str
    title: str
    command: list[str]
    cwd: str
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    returncode: int | None = None
    output: list[str] = field(default_factory=list)
    process: subprocess.Popen[str] | None = None


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
