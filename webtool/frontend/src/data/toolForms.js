export const tabs = [
  { id: "unpack", label: "Unpack" },
  { id: "dump", label: "Dump" },
  { id: "translate", label: "텍스트번역" },
  { id: "bulk-translate", label: "일괄수정" },
  { id: "fonttile", label: "EBOOT번역" },
  { id: "graphic", label: "그래픽번역" },
  { id: "render", label: "Render" },
  { id: "rebuild", label: "Rebuild" },
  { id: "import", label: "Import" },
  { id: "pipeline", label: "Pipeline" },
  { id: "users", label: "Users", adminOnly: true },
  { id: "notice", label: "공지사항" },
  { id: "notifications", label: "알림" },
];

export const actionButtons = {
  unpack: [
    { action: "extract-all", label: "ZZZPSP0-9 해체" },
    { action: "extract-mkd9", label: "ZZZPSP9만 해체" },
  ],
};

export const formPanels = {
  dump: {
    title: "정적 텍스처 덤프",
    description: "MRG 내부 TX/PL 텍스처를 PNG와 manifest로 추출",
    action: "dump-static",
    submit: "덤프 실행",
    fields: [
      { name: "source", label: "소스 폴더", value: "unpacked_mkd" },
      { name: "out", label: "출력 폴더", value: "textures_static" },
      { name: "maxFiles", label: "최대 파일 수", type: "number", min: "1", placeholder: "비워두면 전체" },
      { name: "all", label: "전체 발견용 덤프", type: "checkbox" },
      { name: "categories", label: "검수 카테고리 폴더로 분리", type: "checkbox" },
      { name: "clean", label: "출력 폴더 정리 후 실행", type: "checkbox" },
      { name: "noDedupe", label: "중복 PNG도 모두 저장", type: "checkbox" },
      { name: "skipRawPng", label: "raw PNG 복사 생략", type: "checkbox" },
    ],
  },
  render: {
    title: "렌더",
    description: "",
    action: "render-empty",
    submit: "",
    fields: [],
  },
  rebuild: {
    title: "MKD 리빌드",
    description: "수정된 PNG와 dialogue_line_lengths를 원래 슬롯에 재인코딩하고 SD0 압축",
    action: "rebuild-mkd",
    submit: "리빌드 실행",
    fields: [
      { name: "originalDir", label: "원본 MKD 폴더", value: "ExtractedISO/PSP_GAME/USRDIR" },
      { name: "unpacked", label: "해체 폴더", value: "unpacked_mkd" },
      { name: "out", label: "출력 폴더", value: "rebuilt_mkd" },
      { name: "archives", label: "아카이브 범위", placeholder: "예: 0-8 또는 1,3,9" },
      { name: "applyTextures", label: "적용 텍스처 폴더", value: "textures_translated" },
      { name: "writeStagedUnpacked", label: "스테이징 출력", placeholder: "선택" },
      { name: "verify", label: "원본 대비 검증", type: "checkbox" },
      { name: "forceReencodeTextures", label: "텍스처 강제 재인코딩", type: "checkbox" },
      { name: "relayout", label: "relayout 사용", type: "checkbox", warning: true },
      { name: "noReuseUnchanged", label: "변경 없는 파일도 재압축", type: "checkbox" },
      { name: "optimalSd0", label: "느린 최적 SD0 fallback", type: "checkbox", warning: true },
    ],
  },
  import: {
    title: "ISO 주입",
    description: "리빌드된 MKD를 ISO 내부 동일 LBA 위치에 덮어쓰기",
    action: "import-mkd",
    submit: "ISO 주입 실행",
    fields: [
      { name: "iso", label: "ISO 파일", value: "game-patched.iso" },
      { name: "mkdDir", label: "MKD 폴더", value: "rebuilt_mkd" },
    ],
  },
  pipeline: {
    title: "원클릭 빌드",
    description: "rebuild_mkd.py --apply-textures 실행 후 import_mkd.py 실행",
    action: "one-click-build",
    submit: "리빌드 + ISO 주입",
    fields: [
      { name: "texturesDir", label: "텍스처 폴더", value: "textures_translated" },
      { name: "iso", label: "ISO 파일", value: "game-patched.iso" },
      { name: "optimalSd0", label: "리빌드 시 최적 SD0 fallback", type: "checkbox", warning: true },
    ],
  },
};

const disabledRenderSubtab = (id, label) => ({
  id,
  label,
  disabled: true,
  panel: null,
});

const renderTextFitSubtab = (id, label, targetVerifiedGroup = label) => ({
  id,
  label,
  panel: {
    title: label,
    description: "",
    action: "render-ui-text-fit",
    submit: "렌더 실행",
    payload: { targetVerifiedGroup },
    fields: [],
  },
});

export const defaultRenderSubtab = "all";

