# 视频专区的技能怎么摆，以及 InfiniteTalk 这条路（2026-09-27）

两件事一起做：把新技能挂到「视频制作」，把该卸的卸掉；以及试 InfiniteTalk。
结论先说：**技能那件做完了，而且是量出来的；InfiniteTalk 这条路本机走不通原仓库那条，应用自带的那条正在下权重。**

---

## 一、技能预算的真实机制（这一轮所有决定的依据）

`tools.skills_prompt(sk_dir, names, max_chars=4000, group=False)`：

| 规则 | 值 |
|---|---|
| 一个群的全部技能合起来 | **4000 字符** |
| 单条技能最多吃 | **2000 字符**（`max(600, max_chars // 2)`） |
| 超出 2000 的 | 保留**可读的开头** + 一句指路 |
| 余量 ≤200 时 | 剩下的**全部丢掉** |

`prompting.py:230-231` 是**两块**独立的 4000：群块（`group["ext"]["skills"]`）和成员块（`agent["skills"]`）。
所以一个群实际最多装 2 条完整技能 —— 这决定了一切。

### 改之前两个群的实际状态

```
【视频制作】 3 条：369 + 1192 + 1899 = 3460 → 3464  ✅ 全进
【实测·介入术式科】 7 条 → 只有 1831 + 2143 = 3976 进得去
   hyperframes              1831  ✅
   hyperframes-creative     2143  ✅（且被截到 2000）
   hyperframes-animation    2132  ❌ 完全不在
   embedded-captions        2118  ❌
   motion-graphics          2039  ❌
   Long-form video: …       2007  ❌ ← 这个群做片子最需要的一条
   Check the result …       1192  ❌
```

**7 条里有 5 条完全没有进提示词**，而设置页把它们全显示成「已接入」。那个群自己的账本里就写着
「群配置里登记的 hyperframes / check-the-result 技能在本机库中未直接命中」—— 症状对得上。

---

## 二、动了什么

### 技能清单

| 群 / 成员 | 改成 | 为什么 |
|---|---|---|
| **视频制作**（群） | `Check the result…` + `Work from a reference…` = **3093/4000** | 这两条都是 group scope，只能待在群上；两条都完整进去 |
| **实测·介入术式科**（群） | `Long-form video…` + `Check the result…` = **3206/4000** | 卸掉 5 条 HTML 渲染链，换来那条最关键的完整进去 |
| **两群的 Storyboard 席**（成员） | `Short video storyboards` + `Writing a prompt for a generated shot` = **1302/4000** | 新技能是 member scope；分镜席就是写生成提示词的那一席 |

**为什么不是「往群上加一条」**：群上那两条 group scope 规则已占 3091，再加 931 = **4022，超 22 字符**，
而超出的部分会被截断 —— 见下一节，截断等于永久看不见。所以新技能走成员块（它本来就是 member scope，
`zones.py` 里分镜席的声明也正是它该在的地方）。**结果：两个群 + 两个成员，零截断。**

### 卸掉哪些、依据是什么

卸的是 **HTML 渲染链那 5 条**（`hyperframes`、`hyperframes-creative`、`hyperframes-animation`、
`embedded-captions`、`motion-graphics`）。依据不是我的判断，是**应用自己的话**（`zones.py:262-268`）：

> Remotion、HyperFrames 和 video-shotcraft 都要一个 HTML 工程文件，而本机**没有任何工具能写 `.html`**。
> 别把它们写进计划：它们会跑、会白耗一轮、什么都不产出。

而且它们是**连锁**的：`motion-graphics` 自己写着「The front door is `/hyperframes`」，
`embedded-captions` 写着「Routed through `/hyperframes`」。这一族在本机是**纯占预算**。

### 顺带发现并修掉的四处「同一件事写两份，两处分歧」

1. ⚠️⚠️ **截断说明是一句做不到的指引。** 超长技能后面会附一句「剩下的在 `<路径>/SKILL.md`，
   照着这几步做之前先读它」—— 而**成员根本读不到**：`toolhub.py` 里所有读文件都收在
   `relative_to(workspace)` / `is_relative_to(root)` 之内，整个文件里**没有任何一处引用技能目录**。
   结果：成员要么猜缺掉的步骤，要么**声称自己读过**。改成说清「你调得到的工具打不开它，向用户要」。
2. **同一句话说在「自带文件的技能」上**（`<SKILL_DIR>` 那句「它让你读的东西都在那里」）—— 同一个病，一起改。
3. ⚠️⚠️ **预算用完时仍然静默丢弃。** `break` 会让后面的技能连名字都不留 —— 正是这段代码的注释说它
   已经修好的那个病。现在**点名**：「本群另有 5 条技能放不进这里，所以**不在**本提示词里：…」。
   实测拿原来那 7 条跑：`Long-form video…` 现在会被点名，原来它消失得无声无息。
   （新增 `tests/test_skills_budget.py` —— 这块逻辑**此前一个测试都没有**。）
