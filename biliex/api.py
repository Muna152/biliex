"""接口层：B 站各端点的薄封装 + WBI 密钥缓存。

与 http.py 的分工：
* http.py 管"怎么发请求"（UA、Cookie、重试、错误码映射）；
* api.py 管"调哪个端点、参数怎么拼"。

**上游接口一变更，只改这个文件。**
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .errors import UpstreamChanged
from .http import Cookie, RetryPolicy, request_json, request_payload
from .wbi import WbiKeys, sign_params

API = "https://api.bilibili.com"

EP_NAV = f"{API}/x/web-interface/nav"
EP_VIEW = f"{API}/x/web-interface/view"
# 用非 wbi 变体：实测未签名也可通（code=0），少一层依赖就少一个失效点。
EP_PLAYER = f"{API}/x/player/v2"
EP_CONCLUSION = f"{API}/x/web-interface/view/conclusion/get"
# 播放地址（L3 本地 ASR 取音频用）。非 wbi 变体实测同样免签名可通；
# wbi 变体作为兜底 —— 上游一旦收紧，签名这条路先顶上，不至于整个 L3 失效。
EP_PLAYURL = f"{API}/x/player/playurl"
EP_PLAYURL_WBI = f"{API}/x/player/wbi/playurl"
# 仅用于自检 WBI 签名实现：该端点必须签名
EP_SPACE_ARC = f"{API}/x/space/wbi/arc/search"

_SHORT_LINK = "https://b23.tv/{code}"


@dataclass
class Client:
    """持有一次运行期间的 Cookie 与 WBI 密钥缓存。"""

    cookie: Cookie | None = None
    _keys: WbiKeys | None = None

    # ---------- WBI 密钥 ----------

    def wbi_keys(self, *, force: bool = False) -> WbiKeys:
        """取 WBI 密钥，优先用磁盘缓存（密钥每日轮换）。

        注意：`nav` 在未登录时返回 `code=-101`，但 `data.wbi_img` 依然可用 ——
        所以这里必须走 `request_payload`（不解释业务码），而不是 `request_json`。
        用 `request_json` 会导致未登录时**完全拿不到 WBI 密钥**。
        """
        if not force and self._keys is not None and self._keys.is_fresh():
            return self._keys

        if not force:
            cached = _load_cached_keys()
            if cached is not None and cached.is_fresh():
                self._keys = cached
                return cached

        payload = request_payload(EP_NAV, cookie=self.cookie)
        data = payload.get("data") or {}
        wbi_img = data.get("wbi_img")
        if not isinstance(wbi_img, dict) or "img_url" not in wbi_img:
            raise UpstreamChanged("nav 接口未返回 wbi_img，WBI 密钥获取方式可能已变更")
        keys = WbiKeys.from_nav(wbi_img)
        self._keys = keys
        _save_cached_keys(keys)
        return keys

    def nav(self) -> dict:
        """返回 nav 的 `data`（未登录时也会返回，其中 `isLogin=false`）。"""
        payload = request_payload(EP_NAV, cookie=self.cookie)
        return payload.get("data") or {}

    # ---------- 业务端点 ----------

    def video_view(self, *, bvid: str | None = None, aid: int | None = None) -> dict:
        params: dict[str, Any] = {}
        if bvid:
            params["bvid"] = bvid
        elif aid:
            params["aid"] = aid
        else:
            raise ValueError("必须给出 bvid 或 aid")
        data = request_json(EP_VIEW, params, cookie=self.cookie)
        _require_dict(data, "view")
        return data

    def player_v2(self, bvid: str, cid: int) -> dict:
        data = request_json(
            EP_PLAYER, {"bvid": bvid, "cid": cid}, cookie=self.cookie
        )
        _require_dict(data, "player/v2")
        return data

    def conclusion_get(
        self, bvid: str, cid: int, up_mid: int | None = None
    ) -> dict:
        """B 站官方 AI 总结。需要登录 + WBI 签名。"""
        keys = self.wbi_keys()
        params: dict[str, Any] = {"bvid": bvid, "cid": cid}
        if up_mid:
            params["up_mid"] = up_mid
        signed = sign_params(params, keys)
        data = request_json(EP_CONCLUSION, signed, cookie=self.cookie)
        _require_dict(data, "conclusion/get")
        return data

    def playurl(self, bvid: str, cid: int, *, dash: bool = True) -> dict:
        """取播放地址（L3 本地 ASR 取音频用）。

        优先用**非 wbi** 变体：实测免签名即可返回 dash 音轨。上游一旦收紧签名要求，
        再自动改用 wbi 变体重试一次 —— 这条兜底只是多一次请求，却能让 L3 不至于整体失效。

        `dash=True` 要 dash 格式（`fnval=16`，音频与视频分离）；否则退回老的 durl 整段格式。
        """
        params: dict[str, Any] = {
            "bvid": bvid,
            "cid": cid,
            "fnval": 16 if dash else 1,
            "fnver": 0,
            "fourk": 1,
        }
        try:
            data = request_json(EP_PLAYURL, params, cookie=self.cookie)
        except BiliexError:
            signed = sign_params(params, self.wbi_keys())
            data = request_json(EP_PLAYURL_WBI, signed, cookie=self.cookie)
        _require_dict(data, "playurl")
        return data

    def subtitle_json(self, subtitle_url: str) -> dict:
        """下载字幕 JSON（CDN 上的 `<hash>.json`）。

        用 `request_payload` 而不是 `request_json`：**字幕 JSON 没有 `code` 字段**，
        它的结构是 `{font_size, font_color, background_alpha, body: [...]}`。
        按 API 的业务码规则解析会把它当成「结构变更」而报错。
        """
        url = subtitle_url
        if url.startswith("//"):
            url = f"https:{url}"
        elif url.startswith("http://"):
            url = "https://" + url[len("http://") :]
        # 字幕 CDN 不需要 Cookie；刻意不带，避免凭据泄露到非 bilibili.com 的域名。
        data = request_payload(url, retry=RetryPolicy(max_attempts=3, base_delay=4.0))
        _require_dict(data, "subtitle json")
        return data

    def resolve_short_link(self, code: str) -> str:
        """展开 b23.tv 短链，返回最终 URL。"""
        from .http import _default_headers  # 局部导入，避免扩大公开面

        import urllib.request

        request = urllib.request.Request(
            _SHORT_LINK.format(code=code), headers=_default_headers(None)
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.geturl()

    # ---------- 自检 ----------

    def check_wbi_signature(self, mid: int = 2) -> dict:
        """用「必须签名」的端点自检 WBI 实现是否正确。

        未签名请求应当失败，签名请求应当成功 —— 这能在**不需要登录**的前提下
        证明签名算法实现正确。
        """
        base = {"mid": mid, "ps": 5, "pn": 1, "order": "pubdate"}

        unsigned_error = None
        try:
            request_json(EP_SPACE_ARC, dict(base))
        except Exception as exc:  # noqa: BLE001 - 自检需要吞掉异常看结果
            unsigned_error = getattr(exc, "code", type(exc).__name__)

        keys = self.wbi_keys()
        signed = sign_params(dict(base), keys)
        signed_ok = False
        signed_result: Any = None
        try:
            signed_result = request_json(EP_SPACE_ARC, signed)
            signed_ok = True
        except Exception as exc:  # noqa: BLE001
            signed_result = getattr(exc, "code", type(exc).__name__)

        return {
            "unsigned_error": unsigned_error,
            "signed_ok": signed_ok,
            "signed_result": signed_result,
            "img_key": keys.img_key,
            "mixin_key_prefix": keys.mixin_key[:8],
        }


def _require_dict(data: Any, where: str) -> None:
    if not isinstance(data, dict):
        raise UpstreamChanged(f"{where} 返回结构不是对象，接口可能已变更")


# ---------- WBI 密钥磁盘缓存（非凭据，可安全落盘） ----------


def _cache_path() -> Path:
    return config.config_dir() / "wbi-keys.json"


def _load_cached_keys() -> WbiKeys | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return WbiKeys(
            img_key=data["img_key"],
            sub_key=data["sub_key"],
            fetched_at=float(data["fetched_at"]),
        )
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _save_cached_keys(keys: WbiKeys) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "img_key": keys.img_key,
                    "sub_key": keys.sub_key,
                    "fetched_at": keys.fetched_at or time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
