import { useEffect, useRef, useState } from "react";
import { Download, Trash2, Upload } from "lucide-react";
import { api, downloadBackup, type SystemInfo } from "../api";
import { useData } from "../data";
import { Switch, useConfirm } from "../ui";
import { fmtBytes } from "../components/ExtBits";

export default function DataPage() {
  const { refreshAll, reloadGroups } = useData();
  const fileRef = useRef<HTMLInputElement>(null);
  const confirm = useConfirm();
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [keys, setKeys] = useState(false);
  const [msg, setMsg] = useState("");
  useEffect(() => { api.system().then(setSys).catch(() => undefined); }, []);

  const exportDb = async () => {
    if (keys && !(await confirm("备份文件将包含明文 API 密钥,请妥善保管、不要转发给他人。继续导出?", { okText: "仍然导出", danger: false }))) return;
    try {
      await downloadBackup(keys);
      setMsg("✓ 已导出备份");
    } catch (e) {
      setMsg("✗ " + (e as Error).message);
    }
  };

  const restore = async (f: File | undefined) => {
    if (!f) return;
    if (!(await confirm(`用「${f.name}」替换当前的全部数据(群聊、成员、模型配置、记忆、资料库、聊天记录)?会先自动留一份当前数据的副本,以便找回。如果备份不含密钥,你现在填好的 API 密钥会保留。`, { okText: "恢复", danger: false }))) return;
    try {
      const r = await api.restoreBackup(f);
      await refreshAll();
      setMsg(`✓ 已恢复:${r.groups} 个群、${r.agents} 个成员、${r.memories} 条记忆、${r.docs} 篇资料。恢复前的数据留了一份副本:${r.safety_copy}`);
    } catch (e) {
      setMsg("✗ " + (e as Error).message);
    }
  };

  return (
    <div className="sp">
      <h2 className="sp-title">数据</h2>
      <p className="sp-desc">所有配置和聊天记录都保存在本机的 SQLite 数据库里,不会上传。</p>

      <div className="card flush">
        <div className="setting-row pad">
          <div><div className="sr-title">数据目录</div><div className="sr-desc mono">{sys?.data_dir ?? "…"}</div></div>
        </div>
        <div className="setting-row pad">
          <div><div className="sr-title">数据库大小</div></div>
          <span className="muted">{sys ? fmtBytes(sys.db_bytes) : "…"}</span>
        </div>
      </div>

      <h3 className="sec">备份</h3>
      <div className="card">
        <div className="setting-row">
          <div>
            <div className="sr-title">导出数据库快照</div>
            <div className="sr-desc">包含群聊、成员、模型配置与聊天记录。默认不含 API 密钥,恢复后需要重新填写。</div>
          </div>
          <button className="btn" onClick={exportDb}><Download size={15} /> 导出</button>
        </div>
        <div className="setting-row" style={{ marginTop: 12 }}>
          <div><div className="sr-title">同时导出 API 密钥</div><div className="sr-desc">仅在迁移到自己的另一台电脑时开启。</div></div>
          <Switch checked={keys} onChange={setKeys} label="同时导出 API 密钥" />
        </div>
        <div className="setting-row" style={{ marginTop: 12 }}>
          <div><div className="sr-title">从备份恢复</div><div className="sr-desc">选择之前导出的 .db 备份文件,替换当前全部数据。恢复前会自动留一份当前数据的副本。</div></div>
          <button className="btn" onClick={() => fileRef.current?.click()}><Upload size={15} /> 选择备份…</button>
          <input ref={fileRef} type="file" accept=".db,application/octet-stream" hidden aria-label="选择备份文件" onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; void restore(f); }} />
        </div>
        {msg && <div className={"fb-note " + (msg.startsWith("✓") ? "ok-text" : "err")}>{msg}</div>}
      </div>

      <h3 className="sec">清理</h3>
      <div className="card">
        <div className="setting-row">
          <div><div className="sr-title">清空所有聊天记录</div><div className="sr-desc">群聊和成员会保留,只删除消息。此操作不可恢复。</div></div>
          <button
            className="btn danger-outline"
            onClick={async () => {
              if (!(await confirm("清空所有群聊里的全部消息?此操作不可恢复。", { okText: "清空" }))) return;
              const r = await api.clearAllMessages();
              await reloadGroups();
              setMsg(`✓ 已删除 ${r.deleted} 条消息`);
            }}
          >
            <Trash2 size={15} /> 清空
          </button>
        </div>
      </div>
    </div>
  );
}
