import { useCallback, useEffect, useState } from "react";
import { FolderOpen, LoaderCircle, RefreshCw } from "lucide-react";
import { api, relTime, type ObsidianReport, type ObsidianStatus } from "../api";
import { Switch, useConfirm } from "../ui";

function summary(r: ObsidianReport): string {
  if (!r.ok) return `失败:${r.error}`;
  const parts = [
    r.written && `写出 ${r.written}`, r.pulled && `读回修改 ${r.pulled}`, r.imported && `导入新笔记 ${r.imported}`,
    r.deleted_memories && `删除记忆 ${r.deleted_memories}`, r.removed_files && `文件移入 _已删除 ${r.removed_files}`,
    r.conflicts && `冲突 ${r.conflicts}(较新的一方生效,旧版在 _冲突备份)`,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "已是最新,没有需要同步的内容";
}

/** 记忆页里的「同步到 Obsidian」卡片:选库里的一个文件夹,双向同步。 */
export default function ObsidianCard({ onSynced }: { onSynced: () => void }) {
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
      await load();   // 不管成败都读一次现状:比如「文件夹已保存、但第一次同步失败」,界面要显示已启用
      setBusy(false);
    }
  };
  const sync = (force = false) => run(async () => { await api.obsidianSync(force); onSynced(); });
  const enable = () => run(async () => { await api.setObsidian({ dir: dir.trim() }); setDir(""); await api.obsidianSync(); onSynced(); });
  const browse = async () => { const p = await pick?.(); if (p) setDir(p); };

  if (!st) return err ? <div className="err kn-block">{err}</div> : null;
  const last = st.last;
  const massWarn = last?.warnings.some((w) => w.includes("强制同步"));

  return (
    <div className="card kn-obs">
      <div className="sr-title">同步到 Obsidian</div>
      <div className="sr-desc">
        把记忆镜像到你 Obsidian 库里的一个文件夹:每条记忆是一个 .md 笔记(开头的属性记着归属和类型)。在 Obsidian 里改了、新建了、删了,同步后程序里也会跟着变;程序里的改动也会写回去。
        只同步偏好 / 事实 / 决定 / 教训,「做法」这类程序自己记的流水账不同步。请选一个<b>专用文件夹</b>——里面所有 .md 笔记都会被当作记忆。
      </div>
      {err && <div className="err small" role="alert">{err}</div>}

      {!st.dir ? (
        <div className="kn-obs-setup">
          {(st.vaults?.length ?? 0) > 0 && (
            <div className="kn-obs-vaults">
              <span className="muted small">本机的 Obsidian 库:</span>
              {st.vaults!.map((v) => (
                <button key={v.path} className="btn small" onClick={() => setDir(`${v.path}/Team Agent 记忆`)} title={v.path}>{v.name}</button>
              ))}
            </div>
          )}
          <div className="input-group">
            <input value={dir} onChange={(e) => setDir(e.target.value)} placeholder="文件夹完整路径,如 /Users/你/Documents/MyVault/Team Agent 记忆" aria-label="Obsidian 文件夹路径" spellCheck={false} />
            {pick && <button className="btn" onClick={() => void browse()}><FolderOpen size={14} /> 选择…</button>}
            <button className="btn primary" disabled={busy || !dir.trim()} onClick={() => void enable()}>{busy ? "同步中…" : "启用并同步"}</button>
          </div>
          <div className="muted small">文件夹不存在会自动创建。选中后会立即做第一次同步。</div>
        </div>
      ) : (
        <div className="kn-obs-on">
          <div className="kn-obs-path"><FolderOpen size={14} aria-hidden /> <code>{st.dir}</code></div>
          <div className="muted small">
            {st.exists ? `文件夹里有 ${st.notes} 篇笔记,已对应 ${st.mapped} 条记忆` : "文件夹现在不存在或不可用(外接盘没挂载?)——同步不会改动任何东西"}
            {st.exists && !st.in_vault && " · 这个位置不在任何 Obsidian 库里(没找到 .obsidian),笔记仍会写出,但 Obsidian 打开不了它,除非把它放进库里"}
          </div>
          {last && (
            <div className={"kn-obs-last" + (last.ok ? "" : " bad")}>
              上次同步 {relTime(last.at)}:{summary(last)}
              {last.warnings.length > 0 && (
                <ul className="kn-obs-warn">{last.warnings.slice(0, 6).map((w, i) => <li key={i}>{w}</li>)}</ul>
              )}
            </div>
          )}
          <div className="kn-obs-acts">
            <button className="btn small primary" disabled={busy} onClick={() => void sync()}>
              {busy ? <LoaderCircle size={13} className="kn-spin" /> : <RefreshCw size={13} />} 立即同步
            </button>
            {massWarn && (
              <button className="btn small" disabled={busy} onClick={async () => { if (await confirm("确认要按 Obsidian 文件夹现在的样子删除这些记忆吗?", { okText: "强制同步" })) void sync(true); }}>强制同步</button>
            )}
            <label className="check-inline"><Switch checked={st.auto} label="自动同步" onChange={(v) => void run(() => api.setObsidian({ auto: v }))} /> 自动同步(每 30 秒)</label>
            <span className="grow" />
            <button className="btn small ghost" disabled={busy} onClick={async () => { if (await confirm("停用同步?已写出的笔记和程序里的记忆都保留,只是不再互相同步。", { okText: "停用" })) void run(() => api.setObsidian({ dir: "", auto: false })); }}>停用</button>
          </div>
        </div>
      )}
    </div>
  );
}
