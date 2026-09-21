import type { ReactNode } from "react";
import { AlertTriangle, Info, Loader2 } from "lucide-react";
import { relTime } from "../api";

/** GitHub 的 Octocat 标志(lucide 已不再收录品牌图标,这里内联一个)。 */
export function GithubMark({ size = 13 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" style={{ flex: "none" }}>
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}

export function Spin({ size = 14 }: { size?: number }) {
  return <Loader2 size={size} className="ext-spin" aria-hidden="true" />;
}

/** 只有 https 链接才会被当作可点击的链接;其它一律当文字显示。 */
export const isHttps = (u: unknown): u is string => typeof u === "string" && /^https:\/\/[^\s]+$/i.test(u);

export function ExtLink({ href, children, className }: { href: unknown; children: ReactNode; className?: string }) {
  if (!isHttps(href)) return <span className={className}>{children}</span>;
  return (
    <a href={href} target="_blank" rel="noreferrer" className={"link " + (className ?? "")}>
      {children}
    </a>
  );
}

export function githubUrl(repo: string): string {
  return `https://github.com/${repo}`;
}

/** 带 GitHub 图标的来源徽标。 */
export function SourceBadge({ repo, path }: { repo: string; path?: string }) {
  return (
    <a className="tag ext-gh" href={githubUrl(repo)} target="_blank" rel="noreferrer" title={path ? `${repo} / ${path}` : repo}>
      <GithubMark size={11} /> {repo}
    </a>
  );
}

export function Callout({ tone = "info", title, children }: { tone?: "info" | "warn"; title?: string; children: ReactNode }) {
  return (
    <div className={"ext-callout " + tone} role={tone === "warn" ? "alert" : undefined}>
      {tone === "warn" ? <AlertTriangle size={17} /> : <Info size={17} />}
      <div>
        {title && <div className="ext-callout-title">{title}</div>}
        <div>{children}</div>
      </div>
    </div>
  );
}

/** GitHub 返回的 ISO 时间 → 「3 天前」。 */
export function agoIso(iso: string): string {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? relTime(t / 1000) : "";
}

export function fmtStars(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** 后端 409 的原文里带有「勾选『覆盖』才会替换」之类给表单用的提示,放进确认框里要去掉。 */
export function dupMessage(msg: string): string {
  return msg.replace(/[,,。]?\s*勾选「覆盖」才会替换/, "").replace(/[。.]+$/, "");
}
