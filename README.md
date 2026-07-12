# giren-v-korean-patcher

PSP용 **기렌의 야망 액시즈의 위협 V**(`ULJS00178`)의 한국어 텍스처와 작은 폰트 문자열을 생성하고, 사용자가 직접 덤프한 게임 ISO에 크기 보존 방식으로 적용하는 도구 모음이다.

이 저장소는 패치 제작에 필요한 소스 코드와 번역 데이터만 제공한다. 게임 ISO, `EBOOT.BIN`, `ZZZPSP*.MKD`, 추출 리소스, 생성 PNG, 폰트 바이너리는 포함하지 않는다.

> 이 프로젝트를 사용하려면 정품 게임에서 직접 추출한 파일이 필요하다. 생성된 ISO나 게임 파일을 배포하지 말아야 한다.

## 제공하는 기능

- `ZZZPSP0.MKD`~`ZZZPSP9.MKD`의 순수 Python 해체
- `MRG`, `PSET/PSE`, `TX/PL`, `CMP0`, raw PNG 텍스처 덤프
- 공개 번역 CSV를 새로 덤프한 manifest에 안전하게 병합
- 대사, 오프닝, UI, 도감 등 한국어 텍스처 렌더링
- 작은 8x8 폰트의 초성·중성·종성 조합식 한글 렌더링
- EBOOT lookup table, 결합 mark 범위, UTF-8 UI 문자열 패치
- 슬롯을 넘는 작은 폰트 문자열의 외부 pool 배치
- SD0/CMP0 재압축과 원본 크기 보존 MKD 리빌드
- ISO9660 LBA를 바꾸지 않는 인플레이스 EBOOT/MKD/PNG 주입
- 번역·렌더·리빌드 작업을 실행하는 FastAPI/React 웹툴

## 저장소에 포함된 것

```text
giren-v-korean-patcher/
├── all_rebuild.sh                 # 전체 렌더·패치·ISO 주입
├── prepare_workspace.sh           # 최초 해체·덤프·번역 데이터 준비
├── patch_data/
│   ├── texture_translations.csv   # 텍스처 한국어 번역과 줄 길이
│   ├── fonttile_translations.csv  # 작은 폰트 한국어 번역
│   ├── iso_raw_png_translations.csv
│   └── source_checksums.json      # 지원 입력의 크기/SHA-256
├── scripts/                       # 추출, 렌더, 패치, 리빌드 도구
├── webtool/                       # FastAPI/React 웹툴 소스
├── assets/fonts/README.md         # 사용자가 준비할 폰트 목록
├── docker-compose.yml
└── requirements.txt
```

`results/`, `unpacked_mkd/`, `textures_static/`, `textures_translated/`, `rebuilt_mkd/`, `work/`, `iso_mkd/`는 실행 중 생성되며 Git에서 제외된다.

## 지원 입력

현재 패치 상수와 오프셋은 `ULJS00178`의 프로젝트 검증본만 대상으로 한다. 다른 리비전이나 수정 ISO에 그대로 적용하면 안 된다.

필수 입력은 다음과 같다.

```text
ExtractedISO/
└── PSP_GAME/
    └── USRDIR/
        ├── ZZZPSP0.MKD
        ├── ZZZPSP1.MKD
        ├── ...
        └── ZZZPSP9.MKD

results/
└── ULJS00178_EBOOT.BIN            # 복호화된 ELF EBOOT

game-patched.iso                   # 사용자가 원본 ISO에서 만든 작업 복사본
```

`results/ULJS00178_EBOOT.BIN`은 ISO 안의 암호화된 실행 파일을 단순 복사한 것이 아니라 복호화된 MIPS ELF여야 한다. 프로젝트 검증본의 크기는 `2,379,040`바이트이며 SHA-256은 다음과 같다.

```text
b8ab86c623183a336a2e9d278e976055a4685f786a6a51fabfe815ebd1d9f7e7
```

MKD별 정확한 크기와 SHA-256은 `patch_data/source_checksums.json`에 있다. 다음 명령으로 입력을 검증할 수 있다.

```bash
.venv/bin/python scripts/verify_inputs.py
```

해시가 다르면 강제로 진행하지 말고 게임 리비전과 덤프 상태를 먼저 확인한다.

