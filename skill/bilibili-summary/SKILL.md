---
name: bilibili-summary
description: 总结 B 站（bilibili）视频：调用本地 biliex 工具提取 B 站官方 AI 总结与平台字幕（可选：无字幕时用本机 ASR 转写），产出带时间戳的结构化内容包，并据此生成中文总结。当用户给出 bilibili 链接、BV 号、b23.tv 短链，或说「总结这个视频 / 这视频讲了什么 / 做个笔记 / 提取要点」时使用。
whenToUse: 用户提供了 B 站视频标识并要求总结、摘要、笔记、要点提取、内容复盘，或需要把 B 站视频内容整理成可引用的文字材料时。
---

# B 站视频总结

用本地 `biliex` 工具取内容，再由你生成总结。**工具不调用任何 LLM API** —— 总结由你完成，
这样零费用、内容不出本机。

## 一、先确认前置条件

工具根目录（下称 `<BILIEX>`）按顺序确定：

1. 环境变量 `BILIEX_ROOT`（推荐：装好工具后设一次，Skill 就能自动找到它）；
2. 用户在本轮对话里明确给出的路径；
3. 两者都没有 → **问用户**。不要猜、不要全盘搜索、不要自己另外克隆一份。

> `BILIEX_HOME` 是**凭据与配置目录**（默认 `%USERPROFILE%\.bilibili-ex`），不是工具根目录，别混用。

**所有调用都用绝对路径 + 调用运算符**，这样在 PowerShell 和 cmd 下都成立：

```powershell
& "<BILIEX>\biliex.cmd" <子命令>
```

先查登录态：

```powershell
& "<BILIEX>\biliex.cmd" auth status --offline
```

- `configured: true` → 继续。
- `configured: false` → **停下来**，让用户自己在终端运行 `& "<BILIEX>\biliex.cmd" auth set`。
  B 站的字幕与官方 AI 总结接口**都必须登录**，免登录只能拿到标题简介。

> **红线：绝不要让用户把 SESSDATA 贴到对话里**，也不要自己代填。那不是本地文件，而是账号凭据，
> 一旦进入对话上下文就等同于泄露。只能引导用户在自己的终端里 `auth set`（隐藏回显输入）。

## 二、抓取内容包

```powershell
& "<BILIEX>\biliex.cmd" fetch <BV号 | av号 | 链接 | b23.tv 短链> --json
```

`--json` 会返回统一信封，重点看 `data` 里的：

| 字段 | 含义 |
|---|---|
| `output_dir` | 产物目录 |
| `index_file` | **入口文件，优先读它** |
| `level` | 命中级别：`official_ai_summary` / `subtitle` / `local_asr` / `none` / `mixed` |
| `pages[].coverage` | 字幕时长覆盖率；明显低于 1 说明内容可能被截断 |
| `pages[].unstable` | **两次独立读取结果不一致** —— 上游字幕接口不稳定 |
| `pages[].low_relevance` | **字幕与标题零关键词重合** —— 极可能不是本视频的内容 |
| `pages[].relevance` | 形如 `"2/3"`，命中的内容词数 / 总内容词数 |
| `pages[].warnings` | 告警，**必须读，不要忽略** |
| `asr_used` | 本次是否有分 P 用了本地 ASR 转写（L3） |
| `pages[].asr` | L3 的元数据：模型、设备、耗时、音频大小（没用 L3 时为空对象） |
| `pages[].asr_replaced_subtitle` | 该分 P 的平台字幕不可信，**已被本地转写替换** |

常用参数：`--out <目录>` 指定产出位置；`--page 1` 只取某个分 P；`--no-official` 跳过官方总结。

### 可选能力：对没有字幕的视频做本地 ASR（L3）

默认 `--asr off`，**没有装也不影响任何其它功能**。确认组件状态：

```powershell
& "<BILIEX>\biliex.cmd" asr status --json
```

用户已装好时可用：

| 参数 | 含义 |
|---|---|
| `--asr auto` | 平台内容不可信（无字幕 / `unstable` / `low_relevance`）时才本地转写；没装就照常降级 |
| `--asr on` | 要求 L3 可用；没装则以**退出码 6** 失败并给出安装命令 |
| `--asr-force` | 跳过官方总结与平台字幕，直接本地转写（可用于交叉验证平台内容） |
| `--asr-model` / `--asr-device` / `--asr-lang` | 模型、设备、语言（默认 `large-v3-turbo` / `auto` / `zh`） |

> **红线：不要替用户安装依赖，也不要替用户下载模型。**
> `faster-whisper` 是可选第三方包；模型权重默认从 HuggingFace 下载（`large-v3-turbo` 约 1.6 GB），
> 属于"出网 + 占磁盘"的操作，必须由用户自己决定。需要时把命令交给他：
>
> ```powershell
> pip install faster-whisper        # 或在本仓库里： pip install -e ".[asr]"
> pip install -e ".[asr-gpu]"       # 只有要用 GPU 才需要（约 1.37 GB，NVIDIA 专有再分发）
> ```
>
> 若用户的机器连不上 `huggingface.co`，工具会**自动改用 `hf-mirror.com`** 并在 stderr 说明；
> 想走官方源就自己设 `HF_ENDPOINT`。模型缓存在 `BILIEX_ASR_CACHE`
> （默认 `%USERPROFILE%\.bilibili-ex\models`）。

