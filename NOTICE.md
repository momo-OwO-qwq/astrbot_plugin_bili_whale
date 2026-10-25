# 来源、署名与许可

本项目是一个 **AstrBot 插件**，把下面两个 DSH 插件的思路合到一起，并用 Python 重写：

| 来源 | 许可证 | 借鉴了什么 |
| --- | --- | --- |
| [c1overzz/dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll) | MIT，Copyright (c) 2026 c1overzz | 每日搜关键词 → 读字幕原文 → 写 Markdown 日报 → 挑值得的在评论区 @ 使用者；`### BV号` 的日报格式；WBI 字幕/评论/搜索的接口经验；提示词里的红线 |
| [Alterssss/dsh-bili-whale](https://github.com/Alterssss/dsh-bili-whale) | MIT，Copyright (c) 2026 ALTERSSSS | 生物钟（窗口内随机醒、可能"今天不想看"、最少间隔、一天 N 次）；人设注入；两本日记；阅读/写入/分享配额；412 熔断与请求节流；"代码强制，不靠自觉"的克制 |

两份原始 MIT 许可证全文放在 [`LICENSES/`](LICENSES/) 下：

- `LICENSES/dsh-bili-scroll-LICENSE`
- `LICENSES/dsh-bili-whale-LICENSE`

本仓库自身同样以 MIT 发布，正文见 [`LICENSE`](LICENSE)。

## 没有搬过来的东西

- **没有**内置 `bilibili-mcp` 或 Python 子进程：所有请求由本插件的 `core/bili_api.py` 用 aiohttp 直接发；
- **没有**抽帧/ffmpeg/Whisper：本插件只读**文字**（官方字幕、热评、弹幕），不下载视频、不看画面；
- **没有**私信、动态、收藏、直播、三连：能力越窄越安全，也避免误操作使用者账号；
- **没有**自动关注/互关/群发。

## 免责声明

本项目与哔哩哔哩、AstrBot、DeepSeek 官方均无隶属关系。它用的是**你自己的账号**，发出去的每个字都算在你名下。请遵守平台规则；因使用本项目导致的限流、封号、纠纷等后果由使用者自行承担。
