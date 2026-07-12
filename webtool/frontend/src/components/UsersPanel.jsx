import { useEffect, useState } from "react";
import { UserPlus } from "lucide-react";

import { api } from "../api/client.js";
import { SectionHead } from "./ToolPanels.jsx";


export function UsersPanel() {
  const [users, setUsers] = useState([]);
  const [form, setForm] = useState({ username: "", password: "", role: "viewer" });
  const [resetPasswords, setResetPasswords] = useState({});
  const [message, setMessage] = useState("");

  async function refresh() {
    setUsers(await api("/api/users"));
  }

  async function createUser(event) {
    event.preventDefault();
    setMessage("");
    try {
      await api("/api/users", { method: "POST", body: JSON.stringify(form) });
      setForm({ username: "", password: "", role: "viewer" });
      await refresh();
      setMessage("사용자를 생성했습니다.");
    } catch (err) {
      setMessage(err.message);
    }
  }

  async function patchUser(id, payload) {
    await api(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
    await refresh();
  }

  async function resetPassword(event, id) {
    event.preventDefault();
    const password = resetPasswords[id] || "";
    setMessage("");
    try {
      await patchUser(id, { password });
      setResetPasswords((current) => ({ ...current, [id]: "" }));
      setMessage("비밀번호를 초기화했습니다.");
    } catch (err) {
      setMessage(err.message);
    }
  }

  useEffect(() => {
    refresh().catch((err) => setMessage(err.message));
  }, []);

  return (
    <>
      <SectionHead title="사용자 관리" description="관리자, 편집자, 관람자 계정을 관리" />
      <form className="user-create" onSubmit={createUser}>
        <input placeholder="username" value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} />
        <input placeholder="4자 이상 비밀번호" type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} />
        <select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value })}>
          <option value="viewer">viewer</option>
          <option value="editor">editor</option>
          <option value="admin">admin</option>
        </select>
        <button type="submit"><UserPlus size={16} />추가</button>
      </form>
      {message ? <div className="translation-status">{message}</div> : null}
      <div className="user-list">
        {users.map((item) => (
          <div className="user-row" key={item.id}>
            <div className="user-identity">
              <strong>{item.username}</strong>
              <span>{item.email ? `${item.email}${item.emailVerifiedAt ? "" : " · 미인증"}` : "관리자 생성 계정"}</span>
            </div>
            <select value={item.role} onChange={(event) => patchUser(item.id, { role: event.target.value })}>
              <option value="viewer">viewer</option>
              <option value="editor">editor</option>
              <option value="admin">admin</option>
            </select>
            <label className="check">
              <input type="checkbox" checked={item.isActive} onChange={(event) => patchUser(item.id, { isActive: event.target.checked })} />
              활성
            </label>
            <form className="password-reset" onSubmit={(event) => resetPassword(event, item.id)}>
              <input
                placeholder="새 비밀번호"
                type="password"
                value={resetPasswords[item.id] || ""}
                onChange={(event) => setResetPasswords((current) => ({ ...current, [item.id]: event.target.value }))}
              />
              <button className="secondary" type="submit">암호초기화</button>
            </form>
          </div>
        ))}
      </div>
    </>
  );
}
