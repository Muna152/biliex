# biliex

从 B 站视频提取**官方 AI 总结**与**字幕**，产出带时间戳的结构化内容包，交给 agent 做总结。

设计取舍：**工具只负责"取内容 + 分块"，不接任何 LLM API。** 这样零 API Key、零调用费用，
转录内容也不出本机 —— 总结由你手边的 agent 完成。

**核心零第三方依赖**，只用 Python 标准库。唯一可选的东西是**本地 ASR**（L3）：
给没有字幕的视频做本机转写，需要额外装 `faster-whisper`。
**不装它，其它功能一行都不受影响**（见「可选组件」一节）。

---

## 为什么是这套设计

调研阶段（笔记未随仓库发布）得到四条决定性事实，直接决定了实现方式：

1. **B 站有一个被普遍忽略的官方捷径**：`/x/web-interface/view/conclusion/get` 一次返回
   `summary`（整段摘要）+ `outline[]`（分段标题/要点/**时间戳**）+ AI 字幕（**逐句起止时间戳**）。
   等于 B 站把 ASR 与分段摘要都做完了，不必自建。
2. **字幕与 AI 总结都必须登录**（需 `SESSDATA`）。未登录时 `subtitles` 恒为空数组，
   而且**失败得很安静** —— 不会报错，只是拿不到东西。
3. **风控按接口分级**：`view` / `player` 裸请求可通；列表/排行类直接 `-352` 或 HTTP 412。
   签名类接口（含 `conclusion/get`）必须带 WBI 签名。
4. **批量拉字幕会遇到「假残缺」**：返回的字幕内容被截断（实测有 6 条 vs 单独重请求 373 条的案例）。

因此：**四级降级链 + 假残缺防护 + 单一网络适配层**。

---

## 安装

**零第三方依赖**，只用 Python 标准库。要求 Python ≥ 3.10。

启动器 `biliex.cmd` 会**自动找 Python**，通常什么都不用配。解析顺序（命中即停）：

1. 当前进程环境变量 `BILIEX_PYTHON`
2. **注册表 `HKCU\Environment` 里的 `BILIEX_PYTHON`**
3. PATH 上的 `py`
4. PATH 上的 `python`
5. 常见用户级安装位置（`%LOCALAPPDATA%\Python\pythoncore-*`、`%LOCALAPPDATA%\Python\bin`、`%LOCALAPPDATA%\Programs\Python\Python*`）

> 第 2 条是必需的：`setx` **只对新开的进程生效**。若在同一个窗口里刚 `setx` 就运行启动器，
> 进程环境里读不到，会误报 `Python not found`。直接读注册表就绕过了这个坑。

只有在自动探测选错解释器时，才需要显式指定：

```bat
setx BILIEX_PYTHON "E:\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
```

**然后新开一个终端**再验证（`setx` 写的是用户环境变量，已被缓存的进程读不到；
不过启动器会读注册表，所以同窗口重试通常也能用）：

```bat
cd /d F:\dev\Projects\bilibiliEX
biliex.cmd --version
```

应输出 `biliex 0.2.0`。PowerShell 里当前目录的程序要加 `.\`：`.\biliex.cmd --version`。

> `biliex.cmd` **刻意保持纯 ASCII** —— `cmd.exe` 按 OEM 代码页（简中为 GBK）解码 `.cmd`，
> 里面的中文会被解成乱码并直接破坏批处理语法（这一点是实测踩出来的）。
>
> 本机已确认可用的解释器：DSH 运行时自带的 Python，以及系统自带的
> `%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe`（3.14.3，OpenSSL 3.0.18，实测联网正常）。

---

## 使用

### 1. 设置登录凭据（只做一次）

```bat
biliex.cmd auth set
```

会**隐藏回显**地提示你粘贴 `SESSDATA`（不会进命令历史）。

获取方式：浏览器登录 B 站 → F12 → Application → Cookies → `https://www.bilibili.com`
→ 复制 `SESSDATA` 的值。

> `SESSDATA` 是你账号的登录凭据。它会保存到 `%USERPROFILE%\.bilibili-ex\credential.json`
> （**不在项目目录里**，不进 git，不会被本工具打印或写进任何产出文件）。

检查状态：

```bat
biliex.cmd auth status          :: 会联网校验登录是否仍然有效
biliex.cmd auth status --offline
biliex.cmd auth clear
```

### 2. 抓取内容包

```bat
biliex.cmd fetch BV1VCHY6BEcn
biliex.cmd fetch "https://www.bilibili.com/video/BV1VCHY6BEcn?p=2"
biliex.cmd fetch https://b23.tv/xxxxxxx
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--page all\|N` | 处理全部分 P（默认）或指定分 P |
| `--out DIR` | 产出目录（默认 `./out`） |
| `--max-chars N` | 每块最大字符数（默认 6000） |
| `--overlap N` | 块间重叠字符数（默认 400） |
| `--no-official` | 跳过官方 AI 总结，直接从字幕取 |
| `--asr off\|auto\|on` | **可选组件**：本地 ASR（默认 `off`，见下节） |
| `--json` | 输出机器可读 envelope，便于脚本/agent 调用 |

### 3. 交给 agent 总结

把生成的 `out/<bvid>/index.md` 交给 agent。它已经内含：

- 数据来源与**命中级别**（明确区分"B 站自己总结的"、"平台字幕"和"本机转写的"）
- B 站官方摘要与分段提纲（若命中 L1）
- 分块清单与各分块文件
- **给 agent 的总结要求**（强制带时间戳、不得编造、无内容即省略）

---

## 可选组件：本地 ASR（L3）

**它解决什么问题**：有些视频既没有官方 AI 总结、也没有任何字幕轨。
这时核心路径只能拿到元数据（L4），而 L3 能在本机把音频转写成文字。

**它为什么是可选的**：核心路径刻意零第三方依赖（安装、审计、搬运都简单）。
本地 ASR 必然要引入 `faster-whisper`（连带 `ctranslate2` / `av` / `onnxruntime`），
所以它被完全隔离在两个文件里，并且是**惰性导入**：

| 文件 | 是否需要第三方 |
|---|---|
| `biliex/audio.py` | **不需要**（取播放地址 + 下载音频，纯标准库） |
| `biliex/asr.py` | 需要 `faster-whisper`；**没装时只报告不可用，不抛异常** |

### 安装

建议装在**独立虚拟环境**里，再让启动器指向它的解释器 —— 这样不会污染系统 Python：

```powershell
cd F:\dev\Projects\bilibiliEX
python -m venv .venv-asr
.\.venv-asr\Scripts\python.exe -m pip install faster-whisper
# 或者在本仓库里直接： .\.venv-asr\Scripts\python.exe -m pip install -e ".[asr]"

# 要用 GPU 再加一层（约 1.37 GB，纯 CPU 用户不需要）：见「GPU 与性能」
.\.venv-asr\Scripts\python.exe -m pip install -e ".[asr-gpu]"
setx BILIEX_PYTHON "F:\dev\Projects\bilibiliEX\.venv-asr\Scripts\python.exe"
```

> 国内装上面这些包请配镜像源。`pypi.org` 直连实测只有 **0.01 MB/s**；
> 单个 700 MB 级的轮子还可能让 `pip/uv` 卡死，详见「GPU 与性能」。

之后**新开一个终端**，检查状态（未安装时这条命令同样可用，会直接给出安装命令）：

```bat
biliex.cmd asr status
```

### 用法

```bat
:: 只在平台内容不可信时才转写；没装就照常降级（推荐日常使用）
biliex.cmd fetch BV1VCHY6BEcn --asr auto

:: 要求必须可用；没装则以退出码 6 失败并给出安装命令
biliex.cmd fetch BV1VCHY6BEcn --asr on

:: 跳过官方总结与平台字幕，直接本地转写（用于交叉验证平台内容）
biliex.cmd fetch BV1VCHY6BEcn --asr-force

:: CPU 机器换小模型；保留音频；指定语言
biliex.cmd fetch BV1VCHY6BEcn --asr auto --asr-model small --keep-audio --asr-lang zh
```

L3 的三条完整性判据（与 L2 的「三重校验」对应）：

| 判据 | 挡住的失败模式 |
|---|---|
| 音频字节数与 `Content-Range` 总长度一致 | 音频下载不完整 |
| 转写覆盖率 ≥ 90% | 音频被截断或模型提前结束 |
| 音频时长 vs 视频时长 | 取到的音频比视频短 |

判据不过就标记 `unstable` 并在 `index.md` 顶部写明 —— 绝不产出一份"看起来正常、实际缺一半"的转录。

### 模型权重从哪来

实测（2026-10-07，本机）：

| 源 | 结果 |
|---|---|
| `huggingface.co` | **不可达**（TLS 握手超时） |
| `hf-mirror.com` | 一次成功（27s / 2KB），随后连续 6 次在 ~21s 被对端重置（`WinError 10054`） |
| `modelscope.cn` | **稳定 0.3-0.4s**，Systran 与 turbo 的 CT2 权重都有镜像 |

所以默认顺序是 **ModelScope → HuggingFace（含自动镜像）**：

- `--asr-source auto`（默认）：先 ModelScope，探不通再交回 HuggingFace；
- `--asr-source modelscope`：只走 ModelScope，不可达就**报错**（不静默换源）；
- `--asr-source huggingface`：只用 HuggingFace（`HF_ENDPOINT` 未设置时自动套 `hf-mirror.com`，
  并把 `HF_HUB_*_TIMEOUT` 放宽到 30/60 秒、关掉 `hf-xet` —— 默认 10 秒超时会直接把慢镜像判死）。

权重缓存在 `%USERPROFILE%\.bilibili-ex\models\`，可用 `BILIEX_ASR_CACHE` 改到别的盘。
已经在 HF 缓存里的模型也可以直接把目录传给 `--asr-model <目录>`。

### GPU 与性能

- `--asr-device auto`（默认）识别到 CUDA 设备就用 `float16`，否则用 CPU 的 `int8`；
- **GPU 失败会自动用 CPU 重跑**，并把原因写进告警；
- 不装系统 `ffmpeg` 也能用：faster-whisper 通过 PyAV 解码，已自带 FFmpeg 库。

#### 用上 GPU 的完整路径（本机实测跑通）

```powershell
.\.venv-asr\Scripts\python.exe -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"
# 约 1.37 GB。装完**不需要手工配 PATH** —— biliex 自己会处理（见下）。
# 装完想确认能不能真的用：
.\.venv-asr\Scripts\python.exe tools\cuda_dll_check.py
```

**版本不能换**：`ctranslate2.dll` 里**写死了**动态加载 `cublas64_12.dll`（CUDA 12），
而轮子自带的 `ctranslate2/cudnn64_9.dll` 只是个转发层，会再去拉
`cudnn_{ops,cnn,adv,graph}64_9.dll`。所以是 **CUDA 12 的 cuBLAS + cuDNN 9** 这个组合，
换成 CUDA 13 的 `cublas64_13.dll` 顶不上。

**装完还差一步，也是最容易踩的一步**：Windows **不搜** `site-packages/nvidia/*/bin`。
`biliex/asr.py` 的 `ensure_cuda_dll_dirs()` 会自动把那几个目录**前插到 `PATH`**。
为什么非得是 PATH：

| 办法 | ctranslate2 真用的 `LoadLibraryA` 能不能成功 |
|---|---|
| 什么都不做 | ❌ `ERROR_MOD_NOT_FOUND (126)` |
| `os.add_dll_directory(dir)` | ❌ **仍然 126**（它救不了 `LoadLibraryA`） |
| 目录前插 `PATH` | ✅ 成功 |

> ⚠️ **别用 `ctypes.WinDLL("cublas64_12.dll")` 来判断修好没有。**
> 它走的是 `LoadLibraryExW(..., LOAD_WITH_ALTERED_SEARCH_PATH)`，**认** `add_dll_directory`，
> 与 `LoadLibraryA` 结论可以完全相反；而且它会把 DLL **先加载进进程**，
> 于是"根本没配好"也会显示成成功。判据请用 `tools/cuda_dll_check.py`（内部就是 `LoadLibraryA`）。

#### 实测性能（RTX 3070 Ti Laptop 8GB / 驱动 591.86；同一视频 19:03）

| 模型 | 设备 | 精度 | 耗时（解码+转写） | 相对实时 | 条数 / 覆盖率 |
|---|---|---|---|---|---|
| `tiny` | cpu | int8 | 191.7s | 6.0× | 751 / 99.8% |
| `tiny` | cuda | float16 | 83.3s | 13.7× | 808 / 99.8% |
| **`large-v3-turbo`（默认）** | **cuda** | **float16** | **114.1s** | **10.0×** | 709 / 99.8% |

显存：`large-v3-turbo` float16 实测**约 2.4 GB**（整卡 `used` 峰值 4210 MiB，
扣掉桌面占用的约 1.8 GB），8 GB 卡余量充足。

质量上 `large-v3-turbo` 明显强于 `tiny`，同一段音频：
官方 AI 字幕「新疆竟然有海传出去 / 有红海 / 海南海 / **羊**也不太正常」，
`tiny` 转成「新疆这个人有海 / 有红海 / 南海 / **洋夜**不太正常」，
`large-v3-turbo` 转成「**新疆竟然有海** / 有红海 / 蓝海 / **洋面**不太正常」。
**`tiny` 只适合验证链路，正式使用请用默认的 `large-v3-turbo`。**

#### 已知的版本坑（都是实跑踩出来的）

| 坑 | 症状 | 处理 |
|---|---|---|
| **PyAV ≥ 19 与 faster-whisper 1.2 不兼容** | 解码音频时 `TypeError: open() got an unexpected keyword argument 'metadata_errors'` | `pip install "av<19"`；本仓库的 `[asr]` extra 已卡 `av>=12,<19`，`asr status` 也会提前判出来 |
| 缺 CUDA 12 cuBLAS / cuDNN 9 | `get_cuda_device_count()` 返回 1、精度列表也正常，**推理时**才报 `Library cublas64_12.dll is not found` | 装 `[asr-gpu]`；`ensure_cuda_dll_dirs()` 自动前插 PATH；失败仍会自动退回 CPU |
| `asr status` 显示"已注册"但 GPU 照样退回 CPU | 只调了 `os.add_dll_directory` —— 它救不了 ctranslate2 用的 `LoadLibraryA` | 前插 `PATH` 为主（见上）；判据用 `tools/cuda_dll_check.py` |
| 大轮子下载卡死 | `uv pip install nvidia-cudnn-cu12`（743 MB）连续 4 次超时；换源后卡在 481.8 MB 零增长 | 源没问题（腾讯/清华实测 9–11 MB/s，pypi.org 直连仅 0.01 MB/s），是客户端对单个大请求的处理：用支持 Range 断点续传的下载器（`curl -C -` 或 `wget -c`）取 wheel，再 `pip install --no-index --find-links <目录>` |
| hub 默认 10 秒超时 | 慢镜像直接 `ConnectTimeout`，表现成"模型加载失败" | 自动放宽到 30/60 秒，并关掉镜像不代理的 `hf-xet` |
| `tempfile.mkdtemp()` 建出的目录连本进程都进不去 | `Permission denied`（写入/列举/删除全失败），且**与目录位置无关** | 根因是 **mode 0o700**：CPython 在 Windows 上对它构造**不继承父目录**的受保护权限，DSH 沙箱的继承式授权进不去。改用显式 `mkdir`（`0o750/0o755/0o777` 都正常）。见 `tests/test_asr.py` 里的对照实验 |

### 隐私与成本声明

- 音频只在**本机**处理，转写不上传任何服务器；转写完**默认删除**音频文件（`--keep-audio` 可保留）；
- 下载音频时**不带 Cookie**（CDN 不属于 `bilibili.com`，URL 自带短期签名），
  落档的 `raw/p<N>-playurl.json` 里也**不含**这些签名 URL；
- 首次使用会**从网络下载模型权重**（默认 `large-v3-turbo` 约 1.6 GB）。这是唯一的额外出网行为。

---

## 做成 Skill 使用（推荐）

仓库内 `skill/bilibili-summary/` 是一份 **Agent Skill**，让 agent 在你说「总结这个视频」时
自动完成「调用本工具 → 读内容包 → 产出结构化总结」。它也告诉 agent 几条红线
（不得索要 SESSDATA、不得把 B 站生成的摘要说成自己总结的、不得编造缺失内容）。

### DSH 的 Skill 搜索路径

| Rank | 来源 | 路径 |
|---|---|---|
| 100 | project-dsh | `<项目根>/.dsh/skills` |
| 200 | project-agents | `<项目根>/.agents/skills` |
| 300 | custom | 插件配置 `customSkillDirs` |
| 400 | user-dsh | `<DSH_HOME>/skills` |
| 500 | user-agents | `~/.agents/skills` |

`<项目根>` = 含 `.git` 的最近祖先目录，没有则用 cwd。**`~/.agents/skills` 是跨 agent 共享位**
（其它兼容该约定的 agent 工具也读它），所以安装到这里最通用。

格式约束（来自随包文档，实测确认）：

- 只支持 `<名字>/SKILL.md` 目录 bundle，或平铺 `<名字>.md`；**不支持嵌套的 `**/SKILL.md`**；
- frontmatter 必填 `name`（**kebab-case**）与 `description`，可选 `whenToUse`、`metadata`、
  `disable-model-invocation`、`user-invocable`；
- 根目录被监视，**新增/改名/删除 Skill 无需重启 DSH** 即可进入下一次会话目录（已实测）。

### 安装 / 更新

源在仓库里，安装到共享根（改完源后重新执行一次即可同步）：

```powershell
$src = 'F:\dev\Projects\bilibiliEX\skill\bilibili-summary'
$dst = Join-Path $env:USERPROFILE '.agents\skills\bilibili-summary'
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\*" $dst -Recurse -Force
```

> 刻意做成「仓库内是源、共享根是安装副本」，而不是让 Skill 直接指向仓库 ——
> 这样 Skill 与工具可以一起复制到别的机器，不依赖某个固定盘符。
> 缺点是要记得同步；改动频繁时可改用目录联接（junction）指向仓库内目录。

用法就变成一句话：

> 「总结一下 https://www.bilibili.com/video/BVxxxxxxxxxx」

agent 会自行判登录态、抓内容、读 `index.md`、产出带时间戳的总结，并声明数据来源是哪一级。

---

## 产出结构

```
out/<bvid>/
  index.md            入口：元数据 + 命中级别 + 官方摘要 + 分块清单 + 总结要求
  meta.json           结构化元数据与各级命中情况
  p<N>-transcript.md  分 P 完整转录（每行带 [MM:SS]）
  p<N>-chunks/C##.md  分块文件（带时间范围，块间有重叠）
  raw/                上游原始 JSON（证据留档）
    view.json
    p<N>-player.json
    p<N>-conclusion.json
