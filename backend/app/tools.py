"""Skills(文本技能)与插件(Python 工具)。MCP 见 mcp_client.py,统一调度见 toolhub.py。

  * Skills —— 数据目录 skills/<名称>/SKILL.md。可以给成员勾选(注入该成员的提示词),
              也可以挂到整个群(scope: group 的「群聊规则」类技能,全员都遵守)。纯文本,对所有模型通用。
  * 插件   —— 数据目录 plugins/*.py,里面写 register(registry) 注册若干工具。
              插件是在本进程里运行的 Python 代码,没有沙箱:只装你读过、信得过的。
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import inspect
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import i18n

# ------------------------------------------------------------------- skills
EXAMPLE_SKILLS: dict[str, dict] = {
    # Built-in example skills. As with SEED_AGENTS the English text is the canonical
    # value — it is what gets written to skills/<name>/SKILL.md and what reaches the
    # model — and `<field>_zh` carries the Chinese wording. `localskill()` below swaps
    # in the right one for the request language, and SKILL_ALIASES lets either spelling
    # of a name resolve to the same entry, so a Chinese install whose skills were
    # seeded as 公文写作规范 and a fresh English one behave the same.
    "office-writing": {
        "name": "Office writing conventions", "name_zh": "公文写作规范",
        "description": "Structure and wording for office documents",
        "description_zh": "办公文档写作的结构与措辞要求", "scope": "member",
        "body": "When writing an office document (notice, report, proposal), follow:\n"
                "1. Open with one sentence giving the purpose and the conclusion;\n"
                "2. Break the body into points, one piece of information each;\n"
                "3. Every figure carries its definition and its date;\n"
                "4. Close with the next action, who owns it, and the deadline;\n"
                "5. Keep the tone formal and restrained — no colloquialisms, no hype.",
        "body_zh": "写办公文档(通知、汇报、方案)时遵循:\n"
                   "1. 开头一句话交代目的和结论;\n2. 正文分点,每点一个信息;\n"
                   "3. 数据要带口径和时间;\n4. 结尾写明下一步动作、负责人和时间节点;\n5. 语气正式、克制,不用口语和夸张修辞。",
    },
    "short-video-storyboard": {
        "name": "Short video storyboards", "name_zh": "短视频分镜规范",
        "description": "Output format for short-video scripts and storyboards",
        "description_zh": "短视频脚本与分镜的输出格式", "scope": "member",
        "body": "When producing a short-video script:\n"
                "1. State the one-line theme and the target audience first;\n"
                "2. The first 3 seconds must carry a hook;\n"
                "3. The storyboard table has: shot no. | picture | voice-over or dialogue | duration | camera | sound effects;\n"
                "4. Keep the total duration inside what the user asked for and show the total at the end.",
        "body_zh": "输出短视频脚本时:\n1. 先给一句话主题和目标受众;\n"
                   "2. 前 3 秒必须有钩子;\n3. 分镜表列:镜号 | 画面 | 旁白/台词 | 时长 | 镜头 | 音效;\n"
                   "4. 总时长控制在用户要求内,并在末尾给出总时长核算。",
    },
    "brainstorming": {
        "name": "Brainstorming rules", "name_zh": "头脑风暴规则",
        "description": "Group prompt: diverge first, converge second",
        "description_zh": "群聊提示词:先发散再收敛的讨论规则", "scope": "group",
        "body": "This group is in brainstorming mode:\n"
                "1. Diverge: everyone offers at least 2 distinct directions, no judging yet, and no repeating what has already been said;\n"
                "2. Converge: the facilitator leads the group through screening by feasibility / value / cost and narrows it to 1-2 directions;\n"
                "3. Every idea must say in one sentence who does what and why it works;\n"
                "4. The scribe closes with: the directions, the reasoning, and the next step.",
        "body_zh": "本群进入头脑风暴模式:\n"
                   "1. 发散阶段:每人至少提出 2 个不同方向,先不评判,不重复别人已经说过的点子;\n"
                   "2. 收敛阶段:主持人带大家按「可行性 / 价值 / 成本」筛出 1~2 个方向;\n"
                   "3. 每个点子一句话说清楚「谁、做什么、为什么有效」;\n4. 最后由记录员整理:方向、理由、下一步。",
    },
    "review-meeting": {
        "name": "Review meeting rules", "name_zh": "评审会规则",
        "description": "Group prompt: review-meeting flow and output format",
        "description_zh": "群聊提示词:评审会的流程与输出格式", "scope": "group",
        "body": "This group is in review-meeting mode:\n"
                "1. The facilitator restates what is being reviewed and against which criteria;\n"
                "2. Each reviewer speaks from their own expertise, grades every finding critical / moderate / suggestion, and offers a fix;\n"
                "3. No generic praise and no remarks about people; when you disagree, show your evidence;\n"
                "4. The facilitator closes with a verdict (pass / pass after changes / fail) and a to-do list (owner + deadline).",
        "body_zh": "本群进入评审会模式:\n"
                   "1. 主持人先复述评审对象与评审标准;\n"
                   "2. 评审成员各自从自己的专业角度给出意见,按「严重 / 一般 / 建议」分级,并给出改进办法;\n"
                   "3. 不要泛泛表扬,不要人身评价;有分歧时各自给依据;\n"
                   "4. 最后由主持人给出结论(通过 / 修改后通过 / 不通过)和待办清单(负责人 + 时间)。",
    },
    "relay-writing": {
        "name": "Relay writing rules", "name_zh": "接力创作规则",
        "description": "Group prompt: how consecutive writers hand off",
        "description_zh": "群聊提示词:多人接力写作的衔接规则", "scope": "group",
        "body": "This group is in relay-writing mode:\n"
                "1. Each writer must carry forward the previous writer's characters, setting, and tone — never overturn what is already established;\n"
                "2. Write only your own section, then leave one sentence for the next writer about the threads to watch;\n"
                "3. The proofreader closes by unifying style, timeline, and names.",
        "body_zh": "本群进入接力创作模式:\n"
                   "1. 后一位必须承接前一位的人物、设定和语气,不推翻已有设定;\n"
                   "2. 每人只写自己负责的那一段,写完用一句话交代给下一位需要注意的伏笔;\n"
                   "3. 最后由校对统一文风、时间线和人名。",
    },
    "debate": {
        "name": "Debate rules", "name_zh": "辩论规则",
        "description": "Group prompt: proposition vs. opposition, and the judge's summary",
        "description_zh": "群聊提示词:正反方辩论与裁判总结", "scope": "group",
        "body": "This group is in debate mode:\n"
                "1. The facilitator defines the motion; the proposition and the opposition each set out 3 arguments with evidence;\n"
                "2. Cross-examination targets only the gaps in the other side's arguments — do not restate your own position;\n"
                "3. The judge closes with each side's strongest argument, each side's weakness, and a leaning with reasons.",
        "body_zh": "本群进入辩论模式:\n"
                   "1. 主持人先界定辩题;正方、反方各陈述 3 个论点并给出论据;\n"
                   "2. 交叉质询:只针对对方论点的漏洞,不重复自己的立场;\n"
                   "3. 裁判总结:双方最强的论点、各自的漏洞、最终倾向及理由。",
    },
    "code-review": {
        "name": "Code review checklist", "name_zh": "代码评审清单",
        "description": "The order to check code in during a review",
        "description_zh": "评审代码时按顺序检查的清单", "scope": "member",
        "body": "Review code in this order:\n"
                "1. Is it the right thing: is this what was asked for? Are the edge cases handled (empty values, oversized input, concurrency, retries)?\n"
                "2. Can it fail: exception paths, resource release, out-of-range and null values;\n"
                "3. Security: input validation, injection, secrets or sensitive data in logs;\n"
                "4. Maintainability: naming, single responsibility, duplicated code, tests where they are needed;\n"
                "5. Output: grade every finding critical / moderate / suggestion, and give its location, the problem, and the fix. Never settle for \"consider optimizing\".",
        "body_zh": "评审代码时按这个顺序看:\n"
                   "1. 需求对不对:实现的是不是要求的事?边界条件(空值、超长、并发、失败重试)处理了吗?\n"
                   "2. 会不会出错:异常路径、资源释放、越界与空值;\n"
                   "3. 安全:输入校验、注入、密钥与日志里有没有敏感信息;\n"
                   "4. 可维护:命名、职责是否单一、重复代码、有没有必要的测试;\n"
                   "5. 输出格式:按「严重 / 一般 / 建议」分级,每条给出位置、问题、改法;不要只说「建议优化」。",
    },
    "research-findings": {
        "name": "Research findings write-up", "name_zh": "调研报告规范",
        "description": "Structure and evidence requirements for research output",
        "description_zh": "调研类输出的结构与证据要求", "scope": "member",
        "body": "When writing up research findings:\n"
                "1. Open with one sentence giving the conclusion and the recommendation;\n"
                "2. Cite a source for every conclusion (document title / link / data definition); mark anything uncited as \"unverified\";\n"
                "3. Keep facts, inferences, and opinions in separate sections rather than mixed together;\n"
                "4. Proactively list counter-evidence or uncertainty;\n"
                "5. Close with what further information is needed to settle the question.",
        "body_zh": "写调研结果时:\n"
                   "1. 开头一句话给结论与建议;\n"
                   "2. 每个结论后面标出处(文档标题 / 链接 / 数据口径),没有出处的标「未核实」;\n"
                   "3. 把事实、推断、观点分开写,不要混着写;\n"
                   "4. 主动列出反面证据或不确定性;\n"
                   "5. 结尾给出为了定论还需要补的信息。",
    },
    "data-analysis": {
        "name": "Data analysis conventions", "name_zh": "数据分析规范",
        "description": "Definitions, calculations, and limits for data conclusions",
        "description_zh": "数据结论的口径、计算与局限说明", "scope": "member",
        "body": "When giving a data conclusion:\n"
                "1. State the definitions first: data range, time window, sample size, filters;\n"
                "2. Write out the calculation so someone else can reproduce it (formula or steps);\n"
                "3. Give units and magnitude on numbers; when reporting a change, give both the percentage and the absolute value;\n"
                "4. Note data-quality problems and limits, and never present correlation as causation;\n"
                "5. Put the conclusion in the first sentence, with the evidence after it.",
        "body_zh": "给数据结论时:\n"
                   "1. 先说口径:数据范围、时间窗、样本量、筛选条件;\n"
                   "2. 写清计算方式,让别人能复核(给公式或步骤);\n"
                   "3. 数字带单位与量级;说变化时百分比与绝对值都给;\n"
                   "4. 说明数据质量问题与局限,不要把相关性说成因果;\n"
                   "5. 结论一句话放开头,证据跟在后。",
    },
    "translation": {
        "name": "Chinese-English translation", "name_zh": "中英互译规范",
        "description": "Tone and terminology consistency for Chinese-English translation",
        "description_zh": "中英互译的语气与术语一致性要求", "scope": "member",
        "body": "When translating between Chinese and English:\n"
                "1. Read the whole text first and settle the register (formal / conversational / technical), then keep it consistent throughout;\n"
                "2. Translate proper nouns consistently; on first mention gloss the original in brackets;\n"
                "3. Do not translate word for word — recast the sentences the way the target language prefers;\n"
                "4. Keep numbers, units, and citations exactly as they are;\n"
                "5. For long texts, output paragraph by paragraph so the work can be checked sentence by sentence.",
        "body_zh": "做中英互译时:\n"
                   "1. 先通读全文定语气(正式 / 口语 / 技术文档),整篇保持一致;\n"
                   "2. 专有名词统一译法,首次出现时用「中文(English)」标注原文;\n"
                   "3. 不逐字硬译,按目标语言的表达习惯重组句子;\n"
                   "4. 原样保留数字、单位与引用;\n"
                   "5. 长文分段对照输出,方便逐句校对。",
    },
    "report-structure": {
        "name": "Research report structure", "name_zh": "研究报告结构",
        "description": "Section skeleton for a formal research report or paper",
        "description_zh": "正式研究报告 / 论文的章节骨架", "scope": "member",
        "body": "For a formal research report or paper, follow this skeleton:\n"
                "1. Abstract: one sentence each for the problem, the method, the main result, and the conclusion;\n"
                "2. Introduction: background, what earlier work lacks, the question this piece answers;\n"
                "3. Methods: design, subjects or data source, metrics and statistical definitions, steps someone could reproduce;\n"
                "4. Results: findings only, with numbered figures and tables;\n"
                "5. Discussion: relation to earlier work, limitations, how far the conclusions generalize;\n"
                "6. Conclusion and recommendations. Keep one citation format throughout.",
        "body_zh": "写正式研究报告或论文时按这个骨架:\n"
                   "1. 摘要:问题、方法、主要结果、结论各一句;\n"
                   "2. 引言:背景、已有工作的不足、本文要回答的问题;\n"
                   "3. 方法:设计、对象或数据来源、指标与统计口径、可复现的步骤;\n"
                   "4. 结果:只写发现,图表编号并说明;\n"
                   "5. 讨论:与已有研究的关系、局限、结论能外推到哪里;\n"
                   "6. 结论与建议。全文引用格式统一。",
    },
    "risk-check": {
        "name": "Risk self-check", "name_zh": "风险自查清单",
        "description": "Compliance and risk check before publishing or partnering",
        "description_zh": "对外发布或对外合作前的合规与风险自查", "scope": "member",
        "body": "Before publishing or partnering, check each item and give \"present / absent / unconfirmed\" plus a suggested action:\n"
                "1. Data: any personal data, identifying information, or unpublished internal data?\n"
                "2. Rights: do you have the right to use the assets, data, and quotations? Are the licence terms met (attribution, licence copy, commercial use allowed)?\n"
                "3. Claims: any absolute promises, implied efficacy, or unverified comparisons?\n"
                "4. Compliance: does it touch the rules of the industry you are in (for example advertising and disclosure in healthcare or finance)?\n"
                "5. Security: do the files you are about to send out contain keys, internal addresses, or internal names?",
        "body_zh": "对外发布 / 合作前逐项自查,每项给「有 / 无 / 待确认」和处置建议:\n"
                   "1. 数据:有没有个人信息、可识别信息、未公开的内部数据?\n"
                   "2. 授权:素材、数据、引用是否有权使用?许可要求是否满足(署名、许可副本、是否允许商用)?\n"
                   "3. 表述:有没有绝对化承诺、功效暗示、未经核实的对比?\n"
                   "4. 合规:是否触及所在行业的监管要求(如医疗、金融的宣传与信息披露)?\n"
                   "5. 安全:要发出去的文件里有没有密钥、内网地址、内部人名?",
    },
}


# Both spellings of a built-in skill name (and its stable key) resolve to the same
# entry, so a skill seeded before the content was translated still matches — for the
# "already installed" badge, for the skills a group template asks for, and for the
# body injected into the prompt.
SKILL_ALIASES: dict[str, dict] = {}
for _key, _entry in EXAMPLE_SKILLS.items():
    for _name in (_key, _entry.get("name"), _entry.get("name_zh")):
        if _name:
            SKILL_ALIASES.setdefault(_name, _entry)


def skill_for(name: str | None) -> dict | None:
    """The built-in skill a name refers to, in either language (or its key)."""
    return SKILL_ALIASES.get(name or "")


def skill_names(name: str | None) -> list[str]:
    """Every spelling a stored skill name might have been written in."""
    entry = skill_for(name)
    if not entry:
        return [name] if name else []
    out = [entry["name"]]
    if entry.get("name_zh"):
        out.append(entry["name_zh"])
    return out


def display_skill_name(name: str | None, lang: str) -> str:
    """A built-in skill name as it should be shown in `lang` (either spelling resolves)."""
    entry = skill_for(name)
    if not entry:
        return name or ""
    return (entry.get("name_zh") if lang == "zh" else entry["name"]) or (name or "")


def canonical_skill_name(name: str | None) -> str:
    """The language-neutral name a built-in skill is stored under."""
    entry = skill_for(name)
    return entry["name"] if entry else (name or "")


def localize_skill(skill: "Skill", lang: str) -> "Skill":
    """Show `skill` in `lang`, but only where the user has not edited it.

    A field is swapped only while it still matches the built-in text in either
    language, so anything the user rewrote is handed back exactly as they wrote it.
    """
    entry = skill_for(skill.name)
    if not entry:
        return skill
    out = copy.copy(skill)
    for field, zh_field in (("name", "name_zh"), ("description", "description_zh"),
                            ("body", "body_zh")):
        zh = entry.get(zh_field)
        base = (entry.get(field) or "").strip()
        if not zh:
            continue
        current = (getattr(skill, field) or "").strip()
        if current not in {base, zh.strip()}:
            continue                       # user edited this field — keep their text
        setattr(out, field, zh.strip() if lang == "zh" else base)
    return out


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: str
    scope: str = "member"   # member = 给成员勾选 | group = 群聊规则,挂到群
    version: str = ""

    def summary(self) -> dict:
        return {"name": self.name, "description": self.description, "path": self.path,
                "scope": self.scope, "version": self.version}


def safe_skill_name(name: str) -> str:
    n = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", name).strip().strip(".")
    return n[:60]


def parse_skill_text(text: str, default_name: str, path: str = "") -> Skill:
    name, desc, body, scope, version = default_name, "", text, "member", ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip().strip("\"'")
            if k == "name" and v:
                name = v
            elif k == "description":
                desc = v
            elif k == "scope" and v in ("member", "group"):
                scope = v
            elif k == "version":
                version = v
        body = m.group(2)
    return Skill(name=name, description=desc, body=body.strip(), path=path, scope=scope, version=version)


def _parse_skill(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_skill_text(text, path.parent.name, str(path))


def render_skill(name: str, description: str, body: str, scope: str = "member", version: str = "") -> str:
    head = f"---\nname: {name}\ndescription: {description.strip()}\nscope: {scope}\n"
    if version:
        head += f"version: {version}\n"
    return head + f"---\n{body.strip()}\n"


def write_skill(skills_dir: Path, name: str, description: str, body: str, scope: str = "member",
                version: str = "", old_name: str | None = None) -> Skill:
    folder = safe_skill_name(name)
    if not folder:
        raise ValueError("技能名称不合法")
    d = skills_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        render_skill(folder, description, body, scope if scope in ("member", "group") else "member", version),
        encoding="utf-8",
    )
    if old_name and safe_skill_name(old_name) != folder:
        delete_skill(skills_dir, old_name)
    return _parse_skill(d / "SKILL.md")  # type: ignore[return-value]


def delete_skill(skills_dir: Path, name: str) -> bool:
    d = skills_dir / safe_skill_name(name)
    if d.is_dir() and d.parent == skills_dir:
        shutil.rmtree(d)
        return True
    return False


def ensure_example_skills(skills_dir: Path, flag: Callable[[str], bool] | None = None) -> None:
    """首次运行时写入示例技能。flag(key) 返回「以前是否已经写过」并同时打上标记,
    这样用户删掉某个示例后,它不会在下次启动时又被写回来;新增的示例技能能补给老用户。"""
    was_empty = not any(skills_dir.iterdir())
    for key, ex in EXAMPLE_SKILLS.items():
        if flag is not None:
            if flag(f"seed_skill:{key}"):
                continue
        elif not was_empty:
            return
        # The canonical English name is what lands on disk; a Chinese UI shows the
        # Chinese wording through localize_skill() without touching the file.
        name = ex["name"]
        d = skills_dir / name
        if d.exists():
            continue
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(render_skill(name, ex["description"], ex["body"], ex["scope"]), encoding="utf-8")


def list_skills(skills_dir: Path) -> list[Skill]:
    out = []
    for p in sorted(skills_dir.glob("*/SKILL.md")):
        s = _parse_skill(p)
        if s:
            out.append(s)
    return out


def skills_prompt(skills_dir: Path, names: list[str], max_chars: int = 4000, group: bool = False) -> str:
    """把勾选的 skills 拼成一段提示词。group=True 时标题写成「群聊规则」。

    Names are looked up by every spelling a built-in skill may have been stored
    under, and the text handed to the model is the one matching the request
    language — a group created on a Chinese install and one created on an English
    install produce the same prompt in the same language.
    """
    by_name: dict[str, Skill] = {}
    for s in list_skills(skills_dir):
        for spelling in skill_names(s.name):
            by_name.setdefault(spelling, s)
    lang = i18n.current()
    parts, used = [], 0
    for n in names:
        s = by_name.get(n)
        if not s:
            continue
        s = localize_skill(s, lang)
        is_group = group or s.scope == "group"
        head = i18n.pick(lang, "Group rule" if is_group else "Skill",
                         "群聊规则" if is_group else "技能")
        chunk = (f"【{head}:{s.name}】\n{s.body}" if lang == "zh"
                 else f"[{head}: {s.name}]\n{s.body}")
        if used + len(chunk) > max_chars:
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(parts)


# ------------------------------------------------------------------ plugins
ToolFn = Callable[[dict[str, Any]], Awaitable[Any] | Any]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema
    fn: ToolFn
    source: str = "builtin"      # builtin | plugin | mcp
    plugin: str = ""             # 插件 ID(文件名,不含 .py)

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters,
                "source": self.source, "plugin": self.plugin}


@dataclass
class PluginInfo:
    id: str
    file: str
    name: str = ""
    description: str = ""
    version: str = ""
    tools: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "file": self.file, "name": self.name or self.id, "description": self.description,
                "version": self.version, "tools": self.tools, "error": self.error}


class _PluginScope:
    """交给插件的 registry:登记的工具会记在该插件名下。"""

    def __init__(self, reg: "ToolRegistry", plugin: str):
        self._reg, self._plugin = reg, plugin

    def register(self, name: str, description: str, parameters: dict | None, fn: ToolFn) -> None:
        old = self._reg._tools.get(name)
        if old is not None:
            # 以前是后登记的悄悄顶掉先登记的:被顶掉的插件在权限页里仍显示有这个工具,却调不到,还没有任何提示
            raise ValueError(f"工具名「{name}」已被{('插件 ' + old.plugin) if old.plugin else '内置工具'}占用,请换个名字")
        self._reg._add(Tool(name, description, parameters or {"type": "object", "properties": {}}, fn, "plugin", self._plugin))


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.plugins: dict[str, PluginInfo] = {}

    @property
    def errors(self) -> list[str]:
        return [f"{p.file}: {p.error}" for p in self.plugins.values() if p.error]

    def _add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        if tool.plugin and tool.plugin in self.plugins:
            self.plugins[tool.plugin].tools.append(tool.name)

    def register(self, name: str, description: str, parameters: dict | None, fn: ToolFn,
                 source: str = "builtin") -> None:
        self._add(Tool(name, description, parameters or {"type": "object", "properties": {}}, fn, source))

    def list(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()]

    def plugin_tools(self, plugin_ids: list[str]) -> list[Tool]:
        return [t for t in self._tools.values() if t.plugin in plugin_ids]

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        tool = self._tools[name]
        if inspect.iscoroutinefunction(tool.fn):
            return await tool.fn(args)
        res = await asyncio.to_thread(tool.fn, args)   # 普通函数放到线程里跑,慢插件不会卡住整个后端
        if hasattr(res, "__await__"):
            res = await res  # type: ignore[misc]
        return res

    def load_plugins(self, plugins_dir: Path) -> None:
        """插件文件示例:
            PLUGIN = {"name": "问候", "description": "打招呼", "version": "1.0"}   # 可选
            def register(registry): registry.register("hello", "说你好", None, lambda a: "hi")"""
        for name in [n for n, t in self._tools.items() if t.source == "plugin"]:
            del self._tools[name]
        self.plugins = {}
        for f in sorted(plugins_dir.glob("*.py")):
            info = PluginInfo(id=f.stem, file=f.name)
            self.plugins[f.stem] = info
            try:
                spec = importlib.util.spec_from_file_location(f"team_agent_plugin_{f.stem}", f)
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                meta = getattr(mod, "PLUGIN", None)
                if isinstance(meta, dict):
                    info.name = str(meta.get("name", ""))
                    info.description = str(meta.get("description", ""))
                    info.version = str(meta.get("version", ""))
                mod.register(_PluginScope(self, f.stem))
            except Exception as e:  # noqa: BLE001
                info.error = f"{type(e).__name__}: {e}"[:300]


def build_registry(plugins_dir: Path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.load_plugins(plugins_dir)
    return reg
