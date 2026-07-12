import { useEffect, useMemo, useState } from "react";
import { Check, ChevronLeft, ChevronRight, Inbox, RefreshCw, Search, Send, Trash2, Wand2 } from "lucide-react";

import { api, encodeQuery } from "../api/client.js";
import { SectionHead } from "./ToolPanels.jsx";


const defaultFolder = "textures_static";
const defaultPageSize = 15;
const pageSizeOptions = [10, 15, 30, 50, 100, 200];

function selectedGroupLabel(groups, selectedGroups) {
  if (!selectedGroups.length) {
    return "전체";
  }
  if (selectedGroups.length === 1) {
    const match = groups.find((group) => group.value === selectedGroups[0]);
    return match?.label || selectedGroups[0];
  }
  return `${selectedGroups.length.toLocaleString()}개 선택`;
}

function formatTime(value) {
  if (!value) {
    return "";
  }
  return new Date(Number(value) * 1000).toLocaleString();
}

function decodeBulkSearchText(value) {
  return String(value || "").replaceAll("\\n", "\n");
}

function formatBulkText(value) {
  return String(value ?? "").replaceAll("\n", "\\n");
}

function bulkOperationLabel(targetText, replacementText) {
  return `${formatBulkText(targetText) || "(빈 값)"} → ${formatBulkText(replacementText) || "(빈 값)"}`;
}

function splitHighlighted(text, needle, className) {
  const value = String(text || "");
  const searchNeedle = decodeBulkSearchText(needle);
  if (!searchNeedle) {
    return value;
  }
  const segments = value.split(searchNeedle);
  return segments.map((segment, index) => (
    <span key={`${className}-${index}-${segment}`}>
      {segment}
      {index < segments.length - 1 ? <mark className={className}>{formatBulkText(searchNeedle)}</mark> : null}
    </span>
  ));
}

function resultTitle(mode) {
  if (mode === "approval") {
    return "승인 대상";
  }
  if (mode === "replace") {
    return "치환 결과";
  }
  return "검색 결과";
}

