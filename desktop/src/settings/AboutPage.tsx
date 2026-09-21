import { useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import { api, type SystemInfo } from "../api";
import { useI18n } from "../i18n";
import { APP_VERSION } from "../lib";

export default function AboutPage() {
  const { t } = useI18n();
  const [sys, setSys] = useState<SystemInfo | null>(null);
  useEffect(() => { api.system().then(setSys).catch(() => undefined); }, []);
  return (
    <div className="sp">
      <div className="about-hero">
        <div className="hero-logo"><Sparkles size={24} /></div>
        <div>
          <h2 className="sp-title" style={{ margin: 0 }}>Team Agent</h2>
          <div className="muted">{t("Version {v}", { v: sys?.app_version ?? APP_VERSION })}</div>
        </div>
      </div>
      <p className="sp-desc">{t("Models from several vendors work side by side in one group chat. The router prefers DeepSeek by default and falls back to a local model when that fails or outbound calls are off.")}</p>
      <div className="card flush">
        <div className="setting-row pad"><div className="sr-title">{t("Model gateway")}</div><span className="muted">LiteLLM {sys?.litellm ?? "…"}</span></div>
        <div className="setting-row pad"><div className="sr-title">{t("System")}</div><span className="muted">{sys?.platform ?? "…"}</span></div>
        <div className="setting-row pad"><div className="sr-title">{t("Local access protection")}</div><span className="muted">{sys ? (sys.auth ? t("Token checks are on") : t("Development mode (localhost origins only)")) : "…"}</span></div>
      </div>
    </div>
  );
}
