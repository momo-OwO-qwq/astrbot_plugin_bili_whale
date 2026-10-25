"""插件内部异常。

分开定义是为了让上层能区分"该告诉用户怎么做"的三类问题：
- 配置/登录问题（BiliAuthError）
- 平台风控（BiliRisk）
- 普通接口/网络失败（BiliError）
"""

from __future__ import annotations


class BiliError(RuntimeError):
    def __init__(self, message: str, code: int | str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - 简单转发
        return self.message


class BiliAuthError(BiliError):
    """cookie 缺失 / 过期 / 没有写权限。"""


class BiliRisk(BiliError):
    """撞上 412、-352 等风控信号，已经熔断。"""

    def __init__(self, message: str, code: int | str | None = None, minutes: int = 0):
        super().__init__(message, code)
        self.minutes = minutes


class BudgetError(BiliError):
    """本地预算（读/写/分享）用完了。"""


class LLMUnavailable(BiliError):
    """没有可用的模型提供商，或调用失败。"""
