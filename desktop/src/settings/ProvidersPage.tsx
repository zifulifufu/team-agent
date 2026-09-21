import { useEffect, useMemo, useRef, useState } from "react";
import { Eye, EyeOff, Plus, Search, Tags, Trash2, X } from "lucide-react";
import { api, type Model, type Preset, type Provider, type Tag } from "../api";
import { useData } from "../data";
import { LetterIcon, Modal, Switch, useBusy, useConfirm, useFlash } from "../ui";
import { ModelPicker, pendingNew } from "../components/ModelPicker";
import { StrengthChips, StrengthPicker } from "../components/Strengths";
import { HealthDot } from "../components/Health";
import "../styles/models.css";

const KIND_LABEL: Record<string, string> = {
  deepseek: "DeepSeek",
  openai_compatible: "OpenAI 兼容",
  anthropic: "Anthropic",
  gemini: "Gemini",
  ollama: "Ollama",
};

const DEFAULT_BASE: Record<string, string> = {
  deepseek: "https://api.deepseek.com",
  anthropic: "https://api.anthropic.com",
  gemini: "https://generativelanguage.googleapis.com",
};

/** 预览实际请求地址(和 Cherry Studio 一样,让用户确认地址填对了)。 */
function endpointPreview(kind: string, base: string): string {
  const b = (base || DEFAULT_BASE[kind] || "").replace(/\/+$/, "");
  if (!b) return "";
  if (kind === "openai_compatible" || kind === "deepseek") return `${b}/chat/completions`;
  if (kind === "anthropic") return `${b}/v1/messages`;
  if (kind === "ollama") return `${b}/api/chat`;
  return b;
}

export default function ProvidersPage() {
  const { providers, reload } = useData();
  const [sel, setSel] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [adding, setAdding] = useState(false);
  const [newCounts, setNewCounts] = useState<Record<string, number>>({});
  /** 点了列表里的「N 个新模型」:选中该服务商并直接打开选择对话框(只看新模型) */
  const [openNew, setOpenNew] = useState<{ pid: string; n: number } | null>(null);
  const cur = providers.find((p) => p.id === sel) ?? providers[0] ?? null;
  const list = useMemo(() => providers.filter((p) => p.name.toLowerCase().includes(q.trim().toLowerCase())), [providers, q]);

  // 「N 个新模型」:纯本地计算(不联网),只查启用的服务商
  useEffect(() => {
    let alive = true;
    const on = providers.filter((p) => p.enabled);
    Promise.all(on.map((p) => api.modelOptions(p.id).then((o) => [p.id, pendingNew(o)] as const).catch(() => [p.id, 0] as const))).then((r) => {
      if (alive) setNewCounts(Object.fromEntries(r));
    });
    return () => { alive = false; };
  }, [providers]);

  return (
    <div className="providers">
      <div className="prov-list">
        <div className="search-box">
          <Search size={14} />
          <input placeholder="搜索模型平台…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="搜索模型平台" />
        </div>
        <div className="prov-items">
          {list.map((p) => (
            <button key={p.id} className={"list-item" + (cur?.id === p.id ? " on" : "")} onClick={() => setSel(p.id)}>
              <LetterIcon name={p.name} />
              <span className="li-main">
                <span className="li-name">{p.name}</span>
                {p.enabled && (newCounts[p.id] ?? 0) > 0 && (
                  <span className="li-sub">
                    <span
                      className="tag new mp-new-btn"
                      title="点击查看新模型"
                      onClick={(e) => { e.stopPropagation(); setSel(p.id); setOpenNew({ pid: p.id, n: Date.now() }); }}
                    >
                      {newCounts[p.id]} 个新模型
                    </span>
                  </span>
                )}
              </span>
              {p.is_local && <span className="tag">本地</span>}
              {p.enabled && <span className="tag on">ON</span>}
            </button>
          ))}
          {list.length === 0 && <div className="side-empty">没有匹配的服务商</div>}
        </div>
        <button className="btn add-prov" onClick={() => setAdding(true)}><Plus size={15} /> 添加服务商</button>
      </div>
      <div className="prov-detail">
        {cur ? (
          <ProviderDetail
            key={cur.id}
            p={cur}
            newCount={newCounts[cur.id] ?? 0}
            onNewCount={(n) => setNewCounts((c) => ({ ...c, [cur.id]: n }))}
            openNewReq={openNew?.pid === cur.id ? openNew.n : 0}
            onOpenNewHandled={() => setOpenNew(null)}
            onChanged={reload}
            onDeleted={() => { setSel(null); void reload(); }} />
        ) : (
          <div className="empty big">点左下角「添加服务商」开始</div>
        )}
      </div>
      {adding && <AddProvider onClose={() => setAdding(false)} onAdded={async (id) => { setAdding(false); await reload(); setSel(id); }} />}
    </div>
  );
}

