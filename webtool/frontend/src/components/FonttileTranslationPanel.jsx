import { useEffect, useMemo, useState } from "react";
import { Check, ChevronLeft, ChevronRight, Inbox, Play, RefreshCw, Save, Search, Send, Trash2, Wand2 } from "lucide-react";

import { api } from "../api/client.js";
import { SectionHead } from "./ToolPanels.jsx";


const fonttileSubtabs = [
  { id: "dictionary", label: "dictionary" },
  { id: "bulk", label: "일괄수정" },
];
const defaultPageState = {
  dictionary: { page: 1, pageSize: 50 },
  bulk: { page: 1, pageSize: 50 },
};
const pageSizeOptions = [10, 20, 50, 100, 200];
const markerBytes = new Map([
  ['"-f"', 1],
  ['"+f"', 1],
]);
const singleByteGlyphs = new Set([
  "Ⅱ", "改", "型", "Ⅲ", "ｖ", "ν", "α", "β", "三", "開", "発",
  "ｂ", "ｄ", "ｅ", "ｉ", "ｔ", "・", "ヲ", "ァ", "ィ", "w", "x", "y", "z",
]);

function isKoreanChar(char) {
  const code = char.codePointAt(0);
  return (
    (code >= 0xac00 && code <= 0xd7a3)
    || (code >= 0x1100 && code <= 0x11ff)
    || (code >= 0x3130 && code <= 0x318f)
    || (code >= 0xa960 && code <= 0xa97f)
    || (code >= 0xd7b0 && code <= 0xd7ff)
  );
}

function koreanFonttileByteLength(char) {
  const code = char.codePointAt(0);
  if (code >= 0xac00 && code <= 0xd7a3) {
    const syllableIndex = code - 0xac00;
    const jongseongIndex = syllableIndex % 28;
    return jongseongIndex ? 3 : 2;
  }
  return isKoreanChar(char) ? 1 : 0;
}

function fonttileByteState(text) {
  const value = String(text || "");
  let length = 0;
  const errors = [];
  for (let cursor = 0; cursor < value.length;) {
    let matched = false;
    for (const [marker, markerLength] of markerBytes.entries()) {
      if (value.startsWith(marker, cursor)) {
        length += markerLength;
        cursor += marker.length;
        matched = true;
        break;
      }
    }
    if (matched) {
      continue;
    }
    const char = Array.from(value.slice(cursor))[0];
    const code = char.codePointAt(0);
    const koreanByteLength = koreanFonttileByteLength(char);
    if (koreanByteLength) {
      length += koreanByteLength;
    } else if (singleByteGlyphs.has(char) || (code >= 0x20 && code <= 0x7e) || (code >= 0xff61 && code <= 0xff9f)) {
      length += 1;
    } else {
      length += 2;
      errors.push(char);
    }
    cursor += char.length;
  }
  return { length, errors };
}

function rowWarningState(row) {
  const byteState = fonttileByteState(row.translation || "");
  const limit = Number(row.minMaxBytes || 0);
  return {
    ...byteState,
    hasOverflow: Boolean(row.translation) && limit > 0 && byteState.length > limit,
    limit,
  };
}

function clampPage(page, totalPages) {
  return Math.max(1, Math.min(Number(totalPages || 1), Number(page || 1)));
}

function pagedItems(items, pageState) {
  const pageSize = Math.max(1, Number(pageState?.pageSize || 20));
  const totalRows = items.length;
  const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
  const page = clampPage(pageState?.page || 1, totalPages);
  const startIndex = (page - 1) * pageSize;
  const endIndex = Math.min(totalRows, startIndex + pageSize);
  return {
    rows: items.slice(startIndex, endIndex),
    page,
    pageSize,
    totalRows,
    totalPages,
    startIndex,
    endIndex,
  };
}

function formatTime(value) {
  if (!value) {
    return "";
  }
  return new Date(Number(value) * 1000).toLocaleString();
}

