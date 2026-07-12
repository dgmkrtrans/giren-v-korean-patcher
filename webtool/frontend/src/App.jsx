import { useEffect, useRef, useState } from "react";

import { api } from "./api/client.js";
import { AuthScreen } from "./components/AuthScreen.jsx";
import { BulkTranslationPanel } from "./components/BulkTranslationPanel.jsx";
import { FonttileTranslationPanel } from "./components/FonttileTranslationPanel.jsx";
import { Header, LogArea } from "./components/Chrome.jsx";
import { GraphicTranslationPanel } from "./components/GraphicTranslationPanel.jsx";
import { NoticePanel } from "./components/NoticePanel.jsx";
import { FormPanel, RenderPanel, UnpackPanel } from "./components/ToolPanels.jsx";
import { TranslationPanel } from "./components/TranslationPanel.jsx";
import { UsersPanel } from "./components/UsersPanel.jsx";
import { defaultForms, defaultPanelValues, formPanels, normalizeStoredForms, renderSubtabs, tabs } from "./data/toolForms.js";
import { can } from "./utils/roles.js";


export function App() {
  const [user, setUser] = useState(null);
  const [authMode, setAuthMode] = useState("login");
  const [signupEnabled, setSignupEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState("unpack");
  const [forms, setForms] = useState(defaultForms);
  const [fontOptions, setFontOptions] = useState(["auto"]);
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [selectedJob, setSelectedJob] = useState(null);
  const [commandOnly, setCommandOnly] = useState(false);
  const [logError, setLogError] = useState("");
  const restored = useRef(false);
  const saveTimer = useRef(null);
  const selectedJobIdRef = useRef(null);

  const isAdmin = can(user, "admin");
  const canEdit = can(user, "editor");
  const viewerTabs = ["translate", "bulk-translate", "fonttile", "graphic", "notice", "notifications"];
  const visibleTabs = isAdmin ? tabs : tabs.filter((tab) => viewerTabs.includes(tab.id));

  async function refreshSelectedJob(jobId = selectedJobId) {
    if (!jobId || !isAdmin) {
      return;
    }
    setSelectedJob(await api(`/api/jobs/${jobId}`));
  }

  function saveStateSoon() {
    if (!restored.current) {
      return;
    }
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      api("/api/state", {
        method: "POST",
        body: JSON.stringify({ activeTab, selectedJobId, forms }),
      }).catch(() => {});
    }, 250);
  }

  async function runAction(payload) {
    setLogError("");
    try {
      if (isAdmin && commandOnly && !["translate", "graphic"].includes(renderedActiveTab)) {
        const job = await api("/api/run/command", { method: "POST", body: JSON.stringify(payload) });
        setSelectedJobId(null);
        setSelectedJob(job);
        return;
      }
      const job = await api("/api/run", { method: "POST", body: JSON.stringify(payload) });
      setSelectedJobId(job.id);
      setSelectedJob(job);
      await refreshSelectedJob(job.id);
    } catch (err) {
      setLogError(err.message);
    }
  }

  async function cancelJob() {
    if (!selectedJobId) {
      return;
    }
    await api(`/api/jobs/${selectedJobId}/cancel`, { method: "POST" });
    await refreshSelectedJob(selectedJobId);
  }

  async function logout() {
    await api("/api/auth/logout", { method: "POST" });
    setUser(null);
    setAuthMode("login");
  }

  useEffect(() => {
    async function boot() {
      try {
        const me = await api("/api/auth/me");
        setUser(me.user);
      } catch {
        const setup = await api("/api/auth/setup");
        setAuthMode(setup.needsSetup ? "setup" : "login");
        setSignupEnabled(setup.signupEnabled !== false);
      } finally {
        setLoading(false);
      }
    }
    boot();
  }, []);

  useEffect(() => {
    if (!user) {
      return;
    }
    async function loadInitialData() {
      const [state, fonts] = await Promise.all([api("/api/state"), api("/api/fonts")]);
      setFontOptions(fonts.map((font) => font.value).filter(Boolean));
      if (state.forms) {
        setForms((current) => ({ ...current, ...normalizeStoredForms(state.forms) }));
      }
      if (state.activeTab) {
        setActiveTab(can(user, "admin") || viewerTabs.includes(state.activeTab) ? state.activeTab : "translate");
      }
      if (state.selectedJobId && can(user, "admin")) {
        setSelectedJobId(state.selectedJobId);
        await refreshSelectedJob(state.selectedJobId);
      }
      restored.current = true;
    }
    loadInitialData().catch((err) => setLogError(err.message));
    if (!can(user, "admin")) {
      return undefined;
    }
    const interval = setInterval(() => refreshSelectedJob(selectedJobIdRef.current).catch(() => {}), 1500);
    return () => clearInterval(interval);
  }, [user]);

  useEffect(saveStateSoon, [activeTab, selectedJobId, forms]);

  useEffect(() => {
    selectedJobIdRef.current = selectedJobId;
  }, [selectedJobId]);

  useEffect(() => {
    if (user && !isAdmin && !viewerTabs.includes(activeTab)) {
      setActiveTab("translate");
    }
  }, [activeTab, isAdmin, user]);

  if (loading) {
    return <main className="auth-screen"><div className="auth-panel">로딩 중...</div></main>;
  }
  if (!user) {
    return <AuthScreen mode={authMode} signupEnabled={signupEnabled} onAuthenticated={setUser} />;
  }

  const renderedActiveTab = isAdmin ? activeTab : (["bulk-translate", "fonttile", "graphic", "notice", "notifications"].includes(activeTab) ? activeTab : "translate");
  const activePanel = renderedActiveTab === "render" ? null : formPanels[renderedActiveTab];
  const canViewActivePanel = isAdmin || renderedActiveTab === "translate";
  const selectedValues = activePanel ? forms[activePanel.action] || {} : {};

  return (
    <>
      <Header user={user} isAdmin={isAdmin} commandOnly={commandOnly} onCommandOnlyChange={setCommandOnly} onLogout={logout} />
      <main className="layout">
        <section className="workspace">
          <nav className="tabs" aria-label="기능 탭">
            {visibleTabs.map((tab) => (
              <button key={tab.id} className={`tab ${renderedActiveTab === tab.id ? "active" : ""}`} type="button" onClick={() => setActiveTab(tab.id)}>
                {tab.label}
              </button>
            ))}
          </nav>
          <section className="panel active">
            {renderedActiveTab === "unpack" && isAdmin ? <UnpackPanel canEdit={canEdit} onRun={runAction} /> : null}
            {renderedActiveTab === "render" && isAdmin ? (
              <RenderPanel
                subtabs={renderSubtabs}
                forms={forms}
                canEdit={canEdit}
                onRun={runAction}
                onChange={(panel, name, value) => {
                  setForms((current) => ({
                    ...current,
                    [panel.action]: { ...(current[panel.action] || {}), [name]: value },
                  }));
                }}
                onReset={(panel) => {
                  setForms((current) => ({
                    ...current,
                    [panel.action]: defaultPanelValues(panel),
                  }));
                }}
                dynamicLists={{ font: fontOptions }}
              />
            ) : null}
            <div hidden={renderedActiveTab !== "translate"}>
              <TranslationPanel canEdit={canEdit} isAdmin={isAdmin} onDirtyState={saveStateSoon} />
            </div>
            <div hidden={renderedActiveTab !== "bulk-translate"}>
              <BulkTranslationPanel canEdit={canEdit} isAdmin={isAdmin} />
            </div>
            <div hidden={renderedActiveTab !== "fonttile"}>
              <FonttileTranslationPanel canEdit={canEdit} isAdmin={isAdmin} onDirtyState={saveStateSoon} onRun={runAction} />
            </div>
            <div hidden={renderedActiveTab !== "graphic"}>
              <GraphicTranslationPanel isAdmin={isAdmin} onDirtyState={saveStateSoon} />
            </div>
            {renderedActiveTab === "notice" ? <NoticePanel canEdit={isAdmin} view="notice" /> : null}
            {renderedActiveTab === "notifications" ? <NoticePanel canEdit={isAdmin} view="notifications" /> : null}
            {renderedActiveTab === "users" && isAdmin ? <UsersPanel /> : null}
            {activePanel && canViewActivePanel ? (
              <FormPanel
                panel={activePanel}
                values={selectedValues}
                canEdit={canEdit}
                onRun={runAction}
                onChange={(name, value) => {
                  setForms((current) => ({
                    ...current,
                    [activePanel.action]: { ...(current[activePanel.action] || {}), [name]: value },
                  }));
                }}
                onReset={() => {
                  setForms((current) => ({
                    ...current,
                    [activePanel.action]: defaultPanelValues(activePanel),
                  }));
                }}
                dynamicLists={{ font: fontOptions }}
              />
            ) : null}
            {logError ? <div className="message error">{logError}</div> : null}
          </section>
        </section>
      </main>
      {isAdmin ? <LogArea job={selectedJob} canEdit={canEdit} onCancel={cancelJob} /> : null}
    </>
  );
}
