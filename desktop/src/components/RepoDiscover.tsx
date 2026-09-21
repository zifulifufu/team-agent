import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowLeft, Archive, ExternalLink, FileText, Search, Star } from "lucide-react";
import { api, type FilePreview, type RepoFile, type RepoHit, type UpdatesInfo } from "../api";
import { Modal, useConfirm } from "../ui";
import { agoIso, Callout, dupMessage, ExtLink, fmtBytes, fmtStars, githubUrl, GithubMark, isHttps, Spin } from "./ExtBits";
import PluginInstallModal from "./PluginInstall";
import "../styles/ext.css";

type DiscoverKind = "skill" | "plugin" | "mcp";

const KIND_TEXT: Record<DiscoverKind, { title: string; noun: string; topic: string; hint: string }> = {
  skill: {
    title: "从 GitHub 发现技能",
    noun: "技能",
    topic: "agent-skills / claude-skills",
    hint: "技能是纯文本提示词(SKILL.md),安装后只是一段文字,不会执行任何代码。",
  },
  plugin: {
    title: "从 GitHub 发现插件",
    noun: "插件",
    topic: "team-agent-plugin",
    hint: "插件是 Python 代码,会在本机运行。这里只负责找到它并让你先读源码,不会一键安装。",
  },
  mcp: {
    title: "从 GitHub 发现 MCP 服务器",
    noun: "MCP 服务器",
    topic: "mcp-server / mcp-servers",
    hint: "这里不会自动安装:按仓库 README 的说明,把启动命令填到「添加 MCP 服务器」表单里,确认后保存。",
  },
};