```

`raw/` 里的 `player.json` 在落盘前**已剔除**你的公网 IP 与登录标识哈希。

---

## 降级链与命中级别

| 级别 | 路径 | 前置条件 |
|---|---|---|
| **L1** | `view/conclusion/get` 官方 AI 总结 | SESSDATA + WBI 签名 |
| **L2** | `player/v2` → 字幕 JSON | SESSDATA |
| **L3** | 本机 ASR：`playurl` 取音频 → `faster-whisper` 转写 | **可选组件**（需额外安装）+ SESSDATA |
| **L4** | 仅元数据 | 无（免登录） |

L2 的中文字幕选择顺序：人工 CC 中文 > AI 中文字幕 > 其它中文 > 任意。
**人工字幕优先于 AI 字幕**（同等语言下），因为人工字幕质量更可靠。

L3 与前三级有一处本质差别：它不只是"再兜一层"，而是能**纠正**上游错误 ——
当 L2 的字幕没通过三重校验时，L3 会用本机转写**替换**它，并在产物里注明"平台字幕已被替换"。

---

## 安全约定

- 凭据只存用户目录，**绝不进项目目录、绝不进 git**；
- `Cookie` 的 `repr()`/`str()` 已打码，避免误打印；
- 凭据只发给 `*.bilibili.com`；**字幕 CDN 请求刻意不带 Cookie**；
- 出错时只上报稳定错误码，不透传上游原始响应体；
- 单元测试里有一组**安全不变量测试**专门守卫这几条。

---

## 验证状态（诚实标注）

| 项 | 状态 | 证据 |
|---|---|---|
| 离线单元测试 | ✅ **114 项通过**（核心 54 + L3 60） | `python -m unittest discover -s tests -t .`；L3 的用例**不装 faster-whisper 也能跑** |
| **WBI 签名实现** | ✅ **真实联网验证通过** | `biliex diag wbi`：未签名被拒（HTTP 412），签名通过并返回真实数据 |
| `view` 元数据接口 | ✅ 真实联网验证 | 免登录可通 |
| `nav` 取 WBI 密钥 | ✅ 真实联网验证 | 未登录返回 `-101` 但 `data.wbi_img` 可用（据此修正了实现） |
| 未登录时的降级行为 | ✅ 真实联网验证 | 0.6 秒快速失败，明确提示「需要登录」，不误报为「该视频没字幕」 |
| 隐私字段剔除 | ✅ 已核对落盘产物 | `raw/` 中无 `ip_info` / `login_mid_hash` / 公网 IP |
| **L1 官方 AI 总结** | ✅ **真实联网验证通过（带登录态）** | `BV1VCHY6BEcn`：命中 `official_ai_summary`，593 条 AI 字幕、覆盖率 99.7%，摘要与提纲和视频内容一致 |
| **L2 字幕路径** | ✅ **真实联网验证通过** | 同一视频强制 `--no-official` 走通；`BV1cwHa6mEPH` 分 P1 走通 |
| **上游串号缺陷的拦截** | ✅ **真实联网验证通过** | 见下 |
| 「假残缺」防护 | ✅ 真实触发并拦截 | `BV1cwHa6mEPH` 分 P2：覆盖率仅 20.2%，被判不可信 |
| **L3 本地 ASR（端到端）** | ✅ **真实转写通过** | `BV1VCHY6BEcn`（19:03）：**114 秒 / GPU float16 / large-v3-turbo**，709 条、覆盖率 99.8%，约 10× 实时 |
| **L3 的 GPU 路径** | ✅ **真实跑通（不再退回 CPU）** | 装 `nvidia-*-cu12` 两个轮子 + `ensure_cuda_dll_dirs()` 前插 PATH；判据 `tools/cuda_dll_check.py` 从 `err=126` 变为 `OK` |
| L3 的 GPU 兜底 | ✅ 真实触发并兜住 | 修复前本机报 `cublas64_12.dll is not found` → 自动改用 CPU 重跑成功，告警如实写入产物 |
| L3 的权重来源 | ✅ 真实下载验证 | 先 `Systran/faster-whisper-tiny`（75 MB），后 `pengzhendong/faster-whisper-large-v3-turbo`（1546 MB）；HF 与 hf-mirror 均不可用/不稳 |
| GPU 显存实测 | ✅ 真实采样 | `nvidia-smi` 采样：turbo 整卡峰值 4210 MiB（扣掉桌面约 1.8 GB → 本进程约 2.4 GB） |
| PyAV 版本冲突拦截 | ✅ 真实触发并拦截 | av 19.0.1 解码报 `metadata_errors`；`asr status` 提前判出并给出 `pip install "av<19"` |
| L3 不越权介入 | ✅ 真实对照验证 | 平台字幕这次**是好的**（正确内容 / 双读一致 / 相关性 2/3）→ L3 正确地没有介入，级别仍为 `subtitle` |
| L3 组件未安装 | ✅ 验证 | `asr status` 给出安装命令、`--asr on` 退出码 **6**，核心路径完全不受影响 |
| 音频下载与完整性 | ✅ 真实下载验证 | dash 音轨 `30280` 13.58 MB；`Content-Range` 总长度与落盘字节数一致 |
| 残留音频的续传/416 | ✅ 真实触发并修好 | 曾因上一轮的完整文件报 HTTP 416；修正后按 `written == total` 判完整，或丢弃重下 |

### 实测发现的上游缺陷（重要）

对 `BV1VCHY6BEcn`（《我记得课本里的新疆，不是这样的啊？？》）：

| 路径 | 拿到的内容 | 判定 |
|---|---|---|
| **L1** `conclusion/get` | 「新疆竟然有海……沙漠……农业世界……天山隧道」 | ✅ 与视频一致 |
| **L2** `player/v2` | 「大老师前面视频……巫师……上帝视角……顶层设计者」 | ❌ **完全是另一个视频** |

同一 cid 连拉三次，L2 分别返回 **155 / 428 / 无字幕轨**，内容互不相干 ——
`player/v2` 的 AI 字幕接口存在**内容串号**，而 `conclusion/get` 的关联是正确的
（这一点经人工比对标题确认）。

因此实现了**三重互补校验**（缺一不可，因为它们挡的是不同失败模式）：

| 判据 | 挡住的失败模式 | 实测触发情况 |
|---|---|---|
| 时长覆盖率 ≥ 90% | 内容被截断（「假残缺」） | `BV1cwHa6mEPH` 分 P2：覆盖率 20.2% |
| 双读一致性 | 每次请求返回不同内容 | 同一 cid 出 86 条 vs 245 条 → 判不可信 |
| 标题相关性（内容词零命中） | 每次都错但错得一致 | coverage 100%、双读一致，内容仍是另一个视频 → **`low_relevance: true`** |

第三条是必要的：前两条**挡不住**第三种情况。

> 真跑过程中修掉的三个 bug（均由实测暴露，静态审查看不出来）：
> 1. `nav` 未登录时返回 `code=-101`，但 `data.wbi_img` 仍可用 —— 早期实现按业务码直接报错，
>    导致**完全拿不到 WBI 密钥**；
> 2. **字幕 CDN 的 JSON 没有 `code` 字段**（结构是 `{font_size, body:[...]}`）——
>    按 API 规则解析会把**整条 L2 字幕路径打死**；
> 3. 相关性判据最初用通用 n-gram，被 `是这样` / `这样的` 这类虚词污染导致**完全漏报** ——
>    改为只让「不含虚词的内容词」参与判定后才真正生效（真实全文上：正确内容命中 2/3，错误内容 0/3）。

退出码：`0` 成功 / `1` 未分类 / `2` 用法错误 / `3` 未登录 / `4` 风控或限流 /
`5` 无可用内容 / `6` 要求的可选组件没装。

---

## 已知限制

- **无字幕、也无官方 AI 总结的视频只有装了 L3 才能转写**。不装本地 ASR 时只能拿到元数据（L4），
  这是刻意的设计取舍 —— 核心保持零第三方依赖。
- **依赖 B 站私有接口**，随时可能失效。这也是为什么所有网络调用都收在
  `biliex/http.py` + `biliex/api.py` 两个文件里 —— 接口一变只改这一处。
- 2026 年初 B 站曾对 API 文档项目发律师函，`bilibili-API-collect`、`bilibili-api`、
  `BBDown` 均已归档清空。本工具**不依赖这些仓库**，只作知识来源。
- 批量抓取会触发限流，请控制频率。
- L3 只处理 **dash 音轨**（`fnval=16`）。老格式的多段 `durl` 会被明确拒绝，而不是拼出一份缺内容的结果。
- 转写质量取决于音频与模型：`tiny`/`base` 对中文只能算"能看出大意"，正式使用建议 `small` 以上。

---

## 许可

**MIT**，全文见 `LICENSE`。

为什么选 MIT：核心零依赖、下载与转写都是自己写的，**没有 GPL 传染**；结构上参考过的那几个项目
（`Yotsuki2213/BiliBili_VideoRead_MCP`、`Cansiny0320/bilibili-video-summary-agent`、
`xiapuyang/youtube-summary`）本身也都是 MIT，许可一致最干净；同时 MIT 带有完整的
免责与责任限制条款 —— 对"依赖平台私有接口"这类风险是必要的。

打包元数据用 PEP 639 写法（`license = "MIT"` + `license-files = ["LICENSE"]`），
所以 `[build-system]` 要求 `setuptools>=77`。生成出的元数据**实测**为：

```
License-Expression: MIT
License-File: LICENSE
```

> 踩坑记录：用了 SPDX 表达式之后**不能再写** `License :: OSI Approved :: MIT License` 分类器，
> setuptools 会直接报 `InvalidConfigError` —— 这个错误只在构建时才暴露，不是写的时候。

可选组件的依赖许可（用 `importlib.metadata` 在本机实测）：
`faster-whisper` MIT、`ctranslate2` MIT、`PyAV` BSD-3-Clause、`onnxruntime` MIT、
`tokenizers` Apache-2.0、`huggingface-hub` Apache-2.0 —— **全部是宽松许可**。
（调研阶段评估过的 `yutto` 是 GPL-3.0，最终**没有采用**，取音频那一段是本项目自己实现的。）

### 免责声明

- 本工具依赖 B 站**未公开**的接口，接口随时可能变更或失效；
- 仅供**个人学习与研究**使用；下载内容的版权归原作者与平台所有；
- 请勿用于批量抓取、二次分发、商业用途或规避平台限制，使用者须自行遵守平台服务条款与当地法律；
- 软件按 MIT 的条款**"原样"提供，不附带任何担保**。

---

## 目录

```
biliex/
  cli.py       命令行入口与编排
  http.py      网络适配层（唯一出口：UA / Cookie / 重试 / 错误码映射）
  api.py       端点封装 + WBI 密钥缓存
  wbi.py       WBI 签名算法
  auth.py      凭据管理
  content.py   四级降级链 + 假残缺防护 + L3 编排（惰性导入可选组件）
  chunk.py     带时间戳的重叠分块
  render.py    产出物渲染
  target.py    输入解析（BV / av / 链接 / 短链）
  errors.py    错误模型
  ── 以下两个文件属于「可选组件：本地 ASR」，不装 faster-whisper 也照常存在 ──
  audio.py     取播放地址 + 下载音频（纯标准库，含断点续传与 416 处理）
  asr.py       faster-whisper 后端（惰性导入 / 能力探测 / 权重来源解析 / CUDA DLL 发现）
tools/
  cuda_dll_check.py  GPU 用不上时的第一诊断：用 LoadLibraryA 判 cuBLAS 能不能真的加载
tests/
  test_biliex.py  核心离线单测（含安全不变量）
  test_asr.py     L3 离线单测（不需要装 faster-whisper 也能跑）
```

### 可选组件的依赖许可（补）

`[asr-gpu]` 的 `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` 是 NVIDIA 的**专有**再分发包
（不是开源许可），只在显式安装该 extra 时才会下载。默认路径（核心 + 纯 CPU 的 `[asr]`）
完全不涉及它们。
