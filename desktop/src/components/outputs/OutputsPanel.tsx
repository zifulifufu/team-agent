import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronLeft, Download, File as FileIcon, FileText, Film, FolderOpen, Image as ImageIcon,
  Maximize2, Minimize2, Music, Package, PanelRightClose, RefreshCw,
} from "lucide-react";
import { api, relTime, type Group, type WorkspaceFile, type WorkspaceView } from "../../api";
import { humanBytes } from "../../lib";
import { useI18n } from "../../i18n";
import OutputsPreview from "./OutputsPreview";
import OutputMenu from "./OutputMenu";
import "../../styles/members.css";

/**
 * **成果栏** —— 一个项目干出了什么,挂在**中间框(聊天区)的右侧**。
 *
 * 为什么它在右边,而且不在左边那一栏里:它原先是左栏右侧那一栏(「成员 / 成果」两页)的一页,
 * 于是「这个项目产出了什么」和「这个群里都有谁」挤在同一根柱子上,而它离聊天最远。用户给的参照
 * 是 WorkBuddy:产物属于**这次对话**,所以面板挂在对话的右侧,点开一条就在这里看内容,而不是把
 * 文件交给别的程序、答案跑到别处去。2026-09-25 改的这件事,两条判据都落在几何上:
 * 「面板左边界 ≥ 聊天区右边界」和「面板右边界 = 窗口宽度」。
 *
 * ⚠️ 它不重新读一遍盘:数据来自 `api.workspace(gid)`,和「工作空间」那个弹层是**同一个出口**
 * (后端 `workspace_files` 的 rglob + 跳过 `.runs` 之类的临时目录)。这里只换一种展示,所以两处
 * 不会有一天开始各说各话。
 *
 * ⚠️ 刷新时机:群行的 `last_at` 每有一条新消息就变,所以 `stamp` 一变就重取 —— 这就是「每次群聊后
 * 自动出现」的实现,而不是靠轮询(轮询会在这个面板开着的时候一直敲门)。
 */
