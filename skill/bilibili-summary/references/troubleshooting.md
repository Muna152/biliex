# 环境与故障排查

## 1. 报 `[biliex] Python not found`

启动器 `<BILIEX>\biliex.cmd` 的解析顺序：

1. 进程环境变量 `BILIEX_PYTHON`
2. **注册表 `HKCU\Environment` 里的 `BILIEX_PYTHON`**
3. PATH 上的 `py`
4. PATH 上的 `python`
5. 常见用户级安装位置：`%LOCALAPPDATA%\Python\pythoncore-*`、`%LOCALAPPDATA%\Python\bin`、
   `%LOCALAPPDATA%\Programs\Python\Python*`

第 2 条是必需的：**`setx` 只对新开的进程生效**。若在同一个窗口里刚 `setx` 就运行，
进程环境读不到，会误报找不到 Python。直接读注册表绕过了这个坑。

显式指定解释器：

```bat
setx BILIEX_PYTHON "C:\Python312\python.exe"
```

绕过启动器直接用解释器（最稳的兜底）：

```powershell
& "<python.exe 路径>" -m biliex fetch BV1xxxxxxxxx
```

> 在 `<BILIEX>` 目录下执行时 `-m biliex` 才能找到包；在别处执行需设
> `PYTHONPATH=<BILIEX>`（启动器已自动处理）。

## 2. 中文输出乱码

工具在 **stdout 被管道/脚本捕获**时会强制 UTF-8（因为下游通常按 UTF-8 读）；
连到真实控制台时走 Windows 原生控制台 API，本来就不会乱码。

若终端仍个例乱码，先切 UTF-8 代码页：

```powershell
chcp 65001
```

## 3. 脚本文件编码（改工具时必读）

这两种文件规则**相反**，弄错会直接崩溃：

| 文件类型 | 编码要求 | 原因 |
|---|---|---|
| `.ps1`（PowerShell） | **UTF-8 with BOM** | Windows PowerShell 5.1 会把无 BOM 的 `.ps1` 按 ANSI/GBK 解码，中文乱码并引发语法错误 |
| `.cmd` / `.bat` | **纯 ASCII** | `cmd.exe` 按 OEM 代码页（简中 GBK）读取，UTF-8 中文字符会被解析成乱码并破坏批处理语法 |

`biliex.cmd` 就是把中文注释改成英文后才恢复正常的 —— 这是实测踩出来的。

## 4. 换一台机器部署

1. 复制整个工具目录（**核心**零第三方依赖，只需 Python ≥ 3.10）。
2. 确保能找到 Python：把它加进 PATH，或设 `BILIEX_PYTHON`。
3. 若工具目录不在原位，设环境变量 `BILIEX_ROOT` 指向它 —— Skill 会优先读这个变量。
4. 设好后运行一次 `auth set` 建立登录态。

> 别把 `BILIEX_ROOT`（工具根目录）和 `BILIEX_HOME`（凭据/配置目录，默认
> `%USERPROFILE%\.bilibili-ex`）搞混，两者用途不同。

Skill 的安装位置（DSH 的搜索顺序，rank 越小越优先）：

| Rank | 来源 | 路径 |
|---|---|---|
| 100 | project-dsh | `<项目根>/.dsh/skills` |
| 200 | project-agents | `<项目根>/.agents/skills` |
| 300 | custom | 插件配置 `customSkillDirs` |
| 400 | user-dsh | `<DSH_HOME>/skills` |
| 500 | user-agents | `~/.agents/skills` |

`<项目根>` = 含 `.git` 的最近祖先目录，没有则用 cwd。
Skill 只支持 `<名字>/SKILL.md` 或平铺 `<名字>.md`，**不支持嵌套的 `**/SKILL.md`**；
`name` 必须是 kebab-case。根目录被监视，新增/改名/删除 Skill 无需重启即可生效。

## 5. 已知的上游不稳定现象