## 필요한 프로그램

- Python 3.12 이상
- C++17 컴파일러
  - macOS: Xcode Command Line Tools의 `clang++`
  - Debian/Ubuntu: `g++`
- Node.js 20 이상과 npm: 웹툴 프런트엔드 빌드 시 필요
- 7-Zip 계열 도구: 소유한 ISO를 `ExtractedISO/`로 풀 때 필요
- PPSSPP: 최종 인게임 확인용

macOS 예시:

```bash
xcode-select --install
brew install python node p7zip
```

Python 환경:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

## 폰트 준비

폰트 라이선스와 저장소 크기 때문에 폰트 바이너리는 포함하지 않는다. 다음 파일을 `assets/fonts/`에 둔다.

```text
assets/fonts/
├── dalmoori.ttf
├── NanumGothic.ttf
├── NanumGothicBold.ttf
├── NanumGothicExtraBold.ttf
├── NanumMyeongjo.ttf
├── NanumMyeongjoBold.ttf
└── NanumMyeongjoExtraBold.ttf
```

파일명은 대소문자까지 맞춰야 한다. 자세한 내용은 `assets/fonts/README.md`를 참고한다.

## 1. 게임 파일 준비

### ISO 작업 복사본

`all_rebuild.sh`는 ISO를 인플레이스로 수정한다. 원본 ISO를 직접 지정하지 말고 반드시 복사본을 만든다.

```bash
cp /path/to/owned-game.iso game-patched.iso
```

반복 테스트할 때는 매번 깨끗한 원본에서 `game-patched.iso`를 다시 만드는 것이 안전하다.

### ISO 파일 트리 추출

소유한 ISO를 `ExtractedISO/`로 푼다. 예를 들어 7-Zip을 사용한다면:

```bash
7zz x /path/to/owned-game.iso -oExtractedISO
```

명령 후 `ExtractedISO/PSP_GAME/USRDIR/ZZZPSP0.MKD`가 존재해야 한다.

### 복호화 EBOOT

사용자가 합법적으로 덤프·복호화한 ELF EBOOT를 아래 경로에 둔다.

```text
results/ULJS00178_EBOOT.BIN
```

복호화 방법은 사용하는 PSP/PPSSPP 덤프 환경에 따라 다르므로 이 저장소는 복호화 도구나 게임 실행 파일을 제공하지 않는다.

## 2. 최초 작업공간 생성

필수 입력과 폰트를 준비한 뒤 실행한다.

```bash
./prepare_workspace.sh
```

이 스크립트는 다음 작업을 순서대로 수행한다.

1. EBOOT와 `ZZZPSP0`~`9`의 크기 및 SHA-256 확인
2. MKD를 `unpacked_mkd/unpacked_0`~`unpacked_9`로 해체
3. 정적 TX/PL 및 CMP0 텍스처를 `textures_static/`에 덤프
4. hash key로 `patch_data/texture_translations.csv`를 새 manifest에 병합
5. 작은 폰트 component atlas와 glyph map 생성
6. ISO 전체에서 raw PNG를 순번대로 추출하고 한국어 라벨 생성
7. 로컬 작은 폰트 dictionary를 생성하고 hash key로 한국어 번역 병합

이미 해체된 디렉터리는 기본적으로 건너뛴다. 다시 해체하려면 다음과 같이 실행한다.

```bash
FORCE_EXTRACT=1 ./prepare_workspace.sh
```

## 3. 전체 패치 빌드

```bash
./all_rebuild.sh
```

다른 ISO 파일명을 사용하려면:

```bash
TARGET_ISO=/path/to/test-copy.iso ./all_rebuild.sh
```

전체 빌드는 다음 단계를 수행한다.

1. 번역 manifest에서 한국어 텍스처 PNG 렌더
2. 수정 텍스처를 적용해 `ZZZPSP0`~`9` 리빌드
3. 작은 폰트 atlas와 byte/tile map 재생성
4. EBOOT와 archive 0에서 작은 폰트 문자열 슬롯 재추출
5. 공개 hash-key 번역을 로컬 dictionary에 병합한 뒤 슬롯에 채움
6. EBOOT lookup/결합 mark/외부 문자열 pool 및 UTF-8 UI 패치
7. archive 0의 문자열과 archive 1의 확장 폰트타일 재빌드
8. EBOOT와 MKD를 대상 ISO의 기존 LBA/크기 안에 주입
9. ISO 내부 raw PNG 라벨 주입

