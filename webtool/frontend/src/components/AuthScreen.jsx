import { useState } from "react";

import { api } from "../api/client.js";


export function AuthScreen({ mode, signupEnabled = true, onAuthenticated }) {
  const [username, setUsername] = useState("admin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [devVerificationUrl, setDevVerificationUrl] = useState("");
  const [entryMode, setEntryMode] = useState("login");
  const isSetup = mode === "setup";
  const isSignup = !isSetup && entryMode === "signup";

  async function submit(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    setDevVerificationUrl("");
    try {
      const path = isSetup ? "/api/auth/bootstrap" : isSignup ? "/api/auth/signup" : "/api/auth/login";
      const body = isSignup ? { username, email, password } : { username, password };
      const data = await api(path, {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (isSignup) {
        setMessage(data.mailSent ? "인증 메일을 보냈습니다. 메일의 링크를 열고 로그인하세요." : "SMTP 설정이 없어 개발용 인증 링크를 만들었습니다.");
        setDevVerificationUrl(data.devVerificationUrl || "");
        return;
      }
      onAuthenticated(data.user);
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <main className="auth-screen">
      <form className="auth-panel" onSubmit={submit}>
        <h1>기렌 한글화 웹툴</h1>
        <p>{isSetup ? "초기 관리자 계정을 생성합니다." : isSignup ? "이메일 인증 후 편집자 계정으로 가입합니다." : "계정으로 로그인하세요."}</p>
        <label>
          사용자 이름
          <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
        </label>
        {isSignup ? (
          <label>
            이메일
            <input value={email} onChange={(event) => setEmail(event.target.value)} type="email" autoComplete="email" />
          </label>
        ) : null}
        <label>
          비밀번호
          <input
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            type="password"
            autoComplete={isSetup || isSignup ? "new-password" : "current-password"}
          />
        </label>
        {error ? <div className="message error">{error}</div> : null}
        {message ? <div className="message success">{message}</div> : null}
        {devVerificationUrl ? (
          <a className="dev-verification-link" href={devVerificationUrl}>
            개발용 이메일 인증 열기
          </a>
        ) : null}
        <button type="submit">{isSetup ? "관리자 생성" : isSignup ? "회원가입" : "로그인"}</button>
        {!isSetup && signupEnabled ? (
          <button className="secondary" type="button" onClick={() => setEntryMode(isSignup ? "login" : "signup")}>
            {isSignup ? "로그인으로 돌아가기" : "회원가입"}
          </button>
        ) : null}
      </form>
    </main>
  );
}
