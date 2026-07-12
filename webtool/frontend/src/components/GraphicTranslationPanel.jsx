import { useEffect, useMemo, useRef, useState } from "react";
import { Check, FileText, ImageUp, Inbox, RefreshCw, RotateCcw, Search, Trash2, Upload } from "lucide-react";

import { api, encodeQuery } from "../api/client.js";
import { SectionHead } from "./ToolPanels.jsx";


const csvPageSize = 15;

const defaultGraphicParams = {
  folder: "textures_static",
  search: "",
  showImages: true,
  blackBg: true,
  translatedRoot: "textures_translated",
};

function formatUploadTime(value) {
  if (!value) {
    return "";
  }
  return new Date(Number(value) * 1000).toLocaleString();
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("파일을 읽을 수 없습니다."));
    reader.readAsDataURL(file);
  });
}

export function GraphicTranslationPanel({ isAdmin, onDirtyState }) {
  const [folder, setFolder] = useState(defaultGraphicParams.folder);
  const [search, setSearch] = useState(defaultGraphicParams.search);
  const [showImages, setShowImages] = useState(defaultGraphicParams.showImages);
  const [blackBg, setBlackBg] = useState(defaultGraphicParams.blackBg);
  const [translatedRoot, setTranslatedRoot] = useState(defaultGraphicParams.translatedRoot);
  const [targetRowsText, setTargetRowsText] = useState("");
  const [data, setData] = useState({
    page: 1,
    totalPages: 1,
    totalRows: 0,
    allRows: 0,
    pageSize: csvPageSize,
    rows: [],
    csvPath: "",
    targetRows: 0,
    pendingUploadCount: 0,
  });
  const [uploadingRows, setUploadingRows] = useState({});
  const [approvalOpen, setApprovalOpen] = useState(false);
  const [approvalData, setApprovalData] = useState({ total: 0, valid: 0, invalid: 0, uploads: [] });
  const [statusText, setStatusText] = useState("");
  const [isError, setIsError] = useState(false);
  const autoLoaded = useRef(false);

  function setMessage(message, error = false) {
    setStatusText(message);
    setIsError(error);
  }

  async function loadPage(page = 1) {
    setMessage("그래픽 행을 읽는 중...");
    const response = await api(`/api/graphics?${encodeQuery({ folder, page, showImages, q: search.trim(), translatedRoot })}`);
    setData(response);
    setTargetRowsText(response.targetRowsText || "");
    setMessage(`그래픽 대상 ${Number(response.targetRows || 0).toLocaleString()}행 · 페이지당 ${response.pageSize}행.`);
  }

  async function loadApprovals() {
    if (!isAdmin) {
      return;
    }
    const response = await api(`/api/graphics/uploads?${encodeQuery({ folder, translatedRoot })}`);
    setApprovalData(response);
  }

  async function saveTargets() {
    setMessage("그래픽 작업 대상 저장 중...");
    try {
      const response = await api("/api/graphics/targets", {
        method: "POST",
        body: JSON.stringify({ folder, rowRanges: targetRowsText }),
      });
      setTargetRowsText(response.targetRowsText || "");
      await loadPage(1);
      setMessage(`${Number(response.targetRows || 0).toLocaleString()}개 CSV 행을 그래픽 작업 대상으로 저장했습니다.`);
    } catch (err) {
      setMessage(err.message, true);
    }
  }

  async function uploadImage(row, file) {
    if (!file) {
      return;
    }
    setUploadingRows((current) => ({ ...current, [row.rowNumber]: true }));
    setMessage(`행 ${row.rowNumber} 이미지 업로드 중...`);
    try {
      const contentBase64 = await fileToDataUrl(file);
      const response = await api("/api/graphics/upload", {
        method: "POST",
        body: JSON.stringify({ folder, rowNumber: row.rowNumber, filename: file.name, contentBase64 }),
      });
      await loadPage(data.page);
      if (approvalOpen) {
        await loadApprovals();
      }
      setMessage("업로드 완료. 관리자 압축체크 또는 팔레트체크 대기 중.");
    } catch (err) {
      setMessage(err.message, true);
    } finally {
      setUploadingRows((current) => ({ ...current, [row.rowNumber]: false }));
    }
  }

  async function approveUpload(upload) {
    setMessage("업로드 이미지 승인 중...");
    try {
      const response = await api("/api/graphics/uploads/approve", {
        method: "POST",
        body: JSON.stringify({ folder, translatedRoot, items: [{ rowNumber: upload.rowNumber, userId: upload.userId }] }),
      });
      await loadApprovals();
      await loadPage(data.page);
      const skipped = response.skipped?.length ? ` 보류 ${response.skipped.length.toLocaleString()}건.` : "";
      setMessage(`${response.approved.toLocaleString()}건 승인.${skipped}`);
    } catch (err) {
      setMessage(err.message, true);
    }
  }

  async function checkUpload(upload, mode) {
    setMessage(mode === "palette" ? "팔레트 변환 후 압축 체크 중..." : "업로드 원본 압축 체크 중...");
    try {
      const response = await api("/api/graphics/uploads/check", {
        method: "POST",
        body: JSON.stringify({ folder, rowNumber: upload.rowNumber, userId: upload.userId, mode }),
      });
      await loadApprovals();
      await loadPage(data.page);
      const errors = response.validation?.errors?.length || 0;
      setMessage(errors ? `체크 완료, 오류 ${errors}건.` : "체크 통과.");
    } catch (err) {
      setMessage(err.message, true);
    }
  }

  async function discardUpload(upload) {
    setMessage("업로드 삭제 중...");
    try {
      const response = await api("/api/graphics/uploads/discard", {
        method: "POST",
        body: JSON.stringify({ folder, items: [{ rowNumber: upload.rowNumber, userId: upload.userId }] }),
      });
      await loadApprovals();
      await loadPage(data.page);
      setMessage(`${response.deleted.toLocaleString()}건 삭제했습니다.`);
    } catch (err) {
      setMessage(err.message, true);
    }
  }

  async function writeManifest() {
    setMessage("manifest.json 작성 중...");
    try {
      const response = await api("/api/graphics/rebuild-manifest", {
        method: "POST",
        body: JSON.stringify({ folder, translatedRoot }),
      });
      await loadPage(data.page);
      setMessage(`${response.records.toLocaleString()}개 리빌드 레코드를 작성했습니다.`);
    } catch (err) {
      setMessage(err.message, true);
    }
  }

  function resetParams() {
    setFolder(defaultGraphicParams.folder);
    setSearch(defaultGraphicParams.search);
    setShowImages(defaultGraphicParams.showImages);
    setBlackBg(defaultGraphicParams.blackBg);
    setTranslatedRoot(defaultGraphicParams.translatedRoot);
    setMessage("기본값으로 되돌렸습니다.");
  }

  useEffect(() => {
    if (autoLoaded.current) {
      return;
    }
    autoLoaded.current = true;
    loadPage(1).catch((err) => setMessage(err.message, true));
  }, []);

  useEffect(() => {
    if (data.csvPath) {
      loadPage(data.page).catch((err) => setMessage(err.message, true));
    }
  }, [showImages]);

  useEffect(() => {
    onDirtyState();
  }, [folder, search, showImages, blackBg, translatedRoot, targetRowsText]);

  return (
    <>
      <SectionHead title="그래픽 번역" description="CSV 행 단위 그래픽 이미지를 검수하고 승인된 PNG를 리빌드 manifest 대상으로 등록" />
      <form className="form-grid" onSubmit={(event) => event.preventDefault()}>
        {isAdmin ? <label>CSV 폴더 또는 파일<input value={folder} onChange={(event) => setFolder(event.target.value)} /></label> : null}
        {isAdmin ? <label>한국어 이미지 폴더<input value={translatedRoot} onChange={(event) => setTranslatedRoot(event.target.value)} /></label> : null}
        <label>검색
          <input value={search} onChange={(event) => setSearch(event.target.value)} onKeyDown={(event) => event.key === "Enter" && loadPage(1)} />
          <button type="button" className="inline-field-button" onClick={() => loadPage(1)}><Search size={16} />검색 이동</button>
        </label>
        <label className="check"><input type="checkbox" checked={showImages} onChange={(event) => setShowImages(event.target.checked)} /> 이미지 표시</label>
        <label className="check"><input type="checkbox" checked={blackBg} onChange={(event) => setBlackBg(event.target.checked)} /> 이미지 검은 배경</label>
        {isAdmin ? (
          <label className="full">그래픽 작업 대상 CSV 행
            <textarea
              value={targetRowsText}
              rows={4}
              placeholder="예: 1-15, 42, 100-120"
              onChange={(event) => setTargetRowsText(event.target.value)}
            />
          </label>
        ) : null}
        <div className="actions full">
          <button type="button" onClick={() => loadPage(1)}><RefreshCw size={16} />읽기</button>
          {isAdmin ? <button type="button" onClick={saveTargets}><Check size={16} />대상 저장</button> : null}
          {isAdmin ? (
            <button
              type="button"
              className="secondary"
              onClick={() => {
                const nextOpen = !approvalOpen;
                setApprovalOpen(nextOpen);
                if (nextOpen) {
                  loadApprovals().catch((err) => setMessage(err.message, true));
                }
              }}
            >
              <Inbox size={16} />승인함
            </button>
          ) : null}
          {isAdmin ? <button type="button" className="secondary" onClick={writeManifest}><FileText size={16} />리빌딩 대상 추가</button> : null}
          <button type="button" className="secondary" onClick={resetParams}><RotateCcw size={16} />옵션 기본값</button>
        </div>
      </form>
      {approvalOpen && isAdmin ? (
        <GraphicApprovalPanel
          data={approvalData}
          blackBg={blackBg}
          onRefresh={loadApprovals}
          onCheck={checkUpload}
          onApprove={approveUpload}
          onDiscard={discardUpload}
        />
      ) : null}
      <GraphicPaginationToolbar data={data} setData={setData} loadPage={loadPage} />
      <div className={`translation-status ${isError ? "missing" : ""}`}>{statusText}</div>
      <div className="graphic-rows">
        {data.rows.length ? data.rows.map((row) => (
          <GraphicRow
            key={row.rowNumber}
            row={row}
            showImages={showImages}
            blackBg={blackBg}
            isUploading={Boolean(uploadingRows[row.rowNumber])}
            onUpload={uploadImage}
          />
        )) : <div className="translation-status">표시할 그래픽 행이 없습니다.</div>}
      </div>
      <GraphicPaginationToolbar data={data} setData={setData} loadPage={loadPage} />
    </>
  );
}


