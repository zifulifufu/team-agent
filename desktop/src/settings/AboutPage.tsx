import { useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import { api, type SystemInfo } from "../api";
import { APP_VERSION } from "../lib";

export default function AboutPage() {
  const [sys, setSys] = useState<SystemInfo | null>(null);
  useEffect(() => { api.system().then(setSys).catch(() => undefined); }, []);
  return (
    <div className="sp">
      <div className="about-hero">
        <div className="hero-logo"><Sparkles size={24} /></div>
        <div>
          <h2 className="sp-title" style={{ margin: 0 }}>Team Agent</h2>
          <div className="muted">版本 {sys?.app_version ?? APP_VERSION}</div>
        </div>
      </div>
      <p className="sp-desc">多个国内外大模型在同一个群聊里分工协作。路由层默认优先 DeepSeek,失败或外呼被禁用时自动回退到本地模型。</p>
      <div className="card flush">
        <div className="setting-row pad"><div className="sr-title">模型网关</div><span className="muted">LiteLLM {sys?.litellm ?? "…"}</span></div>
        <div className="setting-row pad"><div className="sr-title">系统</div><span className="muted">{sys?.platform ?? "…"}</span></div>
        <div className="setting-row pad"><div className="sr-title">本机访问保护</div><span className="muted">{sys ? (sys.auth ? "已启用令牌校验" : "开发模式(仅限 localhost 来源)") : "…"}</span></div>
      </div>
    </div>
  );
}
