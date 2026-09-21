import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Plus, RefreshCw, X } from "lucide-react";
import { api, modelLabel, type RoutePreview } from "../api";
import { useData } from "../data";
import { Switch } from "../ui";
import { useSettingsSaver } from "./rows";
import { HealthDot, ModelSelect } from "../components/Health";

export default function RoutingPage() {
  const { settings, models, providers, health, checkHealth } = useData();
  const { set, err: saveErr, saving } = useSettingsSaver();
  const [preview, setPreview] = useState<RoutePreview | null>(null);
  const [add, setAdd] = useState("");
  const [checking, setChecking] = useState(false);
  const [checkErr, setCheckErr] = useState("");
  const runCheck = async () => {
    setChecking(true);
    setCheckErr("");
    try {
      await checkHealth();
      setPreview(await api.routePreview());
    } catch (e) {
      setCheckErr((e as Error).message);
    } finally {
      setChecking(false);
    }
  };

  useEffect(() => {
    api.routePreview().then(setPreview).catch(() => undefined);
  }, [settings, models, providers]);

  if (!settings) return <div className="empty big">加载中…</div>;
  const chain = settings.route_chain;
  const move = (i: number, d: number) => {
    const j = i + d;
    if (saving || j < 0 || j >= chain.length) return;
    const c = [...chain];
    [c[i], c[j]] = [c[j], c[i]];
    void set({ route_chain: c });
  };

  return (
    <div className="sp">
      <h2 className="sp-title">路由与回退</h2>
      <p className="sp-desc">每次成员发言都会按下面的顺序尝试模型:失败(鉴权 / 网络 / 超时 / 限流 / 空回复)就自动换下一个;链尾始终保证有一个本地模型兜底。成员如果单独指定了模型,会排在链的最前面;没有固定模型的成员会先按岗位强项挑模型,再按这里的回退链兜底。</p>

      <div className="card">
        <div className="setting-row">
          <div>
            <div className="sr-title">允许调用云端模型(外呼)</div>
            <div className="sr-desc">关闭后只使用本地模型,不会向任何云端 API 发送内容 —— 适合离线或涉密场景。</div>
          </div>
          <Switch checked={settings.external_calls_enabled} label="允许外呼" onChange={(v) => set({ external_calls_enabled: v })} />
        </div>
      </div>

      <div className="sec-row">
        <h3 className="sec">优先级链</h3>
        <button className="btn small" disabled={checking} onClick={() => void runCheck()} title="向每个可用的云端模型发一条极短的请求(花几个 token),本地模型只探测服务">
          <RefreshCw size={12} className={checking ? "mp-spin" : ""} /> {checking ? "检测中…" : "检测连通性"}
        </button>
      </div>
      {(checkErr || saveErr) && <div className="err small" role="alert">{checkErr || `保存失败:${saveErr}`}</div>}
      <div className="card flush">
        {chain.map((id, i) => {
          const m = models.find((x) => x.id === id);
          return (
            <div key={id} className="model-row">
              <span className="rank">{i + 1}</span>
              <HealthDot h={health[id]} label />
              <div className="mr-main">
                <div className="mr-name">{modelLabel(id, models)} {m?.is_local && <span className="tag">本地</span>}</div>
                <div className="mr-id">{id}{!m && " · 模型已被删除"}</div>
              </div>
              <button className="icon-btn" aria-label="上移" disabled={i === 0 || saving} onClick={() => move(i, -1)}><ArrowUp size={15} /></button>
              <button className="icon-btn" aria-label="下移" disabled={i === chain.length - 1 || saving} onClick={() => move(i, 1)}><ArrowDown size={15} /></button>
              <button className="icon-btn" aria-label="移出链" disabled={saving} onClick={() => set({ route_chain: chain.filter((x) => x !== id) })}><X size={15} /></button>
            </div>
          );
        })}
        {chain.length === 0 && <div className="empty">链是空的,系统会自动补上本地模型兜底</div>}
      </div>
      <div className="input-group" style={{ marginTop: 10 }}>
        <ModelSelect className="grow" value={add || null} autoLabel="选择要加入链的模型…" exclude={chain} ariaLabel="加入链的模型" onChange={(id) => setAdd(id ?? "")} />
        <button className="btn" disabled={!add || saving} onClick={async () => { if (await set({ route_chain: [...chain, add] })) setAdd(""); }}><Plus size={15} /> 加入</button>
      </div>

      <h3 className="sec">当前生效的路由</h3>
      {preview && (
        <div className="card">
          {preview.chain.length === 0 ? (
            <span className="err">没有任何可用模型!请启动本地模型或配置 API 密钥。</span>
          ) : (
            <div className="route-flow">
              {preview.chain.map((c, i) => (
                <span key={c} className="rf-item">
                  <span className="rf-chip"><HealthDot h={health[c]} /> {modelLabel(c, models)}</span>
                  {i < preview.chain.length - 1 && <span className="rf-arrow">→</span>}
                </span>
              ))}
            </div>
          )}
          {preview.skipped.length > 0 && (
            <div className="muted small" style={{ marginTop: 10 }}>
              已跳过:{preview.skipped.map((s) => `${modelLabel(s.model_id, models)}(${s.detail})`).join(";")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
