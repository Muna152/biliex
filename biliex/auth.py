"""登录凭据管理。

安全约定（这是本项目最敏感的模块）：
* 凭据只存**用户目录**（默认 `~/.bilibili-ex/credential.json`），绝不进工作区；
* 任何展示都先过 `config.redact`；
* 支持用环境变量 `BILIEX_SESSDATA` 临时注入，避免落盘。
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from . import config
from .errors import CredentialMissing
from .http import Cookie


@dataclass
class Credential:
    sessdata: str = ""
    bili_jct: str = ""
    buvid3: str = ""
    # 记录来源与时间，便于判断是否过期
    source: str = "manual"
    saved_at: float = 0.0
    mid: int | None = None
    uname: str = ""

    def to_cookie(self) -> Cookie:
        return Cookie(
            sessdata=self.sessdata,
            bili_jct=self.bili_jct,
            buvid3=self.buvid3,
        )

    def age_days(self) -> float:
        if not self.saved_at:
            return 0.0
        return (time.time() - self.saved_at) / 86400.0


def load() -> Credential | None:
    """读取凭据。环境变量优先于文件。"""
    env_sessdata = os.environ.get("BILIEX_SESSDATA")
    if env_sessdata:
        return Credential(
            sessdata=env_sessdata,
            bili_jct=os.environ.get("BILIEX_BILI_JCT", ""),
            buvid3=os.environ.get("BILIEX_BUVUID3", ""),
            source="env",
            saved_at=time.time(),
        )

    path = config.credential_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    allowed = {f for f in Credential.__dataclass_fields__}
    return Credential(**{k: v for k, v in data.items() if k in allowed})


def require() -> Credential:
    cred = load()
    if cred is None or not cred.sessdata:
        raise CredentialMissing(
            "本地没有登录凭据。B 站字幕与 AI 总结接口都必须登录后才能访问，"
            "请先运行 `biliex auth set` 写入 SESSDATA。"
        )
    return cred


def save(cred: Credential) -> Path:
    """写入凭据文件，并尽力收紧权限。"""
    path = config.credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cred.saved_at = cred.saved_at or time.time()
    path.write_text(
        json.dumps(asdict(cred), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _harden(path)
    return path


def clear() -> bool:
    path = config.credential_path()
    if path.exists():
        path.unlink()
        return True
    return False


def _harden(path: Path) -> None:
    """尽量把权限收成「仅本人可读写」。

    Windows 上 POSIX 位语义有限，这里只做尽力而为，并在 status 里如实说明。
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def describe(cred: Credential | None) -> dict:
    """给 `auth status` 用的、**已打码**的描述。"""
    if cred is None or not cred.sessdata:
        return {"configured": False}
    return {
        "configured": True,
        "source": cred.source,
        "sessdata": config.redact(cred.sessdata),
        "has_bili_jct": bool(cred.bili_jct),
        "age_days": round(cred.age_days(), 2),
        "path": "" if cred.source == "env" else str(config.credential_path()),
    }
