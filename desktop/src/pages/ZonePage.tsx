import { useCallback, useEffect, useState, type ReactNode } from "react";
import { CircleAlert, FolderOpen, LayoutTemplate, LoaderCircle, Workflow } from "lucide-react";
import {
  api, type ZoneDetail, type ZoneFileError, type ZoneItem, type ZoneItemState, type ZoneSurface,
} from "../api";
import { zoneIcon } from "../lib";
import { useI18n } from "../i18n";
import { useData } from "../data";
import VideoZonePage from "./VideoZonePage";
import "../styles/zone.css";

/** 一个状态词。`blocked`（建了、本机有东西挡着）与 `planned`（还没建）**分开写**，
 *  因为它们的下一步不一样，用一个词概括会把用户送去错的那一步。
 *
 *  ⚠️ 每一条都是一个**字面量** `t("…")` 调用，而不是「先拼一张字符串表、运行时再查」。
 *  `scripts/check-i18n.py` 只能看见字面量调用，所以那种写法下这四条的译文**整个逃过检查** ——
 *  实测就是这样：`Ready` 和 `Partly ready` 在 ZH 表里根本不存在，中文界面下徽章显示英文；
 *  而 `Blocked` 撞上了权限页那条 `"Blocked": "禁止"`（「总是禁止」的语境），
 *  专区里它的意思是「建了、本机有东西挡着」——**同一个英文词，两种意思**，所以这里换了词。
 */
type TFn = (key: string, vars?: Record<string, string | number>) => string;

const STATE_LABEL: Record<ZoneItemState, (t: TFn) => string> = {
  ready: (t) => t("Ready"),
  partial: (t) => t("Partly ready"),
  blocked: (t) => t("Blocked here"),
  planned: (t) => t("Planned"),
};

/** 一行的排序权重：**能用的排前面**。
 *
 *  ⚠️ 声明顺序会把 ready 和 planned 交错排列（写作专区的工作流就是「ready → planned → planned」,
 *  创作专区的模板是「ready → planned → planned」），于是「这里能做什么」必须把整块读完才知道。
 *  `Array.prototype.sort` 是**稳定**的，所以同一个状态的若干行**保持声明顺序** —— 视频专区那 6 条
 *  作曲风格预设的顺序不会被打乱。
 */
const STATE_RANK: Record<ZoneItemState, number> = { ready: 0, partial: 1, blocked: 2, planned: 3 };

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
export default function ZonePage({ id, onOpen }: { id: string; onOpen?: (gid: string) => void }) {
  const { t } = useI18n();
  // 起群之后要**重取列表**再跳。两件事都靠它：模板会新建成员（成员名册会过期），
  // 而 `App` 有一条「打开中的群不在列表里就回首页」的判断 —— 用旧的群列表切过去会被它弹回首页。
  const { reload } = useData();
  const [zone, setZone] = useState<ZoneDetail | null>(null);
  const [files, setFiles] = useState<ZoneFileError[]>([]);
  const [problem, setProblem] = useState("");
  const [loading, setLoading] = useState(true);
  /**
   * 从一个起点起一个群要几秒（要建成员、建群、再进群），所以按钮要有"正在起"，而且**不能连点** ——
   * 点两次会建出两个群，而用户只想要一个。
   */
  const [starting, setStarting] = useState("");
  const [startProblem, setStartProblem] = useState("");
  const [failedId, setFailedId] = useState("");
  const startFrom = useCallback(async (itemId: string) => {
    if (!onOpen || starting) return;
    setStarting(itemId);
    setStartProblem("");
    setFailedId("");
    try {
      const g = await api.createFromTemplate(itemId);
      // ⚠️ 顺序是「起群 → 重取 → 再跳」。少了中间这一步，用户就**落不到新群里**：切到新群的那一刻
      // App 拿到的还是**旧的**群列表（它 5 秒才轮询一次），而它有一条「打开中的群不在列表里就回首页」
      // 的判断 —— 实测（把 `reload()` 临时撤掉跑一遍冒烟）：群确实建出来了，但页面既不在新群、
      // 也不在首页，用户看不到自己刚点的那个动作的结果。首页起群那条路一直是这么写的
      // （`HomePage.useTemplate`：「a template may have created members, so refresh both lists」）。
      await reload();
      onOpen(g.id);
    } catch (e) {
      // 起不来就说起不来。按钮转回去、原话贴在那**一行**上，而不是静默地什么都不发生。
      setStartProblem((e as Error).message);
      setFailedId(itemId);
    } finally {
      setStarting("");
    }
  }, [onOpen, starting, reload]);

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
  /* 「今天能做什么 / 还缺什么」—— **由这三块的数据算出来**，不是手写的文案。
     ⚠️ 徽标只说得出一件事（这个专区 ready 还是 planned），而写作专区里躺着 3 条 ready 模板 + 1 条
     ready 工作流、创作专区有 3 条：徽标说「还不能用」、条目说「能用」，用户不知道该信哪个。
     算出来的一行让两件事同时为真 —— 不撒谎，也不用把 `planned` 改叫 `ready`。 */
  const all = surfaces.flatMap((s) => s.surface.items);
  const usable = all.filter((x) => x.state === "ready");
  const pending = all.length - usable.length;

  return (
    <div className="zone">
      <header className="zone-head">
        <h2>
          <Icon size={20} aria-hidden /> {zone.name}
          {/* 徽标说的是**这个专区现在能不能用** —— 后端有一条断言守着「ready 的专区里至少
              有一件能跑的事」，所以这个词不是装饰。⚠️ 它和条目共用**同一张状态词表**：
              原来这里写的是 `zone.state === "ready" ? "Ready" : "Planned"`，那是对同一个问题
              的第二处判断 —— 一旦哪一天有专区是 `partial`／`blocked`，这个徽标会管它叫「规划中」。 */}
          <span className={"zone-badge " + zone.state}>{STATE_LABEL[zone.state](t)}</span>
        </h2>
        <p>{zone.blurb}</p>
        {/* 一行摘要：今天能用的是**哪几件**（点名）、还差几件。徽标与条目之间的矛盾由它来消解。 */}
        {!!all.length && (
          <p className="zone-summary">
            {usable.length
              ? t("Usable today: {names}", {
                  names: usable.slice(0, 4).map((x) => x.label).join("、") +
                    (usable.length > 4 ? "…" : ""),
                })
              : t("Nothing here is usable yet")}
            {!!pending && <span className="zs-missing">{t("{n} not built yet", { n: pending })}</span>}
          </p>
        )}
        {!!files.length && (
          <p className="zone-note bad">
            <CircleAlert size={14} /> {t("{n} zone file(s) could not be read", { n: files.length })}: {files.slice(0, 3).map((f) => f.file).join("；")}
          </p>
        )}
        {!!startProblem && failedId === "" && (
          <p className="zone-note bad"><CircleAlert size={14} /> {startProblem}</p>
        )}
      </header>

      {/* 工作台：这个专区**真正做事**的地方。没有工作台的专区不给一个假的操作区 ——
          它下面那四块会如实说哪里还空着，以及卡在哪。 */}
      {id === "video" && <section className="zone-bench"><VideoZonePage /></section>}

      {surfaces.map((s) => (
        <section key={s.key} className={"zone-surface zs-" + s.key}>
          {/* 计数从「一共几条」改成「能用几条 / 一共几条」 —— 「要变成能用还缺多少」不必展开任何一行
              就能读出来。 */}
          <h3>{s.icon} {s.title}{!!s.surface.items.length && (
            <span className="zone-count">
              {s.surface.items.filter((x) => x.state === "ready").length}/{s.surface.items.length}
            </span>
          )}</h3>
          {/* 这一块的说明放在标题下，因为它是「怎么读这一块」的说明书 —— 尤其当这一块
              是空的、或者被挡住的时候。 */}
          {!!s.surface.note && <p className="zone-dim">{s.surface.note}</p>}
          {!s.surface.items.length && <p className="zone-empty">{t("Nothing declared here yet.")}</p>}
          <ItemList t={t} items={s.surface.items} starting={starting} canStart={!!onOpen}
                    failed={failedId} problem={startProblem} onStart={startFrom} />
        </section>
      ))}
    </div>
  );
}

