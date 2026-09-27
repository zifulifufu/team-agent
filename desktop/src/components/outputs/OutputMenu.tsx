import { useEffect, useState, type ReactNode } from "react";
import { Copy, Eye, FileSearch, FolderOpen, Link2, Mail, MessageCircle, Send, Share2 } from "lucide-react";
import type { WorkspaceFile } from "../../api";
import { useI18n } from "../../i18n";
import { notify, useOutside } from "../../ui";

/** What a share target's icon should be, by the id the main process reports. */
const SHARE_ICON: Record<string, typeof Mail> = {
  mail: Mail, messages: MessageCircle, notes: Copy, telegram: Send, whatsapp: MessageCircle,
};
/** How each target is named in the menu. Ids come from the shell, wording lives here. */
const SHARE_LABEL: Record<string, string> = {
  wechat: "WeChat", whatsapp: "WhatsApp", telegram: "Telegram", feishu: "Feishu", dingtalk: "DingTalk",
  qq: "QQ", mail: "Mail", messages: "Messages", notes: "Notes",
};

/**
 * 一条成果上的**右键菜单**。
 *
 * 为什么要有它:清单上原来只有两个动作(点一行=在这里看、右边那个 ↗ =交给别的程序),而用户要的是
 * 「用对应的工具打开,或者分享给微信」—— 那至少是四件事(看 / 用别的程序打开 / 找到文件本体 / 分享),
 * 塞进一行按钮会变成一个谁也不认识的小图标排。
 *
 * 三条设计约束:
 *  1. **做不到的项不出现**。浏览器里没有外壳能力(`window.teamAgent.*` 为 null),那几项直接不渲染
 *     —— 和侧栏那个开文件夹的按钮同一条规矩:宁可少一项,不要一个点了没反应的条目。
 *  2. **分享目标是扫出来的**,不是写死的(哪个聊天软件装了因机器而异,由主进程扫描 /Applications)。
 *     一个「打开什么都没发生」的条目比没有条目更糟。
 *  3. **每个动作都回话**。复制这种动作尤其容易骗人:它不是图片时复制的是**路径**,菜单必须说清楚
 *     复制的是哪一个 —— 否则用户粘到微信里发现是一行路径,而界面上写着「已复制」。
 *
 * ⚠️ 措辞是「用 X 打开」而不是「发送到 X」:这里做的是 `open -a <app> <file>`,和访达的
 * 「打开方式」完全一样,之后那个程序拿文件做什么(邮件写成附件、微信弹发送面板)是**它自己的行为**,
 * 不是这里承诺的。
 */
