import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, ChevronRight, Loader2, PackagePlus, Search, ShieldCheck } from "lucide-react";
import { api, type GalleryApplyResult, type GalleryItem, type GalleryKind, type GalleryOverview } from "../api";
import { useData } from "../data";
import type { PageProps } from "./SettingsModal";
import "../styles/gallery.css";

type Tab = GalleryKind | "all";

const S = (v: unknown) => (v == null ? "" : String(v));
const A = (v: unknown) => (Array.isArray(v) ? v.map(String) : []);

/** 自定义模板文件的格式示例(放在数据目录里就会自动出现在这一页)。 */
const SAMPLE = `{
  "schema_version": 1,
  "catalog_version": "1.0.0",
  "author": "你们团队的名字",
  "items": [
    {
      "id": "weekly-report",
      "kind": "team",
      "name": "周报小组",
      "summary": "每周固定输出一份周报",
      "icon": "🗓️",
      "members": ["主持", "记录", "评审"],
      "host": "主持",
      "skills": ["公文写作规范"],
      "prompt": "本群每周汇总一次进展。"
    },
    {
      "id": "tone",
      "kind": "skill",
      "name": "对外措辞规范",
      "summary": "对外沟通的语气要求",
      "body": "对外一律用正式语气,不承诺时间表。"
    }
  ]
}`;

/**
 * 设置 → 模板中心。
 *
 * 模板随程序分发,**打开就能用**:不用 clone 任何仓库、不用填路径、不联网。
 * 内容全部是本程序自己写的(见 README「许可与合规」),因此分发不产生第三方许可义务。
 * 想放团队自己的模板,把 JSON 丢进数据目录的 templates/ 里即可(这一页会显示读取结果与错误原因)。
 */