export const renderSubtabs = [
  {
    id: "opening",
    label: "각 세력 오프닝",
    panel: {
      title: "각 세력 오프닝",
      description: "",
      action: "render-white-black-background",
      submit: "렌더 실행",
      fields: [],
    },
  },
  disabledRenderSubtab("opening-title", "각 세력 오프닝타이틀"),
  renderTextFitSubtab("development-description", "개발설명"),
  renderTextFitSubtab("development-name", "개발이름"),
  renderTextFitSubtab("game-manual", "게임내 메뉴얼"),
  renderTextFitSubtab("machine-status-name", "기체 스테이터스 이름"),
  {
    id: "dialogue",
    label: "대사들",
    panel: {
      title: "대사들",
      description: "",
      action: "render-white-transparent",
      submit: "렌더 실행",
      fields: [],
    },
  },
  renderTextFitSubtab("database", "도감(DATABASE)"),
  disabledRenderSubtab("memory-card", "메모리카드"),
  disabledRenderSubtab("main-title", "메인 타이틀"),
  disabledRenderSubtab("unit-officer-count", "부대수,사관수"),
  renderTextFitSubtab("faction-leader-name", "세력 이름_지도자 이름"),
  renderTextFitSubtab("faction-name-16-flat", "세력 이름(16/납작)"),
  renderTextFitSubtab("faction-name-22", "세력 이름(22)"),
  renderTextFitSubtab("faction-name-23", "세력 이름(23)"),
  disabledRenderSubtab("faction-select-text", "세력선택 문구"),
  renderTextFitSubtab("system-message", "시스템 메시지"),
  disabledRenderSubtab("ending-text", "엔딩 텍스트"),
  renderTextFitSubtab("unit-status-name", "유닛 스테이터스 이름"),
  disabledRenderSubtab("small-font", "작은폰트"),
  renderTextFitSubtab("enemy-calculating", "적 연산중"),
  {
    id: "all",
    label: "전체",
    panel: {
      title: "전체",
      description: "",
      action: "render-all-categories",
      submit: "렌더 실행",
      fields: [],
    },
  },
  renderTextFitSubtab("leader-name", "지도자 이름"),
  renderTextFitSubtab("area-name", "지역이름"),
  disabledRenderSubtab("progress", "진행"),
  renderTextFitSubtab("title", "칭호"),
  renderTextFitSubtab("special-plan", "특별플랜"),
  renderTextFitSubtab("ui-14", "UI(14)"),
  renderTextFitSubtab("ui-15", "UI(15)"),
  disabledRenderSubtab("ui-16-flat", "UI(16/납작)"),
  renderTextFitSubtab("ui-16-left", "UI(16/좌)"),
  renderTextFitSubtab("ui-17-left", "UI(17/좌)"),
  renderTextFitSubtab("ui-17-bw-center", "UI(17/bw/중앙)"),
  renderTextFitSubtab("ui-17-w-right", "UI(17/w/오)"),
  renderTextFitSubtab("ui-17-wy-center", "UI(17/wy/중앙)"),
  renderTextFitSubtab("ui-17g", "UI(17g)"),
  renderTextFitSubtab("ui-20", "UI(20)"),
  renderTextFitSubtab("ui-promotion", "UI(승격)"),
  renderTextFitSubtab("ui-extra-name", "UI(외,명)"),
  renderTextFitSubtab("ui-money-resource-14", "UI(자금자원14)"),
  renderTextFitSubtab("ui-unit-aptitude", "UI(유닛적성)"),
  renderTextFitSubtab("ui-unit-status", "UI(유닛스테이터스)"),
];

const allFormPanels = [
  ...Object.values(formPanels),
  ...renderSubtabs.map((tab) => tab.panel).filter(Boolean),
];

export function defaultForms() {
  return Object.fromEntries(
    allFormPanels.map((panel) => [
      panel.action,
      defaultPanelValues(panel),
    ]),
  );
}

export function defaultPanelValues(panel) {
  return Object.fromEntries(panel.fields.map((field) => [field.name, field.type === "checkbox" ? false : field.value || ""]));
}

const legacyTranslatedRoots = new Set([
  "translated_textures",
  "translated_textures_white",
  "translated_textures_opening",
]);

function normalizeTextureRoot(value) {
  return legacyTranslatedRoots.has(value) ? "textures_translated" : value;
}

export function normalizeStoredForms(storedForms) {
  if (!storedForms || typeof storedForms !== "object") {
    return storedForms;
  }
  const normalized = {};
  for (const [action, values] of Object.entries(storedForms)) {
    if (!values || typeof values !== "object") {
      normalized[action] = values;
      continue;
    }
    const next = { ...values };
    if (
      action === "render-textures"
      || action === "render-all-categories"
      || action === "render-white-transparent"
      || action === "render-white-black-background"
      || action === "render-ui-text-fit"
    ) {
      next.outRoot = normalizeTextureRoot(next.outRoot);
    }
    if (action === "rebuild-mkd") {
      next.applyTextures = normalizeTextureRoot(next.applyTextures);
    }
    if (action === "one-click-build") {
      next.texturesDir = normalizeTextureRoot(next.texturesDir);
    }
    normalized[action] = next;
  }
  return normalized;
}
