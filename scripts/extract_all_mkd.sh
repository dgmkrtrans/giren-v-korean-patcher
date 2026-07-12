#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -x .venv/bin/python ]]; then
  PYTHON="${PYTHON:-.venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi

for archive in {0..9}; do
  source_file="ExtractedISO/PSP_GAME/USRDIR/ZZZPSP${archive}.MKD"
  output_dir="unpacked_mkd/unpacked_${archive}"
  if [[ ! -f "$source_file" ]]; then
    echo "missing source archive: $source_file" >&2
    exit 1
  fi
  if [[ -d "$output_dir" && -n "$(find "$output_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    if [[ "${FORCE_EXTRACT:-0}" != "1" ]]; then
      echo "skip existing $output_dir (set FORCE_EXTRACT=1 to replace it)"
      continue
    fi
    rm -rf "$output_dir"
  fi
  echo "extracting ZZZPSP${archive}.MKD"
  "$PYTHON" scripts/extract_mkd.py "$source_file" "$output_dir"
done
