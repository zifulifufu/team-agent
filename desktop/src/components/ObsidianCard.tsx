import { useCallback, useEffect, useState } from "react";
import { FolderOpen, LoaderCircle, RefreshCw } from "lucide-react";
import { api, relTime, type ObsidianReport, type ObsidianStatus } from "../api";
import { tr, useI18n } from "../i18n";
import { Switch, useConfirm } from "../ui";

/**
 * Suggested vault subfolder. Kept ASCII and language-independent on purpose: it is a path on
 * disk, and switching the interface language must not silently create a second folder.
 */
const FOLDER_NAME = "Team Agent Memories";

function summary(r: ObsidianReport): string {
  if (!r.ok) return tr("Failed: {error}", { error: r.error });
  const parts = [
    r.written && tr("wrote {n}", { n: r.written }),
    r.pulled && tr("read back {n} edits", { n: r.pulled }),
    r.imported && tr("imported {n} new notes", { n: r.imported }),
    r.deleted_memories && tr("deleted {n} memories", { n: r.deleted_memories }),
    r.removed_files && tr("moved {n} files aside", { n: r.removed_files }),
    r.conflicts && tr("{n} conflicts (the newer side wins; the older copy is kept in the conflict backup folder)", { n: r.conflicts }),
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : tr("Everything is up to date — nothing to sync");
}

/** The "Sync to Obsidian" card on the Memory page: pick a folder in your vault, sync both ways. */
export default function ObsidianCard({ onSynced }: { onSynced: () => void }) {
  const { t } = useI18n();
  const confirm = useConfirm();
  const [st, setSt] = useState<ObsidianStatus | null>(null);
  const [dir, setDir] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const pick = window.teamAgent?.pickFolder;

  const load = useCallback(() => api.obsidian().then(setSt).catch((e) => setErr((e as Error).message)), []);
  useEffect(() => { void load(); }, [load]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setErr("");
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      await load();   // Read the state back either way: if the folder was saved but the first sync failed, the UI must still show it as enabled
      setBusy(false);
    }
  };
  const sync = (force = false) => run(async () => { await api.obsidianSync(force); onSynced(); });
  const enable = () => run(async () => { await api.setObsidian({ dir: dir.trim() }); setDir(""); await api.obsidianSync(); onSynced(); });
  const browse = async () => { const p = await pick?.(); if (p) setDir(p); };

  if (!st) return err ? <div className="err kn-block">{err}</div> : null;
  const last = st.last;
  // A flag from the backend rather than a phrase to match on: warning text follows the request language
  const massWarn = last?.mass_missing === true;

  return (
    <div className="card kn-obs">
      <div className="sr-title">{t("Sync to Obsidian")}</div>
      <div className="sr-desc">
        {t("Mirror your memories into a folder in your Obsidian vault: each memory becomes an .md note, with properties at the top recording its scope and kind. Rename, create or delete notes in Obsidian and the app follows after a sync; changes made in the app are written back too.")}
        {" "}
        {t("Only preferences / facts / decisions / lessons sync — the running log of what the app did is left out. Pick a dedicated folder: every .md note inside it is treated as a memory.")}
      </div>
      {err && <div className="err small" role="alert">{err}</div>}

      {!st.dir ? (
        <div className="kn-obs-setup">
          {(st.vaults?.length ?? 0) > 0 && (
            <div className="kn-obs-vaults">
              <span className="muted small">{t("Obsidian vaults on this machine:")}</span>
              {st.vaults!.map((v) => (
                <button key={v.path} className="btn small" onClick={() => setDir(`${v.path}/${FOLDER_NAME}`)} title={v.path}>{v.name}</button>
              ))}
            </div>
          )}
          <div className="input-group">
            <input value={dir} onChange={(e) => setDir(e.target.value)} placeholder={t("Full path to the folder, e.g. /Users/you/Documents/MyVault/Team Agent Memories")} aria-label={t("Obsidian folder path")} spellCheck={false} />
            {pick && <button className="btn" onClick={() => void browse()}><FolderOpen size={14} /> {t("Choose…")}</button>}
            <button className="btn primary" disabled={busy || !dir.trim()} onClick={() => void enable()}>{busy ? t("Syncing…") : t("Enable and sync")}</button>
          </div>
          <div className="muted small">{t("The folder is created if it does not exist. Selecting it runs the first sync right away.")}</div>
        </div>
      ) : (
        <div className="kn-obs-on">
          <div className="kn-obs-path"><FolderOpen size={14} aria-hidden /> <code>{st.dir}</code></div>
          <div className="muted small">
            {st.exists
              ? t("{notes} notes in the folder, mapped to {mapped} memories", { notes: st.notes, mapped: st.mapped })
              : t("The folder does not exist or is unavailable right now (an external drive that is not mounted?) — syncing will not change anything")}
            {st.exists && !st.in_vault && " " + t("· This location is not inside any Obsidian vault (no .obsidian found); notes are still written, but Obsidian cannot open them unless you move the folder into a vault")}
          </div>
          {last && (
            <div className={"kn-obs-last" + (last.ok ? "" : " bad")}>
              {t("Last sync {when}:", { when: relTime(last.at) })} {summary(last)}
              {last.warnings.length > 0 && (
                <ul className="kn-obs-warn">{last.warnings.slice(0, 6).map((w, i) => <li key={i}>{w}</li>)}</ul>
              )}
            </div>
          )}
          <div className="kn-obs-acts">
            <button className="btn small primary" disabled={busy} onClick={() => void sync()}>
              {busy ? <LoaderCircle size={13} className="kn-spin" /> : <RefreshCw size={13} />} {t("Sync now")}
            </button>
            {massWarn && (
              <button className="btn small" disabled={busy} onClick={async () => { if (await confirm(t("Delete these memories to match the Obsidian folder as it is now?"), { okText: t("Force sync") })) void sync(true); }}>{t("Force sync")}</button>
            )}
            <label className="check-inline"><Switch checked={st.auto} label={t("Auto sync")} onChange={(v) => void run(() => api.setObsidian({ auto: v }))} /> {t("Auto sync (every 30 seconds)")}</label>
            <span className="grow" />
            <button className="btn small ghost" disabled={busy} onClick={async () => { if (await confirm(t("Turn off sync? Notes already written and memories in the app are kept; they just stop syncing with each other."), { okText: t("Turn off") })) void run(() => api.setObsidian({ dir: "", auto: false })); }}>{t("Turn off")}</button>
          </div>
        </div>
      )}
    </div>
  );
}
