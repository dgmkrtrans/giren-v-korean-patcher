#!/usr/bin/env python3
"""Extract every complete PNG embedded in an ISO using the importer's index order."""

from __future__ import annotations

import argparse
import mmap
from pathlib import Path

from import_iso_files import find_pngs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", type=Path, default=Path("game-patched.iso"))
    parser.add_argument("--out", type=Path, default=Path("iso_mkd/_tail_png"))
    args = parser.parse_args()

    if not args.iso.is_file():
        raise FileNotFoundError(f"ISO not found: {args.iso}")
    args.out.mkdir(parents=True, exist_ok=True)
    with args.iso.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        pngs = find_pngs(data)
        for index, (offset, length) in enumerate(pngs):
            (args.out / f"{index:08x}.png").write_bytes(data[offset : offset + length])
    print(f"extracted {len(pngs)} ISO-wide PNGs into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
