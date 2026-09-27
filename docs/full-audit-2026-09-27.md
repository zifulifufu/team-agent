# 全程序审查（2026-09-27）—— 分链路外派 + 逐条核实

用户要求：「分别联合 codex 和 claude 对整个程序每个节点、每个逻辑链都进行调试，找出所有的 bug」。

后端 **24 个模块 / 48172 行**、94 个测试文件，一次读不完。做法是**按链路切块**（视频制作、群聊编排、…），
每块外派给一个只读的命令行智能体，要求它给出「**文件:行号 + 原代码 + 会导致什么现象 + 怎么验证**」四样；
收到的每一条都**由本人在代码里核实过**才进下表 —— 模型的结论不算证据。

## ⚠️ 先说不成立的那一半：codex 这一轮用不上

| 试法 | 结果 |
|---|---|
| `codex --version`（系统 x64 node，Rosetta） | 崩：`Missing optional dependency @openai/codex-darwin-x64` |
| 换 managed arm64 node（能跑，`codex-cli 0.155.1`） | ✅ 版本能打 |
| `echo PONG \| codex exec -s read-only -` | **600 秒无输出**，超时杀 |
| 加 `-c 'mcp_servers={}'`（它的 `config.toml` 里挂了 deepwiki，`startup_timeout_sec = 3600`、`model_reasoning_effort = "xhigh"`） | **280 秒仍无输出**，超时杀 |

`~/.codex/auth.json` 在、登录态在，所以不是没登录。**结论：这条路在本会话环境里走不通**，原因（网络出口 /
代理）无法从程序侧确认。**所以下面只有 claude 一路的产出**；WorkBuddy 自带的引擎上一轮也确认未登录。

---

## 一、群聊编排链路（claude 报 5 条，核实 2 条成立并已修，3 条待修）

### 1.1 ✅ 已修：纯接力轮耗尽轮数时，账本什么都不记

- **它说**：`orchestrator.py:1285-1286` 传 `open_tasks=list(run.unfinished)`，而 `run.unfinished` 只在
  `_execute_plan` 的 finally 里被赋值（`:1974`）——没走分工的接力轮是空列表，`proclog.py:601` 的
  `if exhausted and open_tasks:` 直接不产条目。
- **核实**：成立。跑 `auto_round_defects(exhausted=True, open_tasks=[])` → `[]`；
  传一个任务 → `['The round ran out of turns with work unfinished']`。
  而调用处上方注释明写「The process log keeps this one … the entry is what makes it countable a week
  later」——**注释承诺的与代码行为相反**，且落空的恰好是「连任务板都没有、再没别处能说清」的那一轮。
- **修**：门槛改成只看 `exhausted`；没有 `open_tasks` 时换一句如实的话（「这一轮到达上限结束，而且没有任务
  板，所以没有任何地方记下剩下的是什么」）。测试：`test_proclog.py::test_a_round_that_ran_out_of_turns_is_recorded_even_with_no_task_board`。

### 1.2 ✅ 已修：取消落在任务板第一次 emit 上，板子永远卡在 running

- **它说**：`_execute_plan` 里 `add_message(...)`（`status="running"`）与 `await emit(...)` 在 `try:` **之外**
  （`:1711-1713`），改写 running/pending 的两个异常处理器在 try **里面**（`:1945-1971`）→ 取消落在那次 emit
  上就两边都躲过。
- **核实**：成立，代码顺序就是如此。
- **修**：把 `try:` 提到 `add_message` 之前（`by_id`/`outputs`/`push` 的定义上移）。测试：
  `test_collab.py::test_a_cancel_landing_on_the_boards_own_first_emit_still_stops_it`
  ⚠️ 断言**不写死任务状态列表**：取消发生在任何任务开始之前，所以全是 `skipped` 而没有一个 `stopped` ——
  写死列表等于把「取消的时机」钉住，而不是把「没有任务卡在中间态」钉住（第一版就是这么写错的）。

### 1.3 ⏳ 待修：交付重试失败后，**第一次**那条假声明不盖章

- **它说**：`out = again`（`:1884`）后 `_stamp_failure(out.message, ...)`（`:1900`）只盖**重试**那条；
  触发重试的第一条「已落盘」原样留在 transcript 里，下一轮群主读到它就当既成事实。
- **核实**：**位置属实**，但那段代码有一段注释明确解释 `out = again` 是**有意**的
  （「板上的 error 和它挂的消息必须来自同一次尝试」）。所以这不是"写错了"，而是**只做了一半**：
  同源的要求与「第一条也要被标记」并不冲突，缺的是后者。
