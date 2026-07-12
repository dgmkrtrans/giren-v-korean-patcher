import { useState } from "react";
import { Play, RotateCcw } from "lucide-react";

import { actionButtons } from "../data/toolForms.js";

export function SectionHead() {
  return null;
}

export function Field({ field, value, disabled, onChange, dynamicLists = {} }) {
  const id = `${field.name}-${field.label}`;
  const listOptions = dynamicLists[field.name] || field.list;
  if (field.type === "checkbox") {
    return (
      <label className={`check ${field.warning ? "warning" : ""}`}>
        <input id={id} type="checkbox" checked={Boolean(value)} disabled={disabled} onChange={(event) => onChange(field.name, event.target.checked)} />
        {field.label}
      </label>
    );
  }
  if (field.type === "textarea") {
    return (
      <label className={field.full ? "full" : ""}>
        {field.label}
        <textarea
          rows={field.rows || 3}
          value={value || ""}
          disabled={disabled}
          placeholder={field.placeholder || ""}
          onChange={(event) => onChange(field.name, event.target.value)}
        />
      </label>
    );
  }
  if (field.type === "select") {
    return (
      <label>
        {field.label}
        <select value={value || field.value || ""} disabled={disabled} onChange={(event) => onChange(field.name, event.target.value)}>
          {field.options.map((option) => (
            <option key={option} value={option}>{option}</option>
          ))}
        </select>
      </label>
    );
  }
  return (
    <label>
      {field.label}
      <input
        value={value || ""}
        disabled={disabled}
        type={field.type || "text"}
        min={field.min}
        max={field.max}
        step={field.step}
        list={listOptions ? `${field.name}-options` : undefined}
        placeholder={field.placeholder || ""}
        onChange={(event) => onChange(field.name, event.target.value)}
      />
      {listOptions ? (
        <datalist id={`${field.name}-options`}>
          {listOptions.map((option) => <option key={option} value={option} />)}
        </datalist>
      ) : null}
    </label>
  );
}

export function FormPanel({ panel, values, canEdit, onChange, onReset, onRun, dynamicLists = {} }) {
  return (
    <>
      <SectionHead title={panel.title} description={panel.description} />
      <form
        className="form-grid"
        onSubmit={(event) => {
          event.preventDefault();
          const fieldValues = panel.fields.length ? values : {};
          onRun({ ...fieldValues, ...(panel.payload || {}), action: panel.action });
        }}
      >
        {panel.fields.map((field) => (
          <Field key={field.name} field={field} value={values?.[field.name]} disabled={!canEdit} onChange={onChange} dynamicLists={dynamicLists} />
        ))}
        <div className="form-actions full">
          <button type="submit" disabled={!canEdit}>
            <Play size={16} />
            {panel.submit}
          </button>
          {panel.fields.length ? (
            <button type="button" className="secondary" disabled={!canEdit} onClick={onReset}>
              <RotateCcw size={16} />
              옵션 기본값
            </button>
          ) : null}
        </div>
      </form>
    </>
  );
}

export function RenderPanel({ subtabs, forms, canEdit, onChange, onReset, onRun, dynamicLists = {} }) {
  const [activeSubtab, setActiveSubtab] = useState("dialogue");
  const active = subtabs.find((tab) => tab.id === activeSubtab) || subtabs[0];
  const panel = active?.panel || null;
  const values = panel ? forms[panel.action] || {} : {};

  return (
    <>
      <div className="translation-group-tabs" role="tablist" aria-label="render 하위 탭">
        {subtabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            disabled={tab.disabled}
            aria-selected={activeSubtab === tab.id}
            className={`translation-group-tab ${activeSubtab === tab.id ? "active" : ""}`}
            onClick={() => setActiveSubtab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {panel ? (
        <FormPanel
          panel={panel}
          values={values}
          canEdit={canEdit}
          onRun={onRun}
          onChange={(name, value) => onChange(panel, name, value)}
          onReset={() => onReset(panel)}
          dynamicLists={dynamicLists}
        />
      ) : null}
    </>
  );
}

export function UnpackPanel({ canEdit, onRun }) {
  return (
    <>
      <SectionHead title="리소스 해체" description="QuickBMS/WINE 기반 추출 스크립트 실행" />
      <div className="actions">
        {actionButtons.unpack.map((item) => (
          <button key={item.action} type="button" disabled={!canEdit} onClick={() => onRun({ action: item.action })}>
            <Play size={16} />
            {item.label}
          </button>
        ))}
      </div>
    </>
  );
}
