"""错误模型：对外统一错误码，不外泄上游原始响应。

错误码分层是为了让调用方（agent）能据此决定**是否降级**，
而不是把上游的 code/message 原样抛出去。
"""

from __future__ import annotations


class BiliexError(Exception):
    """所有错误的基类。code 是稳定的机器可读标识。"""

    code = "internal_error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        # detail 只放可公开的信息，**绝不放 Cookie / SESSDATA / 原始响应体**。
        self.detail = detail

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.detail:
            d["detail"] = self.detail
        return d


class NotAuthenticated(BiliexError):
    """未登录或登录态已失效（上游 code=-101 或本地无凭据）。"""

    code = "not_authenticated"


class PermissionDenied(BiliexError):
    """访问权限不足（上游 code=-403）。"""

    code = "permission_denied"


class RiskControl(BiliexError):
    """被风控拦截（上游 code=-352 / HTTP 412）。"""

    code = "risk_control"


class RateLimited(BiliexError):
    """请求过快触发限流。"""

    code = "rate_limited"


class NotFound(BiliexError):
    """资源不存在（视频被删、BV 号错误等）。"""

    code = "not_found"


class UpstreamChanged(BiliexError):
    """上游接口结构变化 —— 命令契约被破坏，需要改适配层。

    这是最需要显式暴露的一类错误：静默失败会让下游产出错误结果。
    """

    code = "upstream_changed"


class NetworkError(BiliexError):
    """网络层失败：连不上、超时、TLS 错误。"""

    code = "network_error"


class UpstreamError(BiliexError):
    """上游返回了非预期结构或未知错误码。"""

    code = "upstream_error"


class ContentUnavailable(BiliexError):
    """该视频确实没有可用内容（无 AI 总结且无字幕）。属于正常降级结果。"""

    code = "content_unavailable"


class CredentialMissing(BiliexError):
    """本地没有凭据，且该操作必须登录。"""

    code = "credential_missing"


class OptionalComponentMissing(BiliexError):
    """用户显式要求了某个**可选组件**，但它没有安装。

    单独一类是为了让调用方分得清"工具坏了"和"少装了一个可选件"：
    后者只需按提示装一次，前者才需要排查。
    """

    code = "optional_component_missing"
