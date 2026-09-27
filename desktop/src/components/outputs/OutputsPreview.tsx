import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { LoaderCircle } from "lucide-react";
import { api, type WorkspaceFile } from "../../api";
import { useI18n } from "../../i18n";

/**
 * 一个产物的**内容**,就显示在右边这一栏里。
 *
 * 为什么要有它:成果栏原来只是一张清单 —— 点一行,文件被交给系统里另一个程序打开,答案跑到别处
 * 去了。用户要的是「像 WorkBuddy 那样,结果就在中间框的右侧栏里」:点了就在这里看。
 *
 * ⚠️ **能不能显示由后端说**,不是我在这里按扩展名猜:
 *   - 图片/视频/音频 → `workspaceMediaBytes`(带 `inline=1`,由字节定类型)
 *   - 文档类 → `workspaceText`,后端用全应用同一套判据(`PLAIN_EXT` + `extract_text`)
 *     决定它有没有文字,没有就 415 —— 这里只负责把拿到的文字画出来。
 *   所以「什么算文本」这件事全应用仍然只有一份判据,界面不会和知识库各说各话。
 *
 * ⚠️ **markdown 之外一律走 `<pre>`**,markdown 走 `ReactMarkdown`(它默认不渲染原始 HTML)。
 * 工作目录里的文件是成员写的,包括用户自己上传的东西 —— **永远不让它变成可执行的页面**。
 */
export default function OutputsPreview({ gid, file, onOpenExternally }: {
  gid: string;
  file: WorkspaceFile;
  /** 交给系统里的程序打开。显示不了的时候,这是唯一能干活的动作,所以它必须真的在。 */
  onOpenExternally: () => void;
}) {
  const { t } = useI18n();
  const [got, setGot] = useState<Loaded | null>(null);
  const [why, setWhy] = useState("");

  useEffect(() => {
    let live = true;
    let url = "";
    setGot(null);
    setWhy("");
    (async () => {
      try {
        if (file.kind === "image" || file.kind === "video" || file.kind === "audio") {
          const blob = await api.workspaceMediaBytes(gid, file.path);
          if (!live) return;
          url = URL.createObjectURL(blob);
          setGot({ how: "media", url, kind: file.kind as MediaKind });
          return;
        }
        if (file.kind === "document") {
          const r = await api.workspaceText(gid, file.path);
          if (!live) return;
          setGot({ how: "text", text: r.text, truncated: r.truncated, extracted: r.extracted,
                   md: /\.(md|markdown)$/i.test(file.name) });
          return;
        }
        if (live) setGot({ how: "none" });
      } catch (e) {
        // 后端说得比我准(415 = 里面没有文字;413 = 太大)。它的话要原样说出来,不要换一句
        // 「预览失败」—— 用户据此才知道是文件不对还是别的问题。
        if (live) { setGot({ how: "none" }); setWhy((e as Error).message); }
      }
    })();
    // ⚠️ 一定要回收:每次点一行就建一个 blob URL,不撤销的话看十几个文件就把整轮的产物都钉在
    // 内存里(mp4 动辄几十上百 MB)。
    return () => { live = false; if (url) URL.revokeObjectURL(url); };
  }, [gid, file.path, file.kind, file.name]);

  const cant = () => (
    <div className="op-note">
      <div>{why || t("This file cannot be shown here.")}</div>
      <button className="btn small" onClick={onOpenExternally}>{t("Open with another program")}</button>
    </div>
  );

  if (!got) {
    return <div className="op-note"><LoaderCircle size={14} className="spin" aria-hidden /> {t("Loading…")}</div>;
  }
  if (got.how === "none") return cant();
  if (got.how === "media") {
    if (got.kind === "image") return <img className="op-media" src={got.url} alt={file.name} />;
    if (got.kind === "video") return <video className="op-media" src={got.url} controls playsInline />;
    return <audio className="op-audio" src={got.url} controls />;
  }
  return (
    <div className="op-text">
      {/* 「抽出来的」和「文件本身」是两件事,说出来 —— 一份 docx 显示成散文,用户有权知道那句话
          不是文件的原貌。 */}
      {(got.extracted || got.truncated) && (
        <div className="op-flag">
          {got.extracted && t("This is the text read out of the document, not the file itself.")}
          {got.extracted && got.truncated && " · "}
          {got.truncated && t("Only the beginning is shown.")}
        </div>
      )}
      {got.md
        ? <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{got.text}</ReactMarkdown></div>
        : <pre className="op-pre">{got.text}</pre>}
    </div>
  );
}

type MediaKind = "image" | "video" | "audio";
type Loaded =
  | { how: "media"; url: string; kind: MediaKind }
  | { how: "text"; text: string; truncated: boolean; extracted: boolean; md: boolean }
  | { how: "none" };
