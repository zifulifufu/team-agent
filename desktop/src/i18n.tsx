import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { prefs } from "./theme";

/**
 * 界面多语言。
 *
 * 约定(很重要,加文案时照这个来):
 *   * **源码里直接写英文**,英文就是 key;中文放到下面的 `ZH` 表里。
 *     这样只有一份"原文",不会出现 key 和文案各写一遍、两边慢慢不一致的问题。
 *   * 需要插值就用 `{name}`,例如 `t("Delete the group chat \"{name}\"?", { name })`。
 *   * 组件里用 `useI18n().t`;非组件代码(如 api.ts / lib.ts)用模块级的 `tr`(它按当前语言返回)。
 *   * 英文缺中文时原样显示英文 —— 不会显示成 key 或空白,便于逐批补翻译。
 *
 * 默认语言是**英文**(面向国际传播);用户在「设置 → 外观 → 语言」改过之后按用户的来。
 */

export type Lang = "en" | "zh";
export const LANGS: { id: Lang; label: string }[] = [
  { id: "en", label: "English" },
  { id: "zh", label: "中文(简体)" },
];
export const DEFAULT_LANG: Lang = "en";
const PREF_KEY = "ta.lang";

/** 中文词典:键 = 源码里的英文原文。 */
const ZH: Record<string, string> = {
  // ---------------------------------------------------------------- 侧边栏
  "Collapse sidebar": "收起侧边栏",
  "Search group chats": "搜索群聊",
  "New group chat": "新建群聊",
  "Search group chats…": "搜索群聊…",
  "Clear": "清除",
  "Members": "成员",
  "Collapse members": "收起群成员",
  "Expand members": "展开群成员",
  "Open a group chat and its members show up here — you can add more at any time":
    "打开一个群聊后,这里显示群成员,可随时添加",
  "Tools": "工具",
  "Group chats": "群聊",
  "Delete group chat": "删除群聊",
  "Delete group chat {name}": "删除群聊 {name}",
  "No matching group chats": "没有匹配的群聊",
  "No group chats yet": "还没有群聊",
  "Settings": "设置",
  "Appearance": "外观",
  "Offline mode": "离线模式",
  "Updates & discovery": "更新与发现",
  "About": "关于",
  "Me": "我",
  "Local user": "本地用户",
  "Backend not connected": "后端未连接",
  "Hosted + local": "云端 + 本地",
  "Updates available": "有可用更新",
  'Delete the group chat "{name}" and all of its messages?': "删除群聊「{name}」及其全部聊天记录?",
  "Delete": "删除",

  // ---------------------------------------------------------------- 工具入口(侧边栏与设置共用)
  "Skills": "技能",
  "Plugins": "插件",
  "Prompts": "提示词",
  "Library": "资料库",
  "Memory": "记忆",

  // ---------------------------------------------------------------- 设置导航
  "Models": "模型",
  "App": "应用",
  "Providers": "模型服务",
  "Routing & fallback": "路由与回退",
  "Local models": "本地模型",
  "External agents": "外部智能体",
  "Template gallery": "模板中心",
  "General": "通用",
  "Permissions & control": "权限与操控",
  "Data": "数据",
  "Usage stats": "使用统计",
  "Dependencies": "依赖",
  "Back": "返回",

  // ---------------------------------------------------------------- 外观页
  "Theme": "主题",
  "Light": "浅色",
  "Dark": "深色",
  "Follow system": "跟随系统",
  "Accent colour": "主题色",
  "Custom accent colour (hex)": "自定义主题色(十六进制)",
  "Reset": "重置",
  "Enter a 6-digit hex colour, e.g. #00B96B": "请输入 6 位十六进制颜色,如 #00B96B",
  "The accent colour is used for switches, selected states and highlights. Stored on this machine.":
    "主题色用于开关、选中状态和强调元素。设置保存在本机。",
  "Language": "语言",
  "Interface language. Built-in templates and role presets follow the same setting.":
    "界面语言。自带的模板与岗位预设也跟随这个设置。",
  // ---------------------------------------------------------------- 通用
  "Offline mode · local models only": "离线模式 · 仅本地模型",
  "No model available": "无可用模型",

  // ---------------------------------------------------------------- 首页
  "Pull hosted and local models into one group and let each do what it is best at — office documents, video production, writing.":
    "把国内外大模型拉进同一个群,各展所长,协同完成办公、视频制作与创作。",
  "Describe your task; type @ to assign members, or leave it and 小助 will coordinate":
    "描述你的任务;输入 @ 点名成员分工,不点名则由小助统筹",
  "Which group chat to send to": "发送到哪个群聊",
  "Send to": "发送到",
  "New group chat · {names}": "新建群聊 · {names}",
  'Send to "{name}"': "发送到「{name}」",
  "Group templates": "群聊模板",
  "Members, host, skills and prompt installed in one click": "一键建好成员、群主、技能和提示词",
  "More teams: template gallery →": "更多团队:模板中心 →",
  'Create a group chat from template "{name}"': "用模板「{name}」新建群聊",
  "{name} (host)": "{name}(群主)",
  "No members yet — create some under Members in the sidebar first": "还没有成员,请先到左侧「成员」里创建",
  "Everyone": "所有人",
  "Everyone speaks in turn": "全员依次发言",
  "Message input": "消息输入框",
  "@-mention a member": "@ 点名成员",
  "Click to allow / block hosted model calls": "点击切换:允许 / 禁止调用云端模型",
  "Stop": "停止",
  "Send": "发送",
};

let current: Lang = DEFAULT_LANG;

function interpolate(s: string, vars?: Record<string, string | number>): string {
  if (!vars) return s;
  for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v));
  return s;
}

function translate(lang: Lang, key: string, vars?: Record<string, string | number>): string {
  return interpolate(lang === "zh" ? ZH[key] ?? key : key, vars);
}

/** 非组件代码(api.ts / lib.ts / 事件回调)用的翻译函数:按当前语言返回。 */
export const tr = (key: string, vars?: Record<string, string | number>) => translate(current, key, vars);

/**
 * 双语内容取值:源码里同时写着英文和中文的「内容」用这个(界面文案不要用它,用 t())。
 * 中文缺失时回退英文,所以可以先把英文补上、中文随后补。
 */
export function pickLang(en: string, zh: string | undefined, lang: Lang): string {
  return lang === "zh" ? zh || en : en;
}

interface I18nCtx {
  lang: Lang;
  setLang: (l: Lang) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
  /** 双语内容取值(见 pickLang)。 */
  pick: (en: string, zh?: string) => string;
}
const Ctx = createContext<I18nCtx | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => {
    const v = prefs.read(PREF_KEY);
    return v === "zh" || v === "en" ? v : DEFAULT_LANG;
  });
  current = lang;

  const setLang = useCallback((l: Lang) => {
    current = l;
    setLangState(l);
    prefs.write(PREF_KEY, l);
  }, []);

  // 让 <html lang> 跟着变(CSS、系统字体回退、辅助技术都看它)
  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  const t = useCallback(
    (key: string, vars?: Record<string, string | number>) => translate(lang, key, vars),
    [lang],
  );

  const pick = useCallback((en: string, zh?: string) => pickLang(en, zh, lang), [lang]);

  const value = useMemo(() => ({ lang, setLang, t, pick }), [lang, setLang, t, pick]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n(): I18nCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useI18n 必须在 I18nProvider 里使用");
  return c;
}
