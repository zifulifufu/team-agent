import { useEffect, useRef, useState } from "react";
import { Download, Trash2, Upload } from "lucide-react";
import { api, downloadBackup, type SystemInfo } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Switch, useConfirm } from "../ui";
import { fmtBytes } from "../components/ExtBits";

/** Outcome of the last action, split so the styling never has to parse the message text. */
type Note = { ok: boolean; text: string };

export default function DataPage() {
  const { t } = useI18n();
  const { refreshAll, reloadGroups } = useData();
  const fileRef = useRef<HTMLInputElement>(null);
  const confirm = useConfirm();
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [keys, setKeys] = useState(false);
  const [msg, setMsg] = useState<Note | null>(null);
  useEffect(() => { api.system().then(setSys).catch(() => undefined); }, []);

  const exportDb = async () => {
    if (keys && !(await confirm(t("The backup file will contain your API keys in plain text. Keep it safe and do not forward it to anyone. Export anyway?"), { okText: t("Export anyway"), danger: false }))) return;
    try {
      await downloadBackup(keys);
      setMsg({ ok: true, text: t("Backup exported") });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  };

  const restore = async (f: File | undefined) => {
    if (!f) return;
    if (!(await confirm(t("Replace all current data (group chats, members, model configuration, memories, library, chat history) with the backup {name}? A copy of the current data is saved first so you can go back. If the backup has no keys, the API keys you have entered now are kept.", { name: f.name }), { okText: t("Restore"), danger: false }))) return;
    try {
      const r = await api.restoreBackup(f);
      await refreshAll();
      setMsg({ ok: true, text: t("Restored: {groups} groups, {agents} members, {memories} memories, {docs} documents. A copy of the pre-restore data was kept at {path}.", { groups: r.groups, agents: r.agents, memories: r.memories, docs: r.docs, path: r.safety_copy }) });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  };

  return (
    <div className="sp">
      <h2 className="sp-title">{t("Data")}</h2>
      <p className="sp-desc">{t("All configuration and chat history live in a local SQLite database on this machine — nothing is uploaded.")}</p>

      <div className="card flush">
        <div className="setting-row pad">
          <div><div className="sr-title">{t("Data directory")}</div><div className="sr-desc mono">{sys?.data_dir ?? "…"}</div></div>
        </div>
        <div className="setting-row pad">
          <div><div className="sr-title">{t("Database size")}</div></div>
          <span className="muted">{sys ? fmtBytes(sys.db_bytes) : "…"}</span>
        </div>
      </div>

      <h3 className="sec">{t("Backup")}</h3>
      <div className="card">
        <div className="setting-row">
          <div>
            <div className="sr-title">{t("Export database snapshot")}</div>
            <div className="sr-desc">{t("Includes group chats, members, model configuration and chat history. API keys are excluded by default; you will need to re-enter them after restoring.")}</div>
          </div>
          <button className="btn" onClick={exportDb}><Download size={15} /> {t("Export")}</button>
        </div>
        <div className="setting-row" style={{ marginTop: 12 }}>
          <div><div className="sr-title">{t("Also export API keys")}</div><div className="sr-desc">{t("Only turn this on when migrating to another computer of your own.")}</div></div>
          <Switch checked={keys} onChange={setKeys} label={t("Also export API keys")} />
        </div>
        <div className="setting-row" style={{ marginTop: 12 }}>
          <div><div className="sr-title">{t("Restore from backup")}</div><div className="sr-desc">{t("Pick a .db backup you exported earlier to replace all current data. A copy of the current data is kept automatically before restoring.")}</div></div>
          <button className="btn" onClick={() => fileRef.current?.click()}><Upload size={15} /> {t("Choose backup…")}</button>
          <input ref={fileRef} type="file" accept=".db,application/octet-stream" hidden aria-label={t("Choose a backup file")} onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; void restore(f); }} />
        </div>
        {msg && <div className={"fb-note " + (msg.ok ? "ok-text" : "err")}>{msg.ok ? "✓" : "✗"} {msg.text}</div>}
      </div>

      <h3 className="sec">{t("Cleanup")}</h3>
      <div className="card">
        <div className="setting-row">
          <div><div className="sr-title">{t("Clear all chat history")}</div><div className="sr-desc">{t("Group chats and members are kept; only messages are deleted. This cannot be undone.")}</div></div>
          <button
            className="btn danger-outline"
            onClick={async () => {
              if (!(await confirm(t("Delete every message in all group chats? This cannot be undone."), { okText: t("Clear all") }))) return;
              const r = await api.clearAllMessages();
              await reloadGroups();
              setMsg({ ok: true, text: t("Deleted {n} messages", { n: r.deleted }) });
            }}
          >
            <Trash2 size={15} /> {t("Clear all")}
          </button>
        </div>
      </div>
    </div>
  );
}