export default function GalleryPage({ onTab, onOpenGroup }: PageProps) {
  const { groups, reload, reloadGroups } = useData();
  const [ov, setOv] = useState<GalleryOverview | null>(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState<Tab>("all");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState("");
  const [details, setDetails] = useState<Record<string, GalleryItem & { def: Record<string, unknown> }>>({});
  const [busy, setBusy] = useState("");
  const [res, setRes] = useState<GalleryApplyResult | null>(null);
  const [join, setJoin] = useState("");
  const [showSchema, setShowSchema] = useState(false);

  const load = useCallback(() => {
    api.gallery().then(setOv).catch((e) => setErr((e as Error).message));
  }, []);
  useEffect(load, [load]);

  const apply = async (it: GalleryItem, overwrite = false) => {
    if (busy) return;
    setBusy(it.id);
    setRes(null);
    setErr("");
    try {
      const r = await api.galleryApply(it.id, {
        overwrite: overwrite || undefined,
        group_id: it.kind === "agent" && join ? join : undefined,
      });
      setRes(r);
      await reload();          // 可能新建了成员
      await reloadGroups();    // 可能新建了群
      load();                  // 刷新「已装」状态
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  const toggle = async (it: GalleryItem) => {
    if (open === it.id) {
      setOpen("");
      return;
    }
    setOpen(it.id);
    if (details[it.id]) return;
    try {
      const d = await api.galleryDetail(it.id);
      setDetails((m) => ({ ...m, [it.id]: d }));
    } catch {
      /* 详情拿不到不影响直接使用 */
    }
  };

  const items = useMemo(() => {
    if (!ov) return [];
    const k = q.trim().toLowerCase();
    return ov.items.filter((i) => (tab === "all" || i.kind === tab)
      && (!k || [i.name, i.summary, ...i.tags, i.id].some((x) => (x || "").toLowerCase().includes(k))));
  }, [ov, tab, q]);

  if (!ov) return <div className="empty big">{err || "加载中…"}</div>;

  const tabs: { id: Tab; label: string; n: number }[] = [
    { id: "all", label: "全部", n: ov.total },
    ...ov.categories.map((c) => ({ id: c.id as Tab, label: c.label, n: ov.counts[c.id] ?? 0 })),
  ];
  const hint = ov.categories.find((c) => c.id === tab)?.hint ?? "程序自带的一手模板,点一下就能用;下方可放你们自己的模板 JSON。";

  const actionLabel = (it: GalleryItem) => {
    if (it.kind === "team") return "建成群聊";
    if (it.kind === "agent") return join ? "创建并入群" : "创建成员";
    if (it.kind === "skill") return it.installed ? "重新导入" : "导入技能";
    if (it.kind === "prompt") return it.installed ? "覆盖为模板版" : "存入提示词库";
    return "添加(停用)";
  };

  const Detail = ({ it }: { it: GalleryItem }) => {
    const d = details[it.id];
    const p = it.preview;
    if (!d) return <div className="gl-detail muted small">正在读取内容…</div>;
    return (
      <div className="gl-detail">
        {it.kind === "team" && (
          <>
            <div className="gl-roles">
              {(p.members ?? []).map((m) => (
                <span key={m.name} className={"gl-role" + (m.name === p.host ? " host" : "")}
                      title={m.name === p.host ? `${m.name}(群主)` : m.role || m.name}>
                  <b>{m.avatar} {m.name}</b>{m.role && <i>{m.role}</i>}
                </span>
              ))}
            </div>
            {A(d.def.skills).length > 0 && (
              <div className="gl-line">随附技能:{A(d.def.skills).map((s) => <span key={s} className="tag on">{s}</span>)}</div>
            )}
            {S(d.def.prompt) && <div className="gl-line">群规则:{S(d.def.prompt)}</div>}
          </>
        )}
        {it.kind === "agent" && (
          <>
            <div className="gl-line">{S(d.def.avatar)} {S(d.def.role)}{A(d.def.tags).length > 0 && <> · 强项 {A(d.def.tags).join(" / ")}</>}</div>
            <pre className="gl-body">{S(d.def.prompt)}</pre>
          </>
        )}
        {it.kind === "skill" && (
          <>
            <div className="gl-line">
              用途:{S(d.def.scope) === "group" ? "群聊规则(挂到整个群)" : "成员技能(勾给成员)"}
            </div>
            <pre className="gl-body">{S(d.def.body)}</pre>
          </>
        )}
        {it.kind === "prompt" && (
          <>
            <div className="gl-line">类型:{S(d.def.kind) === "group" ? "群提示词" : "通用提示词"}</div>
            <pre className="gl-body">{S(d.def.content)}</pre>
          </>
        )}
        {it.kind === "mcp" && (
          <>
            <code className="gl-cmd">{[S(d.def.command), ...A(d.def.args)].join(" ")}</code>
            <div className="gl-line">{S(d.def.note)}</div>
            {A(d.def.env_keys).length > 0 && <div className="gl-line">需要你填:{A(d.def.env_keys).join("、")}</div>}
          </>
        )}
      </div>
    );
  };

  return (
    <div className="sp">
      <h2 className="sp-title"><PackagePlus size={20} aria-hidden /> 模板中心</h2>
      <p className="sp-desc">
        现成的团队、角色、技能、提示词和 MCP 用法,<b>点一下就能用</b> —— 不用下载任何东西,也不用填路径,全程在这台电脑上完成。
        模板都是本程序自带的原创内容(目录版本 {ov.catalog_version}),不包含任何第三方项目的文件或数据。
      </p>

      {err && <div className="gl-msg bad" role="alert">{err}</div>}

      {res && (
        <div className="gl-result" role="status">
          <div className="gl-result-top">
            <b>{res.name}</b>
            <span>{res.summary}</span>
          </div>
          {res.added.length > 0 && <div className="gl-line ok">已完成:{res.added.join("、")}</div>}
          {res.skipped.length > 0 && <div className="gl-line">已存在,未改动:{res.skipped.join("、")}</div>}
          {res.notes.map((n) => <div className="gl-line note" key={n}><AlertTriangle size={12} aria-hidden /> {n}</div>)}
          {res.group && onOpenGroup && (
            <button className="btn small primary" onClick={() => onOpenGroup(res.group!.id)}>打开群聊「{res.group.name}」</button>
          )}
          {res.kind === "skill" && <button className="btn small" onClick={() => onTab("skills")}>去勾选技能</button>}
          {res.kind === "prompt" && <button className="btn small" onClick={() => onTab("prompts")}>去挂提示词</button>}
          {res.kind === "mcp" && <button className="btn small" onClick={() => onTab("mcp")}>去核对并启用</button>}
        </div>
      )}

      <div className="gl-tabs" role="tablist">
        {tabs.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id} className={"gl-tab" + (tab === t.id ? " on" : "")}
                  onClick={() => { setTab(t.id); setOpen(""); }}>
            {t.label} <span className="gl-count">{t.n}</span>
          </button>
        ))}
        <label className="gl-search">
          <Search size={13} aria-hidden />
          <input value={q} placeholder="搜索模板…" aria-label="搜索模板" onChange={(e) => setQ(e.target.value)} />
        </label>
      </div>
      <p className="gl-hint">{hint}</p>

      {tab === "agent" && groups.length > 0 && (
        <div className="gl-join">
          <span>创建成员后:</span>
          <select value={join} onChange={(e) => setJoin(e.target.value)} aria-label="创建后拉进哪个群">
            <option value="">只创建,不入群</option>
            {groups.map((g) => <option key={g.id} value={g.id}>并加入「{g.name}」</option>)}
          </select>
        </div>
      )}

      <div className="card flush">
        {items.length === 0
          ? <div className="empty">没有匹配的模板</div>
          : items.map((it) => {
            const expanded = open === it.id;
            return (
              <div className={"gl-item" + (expanded ? " open" : "")} key={it.id}>
                <button className="gl-head" aria-expanded={expanded} onClick={() => void toggle(it)}>
                  <ChevronRight size={14} className={"gl-chev" + (expanded ? " open" : "")} aria-hidden />
                  <span className="gl-icon" aria-hidden>{it.icon}</span>
                  <span className="gl-main">
                    <span className="gl-title">
                      {it.name}
                      {it.tags.map((t) => <span className="tag" key={t}>{t}</span>)}
                      {it.source.startsWith("custom:") && <span className="tag new">自备</span>}
                    </span>
                    <span className="gl-sum">{it.summary}</span>
                  </span>
                </button>
                <div className="gl-side">
                  {it.installed && <span className="gl-state"><ShieldCheck size={12} aria-hidden /> {it.state_note}</span>}
                  <button className="btn small primary" disabled={!!busy || (it.kind === "mcp" && it.installed)}
                          onClick={() => void apply(it, it.installed && it.kind !== "mcp")}>
                    {busy === it.id && <Loader2 size={12} className="spin" aria-hidden />} {actionLabel(it)}
                  </button>
                </div>
                {expanded && <Detail it={it} />}
              </div>
            );
          })}
      </div>

      <div className="sec">自定义模板(可选)</div>
      <div className="card gl-custom">
        <div className="gl-line">
          想放你们团队自己的模板?把 JSON 文件放进下面这个目录就会自动出现在这一页 —— <b>改完刷新即可,不用重启</b>;
          格式不对的条目会被跳过并写明原因。
        </div>
        <code className="gl-cmd">{ov.custom.dir}</code>
        <div className="gl-line muted small">
          当前读取到 {ov.custom.loaded} 条
          {ov.custom.files.length > 0 && <> —— {ov.custom.files.map((f) => `${f.name}(${f.items} 条${f.author ? `,${f.author}` : ""})`).join(";")}</>}
          {!ov.custom.exists && <> —— 目录还不存在,需要就自己建一个</>}
        </div>
        {ov.custom.errors.length > 0 && (
          <div className="gl-errors">
            {ov.custom.errors.map((e, i) => (
              <div className="gl-line bad" key={`${e.file}-${i}`}>
                <AlertTriangle size={12} aria-hidden /> {e.file}{e.id ? ` · ${e.id}` : ""}:{e.reason}
              </div>
            ))}
          </div>
        )}
        <button className="link small" onClick={() => setShowSchema((v) => !v)}>
          {showSchema ? "收起格式说明" : "看格式说明与示例"}
        </button>
        {showSchema && (
          <>
            <div className="gl-line small">
              支持的 <code>kind</code>:team(团队)/ agent(角色)/ skill(技能)/ prompt(提示词)。
              <b>命令类(MCP)不接受</b> —— 那等于让一个 JSON 文件决定本机要执行什么命令,请到「MCP」页自己添加。
              提示词的类型写在 <code>prompt_kind</code> 里(general 或 group);可选的 <code>requires</code> 写所需的最低目录版本。
            </div>
            <pre className="gl-body">{SAMPLE}</pre>
          </>
        )}
      </div>
    </div>
  );
}