function ItemList({ t, items, starting, canStart, failed, problem, onStart }:
                  { t: TFn; items: ZoneItem[]; starting: string; canStart: boolean;
                    failed: string; problem: string; onStart: (id: string) => void }) {
  // 能用的排前面（稳定排序，见 `STATE_RANK`）。只影响**画出来的顺序**，数据与声明顺序都不动。
  const rows = [...items].sort((a, b) => STATE_RANK[a.state] - STATE_RANK[b.state]);
  return (
    <ul className="zone-items">
      {rows.map((it) => {
        // ⚠️ Only a row the backend marked with an `action` can be pressed. That flag comes from
        // **the same lookup `POST /api/templates/{id}/create-group` performs**, so a button here can
        // never be one that the endpoint behind it refuses — which is the failure this page's header
        // comment warns about ("a button that does nothing, or one that jumps away and comes back").
        // Everything unmarked — every `planned` row, and the video zone's composer presets — stays
        // exactly what it was: an index row.
        // ⚠️ 两件事都要成立才画按钮：后端标了 `action`，**而且**这一页真的能带用户走。
        // 只看 `action` 的话，一个不传 `onOpen` 的调用点会得到一排**点了什么也不发生**的按钮 ——
        // 正是这段头注释点名禁止的那种东西（`startFrom` 里那个守卫治不了它：守卫在点击之后）。
        const startable = canStart && it.action === "create-group";
        return (
          <li key={it.id} className={"zone-item st-" + it.state}>
            <div className="zi-head">
              <b>{it.label}</b>
              {/* 计数是「现在有几件」，只有资料库那一块有，而且来自货架本身。 */}
              {typeof it.count === "number" && <span className="zi-count">{it.count}</span>}
              <span className={"zone-pill " + it.state}>{STATE_LABEL[it.state](t)}</span>
            </div>
            {!!it.note && <p className="zi-note">{it.note}</p>}
            {startable && (
              <button className="btn small zi-start" disabled={!!starting}
                      onClick={() => onStart(it.id)}>
                {starting === it.id ? t("Starting…") : t("Start a group from this")}
              </button>
            )}
            {/* 起不来就说在**按下按钮的那一行上**说。贴在页头的话，按钮在下方、用户点完看到的
                是一个没有任何变化的页面 —— 从用户视角那就是静默失败，而它其实说了。 */}
            {startable && failed === it.id && !!problem && (
              <p className="zi-start-err"><CircleAlert size={13} /> {problem}</p>
            )}
          </li>
        );
      })}
    </ul>
  );
}