4. ⚠️⚠️ **`zones.py` 里那句「口播口型」的说明是假的。** 它写着
   「**权重全齐**（14B 底座、补丁、中文音频编码器），唯独缺一个节点包 `ComfyUI-WanVideoWrapper`。除此以外不缺东西。」
   两半都错（实测 2026-09-27）：
   - **六个权重文件一个都不在盘上**（`comfyui.SETUP_WORKFLOWS["infinite-talk"]` 自己写着
     「six weight files are not on this machine yet」—— 同一件事的两份说明互相矛盾）；
   - **节点是 ComfyUI 自带的**：`comfy_extras/nodes_wan.py` 有 `WanInfiniteTalkToVideo`、
     `nodes_model_patch.py` 有 `ModelPatchLoader`、`nodes_audio_encoder.py` 有 `AudioEncoderLoader`，
     本机的 ComfyUI 是 **0.35.0**，三个都在。**不需要任何节点包。**

   现在这条说明只写「它需要什么」和「缺哪个由『测试』指名」，不再替这台机器下结论。

---

## 三、InfiniteTalk：两条路，一条死了，一条在跑

### 路 A：仓库自己的推理脚本 —— **本机装不起来**（实测，不是推断）

| 检查 | 结果 |
|---|---|
| `pip install torch`（arm64 mac） | ✅ `torch 2.14.0`，**MPS 可用** |
| `pip install -r requirements.txt` | ❌ **死在 `decord`**：`Could not find a version that satisfies the requirement decord (from versions: none)` |
| `decord` 在 PyPI 上 | 5 个轮子，**macOS arm64 一个都没有，而且没有源码包** → 装不上就是装不上 |
| `xformers` / `flash-attn` | 只有源码包，**需要 CUDA**；而 `wan/modules/attention.py` 在**模块顶层硬导入** `xfuser` 和 `xformers.ops` |
| 权重总量 | Wan2.1-I2V-14B-480P **76.6 GB** + InfiniteTalk 权重（含多个量化版，`quant_models` 里 5 个各 18 GB）+ wav2vec2 1.4 GB |

硬件不是瓶颈（**M5 Max / 128 GB / 5.9 TB 可用**）。挡路的是**这条栈是 CUDA 专用的**。

### 路 B：应用自带的 ComfyUI 工作流 —— **正在下权重**

应用里已经有一条 `infinite-talk`（`SETUP_WORKFLOWS`），`needs` 列了 **6 个文件**，全是 ComfyUI 用的
fp8/scaled 版，比原仓库小得多：

| 大小 | 文件 | 来自 |
|---|---|---|
| 15.50 GB | `Wan2_1-I2V-14B-480p_fp8_e4m3fn_scaled_KJ.safetensors` | Kijai/WanVideo_comfy_fp8_scaled |
| 6.27 GB | `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | Comfy-Org/Wan_2.1_ComfyUI_repackaged |
| 4.77 GB | `wan2.1_infiniteTalk_multi_fp16.safetensors` | 同上 |
| 0.69 GB | `lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors` | Kijai/WanVideo_comfy |
| 0.24 GB | `Wan2_1_VAE_bf16.safetensors` | 同上 |
| 0.18 GB | `wav2vec2-chinese-base_fp16.safetensors` | Kijai/wav2vec2_safetensors |
| **27.6 GB** | 合计 | |

**huggingface.co 本机不通**（本地代理答 **502**），**镜像 `hf-mirror.com` 通**，实测 **6.1 MB/s**。
所以下载脚本把 `HF_ENDPOINT` 自己设好，不再依赖调用者的 shell。

ComfyUI 的运行环境是现成的：`~/Documents/GitHub/ComfyUI/.venv` 里 **torch 2.14.0 + MPS True**。

⚠️ 我顺手 clone 了 `ComfyUI-WanVideoWrapper` ——**这是照 `zones.py` 那句错话做的，其实不需要**
（节点是 ComfyUI 自带的）。它躺在 `custom_nodes/` 里，不用可以删。

---

## 四、改了哪些文件

| 文件 | 改动 |
|---|---|
| `backend/app/tools.py` | 两处「成员做不到的指引」改成实话；预算用完时**点名**剩下的技能 |
| `backend/app/zones.py` | 分镜席的 `skills` 加上新技能；「口播口型」那条说明改成与实测一致 |
| `backend/tests/test_skills_budget.py` | **新增** 5 条测试，这块逻辑此前无测试 |
| 群与成员数据 | 见第二节的清单（直接改的是本机数据目录，不是代码） |
| `~/.local/opt/fetch-infinite-talk-weights.py` | **新增**：按 `comfyui.SETUP_WORKFLOWS` 的 `needs` 拉这 6 个文件，不手抄文件名 |

## 五、还没做的

- **权重下完要真跑一次**才知道 MPS 上能不能出片。下完 ≠ 能跑 —— 这个项目里这种事有先例
  （`wav2lip-onnx` 权重在、跑得动，但出的片子等于静帧）。
- **新技能还没接进 `generate_video` 工具的描述**（工具描述里现在没有提示词规范那几条）。
- group 块的 4000/2000 上限是**硬编码**的，不是设置项。想挂更多就得改代码；这一轮用「挑着挂」解决。
