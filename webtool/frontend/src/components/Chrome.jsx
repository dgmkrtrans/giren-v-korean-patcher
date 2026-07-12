import { LogOut, Square } from "lucide-react";

function formatCommand(command) {
  return command.map((part, index) => {
    if (command[index - 1] === "--target-verified-group" && !part.startsWith("\"") && !part.endsWith("\"")) {
      return `"${part}"`;
    }
    return part;
  }).join(" ");
}

export function Header({ user, isAdmin, commandOnly, onCommandOnlyChange, onLogout }) {
  return (
    <header className="topbar">
      <div>
        <h1>기렌 한글화 웹툴</h1>
      </div>
      <div className="top-actions">
        {isAdmin ? (
          <label className="topbar-check">
            <input type="checkbox" checked={commandOnly} onChange={(event) => onCommandOnlyChange(event.target.checked)} />
            커맨드만 출력
          </label>
        ) : null}
        <span className={`role-badge ${user.role}`}>{user.username} · {user.role}</span>
        <button className="icon-button secondary" onClick={onLogout} title="로그아웃" type="button">
          <LogOut size={17} />
        </button>
      </div>
    </header>
  );
}

export function LogArea({ job, canEdit, onCancel }) {
  const command = job ? `$ ${formatCommand(job.command)}\n\n` : "";
  const output = job ? command + (job.output.length ? job.output.join("\n") : "실행 중...") : "아직 실행된 작업이 없습니다.";
  return (
    <section className="log-area">
      <div className="log-head">
        <h2>{job ? `${job.title} · ${job.status}` : "로그"}</h2>
        <button type="button" disabled={!canEdit || job?.status !== "running"} onClick={onCancel}><Square size={15} />중지</button>
      </div>
      <pre>{output}</pre>
    </section>
  );
}