function GraphicPaginationToolbar({ data, setData, loadPage }) {
  return (
    <div className="translation-toolbar">
      <button type="button" disabled={data.page <= 1} onClick={() => loadPage(Math.max(1, data.page - 1))}>이전</button>
      <label>페이지<input type="number" min="1" value={data.page} onChange={(event) => setData((current) => ({ ...current, page: Number(event.target.value || 1) }))} /></label>
      <button type="button" onClick={() => loadPage(data.page)}>이동</button>
      <button type="button" disabled={data.page >= data.totalPages} onClick={() => loadPage(Math.min(data.totalPages, data.page + 1))}>다음</button>
      <span>{data.csvPath ? `대상 ${data.totalRows.toLocaleString()}행 · ${data.page}/${data.totalPages}페이지` : "아직 CSV를 읽지 않았습니다."}</span>
    </div>
  );
}


function GraphicRow({ row, showImages, blackBg, isUploading, onUpload }) {
  const validation = row.myUpload?.validation;
  const statusLabel = useMemo(() => {
    if (row.approvedExists && row.rebuildTarget) {
      return "리빌드 대상";
    }
    if (row.approvedExists) {
      return "완료";
    }
    if (row.myUpload) {
      return "승인 대기";
    }
    return "미작업";
  }, [row.approvedExists, row.myUpload, row.rebuildTarget]);

  return (
    <div className={`graphic-row ${showImages ? "with-image" : ""}`}>
      <div className="translation-number">
        <span>행 {row.rowNumber}</span>
        <small>{row.verifiedGroup || "미분류"}</small>
        <small className={row.approvedExists ? "ok" : ""}>{statusLabel}</small>
        {row.pendingUploadCount ? <small>대기 {row.pendingUploadCount}건</small> : null}
      </div>
      {showImages ? (
        <div className="graphic-image-pair">
          <ImageBox label="JP" url={row.imageUrl} blackBg={blackBg} />
          <ImageBox label={row.korean || "한국어"} url={row.koreanImageUrl} blackBg={blackBg} />
        </div>
      ) : null}
      <div className="graphic-meta">
        <b>{row.korean || "한국어"}</b>
        <span>{row.verifiedGroup || "미분류"}</span>
        {/* {row.approvedExists && !row.rebuildTarget ? <span className="translation-warning-text">manifest.json 미등록</span> : null} */}
      </div>
      <div className="graphic-upload">
        <label className="graphic-file-label">
          <ImageUp size={16} />
          PNG 업로드
          <input
            className="graphic-file-input"
            type="file"
            accept="image/png"
            disabled={isUploading}
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              onUpload(row, file);
            }}
          />
        </label>
        {isUploading ? <span className="translation-inline-meta">업로드 중...</span> : null}
        {row.myUpload ? <span className="translation-inline-meta">내 업로드 {formatUploadTime(row.myUpload.updatedAt)}</span> : null}
        <ValidationMessages validation={validation} />
      </div>
    </div>
  );
}


