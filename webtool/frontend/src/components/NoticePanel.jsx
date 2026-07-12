import { useEffect, useState } from "react";
import { Bell, RefreshCw, Save, Trash2 } from "lucide-react";

import { api } from "../api/client.js";
import { SectionHead } from "./ToolPanels.jsx";


export function NoticePanel({ canEdit, view = "notice" }) {
  const [notice, setNotice] = useState("");
  const [draft, setDraft] = useState("");
  const [notifications, setNotifications] = useState([]);
  const [selectedNotification, setSelectedNotification] = useState(null);
  const [statusText, setStatusText] = useState("");
  const [isError, setIsError] = useState(false);

  function setMessage(message, error = false) {
    setStatusText(message);
    setIsError(error);
  }

  async function loadNotice() {
    setMessage("공지사항을 읽는 중...");
    const response = await api("/api/notice");
    setNotice(response.notice || "");
    setDraft(response.notice || "");
    setMessage("공지사항을 불러왔습니다.");
  }

  async function loadNotifications() {
    setMessage("알림을 읽는 중...");
    const response = await api("/api/translation/notifications");
    setNotifications(response.notifications || []);
    setMessage("알림을 불러왔습니다.");
  }

  async function openNotification(notification) {
    setMessage("알림 상세를 읽는 중...");
    const response = await api(`/api/translation/notifications/${notification.id}`);
    setSelectedNotification(response);
    setMessage("알림 상세를 불러왔습니다.");
  }

  async function deleteNotification(notification) {
    if (!window.confirm("알림 카드를 삭제할까요?")) {
      return;
    }
    setMessage("알림 삭제 중...");
    await api(`/api/translation/notifications/${notification.id}`, { method: "DELETE" });
    setNotifications((current) => current.filter((item) => item.id !== notification.id));
    if (selectedNotification?.notification?.id === notification.id) {
      setSelectedNotification(null);
    }
    setMessage("알림 카드를 삭제했습니다.");
  }

  async function sendNotifications() {
    setMessage("알림 전송 중...");
    const response = await api("/api/translation/notifications/send", { method: "POST" });
    await loadNotifications();
    setMessage(`${Number(response.notifications || 0).toLocaleString()}명에게 ${Number(response.items || 0).toLocaleString()}건의 알림을 전송했습니다.`);
  }

  async function saveNotice() {
    setMessage("공지사항 저장 중...");
    const response = await api("/api/notice", {
      method: "POST",
      body: JSON.stringify({ notice: draft }),
    });
    setNotice(response.notice || "");
    setDraft(response.notice || "");
    setMessage("공지사항을 저장했습니다.");
  }

  async function deleteNotice() {
    if (!window.confirm("공지사항을 삭제할까요?")) {
      return;
    }
    setMessage("공지사항 삭제 중...");
    const response = await api("/api/notice", { method: "DELETE" });
    setNotice(response.notice || "");
    setDraft(response.notice || "");
    setMessage("공지사항을 삭제했습니다.");
  }

  useEffect(() => {
    if (view === "notifications") {
      loadNotifications().catch((err) => setMessage(err.message, true));
      return;
    }
    loadNotice().catch((err) => setMessage(err.message, true));
  }, [view]);

  if (view === "notifications") {
    return (
      <>
        <SectionHead title="알림" description="관리자 병합/삭제 결과를 사용자별 카드로 확인" />
      <section className="notification-panel">
        <div className="notification-head">
          <h2>텍스트 병합 알림</h2>
          <div className="actions">
            <button type="button" className="secondary" onClick={() => loadNotifications().catch((err) => setMessage(err.message, true))}><RefreshCw size={16} />새로고침</button>
            {canEdit ? <button type="button" onClick={() => sendNotifications().catch((err) => setMessage(err.message, true))}><Bell size={16} />알림전송</button> : null}
          </div>
        </div>
        <div className="notification-grid">
          <div className="notification-cards">
            {notifications.length ? notifications.map((item) => (
              <article
                key={item.id}
                className={`notification-card ${selectedNotification?.notification?.id === item.id ? "active" : ""}`}
              >
                <button
                  type="button"
                  className="notification-open"
                  onClick={() => openNotification(item).catch((err) => setMessage(err.message, true))}
                >
                  <strong>{notificationTitle(item)}</strong>
                  <span>{formatNoticeTime(item.createdAt)}</span>
                </button>
                <button
                  type="button"
                  className="icon-button secondary danger-button"
                  title="알림 삭제"
                  onClick={() => deleteNotification(item).catch((err) => setMessage(err.message, true))}
                >
                  <Trash2 size={15} />
                </button>
              </article>
            )) : <div className="translation-status">도착한 병합 알림이 없습니다.</div>}
          </div>
          <NotificationDetail data={selectedNotification} />
        </div>
      </section>
        <div className={`translation-status ${isError ? "missing" : ""}`}>{statusText}</div>
      </>
    );
  }

  return (
    <>
      <SectionHead title="공지사항" description="웹툴 사용자에게 표시할 공지 내용을 관리" />
      {canEdit ? (
        <form className="notice-editor" onSubmit={(event) => { event.preventDefault(); saveNotice().catch((err) => setMessage(err.message, true)); }}>
          <label>공지 내용
            <textarea value={draft} rows={12} onChange={(event) => setDraft(event.target.value)} placeholder="공지사항을 입력하세요." />
          </label>
          <div className="actions">
            <button type="submit"><Save size={16} />수정 저장</button>
            <button type="button" className="secondary" onClick={() => loadNotice().catch((err) => setMessage(err.message, true))}><RefreshCw size={16} />다시 읽기</button>
            <button type="button" className="secondary danger-button" disabled={!notice.trim() && !draft.trim()} onClick={() => deleteNotice().catch((err) => setMessage(err.message, true))}><Trash2 size={16} />삭제</button>
          </div>
        </form>
      ) : null}
      <section className="notice-view" aria-label="공지사항 내용">
        {notice.trim() ? <pre>{notice}</pre> : <p>등록된 공지사항이 없습니다.</p>}
      </section>
      <div className={`translation-status ${isError ? "missing" : ""}`}>{statusText}</div>
    </>
  );
}