性能参考（本机实测，19:03 的视频）：`large-v3-turbo` + **GPU** 约 **114 秒（10× 实时）**、
显存约 2.4 GB；同一模型在 CPU 上会慢很多。所以**别急着把 `--asr-device` 钉成 `cpu`** ——
先跑 `asr status` 看 `CUDA 运行时` 那行；GPU 用不上时工具会自动退回 CPU 并给出告警，
不会失败。想确认 GPU 到底能不能用，用 `python tools/cuda_dll_check.py`
（**不要**用 `ctypes` 探 —— 见 `references/troubleshooting.md` 第 6 节）。

## 三、读产物并生成总结

读 `index_file`（即 `out/<bvid>/index.md`）。它已内含元数据、命中级别、官方摘要与提纲、
分块清单，以及给 agent 的总结要求。若转录很长，再按需读 `p<N>-chunks/C##.md`。

**明确区分两种数据来源，并在回答里如实声明：**

- `official_ai_summary` —— 摘要与提纲**由 B 站生成**。直接采用或润色即可，
  时间戳可直接引用。**不要把它说成是你总结的。**
- `subtitle` —— 只有平台字幕转录，需要你自己总结。
- `local_asr` —— 素材是**本机转写**的（faster-whisper 在本机跑）。不是平台字幕，
  也不是 B 站摘要；可能有同音字与断句误差，引用原文时要说明这是转写文本。
- `none` —— 该视频没有可用内容，只有标题/简介。如实告知用户，不要编造内容。

### ⚠️ 字幕可信度：L2 可能是别的视频的内容

这是**实测确认**的上游缺陷，不是理论风险：对同一个 cid，`player/v2` 的 AI 字幕接口
可能返回**完全属于另一个视频**的字幕（实例：标题讲新疆农业，字幕却在讲"顶层设计"，
连拉三次分别得到 155 / 428 / 无字幕轨，内容互不相干）。而 `conclusion/get`（L1）
的关联是正确的。

因此**信任等级不同**：

| 级别 | 信任度 | 依据 |
|---|---|---|
| `official_ai_summary` | **可信** | 摘要、提纲、AI 字幕三者关联一致，已与标题人工核对 |
| `subtitle`（`unstable=false` 且 `low_relevance=false`） | 基本可信，仍需扫一眼 | 通过双读一致性 + 覆盖率 + 相关性三重校验 |
| `subtitle`（`unstable=true` 或 `low_relevance=true`） | **不可信** | **不要据此生成总结** |
| `local_asr` | 来源清楚，但质量取决于音频 | 本机从播放流转写，不存在上游"串号"；`unstable` 为真时说明音频不完整或覆盖率不足 |

三重校验分别挡住不同的失败模式，缺一不可：

1. **时长覆盖率**挡「被截断」（实测有 6 条 vs 373 条的案例）；
2. **双读一致性**挡「每次请求返回不同内容」；
3. **标题相关性**挡「每次都错、但错得一致」（前两条挡不住这种 —— 实测 coverage 100%、双读一致，内容却完全是另一个视频）。

当 `unstable` 或 `low_relevance` 为真时：**不要拿它生成总结**。改为
① 看同分 P 是否有官方 AI 总结可用；② 如实告诉用户「该视频无法获取可靠内容」。
**绝不要把错配的内容包装成这个视频的总结** —— 那比没有总结更糟。

**总结的硬性要求：**

1. **只依据给定文本**，不引入外部知识；不确定就写「原文未说明」，不要补全。
2. 每条要点带 `[MM:SS]` 时间戳。
3. 不写「视频中提到」「作者认为」这类归因前缀，直接陈述内容。
4. 每条要点至少 2-3 句。
5. 某节在原文里确实没有内容就**省略该节**，不要为凑结构而编造。
6. 结构：一句话概括 → 总体摘要 → 话题章节（带时间戳）→ 关键引用 → 新颖/反直觉观点 →
   方法论 → 关键数据。
7. 转录很长时：先做块级摘要，再归并；归并只做合并与排序，不重新自由发挥，以免丢时间戳与章节结构。

最后告诉用户产物目录路径，以及**本次命中的是哪一级、来源是什么**。

## 四、失败与降级处理

按退出码判断（`--json` 时看 `error.code`）：

| 退出码 | `error.code` | 含义 | 该怎么办 |
|---|---|---|---|
| 0 | — | 成功 | 继续 |
| 2 | — | 用法/输入错误 | 检查 BV 号或链接是否合法 |
| 3 | `not_authenticated` / `credential_missing` | 未登录或登录态过期 | 让用户重跑 `auth set` |
| 4 | `risk_control` / `rate_limited` | 被风控或限流 | **等待 30-60 秒再重试**；不要连续猛刷 |
| 5 | `content_unavailable` | 无可用内容 | 如实告知，不要编造总结 |
| 6 | `optional_component_missing` | 要求了本地 ASR（`--asr on` / `--asr-force`）但没装 | 把安装命令给用户，或改回 `--asr off` / `--asr auto` |
| 1 | `upstream_changed` 等 | 上游接口可能已变 | 报告错误码，不要自行改动工具 |

已知的间歇性现象（不要误判为工具坏了）：

- 同一接口可能**先成功、后返回 HTTP 412**，属于风控，间隔一会儿重试即可。
- 播放器接口未登录时 `subtitles` 恒为空数组 —— 这是「没登录」，不是「该视频没字幕」。
  工具已做 fail-fast，会明确区分二者。

## 五、参考文件

- `references/output-format.md` —— 内容包的完整产物结构与字段说明。
- `references/troubleshooting.md` —— 环境问题（Python 找不到、编码乱码、安装到别的机器）。