function GraphicApprovalPanel({ data, blackBg, onRefresh, onCheck, onApprove, onDiscard }) {
  return (
    <section className="draft-merge-panel">
      <div className="draft-merge-head">
        <div>
          <h2>관리자 승인함</h2>
          <p>
            전체 {Number(data.total || 0).toLocaleString()} · 승인가능 {Number(data.valid || 0).toLocaleString()} · 오류 {Number(data.invalid || 0).toLocaleString()}
          </p>
        </div>
        <div className="actions">
          <button type="button" className="secondary" onClick={onRefresh}><RefreshCw size={16} />새로고침</button>
        </div>
      </div>
      <div className="graphic-approval-list">
        {data.uploads?.length ? data.uploads.map((upload) => (
          <article key={`${upload.rowNumber}-${upload.userId}`} className={`graphic-approval-item ${upload.canApprove ? "" : "conflict"}`}>
            <div className="draft-meta">
              <strong>{upload.username}</strong>
              <span>행 {upload.rowNumber} · {upload.korean || "한국어"} · {upload.verifiedGroup || "미분류"}</span>
              <span>{formatUploadTime(upload.updatedAt)}</span>
            </div>
            <div className="graphic-approval-images">
              <ImageBox label="JP" url={upload.imageUrl} blackBg={blackBg} />
              <ImageBox label="업로드" url={upload.pendingImageUrl} blackBg={blackBg} />
              <ImageBox label={upload.korean || "한국어"} url={upload.koreanImageUrl} blackBg={blackBg} />
            </div>
            <div className="graphic-approval-side">
              <ValidationMessages validation={upload.validation} />
              <div className="draft-actions">
                <button type="button" className="secondary" onClick={() => onCheck(upload, "compression")}><Check size={15} />압축체크</button>
                <button type="button" className="secondary" onClick={() => onCheck(upload, "palette")}><Check size={15} />팔레트체크</button>
                <button type="button" disabled={!upload.canApprove} onClick={() => onApprove(upload)}><Upload size={15} />승인</button>
                <button type="button" className="secondary" onClick={() => onDiscard(upload)}><Trash2 size={15} />삭제</button>
              </div>
            </div>
          </article>
        )) : <div className="translation-status">대기 중인 업로드가 없습니다.</div>}
      </div>
    </section>
  );
}


