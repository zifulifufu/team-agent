import { useState, type ReactNode } from "react";
import { BookOpen, Boxes, Download, HardDrive, Package, Puzzle, Sparkles, type LucideIcon } from "lucide-react";
import { api, type UpdateItem } from "../api";
import { useI18n } from "../i18n";
import { useConfirm } from "../ui";
import { agoIso, fmtBytes, isHttps, SourceBadge, Spin } from "./ExtBits";
import PluginInstallModal from "./PluginInstall";
import type { SettingsTab } from "../settings/SettingsModal";

// English labels, translated where they are shown (a module-level tr() would be frozen at import).
const KIND_META: Record<UpdateItem["kind"], { icon: LucideIcon; label: string }> = {
  app: { icon: Package, label: "App" },
  catalog: { icon: BookOpen, label: "Model catalog" },
  skill: { icon: Sparkles, label: "Skill" },
  plugin: { icon: Puzzle, label: "Plugin" },
  model: { icon: Boxes, label: "New model" },
  localmodel: { icon: HardDrive, label: "Local model" },
  localcatalog: { icon: BookOpen, label: "Local model catalog" },
};

const s = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v));

/** One reminder, rendered according to its kind.
 *
 *  Shared by the two settings pages that show reminders — Discover (skills, plugins, catalogs,
 *  provider and local models) and Software update (the app itself) — so a row looks and behaves the
 *  same wherever it turns up. Which page shows which kind is the pages' business; this component
 *  renders whatever it is handed, and `onTab` is only used by the rows that offer a jump. */
