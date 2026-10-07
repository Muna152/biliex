"""配置与路径。

凭据放在**用户目录**下，刻意不放在工作区里 —— 避免账号凭据进入项目目录、
被 git 跟踪、或被打包分发。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "biliex"

# 常规浏览器 UA。B 站对非浏览器 UA 的风控更严，这里必须伪装成正常浏览器。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

REFERER = "https://www.bilibili.com/"

# 上游返回码 → 语义（集中定义，风控策略变化时只改这里）
CODE_NOT_LOGGED_IN = -101
CODE_RISK_CONTROL = -352
CODE_PERMISSION_DENIED = -403
CODE_BAD_REQUEST = -400


def config_dir() -> Path:
    """凭据目录：优先 BILIEX_HOME，其次用户目录下的 .bilibili-ex。"""
    override = os.environ.get("BILIEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".bilibili-ex"


def credential_path() -> Path:
    return config_dir() / "credential.json"


def default_out_dir() -> Path:
    """默认产出目录：当前工作目录下的 out/。"""
    return Path.cwd() / "out"


def redact(value: str | None, keep: int = 4) -> str:
    """把凭据之类的东西打码后再展示。

    任何要打印凭据的地方都必须先过这个函数。
    """
    if not value:
        return "<empty>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"
