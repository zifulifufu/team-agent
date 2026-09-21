import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Plus, RefreshCw, X } from "lucide-react";
import { api, modelLabel, type RoutePreview } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Switch } from "../ui";
import { useSettingsSaver } from "./rows";
import { HealthDot, ModelSelect } from "../components/Health";

export default function RoutingPage() {
  const { t } = useI18n();
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

  if (!settings) return <div className="empty big">{t("Loading…")}</div>;
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
      <h2 className="sp-title">{t("Routing & fallback")}</h2>
      <p className="sp-desc">{t("Every member reply tries the models below in order: on failure (auth / network / timeout / rate limit / empty reply) it moves on to the next one, and the end of the chain always keeps a local model as a fallback. A member with its own model goes to the front of the chain; a member without a fixed model picks one by its role strengths first, then falls back to the chain below.")}</p>

      <div className="card">
        <div className="setting-row">
          <div>
            <div className="sr-title">{t("Allow cloud models (external calls)")}</div>
            <div className="sr-desc">{t("When off, only local models are used and nothing is sent to any cloud API — suited to offline or confidential settings.")}</div>
          </div>
          <Switch checked={settings.external_calls_enabled} label={t("Allow external calls")} onChange={(v) => set({ external_calls_enabled: v })} />
        </div>
      </div>

      <div className="sec-row">
        <h3 className="sec">{t("Priority chain")}</h3>
        <button className="btn small" disabled={checking} onClick={() => void runCheck()} title={t("Send a very short request to every available cloud model (costs a few tokens); local models are only probed for the service.")}>
          <RefreshCw size={12} className={checking ? "mp-spin" : ""} /> {checking ? t("Checking…") : t("Check connectivity")}
        </button>
      </div>
      {(checkErr || saveErr) && <div className="err small" role="alert">{checkErr || t("Failed to save: {error}", { error: saveErr })}</div>}
      <div className="card flush">
        {chain.map((id, i) => {
          const m = models.find((x) => x.id === id);
          return (
            <div key={id} className="model-row">
              <span className="rank">{i + 1}</span>
              <HealthDot h={health[id]} label />
              <div className="mr-main">
                <div className="mr-name">{modelLabel(id, models)} {m?.is_local && <span className="tag">{t("Local")}</span>}</div>
                <div className="mr-id">{id}{!m && " " + t("· Model was deleted")}</div>
              </div>
              <button className="icon-btn" aria-label={t("Move up")} disabled={i === 0 || saving} onClick={() => move(i, -1)}><ArrowUp size={15} /></button>
              <button className="icon-btn" aria-label={t("Move down")} disabled={i === chain.length - 1 || saving} onClick={() => move(i, 1)}><ArrowDown size={15} /></button>
              <button className="icon-btn" aria-label={t("Remove from chain")} disabled={saving} onClick={() => set({ route_chain: chain.filter((x) => x !== id) })}><X size={15} /></button>
            </div>
          );
        })}
        {chain.length === 0 && <div className="empty">{t("The chain is empty; a local fallback model is added automatically")}</div>}
      </div>
      <div className="input-group" style={{ marginTop: 10 }}>
        <ModelSelect className="grow" value={add || null} autoLabel={t("Choose a model to add to the chain…")} exclude={chain} ariaLabel={t("Model to add to the chain")} onChange={(id) => setAdd(id ?? "")} />
        <button className="btn" disabled={!add || saving} onClick={async () => { if (await set({ route_chain: [...chain, add] })) setAdd(""); }}><Plus size={15} /> {t("Add to chain")}</button>
      </div>

      <h3 className="sec">{t("Active routing right now")}</h3>
      {preview && (
        <div className="card">
          {preview.chain.length === 0 ? (
            <span className="err">{t("No model is available! Start a local model or configure an API key.")}</span>
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
              {t("Skipped:")}{preview.skipped.map((s) => `${modelLabel(s.model_id, models)} (${s.detail})`).join(";")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
