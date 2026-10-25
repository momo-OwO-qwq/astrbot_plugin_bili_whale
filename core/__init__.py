"""astrbot_plugin_bili_whale 的纯 Python 内核。

这里**不导入 astrbot**，也不在模块顶层导入 aiohttp：
- 计划器、配额、报告、提示词都可以单独跑单元测试；
- 网络层只在真正发请求时用到 aiohttp。

设计灵感来自 c1overzz/dsh-bili-scroll（每天替你刷一轮）与
Alterssss/dsh-bili-whale（生物钟 / 人设 / 克制 / 两本日记），
在 AstrBot 上重写为 Python 实现。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
