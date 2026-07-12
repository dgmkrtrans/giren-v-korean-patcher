#!/usr/bin/env python3
"""Verify user-supplied game files without distributing any game data."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checksums",
        type=Path,
        default=Path("patch_data/source_checksums.json"),
    )
    parser.add_argument("--skip-hash", action="store_true")
    args = parser.parse_args()

    data = json.loads(args.checksums.read_text(encoding="utf-8"))
    errors: list[str] = []
    for record in data.get("files", []):
        path = Path(record["path"])
        if not path.is_file():
            errors.append(f"missing: {path}")
            continue
        size = path.stat().st_size
        expected_size = int(record["size"])
        if size != expected_size:
            errors.append(f"size mismatch: {path}: {size} != {expected_size}")
            continue
        if not args.skip_hash:
            actual_hash = sha256(path)
            if actual_hash != record["sha256"]:
                errors.append(f"SHA-256 mismatch: {path}: {actual_hash}")
                continue
        print(f"OK {path}")

    if errors:
        for error in errors:
            print(f"ERROR {error}")
        return 1
    print(f"verified {len(data.get('files', []))} source files for {data.get('game_id', 'unknown game')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
