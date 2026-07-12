#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -x .venv/bin/python ]]; then
  PYTHON="${PYTHON:-.venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi
TARGET_ISO="${TARGET_ISO:-game-patched.iso}"

require_file() {
  [[ -f "$1" ]] || { echo "required file not found: $1" >&2; exit 1; }
}

require_file "$TARGET_ISO"
require_file results/ULJS00178_EBOOT.BIN
require_file textures_static/manifest.csv
require_file iso_mkd/_tail_png/00000003.png
require_file assets/fonts/dalmoori.ttf
for font in \
  NanumGothic.ttf \
  NanumGothicBold.ttf \
  NanumGothicExtraBold.ttf \
  NanumMyeongjo.ttf \
  NanumMyeongjoBold.ttf \
  NanumMyeongjoExtraBold.ttf; do
  require_file "assets/fonts/$font"
done

"$PYTHON" scripts/verify_inputs.py

mkdir -p results rebuilt_mkd work iso_mkd

"$PYTHON" scripts/tile_text/merge_text/all_korean_fonttile.py

"$PYTHON" scripts/txt_gen/render_all_categories.py \
  --csv textures_static/manifest.csv \
  --textures-root textures_static \
  --out-root textures_translated

"$PYTHON" scripts/rebuild_mkd.py \
  --original-dir ExtractedISO/PSP_GAME/USRDIR \
  --unpacked unpacked_mkd \
  --out rebuilt_mkd \
  --apply-textures textures_translated \
  --optimal-sd0 \
  --optimal-cmp0

"$PYTHON" scripts/fonttile_text_tool.py render-korean-tile \
  --map-output results/fonttile_korean_glyph_map.csv

"$PYTHON" scripts/fonttile_text_tool.py dump \
  --output results/fonttile_text_slots.csv

"$PYTHON" scripts/fonttile_text_tool.py dictionary \
  results/fonttile_text_slots.csv \
  --output results/fonttile_text_dictionary.csv

"$PYTHON" scripts/apply_fonttile_translations.py \
  --translations patch_data/fonttile_translations.csv \
  --dictionary results/fonttile_text_dictionary.csv

"$PYTHON" scripts/fonttile_text_tool.py fill \
  results/fonttile_text_slots.csv \
  results/fonttile_text_dictionary.csv \
  --output results/fonttile_text_slots.filled.csv

rm -rf work/fonttile_patch
mkdir -p work/fonttile_patch
cp -R unpacked_mkd work/fonttile_patch/unpacked_mkd

"$PYTHON" scripts/fonttile_text_tool.py apply \
  results/fonttile_text_slots.filled.csv \
  --out-root work/fonttile_patch \
  --patch-korean-font-lookup \
  --relocated-external-pool

"$PYTHON" scripts/fonttile_text_tool.py utf8 \
  --eboot work/fonttile_patch/results/ULJS00178_EBOOT.BIN \
  --in-place

"$PYTHON" scripts/import_iso_files.py \
  --iso "$TARGET_ISO" \
  --file PSP_GAME/SYSDIR/EBOOT.BIN=work/fonttile_patch/results/ULJS00178_EBOOT.BIN

"$PYTHON" scripts/rebuild_mkd.py \
  --archives 0 \
  --unpacked work/fonttile_patch/unpacked_mkd \
  --baseline-unpacked unpacked_mkd \
  --out rebuilt_mkd \
  --optimal-sd0

"$PYTHON" scripts/rebuild_mkd.py \
  --archives 1 \
  --unpacked unpacked_mkd \
  --out rebuilt_mkd \
  --apply-textures textures_translated \
  --optimal-sd0 \
  --optimal-cmp0

"$PYTHON" scripts/import_mkd.py \
  --iso "$TARGET_ISO" \
  --mkd-dir rebuilt_mkd

"$PYTHON" scripts/iso_mkd_translate.py \
  --csv patch_data/iso_raw_png_translations.csv \
  --source iso_mkd/_tail_png \
  --out iso_mkd/translated

"$PYTHON" scripts/import_iso_files.py \
  --iso "$TARGET_ISO" \
  --raw-png-dir iso_mkd/translated

echo "completed: $TARGET_ISO"