export function BulkTranslationPanel({ canEdit, isAdmin }) {
  const [folder, setFolder] = useState(defaultFolder);
  const [options, setOptions] = useState({ csvPath: "", groups: [], totalRows: 0 });
  const [selectedGroups, setSelectedGroups] = useState([]);
  const [pageSize, setPageSize] = useState(defaultPageSize);
  const [targetText, setTargetText] = useState("");
  const [replacementText, setReplacementText] = useState("");
  const [resultMode, setResultMode] = useState("search");
  const [result, setResult] = useState(null);
  const [previewSignature, setPreviewSignature] = useState("");
  const [requests, setRequests] = useState({ total: 0, requests: [] });
  const [selectedRequestId, setSelectedRequestId] = useState("");
  const [statusText, setStatusText] = useState("");
  const [isError, setIsError] = useState(false);

  const currentSignature = useMemo(
    () => JSON.stringify({ targetText, replacementText, selectedGroups }),
    [replacementText, selectedGroups, targetText],
  );
  const hasTargetText = targetText.length > 0;
  const canSubmit = canEdit && resultMode === "replace" && result?.totalRows > 0 && previewSignature === currentSignature;
  const approveWarning = resultMode === "approval" && result?.conflictRows > 0
    ? [
      result.missingTargetRows > 0 ? "문자 사라짐" : "",
      result.changedRows > 0 ? "값이 변경됨" : "",
    ].filter(Boolean).join(" / ")
    : "";

  function setMessage(message, error = false) {
    setStatusText(message);
    setIsError(error);
  }

  async function loadOptions() {
    const response = await api(`/api/translation/bulk/options?${encodeQuery({ folder })}`);
    setOptions(response);
  }

  async function loadRequests() {
    const response = await api(`/api/translation/bulk/requests?${encodeQuery({ folder })}`);
    setRequests(response);
  }

  function clearResultForFormChange() {
    if (resultMode === "replace") {
      setPreviewSignature("");
    }
  }

  async function runSearch(page = 1) {
    if (!targetText) {
      setMessage("대상문자를 입력하세요.", true);
      return;
    }
    setMessage("검색 중...");
    const response = await api("/api/translation/bulk/preview", {
      method: "POST",
      body: JSON.stringify({
        folder,
        targetText,
        replacementText: "",
        groups: selectedGroups,
        page,
        pageSize,
      }),
    });
    setResultMode("search");
    setResult(response);
    setMessage(`${response.totalRows.toLocaleString()}개 문장을 찾았습니다.`);
  }

  async function runPreview(page = 1) {
    if (!targetText) {
      setMessage("대상문자를 입력하세요.", true);
      return;
    }
    setMessage("치환 결과를 계산 중...");
    const response = await api("/api/translation/bulk/preview", {
      method: "POST",
      body: JSON.stringify({
        folder,
        targetText,
        replacementText,
        groups: selectedGroups,
        page,
        pageSize,
      }),
    });
    setResultMode("replace");
    setResult(response);
    setPreviewSignature(currentSignature);
    setSelectedRequestId("");
    setMessage(`${response.totalRows.toLocaleString()}개 문장이 치환 대상입니다.`);
  }

  async function submitRequest() {
    if (!canSubmit) {
      return;
    }
    setMessage("일괄수정 요청 제출 중...");
    const response = await api("/api/translation/bulk/requests", {
      method: "POST",
      body: JSON.stringify({ folder, targetText, replacementText, groups: selectedGroups }),
    });
    setMessage(`${response.matchedRows.toLocaleString()}행 일괄수정 요청을 제출했습니다.`);
    await loadRequests();
  }

  async function loadRequestDetail(requestId, page = 1) {
    setMessage("승인 대상을 확인 중...");
    const response = await api(`/api/translation/bulk/requests/${requestId}?${encodeQuery({ page, pageSize })}`);
    setSelectedRequestId(requestId);
    setResultMode("approval");
    setResult(response);
    setTargetText(response.targetText || "");
    setReplacementText(response.replacementText || "");
    setSelectedGroups(response.groups || []);
    setPreviewSignature("");
    const conflictText = response.conflictRows ? ` 충돌 ${response.conflictRows.toLocaleString()}건.` : "";
    setMessage(`${response.totalRows.toLocaleString()}행 승인 대상입니다.${conflictText}`, Boolean(response.conflictRows));
  }

  async function approveSelectedRequest() {
    if (!selectedRequestId || !result?.canApprove) {
      return;
    }
    setMessage("일괄수정 승인 적용 중...");
    const response = await api(`/api/translation/bulk/requests/${selectedRequestId}/approve`, { method: "POST" });
    if (!response.approved) {
      await loadRequestDetail(selectedRequestId, result.page || 1);
      setMessage(`승인할 수 없습니다. 충돌 ${Number(response.conflictRows || 0).toLocaleString()}건.`, true);
      return;
    }
    setMessage(`${response.changedRows.toLocaleString()}행에 일괄수정을 적용했습니다.`);
    await loadRequests();
    setSelectedRequestId("");
    setResult(null);
  }

  async function deleteSelectedRequest() {
    if (!selectedRequestId) {
      return;
    }
    setMessage("일괄수정 요청 삭제 중...");
    const response = await api(`/api/translation/bulk/requests/${selectedRequestId}`, { method: "DELETE" });
    setMessage(`${Number(response.deleted || 0).toLocaleString()}개 요청을 삭제했습니다.`);
    await loadRequests();
    setSelectedRequestId("");
    setResult(null);
  }

  async function goToPage(page) {
    if (resultMode === "search") {
      await runSearch(page);
    } else if (resultMode === "replace") {
      await runPreview(page);
    } else if (selectedRequestId) {
      await loadRequestDetail(selectedRequestId, page);
    }
  }

  useEffect(() => {
    loadOptions().catch((err) => setMessage(err.message, true));
    loadRequests().catch(() => {});
  }, []);

  useEffect(() => {
    clearResultForFormChange();
  }, [currentSignature]);

  return (
    <>
      <SectionHead title="일괄수정" description="CSV의 korean 컬럼에서 문자열을 검색하고, 치환 요청을 제출하거나 승인" />
      <form className="form-grid bulk-form" onSubmit={(event) => event.preventDefault()}>
        {isAdmin ? <label>CSV 폴더 또는 파일<input value={folder} onChange={(event) => setFolder(event.target.value)} /></label> : null}
        <CategoryDropdown groups={options.groups} selectedGroups={selectedGroups} setSelectedGroups={setSelectedGroups} />
        <label>페이지당 개수
          <select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value))}>
            {pageSizeOptions.map((size) => <option key={size} value={size}>{size}개</option>)}
          </select>
        </label>
        {isAdmin ? (
          <div className="actions full">
            <button
              type="button"
              className="secondary"
              onClick={() => {
                loadOptions().then(loadRequests).then(() => setMessage("목록을 새로고침했습니다.")).catch((err) => setMessage(err.message, true));
              }}
            >
              <RefreshCw size={16} />새로고침
            </button>
            <span className="bulk-csv-summary">{options.csvPath ? `${options.csvPath} · ${Number(options.totalRows || 0).toLocaleString()}행` : ""}</span>
          </div>
        ) : null}
      </form>

      <section className="bulk-workbench">
        <form className="bulk-card" onSubmit={(event) => { event.preventDefault(); runSearch(1).catch((err) => setMessage(err.message, true)); }}>
          <h2>검색/제출</h2>
          <label>대상문자
            <input value={targetText} onChange={(event) => setTargetText(event.target.value)} />
          </label>
          <label>치환문자
            <input value={replacementText} onChange={(event) => setReplacementText(event.target.value)} />
          </label>
          <div className="actions">
            <button type="submit" disabled={!hasTargetText}><Search size={16} />검색</button>
            <button type="button" disabled={!hasTargetText} onClick={() => runPreview(1).catch((err) => setMessage(err.message, true))}><Wand2 size={16} />치환</button>
            <button type="button" disabled={!canSubmit} onClick={() => submitRequest().catch((err) => setMessage(err.message, true))}><Send size={16} />제출</button>
          </div>
        </form>

        <section className="bulk-approval-panel">
          <div className="draft-merge-head">
            <div>
              <h2>{isAdmin ? "승인함" : "제출함"}</h2>
              <p>{isAdmin ? "승인 대기" : "승인대기중"} {Number(requests.total || 0).toLocaleString()}건</p>
            </div>
            {isAdmin ? (
              <div className="actions">
                <button type="button" className="secondary" onClick={() => loadRequests().catch((err) => setMessage(err.message, true))}><Inbox size={16} />새로고침</button>
                <button type="button" disabled={!selectedRequestId || !result?.canApprove} onClick={() => approveSelectedRequest().catch((err) => setMessage(err.message, true))}><Check size={16} />승인</button>
                <button type="button" className="secondary danger-button" disabled={!selectedRequestId} onClick={() => deleteSelectedRequest().catch((err) => setMessage(err.message, true))}><Trash2 size={16} />삭제</button>
                {approveWarning ? <span className="bulk-approve-warning">{approveWarning}</span> : null}
              </div>
            ) : null}
          </div>
          <div className="bulk-request-list">
            {requests.requests?.length ? requests.requests.map((request) => (
              isAdmin ? (
                <button
                  key={request.id}
                  type="button"
                  className={`bulk-request-item ${selectedRequestId === request.id ? "active" : ""}`}
                  onClick={() => loadRequestDetail(request.id, 1).catch((err) => setMessage(err.message, true))}
                >
                  <strong>{bulkOperationLabel(request.targetText, request.replacementText)}</strong>
                  <span>{request.submittedUsername} · {formatTime(request.createdAt)}</span>
                  <span>{request.groups?.length ? request.groups.join(", ") : "전체"}</span>
                </button>
              ) : (
                <article key={request.id} className="bulk-request-item">
                  <strong>{bulkOperationLabel(request.targetText, request.replacementText)}</strong>
                  <span className="bulk-pending-label">승인대기중</span>
                  <span>{formatTime(request.createdAt)}</span>
                  <span>{request.groups?.length ? request.groups.join(", ") : "전체"}</span>
                </article>
              )
            )) : <div className="translation-status">{isAdmin ? "대기 중인 일괄수정 요청이 없습니다." : "승인대기중인 제출이 없습니다."}</div>}
          </div>
        </section>
      </section>

      <div className={`translation-status ${isError ? "missing" : ""}`}>{statusText}</div>
      <BulkResults
        mode={resultMode}
        result={result}
        targetText={result?.targetText ?? targetText}
        replacementText={result?.replacementText ?? replacementText}
        goToPage={(page) => goToPage(page).catch((err) => setMessage(err.message, true))}
      />
    </>
  );
}

