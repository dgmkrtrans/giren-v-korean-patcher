import { useEffect, useMemo, useRef, useState } from "react";
import { Bell, ChevronLeft, ChevronRight, GitMerge, Inbox, RefreshCw, RotateCcw, Save, Search, Trash2 } from "lucide-react";

import { api, encodeQuery } from "../api/client.js";
import { SectionHead } from "./ToolPanels.jsx";


const csvPageSize = 15;

const defaultTranslationParams = {
  folder: "textures_static",
  search: "",
  showImages: true,
  showTranslatedImages: true,
  blackBg: true,
};

const overflowTargetGroups = new Set(["각 세력 오프닝"]);
const openingHalfCellChars = new Set([" ", ".", ",", "!", "，", "．", "！","A","B","C","D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z","a","b","c","d","e","f","g","h","i","j","k","l","m","n","o","p","q","r","s","t","u","v","x","y","z","1","2","3","4","5","6","7","8","9","0"]);

function normalizeTextLines(value) {
  return String(value || "")
    .normalize("NFC")
    .replaceAll("\r\n", "\n")
    .replaceAll("\r", "\n")
    .replaceAll("　", " ")
    .split("\n")
    .map((line) => line.trim().split(/\s+/).filter(Boolean).join(" "))
    .filter(Boolean);
}

function lineCellUnits(line, group) {
  if (group !== "각 세력 오프닝") {
    return Array.from(line).length * 2;
  }
  return Array.from(line).reduce((sum, char) => sum + (openingHalfCellChars.has(char) ? 1 : 2), 0);
}

function logicalLengthFromUnits(units) {
  return units / 2;
}

function formatLogicalLength(value) {
  const number = Number(value || 0);
  return Number.isInteger(number) ? String(number) : number.toFixed(1);
}

function normalizedTextLength(value, group = "") {
  const lines = normalizeTextLines(value);
  if (group !== "각 세력 오프닝") {
    return lines.reduce((sum, line) => sum + Array.from(line).length, 0);
  }
  return logicalLengthFromUnits(lines.reduce((sum, line) => sum + lineCellUnits(line, group), 0));
}

function groupLineLimit(group) {
  if (group === "대사들") {
    return 21;
  }
  if (group === "각 세력 오프닝") {
    return 26;
  }
  return 0;
}

function groupLineMax(group) {
  if (group === "각 세력 오프닝") {
    return 2;
  }
  return 0;
}

function getRowWarningState(row) {
  const group = row.verifiedGroup || "";
  const liveKoreanLength = normalizedTextLength(row.korean || "", group);
  const showOverflow = overflowTargetGroups.has(row.verifiedGroup) && Number(row.textCapacity || 0) > 0;
  const liveLines = normalizeTextLines(row.korean || "");
  const liveLineLimit = groupLineLimit(group);
  const liveLineMax = groupLineMax(group);
  const liveLongestLine = liveLines.reduce((max, line) => Math.max(max, lineCellUnits(line, group)), 0);
  const lineWarnings = liveLineLimit > 0
    ? liveLines.map((line, index) => ({
      index,
      length: logicalLengthFromUnits(lineCellUnits(line, group)),
      limit: liveLineLimit,
      hasOverflow: lineCellUnits(line, group) > liveLineLimit * 2,
    }))
    : [];
  const hasTextOverflow = showOverflow && liveKoreanLength > Number(row.textCapacity || 0);
  const hasLineOverflow = liveLineLimit > 0 && liveLongestLine > liveLineLimit * 2;
  const hasLineCountOverflow = liveLineMax > 0 && liveLines.length > liveLineMax;
  return {
    group,
    liveKoreanLength,
    liveLines,
    showOverflow,
    liveLineLimit,
    liveLineMax,
    liveLongestLine,
    lineWarnings,
    hasTextOverflow,
    hasLineOverflow,
    hasLineCountOverflow,
    hasWarning: hasTextOverflow || hasLineOverflow || hasLineCountOverflow,
  };
}

function renderKoreanHighlightSegments(value, warningState) {
  const text = String(value || "").replaceAll("\r\n", "\n").replaceAll("\r", "\n");
  const segments = text.split("\n");
  const lineLimit = warningState.liveLineLimit;
  const lineMax = warningState.liveLineMax;

  return segments.map((line, index) => {
    const lineOverflow = lineLimit > 0 && lineCellUnits(line, warningState.group) > lineLimit * 2;
    const lineCountOverflow = lineMax > 0 && index >= lineMax;
    let className = "";
    if (warningState.hasTextOverflow) {
      className = "overflow-text";
    } else if (lineOverflow || lineCountOverflow) {
      className = "overflow-line";
    }
    return (
      <span key={`${index}-${line}`} className={className}>
        {line || " "}
        {index < segments.length - 1 ? "\n" : ""}
      </span>
    );
  });
}


