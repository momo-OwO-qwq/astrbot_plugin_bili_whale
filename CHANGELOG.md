# Changelog

## 0.1.0 — 2026-10-25

首个 AstrBot 版本，把 dsh-bili-scroll 与 dsh-bili-whale 的思路合并为纯 Python 插件：

- 生物钟：作息窗口内随机醒、每天 N 次、最少间隔、30% 概率今天不看；
- 内容：WBI 签名搜索、官方字幕、热评、弹幕热区；
- 判断：模型挑片/深读/评分；无字幕证据时**代码强制**最多「可以看」；
- 产出：`### BV号` 日报 + 两本日记；
- 分享：评论区 @ 使用者，默认关；manual 预览确认 / auto 自动发；每天 ≤3、同视频 7 天；
- 保护：1500ms 请求间隔、读写预算、120 秒写冷却、412 熔断 5–30 分钟；
- AstrBot：`/bili` 指令组、`bili_video_digest` LLM Tool、主动推送、WebUI 配置；
- 测试：47 个不联网用例 + AstrBot stub 冒烟脚本。