/** 在渲染 GitHub README 时:只放行 https 链接、不加载远程图片(内容不可信)。 */
function ReadmeView({ text }: { text: string }) {
  return (
    <div className="md ext-readme">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) =>
            isHttps(href) ? <a href={href} target="_blank" rel="noreferrer">{children}</a> : <span>{children}</span>,
          img: ({ alt }) => <span className="muted">[图片{alt ? `:${alt}` : ""}]</span>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function RepoCard({ hit, onOpen }: { hit: RepoHit; onOpen: () => void }) {
  return (
    <div className="ext-repo">
      <div className="ext-repo-main">
        <div className="ext-repo-name">
          <GithubMark size={13} /> <b>{hit.repo}</b>
          {hit.archived && <span className="tag warn"><Archive size={11} /> 已归档</span>}
          {hit.license ? <span className="tag">{hit.license}</span> : <span className="tag warn" title="没有声明许可证">无许可证</span>}
        </div>
        {hit.description && <div className="ext-repo-desc">{hit.description}</div>}
        <div className="ext-repo-meta muted small">
          <span title="GitHub 星数"><Star size={11} /> {fmtStars(hit.stars)}</span>
          {hit.updated_at && <span>最近更新 {agoIso(hit.updated_at) || hit.updated_at}</span>}
        </div>
      </div>
      <button className="btn small" onClick={onOpen}>查看</button>
    </div>
  );
}

export function RepoDiscoverModal({
  kind,
  onClose,
  onInstalled,
  onPrefillMcp,
}: {
  kind: DiscoverKind;
  onClose: () => void;
  onInstalled?: () => void;
  onPrefillMcp?: (name: string, description: string) => void;
}) {
  const T = KIND_TEXT[kind];
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<RepoHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [err, setErr] = useState("");
  const [curated, setCurated] = useState<UpdatesInfo["curated"]>([]);
  const [repo, setRepo] = useState<{ repo: string; description: string } | null>(null);
  const seq = useRef(0);

  const search = useCallback(async (query: string) => {
    const my = ++seq.current;
    setSearching(true);
    setErr("");
    try {
      const r = await api.searchRepos(kind, query.trim());
      if (my === seq.current) setHits(r);
    } catch (e) {
      if (my === seq.current) {
        setHits(null);
        setErr((e as Error).message);
      }
    } finally {
      if (my === seq.current) setSearching(false);
    }
  }, [kind]);

  useEffect(() => {
    api.updates().then((u) => setCurated(u.curated.filter((c) => c.kind === kind))).catch(() => undefined);
    void search("");
  }, [kind, search]);

  return (
    <Modal title={T.title} onClose={onClose} wide>
      <div className="ext-xl">
        {repo ? (
          <RepoView kind={kind} repo={repo.repo} description={repo.description} onBack={() => setRepo(null)} onInstalled={onInstalled} onPrefillMcp={onPrefillMcp} onClose={onClose} />
        ) : (
          <>
            <Callout tone="warn" title="来自 GitHub 的内容不可信,先看一眼再装">
              {T.hint}星数、更新时间和许可证只是参考,不代表安全。
            </Callout>
            <form className="ext-search" onSubmit={(e) => { e.preventDefault(); void search(q); }}>
              <div className="search-box grow">
                <Search size={14} />
                <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={`搜索${T.noun}仓库(可留空,按星数列出话题 ${T.topic})`} aria-label={`搜索${T.noun}仓库`} />
              </div>
              <button className="btn primary" type="submit" disabled={searching}>{searching ? <><Spin /> 搜索中</> : "搜索"}</button>
            </form>

            {curated.length > 0 && (
              <>
                <div className="ext-sub">推荐来源</div>
                {curated.map((c) => (
                  <div key={c.repo} className="ext-repo">
                    <div className="ext-repo-main">
                      <div className="ext-repo-name"><GithubMark size={13} /> <b>{c.repo}</b></div>
                      <div className="ext-repo-desc">{c.desc}</div>
                    </div>
                    <button className="btn small" onClick={() => setRepo({ repo: c.repo, description: c.desc })}>打开</button>
                  </div>
                ))}
              </>
            )}

            <div className="ext-sub">搜索结果{hits ? <span className="count-badge-plain">{hits.length}</span> : null}</div>
            {searching && !hits && <div className="empty"><Spin /> 正在向 GitHub 搜索…</div>}
            {err && (
              <div className="ext-errbox">
                <div className="err">搜索失败:{err}</div>
                <button className="btn small" onClick={() => void search(q)}>重试</button>
              </div>
            )}
            {hits && hits.length === 0 && <div className="empty">没有找到匹配的仓库,换个关键词试试。</div>}
            {hits?.map((h) => (
              <RepoCard key={h.repo} hit={h} onOpen={() => setRepo({ repo: h.repo, description: h.description })} />
            ))}
          </>
        )}
      </div>
    </Modal>
  );
}

// ------------------------------------------------------------- 选中的仓库
function RepoView({
  kind, repo, description, onBack, onInstalled, onPrefillMcp, onClose,
}: {
  kind: DiscoverKind;
  repo: string;
  description: string;
  onBack: () => void;
  onInstalled?: () => void;
  onPrefillMcp?: (name: string, description: string) => void;
  onClose: () => void;
}) {
  const [files, setFiles] = useState<RepoFile[] | null>(null);
  const [readme, setReadme] = useState<{ content: string; url: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [pick, setPick] = useState<RepoFile | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setErr("");
    const job =
      kind === "skill" ? api.repoSkills(repo).then((f) => alive && setFiles(f))
      : kind === "plugin" ? api.repoPlugins(repo).then((f) => alive && setFiles(f))
      : api.repoReadme(repo).then((r) => alive && setReadme(r));
    job.catch((e) => alive && setErr((e as Error).message)).finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [kind, repo, tick]);

  return (
    <>
      <div className="ext-repo-head">
        <button className="btn small ghost" onClick={onBack}><ArrowLeft size={14} /> 返回搜索</button>
        <div className="grow">
          <b>{repo}</b>
          {description && <div className="muted small">{description}</div>}
        </div>
        <a className="btn small" href={githubUrl(repo)} target="_blank" rel="noreferrer"><ExternalLink size={13} /> 在 GitHub 打开</a>
      </div>

      {loading && <div className="empty"><Spin /> 正在读取仓库…</div>}
      {err && (
        <div className="ext-errbox">
          <div className="err">{err}</div>
          <button className="btn small" onClick={() => setTick((t) => t + 1)}>重试</button>
        </div>
      )}

      {kind === "skill" && files && (
        <>
          <div className="ext-sub">仓库里的技能文件{files.length ? <span className="count-badge-plain">{files.length}</span> : null}</div>
          {files.length === 0 && <div className="empty">没有找到 SKILL.md 文件。</div>}
          <div className="card flush">
            {files.map((f) => (
              <div key={f.path} className="model-row">
                <FileText size={15} className="muted" />
                <div className="mr-main">
                  <div className="mr-name">{f.name || f.path}</div>
                  <div className="mr-id">{f.path}</div>
                </div>
                <button className="btn small" onClick={() => setPick(f)}>预览</button>
              </div>
            ))}
          </div>
          {pick && (
            <SkillPreviewModal
              repo={repo}
              path={pick.path}
              onClose={() => setPick(null)}
              onInstalled={() => { setPick(null); onInstalled?.(); }}
            />
          )}
        </>
      )}

      {kind === "plugin" && files && (
        <>
          <div className="ext-sub">仓库里的 .py 文件{files.length ? <span className="count-badge-plain">{files.length}</span> : null}</div>
          <p className="muted small" style={{ margin: "0 0 8px" }}>只列出根目录和 plugins/ 目录下的 .py 文件。每个文件都要先预览完整源码才能安装。</p>
          {files.length === 0 && <div className="empty">没有找到可安装的 .py 文件。</div>}
          <div className="card flush">
            {files.map((f) => (
              <div key={f.path} className="model-row">
                <FileText size={15} className="muted" />
                <div className="mr-main">
                  <div className="mr-name">{f.path}</div>
                  <div className="mr-id">{f.size != null ? fmtBytes(f.size) : ""}</div>
                </div>
                <button className="btn small" onClick={() => setPick(f)}>预览源码</button>
              </div>
            ))}
          </div>
          {pick && (
            <PluginInstallModal
              repo={repo}
              path={pick.path}
              onClose={() => setPick(null)}
              onInstalled={() => { setPick(null); onInstalled?.(); onClose(); }}
            />
          )}
        </>
      )}

      {kind === "mcp" && readme && (
        <>
          <Callout title="这里不会自动安装">
            MCP 服务器是要在本机运行的命令。请读下面的 README,按它的说明把「启动命令」和「参数」填到「添加 MCP 服务器」表单里,确认无误后再保存。
          </Callout>
          <div className="row" style={{ margin: "10px 0" }}>
            <button
              className="btn primary"
              onClick={() => { onPrefillMcp?.(repo.split("/")[1] ?? repo, description); onClose(); }}
              disabled={!onPrefillMcp}
            >
              用这个仓库名预填表单
            </button>
            <span className="muted small">只预填名称和说明,命令需要你自己填。</span>
          </div>
          <ReadmeView text={readme.content || "(这个仓库没有 README)"} />
        </>
      )}
    </>
  );
}

// ------------------------------------------------------------- 技能预览与安装
function SkillPreviewModal({
  repo, path, gitRef = "", onClose, onInstalled,
}: {
  repo: string;
  path: string;
  gitRef?: string;
  onClose: () => void;
  onInstalled: () => void;
}) {
  const confirm = useConfirm();
  const [pv, setPv] = useState<FilePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadErr, setLoadErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setLoadErr("");
    api.previewFile(repo, path, gitRef)
      .then((p) => alive && setPv(p))
      .catch((e) => alive && setLoadErr((e as Error).message))
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [repo, path, gitRef, tick]);

  const install = async (overwrite: boolean): Promise<void> => {
    setBusy(true);
    setErr("");
    try {
      await api.installSkill(repo, path, gitRef, overwrite);
      onInstalled();
    } catch (e) {
      const msg = (e as Error).message;
      if (/已有同名/.test(msg) && !overwrite) {
        setBusy(false);
        if (await confirm(`${dupMessage(msg)}。覆盖会替换本地同名技能的内容,你在本地做过的修改会丢失。要覆盖吗?`, { okText: "覆盖安装" })) return install(true);
      } else {
        setErr(msg);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="预览技能"
      onClose={onClose}
      wide
      actions={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={!pv || busy} onClick={() => void install(false)}>{busy ? <><Spin /> 安装中…</> : "安装"}</button>
        </>
      }
    >
      <div className="ext-xl">
        <Callout title="技能是纯文本提示词,不会执行代码">
          安装后它只是一段文字,会被加进用到它的成员或群的提示词里。内容来自 GitHub,不可信,请先读一遍:一段恶意的提示词也可能诱导模型做你不想要的事。
        </Callout>
        <div className="ext-meta">
          <ExtLink href={githubUrl(repo)}>{repo}</ExtLink>
          <span className="mono">/ {path}</span>
        </div>
        {loading && <div className="empty"><Spin /> 正在下载…</div>}
        {loadErr && (
          <div className="ext-errbox">
            <div className="err">{loadErr}</div>
            <button className="btn small" onClick={() => setTick((t) => t + 1)}>重试</button>
          </div>
        )}
        {pv && (
          <>
            <div className="ext-meta"><span className="muted small">大小 {fmtBytes(pv.size)} · {pv.content.length} 字</span></div>
            <pre className="ext-src ext-prose" tabIndex={0} aria-label="技能原文">{pv.content}</pre>
          </>
        )}
        {err && <div className="err" style={{ marginTop: 8 }}>{err}</div>}
      </div>
    </Modal>
  );
}