function ProviderDetail({
  p,
  newCount,
  onNewCount,
  openNewReq,
  onOpenNewHandled,
  onChanged,
  onDeleted,
}: {
  p: Provider;
  newCount: number;
  onNewCount: (n: number) => void;
  openNewReq: number;
  onOpenNewHandled: () => void;
  onChanged: () => Promise<void>;
  onDeleted: () => void;
}) {
  const confirm = useConfirm();
  const { health } = useData();
  const [key, setKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [base, setBase] = useState(p.base_url);
  const [picker, setPicker] = useState<{ onlyNew: boolean } | null>(null);
  const [editStr, setEditStr] = useState<Model | null>(null);
  const [tests, setTests] = useState<Record<string, string>>({});
  const [check, setCheck] = useState("");
  const [err, setErr] = useState("");
  const [saved, flash] = useFlash();
  const keyRef = useRef(key);
  keyRef.current = key;
  useEffect(() => {
    if (!openNewReq) return;
    setPicker({ onlyNew: true });
    onOpenNewHandled();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openNewReq]);

  const save = async () => {
    const body: Record<string, unknown> = {};
    if (base !== p.base_url) body.base_url = base;
    if (keyRef.current) body.api_key = keyRef.current;
    if (!Object.keys(body).length) return true;
    setErr("");
    try {
      await api.patchProvider(p.id, body);
      setKey("");
      flash();
      await onChanged();
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    }
  };

  // 同一个服务商的测试一次只发一个:有的账号每分钟只允许 3 次请求,并发点几下就会被限速
  const [busy, setBusy] = useState(false);
  const test = async (id: string) => {
    if (busy) return;
    setBusy(true);
    setTests((t) => ({ ...t, [id]: "测试中…" }));
    try {
      const r = await api.testModel(id);
      await onChanged(); // 测试结果会记进指示灯
      setTests((t) => ({ ...t, [id]: r.ok ? `✓ ${r.reply?.startsWith("(") ? r.reply : `${r.latency_ms}ms`}` : `✗ ${r.error}` }));
    } catch (e) {
      setTests((t) => ({ ...t, [id]: `✗ ${(e as Error).message}` }));
    } finally {
      setBusy(false);
    }
  };
  const checkKey = async () => {
    if (busy) return;
    const m = p.models.find((x) => x.enabled) ?? p.models[0];
    if (!m) return setCheck("✗ 请先添加一个模型再检测");
    setBusy(true);
    try {
      setCheck("检测中…");
      if (!(await save())) return setCheck("");
      const r = await api.testModel(m.id);
      await onChanged();
      setCheck(r.ok ? `✓ 连接正常 · ${m.display_name} · ${r.reply?.startsWith("(") ? r.reply : `${r.latency_ms}ms`}` : `✗ ${r.error}`);
    } catch (e) {
      setCheck(`✗ ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  const preview = endpointPreview(p.kind, base);
  const keyOptional = p.is_local || p.kind === "ollama";

  return (
    <div className="prov-inner">
      <div className="detail-head">
        <h2>
          {p.name}
          {p.enabled && newCount > 0 && (
            <span className="mp-head-tags">
              <button className="tag new mp-new-btn" title="打开选择模型对话框,只看新模型" onClick={() => setPicker({ onlyNew: true })}>{newCount} 个新模型</button>
            </span>
          )}
        </h2>
        <div className="row">
          <Switch checked={p.enabled} label={`启用 ${p.name}`} onChange={async (v) => { await api.patchProvider(p.id, { enabled: v }); await onChanged(); }} />
        </div>
      </div>

      {!p.is_local && (
        <div className="field-block">
          <div className="fb-label">API 密钥 {p.has_key && <span className="tag on">已设置 {p.key_hint}</span>}</div>
          <div className="input-group">
            <input
              type={showKey ? "text" : "password"}
              value={key}
              onChange={(e) => { setKey(e.target.value); setCheck(""); }}
              onBlur={() => void save()}
              placeholder={p.has_key ? "已保存,输入新的密钥可覆盖" : "sk-…"}
              autoComplete="off"
              spellCheck={false}
              aria-label="API 密钥"
            />
            <button className="icon-btn" aria-label={showKey ? "隐藏密钥" : "显示密钥"} onClick={() => setShowKey((s) => !s)}>{showKey ? <EyeOff size={15} /> : <Eye size={15} />}</button>
            <button className="btn" disabled={busy} onClick={checkKey}>{busy ? "检测中…" : "检测"}</button>
          </div>
          {check && <div className={"fb-note " + (check.startsWith("✓") ? "ok-text" : check.startsWith("✗") ? "err" : "muted")}>{check}</div>}
          {!p.has_key && !key && <div className="fb-note muted">密钥仅保存在本机数据目录,只会发送给这个服务商。</div>}
        </div>
      )}

      <div className="field-block">
        <div className="fb-label">API 地址{DEFAULT_BASE[p.kind] ? "(留空使用官方默认)" : ""}</div>
        <div className="input-group">
          <input value={base} onChange={(e) => setBase(e.target.value)} onBlur={() => void save()} placeholder={DEFAULT_BASE[p.kind] ?? "https://…/v1"} aria-label="API 地址" spellCheck={false} />
        </div>
        {preview && <div className="fb-note muted">预览:{preview}</div>}
        {saved && <div className="fb-note ok-text">已保存</div>}
      </div>

      <div className="fb-label models-head">
        <span>模型 <span className="chip">{p.models.length}</span></span>
        <span className="row">
          <button className="btn small primary" onClick={() => setPicker({ onlyNew: false })}><Plus size={14} /> 添加模型</button>
        </span>
      </div>
      {err && <div className="err" style={{ marginBottom: 8 }}>{err}</div>}
      <div className="model-list">
        {p.models.map((m) => (
          <div key={m.id} className="model-row mp-mrow">
            <div className="mr-main">
              <div className="mr-name">
                <HealthDot h={health[m.id]} label />&nbsp;{m.display_name}
                {m.strengths_custom && <span className="tag" title="强项已手动修改;可在「强项」里恢复自动">自定义</span>}
                {m.retired_reason && <span className="tag mp-warn-tag">已停用</span>}
              </div>
              <div className="mr-id">{m.id}</div>
              {m.summary && <div className="mp-summary">{m.summary}</div>}
              {m.retired_reason && <div className="mp-warn">{m.retired_reason}</div>}
              {tests[m.id]?.startsWith("✗") && <div className="err small mp-testerr" role="alert">{tests[m.id]}</div>}
              {m.strengths.length > 0 && <div><StrengthChips tags={m.strengths} max={8} /></div>}
            </div>
            <div className="mr-actions">
              {!tests[m.id]?.startsWith("✗") && <div className={"test-res" + (tests[m.id]?.startsWith("✓") ? " ok-text" : " muted")} title={tests[m.id]}>{tests[m.id]}</div>}
              <button className="btn small" disabled={busy} onClick={() => test(m.id)}>测试</button>
              <button className="btn small" title="编辑这个模型的强项标签" aria-label={`编辑 ${m.display_name} 的强项`} onClick={() => setEditStr(m)}><Tags size={13} /> 强项</button>
              <Switch checked={m.enabled} label={`启用 ${m.display_name}`} onChange={async (v) => { await api.patchModel(m.id, { enabled: v }); await onChanged(); }} />
              <button className="icon-btn" title="移除模型" aria-label={`移除 ${m.display_name}`} onClick={async () => {
                if (!(await confirm(`移除模型「${m.display_name}」?如果它已被拉进群当成员,这些成员也会一并移除。`, { okText: "移除" }))) return;
                await api.delModel(m.id);
                await onChanged();
              }}><X size={15} /></button>
            </div>
          </div>
        ))}
        {p.models.length === 0 && <div className="empty">还没有模型 —— 点「添加模型」,从该服务商的全部型号里勾选</div>}
      </div>

      <div className="danger-zone">
        <button
          className="btn ghost small danger-text"
          onClick={async () => {
            if (await confirm(`删除服务商「${p.name}」及其所有模型?由这些模型拉进群的成员也会一并移除。`, { okText: "删除" })) {
              await api.delProvider(p.id);
              onDeleted();
            }
          }}
        >
          <Trash2 size={14} /> 删除该服务商
        </button>
        {keyOptional && <span className="muted small">本地服务不需要 API 密钥</span>}
      </div>

      {picker && (
        <ModelPicker
          provider={p}
          focusNew={picker.onlyNew}
          onClose={() => setPicker(null)}
          onOptions={(o) => onNewCount(pendingNew(o))}
        />
      )}
      {editStr && (
        <StrengthEditor
          model={editStr}
          onClose={() => setEditStr(null)}
          onSaved={async () => { setEditStr(null); await onChanged(); }}
        />
      )}
    </div>
  );
}

function StrengthEditor({ model, onClose, onSaved }: { model: Model; onClose: () => void; onSaved: () => Promise<void> }) {
  const [val, setVal] = useState<Tag[]>(model.strengths);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const run = async (strengths: Tag[] | null) => {
    setBusy(true);
    setErr("");
    try {
      await api.patchModel(model.id, { strengths });
      await onSaved();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <Modal
      title={`强项 · ${model.display_name}`}
      onClose={onClose}
      actions={
        <>
          {err && <span className="err mp-foot-note">{err}</span>}
          <button className="btn" style={err ? undefined : { marginRight: "auto" }} disabled={busy || !model.strengths_custom} title={model.strengths_custom ? "丢弃手动修改,回到自动推断的强项" : "当前已经是自动推断"} onClick={() => run(null)}>恢复自动</button>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={busy} onClick={() => run(val)}>保存</button>
        </>
      }
    >
      <p className="mp-str-note">强项是根据模型系列和名称推断的标签,不是评测成绩。你可以按自己的使用体会调整;成员没有指定模型时,会按岗位强项从这里挑选。</p>
      <StrengthPicker value={val} onChange={setVal} disabled={busy} />
      <div className="mp-str-auto">
        自动推断:
        {model.strengths_auto.length ? <StrengthChips tags={model.strengths_auto} max={10} /> : <span>(无)</span>}
        {model.strengths_custom && <span className="tag">当前为自定义</span>}
      </div>
    </Modal>
  );
}

function AddProvider({ onClose, onAdded }: { onClose: () => void; onAdded: (id: string) => void }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [custom, setCustom] = useState({ name: "", base_url: "", kind: "openai_compatible", is_local: false });
  const [err, setErr] = useState("");
  const { providers } = useData();
  useEffect(() => { api.presets().then(setPresets).catch(() => undefined); }, []);
  const have = new Set(providers.map((p) => p.id));

  const [adding, guard] = useBusy();
  const addPreset = (id: string) => guard(async () => {
    try { onAdded((await api.addProvider({ preset: id })).id); } catch (e) { setErr((e as Error).message); }
  });
  const addCustom = () => guard(async () => {
    try { onAdded((await api.addProvider(custom)).id); } catch (e) { setErr((e as Error).message); }
  });

  return (
    <Modal title="添加服务商" onClose={onClose} wide>
      <div className="preset-grid">
        {presets.map((p) => (
          <button key={p.preset} className="preset" disabled={have.has(p.preset) || adding} onClick={() => void addPreset(p.preset)}>
            <LetterIcon name={p.name} size={30} />
            <span className="preset-text">
              <b>{p.name}</b>
              <span className="muted small">{have.has(p.preset) ? "已添加" : p.hint || KIND_LABEL[p.kind]}</span>
            </span>
          </button>
        ))}
      </div>
      <h4 className="sec-sm">自定义(任意 OpenAI 兼容接口)</h4>
      <div className="custom-row">
        <input placeholder="名称" value={custom.name} onChange={(e) => setCustom({ ...custom, name: e.target.value })} aria-label="名称" />
        <input placeholder="API 地址,如 http://127.0.0.1:8000/v1" value={custom.base_url} onChange={(e) => setCustom({ ...custom, base_url: e.target.value })} aria-label="API 地址" />
        <label className="check-inline"><input type="checkbox" checked={custom.is_local} onChange={(e) => setCustom({ ...custom, is_local: e.target.checked })} />本地</label>
        <button className="btn primary" disabled={!custom.name || !custom.base_url || adding} onClick={() => void addCustom()}>添加</button>
      </div>
      {err && <div className="err" style={{ marginTop: 8 }}>{err}</div>}
    </Modal>
  );
}
