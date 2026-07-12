#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -x .venv/bin/python ]]; then
  PYTHON="${PYTHON:-.venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi
TARGET_ISO="${TARGET_ISO:-game-patched.iso}"

for required in \
  "$TARGET_ISO" \
  assets/fonts/dalmoori.ttf \
  assets/fonts/NanumMyeongjoExtraBold.ttf; do
  [[ -f "$required" ]] || { echo "required file not found: $required" >&2; exit 1; }
done

"$PYTHON" scripts/verify_inputs.py
bash scripts/extract_all_mkd.sh

"$PYTHON" scripts/dump_static_cmp0_textures.py \
  --source unpacked_mkd \
  --out textures_static \
  --clean

"$PYTHON" scripts/apply_texture_translations.py \
  --patch patch_data/texture_translations.csv \
  --manifest textures_static/manifest.csv

mkdir -p results iso_mkd
cp patch_data/iso_raw_png_translations.csv iso_mkd/translate.csv

"$PYTHON" scripts/tile_text/merge_text/all_korean_fonttile.py
"$PYTHON" scripts/fonttile_text_tool.py dump \
  --output results/fonttile_text_slots.csv
"$PYTHON" scripts/fonttile_text_tool.py dictionary \
  results/fonttile_text_slots.csv \
  --output results/fonttile_text_dictionary.csv
"$PYTHON" scripts/apply_fonttile_translations.py
"$PYTHON" scripts/fonttile_text_tool.py fill \
  results/fonttile_text_slots.csv \
  results/fonttile_text_dictionary.csv \
  --output results/fonttile_text_slots.filled.csv
"$PYTHON" scripts/extract_iso_raw_pngs.py \
  --iso "$TARGET_ISO" \
  --out iso_mkd/_tail_png
"$PYTHON" scripts/iso_mkd_translate.py \
  --csv patch_data/iso_raw_png_translations.csv \
  --source iso_mkd/_tail_png \
  --out iso_mkd/translated

echo "workspace prepared"