export function FonttileTranslationPanel({ canEdit, isAdmin = false, onDirtyState, onRun }) {
  const [data, setData] = useState(null);
  const [dictionaryRows, setDictionaryRows] = useState([]);
  const [savedDictionaryRows, setSavedDictionaryRows] = useState([]);
  const [activeSubtab, setActiveSubtab] = useState("dictionary");
  const [pageState, setPageState] = useState(defaultPageState);
  const [search, setSearch] = useState("");
  const [warningsOnly, setWarningsOnly] = useState(false);
  const [emptyOnly, setEmptyOnly] = useState(false);
  const [statusText, setStatusText] = useState("");
  const [isError, setIsError] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [dictionaryRequests, setDictionaryRequests] = useState({ total: 0, requests: [] });
  const [selectedDictionaryRequestId, setSelectedDictionaryRequestId] = useState("");
  const [dictionaryRequestDetail, setDictionaryRequestDetail] = useState(null);
  const [bulkTargetText, setBulkTargetText] = useState("");
  const [bulkReplacementText, setBulkReplacementText] = useState("");
  const [bulkResult, setBulkResult] = useState(null);
  const [bulkPreviewSignature, setBulkPreviewSignature] = useState("");
  const [bulkRequests, setBulkRequests] = useState({ total: 0, requests: [] });
  const [selectedBulkRequestId, setSelectedBulkRequestId] = useState("");
  const [bulkRequestDetail, setBulkRequestDetail] = useState(null);

  const savedDictionaryRowMap = useMemo(
    () => new Map(savedDictionaryRows.map((row) => [row.rowNumber, row])),
    [savedDictionaryRows],
  );
  const dictionaryWarningCount = useMemo(
    () => dictionaryRows.filter((row) => {
      const warning = rowWarningState(row);
      return warning.hasOverflow || warning.errors.length;
    }).length,
    [dictionaryRows],
  );
  const filteredDictionaryRows = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("ko");
    return dictionaryRows.filter((row) => {
      const savedRow = savedDictionaryRowMap.get(row.rowNumber) || row;
      const warning = rowWarningState(savedRow);
      if (warningsOnly && !(warning.hasOverflow || warning.errors.length)) {
        return false;
      }
      if (emptyOnly && String(savedRow.translation || "").trim()) {
        return false;
      }
      if (!query) {
        return true;
      }
      return [row.original, savedRow.translation, row.translation, row.samples]
        .some((value) => String(value || "").toLocaleLowerCase("ko").includes(query));
    });
  }, [dictionaryRows, emptyOnly, savedDictionaryRowMap, search, warningsOnly]);
  const dictionaryPage = useMemo(() => pagedItems(filteredDictionaryRows, pageState.dictionary), [filteredDictionaryRows, pageState.dictionary]);
  const subtabCounts = {
    dictionary: filteredDictionaryRows.length,
    bulk: bulkResult?.totalRows || bulkRequests.total || 0,
  };
  const subtabWarnings = {
    dictionary: dictionaryWarningCount,
    bulk: 0,
  };
  const visibleSubtabs = fonttileSubtabs;
  const changedDictionaryRows = useMemo(
    () => dictionaryRows.flatMap((row) => {
      const savedRow = savedDictionaryRowMap.get(row.rowNumber);
      if (!savedRow || String(savedRow.translation || "") === String(row.translation || "")) {
        return [];
      }
      return [{ ...row, baseTranslation: savedRow.translation || "" }];
    }),
    [dictionaryRows, savedDictionaryRowMap],
  );
  const bulkSignature = useMemo(
    () => JSON.stringify({ bulkTargetText, bulkReplacementText }),
    [bulkReplacementText, bulkTargetText],
  );
  const canSubmitBulk = canEdit && bulkResult?.totalRows > 0 && bulkPreviewSignature === bulkSignature;

  function setMessage(message, error = false) {
    setStatusText(message);
    setIsError(error);
  }

  function markDirty() {
    setDirty(true);
    onDirtyState?.();
  }

  function setSubtabPage(tabId, page) {
    setPageState((current) => ({
      ...current,
      [tabId]: { ...current[tabId], page },
    }));
  }

  function setSubtabPageSize(tabId, pageSize) {
    setPageState((current) => ({
      ...current,
      [tabId]: { page: 1, pageSize },
    }));
  }

  async function loadData() {
    setMessage("EBOOT 번역 파일을 읽는 중...");
    const response = await api("/api/fonttile");
    setData(response);
    setDictionaryRows(response.dictionary?.rows || []);
    setSavedDictionaryRows(response.dictionary?.rows || []);
    setDirty(false);
    setMessage(`dictionary ${Number(response.dictionary?.totalRows || 0).toLocaleString()}행을 읽었습니다.`);
  }

  async function loadDictionaryRequests() {
    setDictionaryRequests(await api("/api/fonttile/dictionary/requests"));
  }

  async function loadBulkRequests() {
    setBulkRequests(await api("/api/fonttile/bulk/requests"));
  }

  async function saveAll() {
    if (!isAdmin) {
      setMessage("저장 권한이 없습니다.", true);
      return null;
    }
    setMessage("EBOOT 번역 파일 저장 중...");
    const response = await api("/api/fonttile/save", {
      method: "POST",
      body: JSON.stringify({ dictionaryRows }),
    });
    setData(response);
    setDictionaryRows(response.dictionary?.rows || []);
    setSavedDictionaryRows(response.dictionary?.rows || []);
    setDirty(false);
    setMessage("저장했습니다.");
    return response;
  }

  async function fillSlots() {
    if (!isAdmin) {
      setMessage("실행 권한이 없습니다.", true);
      return;
    }
    if (dirty) {
      await saveAll();
    }
    setMessage("슬롯채우기 작업을 시작합니다...");
    if (onRun) {
      await onRun({ action: "fonttile-fill-slots" });
    } else {
      await api("/api/run", { method: "POST", body: JSON.stringify({ action: "fonttile-fill-slots" }) });
    }
    setMessage("슬롯채우기 작업을 시작했습니다. 하단 로그를 확인하세요.");
  }

  async function submitDictionaryRequest() {
    if (!canEdit || !changedDictionaryRows.length) {
      return;
    }
    setMessage("dictionary 번역 제출 중...");
    const response = await api("/api/fonttile/dictionary/requests", {
      method: "POST",
      body: JSON.stringify({ changes: changedDictionaryRows }),
    });
    setMessage(`${response.submittedRows.toLocaleString()}행을 제출했습니다.`);
    setSavedDictionaryRows(dictionaryRows);
    setDirty(false);
    await loadDictionaryRequests();
  }

  async function loadDictionaryRequestDetail(requestId, page = 1) {
    setMessage("dictionary 승인 대상을 확인 중...");
    const response = await api(`/api/fonttile/dictionary/requests/${requestId}?page=${page}&pageSize=${pageState.dictionary.pageSize}`);
    setSelectedDictionaryRequestId(requestId);
    setDictionaryRequestDetail(response);
    setMessage(`${response.totalRows.toLocaleString()}행 승인 대상입니다.${response.conflictRows ? ` 충돌 ${response.conflictRows.toLocaleString()}건.` : ""}`, Boolean(response.conflictRows));
  }

  async function approveDictionaryRequest() {
    if (!selectedDictionaryRequestId || !dictionaryRequestDetail?.canApprove) {
      return;
    }
    setMessage("dictionary 제출 승인 중...");
    const response = await api(`/api/fonttile/dictionary/requests/${selectedDictionaryRequestId}/approve`, { method: "POST" });
    if (!response.approved) {
      await loadDictionaryRequestDetail(selectedDictionaryRequestId, dictionaryRequestDetail.page || 1);
      setMessage(`승인할 수 없습니다. 충돌 ${Number(response.conflictRows || 0).toLocaleString()}건.`, true);
      return;
    }
    setMessage(`${response.changedRows.toLocaleString()}행을 승인했습니다.`);
    setSelectedDictionaryRequestId("");
    setDictionaryRequestDetail(null);
    await loadDictionaryRequests();
    await loadData();
  }

  async function deleteDictionaryRequest() {
    if (!selectedDictionaryRequestId) {
      return;
    }
    const response = await api(`/api/fonttile/dictionary/requests/${selectedDictionaryRequestId}`, { method: "DELETE" });
    setMessage(`${Number(response.deleted || 0).toLocaleString()}개 요청을 삭제했습니다.`);
    setSelectedDictionaryRequestId("");
    setDictionaryRequestDetail(null);
    await loadDictionaryRequests();
  }

  async function runBulkPreview(page = 1) {
    if (!bulkTargetText) {
      setMessage("대상문자를 입력하세요.", true);
      return;
    }
    setMessage("dictionary 일괄수정 결과 계산 중...");
    const response = await api("/api/fonttile/bulk/preview", {
      method: "POST",
      body: JSON.stringify({
        targetText: bulkTargetText,
        replacementText: bulkReplacementText,
        page,
        pageSize: pageState.bulk.pageSize,
      }),
    });
    setBulkResult(response);
    setBulkPreviewSignature(bulkSignature);
    setBulkRequestDetail(null);
    setSelectedBulkRequestId("");
    setMessage(`${response.totalRows.toLocaleString()}행이 치환 대상입니다.`);
  }

  async function submitBulkRequest() {
    if (!canSubmitBulk) {
      return;
    }
    setMessage("dictionary 일괄수정 제출 중...");
    const response = await api("/api/fonttile/bulk/requests", {
      method: "POST",
      body: JSON.stringify({ targetText: bulkTargetText, replacementText: bulkReplacementText }),
    });
    setMessage(`${response.matchedRows.toLocaleString()}행 일괄수정을 제출했습니다.`);
    await loadBulkRequests();
  }

  async function loadBulkRequestDetail(requestId, page = 1) {
    setMessage("일괄수정 승인 대상을 확인 중...");
    const response = await api(`/api/fonttile/bulk/requests/${requestId}?page=${page}&pageSize=${pageState.bulk.pageSize}`);
    setSelectedBulkRequestId(requestId);
    setBulkRequestDetail(response);
    setBulkResult(null);
    setBulkTargetText(response.targetText || "");
    setBulkReplacementText(response.replacementText || "");
    setMessage(`${response.totalRows.toLocaleString()}행 승인 대상입니다.${response.conflictRows ? ` 충돌 ${response.conflictRows.toLocaleString()}건.` : ""}`, Boolean(response.conflictRows));
  }

  async function approveBulkRequest() {
    if (!selectedBulkRequestId || !bulkRequestDetail?.canApprove) {
      return;
    }
    setMessage("일괄수정 승인 중...");
    const response = await api(`/api/fonttile/bulk/requests/${selectedBulkRequestId}/approve`, { method: "POST" });
    if (!response.approved) {
      await loadBulkRequestDetail(selectedBulkRequestId, bulkRequestDetail.page || 1);
      setMessage(`승인할 수 없습니다. 충돌 ${Number(response.conflictRows || 0).toLocaleString()}건.`, true);
      return;
    }
    setMessage(`${response.changedRows.toLocaleString()}행에 일괄수정을 적용했습니다.`);
    setSelectedBulkRequestId("");
    setBulkRequestDetail(null);
    await loadBulkRequests();
    await loadData();
  }

  async function deleteBulkRequest() {
    if (!selectedBulkRequestId) {
      return;
    }
    const response = await api(`/api/fonttile/bulk/requests/${selectedBulkRequestId}`, { method: "DELETE" });
    setMessage(`${Number(response.deleted || 0).toLocaleString()}개 요청을 삭제했습니다.`);
    setSelectedBulkRequestId("");
    setBulkRequestDetail(null);
    await loadBulkRequests();
  }

  function updateDictionary(rowNumber, value) {
    setDictionaryRows((current) => current.map((item) => (item.rowNumber === rowNumber ? { ...item, translation: value } : item)));
    markDirty();
  }

  useEffect(() => {
    loadData().catch((err) => setMessage(err.message, true));
    loadDictionaryRequests().catch(() => {});
    loadBulkRequests().catch(() => {});
  }, []);

  useEffect(() => {
    setSubtabPage("dictionary", 1);
  }, [emptyOnly, search, warningsOnly]);

  useEffect(() => {
    if (!visibleSubtabs.some((tab) => tab.id === activeSubtab)) {
      setActiveSubtab("dictionary");
    }
  }, [activeSubtab, visibleSubtabs]);

  useEffect(() => {
    if (bulkPreviewSignature && bulkPreviewSignature !== bulkSignature) {
      setBulkPreviewSignature("");
    }
  }, [bulkPreviewSignature, bulkSignature]);

  return (
    <>
      <SectionHead title="EBOOT번역" description="로컬에서 생성한 dictionary의 한국어 번역을 편집" />
      <div className="fonttile-actions">
        <div className={`translation-status ${isError ? "missing" : ""}`}>{statusText}</div>
        <div className="actions">
          <button type="button" className="secondary" onClick={() => loadData().catch((err) => setMessage(err.message, true))}><RefreshCw size={16} />읽기</button>
          {isAdmin ? <button type="button" disabled={!dirty} onClick={() => saveAll().catch((err) => setMessage(err.message, true))}><Save size={16} />저장</button> : null}
          {isAdmin ? <button type="button" onClick={() => fillSlots().catch((err) => setMessage(err.message, true))}><Play size={16} />슬롯채우기</button> : null}
        </div>
      </div>
      {data ? (
        <div className="fonttile-summary">
          <span className={dictionaryWarningCount ? "translation-warning-text" : ""}>사전 경고 {dictionaryWarningCount.toLocaleString()}건</span>
          <span>{data.paths?.dictionary}</span>
        </div>
      ) : null}
      <FonttileSubtabs
        tabs={visibleSubtabs}
        activeSubtab={activeSubtab}
        counts={subtabCounts}
        warnings={subtabWarnings}
        onSelect={setActiveSubtab}
      />
      {activeSubtab === "dictionary" ? (
        <DictionarySection
          canEdit={canEdit}
          rows={dictionaryPage.rows}
          page={dictionaryPage}
          totalRows={dictionaryRows.length}
          search={search}
          warningsOnly={warningsOnly}
          emptyOnly={emptyOnly}
          onSearchChange={setSearch}
          onWarningsOnlyChange={setWarningsOnly}
          onEmptyOnlyChange={setEmptyOnly}
          onUpdate={updateDictionary}
          changedCount={changedDictionaryRows.length}
          onSubmit={() => submitDictionaryRequest().catch((err) => setMessage(err.message, true))}
          onPageChange={(page) => setSubtabPage("dictionary", page)}
          onPageSizeChange={(pageSize) => setSubtabPageSize("dictionary", pageSize)}
        />
      ) : null}
      {activeSubtab === "dictionary" ? (
        <FonttileApprovalPanel
          title={isAdmin ? "dictionary 승인함" : "dictionary 제출함"}
          isAdmin={isAdmin}
          requests={dictionaryRequests}
          selectedRequestId={selectedDictionaryRequestId}
          detail={dictionaryRequestDetail}
          onRefresh={() => loadDictionaryRequests().catch((err) => setMessage(err.message, true))}
          onSelect={(requestId) => loadDictionaryRequestDetail(requestId, 1).catch((err) => setMessage(err.message, true))}
          onApprove={() => approveDictionaryRequest().catch((err) => setMessage(err.message, true))}
          onDelete={() => deleteDictionaryRequest().catch((err) => setMessage(err.message, true))}
          onPageChange={(page) => loadDictionaryRequestDetail(selectedDictionaryRequestId, page).catch((err) => setMessage(err.message, true))}
        />
      ) : null}
      {activeSubtab === "bulk" ? (
        <FonttileBulkSection
          canEdit={canEdit}
          isAdmin={isAdmin}
          targetText={bulkTargetText}
          replacementText={bulkReplacementText}
          result={bulkRequestDetail || bulkResult}
          resultMode={bulkRequestDetail ? "approval" : "preview"}
          requests={bulkRequests}
          selectedRequestId={selectedBulkRequestId}
          canSubmit={canSubmitBulk}
          onTargetTextChange={setBulkTargetText}
          onReplacementTextChange={setBulkReplacementText}
          onPreview={(page) => runBulkPreview(page).catch((err) => setMessage(err.message, true))}
          onSubmit={() => submitBulkRequest().catch((err) => setMessage(err.message, true))}
          onRefresh={() => loadBulkRequests().catch((err) => setMessage(err.message, true))}
          onSelect={(requestId) => loadBulkRequestDetail(requestId, 1).catch((err) => setMessage(err.message, true))}
          onApprove={() => approveBulkRequest().catch((err) => setMessage(err.message, true))}
          onDelete={() => deleteBulkRequest().catch((err) => setMessage(err.message, true))}
          onPageChange={(page) => {
            if (bulkRequestDetail) {
              loadBulkRequestDetail(selectedBulkRequestId, page).catch((err) => setMessage(err.message, true));
            } else {
              runBulkPreview(page).catch((err) => setMessage(err.message, true));
            }
          }}
        />
      ) : null}
    </>
  );
}

