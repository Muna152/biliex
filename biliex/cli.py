"""命令行入口。

输出约定：
* 默认给人看（简洁、要点式）；
* `--json` 给程序/agent 看，统一 envelope：`{ok, schema_version, data|error}`。
* 退出码分层，便于脚本据此判断该重试还是该换路径：
    0 成功 / 1 未分类错误 / 2 用法错误 / 3 未登录 / 4 风控或限流 / 5 无可用内容
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, auth, target as target_mod
from .api import Client
from .chunk import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP_CHARS, chunk_cues, fmt_ts
from .content import LEVEL_OFFICIAL, fetch_page
from .errors import (
    BiliexError,
    ContentUnavailable,
    CredentialMissing,
    NotAuthenticated,
    OptionalComponentMissing,
    RateLimited,
    RiskControl,
)

SCHEMA_VERSION = "1"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_AUTHENTICATED = 3
EXIT_RISK_CONTROL = 4
EXIT_NO_CONTENT = 5
EXIT_OPTIONAL_MISSING = 6


def _configure_output() -> None:
    """让中文输出在被管道/脚本捕获时也不乱码。

    Windows 上 Python 连到**真实控制台**时走 `WriteConsoleW`，中文本来就正常，
    所以那种情况不动它。

    但 stdout 一旦被重定向成管道（脚本调用、agent 读取），Python 会按 ANSI 代码页
    （简中是 GBK）编码，而下游通常按 UTF-8 解码 → 中文全成乱码。
    本工具的主要用途就是把输出交给 agent，所以这种情况统一成 UTF-8。
    """
    try:
        if sys.stdout.isatty():
            return
    except (AttributeError, ValueError):
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _exit_code_for(error: BiliexError) -> int:
    if isinstance(error, (NotAuthenticated, CredentialMissing)):
        return EXIT_NOT_AUTHENTICATED
    if isinstance(error, (RiskControl, RateLimited)):
        return EXIT_RISK_CONTROL
    if isinstance(error, OptionalComponentMissing):
        return EXIT_OPTIONAL_MISSING
    if isinstance(error, ContentUnavailable):
        return EXIT_NO_CONTENT
    return EXIT_ERROR


def _emit(payload: dict, *, as_json: bool, human: str = "") -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif human:
        print(human)


def _ok(data: dict, *, as_json: bool, human: str = "") -> None:
    _emit({"ok": True, "schema_version": SCHEMA_VERSION, "data": data},
          as_json=as_json, human=human)


def _fail(error: BiliexError, *, as_json: bool) -> int:
    _emit(
        {"ok": False, "schema_version": SCHEMA_VERSION, "error": error.to_dict()},
        as_json=as_json,
        human=f"错误 [{error.code}]：{error.message}",
    )
    return _exit_code_for(error)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _scrub(payload: dict) -> dict:
    """落盘前剔除隐私字段。

    `player/v2` 会返回请求方的公网 IP（`ip_info.ip`）与登录标识哈希，
    这些属于本机隐私，**不应留在项目目录里**。
    """
    cleaned = dict(payload)
    cleaned.pop("ip_info", None)
    cleaned.pop("login_mid_hash", None)
    return cleaned


# ---------------------------------------------------------------- auth


def cmd_auth(args: argparse.Namespace) -> int:
    if args.action == "set":
        sessdata = args.sessdata
        if not sessdata:
            if not sys.stdin.isatty():
                sessdata = sys.stdin.read().strip()
            else:
                print(
                    "请粘贴 SESSDATA（浏览器 → 开发者工具 → Application → "
                    "Cookies → bilibili.com → SESSDATA）。输入不回显，也不会进命令历史。",
                    file=sys.stderr,
                )
                sessdata = getpass.getpass("SESSDATA: ").strip()
        if not sessdata:
            return _fail(CredentialMissing("未提供 SESSDATA"), as_json=args.json)

        jct = args.bili_jct or ""
        if not jct and sys.stdin.isatty() and not args.sessdata:
            jct = getpass.getpass("bili_jct（可留空，直接回车）: ").strip()

        cred = auth.Credential(sessdata=sessdata, bili_jct=jct, source="manual")
        path = auth.save(cred)

        verified = _verify_login(cred.to_cookie())
        data = {"saved_to": str(path), "verify": verified}
        human = f"凭据已保存到 {path}\n登录校验：{verified['summary']}"
        _ok(data, as_json=args.json, human=human)
        return EXIT_OK

    if args.action == "status":
        cred = auth.load()
        info = auth.describe(cred)
        if info.get("configured") and not args.offline:
            info["verify"] = _verify_login(cred.to_cookie())
        human = _human_status(info)
        _ok(info, as_json=args.json, human=human)
        return EXIT_OK

    if args.action == "clear":
        removed = auth.clear()
        _ok(
            {"removed": removed},
            as_json=args.json,
            human="已删除本地凭据" if removed else "本地没有凭据，无需删除",
        )
        return EXIT_OK

    return EXIT_USAGE


def _verify_login(cookie) -> dict:
    """调用 nav 接口校验登录态是否仍然有效。"""
    try:
        data = Client(cookie=cookie).nav()
    except BiliexError as exc:
        return {"is_login": False, "error": exc.code, "summary": f"校验失败（{exc.code}）"}
    is_login = bool(data.get("isLogin"))
    if is_login:
        summary = f"已登录，mid={data.get('mid')}，昵称={data.get('uname', '')}"
    else:
        summary = "未登录（SESSDATA 可能已失效或格式不对）"
    return {"is_login": is_login, "mid": data.get("mid"),
            "uname": data.get("uname", ""), "summary": summary}


def _human_status(info: dict) -> str:
    if not info.get("configured"):
        return (
            "未配置凭据。\n"
            "注意：B 站的字幕接口与官方 AI 总结接口都**必须登录**才能访问，"
            "请先运行 `biliex auth set`。"
        )
    lines = [
        f"SESSDATA：{info['sessdata']}（来源：{info['source']}）",
        f"已保存 {info['age_days']} 天" + (f"　文件：{info['path']}" if info.get("path") else ""),
    ]
    if verify := info.get("verify"):
        lines.append(f"校验：{verify['summary']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 本地 ASR（可选组件）


def resolve_asr(args: argparse.Namespace):
    """根据 `--asr` 决定这次要不要走 L3，并给出参数。

    返回 `(settings 或 None, 错误 或 None)`。

    三种模式的区别是**意图**，不是能力：
    * `off`  —— 不碰 L3（默认）。核心路径零第三方依赖。
    * `auto` —— 平台内容不可信时才用；**没装就静默降级**，只提示一句怎么装。
    * `on`   —— 要求 L3 可用；没装就 fail-fast（退出码 6），而不是悄悄给一份降级结果。
    """
    if args.asr == "off" and not args.asr_force:
        return None, None

    from . import asr as asr_mod  # 惰性导入：可选组件不在核心导入路径上

    capability = asr_mod.probe()
    if not capability.available:
        hint = capability.hint
        if args.asr == "on" or args.asr_force:
            return None, OptionalComponentMissing(
                f"要求使用本地 ASR（--asr {args.asr}），但{capability.reason}",
                detail=f"安装方式：{hint}",
            )
        print(f"提示：{capability.summary()}", file=sys.stderr)
        return None, None

    if capability.mirror_applied and (args.asr_source or "auto") == "huggingface":
        print(
            f"提示：huggingface.co 在本机不可达，已自动改用镜像 {capability.hf_endpoint} "
            "下载模型（想走官方源请自行设置 HF_ENDPOINT）。",
            file=sys.stderr,
        )

    settings = asr_mod.AsrSettings.from_env(
        model=args.asr_model,
        device=args.asr_device,
        compute_type=args.asr_compute_type,
        language=args.asr_lang,
        source=args.asr_source,
        keep_audio=args.keep_audio or None,
    )
    return settings, None


def asr_progress_reporter():
    """给 L3 报进度到 stderr（**不污染 stdout**，`--json` 才不会被弄坏）。

    stderr 是给人看的，所以做节流：最多每 3 秒一行。
    """
    state = {"last": 0.0}

    def report(stage: str, done: float, total: float) -> None:
        now = time.monotonic()
        if now - state["last"] < 3.0 and done < total:
            return
        state["last"] = now
        if stage == "download":
            print(
                f"[ASR] 下载音频 {done / 1024 / 1024:.1f}/{(total or done) / 1024 / 1024:.1f} MB",
                file=sys.stderr,
            )
        elif stage == "model":
            print(
                f"[ASR] 下载模型权重 {done / 1024 / 1024:.0f}/{(total or done) / 1024 / 1024:.0f} MB"
                "（首次使用，之后走缓存）",
                file=sys.stderr,
            )
        else:
            print(
                f"[ASR] 转写中 {fmt_ts(done)} / {fmt_ts(total)}",
                file=sys.stderr,
            )

    return report


def cmd_asr(args: argparse.Namespace) -> int:
    """`biliex asr status` —— 报告本地 ASR 这个**可选组件**的状态。

    刻意做成独立子命令：核心功能不受它影响，它的状态也不该混在 `fetch` 的输出里。
    """
    if args.action != "status":
        return EXIT_USAGE

    from . import asr as asr_mod

    capability = asr_mod.probe(refresh=True)
    settings = asr_mod.AsrSettings.from_env()
    ids = asr_mod.repo_ids(settings.model)
    cache_dir = asr_mod.asr_cache_dir()
    weights_dir = cache_dir / ids["modelscope"].replace("/", "__")
    source_info = {
        "source": settings.source,
        "modelscope_repo": ids["modelscope"],
        "modelscope_reachable": asr_mod.check_modelscope(ids["modelscope"]),
        "huggingface_repo": ids["hf"],
        "hf_endpoint": capability.hf_endpoint,
        "cache_dir": str(cache_dir),
        "weights_cached": asr_mod.has_weights(weights_dir),
    }
    data = {
        "capability": capability.to_dict(),
        "settings": settings.to_dict(),
        "weights": source_info,
        "summary": capability.summary(),
    }

    lines = [capability.summary()]
    if capability.available:
        lines.append(f"模型：{settings.model}　设备：{settings.device}　精度：{settings.compute_type}")
        lines.append(
            f"权重来源：{settings.source}（ModelScope "
            f"{'可达' if source_info['modelscope_reachable'] else '不可达'}；"
            f"缓存 {'已有' if source_info['weights_cached'] else '尚无'}）"
        )
        lines.append(f"权重缓存：{cache_dir}")
        if capability.cuda_runtime:
            lines.append(f"CUDA 运行时：已注册 {len(capability.cuda_runtime)} 个目录（来自 pip 的 nvidia-*-cu12）")
        elif "cuda" in capability.devices:
            lines.append(f"CUDA 运行时：**未发现**（用 GPU 会失败）→ {asr_mod.CUDA_INSTALL_HINT}")
        else:
            lines.append("CUDA 运行时：不需要（没有可用 CUDA 设备，走 CPU）")
        lines.append(f"HuggingFace 端点：{capability.hf_endpoint}")
        if capability.mirror_applied:
            lines.append("（huggingface.co 在本机不可达，已自动使用镜像；设 HF_ENDPOINT 可覆盖）")
        lines.append("首次使用会在 `biliex fetch --asr on <BV号>` 时下载权重（默认模型约 1.6 GB）。")
    else:
        lines.append(f"安装命令：{capability.hint}")
        lines.append("不安装也完全不影响 L1/L2/L4 —— 本地 ASR 是可选组件。")
    _ok(data, as_json=args.json, human="\n".join(lines))
    return EXIT_OK


# ---------------------------------------------------------------- fetch


def cmd_fetch(args: argparse.Namespace) -> int:
    try:
        tgt = target_mod.parse(args.target)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    cred = auth.load()
    cookie = cred.to_cookie() if cred else None
    client = Client(cookie=cookie)

    # 可选组件（本地 ASR）在这里一次性决定启不启用 —— 缺它就 fail-fast，不要跑到一半才发现
    asr_settings, asr_error = resolve_asr(args)
    if asr_error is not None:
        return _fail(asr_error, as_json=args.json)

    if tgt.is_short_link:
        try:
            resolved = client.resolve_short_link(tgt.short_code)
        except Exception as exc:  # noqa: BLE001
            return _fail(BiliexError(f"短链展开失败：{exc}"), as_json=args.json)
        try:
            tgt = target_mod.parse(resolved)
        except ValueError as exc:
            return _fail(BiliexError(f"短链指向无法识别：{exc}"), as_json=args.json)

    try:
        view = client.video_view(bvid=tgt.bvid, aid=tgt.aid)
    except BiliexError as exc:
        return _fail(exc, as_json=args.json)

    bvid = view.get("bvid") or tgt.bvid or ""
    pages = view.get("pages") or []
    if not pages:
        return _fail(ContentUnavailable("该稿件没有可播放的分 P"), as_json=args.json)

    if args.page == "all":
        selected = pages
    else:
        wanted = int(args.page)
        selected = [p for p in pages if p.get("page") == wanted]
        if not selected:
            return _fail(
                ContentUnavailable(f"该稿件没有第 {wanted} 个分 P（共 {len(pages)} 个）"),
                as_json=args.json,
            )

    out_root = Path(args.out).expanduser() if args.out else None
    bundle_root = (out_root or Path(args.out_dir_default)) / bvid
    generated_at = _now()
    meta = {
        "bvid": bvid,
        "aid": view.get("aid"),
        "title": view.get("title", ""),
        "desc": (view.get("desc") or "")[:2000],
        "duration": view.get("duration", 0),
        "owner_name": (view.get("owner") or {}).get("name", ""),
        "owner_mid": (view.get("owner") or {}).get("mid"),
        "pubdate": view.get("pubdate"),
        "stat": {
            k: (view.get("stat") or {}).get(k)
            for k in ("view", "danmaku", "reply", "like", "coin", "favorite")
        },
        "page_count": len(pages),
        "processed_pages": [p.get("page") for p in selected],
        "generated_at": generated_at,
        "tool_version": __version__,
        "asr_enabled": asr_settings is not None,
    }
    raw_dir = bundle_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "view.json").write_text(
        json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    from .render import Bundle, write_bundle

    bundle = Bundle(bvid=bvid, root=bundle_root, meta=meta, pages=[])
    chunks_by_cid: dict[int, list] = {}
    asr_progress = asr_progress_reporter() if asr_settings is not None else None

    for item in selected:
        cid = item.get("cid")
        page_no = item.get("page", 1)
        content = fetch_page(
            client,
            bvid,
            cid,
            page=page_no,
            part=item.get("part", ""),
            duration=float(item.get("duration") or 0),
            up_mid=meta["owner_mid"],
            title=meta["title"],
            prefer_official=not args.no_official,
            asr_settings=asr_settings,
            asr_force=args.asr_force,
            asr_dest_dir=bundle_root / ".asr",
            asr_progress=asr_progress,
        )
        if "player" in content.raw:
            (raw_dir / f"p{page_no}-player.json").write_text(
                json.dumps(_scrub(content.raw["player"]), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if "conclusion" in content.raw:
            (raw_dir / f"p{page_no}-conclusion.json").write_text(
                json.dumps(content.raw["conclusion"], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if "playurl" in content.raw:
            # 音轨元数据（**不含** CDN 签名 URL —— 那是几分钟就过期的临时令牌）
            payload = {"playurl": content.raw["playurl"]}
            if "playurl_retry" in content.raw:
                payload["playurl_retry"] = content.raw["playurl_retry"]
            (raw_dir / f"p{page_no}-playurl.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        chunks_by_cid[cid] = chunk_cues(
            content.cues, max_chars=args.max_chars, overlap_chars=args.overlap
        )
        bundle.pages.append(content)

    levels = [p.level for p in bundle.pages]
    meta["level"] = LEVEL_OFFICIAL if LEVEL_OFFICIAL in levels and len(set(levels)) == 1 \
        else (levels[0] if len(set(levels)) == 1 else "mixed")
    meta["subtitle_is_ai"] = all(p.subtitle_is_ai for p in bundle.pages)
    meta["asr_used"] = any(p.asr for p in bundle.pages)
    meta["asr_replaced_subtitle"] = any(p.asr_replaced_subtitle for p in bundle.pages)
    meta["pages"] = [p.to_dict() for p in bundle.pages]

    root = write_bundle(bundle, chunks_by_cid=chunks_by_cid)

    data = {
        "bvid": bvid,
        "title": meta["title"],
        "output_dir": str(root),
        "index_file": str(root / "index.md"),
        "level": meta["level"],
        "asr_used": meta["asr_used"],
        "asr_replaced_subtitle": meta["asr_replaced_subtitle"],
        "pages": [
            {
                "page": p.page,
                "cid": p.cid,
                "level": p.level,
                "cue_count": len(p.cues),
                "coverage": round(p.coverage, 4),
                "unstable": p.unstable,
                "low_relevance": p.low_relevance,
                "relevance": f"{p.relevance_matched}/{p.relevance_total}",
                "is_ai": p.subtitle_is_ai,
                "asr": p.asr.get("info", {}),
                "asr_replaced_subtitle": p.asr_replaced_subtitle,
                "chunks": len(chunks_by_cid.get(p.cid, [])),
                "warnings": p.warnings,
                "fallback_reasons": p.fallback_reasons,
            }
            for p in bundle.pages
        ],
    }

    human = [
        f"《{meta['title']}》",
        f"命中级别：{meta['level']}",
        f"产出目录：{root}",
    ]
    for p in data["pages"]:
        human.append(
            f"  分 P{p['page']}：{p['level']}，字幕 {p['cue_count']} 条，"
            f"覆盖率 {p['coverage']:.1%}，分块 {p['chunks']} 个"
        )
        if p["asr"]:
            human.append(
                f"    🎙️ 由本机 ASR 转写（模型 {p['asr'].get('model')}，"
                f"设备 {p['asr'].get('resolved_device')}，"
                f"耗时 {float(p['asr'].get('elapsed_seconds') or 0):.0f}s）"
            )
        if p["asr_replaced_subtitle"]:
            human.append("    ⚠️ 平台字幕不可信，已用本地转写替换")
        for warning in p["warnings"]:
            human.append(f"    ⚠️ {warning}")
        for reason in p["fallback_reasons"]:
            human.append(f"    · {reason}")
    human.append(f"\n下一步：把 {root / 'index.md'} 交给 agent 总结。")
    _ok(data, as_json=args.json, human="\n".join(human))
    return EXIT_OK


# ---------------------------------------------------------------- diag


def cmd_diag(args: argparse.Namespace) -> int:
    if args.what != "wbi":
        return EXIT_USAGE
    client = Client()
    try:
        result = client.check_wbi_signature(mid=args.mid)
    except BiliexError as exc:
        return _fail(exc, as_json=args.json)

    ok = bool(result["signed_ok"]) and not result["unsigned_error"] is None
    verdict = (
        "WBI 签名实现正确：未签名被拒、签名通过"
        if ok
        else "自检未通过，需要检查签名实现或上游已变更"
    )
    human = "\n".join(
        [
            f"未签名请求结果：{result['unsigned_error']}",
            f"签名请求是否成功：{result['signed_ok']}",
            f"签名请求返回：{result['signed_result'] if not result['signed_ok'] else 'code=0'}",
            f"img_key={result['img_key']}　mixin_key 前 8 位={result['mixin_key_prefix']}",
            verdict,
        ]
    )
    _ok(result | {"verdict": verdict, "passed": ok}, as_json=args.json, human=human)
    return EXIT_OK if ok else EXIT_ERROR


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="biliex",
        description="从 B 站视频提取官方 AI 总结与字幕，产出带时间戳的内容包供 agent 总结。",
    )
    parser.add_argument("--version", action="version", version=f"biliex {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth", help="管理登录凭据（SESSDATA）")
    p_auth.add_argument("action", choices=["set", "status", "clear"])
    p_auth.add_argument("--sessdata", help="直接传入 SESSDATA（更推荐用交互式输入，避免进命令历史）")
    p_auth.add_argument("--bili-jct", help="CSRF token（多数只读接口不需要）")
    p_auth.add_argument("--offline", action="store_true", help="status 时不联网校验")
    p_auth.add_argument("--json", action="store_true")
    p_auth.set_defaults(func=cmd_auth)

    p_fetch = sub.add_parser("fetch", help="抓取一个视频的内容包")
    p_fetch.add_argument("target", help="BV 号 / av 号 / bilibili 链接 / b23.tv 短链")
    p_fetch.add_argument("--out", default="", help="产出目录（默认 ./out）")
    p_fetch.add_argument("--page", default="all", help="分 P：all（默认）或某个序号")
    p_fetch.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                         help=f"每块最大字符数（默认 {DEFAULT_MAX_CHARS}）")
    p_fetch.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP_CHARS,
                         help=f"块间重叠字符数（默认 {DEFAULT_OVERLAP_CHARS}）")
    p_fetch.add_argument("--no-official", action="store_true",
                         help="跳过后端官方 AI 总结，直接从字幕取")
    asr_group = p_fetch.add_argument_group(
        "本地 ASR（可选组件）",
        "需要额外安装 faster-whisper，未安装不影响上面的任何功能。",
    )
    asr_group.add_argument(
        "--asr", choices=["off", "auto", "on"], default="off",
        help="本地 ASR：off 不用（默认）；auto 平台内容不可信时才用、没装就降级；"
             "on 要求可用，没装则报错退出（退出码 6）",
    )
    asr_group.add_argument("--asr-force", action="store_true",
                           help="跳过官方总结与平台字幕，直接本地转写（可用于交叉验证平台内容）")
    asr_group.add_argument("--asr-model", default=None,
                           help="模型（默认 large-v3-turbo；CPU 上建议 small）")
    asr_group.add_argument("--asr-device", default=None, help="auto（默认）/ cuda / cpu")
    asr_group.add_argument("--asr-compute-type", default=None,
                           help="auto（默认，GPU float16 / CPU int8）/ int8 / float16 / int8_float16")
    asr_group.add_argument("--asr-lang", default=None, help="语言，默认 zh；auto 表示自动检测")
    asr_group.add_argument("--asr-source", choices=["auto", "modelscope", "huggingface"],
                           default=None,
                           help="权重来源：auto（默认，先 ModelScope 再 HuggingFace）/ "
                                "modelscope / huggingface")
    asr_group.add_argument("--keep-audio", action="store_true",
                           help="保留下载的音频文件（默认转写后删除）")
    p_fetch.add_argument("--json", action="store_true")
    p_fetch.set_defaults(func=cmd_fetch, out_dir_default="out")

    p_asr = sub.add_parser("asr", help="本地 ASR 可选组件的状态（不影响核心功能）")
    p_asr.add_argument("action", choices=["status"])
    p_asr.add_argument("--json", action="store_true")
    p_asr.set_defaults(func=cmd_asr)

    p_diag = sub.add_parser("diag", help="自检")
    p_diag.add_argument("what", choices=["wbi"])
    p_diag.add_argument("--mid", type=int, default=2, help="用于自检的 UP mid")
    p_diag.add_argument("--json", action="store_true")
    p_diag.set_defaults(func=cmd_diag)

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.json = getattr(args, "json", False)
    try:
        return int(args.func(args))
    except BiliexError as exc:
        return _fail(exc, as_json=args.json)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:  # pragma: no cover
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
