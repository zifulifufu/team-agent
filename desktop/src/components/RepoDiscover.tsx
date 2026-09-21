import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowLeft, Archive, ExternalLink, FileText, Search, Star } from "lucide-react";
import { api, ApiError, type FilePreview, type RepoFile, type RepoHit, type UpdatesInfo } from "../api";
import { useI18n } from "../i18n";
import { Modal, useConfirm } from "../ui";
import { agoIso, Callout, dupMessage, ExtLink, fmtBytes, fmtStars, githubUrl, GithubMark, isHttps, Spin } from "./ExtBits";
import PluginInstallModal from "./PluginInstall";
import "../styles/ext.css";

type DiscoverKind = "skill" | "plugin" | "mcp";

/**
 * Both languages live here: `title`/`hint` are the English wording and `…Zh` the Chinese.
 * `RepoDiscoverModal` picks one with the context's `pick` — a module-level `t()` would run before
 * provider is mounted, so it cannot be used at this level.
 */
const KIND_TEXT: Record<
  DiscoverKind,
  { title: string; titleZh: string; noun: string; nounZh: string; topic: string; hint: string; hintZh: string }
> = {
  skill: {
    title: "Discover skills on GitHub",
    titleZh: "从 GitHub 发现技能",
    noun: "skill",
    nounZh: "技能",
    topic: "agent-skills / claude-skills",
    hint: "A skill is a plain-text prompt (SKILL.md). Installing it adds text and nothing else — no code runs.",
    hintZh: "技能是纯文本提示词(SKILL.md),安装后只是一段文字,不会执行任何代码。",
  },
  plugin: {
    title: "Discover plugins on GitHub",
    titleZh: "从 GitHub 发现插件",
    noun: "plugin",
    nounZh: "插件",
    topic: "team-agent-plugin",
    hint: "A plugin is Python code that runs on this machine. This page only finds it and lets you read the source first; there is no one-click install.",
    hintZh: "插件是 Python 代码,会在本机运行。这里只负责找到它并让你先读源码,不会一键安装。",
  },
  mcp: {
    title: "Discover MCP servers on GitHub",
    titleZh: "从 GitHub 发现 MCP 服务器",
    noun: "MCP server",
    nounZh: "MCP 服务器",
    topic: "mcp-server / mcp-servers",
    hint: "Nothing is installed automatically here: read the repository README and type the start command into the Add MCP server form yourself, then save.",
    hintZh: "这里不会自动安装:按仓库 README 的说明,把启动命令填到「添加 MCP 服务器」表单里,确认后保存。",
  },
};
/** Rendering a GitHub README: only https links are allowed through and remote images are never loaded (the content is untrusted). */
function ReadmeView({ text }: { text: string }) {
  const { t } = useI18n();
  return (
    <div className="md ext-readme">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) =>
            isHttps(href) ? <a href={href} target="_blank" rel="noreferrer">{children}</a> : <span>{children}</span>,
          img: ({ alt }) => <span className="muted">{t("[image{alt}]", { alt: alt ? `:${alt}` : "" })}</span>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function RepoCard({ hit, onOpen }: { hit: RepoHit; onOpen: () => void }) {
  const { t } = useI18n();
  return (
    <div className="ext-repo">
      <div className="ext-repo-main">
        <div className="ext-repo-name">
          <GithubMark size={13} /> <b>{hit.repo}</b>
          {hit.archived && <span className="tag warn"><Archive size={11} /> {t("Archived")}</span>}
          {hit.license ? <span className="tag">{hit.license}</span> : <span className="tag warn" title={t("No licence declared")}>{t("No licence")}</span>}
        </div>
        {hit.description && <div className="ext-repo-desc">{hit.description}</div>}
        <div className="ext-repo-meta muted small">
          <span title={t("GitHub stars")}><Star size={11} /> {fmtStars(hit.stars)}</span>
          {hit.updated_at && <span>{t("Updated")} {agoIso(hit.updated_at) || hit.updated_at}</span>}
        </div>
      </div>
      <button className="btn small" onClick={onOpen}>{t("View")}</button>
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
  const { t, pick } = useI18n();
  const raw = KIND_TEXT[kind];
  const T = {
    ...raw,
    title: pick(raw.title, raw.titleZh),
    noun: pick(raw.noun, raw.nounZh),
    hint: pick(raw.hint, raw.hintZh),
  };
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
            <Callout tone="warn" title={t("GitHub content is untrusted — read it before you install")}>
              {T.hint}{t("Star counts, update times and licences are only hints, not a safety guarantee.")}
            </Callout>
            <form className="ext-search" onSubmit={(e) => { e.preventDefault(); void search(q); }}>
              <div className="search-box grow">
                <Search size={14} />
                <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("Search {noun} repositories (leave it blank to list topic {topic} by stars)", { noun: T.noun, topic: T.topic })} aria-label={t("Search {noun} repositories", { noun: T.noun })} />
              </div>
              <button className="btn primary" type="submit" disabled={searching}>{searching ? <><Spin /> {t("Searching")}</> : t("Search")}</button>
            </form>

            {curated.length > 0 && (
              <>
                <div className="ext-sub">{t("Curated sources")}</div>
                {curated.map((c) => (
                  <div key={c.repo} className="ext-repo">
                    <div className="ext-repo-main">
                      <div className="ext-repo-name"><GithubMark size={13} /> <b>{c.repo}</b></div>
                      <div className="ext-repo-desc">{c.desc}</div>
                    </div>
                    <button className="btn small" onClick={() => setRepo({ repo: c.repo, description: c.desc })}>{t("Open")}</button>
                  </div>
                ))}
              </>
            )}

            <div className="ext-sub">{t("Search results")}{hits ? <span className="count-badge-plain">{hits.length}</span> : null}</div>
            {searching && !hits && <div className="empty"><Spin /> {t("Searching GitHub…")}</div>}
            {err && (
              <div className="ext-errbox">
                <div className="err">{t("Search failed:")} {err}</div>
                <button className="btn small" onClick={() => void search(q)}>{t("Retry")}</button>
              </div>
            )}
            {hits && hits.length === 0 && <div className="empty">{t("No matching repositories — try another keyword.")}</div>}
            {hits?.map((h) => (
              <RepoCard key={h.repo} hit={h} onOpen={() => setRepo({ repo: h.repo, description: h.description })} />
            ))}
          </>
        )}
      </div>
    </Modal>
  );
}