export default function OutputsPanel({ group, stamp, onCount, maximized, onMaximize, onClose }: {
  group: Group | null;
  /** 群最后一次动的时间。它一变就重取 —— 新一轮跑完,成果自己就上来了。 */
  stamp: number | undefined;
  /** 数出多少个文件,回填给外层(聊天头部那个入口上的数字)。**同一个数只有这一处算**。 */
  onCount?: (n: number) => void;
  /** 是否已经占满主区域(聊天让位)。 */
  maximized: boolean;
  onMaximize: () => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [view, setView] = useState<WorkspaceView | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");
  /** 正在这里看哪一个文件。null = 看清单。 */
  const [sel, setSel] = useState<WorkspaceFile | null>(null);
  /**
   * 右键菜单开在哪儿、对哪一条。
   *
   * 为什么要有它:清单上一行原来只有两个动作(点一行=在这里看、右边那个 ↗ =交给别的程序),而
   * 「用对应的工具打开 / 找到文件本体 / 分享给微信」至少要四件事 —— 塞进一行会变成一排谁也不认识的
   * 小图标。右键才是「对这条东西还能做什么」的自然位置。
   */
  const [menu, setMenu] = useState<{ at: { x: number; y: number }; file: WorkspaceFile } | null>(null);
  // 换群/刷新之后那一条可能已经不在了:菜单要跟着关,否则「打开」的是一个盘上没了的文件。
  useEffect(() => {
    if (menu && !(view?.files ?? []).some((f) => f.path === menu.file.path)) setMenu(null);
  }, [view, menu]);

  const gid = group?.id ?? "";
  const load = useCallback(async () => {
    if (!gid) { setView(null); return; }
    setBusy(true);
    try {
      setView(await api.workspace(gid));
      setFailed("");
    } catch (e) {
      // 拿不到就明说,不留一个空列表让人以为「这个项目什么也没产出」。
      setView(null);
      setFailed((e as Error).message);
    } finally {
      setBusy(false);
    }
    // ⚠️ 依赖是**群 id**,不是 `group` 对象。`useData` 每隔几秒就换一批新的群对象,于是这个 callback
    // 每次都变、下面那个 effect 每次都重跑 —— **面板一开着就在反复重取整个工作目录**,而组件自己的
    // 注释写着「不靠轮询,靠 stamp 一变就重取」。依赖对象等于把那条设计悄悄推翻了(而且工作目录大时
    // 每次都是一次真实的全目录 rglob)。
  }, [gid]);

  useEffect(() => { void load(); }, [load, stamp]);

  /** 按文件夹分组,组内最新的文件在最上面;组之间也按最新排。 */
  const groups = useMemo(() => {
    const files = view?.files ?? [];
    const by = new Map<string, WorkspaceFile[]>();
    for (const f of files) {
      const key = f.folder || "";
      const list = by.get(key);
      if (list) list.push(f); else by.set(key, [f]);
    }
    return [...by.entries()]
      .map(([folder, list]) => ({
        folder,
        // 组标题取路径的**最后一段**:任务的目录名是「任务名-随机后缀/子目录」这种,整条放不下。
        label: folder ? folder.split("/").filter(Boolean).slice(-1)[0] : t("In the folder itself"),
        full: folder,
        files: [...list].sort((a, b) => b.modified - a.modified),
        bytes: list.reduce((n, f) => n + (f.size || 0), 0),
        newest: Math.max(0, ...list.map((f) => f.modified || 0)),
      }))
      .sort((a, b) => b.newest - a.newest);
  }, [view, t]);

  const total = view?.files.length ?? 0;
  const bytes = (view?.files ?? []).reduce((n, f) => n + (f.size || 0), 0);
  // ⚠️ 在 effect 里回填,不在 render 里 —— render 里回调父组件会直接把两边都拖进无限重渲染。
  useEffect(() => { onCount?.(total); }, [total, onCount]);

  // 换群 = 换项目:上一个项目的文件不能继续留在这一栏里当「当前看的这个」。
  useEffect(() => { setSel(null); }, [group?.id]);
  // 看的东西被新一轮覆盖/删掉了:退回清单,而不是继续显示一个盘上已经没有的文件。
  useEffect(() => {
    if (sel && !(view?.files ?? []).some((f) => f.path === sel.path)) setSel(null);
  }, [view, sel]);

  /**
   * 交给系统里的程序打开。
   *
   * 首选外壳的系统默认程序(和项目文件夹用的是同一个 IPC)。**没有外壳时(浏览器里)退回下载**
   * —— 这一栏必须在哪种环境里都有一个真能干活的动作,而不是一个点了没反应的按钮。
   */
  const openExternally = async (f: WorkspaceFile) => {
    const shell = window.teamAgent?.openPath;
    const abs = view ? `${view.path}/${f.path}` : "";
    if (shell && abs) {
      const got = await shell(abs);
      if (got.ok) return;
    }
    if (!group) return;
    try {
      const blob = await api.workspaceBytes(group.id, f.path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = f.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch {
      /* 两条路都不通就算了:行上的 tooltip 里写着完整相对路径,用户可以自己去工作目录拿 */
    }
  };

  const iconFor = (kind: string) =>
    kind === "image" ? ImageIcon : kind === "video" ? Film : kind === "audio" ? Music
      : kind === "document" ? FileText : FileIcon;

  if (!group) {
    return (
      <aside className="outpanel" aria-label={t("Outputs")}>
        <div className="op-head">
          <Package size={15} className="op-ico" aria-hidden />
          <span className="op-titles"><span className="op-title">{t("Outputs")}</span></span>
          <span className="grow" />
          <button className="icon-btn tiny" title={t("Collapse the outputs column")}
            aria-label={t("Collapse the outputs column")} onClick={onClose}>
            <PanelRightClose size={15} />
          </button>
        </div>
        <div className="op-body">
          <div className="mrail-empty">{t("Open a group chat and everything it produced shows up here")}</div>
        </div>
      </aside>
    );
  }

  const HeadIcon = sel ? iconFor(sel.kind) : Package;

  return (
    <aside className={"outpanel" + (maximized ? " maximized" : "")} aria-label={t("Outputs")}>
      <div className="op-head">
        {/* 返回清单。**必须有一个显式的返回键**:点一行就进了内容页,没有它就只剩「收起整栏」
            一条路 —— 那会让人以为清单不见了。 */}
        {sel && (
          <button className="icon-btn tiny op-back" onClick={() => setSel(null)}
            title={t("Back to the list")} aria-label={t("Back to the list")}>
            <ChevronLeft size={15} />
          </button>
        )}
        <HeadIcon size={15} className="op-ico" aria-hidden />
        <span className="op-titles">
          <span className="op-title" title={sel ? sel.path : group.name}>
            {sel ? sel.name : t("Outputs")}
          </span>
          <span className="op-sub">
            {sel
              ? t("{size} · {when}", { size: humanBytes(sel.size), when: relTime(sel.modified) })
              : t("What this project produced · {n}", { n: total })}
          </span>
        </span>
        <span className="grow" />
        {sel ? (
          <button className="icon-btn tiny" title={t("Open with another program")}
            aria-label={t("Open with another program")} onClick={() => void openExternally(sel)}>
            <FolderOpen size={15} />
          </button>
        ) : (
          <button className="icon-btn tiny out-refresh" title={t("Refresh")} aria-label={t("Refresh")}
            onClick={() => void load()} disabled={busy}>
            <RefreshCw size={14} className={busy ? "spin" : ""} />
          </button>
        )}
        {/* 最大化:占满主区域(聊天暂时让位),再点一下还原。图标本身说明方向,和侧栏那个开关
            不同 —— 它不换位置,只在两个图标之间切。 */}
        <button className="icon-btn tiny op-max" onClick={onMaximize}
          title={t(maximized ? "Back to the chat" : "Fill the window with this panel")}
          aria-label={t(maximized ? "Back to the chat" : "Fill the window with this panel")}
          aria-pressed={maximized}>
          {maximized ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
        </button>
        <button className="icon-btn tiny op-close" title={t("Collapse the outputs column")}
          aria-label={t("Collapse the outputs column")} onClick={onClose}>
          <PanelRightClose size={15} />
        </button>
      </div>
      {sel ? (
        <div className="op-body op-body-one">
          <OutputsPreview gid={group.id} file={sel} onOpenExternally={() => void openExternally(sel)} />
        </div>
      ) : (
        <div className="op-body">
          <div className="out">
            <div className="out-head">
              <span className="out-count">
                {total > 0
                  ? t("{n} files · {size}", { n: total, size: humanBytes(bytes) })
                  : t("Nothing produced yet")}
              </span>
            </div>
            {failed && <div className="mrail-empty">{t("The workspace could not be read: {why}", { why: failed })}</div>}
            {!failed && total === 0 && (
              <div className="mrail-empty">{t("Nothing here yet — send a message and whatever the members write lands in this list")}</div>
            )}
            {groups.map((g) => (
              <div className="out-group" key={g.full || "__root"}>
                <div className="out-ghead" title={g.full || undefined}>
                  <FolderOpen size={12} aria-hidden />
                  <span className="out-gname">{g.label}</span>
                  <span className="out-gn">{t("{n} files", { n: g.files.length })}</span>
                  <span className="out-gsz">{humanBytes(g.bytes)}</span>
                </div>
                {g.files.map((f) => {
                  const Icon = iconFor(f.kind);
                  return (
                    <div className="out-row" key={f.path} title={f.path}
                      /* 右键:对**这一条**还能做什么。左键仍然是「在这里看它」—— 两个动作分开。 */
                      onContextMenu={(e) => { e.preventDefault(); setMenu({ at: { x: e.clientX, y: e.clientY }, file: f }); }}>
                      {/* 点这一行 = **在这一栏里看它**(原来是交给别的程序打开,答案跑到别处)。
                          要真的打开,右边那个 ↗ 就是 —— 两个动作分开,才不会只能二选一。 */}
                      <button className="out-pick" onClick={() => setSel(f)}
                        onContextMenu={(e) => { e.preventDefault(); setMenu({ at: { x: e.clientX, y: e.clientY }, file: f }); }}>
                        <Icon size={13} className="out-ico" aria-hidden />
                        <span className="out-name">{f.name}</span>
                        <span className="out-time">{relTime(f.modified)}</span>
                        <span className="out-size">{humanBytes(f.size)}</span>
                      </button>
                      <button className="icon-btn tiny out-act" title={t("Open with another program")}
                        aria-label={t("Open with another program")} onClick={() => void openExternally(f)}>
                        <Download size={12} aria-hidden />
                      </button>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      )}
      {/* 菜单挂在面板里,不在每一行里 —— 一个就够,而且它要能盖过列表(见 `.om` 的 z-index)。 */}
      {menu && (
        <OutputMenu at={menu.at} file={menu.file} workspace={view?.path ?? ""}
          onView={() => setSel(menu.file)} onClose={() => setMenu(null)} />
      )}
    </aside>
  );
}