function warningTextClassName(warningState) {
  if (warningState.hasTextOverflow) {
    return "translation-warning-text";
  }
  if (warningState.hasLineOverflow || warningState.hasLineCountOverflow) {
    return "translation-warning-line";
  }
  return "";
}


function rowChangeKey(row) {
  return row?.sha1 || String(row?.rowNumber || "");
}


function changeKey(item) {
  return item?.sha1 || String(item?.rowNumber || "");
}


function hasChangedKorean(item, previousRow) {
  if (!previousRow) {
    return true;
  }
  return String(item?.korean || "") !== String(previousRow.korean || "");
}


function formatDraftTime(value) {
  if (!value) {
    return "";
  }
  return new Date(Number(value) * 1000).toLocaleString();
}


const fieldLabels = {
  japanese: "일본어",
  korean: "한국어",
  dialogueLineLengths: "줄 길이",
  sha1: "sha1",
};


export function TranslationPanel({ canEdit, isAdmin, onDirtyState }) {
  const [folder, setFolder] = useState(defaultTranslationParams.folder);
  const [search, setSearch] = useState(defaultTranslationParams.search);
  const [showImages, setShowImages] = useState(defaultTranslationParams.showImages);
  const [showTranslatedImages, setShowTranslatedImages] = useState(defaultTranslationParams.showTranslatedImages);
  const [blackBg, setBlackBg] = useState(defaultTranslationParams.blackBg);
  const [selectedGroup, setSelectedGroup] = useState("");
  const [data, setData] = useState({
    page: 1,
    totalPages: 1,
    totalRows: 0,
    allRows: 0,
    pageSize: csvPageSize,
    rows: [],
    csvPath: "",
    groups: [],
    selectedGroup: "",
    draftCount: 0,
    overflowCount: 0,
    firstOverflowPage: 0,
    lineOverflowCount: 0,
    firstLineOverflowPage: 0,
  });
  const [changes, setChanges] = useState({});
  const [tempPageSize, setTempPageSize] = useState(15);

  useEffect(() => {
    if (data.pageSize) {
      setTempPageSize(data.pageSize);
    }
  }, [data.pageSize]);
  const [draftPanelOpen, setDraftPanelOpen] = useState(false);
  const [draftData, setDraftData] = useState({ total: 0, mergeable: 0, conflicts: 0, alreadyApplied: 0, drafts: [] });
  const [statusText, setStatusText] = useState("");
  const [isError, setIsError] = useState(false);
  const autoLoadedForViewer = useRef(false);
  const changesRef = useRef(changes);

  const mergedRows = useMemo(
    () => data.rows.map((row) => ({ ...row, ...(changes[rowChangeKey(row)] || {}) })),
    [data.rows, changes],
  );
  const submittableChangeCount = useMemo(() => {
    const rowsByKey = new Map(data.rows.map((row) => [rowChangeKey(row), row]));
    return Object.values(changes).filter((item) => hasChangedKorean(item, rowsByKey.get(changeKey(item)))).length;
  }, [changes, data.rows]);

  function setMessage(message, error = false) {
    setStatusText(message);
    setIsError(error);
  }

  useEffect(() => {
    changesRef.current = changes;
  }, [changes]);

  async function loadPage(page = 1) {
    setMessage("CSV를 읽는 중...");
    const response = await api(`/api/translation?${encodeQuery({ folder, page, showImages, showTranslatedImages, group: selectedGroup })}`);
    setData(response);
    setMessage(`페이지당 ${response.pageSize}행만 표시합니다. 저장 전 변경 ${Object.keys(changes).length}개.`);
  }

  async function loadDrafts() {
    if (!isAdmin) {
      return;
    }
    const response = await api(`/api/translation/drafts?${encodeQuery({ folder })}`);
    setDraftData(response);
  }

  async function saveChanges({ reload = true } = {}) {
    const rowsByKey = new Map(data.rows.map((row) => [rowChangeKey(row), row]));
    const candidates = Object.values(changesRef.current);
    const items = candidates.filter((item) => hasChangedKorean(item, rowsByKey.get(changeKey(item))));
    const skipped = candidates.length - items.length;
    if (!items.length) {
      setMessage(skipped ? "번역문이 이전과 같은 행은 제출하지 않았습니다." : "저장할 변경 사항이 없습니다.");
      setChanges({});
      return { savedDrafts: 0, saved: 0 };
    }
    setMessage("초안 저장 중...");
    const saveResponse = await api("/api/translation/save", {
      method: "POST",
      body: JSON.stringify({ folder: data.csvPath ? folder : "textures_static", changes: items }),
    });
    setChanges({});
    if (reload) {
      await loadPage(data.page);
    }
    if (draftPanelOpen) {
      await loadDrafts();
    }
    const skippedText = skipped ? ` 번역문이 이전과 같은 ${skipped.toLocaleString()}행은 제외했습니다.` : "";
    setMessage(`${saveResponse.savedDrafts ?? saveResponse.saved}개 초안을 저장했습니다.${skippedText} 메인 CSV 반영은 관리자 병합함에서 진행됩니다.`);
    return saveResponse;
  }

  async function goToPage(page) {
    const targetPage = Math.max(1, Math.min(Number(data.totalPages || 1), Number(page || 1)));
    const saveResponse = await saveChanges({ reload: false });
    await loadPage(targetPage);
    const saved = Number(saveResponse.savedDrafts ?? saveResponse.saved ?? 0);
    if (saved > 0) {
      setMessage(`${saved.toLocaleString()}개 초안을 저장하고 ${targetPage}페이지로 이동했습니다.`);
    }
  }

  async function applyDrafts(items = [], forceConflicts = false, note = "") {
    setMessage(forceConflicts ? "충돌 초안 강제 반영 중..." : "3-way 자동 병합 중...");
    const response = await api("/api/translation/drafts/apply", {
      method: "POST",
      body: JSON.stringify({ folder, items, forceConflicts, note }),
    });
    await loadDrafts();
    await loadPage(data.page);
    const conflictText = response.conflicts?.length ? ` 충돌 ${response.conflicts.length.toLocaleString()}건은 보류.` : "";
    setMessage(`${response.applied.toLocaleString()}개 초안 반영, ${response.changedRows.toLocaleString()}행 갱신.${conflictText}`);
  }

  async function discardDrafts(items, note = "") {
    if (!items.length) {
      return;
    }
    setMessage("초안 삭제 중...");
    const response = await api("/api/translation/drafts/discard", {
      method: "POST",
      body: JSON.stringify({ folder, items, note }),
    });
    setChanges((current) => {
      const next = { ...current };
      items.forEach((item) => delete next[item.sha1]);
      return next;
    });
    if (draftPanelOpen) {
      await loadDrafts();
    }
    await loadPage(data.page);
    setMessage(`${response.deleted.toLocaleString()}개 초안을 삭제했습니다.`);
  }

  async function sendNotifications() {
    setMessage("알림 전송 중...");
    const response = await api("/api/translation/notifications/send", { method: "POST" });
    setMessage(`${Number(response.notifications || 0).toLocaleString()}명에게 ${Number(response.items || 0).toLocaleString()}건의 알림을 전송했습니다.`);
  }

  async function searchRows() {
    if (!search.trim()) {
      setMessage("검색어를 입력하세요.", true);
      return;
    }
    setMessage("검색 중...");
    const response = await api(`/api/translation/search?${encodeQuery({ folder, q: search.trim(), group: selectedGroup })}`);
    if (!response.found) {
      setMessage(response.error || "검색 결과가 없습니다.", true);
      return;
    }
    await goToPage(response.page);
    setMessage(`행 ${response.rowNumber}이 포함된 ${response.page}페이지로 이동했습니다.`);
  }

  function updateRow(rowNumber, field, value) {
    const source = mergedRows.find((row) => row.rowNumber === rowNumber);
    const original = data.rows.find((row) => row.rowNumber === rowNumber);
    const key = rowChangeKey(source);
    setChanges((current) => {
      const nextItem = {
        rowNumber,
        sha1: source?.sha1 || "",
        previousKorean: original?.korean || "",
        japanese: source?.japanese || "",
        korean: source?.korean || "",
        dialogueLineLengths: source?.dialogueLineLengths || "",
        [field]: value,
      };
      const next = { ...current };
      if (hasChangedKorean(nextItem, original)) {
        next[key] = nextItem;
      } else {
        delete next[key];
      }
      return next;
    });
    onDirtyState();
  }

  function resetParams() {
    setFolder(defaultTranslationParams.folder);
    setSearch(defaultTranslationParams.search);
    setShowImages(defaultTranslationParams.showImages);
    setShowTranslatedImages(defaultTranslationParams.showTranslatedImages);
    setBlackBg(defaultTranslationParams.blackBg);
    setSelectedGroup("");
    setMessage("기본값으로 되돌렸습니다.");
  }

  async function revertRow(row) {
    const key = rowChangeKey(row);
    setChanges((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
    if (row.hasDraft) {
      await discardDrafts([{ sha1: row.sha1 }]);
      return;
    }
    setMessage(`행 ${row.rowNumber}의 편집 내용을 되돌렸습니다.`);
  }

  async function revertPageRows() {
    const rowKeys = new Set(data.rows.map((row) => rowChangeKey(row)));
    const drafts = data.rows.filter((row) => row.hasDraft).map((row) => ({ sha1: row.sha1 }));
    setChanges((current) => {
      const next = { ...current };
      rowKeys.forEach((key) => delete next[key]);
      return next;
    });
    if (drafts.length) {
      await discardDrafts(drafts);
      return;
    }
    setMessage("현재 페이지의 편집 내용을 되돌렸습니다.");
  }

  async function applyPageSize(newSize) {
    const size = Math.max(1, Math.min(1000, Number(newSize || 15)));
    setMessage("페이지 설정 변경 중...");
    try {
      await api("/api/state", {
        method: "POST",
        body: JSON.stringify({ translationPageSize: size }),
      });
      await goToPage(1);
    } catch (err) {
      setMessage(err.message, true);
    }
  }

  useEffect(() => {
    if (data.csvPath) {
      loadPage(data.page).catch((err) => setMessage(err.message, true));
    }
  }, [showImages, showTranslatedImages]);

  useEffect(() => {
    if (isAdmin || autoLoadedForViewer.current) {
      return;
    }
    autoLoadedForViewer.current = true;
    loadPage(1).catch((err) => setMessage(err.message, true));
  }, [isAdmin]);

  function selectGroup(group) {
    setSelectedGroup(group);
    if (data.csvPath) {
      setMessage("그룹을 전환하는 중...");
    }
  }

  useEffect(() => {
    if (data.csvPath) {
      loadPage(1).catch((err) => setMessage(err.message, true));
    }
  }, [selectedGroup]);

  return (
    <>
      <SectionHead title="텍스트 번역" description="CSV의 일본어/한국어와 대사 줄 길이 제어값을 페이지 단위로 편집하고 원본/생성본을 비교" />
      <form className="form-grid" onSubmit={(event) => event.preventDefault()}>
        {isAdmin ? <label>CSV 폴더 또는 파일<input value={folder} onChange={(event) => setFolder(event.target.value)} /></label> : null}
        <label>검색
          <input value={search} onChange={(event) => setSearch(event.target.value)} onKeyDown={(event) => event.key === "Enter" && searchRows()} />
          <button type="button" className="inline-field-button" onClick={searchRows}><Search size={16} />검색 이동</button>
        </label>
        <label>페이지당 개수
          <input
            type="number"
            min="1"
            max="1000"
            value={tempPageSize}
            onChange={(event) => setTempPageSize(Number(event.target.value))}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                applyPageSize(tempPageSize);
              }
            }}
            onBlur={() => applyPageSize(tempPageSize)}
          />
        </label>
        <label className="check"><input type="checkbox" checked={showImages} onChange={(event) => setShowImages(event.target.checked)} /> 이미지 표시</label>
        <label className="check"><input type="checkbox" checked={showTranslatedImages} onChange={(event) => setShowTranslatedImages(event.target.checked)} /> 번역된 이미지 표시</label>
        <label className="check"><input type="checkbox" checked={blackBg} onChange={(event) => setBlackBg(event.target.checked)} /> 이미지 검은 배경</label>
        <div className="actions full">
          <button type="button" onClick={() => loadPage(1)}><RefreshCw size={16} />읽기</button>
          <button type="button" disabled={!canEdit || !submittableChangeCount} onClick={() => saveChanges().catch((err) => setMessage(err.message, true))}><Save size={16} />초안 저장</button>
          {isAdmin ? (
            <button
              type="button"
              className="secondary"
              onClick={() => {
                const nextOpen = !draftPanelOpen;
                setDraftPanelOpen(nextOpen);
                if (nextOpen) {
                  loadDrafts().catch((err) => setMessage(err.message, true));
                }
              }}
            >
              <Inbox size={16} />병합함
            </button>
          ) : null}
          <button type="button" className="secondary" onClick={resetParams}><RotateCcw size={16} />옵션 기본값</button>
        </div>
      </form>
      {draftPanelOpen && isAdmin ? (
        <DraftMergePanel
          data={draftData}
          blackBg={blackBg}
          onRefresh={loadDrafts}
          onApplyClean={(items) => applyDrafts(items, false)}
          onApplyDraft={(draft, forceConflicts = false) => applyDrafts([draft], forceConflicts)}
          onDiscard={(draft) => discardDrafts([draft])}
          onSendNotifications={sendNotifications}
        />
      ) : null}
      {data.overflowCount > 0 || data.lineOverflowCount > 0 ? (
        <div className="warning-jump-actions">
          {data.overflowCount > 0 ? (
            <button type="button" className="warning-jump-button warning-jump-button-text" onClick={() => goToPage(data.firstOverflowPage || 1).catch((err) => setMessage(err.message, true))}>
              <Search size={16} />
              텍스트 초과 {data.overflowCount.toLocaleString()}건
            </button>
          ) : null}
          {data.lineOverflowCount > 0 ? (
            <button type="button" className="warning-jump-button warning-jump-button-line" onClick={() => goToPage(data.firstLineOverflowPage || 1).catch((err) => setMessage(err.message, true))}>
              <Search size={16} />
              줄 길이 초과 {data.lineOverflowCount.toLocaleString()}건
            </button>
          ) : null}
        </div>
      ) : null}
      <TranslationGroupTabs
        groups={data.groups}
        selectedGroup={selectedGroup}
        totalRows={data.allRows}
        onSelect={selectGroup}
      />
      <PaginationToolbar
        data={data}
        setData={setData}
        loadPage={goToPage}
        canEdit={canEdit}
        hasPageChanges={Boolean(Object.keys(changes).length || data.rows.some((row) => row.hasDraft))}
        revertPageRows={() => revertPageRows().catch((err) => setMessage(err.message, true))}
      />
      <div className={`translation-status ${isError ? "missing" : ""}`}>{statusText}</div>
      <div className="translation-rows">
        {mergedRows.length ? mergedRows.map((row) => (
          <TranslationRow
            key={row.rowNumber}
            row={row}
            canEdit={canEdit}
            showImages={showImages}
            showTranslatedImages={showTranslatedImages}
            blackBg={blackBg}
            isAdmin={isAdmin}
            updateRow={updateRow}
            discardDraft={(row) => discardDrafts([{ sha1: row.sha1 }])}
            revertRow={(row) => revertRow(row)}
            hasLocalChange={Boolean(changes[rowChangeKey(row)])}
          />
        )) : <div className="translation-status">표시할 행이 없습니다.</div>}
      </div>
      <PaginationToolbar
        data={data}
        setData={setData}
        loadPage={goToPage}
        canEdit={canEdit}
        hasPageChanges={Boolean(Object.keys(changes).length || data.rows.some((row) => row.hasDraft))}
        revertPageRows={() => revertPageRows().catch((err) => setMessage(err.message, true))}
      />
    </>
  );
}


