"""WBI 签名。

来源：bilibili-API-collect 的 `docs/misc/sign/wbi.md`（原仓库已归档，
本文档从存活镜像 gitea.s1f.ren/shiran/bilibili-API-collect 读取）。

要点：
* 2023-03 起部分 Web 接口启用；缺 `w_rid`/`wts` 会返回 `v_voucher` 之类的失败。
* `img_key` / `sub_key` 从 `nav` 接口取，**每日轮换** → 必须缓存，不能每次重算。
* 算法：按 64 位重排表从 `img_key + sub_key` 取前 32 位得 `mixin_key`；
  参数按键名升序 URL 编码，拼上 `mixin_key` 取 MD5 得 `w_rid`。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from dataclasses import dataclass

# 64 位重排表（常量，来自官方文档）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# 计算 w_rid 前必须从参数值里剔除的字符
_FILTER_CHARS = "!'()*"


def get_mixin_key(img_key: str, sub_key: str) -> str:
    """由 img_key + sub_key 推导 mixin_key（取前 32 位）。"""
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _strip_filename(url: str) -> str:
    """从 `.../7cd084941338484aae1ad9425b84077c.png` 取出文件名部分。"""
    return url.rsplit("/", 1)[-1].split(".", 1)[0]


@dataclass
class WbiKeys:
    """一对每日轮换的密钥。"""

    img_key: str
    sub_key: str
    fetched_at: float

    @classmethod
    def from_nav(cls, wbi_img: dict) -> "WbiKeys":
        """从 `nav` 接口的 `data.wbi_img` 构造。"""
        return cls(
            img_key=_strip_filename(wbi_img["img_url"]),
            sub_key=_strip_filename(wbi_img["sub_url"]),
            fetched_at=time.time(),
        )

    def is_fresh(self, ttl_seconds: float = 6 * 3600) -> bool:
        """密钥每日轮换，这里保守地按 6 小时过期。"""
        return (time.time() - self.fetched_at) < ttl_seconds

    @property
    def mixin_key(self) -> str:
        return get_mixin_key(self.img_key, self.sub_key)


def sign_params(params: dict, keys: WbiKeys, *, wts: int | None = None) -> dict:
    """返回带 `wts` 与 `w_rid` 的新参数字典（不修改入参）。"""
    signed = {k: v for k, v in params.items() if v is not None}
    signed["wts"] = int(wts if wts is not None else time.time())

    # 1) 剔除特殊字符；2) 按键名升序；3) URL 编码
    cleaned = {
        k: "".join(ch for ch in str(v) if ch not in _FILTER_CHARS)
        for k, v in signed.items()
    }
    query = urllib.parse.urlencode(sorted(cleaned.items()))
    w_rid = hashlib.md5((query + keys.mixin_key).encode("utf-8")).hexdigest()

    signed["w_rid"] = w_rid
    return signed
