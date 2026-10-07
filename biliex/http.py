"""HTTP 适配层 —— **所有网络调用唯一的出口**。

为什么集中：B 站的风控是按接口分级的（`view`/`player` 放行，`popular`/`ranking`
直接返回 -352），且随时可能变。集中在一处，风控一变只改这个文件。

安全约定：
* 绝不打印 Cookie / SESSDATA；
* 绝不把凭据写进日志、异常 detail 或产出文件；
* 出错时只上报稳定的错误码，不透传上游原始响应体。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import config
from .errors import (
    BiliexError,
    NetworkError,
    NotFound,
    NotAuthenticated,
    PermissionDenied,
    RateLimited,
    RiskControl,
    UpstreamError,
)


@dataclass
class RetryPolicy:
    """重试策略。

    字幕接口的「假残缺」问题要求更激进的策略：实测有效做法是
    每个视频最多 12 次、间隔 `8s + attempt*3s`。
    """

    max_attempts: int = 4
    base_delay: float = 1.5
    step_delay: float = 3.0
    # 是否对风控/限流重试。默认是 —— 这两类都是暂时性的。
    retry_on_risk: bool = True

    def delay_for(self, attempt: int) -> float:
        """attempt 从 1 开始计数。"""
        return self.base_delay + (attempt - 1) * self.step_delay


# 常规接口
DEFAULT_RETRY = RetryPolicy()
# 字幕接口：按实测有效的策略放大
SUBTITLE_RETRY = RetryPolicy(max_attempts=12, base_delay=8.0, step_delay=3.0)


@dataclass
class Cookie:
    """登录凭据。__repr__ 已打码，避免误打印。"""

    sessdata: str = ""
    bili_jct: str = ""
    buvid3: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def header(self) -> str:
        parts = []
        if self.sessdata:
            parts.append(f"SESSDATA={self.sessdata}")
        if self.bili_jct:
            parts.append(f"bili_jct={self.bili_jct}")
        if self.buvid3:
            parts.append(f"buvid3={self.buvid3}")
        for k, v in self.extra.items():
            parts.append(f"{k}={v}")
        return "; ".join(parts)

    def __repr__(self) -> str:  # pragma: no cover - 防误打印
        return (
            f"Cookie(sessdata={config.redact(self.sessdata)}, "
            f"bili_jct={config.redact(self.bili_jct)}, "
            f"buvid3={config.redact(self.buvid3)})"
        )

    __str__ = __repr__


def _default_headers(cookie: Cookie | None) -> dict[str, str]:
    headers = {
        "User-Agent": config.USER_AGENT,
        "Referer": config.REFERER,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.bilibili.com",
    }
    if cookie is not None and cookie.header():
        headers["Cookie"] = cookie.header()
    return headers


def _classify_http_error(status: int) -> BiliexError:
    if status == 412:
        return RateLimited("被 B 站限流（HTTP 412），请降低频率后重试")
    if status == 403:
        return PermissionDenied("访问被拒绝（HTTP 403），可能是风控或权限不足")
    if status == 404:
        return NotFound("接口不存在（HTTP 404）—— 上游可能已变更")
    if status == 429:
        return RateLimited("请求过多（HTTP 429）")
    return NetworkError(f"HTTP {status}")


def _classify_business_code(code: int, message: str) -> BiliexError:
    """把上游业务码映射成稳定错误码。"""
    if code == config.CODE_NOT_LOGGED_IN:
        return NotAuthenticated("未登录或登录态已失效，请重新设置 SESSDATA")
    if code == config.CODE_RISK_CONTROL:
        return RiskControl(f"被风控拦截（code={code}）")
    if code == config.CODE_PERMISSION_DENIED:
        return PermissionDenied("访问权限不足（该接口需要登录 + WBI 签名）")
    if code == config.CODE_BAD_REQUEST:
        return UpstreamError("请求参数错误（上游 code=-400）")
    if code == -404:
        return NotFound("视频不存在或已被删除")
    return UpstreamError(f"上游返回未知错误码 {code}: {message}")


def request_payload(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    cookie: Cookie | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
    timeout: float = 20.0,
) -> dict:
    """发起 GET，返回**完整响应体**，不解释业务码。

    需要它的有两个地方，都是实测踩出来的：
    * `nav` —— 未登录时返回 `code=-101`，但 `data.wbi_img`（WBI 密钥）依然可用。
      若在这里按业务码报错，就拿不到密钥，WBI 签名根本无从做起。
    * 字幕 CDN 的 JSON —— 它根本没有 `code` 字段（结构是 `{font_size, body:[...]}`），
      与 API 响应不是一个形状。按 API 规则解析会把整条字幕路径打死。
    """
    query = urllib.parse.urlencode(params or {})
    full_url = f"{url}?{query}" if query else url
    last_error: BiliexError | None = None

    for attempt in range(1, retry.max_attempts + 1):
        request = urllib.request.Request(full_url, headers=_default_headers(cookie))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            error = _classify_http_error(exc.code)
            last_error = error
            retryable = isinstance(error, (RateLimited, RiskControl))
            if retryable and retry.retry_on_risk and attempt < retry.max_attempts:
                time.sleep(retry.delay_for(attempt))
                continue
            raise error from None
        except urllib.error.URLError as exc:
            last_error = NetworkError(f"网络不可达: {exc.reason}")
            if attempt < retry.max_attempts:
                time.sleep(retry.delay_for(attempt))
                continue
            raise last_error from None
        except json.JSONDecodeError:
            # 返回了非 JSON（通常是风控挑战页），按风控处理
            last_error = RiskControl("上游返回非 JSON 内容，可能是风控挑战页")
            if attempt < retry.max_attempts:
                time.sleep(retry.delay_for(attempt))
                continue
            raise last_error from None

        if not isinstance(payload, dict):
            raise UpstreamError("上游响应不是 JSON 对象")
        return payload

    raise last_error or NetworkError("请求失败且未产生具体错误")


def request_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    cookie: Cookie | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
    timeout: float = 20.0,
) -> Any:
    """在 `request_payload` 之上强制 `code == 0`，返回 `data`。

    业务码级的风控（如 `-352` 是 HTTP 200 + JSON 错误码，不是 HTTP 错误）
    在这里重试；HTTP 级的问题由 `request_payload` 负责。
    """
    last_error: BiliexError | None = None

    for attempt in range(1, retry.max_attempts + 1):
        payload = request_payload(
            url, params, cookie=cookie, retry=retry, timeout=timeout
        )
        if "code" not in payload:
            raise UpstreamError("上游响应缺少 code 字段，接口结构可能已变更")

        code = payload.get("code")
        if code == 0:
            return payload.get("data")

        error = _classify_business_code(int(code), str(payload.get("message", "")))
        last_error = error
        if (
            isinstance(error, (RateLimited, RiskControl))
            and retry.retry_on_risk
            and attempt < retry.max_attempts
        ):
            time.sleep(retry.delay_for(attempt))
            continue
        raise error

    raise last_error or NetworkError("请求失败且未产生具体错误")
