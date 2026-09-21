import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, type LocalStatus, type SystemInfo } from "../api";
import { useI18n } from "../i18n";

export default function DepsPage() {
  const { t } = useI18n();
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
    { name: "Python", desc: t("Runs the backend service"), ok: sys ? true : null, ver: sys?.python ?? "…" },
    { name: "LiteLLM", desc: t("The gateway that reaches every vendor's models"), ok: sys ? !!sys.litellm : null, ver: sys?.litellm ?? t("not installed") },
    { name: "FastAPI", desc: t("Local REST / WebSocket API"), ok: sys ? !!sys.fastapi : null, ver: sys?.fastapi ?? t("not installed") },
    { name: "Ollama", desc: t("The local model runtime (the offline fallback)"), ok: local ? local.running : null, ver: local ? (local.running ? t("running · {n} models", { n: local.installed.length }) : t("not detected")) : "…" },
    { name: electron ? "Electron" : t("Browser"), desc: t("Desktop shell / rendering engine"), ok: true, ver: electron ? `${electron}(Chromium ${chrome ?? "?"})` : chrome ? `Chromium ${chrome}` : t("unknown") },
  ];

  return (
    <div className="sp">
      <h2 className="sp-title">{t("Dependencies")} <button className="btn small" style={{ marginLeft: 12 }} onClick={load}><RefreshCw size={13} /> {t("Refresh")}</button></h2>
      <p className="sp-desc">{t("The state of the parts Team Agent needs to run.")}</p>
      {err && <div className="err">{t("Could not read it:")} {err}</div>}
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