function DraftMergePanel({ data, blackBg, onRefresh, onApplyClean, onApplyDraft, onDiscard, onSendNotifications }) {
  const [draftEdits, setDraftEdits] = useState({});
  const [draftNotes, setDraftNotes] = useState({});
  const [conflictFilter, setConflictFilter] = useState("all");
  const [userFilter, setUserFilter] = useState("");

  useEffect(() => {
    setDraftEdits({});
    setDraftNotes({});
  }, [data]);

  const draftUsers = useMemo(() => {
    const users = new Map();
    for (const draft of data.drafts || []) {
      if (!draft.userId || users.has(draft.userId)) {
        continue;
      }
      users.set(draft.userId, draft.username || draft.userId);
    }
    return Array.from(users, ([id, name]) => ({ id, name }))
      .sort((left, right) => left.name.localeCompare(right.name, "ko"));
  }, [data.drafts]);

  const filteredDrafts = useMemo(() => {
    return (data.drafts || []).filter((draft) => {
      if (conflictFilter === "conflict" && !draft.hasConflict) {
        return false;
      }
      if (conflictFilter === "clean" && draft.hasConflict) {
        return false;
      }
      if (userFilter && draft.userId !== userFilter) {
        return false;
      }
      return true;
    });
  }, [conflictFilter, data.drafts, userFilter]);

  const filteredMergeableCount = filteredDrafts.filter((draft) => draft.mergeable).length;

  function draftEditKey(draft) {
    return `${draft.sha1}-${draft.userId}`;
  }

  function submittedKorean(draft) {
    return draft.draft?.korean || "";
  }

  function editedKorean(draft) {
    const key = draftEditKey(draft);
    return Object.prototype.hasOwnProperty.call(draftEdits, key) ? draftEdits[key] : submittedKorean(draft);
  }

  function isEdited(draft) {
    return editedKorean(draft) !== submittedKorean(draft);
  }

  function draftNote(draft) {
    return draftNotes[draftEditKey(draft)] || "";
  }

  function withDraftNote(draft, payload) {
    return {
      ...payload,
      note: draftNote(draft),
    };
  }

  function applyPayload(draft) {
    const base = { sha1: draft.sha1, userId: draft.userId };
    if (!isEdited(draft)) {
      return withDraftNote(draft, base);
    }
    return {
      ...withDraftNote(draft, base),
      draft: { korean: editedKorean(draft) },
    };
  }

  function discardPayload(draft) {
    return withDraftNote(draft, { sha1: draft.sha1, userId: draft.userId });
  }

  function applyCleanDrafts() {
    const items = filteredDrafts
      .filter((draft) => draft.mergeable)
      .map((draft) => applyPayload(draft));
    onApplyClean(items);
  }

  return (
    <section className="draft-merge-panel">
      <div className="draft-merge-head">
        <div>
          <h2>관리자 병합함</h2>
          <p>
            전체 {Number(data.total || 0).toLocaleString()} · 자동 {Number(data.mergeable || 0).toLocaleString()} · 충돌 {Number(data.conflicts || 0).toLocaleString()}
            {filteredDrafts.length !== Number(data.total || 0) ? ` · 표시 ${filteredDrafts.length.toLocaleString()}` : ""}
          </p>
        </div>
        <div className="actions">
          <button type="button" className="secondary" onClick={onRefresh}><RefreshCw size={16} />새로고침</button>
          <button type="button" className="secondary" onClick={onSendNotifications}><Bell size={16} />알림전송</button>
          <button type="button" disabled={!filteredMergeableCount} onClick={applyCleanDrafts}><GitMerge size={16} />자동 병합 반영</button>
        </div>
      </div>
      <div className="draft-filter-bar">
        <div className="draft-filter-group" role="group" aria-label="충돌 상태 필터">
          {[
            ["all", "전체"],
            ["conflict", "충돌"],
            ["clean", "비충돌"],
          ].map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={conflictFilter === value ? "active" : ""}
              onClick={() => setConflictFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="draft-user-filter">
          <span>제출자</span>
          <select value={userFilter} onChange={(event) => setUserFilter(event.target.value)}>
            <option value="">전체</option>
            {draftUsers.map((user) => (
              <option key={user.id} value={user.id}>{user.name}</option>
            ))}
          </select>
        </label>
      </div>
      <div className="draft-list">
        {filteredDrafts.length ? filteredDrafts.map((draft) => {
          const editKey = draftEditKey(draft);
          const hasAdminEdit = isEdited(draft);
          const statusText = draft.hasConflict
            ? `${draft.queueConflictFields?.length ? "대기열 충돌" : "충돌"}: ${draft.conflictFields.map((field) => fieldLabels[field] || field).join(", ")}`
            : draft.alreadyApplied ? "이미 반영됨" : draft.hasChanges ? "자동 병합 가능" : "변경 없음";
          const statusClassName = draft.hasConflict ? "translation-warning-text" : "ok";
          const currentWarningClassName = warningTextClassName(getRowWarningState({ ...draft, korean: draft.current?.korean || "" }));
          const draftWarningClassName = warningTextClassName(getRowWarningState({ ...draft, korean: editedKorean(draft) }));
          return (
            <article key={editKey} className={`draft-item ${draft.hasConflict ? "conflict" : ""}`}>
              <div className="draft-meta">
                <strong>{draft.username}</strong>
                <span className={statusClassName}>{statusText}</span>
                <span className="draft-meta-line">{draft.verifiedGroup || "미분류"}</span>
                <span className="draft-meta-line">중복 {Number(draft.duplicateCount || 0).toLocaleString()}행</span>
                <span>{formatDraftTime(draft.updatedAt)}</span>
              </div>
              <div className="draft-image">
                <ImageBox label="원본" url={draft.imageUrl} blackBg={blackBg} />
              </div>
              <div className="draft-fields">
                <div><b>CSV</b><p className={currentWarningClassName}>{draft.current?.korean || ""}</p></div>
                <label className="draft-edit-field">
                  <b>초안</b>
                  <textarea
                    value={editedKorean(draft)}
                    className={`${hasAdminEdit ? "admin-edited" : ""} ${draftWarningClassName}`.trim()}
                    onChange={(event) => setDraftEdits((current) => ({ ...current, [editKey]: event.target.value }))}
                  />
                </label>
                <label className="draft-note-field">
                  <b>비고</b>
                  <textarea
                    value={draftNote(draft)}
                    rows={3}
                    onChange={(event) => setDraftNotes((current) => ({ ...current, [editKey]: event.target.value }))}
                    placeholder="이 텍스트의 병합/삭제 알림에 남길 비고"
                  />
                </label>
              </div>
              <div className="draft-actions">
                <button type="button" disabled={draft.hasConflict} onClick={() => onApplyDraft(applyPayload(draft), false)}><GitMerge size={15} />반영</button>
                <button type="button" className="secondary" onClick={() => onApplyDraft(applyPayload(draft), true)}><GitMerge size={15} />강제 반영</button>
                <button
                  type="button"
                  className="secondary"
                  disabled={!hasAdminEdit}
                  onClick={() => setDraftEdits((current) => ({ ...current, [editKey]: submittedKorean(draft) }))}
                >
                  <RotateCcw size={15} />원본되돌리기
                </button>
                <button type="button" className="secondary" onClick={() => onDiscard(discardPayload(draft))}><Trash2 size={15} />삭제</button>
              </div>
            </article>
          );
        }) : <div className="translation-status">{data.drafts?.length ? "필터 조건에 맞는 초안이 없습니다." : "대기 중인 초안이 없습니다."}</div>}
      </div>
    </section>
  );
}


function TranslationGroupTabs({ groups, selectedGroup, totalRows, onSelect }) {
  if (!groups?.length) {
    return null;
  }

  const totalCount = groups.reduce((sum, g) => sum + (g.count || 0), 0);
  const totalTranslated = groups.reduce((sum, g) => sum + (g.translatedCount || 0), 0);
  const totalPercent = totalCount > 0 ? (totalTranslated / totalCount) * 100 : 0;
  const isTotalCompleted = totalCount > 0 && totalTranslated === totalCount;
  const sortedGroups = [...groups].sort((a, b) => (
    String(a.label || a.value || "").localeCompare(String(b.label || b.value || ""), "ko")
  ));

  return (
    <div className="translation-group-tabs" role="tablist" aria-label="verified_group 하위 탭">
      <button
        type="button"
        role="tab"
        aria-selected={!selectedGroup}
        className={`translation-group-tab ${!selectedGroup ? "active" : ""} ${isTotalCompleted ? "completed" : ""}`}
        onClick={() => onSelect("")}
      >
        전체 <span>{Number(totalTranslated || 0).toLocaleString()}/{Number(totalCount || 0).toLocaleString()} ({totalPercent.toFixed(1)}%)</span>
      </button>
      {sortedGroups.map((group) => {
        const count = group.count || 0;
        const translatedCount = group.translatedCount || 0;
        const percent = count > 0 ? (translatedCount / count) * 100 : 0;
        const isCompleted = count > 0 && translatedCount === count;

        return (
          <button
            type="button"
            role="tab"
            aria-selected={selectedGroup === group.value}
            key={group.value}
            className={`translation-group-tab ${selectedGroup === group.value ? "active" : ""} ${isCompleted ? "completed" : ""}`}
            onClick={() => onSelect(group.value)}
            title={`${group.label} (${percent.toFixed(1)}%)`}
          >
            {group.label} <span>{Number(translatedCount).toLocaleString()}/{Number(count).toLocaleString()} ({percent.toFixed(1)}%)</span>
          </button>
        );
      })}
    </div>
  );
}


function PaginationToolbar({ data, setData, loadPage, canEdit, hasPageChanges, revertPageRows }) {
  return (
    <div className="translation-toolbar">
      <button type="button" disabled={data.page <= 1} onClick={() => loadPage(Math.max(1, data.page - 1))}><ChevronLeft size={16} />이전</button>
      <label>페이지<input type="number" min="1" value={data.page} onChange={(event) => setData((current) => ({ ...current, page: Number(event.target.value || 1) }))} /></label>
      <button type="button" onClick={() => loadPage(data.page)}>이동</button>
      <button type="button" disabled={data.page >= data.totalPages} onClick={() => loadPage(Math.min(data.totalPages, data.page + 1))}>다음<ChevronRight size={16} /></button>
      <button type="button" className="secondary" disabled={!canEdit || !hasPageChanges} onClick={revertPageRows}>
        <RotateCcw size={16} />현재 페이지 되돌리기
      </button>
      <span>{data.csvPath ? `${data.totalRows.toLocaleString()}행 · ${data.page}/${data.totalPages}페이지` : "아직 CSV를 읽지 않았습니다."}</span>
    </div>
  );
}


function TranslationRow({ row, canEdit, showImages, showTranslatedImages, blackBg, isAdmin, updateRow, discardDraft, revertRow, hasLocalChange }) {
  const warningState = useMemo(() => getRowWarningState(row), [row]);
  const highlightRef = useRef(null);
  const { liveKoreanLength, showOverflow, lineWarnings, hasTextOverflow, hasLineCountOverflow } = warningState;
  const useAdminImageLayout = isAdmin || showTranslatedImages;
  const showImageColumn = showImages || showTranslatedImages;
  const overflowMessage = useMemo(() => {
    if (!showOverflow) {
      return "";
    }
    const length = formatLogicalLength(liveKoreanLength);
    const capacity = formatLogicalLength(row.textCapacity);
    const excess = formatLogicalLength(liveKoreanLength - row.textCapacity);
    const base = `${row.textureWidth}x${row.textureHeight} · ${length}/${capacity}자`;
    return liveKoreanLength > row.textCapacity ? `${base} 초과 ${excess}자` : base;
  }, [liveKoreanLength, row.textCapacity, row.textureHeight, row.textureWidth, showOverflow]);

  function syncHighlightScroll(event) {
    if (!highlightRef.current) {
      return;
    }
    highlightRef.current.scrollTop = event.target.scrollTop;
    highlightRef.current.scrollLeft = event.target.scrollLeft;
  }

  return (
    <div className={`translation-row ${showImageColumn ? "with-image" : ""} ${useAdminImageLayout ? "admin-translation-row" : ""}`.trim()}>
      <div className="translation-number">
        <span>{row.rowNumber}</span>
        {isAdmin && row.filePath ? <small className="translation-file-path" title={row.filePath}>{row.filePath}</small> : null}
        {row.duplicateCount > 1 ? <small>중복 {row.duplicateCount}행</small> : null}
        {row.hasDraft ? <small className={row.draftHasConflict ? "translation-warning-text" : "ok"}>내 초안</small> : null}
      </div>
      {showImageColumn ? (
        <div className={`translation-image-pair ${showImages && showTranslatedImages ? "paired" : ""}`}>
          {showImages ? <ImageBox label={useAdminImageLayout ? "일본어 원본" : "JP"} url={row.imageUrl} blackBg={blackBg} /> : null}
          {showTranslatedImages ? <ImageBox label="한국어 렌더" url={row.koreanImageUrl} blackBg={blackBg} /> : null}
        </div>
      ) : null}
      {!useAdminImageLayout ? (
        <label className="translation-cell">일본어
          <textarea value={row.japanese || ""} readOnly={!canEdit} onChange={(event) => updateRow(row.rowNumber, "japanese", event.target.value)} />
        </label>
      ) : null}
      <label className="translation-cell">한국어
        <div className="translation-textarea-wrap">
          <div ref={highlightRef} className="translation-text-highlight" aria-hidden="true">
            <pre>{renderKoreanHighlightSegments(row.korean || "", warningState)}</pre>
          </div>
          <textarea
            value={row.korean || ""}
            readOnly={!canEdit}
            className="translation-highlight-input"
            onChange={(event) => updateRow(row.rowNumber, "korean", event.target.value)}
            onScroll={syncHighlightScroll}
          />
        </div>
      </label>
      <div className="translation-warning-panel">
        <span className="translation-warning-panel-title">경고</span>
        {row.pendingDraftCount > 0 ? (
          <span className="translation-inline-meta translation-pending-merge-marker">반영대기중</span>
        ) : row.isModified ? (
          <span className="translation-inline-meta translation-modified-marker">수정됨</span>
        ) : null}
        {canEdit && row.hasDraft ? (
          <button type="button" className="secondary compact-button" onClick={() => discardDraft(row)}><Trash2 size={14} />초안 삭제</button>
        ) : null}
        {canEdit ? (
          <button type="button" className="secondary compact-button" disabled={!hasLocalChange && !row.hasDraft} onClick={() => revertRow(row)}><RotateCcw size={14} />되돌리기</button>
        ) : null}
        {overflowMessage ? <span className={`translation-inline-meta ${hasTextOverflow ? "translation-warning-text" : ""}`}>{overflowMessage}</span> : null}
        {warningState.liveLineMax > 0 ? (
          <span className={`translation-inline-meta ${hasLineCountOverflow ? "translation-warning-line" : ""}`}>
            행수 {warningState.liveLines.length}/{warningState.liveLineMax}행{hasLineCountOverflow ? ` 초과 ${warningState.liveLines.length - warningState.liveLineMax}행` : ""}
          </span>
        ) : null}
        {lineWarnings.map((item) => {
          const base = `${item.index + 1}행 ${formatLogicalLength(item.length)}/${formatLogicalLength(item.limit)}자`;
          const message = item.hasOverflow ? `${base} 초과 ${formatLogicalLength(item.length - item.limit)}자` : base;
          return (
            <span key={`line-warning-${item.index}`} className={`translation-inline-meta ${item.hasOverflow ? "translation-warning-line" : ""}`}>
              {message}
            </span>
          );
        })}
      </div>
    </div>
  );
}


function ImageBox({ label, url, blackBg }) {
  return (
    <div className={`translation-image ${blackBg ? "black-background" : ""}`}>
      <span>{label}</span>
      {url ? <img src={url} alt={label} loading="lazy" /> : <em className="translation-empty-image">이미지 없음</em>}
    </div>
  );
}
