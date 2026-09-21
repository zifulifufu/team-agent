import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, type LocalStatus, type SystemInfo } from "../api";

export default function DepsPage() {
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [local, setLocal] = useState<LocalStatus | null>(null);
  const [err, setErr] = useState("");
  const load = () => {
    setErr("");
    api.system().then(setSys).catch((e) => setErr((e as Error).message));
    api.localStatus().then(setLocal).catch(() => undefined);
  };
  useEffect(load, []);

  const ua = navigator.userAgent;
  const electron = /Electron\/([\d.]+)/.exec(ua)?.[1];
  const chrome = /Chrome\/([\d.]+)/.exec(ua)?.[1];

  const rows: { name: string; desc: string; ok: boolean | null; ver: string }[] = [
    { name: "Python", desc: "运行后端服务", ok: sys ? true : null, ver: sys?.python ?? "…" },
    { name: "LiteLLM", desc: "统一调用各家大模型的网关", ok: sys ? !!sys.litellm : null, ver: sys?.litellm ?? "未安装" },
    { name: "FastAPI", desc: "本机 REST / WebSocket 接口", ok: sys ? !!sys.fastapi : null, ver: sys?.fastapi ?? "未安装" },
    { name: "Ollama", desc: "本地模型运行时(离线兜底)", ok: local ? local.running : null, ver: local ? (local.running ? `运行中 · ${local.installed.length} 个模型` : "未检测到") : "…" },
    { name: electron ? "Electron" : "浏览器", desc: "桌面外壳 / 渲染引擎", ok: true, ver: electron ? `${electron}(Chromium ${chrome ?? "?"})` : chrome ? `Chromium ${chrome}` : "未知" },
  ];

  return (
    <div className="sp">
      <h2 className="sp-title">依赖 <button className="btn small" style={{ marginLeft: 12 }} onClick={load}><RefreshCw size={13} /> 刷新</button></h2>
      <p className="sp-desc">Team Agent 运行所依赖的组件状态。</p>
      {err && <div className="err">读取失败:{err}</div>}
      <div className="card flush">
        {rows.map((r) => (
          <div key={r.name} className="setting-row pad">
            <div><div className="sr-title">{r.name}</div><div className="sr-desc">{r.desc}</div></div>
            <span className="dep-ver"><i className={"dot " + (r.ok === null ? "off" : r.ok ? "ok" : "bad")} /> {r.ver}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