완료되면 대상 ISO 자체가 수정된다. 별도의 새 ISO 파일이 자동 생성되는 방식이 아니다.

## 번역 데이터 편집

### 텍스처 번역

공개 원본은 `patch_data/texture_translations.csv`다. 주요 컬럼:

- `translation_key`: 로컬 manifest 구조와 이미지 SHA-1에서 계산한 opaque SHA-256
- `korean`: 렌더할 한국어
- `dialogue_line_lengths`: 게임의 대사 줄 길이 제어값

공개 CSV에는 OCR 일본어, 원본 경로, offset, 이미지 SHA-1을 넣지 않는다. `translation_key`는 다음 값을 순서대로 NUL 구분하여 SHA-256한 값이다.

```text
domain = giren-v-texture-translation-v1\0
source\0tree_path\0offset\0sha1\0
```

`japanese` 컬럼은 사용자의 기존 로컬 manifest에 남아 있을 수 있지만 공개 데이터 생성, 식별자 계산, 병합에는 사용하지 않는다. 기존 OCR 결과의 정확도가 낮아 OCR 도구와 실행 경로도 공개 저장소에서 제외했다.

CSV를 수정한 뒤 기존 작업공간에 다시 반영하려면:

```bash
.venv/bin/python scripts/apply_texture_translations.py
```

### 작은 폰트 번역

`patch_data/fonttile_translations.csv`에는 `translation_key,korean`만 들어 있다. `prepare_workspace.sh`와 `all_rebuild.sh`가 사용자 게임에서 `results/fonttile_text_dictionary.csv`를 새로 생성한 뒤, 로컬 `original`을 hash key로 바꿔 한국어 번역을 병합한다.

```text
domain = giren-v-fonttile-translation-v1\0
original\0
```

따라서 공개 저장소에는 작은 폰트 일본어 원문, 원본 byte 또는 샘플 경로가 남지 않는다. 부분 자동 치환용 일본어 map도 사용하지 않는다.

웹툴에서 로컬 번역을 수정한 뒤 공개용 한국어 전용 CSV를 다시 만들려면:

```bash
.venv/bin/python scripts/export_public_translations.py \
  --texture-manifest textures_static/manifest.csv \
  --fonttile-dictionary results/fonttile_text_dictionary.csv
```

이 명령은 입력 파일의 원문을 읽지만 출력에는 hash key와 한국어만 기록한다.

공개 데이터에 원문 열이나 금지된 파일이 다시 들어오지 않았는지 확인한다.

```bash
.venv/bin/python scripts/audit_public_data.py
```

작은 폰트 component 구성은 다음 파일에서 관리한다.

- `scripts/tile_text/korean_font_source_texts.json`
- `scripts/tile_text/merge_text/all_merge_font_source_texts.json`

component source를 바꾸면 atlas, glyph map, lookup table, 결합 mark 범위가 함께 바뀌므로 반드시 전체 테스트를 실행한다.

## 웹툴

웹툴은 번역 CSV 편집, 그래픽 업로드 검토, 렌더, MKD 리빌드, ISO 주입 명령을 브라우저에서 실행한다.

### 로컬 SQLite 실행

```bash
cd webtool/frontend
npm ci
npm run build
cd ../..
.venv/bin/python webtool/server.py
```

브라우저에서 `http://127.0.0.1:8765`를 연다. 최초 사용자 등록 화면에서 관리자 계정을 만든다. 로컬 상태 DB는 `webtool/state.sqlite3`이며 Git에 포함되지 않는다.

### Docker + MySQL 실행

Docker 실행 전 `game-patched.iso`와 필요한 입력/작업 디렉터리를 먼저 준비한다. bind mount 대상 ISO가 없으면 Docker가 같은 이름의 디렉터리를 만들 수 있으므로 주의한다.

```bash
docker compose up --build
```