function FonttileSubtabs({ tabs, activeSubtab, counts, warnings, onSelect }) {
  return (
    <div className="fonttile-subtabs" role="tablist" aria-label="EBOOT 번역 편집 구역">
      {tabs.map((tab) => {
        const warningCount = Number(warnings[tab.id] || 0);
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={activeSubtab === tab.id}
            className={`fonttile-subtab ${activeSubtab === tab.id ? "active" : ""} ${warningCount ? "has-warning" : ""}`}
            onClick={() => onSelect(tab.id)}
          >
            {tab.label}
            <span>
              {Number(counts[tab.id] || 0).toLocaleString()}행{warningCount ? ` · 경고 ${warningCount.toLocaleString()}` : ""}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function FonttilePagination({ page, onPageChange, onPageSizeChange }) {
  const start = page.totalRows ? page.startIndex + 1 : 0;
  const end = page.endIndex;
  return (
    <div className="fonttile-page-toolbar">
      <button type="button" className="secondary compact-button" disabled={page.page <= 1} onClick={() => onPageChange(page.page - 1)}>
        <ChevronLeft size={15} />이전
      </button>
      <label>페이지
        <input
          type="number"
          min="1"
          max={page.totalPages}
          value={page.page}
          onChange={(event) => onPageChange(clampPage(event.target.value, page.totalPages))}
        />
      </label>
      <span>{page.page}/{page.totalPages} · {start.toLocaleString()}-{end.toLocaleString()} / {page.totalRows.toLocaleString()}</span>
      <button type="button" className="secondary compact-button" disabled={page.page >= page.totalPages} onClick={() => onPageChange(page.page + 1)}>
        다음<ChevronRight size={15} />
      </button>
      <label>페이지당
        <select value={page.pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))}>
          {pageSizeOptions.map((size) => <option key={size} value={size}>{size}</option>)}
        </select>
      </label>
    </div>
  );
}

function DictionarySection({
  canEdit,
  rows,
  page,
  totalRows,
  search,
  warningsOnly,
  emptyOnly,
  onSearchChange,
  onWarningsOnlyChange,
  onEmptyOnlyChange,
  onUpdate,
  changedCount,
  onSubmit,
  onPageChange,
  onPageSizeChange,
}) {
  return (
    <section className="fonttile-section">
      <div className="fonttile-section-head">
        <div>
          <h2>dictionary</h2>
          <p className="translation-inline-meta">{page.totalRows.toLocaleString()}/{totalRows.toLocaleString()}행 표시</p>
        </div>
        <div className="fonttile-dictionary-tools">
          <label>검색
            <input value={search} onChange={(event) => onSearchChange(event.target.value)} />
          </label>
          <label className="check"><input type="checkbox" checked={warningsOnly} onChange={(event) => onWarningsOnlyChange(event.target.checked)} /> 경고만</label>
          <label className="check"><input type="checkbox" checked={emptyOnly} onChange={(event) => onEmptyOnlyChange(event.target.checked)} /> 빈칸만</label>
          <button type="button" disabled={!canEdit || !changedCount} onClick={onSubmit}><Send size={16} />제출 {Number(changedCount || 0).toLocaleString()}</button>
        </div>
      </div>
      <FonttilePagination page={page} onPageChange={onPageChange} onPageSizeChange={onPageSizeChange} />
      <div className="fonttile-dictionary-list">
        {rows.map((row) => {
          const warning = rowWarningState(row);
          return (
            <div key={row.rowNumber} className={`fonttile-dictionary-row ${warning.hasOverflow || warning.errors.length ? "warning-row" : ""}`}>
              <div className="fonttile-row-number">{row.rowNumber}</div>
              <div className="fonttile-original">
                <b>{row.original}</b>
                <span>count {Number(row.count || 0).toLocaleString()} · 안전일괄용량 {row.minMaxBytes} · 최대 {row.maxMaxBytes}</span>
                {row.samples ? <small title={row.samples}>{row.samples}</small> : null}
              </div>
              <label className="fonttile-translation-field">번역
                <input value={row.translation || ""} readOnly={!canEdit} onChange={(event) => onUpdate(row.rowNumber, event.target.value)} />
              </label>
              <div className="fonttile-row-warning">
                <span className={warning.hasOverflow ? "translation-warning-text" : "translation-inline-meta"}>
                  {warning.length}/{warning.limit || "-"} byte
                </span>
                {warning.hasOverflow ? <span className="translation-warning-text">초과 {warning.length - warning.limit} byte</span> : null}
                {warning.errors.length ? <span className="translation-warning-text">확인필요: {[...new Set(warning.errors)].join("")}</span> : null}
              </div>
            </div>
          );
        })}
        {rows.length ? null : <div className="translation-status"><Search size={16} />표시할 dictionary 행이 없습니다.</div>}
      </div>
      <FonttilePagination page={page} onPageChange={onPageChange} onPageSizeChange={onPageSizeChange} />
    </section>
  );
}

function FonttileApprovalPanel({
  title,
  isAdmin,
  requests,
  selectedRequestId,
  detail,
  onRefresh,
  onSelect,
  onApprove,
  onDelete,
  onPageChange,
}) {
  return (
    <section className="bulk-approval-panel fonttile-approval-panel">
      <div className="draft-merge-head">
        <div>
          <h2>{title}</h2>
          <p>{isAdmin ? "승인 대기" : "승인대기중"} {Number(requests.total || 0).toLocaleString()}건</p>
        </div>
        <div className="actions">
          <button type="button" className="secondary" onClick={onRefresh}><Inbox size={16} />새로고침</button>
          {isAdmin ? <button type="button" disabled={!selectedRequestId || !detail?.canApprove} onClick={onApprove}><Check size={16} />승인</button> : null}
          {isAdmin ? <button type="button" className="secondary danger-button" disabled={!selectedRequestId} onClick={onDelete}><Trash2 size={16} />삭제</button> : null}
          {isAdmin && detail?.conflictRows ? <span className="bulk-approve-warning">충돌 {detail.conflictRows.toLocaleString()}건</span> : null}
        </div>
      </div>
      <div className="bulk-request-list">
        {requests.requests?.length ? requests.requests.map((request) => (
          isAdmin ? (
            <button
              key={request.id}
              type="button"
              className={`bulk-request-item ${selectedRequestId === request.id ? "active" : ""}`}
              onClick={() => onSelect(request.id)}
            >
              <strong>{request.type === "bulk" ? `${request.targetText || "(빈 값)"} → ${request.replacementText || "(빈 값)"}` : "dictionary 개별 번역"}</strong>
              <span>{request.submittedUsername} · {formatTime(request.createdAt)}</span>
            </button>
          ) : (
            <article key={request.id} className="bulk-request-item">
              <strong>{request.type === "bulk" ? `${request.targetText || "(빈 값)"} → ${request.replacementText || "(빈 값)"}` : "dictionary 개별 번역"}</strong>
              <span className="bulk-pending-label">승인대기중</span>
              <span>{formatTime(request.createdAt)}</span>
            </article>
          )
        )) : <div className="translation-status">{isAdmin ? "대기 중인 요청이 없습니다." : "승인대기중인 제출이 없습니다."}</div>}
      </div>
      {isAdmin && detail ? <FonttileRequestRows result={detail} mode="approval" onPageChange={onPageChange} /> : null}
    </section>
  );
}

function FonttileBulkSection({
  canEdit,
  isAdmin,
  targetText,
  replacementText,
  result,
  resultMode,
  requests,
  selectedRequestId,
  canSubmit,
  onTargetTextChange,
  onReplacementTextChange,
  onPreview,
  onSubmit,
  onRefresh,
  onSelect,
  onApprove,
  onDelete,
  onPageChange,
}) {
  return (
    <section className="fonttile-section">
      <div className="bulk-workbench">
        <form className="bulk-card" onSubmit={(event) => { event.preventDefault(); onPreview(1); }}>
          <h2>dictionary 일괄수정</h2>
          <label>대상문자
            <input value={targetText} onChange={(event) => onTargetTextChange(event.target.value)} />
          </label>
          <label>치환문자
            <input value={replacementText} onChange={(event) => onReplacementTextChange(event.target.value)} />
          </label>
          <div className="actions">
            <button type="submit" disabled={!targetText}><Wand2 size={16} />치환</button>
            <button type="button" disabled={!canSubmit} onClick={onSubmit}><Send size={16} />제출</button>
          </div>
        </form>
        <FonttileApprovalPanel
          title={isAdmin ? "일괄수정 승인함" : "일괄수정 제출함"}
          isAdmin={isAdmin}
          requests={requests}
          selectedRequestId={selectedRequestId}
          detail={resultMode === "approval" ? result : null}
          onRefresh={onRefresh}
          onSelect={onSelect}
          onApprove={onApprove}
          onDelete={onDelete}
          onPageChange={onPageChange}
        />
      </div>
      <FonttileRequestRows result={result} mode={resultMode} onPageChange={onPageChange} />
    </section>
  );
}

function FonttileRequestRows({ result, mode, onPageChange }) {
  if (!result) {
    return <div className="translation-status">치환을 실행하거나 승인 대상을 선택하면 결과가 표시됩니다.</div>;
  }
  return (
    <section className="bulk-results fonttile-request-results">
      <div className="bulk-results-head">
        <h2>{mode === "approval" ? "승인 대상" : "치환 결과"}</h2>
        <span>{Number(result.totalRows || 0).toLocaleString()}행 · {result.page}/{result.totalPages}페이지</span>
      </div>
      <FonttileResultPagination result={result} onPageChange={onPageChange} />
      <div className="bulk-result-list">
        {result.rows?.length ? result.rows.map((row) => {
          const warning = rowWarningState({ translation: row.submittedTranslation, minMaxBytes: row.minMaxBytes });
          return (
            <article key={`${row.rowNumber}-${row.original}`} className={`bulk-result-row ${row.hasConflict ? "conflict" : ""}`}>
              <div className="bulk-row-meta">
                <strong>행 {row.rowNumber}</strong>
                <span>count {Number(row.count || 0).toLocaleString()} · 안전일괄용량 {row.minMaxBytes}</span>
                {row.conflictReason ? <span className="bulk-approve-warning">{row.conflictReason}</span> : null}
                {warning.hasOverflow ? <span className="bulk-approve-warning">초과 {warning.length - warning.limit} byte</span> : null}
                {warning.errors.length ? <span className="bulk-approve-warning">확인필요: {[...new Set(warning.errors)].join("")}</span> : null}
              </div>
              <div className="bulk-before-after">
                <div>
                  <b>원문</b>
                  <p>{row.original}</p>
                </div>
                <div>
                  <b>이전</b>
                  <p>{row.before ?? row.baseTranslation}</p>
                </div>
                <div>
                  <b>제출</b>
                  <p>{row.after ?? row.submittedTranslation}</p>
                </div>
                {row.hasConflict ? (
                  <div>
                    <b>현재값</b>
                    <p>{row.currentTranslation}</p>
                  </div>
                ) : null}
              </div>
            </article>
          );
        }) : <div className="translation-status">표시할 결과가 없습니다.</div>}
      </div>
      <FonttileResultPagination result={result} onPageChange={onPageChange} />
    </section>
  );
}

function FonttileResultPagination({ result, onPageChange }) {
  return (
    <div className="translation-toolbar">
      <button type="button" disabled={result.page <= 1} onClick={() => onPageChange(Math.max(1, result.page - 1))}><ChevronLeft size={16} />이전</button>
      <span>{result.page}/{result.totalPages}</span>
      <button type="button" disabled={result.page >= result.totalPages} onClick={() => onPageChange(Math.min(result.totalPages, result.page + 1))}>다음<ChevronRight size={16} /></button>
      <span>페이지당 {result.pageSize}개</span>
    </div>
  );
}
