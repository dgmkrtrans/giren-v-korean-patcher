#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -x .venv/bin/python ]]; then
  PYTHON="${PYTHON:-.venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi

source_file="ExtractedISO/PSP_GAME/USRDIR/ZZZPSP9.MKD"
output_dir="unpacked_mkd/unpacked_9"
[[ -f "$source_file" ]] || { echo "missing source archive: $source_file" >&2; exit 1; }
if [[ "${FORCE_EXTRACT:-0}" == "1" ]]; then
  rm -rf "$output_dir"
fi
"$PYTHON" scripts/extract_mkd.py "$source_file" "$output_dir"