export default function OutputMenu({ at, file, workspace, onView, onClose }: {
  /** 指针位置(视口坐标)。菜单贴着它开,并保证不越出窗口。 */
  at: { x: number; y: number };
  file: WorkspaceFile;
  /** 这个群工作目录的**绝对路径**(接口给的),拼出来才是外壳能打开的那个路径。 */
  workspace: string;
  onView: () => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const ref = useOutside<HTMLDivElement>(true, onClose);
  const [targets, setTargets] = useState<{ id: string; app: string }[] | null>(null);
  /** 放置:先按指针位置,量到尺寸后再夹进窗口 —— 否则右边缘上的那一行会把菜单开到屏幕外。 */
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useEffect(() => {
    const got = window.teamAgent?.shareTargets;
    if (!got) { setTargets([]); return; }
    let live = true;
    void got().then((rows) => { if (live) setTargets(rows); }).catch(() => { if (live) setTargets([]); });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el || pos) return;
    const r = el.getBoundingClientRect();
    const pad = 8;
    setPos({
      left: Math.max(pad, Math.min(at.x, innerWidth - r.width - pad)),
      top: Math.max(pad, Math.min(at.y, innerHeight - r.height - pad)),
    });
  }, [at, pos, ref, targets]);

  const abs = workspace ? `${workspace}/${file.path}` : "";
  const done = (m: string) => { notify(m); onClose(); };
  const failed = (why: string | undefined, what: string) =>
    notify(t("{what} did not work: {why}", { what, why: why || t("the system refused it") }));

  /** Each shell action: absent in a browser → the row is not rendered at all. */
  const shellAction = (
    fn: ((p: string) => Promise<{ ok: boolean; why?: string }>) | null | undefined,
    label: string, icon: ReactNode, ok: string, act: string,
  ) => {
    if (!fn || !abs) return null;
    return (
      <button key={label} className="om-item" role="menuitem" data-act={act}
        onClick={() => void fn(abs).then((r) => (r.ok ? done(ok) : failed(r.why, label)))}>
        {icon}<span>{label}</span>
      </button>
    );
  };

  return (
    <div className="om" ref={ref} role="menu" aria-label={t("Actions for {name}", { name: file.name })}
      style={pos ? { left: pos.left, top: pos.top } : { left: -9999, top: 0 }}
      onContextMenu={(e) => e.preventDefault()}>
      <div className="om-head" title={file.path}>{file.name}</div>
      <button className="om-item" role="menuitem" data-act="view" onClick={() => { onView(); onClose(); }}>
        <Eye size={14} aria-hidden /><span>{t("Look at it here")}</span>
      </button>
      {shellAction(window.teamAgent?.openFile, t("Open with its own program"), <FolderOpen size={14} aria-hidden />,
        t("Opened it with the program this kind of file belongs to"), "open")}
      {shellAction(window.teamAgent?.reveal, t("Show it in the Finder"), <FileSearch size={14} aria-hidden />,
        t("Shown in the Finder"), "reveal")}
      {(() => {
        const copy = window.teamAgent?.copyFile;
        if (!copy || !abs) return null;
        return (
          <button className="om-item" role="menuitem" data-act="copy"
            onClick={() => void copy(abs).then((r) => {
              if (!r.ok) return failed(r.why, t("Copy"));
              // ⚠️ 说清复制的是**哪一个**:不是图片时复制的是路径,而「已复制」两个字会让用户
              // 以为粘出来是一张图。
              done(r.how === "image"
                ? t("The picture is on the clipboard — paste it into WeChat or anywhere")
                : t("The file's path is on the clipboard"));
            })}>
            <Copy size={14} aria-hidden /><span>{t("Copy the file")}</span>
          </button>
        );
      })()}
      {(() => {
        const put = window.teamAgent?.copyText;
        if (!put) return null;
        return (
          <button className="om-item" role="menuitem" data-act="copy-path"
            onClick={() => void put(abs).then((r) => (r.ok ? done(t("The file's path is on the clipboard"))
                                                          : failed(r.why, t("Copy the path"))))}>
            <Link2 size={14} aria-hidden /><span>{t("Copy the path")}</span>
          </button>
        );
      })()}

      {targets === null ? null : targets.length > 0 ? (
        <>
          <div className="om-sep" role="separator" />
          <div className="om-group"><Share2 size={12} aria-hidden />{t("Open with…")}</div>
          {/* ⚠️ 分享目标在**主菜单里平铺**,不做二级弹出:这一栏只有 300 多像素宽,二级菜单会伸到
              窗口外面去,而它们通常只有两三个。 */}
          {targets.map((tgt) => (
            <button key={tgt.id} className="om-item" role="menuitem" data-act={"share:" + tgt.id}
              onClick={() => {
                const go = window.teamAgent?.openWith;
                if (!go) return;
                const label = t(SHARE_LABEL[tgt.id] || tgt.id);
                void go(tgt.app, abs).then((r) => (r.ok ? done(t("Handed it to {app}", { app: label }))
                                                        : failed(r.why, label)));
              }}>
              {(() => { const I = SHARE_ICON[tgt.id] || Share2; return <I size={14} aria-hidden />; })()}
              <span>{t("Open with {app}", { app: t(SHARE_LABEL[tgt.id] || tgt.id) })}</span>
            </button>
          ))}
        </>
      ) : null}
    </div>
  );
}
