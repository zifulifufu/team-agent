import { useCallback, useEffect, useState, type ReactNode } from "react";
import { CircleAlert, FolderOpen, LayoutTemplate, LoaderCircle, Workflow } from "lucide-react";
import {
  api, type ZoneDetail, type ZoneFileError, type ZoneItem, type ZoneItemState, type ZoneSurface,
} from "../api";
import { zoneIcon } from "../lib";
import { useI18n } from "../i18n";
import VideoZonePage from "./VideoZonePage";
import "../styles/zone.css";

/** 一个状态词。`blocked`（建了、本机有东西挡着）与 `planned`（还没建）**分开写**，
 *  因为它们的下一步不一样，用一个词概括会把用户送去错的那一步。 */
const STATE_LABEL: Record<ZoneItemState, string> = {
  ready: "Ready", partial: "Partly ready", blocked: "Blocked", planned: "Planned",
};

/** 一个专区页：**三块** —— 资料库 / 模板 / 工作流。
 *
 *  ⚠️ 用户 2026-09-27 明确：**「在视频专区里面的各个面板里不需要放分工、工具与技能。」**
 *  所以第四块（分工与技能）**不画在这一页上**。它没有消失：数据仍在 `zones.py` 的 `roles` 里，
 *  带着五条断言（技能名/工具名/模板 id/成员名存在、`in_template` 与群模板对齐）—— 它将来是
 *  「从这个专区起一个群」时那份名单，而不是这一页上的一节。
 *  后端也**不再把它塞进响应**：送过来却不渲染的东西，正是这个项目反复在治的那种「等于不存在」。
 *
 *  三块共用同一套渲染，而不是各写一张页：它们在四个专区里形状完全一样，差别只在内容。
 *  三份布局意味着每加一个专区就要再挑一次「这一块该长什么样」，而用户看到的是四个专区四个样子。
 *
 *  ⚠️⚠️ **三块是只读的索引，不是一堆按钮。** 能做事的入口只有一处 —— 工作台（视频专区就是
 *  上边那四个标签页）。在索引里塞一个「点了没反应」或者「点了要跳走再回来」的按钮，正是
 *  这个项目反复在处理的那类问题。所以每一项只有：名字、状态、一句说明。
 *
 *  ⚠️ 空就空着。后端不为了「这一块看起来太满」编内容，这里也不为了「页面好看」补一句假话。
 */
export default function ZonePage({ id }: { id: string }) {
  const { t } = useI18n();
  const [zone, setZone] = useState<ZoneDetail | null>(null);
  const [files, setFiles] = useState<ZoneFileError[]>([]);
  const [problem, setProblem] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const got = await api.zone(id);
      setZone(got.zone);
      setFiles(got.errors);
      setProblem("");
    } catch (e) {
      // 读不到就说读不到。空页面和「请求失败」长得一样，而它们的下一步完全不同。
      setProblem((e as Error).message);
      setZone(null);
    } finally {
      setLoading(false);
    }
  }, [id]);
  useEffect(() => { void load(); }, [load]);

  if (loading && !zone) {
    return <div className="zone"><p className="zone-dim">
      <LoaderCircle size={14} className="vz-spin" /> {t("Opening…")}</p></div>;
  }
  if (!zone) {
    return (
      <div className="zone">
        <header className="zone-head"><h2>{t("This zone could not be opened")}</h2></header>
        <p className="zone-note bad"><CircleAlert size={14} /> {problem}</p>
      </div>
    );
  }

  const Icon = zoneIcon(zone.icon);
  // 三块，顺序固定 —— 后端 `zones.DRAWN` 是同一件事的另一处读法（它决定响应里带哪几块）。
  const surfaces: { key: string; title: string; icon: ReactNode; surface: ZoneSurface }[] = [
    { key: "library", title: t("Library"), icon: <FolderOpen size={15} aria-hidden />, surface: zone.library },
    { key: "templates", title: t("Templates"), icon: <LayoutTemplate size={15} aria-hidden />, surface: zone.templates },
    { key: "workflows", title: t("Workflows"), icon: <Workflow size={15} aria-hidden />, surface: zone.workflows },
  ];

  return (
    <div className="zone">
      <header className="zone-head">
        <h2>
          <Icon size={20} aria-hidden /> {zone.name}
          {/* 徽标说的是**这个专区现在能不能用** —— 后端有一条断言守着「ready 的专区里至少
              有一件能跑的事」，所以这个词不是装饰。 */}
          <span className={"zone-badge " + zone.state}>
            {t(zone.state === "ready" ? "Ready" : "Planned")}
          </span>
        </h2>
        <p>{zone.blurb}</p>
        {!!files.length && (
          <p className="zone-note bad">
            <CircleAlert size={14} /> {t("{n} zone file(s) could not be read", { n: files.length })}: {files.slice(0, 3).map((f) => f.file).join("；")}
          </p>
        )}
      </header>

      {/* 工作台：这个专区**真正做事**的地方。没有工作台的专区不给一个假的操作区 ——
          它下面那四块会如实说哪里还空着，以及卡在哪。 */}
      {id === "video" && <section className="zone-bench"><VideoZonePage /></section>}

      {surfaces.map((s) => (
        <section key={s.key} className={"zone-surface zs-" + s.key}>
          <h3>{s.icon} {s.title}{!!s.surface.items.length && <span className="zone-count">{s.surface.items.length}</span>}</h3>
          {/* 这一块的说明放在标题下，因为它是「怎么读这一块」的说明书 —— 尤其当这一块
              是空的、或者被挡住的时候。 */}
          {!!s.surface.note && <p className="zone-dim">{s.surface.note}</p>}
          {!s.surface.items.length && <p className="zone-empty">{t("Nothing declared here yet.")}</p>}
          <ItemList t={t} items={s.surface.items} />
        </section>
      ))}
    </div>
  );
}

type TFn = (key: string, vars?: Record<string, string | number>) => string;

function ItemList({ t, items }: { t: TFn; items: ZoneItem[] }) {
  return (
    <ul className="zone-items">
      {items.map((it) => (
        <li key={it.id} className={"zone-item st-" + it.state}>
          <div className="zi-head">
            <b>{it.label}</b>
            {/* 计数是「现在有几件」，只有资料库那一块有，而且来自货架本身。 */}
            {typeof it.count === "number" && <span className="zi-count">{it.count}</span>}
            <span className={"zone-pill " + it.state}>{t(STATE_LABEL[it.state])}</span>
          </div>
          {!!it.note && <p className="zi-note">{it.note}</p>}
        </li>
      ))}
    </ul>
  );
}