기본 주소는 `http://127.0.0.1:8765`다. 외부 공개 배포 전에는 `.env.example`을 참고해 호스트 제한, 보안 쿠키, 비밀번호와 SMTP 설정을 반드시 바꾼다.

## 개별 명령

MKD 하나 해체:

```bash
.venv/bin/python scripts/extract_mkd.py \
  ExtractedISO/PSP_GAME/USRDIR/ZZZPSP1.MKD \
  unpacked_mkd/unpacked_1
```

텍스처 전체 덤프:

```bash
.venv/bin/python scripts/dump_static_cmp0_textures.py \
  --source unpacked_mkd \
  --out textures_static \
  --clean
```

특정 archive만 리빌드:

```bash
.venv/bin/python scripts/rebuild_mkd.py \
  --archives 1 \
  --original-dir ExtractedISO/PSP_GAME/USRDIR \
  --unpacked unpacked_mkd \
  --out rebuilt_mkd \
  --apply-textures textures_translated \
  --optimal-sd0 \
  --optimal-cmp0
```

ISO에 MKD 주입:

```bash
.venv/bin/python scripts/import_mkd.py \
  --iso game-patched.iso \
  --mkd-dir rebuilt_mkd
```

## 테스트

게임 바이너리가 없어도 가능한 문법 검사:

```bash
.venv/bin/python -m py_compile $(find scripts webtool -name '*.py' -type f)
```

검증 입력을 준비한 뒤 작은 폰트 테스트:

```bash
.venv/bin/python -m unittest discover -s scripts/tile_text -p 'test_*.py'
```

프런트엔드 빌드:

```bash
cd webtool/frontend
npm ci
npm run build
```

## 안전장치

- `import_mkd.py`는 리빌드 MKD 크기가 ISO 내부 원본 크기와 다르면 중단한다.
- `import_iso_files.py`는 EBOOT 크기가 원본 ISO entry와 다르면 중단한다.
- 텍스처와 작은 폰트 번역 병합은 공개 `translation_key`를 로컬 생성 파일에서 찾지 못하면 쓰기 전에 중단한다.
- 작은 폰트 apply는 원본 byte, EBOOT 크기, relocation/pool 범위를 검증한다.
- `rebuild_mkd.py --relayout` 결과는 크기 보존 ISO 주입용으로 사용하지 않는다.

## 알려진 제한

- EBOOT 오프셋은 검증된 `ULJS00178` 실행 파일에 고정되어 있다.
- 작은 폰트에서 12바이트 unit short-name 슬롯을 넘는 이름은 `0x1f + pool offset` 간접 참조로 표시된다. 화면 출력은 정상이나, 현재 게임의 이름 색인 comparator는 간접 참조를 해석하지 않아 긴 이름들이 가나다 목록 뒤에 별도 묶음으로 나올 수 있다.
- 생성 텍스처가 원래 SD0/CMP0 슬롯보다 크면 최적 압축으로도 리빌드가 실패할 수 있다. 번역 길이, 폰트 크기 또는 렌더 규칙을 조정해야 한다.
- 최종 품질과 구동 여부는 PPSSPP 또는 실기에서 직접 확인해야 한다.

## 공개 저장소 주의사항

다음 항목은 커밋하지 않는다.

- 게임 ISO와 그 복사본
- `EBOOT.BIN`, `BOOT.BIN`, PRX, MKD, MRG, PSE
- `ExtractedISO/`, `unpacked_mkd/`, `rebuilt_mkd/`, `work/`
- 원본 및 생성 PNG/XCF
- 폰트 바이너리
- 웹툴 SQLite DB, 사용자 업로드, 인증/SMTP 비밀값
- OCR 일본어가 포함된 로컬 manifest와 작은 폰트 dictionary

`.gitignore`와 `.dockerignore`가 이를 기본 차단하지만, 공개 push 전에는 반드시 `git status`와 staged 파일 목록을 직접 확인한다.

## 라이선스

게임과 폰트의 저작권은 각각의 권리자에게 있다. 이 저장소에는 아직 코드 라이선스 파일을 선택해 넣지 않았다. 공개 게시 전에 프로젝트 소유자가 원하는 오픈소스 라이선스를 결정해 `LICENSE`를 추가해야 한다.