// -------------------------------------------------------------- selected repository
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
  const { t } = useI18n();
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
        <button className="btn small ghost" onClick={onBack}><ArrowLeft size={14} /> {t("Back to search")}</button>
        <div className="grow">
          <b>{repo}</b>
          {description && <div className="muted small">{description}</div>}
        </div>
        <a className="btn small" href={githubUrl(repo)} target="_blank" rel="noreferrer"><ExternalLink size={13} /> {t("Open on GitHub")}</a>
      </div>

      {loading && <div className="empty"><Spin /> {t("Reading the repository…")}</div>}
      {err && (
        <div className="ext-errbox">
          <div className="err">{err}</div>
          <button className="btn small" onClick={() => setTick((t) => t + 1)}>{t("Retry")}</button>
        </div>
      )}

      {kind === "skill" && files && (
        <>
          <div className="ext-sub">{t("Skill files in this repository")}{files.length ? <span className="count-badge-plain">{files.length}</span> : null}</div>
          {files.length === 0 && <div className="empty">{t("No SKILL.md files found.")}</div>}
          <div className="card flush">
            {files.map((f) => (
              <div key={f.path} className="model-row">
                <FileText size={15} className="muted" />
                <div className="mr-main">
                  <div className="mr-name">{f.name || f.path}</div>
                  <div className="mr-id">{f.path}</div>
                </div>
                <button className="btn small" onClick={() => setPick(f)}>{t("Preview")}</button>
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
          <div className="ext-sub">{t(".py files in this repository")}{files.length ? <span className="count-badge-plain">{files.length}</span> : null}</div>
          <p className="muted small" style={{ margin: "0 0 8px" }}>{t("Only .py files in the repository root and in plugins/ are listed. Every file has to be previewed in full before it can be installed.")}</p>
          {files.length === 0 && <div className="empty">{t("No installable .py files found.")}</div>}
          <div className="card flush">
            {files.map((f) => (
              <div key={f.path} className="model-row">
                <FileText size={15} className="muted" />
                <div className="mr-main">
                  <div className="mr-name">{f.path}</div>
                  <div className="mr-id">{f.size != null ? fmtBytes(f.size) : ""}</div>
                </div>
                <button className="btn small" onClick={() => setPick(f)}>{t("Preview source")}</button>
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
          <Callout title={t("Nothing is installed automatically here")}>
            {t("An MCP server is a command that runs on this machine. Read the README below and put the start command and its arguments into the Add MCP server form yourself; save only once you are sure.")}
          </Callout>
          <div className="row" style={{ margin: "10px 0" }}>
            <button
              className="btn primary"
              onClick={() => { onPrefillMcp?.(repo.split("/")[1] ?? repo, description); onClose(); }}
              disabled={!onPrefillMcp}
            >
              {t("Prefill the form with this repository name")}
            </button>
            <span className="muted small">{t("Only the name and description are prefilled; you fill in the command yourself.")}</span>
          </div>
          <ReadmeView text={readme.content || t("(this repository has no README)")} />
        </>
      )}
    </>
  );
}

// -------------------------------------------------------------- skill preview and install
function SkillPreviewModal({
  repo, path, gitRef = "", onClose, onInstalled,
}: {
  repo: string;
  path: string;
  gitRef?: string;
  onClose: () => void;
  onInstalled: () => void;
}) {
  const { t } = useI18n();
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
      // The hash pins what was previewed: the server re-downloads and refuses if it changed.
      await api.installSkill(repo, path, gitRef, overwrite, pv?.sha256 ?? "");
      onInstalled();
    } catch (e) {
      const msg = (e as Error).message;
      // 409 is the duplicate-name conflict; matching on the message text would break the
      // moment the interface language changes (the server words it per request).
      if (e instanceof ApiError && e.status === 409 && !overwrite) {
        setBusy(false);
        if (await confirm(t("{msg}. Overwriting replaces the content of the local skill with the same name, and anything you changed locally is lost. Overwrite it?", { msg: dupMessage(msg) }), { okText: t("Overwrite") })) return install(true);
      } else {
        setErr(msg);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={t("Preview skill")}
      onClose={onClose}
      wide
      actions={
        <>
          <button className="btn" onClick={onClose}>{t("Cancel")}</button>
          <button className="btn primary" disabled={!pv || busy} onClick={() => void install(false)}>{busy ? <><Spin /> {t("Installing…")}</> : t("Install")}</button>
        </>
      }
    >
      <div className="ext-xl">
        <Callout title={t("A skill is a plain-text prompt; it does not run code")}>
          {t("Installing only adds text, and that text then goes into the prompt of whichever member or group uses it. The content comes from GitHub and is untrusted, so read it first: a malicious prompt can still talk a model into doing something you did not ask for.")}
        </Callout>
        <div className="ext-meta">
          <ExtLink href={githubUrl(repo)}>{repo}</ExtLink>
          <span className="mono">/ {path}</span>
        </div>
        {loading && <div className="empty"><Spin /> {t("Downloading…")}</div>}
        {loadErr && (
          <div className="ext-errbox">
            <div className="err">{loadErr}</div>
            <button className="btn small" onClick={() => setTick((t) => t + 1)}>{t("Retry")}</button>
          </div>
        )}
        {pv && (
          <>
            <div className="ext-meta"><span className="muted small">{t("Size {size} · {n} characters", { size: fmtBytes(pv.size), n: pv.content.length })}</span></div>
            <pre className="ext-src ext-prose" tabIndex={0} aria-label={t("Skill text")}>{pv.content}</pre>
          </>
        )}
        {err && <div className="err" style={{ marginTop: 8 }}>{err}</div>}
      </div>
    </Modal>
  );
}