function notificationTitle(item) {
  const parts = [];
  if (Number(item.mergedCount || 0) > 0) {
    parts.push(`${Number(item.mergedCount).toLocaleString()}건이 병합됐습니다`);
  }
  if (Number(item.deletedCount || 0) > 0) {
    parts.push(`${Number(item.deletedCount).toLocaleString()}건 삭제됐습니다`);
  }
  return parts.join(", ") || "변경된 텍스트가 없습니다";
}


function formatNoticeTime(value) {
  if (!value) {
    return "";
  }
  return new Date(Number(value) * 1000).toLocaleString();
}


function NotificationDetail({ data }) {
  if (!data) {
    return <section className="notification-detail"><p>알림 카드를 선택하면 병합/삭제된 텍스트가 표시됩니다.</p></section>;
  }
  return (
    <section className="notification-detail">
      <h3>{notificationTitle(data.notification)}</h3>
      <div className="notification-detail-list">
        {data.items?.length ? data.items.map((item) => (
          <article key={item.id} className={`notification-detail-item ${item.action}`}>
            <div className="notification-detail-meta">
              <strong>{item.action === "merged" ? "병합" : "삭제"}</strong>
              <span>{item.csvPath} · {item.sha1}</span>
              <span>{formatNoticeTime(item.executedAt)}</span>
            </div>
            <div className="notification-text-pair">
              <div><b>원본</b><p>{item.original?.korean || ""}</p></div>
              <div><b>제출</b><p>{item.submitted?.korean || ""}</p></div>
              {item.action === "merged" ? <div><b>반영</b><p>{item.applied?.korean || ""}</p></div> : null}
            </div>
            {item.note ? <p className="notification-note"><b>비고</b> {item.note}</p> : null}
          </article>
        )) : <p>표시할 상세 항목이 없습니다.</p>}
      </div>
    </section>
  );
}