| 现象 | 性质 | 处理 |
|---|---|---|
| 同一接口先成功、后 `HTTP 412` | 间歇性风控 | 等 30-60 秒重试，别猛刷 |
| `code: -352` | 风控（列表/排行类接口更严） | 同上 |
| `subtitles: []` 且 `login_mid: 0` | **没登录**，不是没字幕 | 跑 `auth set` |
| `data.code: 1`（官方总结） | 未识别到语音 | 自动降级到字幕路径 |
| `data.code: -1`（官方总结） | 不支持 AI 摘要（敏感内容等） | 自动降级 |
| `coverage` 明显 < 1 | 字幕疑似被截断 | 工具已自动重试；仍低则在回答里提示用户 |

## 6. 本地 ASR（L3，可选组件）出问题

先看状态，它会把"缺什么、怎么装"直接写出来：

```powershell
& "<BILIEX>\biliex.cmd" asr status --json
```

| 现象 | 原因 | 处理 |
|---|---|---|
| `available: false`，原因为"未安装 faster-whisper" | 没装可选组件 | 让用户自己执行 `pip install faster-whisper`（或 `pip install -e ".[asr]"`）；**不要代装** |
| 退出码 6 | 用了 `--asr on` / `--asr-force` 但组件不可用 | 同上；或改回 `--asr auto` / `--asr off` |
| 卡在加载模型很久 | 正在从 HuggingFace 下载权重（默认模型约 1.6 GB） | 属正常；国内网络会**自动走 `hf-mirror.com`**，stderr 会有提示 |
| `huggingface.co` 超时 | 网络不可达（本机实测就是这种情况） | 工具已自动套用镜像；也可自行设 `HF_ENDPOINT` |
| 警告"GPU 加载失败……已退回 CPU" | 缺 CUDA 12 的 cuBLAS / cuDNN 9 的 DLL | 仍能出结果，只是慢（本机实测 GPU 约 10× 实时 vs CPU 约 6×）。要用 GPU：让用户自己装 `pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"`（约 1.37 GB），**不要代装**。装完**不需要手工配 PATH**，`biliex` 自己会处理 |
| `asr status` 说 CUDA 运行时"已注册"，但推理仍退回 CPU | 只注册了 `os.add_dll_directory` —— 它救不了 ctranslate2 内部用的 `LoadLibraryA` | 属于已修复的历史问题；确认版本含 `ensure_cuda_dll_dirs()` 前插 `PATH` 的实现，并用 `python tools/cuda_dll_check.py` 复判 |
| 判断"GPU 修好没有" | **不要**用 `ctypes.WinDLL("cublas64_12.dll")` | 它认 `add_dll_directory`，而 ctranslate2 不认；且会把 DLL 预加载进进程造成假阳性。唯一判据是 `python tools/cuda_dll_check.py`（内部走 `LoadLibraryA`） |
| 转写很慢 | 在 CPU 上用大模型 | 换小模型：`--asr-model small`（或 `base`）；或 `--asr-device cpu --asr-compute-type int8` |
| 显存不够 / 报显存相关错 | `large-v3-turbo` float16 实测约 2.4 GB | 8 GB 卡够用；紧张时用 `--asr-model small` 或 `medium` |
| 磁盘被模型占满 | 权重缓存在 `BILIEX_ASR_CACHE`（默认 `%USERPROFILE%\.bilibili-ex\models`；设了 `HF_HOME` 时以它为准） | 设 `BILIEX_ASR_CACHE` 到别的盘；缓存可安全删除，下次会自动重下 |
| 想保留/删除下载的音频 | 默认转写完就删 | `--keep-audio` 保留（落在 `out/<bvid>/.asr/`） |

> 音频下载**不带 Cookie**（CDN 不属于 bilibili.com，URL 自带短期签名），
> 落档的 `raw/p<N>-playurl.json` 里也**不含**这些签名 URL。

## 7. 安全约定（不要破坏）

- 凭据只存 `%USERPROFILE%\.bilibili-ex\credential.json`，**不进项目目录、不进 git**；
- `Cookie` 的 `repr()` 已打码；任何要打印凭据的地方都要先过 `config.redact`；
- 凭据只发给 `*.bilibili.com`；**字幕与音频 CDN 请求刻意不带 Cookie**；
- 落盘前会剔除 `ip_info` / `login_mid_hash` 等隐私字段；
- 测试里有一组专门守卫这些不变量的用例：
  `tests/test_biliex.py::TestSecurityInvariants` 与
  `tests/test_asr.py::TestAudioSecurityInvariants`。