- **要做**：给第一条也盖章（`_stamp_failure` 自身幂等，靠 `meta.delivery_verdict`），并补一条断言查第一条。
  ⚠️ 动手前必须复查 `_task_shortfall` —— 它有一个分支**从正文抠文件名**，盖章正文里含那个缺失名会
  **反过来替声明作证**。

### 1.4 ⏳ 待修：计划被拒时，推送通道收到的仍是「已做好分工，见任务板」

- **它说**：`_plan_failed` 改写了**库里那条消息**（`:1228`），但 `out.text` / `run.final_text` 是旧值，
  `_announce`（`:991`）把旧值发给 `on_answer` → 推向 WhatsApp 等的与库里相反，且「见任务板」对推送用户
  毫无意义。
- **核实**：**未核实**（要读 `_announce` 与 `_plan_failed` 的完整路径，本轮没做）。**列入下一轮第一件事**。

### 1.5 ⏳ 待修：本地工具成员的交付重试，nudge 永远到不了它

- **它说**：重试把 nudge 拼进 `extra_user`，但 `_local_tool_turn` 取指令时 `task_instruction` **优先**
  （`:2504-2506`），且 `arguments` 与第一次逐字节相同 → 同一渲染以相同输入再跑一遍，耗时翻倍。
  「讽刺的是 `run.tool_failures` 会把它记成 tool-loop，而账本那条 hint 正是『失败原因要作为指令回到成员
  那里，或者干脆别再让它重试』」。
- **核实**：**未核实**。这条如果成立，代价很实在（渲染一次几分钟 ×2）。

---

## 二、视频制作链路（claude 报 4 条，核实 1 条成立并已修，3 条待修）

### 2.1 ✅ 已修：`figure.wrap` 的「标点不单独成行」防护是死代码

- **它说**：`figure.py:107` 的 `elif not cur and ch in NO_START and lines:` —— `cur` 从第二个字符起就非空，
  `not cur` 只在**行首**成立；真正出问题的场景（一行撑满、下个字符是「。」）走的是 `else` 分支。
  注释（`:87-91`）却声称它修的就是审片人报的「句号单独挂在最后一行」。
- **核实**：**成立，且实测复现**：
  `figure.wrap("一二三四五六七八。", f, 456, draw)` → `['一二三四五六七八', '。']`。
  这个分支真能触发时（某段以标点开头）反而会把标点粘到**上一段**的行尾。
- **修**：把判断挪到「正被推到新行的那一个字符」上；无上一行可粘时允许它起行（否则字符会被静默丢掉）。
  测试：`test_figures.py::test_a_full_stop_never_lands_on_a_line_of_its_own`
  ⚠️ 断言写成**意图**（一字不丢 + 没有一行以标点开头），不写死断点：中文标点比汉字窄，第一版写死的
  断点当场就错了。

### 2.2 ⏳ 待修（最影响成片）：动画镜头超过 120 秒被静默截断

- **它说**：`animate.py:125` 把绘制时长钳到 `MAX_SECONDS = 120`，而 `make_plan` 允许单镜头到 600 秒，
  且「凑总时长」会把缺口摊给 anim 镜头；anim 分支没有 video 分支那样的 `tpad` 兜底（注释还写着
  「永远不会短于自己的格子」）。**从该镜头起，字幕 / 音乐 ducking / 混音截断 / 分镜表的总时长全部与画面错位**，
  而 `_concat` 按实测求和，什么都不会报。
- **核实**：**未核实**（要跑 ffmpeg 渲染）。这条**优先级最高** —— 它直接决定「300 秒成片到底是不是 300 秒」。

### 2.3 ⏳ 待修：动画镜头的 `subtitle` 被静默丢弃

- **它说**：`assemble.py:929-931` 调 `animate.render` 时没传 `caption`，而 `animate.render` 有这个形参
  （`:110`）；卡片镜头和静帧都给画，**同一字段在动画镜头上无声消失**。
- **核实**：**未核实**（要读三处签名 + 渲染一帧看脚注区）。

### 2.4 ⏳ 待修：「哪些镜头硬切」的备注先于旁白延长算出

- **它说**：`render()` 在**录音之前**用旧时长算硬切名单（`:1127-1135`），而 `_fade_chain` 用**延长后**的时长
  （`:1020`）→ 一个被旁白拉长的镜头实际带了淡入淡出，备注却说它硬切，`fade_fits` 的 docstring 自称
  「两处口径一致」。
- **核实**：**未核实**。

**它明确排除的（不必重查）**：`_render_shot` 的输入序号与 `-ss -t` 位置、`_concat` 的复制校验与回退重编码、
`_unify` 与 `visual.admit` 的返回契约、`_pending_reviews` 的 typo 豁免、`_task_shortfall` 的后缀计数、
`planner._FILE_NAME` 对 `video/成片-60s.mp4` 的匹配。