function ValidationMessages({ validation }) {
  if (!validation) {
    return null;
  }
  const errors = validation.errors || [];
  const warnings = validation.warnings || [];
  const details = validation.details || [];
  const modeLabel = validation.mode === "palette" ? "팔레트체크" : validation.mode === "compression" ? "압축체크" : "";
  if (!errors.length && !warnings.length && !details.length) {
    return <span className="translation-inline-meta ok">리빌드 검증 통과</span>;
  }
  return (
    <div className="graphic-validation">
      {modeLabel ? <span className="translation-inline-meta ok">{modeLabel}</span> : null}
      {errors.map((message, index) => (
        <span key={`error-${index}`} className="translation-warning-text">{message}</span>
      ))}
      {warnings.map((message, index) => (
        <span key={`warning-${index}`} className="running">{message}</span>
      ))}
      {details.map((detail, index) => (
        <span key={`detail-${index}`} className={detail.fits ? "translation-inline-meta ok" : "translation-warning-text"}>
          {detail.source} · 슬롯 {Number(detail.slotSize || 0).toLocaleString()} · greedy {Number(detail.greedySize || 0).toLocaleString()}
          {detail.optimalSize ? ` · optimal ${Number(detail.optimalSize).toLocaleString()}` : ""}
        </span>
      ))}
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