export function UpdateRow({ item, onTab, onDone }: { item: UpdateItem; onTab: (t: SettingsTab) => void; onDone: (okText?: string) => Promise<void> }) {
  const { t } = useI18n();
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [reinstall, setReinstall] = useState(false);
  const d = item.detail;
  const meta = KIND_META[item.kind] ?? KIND_META.app;

  const run = async (fn: () => Promise<string | void>) => {
    setBusy(true);
    setErr("");
    try {
      const text = await fn();
      await onDone(text || undefined);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  let body: ReactNode = null;
  let actions: ReactNode = null;

  if (item.kind === "app") {
    const assets = Array.isArray(d.assets) ? (d.assets as { name?: unknown; size?: unknown; url?: unknown }[]) : [];
    body = (
      <>
        <div>{t("Current version")} {s(d.current) || "?"} → {t("latest")} <b>{s(d.latest)}</b>{d.published_at ? <span className="muted"> · {t("published {when}", { when: agoIso(s(d.published_at)) || s(d.published_at) })}</span> : null}</div>
        <div><b>{t("The app is never replaced automatically — download the installer to update by hand.")}</b></div>
        {s(d.notes) && (
          <details className="ext-details" style={{ marginTop: 6 }}>
            <summary>{t("Release notes")}</summary>
            <div className="ext-notes">{s(d.notes)}</div>
          </details>
        )}
        {assets.length > 0 && (
          <div className="ext-assets" aria-label={t("Installers")}>
            {assets.map((a, i) => (
              <div key={i}>
                {isHttps(a.url) ? <a className="link" href={a.url} target="_blank" rel="noreferrer">{s(a.name) || a.url}</a> : <span>{s(a.name)}</span>}
                {typeof a.size === "number" && a.size > 0 && <span className="muted"> · {fmtBytes(a.size)}</span>}
              </div>
            ))}
          </div>
        )}
      </>
    );
    actions = isHttps(d.url) ? <a className="btn small" href={d.url} target="_blank" rel="noreferrer"><Download size={13} /> {t("Open the release page")}</a> : <span className="muted small">{t("No release page link available")}</span>;
  } else if (item.kind === "catalog") {
    body = (
      <>
        <div>{t("Current")} {s(d.current)} → {t("latest")} {s(d.latest)}{typeof d.new_models === "number" ? t(", {n} new models", { n: d.new_models }) : ""}</div>
        <div className="muted">{t("Only the model list and strength tags are updated; models and routes you added are left alone.")}</div>
      </>
    );
    actions = (
      <button className="btn small primary" disabled={busy} onClick={() => void run(async () => {
        const r = await api.applyCatalog();
        return r.applied ? t("Applied the new model catalog ({v}).", { v: s(r.latest) }) : t("The catalog did not change.");
      })}>{busy ? <><Spin size={12} /> {t("Applying")}</> : t("Apply the new catalog")}</button>
    );
  } else if (item.kind === "skill") {
    const name = s(d.name) || item.ref;
    body = <div>{t("The skill files on GitHub have changed. Updating overwrites the local skill with the latest content, so any local edits are lost.")}{s(d.repo) && <> {t("Source:")} <SourceBadge repo={s(d.repo)} path={s(d.path)} /></>}</div>;
    actions = (
      <button className="btn small primary" disabled={busy} onClick={async () => {
        if (!(await confirm(t("Update the skill {name} with the latest content from GitHub? This overwrites the local copy and any local edits are lost.", { name }), { okText: t("Update and overwrite") }))) return;
        void run(async () => { await api.updateSkill(name); return t("The skill {name} was updated.", { name }); });
      }}>{busy ? <><Spin size={12} /> {t("Updating")}</> : t("Update")}</button>
    );
  } else if (item.kind === "plugin") {
    const repo = s(d.repo), path = s(d.path);
    body = (
      <>
        <div>{t("The plugin files on GitHub have changed.")}{repo && <> {t("Source:")} <SourceBadge repo={repo} path={path} /></>}</div>
        <div className="muted">{t("A plugin is code that executes, so it cannot be updated in place: read the new source and confirm before it is reinstalled.")}</div>
      </>
    );
    actions = <button className="btn small primary" disabled={!repo || !path} onClick={() => setReinstall(true)}>{t("Review and reinstall")}</button>;
  } else if (item.kind === "localmodel") {
    const src = s(d.source);
    const tag = s(d.tag);
    body = src === "ollama-release" ? (
      <div>{t("Local Ollama {local}; latest on GitHub {latest}. New models often need a newer Ollama.", { local: s(d.local) || "?", latest: s(d.latest) })} <b>{t("The app never upgrades Ollama automatically")}</b>{t(" — install it yourself.")}</div>
    ) : (
      <>
        <div>{s(d.desc) || t("A new open-source model was found")}{d.size_gb ? t(", about {n} GB", { n: s(d.size_gb) }) : ""}{s(d.license) ? ` · ${s(d.license)}` : ""}</div>
        {tag ? <div className="muted">{t("The Ollama registry confirms this model exists. Add to recommendations only puts it on the local models page — nothing is downloaded.")}</div>
              : <div className="muted">{t("This came from {src}. It is only a heads-up: there may be no Ollama build, so check the licence and hardware requirements yourself.", { src: src === "hf" ? "Hugging Face" : "GitHub" })}</div>}
      </>
    );
    actions = (
      <>
        {tag && (
          <button className="btn small primary" disabled={busy} onClick={() => void run(async () => { await api.localAdd(tag); return t("Added {tag} to the local model recommendations.", { tag }); })}>{t("Add to recommendations")}</button>
        )}
        {isHttps(d.url) && <a className="btn small" href={d.url} target="_blank" rel="noreferrer">{t("Open the page")}</a>}
        <button className="btn small" onClick={() => onTab("local")}>{t("Go to local models")}</button>
      </>
    );
  } else if (item.kind === "localcatalog") {
    body = <div>{t("Current")} {s(d.current)} → {t("latest")} {s(d.latest)}. {t("The catalog is just a list of models and sizes — it contains no code.")}</div>;
    actions = (
      <button className="btn small primary" disabled={busy} onClick={() => void run(async () => {
        const r = await api.applyLocalCatalog();
        return r.applied ? t("Applied the new local model catalog ({v}).", { v: s(r.latest) }) : t("The catalog did not change.");
      })}>{busy ? <><Spin size={12} /> {t("Applying")}</> : t("Apply the new catalog")}</button>
    );
  } else if (item.kind === "model") {
    const ids = Array.isArray(d.ids) ? (d.ids as unknown[]).map(s) : [];
    body = (
      <>
        <div>{t("The live provider list contains models you have not seen yet.")}</div>
        {ids.length > 0 && (
          <div className="ext-chips">
            {ids.slice(0, 8).map((id) => <span key={id} className="ext-tool-chip">{id}</span>)}
            {ids.length > 8 && <span className="muted small">{t("…{n} in total", { n: ids.length })}</span>}
          </div>
        )}
      </>
    );
    actions = <button className="btn small primary" onClick={() => onTab("providers")}>{t("Pick some")}</button>;
  }

  return (
    <div className="ext-upd">
      <div className="ext-upd-ico"><meta.icon size={17} /></div>
      <div className="ext-upd-main">
        <div className="ext-upd-title">{item.title}<span className="tag">{t(meta.label)}</span></div>
        <div className="ext-upd-body">{body}</div>
        {err && <div className="ext-errline">{err}</div>}
        <div className="ext-upd-actions">
          {actions}
          <button className="btn small ghost" disabled={busy} onClick={() => void run(async () => { await api.dismissUpdate(item.id); })}>{t("Ignore")}</button>
        </div>
      </div>
      {reinstall && (
        <PluginInstallModal
          repo={s(d.repo)}
          path={s(d.path)}
          gitRef={s(d.ref)}
          overwrite
          onClose={() => setReinstall(false)}
          onInstalled={() => { setReinstall(false); void onDone(t("The plugin {name} was reinstalled.", { name: item.ref })); }}
        />
      )}
    </div>
  );
}