---

## 三、还没派人去查的链路（下一轮）

写下来，免得下一轮又从头想：

1. **写作文档链路**：`write_document` / `render_document` / 分节 append / 长文超限被整个丢掉那条路。
2. **知识库与检索链路**：BM25+向量+RRF 的合并、`scope_kbs` 可见性、跨语言 0 命中。
3. **媒体生成链路**：`make_figure` / `make_animation` / `make_music` / `generate_image` 的产物落盘与验收。
4. **文件与工作目录**：`uploads/`、`tasks/`、软链越界、`is_symlink` 判定。
5. **专区与设置面板**：见第四节。

---

## 四、⚠️ 四个专区的 `planned`：为什么不能靠改字变绿

用户要求「视频、写作、故事、创作专区都要把功能规划好，不要有规划中，不能留空」。本轮**没有动它们**，
原因必须说清楚：

`zones.py` 顶部自己写着这条规矩：

> ⚠️⚠️ `state` 是**承诺**，而这是唯一做出承诺的地方。`ready` 意味着这个专区现在能用；
> `planned` 意味着它被声明了、而页面说出了缺什么。**在一个 planned 专区下面写上看起来像样的内容，
> 正是让人点到一个什么都不做的按钮的方式。**

所以「把 `planned` 改成 `ready`」不是完成任务，是**让页面开始撒谎**。要么把功能真做出来，要么如实留着。

四个专区的现状（`app/zones.py`）：

| 专区 | 专区状态 | 里面标 `planned` 的条目 |
|---|---|---|
| 视频 | **ready** | 无（`motion` 是 `partial`、`lipsync`/`scene-stills`/`html-renderers` 是 `blocked`，各自都写明了卡在哪） |
| 写作 | `planned` | 草稿架、材料架、提纲起手件、"收成一篇"、"逐条核对引用" |
| 故事 | `planned` | 人物与设定、节拍、三幕骨架、节拍表、"不让故事自相矛盾"、"通篇一个声音" |
| 创作 | `planned` | 灵感板、母题与意象、生成图（blocked） |

**下一轮的做法**（每条都验「真的能做」再改状态）：
1. **能靠加真实东西解决的**：`three-act` / `beats-template` / `moodboard` / `motifs` / `outline` 这五个是
   **起手件**，对应 `presets.py` 里真实的群模板 —— 加模板 + 加对应测试即可标 `ready`。
2. **需要新引擎的**：草稿版本、材料架、"收成一篇"、"逐条核对引用"、"不让故事自相矛盾" —— 各是一小块真代码
   （存储 + 一条流水线 + 一条校验），不是配置。
3. **真的做不了的**：`scene-stills`、`generate`（云端无额度、本机无图像模型）—— 保持 `blocked` 并写明卡在哪，
   这比标绿有价值。

---

## 五、重做那条动脉瘤科普片：先看已量到的东西

`docs/video-quality-audit-2026-09-26.md` 已经把上一版**逐条核对过用户意见并量过**（成片
`31dd6fef6e27/video/颅内动脉瘤介入治疗科普-300s.mp4`，300.0s / 1080×1920 / 24fps / AAC / 116.5 MB）。
里面**已修** 1 条（静帧默认推镜），**待改** 4 条：

| 待改 | 代价 |
|---|---|
| 逐句字幕 | 一个镜头只有一个 `text` —— 要么让镜头带 `lines[]`（动 plan 解析 + `srt_of` + 烧字三段），要么分镜阶段按句切镜头（只动提示词，但镜头数翻倍、渲染更久） |
| 版式默认 | 科普片该用 `public-science`，现在成员一律传 `default` |
| 技能接入 | 建群时不按任务性质推荐技能（这一步是配置） |
| 音画对齐 / 配音情感 | 需要先能量到（抽帧对照 srt 时间轴；听音频或看合成参数） |

⚠️ **本节 2.2 那条（动画镜头被钳到 120 秒）如果成立，会直接推翻"成片是 300 秒"这件事** —— 所以重做之前
应当先把它核实掉，否则新片会和旧片犯同一个错。

---

## 六、本轮验证

- 后端全量（隔离环境）：见提交说明。
- 新增测试 4 条：`test_proclog.py` 1 条、`test_collab.py` 1 条、`test_figures.py` 1 条（另含前一轮的
  `test_handoff.py` 17 条与 `test_advisor.py` 1 条）。
- ⚠️ **没有做的**：写作/故事/创作三个专区、面板设计评审、视频重做。它们各自是独立的一轮。