function CategoryDropdown({ groups, selectedGroups, setSelectedGroups }) {
  function toggleGroup(value) {
    setSelectedGroups((current) => (
      current.includes(value)
        ? current.filter((item) => item !== value)
        : [...current, value]
    ));
  }

  return (
    <label className="bulk-category-label">카테고리
      <details className="bulk-category-menu">
        <summary>{selectedGroupLabel(groups, selectedGroups)}</summary>
        <div className="bulk-category-options">
          <label className="check"><input type="checkbox" checked={!selectedGroups.length} onChange={() => setSelectedGroups([])} /> 전체</label>
          {groups.map((group) => (
            <label key={group.value} className="check">
              <input
                type="checkbox"
                checked={selectedGroups.includes(group.value)}
                onChange={() => toggleGroup(group.value)}
              />
              {group.label} <span>{Number(group.count || 0).toLocaleString()}</span>
            </label>
          ))}
        </div>
      </details>
    </label>
  );
}

function BulkResults({ mode, result, targetText, replacementText, goToPage }) {
  if (!result) {
    return <div className="translation-status">검색 또는 치환을 실행하면 결과가 표시됩니다.</div>;
  }

  return (
    <section className="bulk-results">
      <div className="bulk-results-head">
        <h2>{resultTitle(mode)}</h2>
        <span>{Number(result.totalRows || 0).toLocaleString()}행 · {result.page}/{result.totalPages}페이지</span>
      </div>
      <BulkPagination result={result} goToPage={goToPage} />
      <div className="bulk-result-list">
        {result.rows?.length ? result.rows.map((row) => (
          <article key={`${row.rowNumber}-${row.sha1}`} className={`bulk-result-row ${row.hasConflict ? "conflict" : ""}`}>
            <div className="bulk-row-meta">
              <strong>행 {row.rowNumber}</strong>
              <span>{row.verifiedGroup || "미분류"}</span>
              {row.conflictReason ? <span className="bulk-approve-warning">{row.conflictReason}</span> : null}
            </div>
            {mode === "search" ? (
              <p className="bulk-single-text">{splitHighlighted(row.korean, targetText, "bulk-highlight-search")}</p>
            ) : (
              <div className="bulk-before-after">
                <div>
                  <b>치환전</b>
                  <p>{splitHighlighted(row.before, targetText, "bulk-highlight-before")}</p>
                </div>
                <div>
                  <b>치환후</b>
                  <p>{replacementText ? splitHighlighted(row.after, replacementText, "bulk-highlight-after") : row.after}</p>
                </div>
                {row.hasConflict ? (
                  <div>
                    <b>현재값</b>
                    <p>{row.currentKorean}</p>
                  </div>
                ) : null}
              </div>
            )}
          </article>
        )) : <div className="translation-status">표시할 결과가 없습니다.</div>}
      </div>
      <BulkPagination result={result} goToPage={goToPage} />
    </section>
  );
}

function BulkPagination({ result, goToPage }) {
  return (
    <div className="translation-toolbar">
      <button type="button" disabled={result.page <= 1} onClick={() => goToPage(Math.max(1, result.page - 1))}><ChevronLeft size={16} />이전</button>
      <span>{result.page}/{result.totalPages}</span>
      <button type="button" disabled={result.page >= result.totalPages} onClick={() => goToPage(Math.min(result.totalPages, result.page + 1))}>다음<ChevronRight size={16} /></button>
      <span>페이지당 {result.pageSize}개</span>
    </div>
  );
}
