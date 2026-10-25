import tempfile
import time
import unittest

from core.models import Comment, DanmakuHotspot, Subtitle, Video
from core.runner import Runner
from core.store import Store


def base_config(**overrides):
    cfg = {
        "account": {"owner_name": "阿鲸"},
        "content": {
            "keywords": ["测试关键词"],
            "exclude_keywords": [],
            "search_days": 7,
            "deep_read": 2,
            "min_duration_sec": 0,
            "max_duration_sec": 7200,
            "subtitle_lang": "ai-zh",
            "danmaku": True,
            "max_subtitle_chars": 10000,
        },
        "diary": {"enabled": True},
        "report": {"detail": "full"},
        "share": {"mode": "off", "min_score": 13, "max_per_day": 3, "same_video_days": 7, "min_reason_len": 8},
        "risk": {"request_interval_ms": 300},
        "persona": {},
        "rhythm": {"window": "20:00-24:00", "wakes_per_day": 1},
    }
    for key, value in overrides.items():
        cfg[key] = {**cfg.get(key, {}), **value}
    return cfg


class FakeAPI:
    def __init__(self, videos, subtitle=True):
        self.videos = {v.bvid: v for v in videos}
        self.subtitle = subtitle
        self.posted = []

    async def login_info(self):
        return {"is_login": True, "uname": "小号", "mid": 1}

    async def search(self, keyword, page=1, page_size=30):
        return list(self.videos.values())

    async def video_info(self, bvid, force=False):
        return self.videos[bvid]

    async def subtitles(self, video, preferred_lang="ai-zh"):
        if not self.subtitle:
            return None
        return Subtitle("ai-zh", ["[00:01] 这是一段足够长的字幕内容用来证明真的读过视频", "[00:02] 第二句"])

    async def hot_comments(self, video, limit=10):
        return [Comment(user="观众", text="讲得挺清楚", like=10)]

    async def danmaku_hotspots(self, cid, duration=0, buckets=8):
        return [DanmakuHotspot(minute=1, count=20, samples=["有点意思"])]

    async def post_comment(self, aid, message):
        self.posted.append((aid, message))
        return "12345"


class FakeChat:
    def __init__(self):
        self.calls = []

    async def __call__(self, system, prompt):
        self.calls.append(prompt)
        if "请挑出最多" in prompt:
            import json

            return json.dumps({"picks": [{"bvid": "BV1xx411c7mD"}, {"bvid": "BV1yy411c7mD"}]}, ensure_ascii=False)
        if "请读懂下面这条" in prompt:
            return '{"score": 16, "verdict": "值得看", "why": "它把原理讲清楚了，还给了例子", "one_liner": "值得花十分钟"}'
        if "请写三样东西" in prompt:
            return '{"letter": "今天看到一条好视频。", "diary": "有点累，但还行。", "closing": "今天有收获。"}'
        if "公开评论" in prompt:
            return "这个例子讲得很实在，推荐看看"
        return "{}"


def make_video(bvid, title, aid):
    return Video(
        bvid=bvid, title=title, author="UP", aid=aid, cid=aid + 100,
        play=10000, pubdate=int(time.time()) - 3600, duration=600,
    )


class RunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_full_run_writes_report_and_diaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            videos = [
                make_video("BV1xx411c7mD", "第一条视频", 1),
                make_video("BV1yy411c7mD", "第二条视频", 2),
                make_video("BV1zz411c7mD", "第三条视频", 3),
            ]
            runner = Runner(base_config(), FakeAPI(videos), store, chat=FakeChat())
            result = await runner.run(allow_share=False)
            self.assertEqual(result.scanned, 3)
            self.assertEqual(result.deep, 2)
            self.assertEqual(result.picked, 2)
            self.assertTrue(result.report_path)
            report = store.read_report()
            self.assertEqual(report.count("### BV"), 2)
            self.assertTrue(store.read_diary("owner"))
            self.assertTrue(store.read_diary("self"))
            self.assertEqual(store.seen_count(), 2)
            self.assertIn("刷了 3 条", result.summary)

    async def test_no_subtitle_never_worth_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            videos = [make_video("BV1xx411c7mD", "没有字幕的视频", 1)]
            runner = Runner(base_config(), FakeAPI(videos, subtitle=False), store, chat=FakeChat())
            result = await runner.run(allow_share=False)
            self.assertEqual(result.picked, 1)
            report = store.read_report()
            self.assertIn("档位：可以看", report)
            self.assertNotIn("档位：值得看", report)

    async def test_auto_share_posts_once_and_records_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            videos = [make_video("BV1xx411c7mD", "值得分享的视频", 1)]
            api = FakeAPI(videos)
            cfg = base_config(share={"mode": "auto"})
            runner = Runner(cfg, api, store, chat=FakeChat())
            result = await runner.run(allow_share=True)
            self.assertEqual(result.shared, 1)
            self.assertEqual(len(api.posted), 1)
            self.assertTrue(api.posted[0][1].startswith("@阿鲸"))
            self.assertEqual(store.quota["shares_day"], 1)

    async def test_manual_share_preview_and_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            videos = [make_video("BV1xx411c7mD", "值得分享的视频", 1)]
            api = FakeAPI(videos)
            cfg = base_config(share={"mode": "manual"})
            runner = Runner(cfg, api, store, chat=FakeChat())
            payload = await runner.prepare_share("BV1xx411c7mD")
            self.assertTrue(payload["comment"].startswith("@阿鲸"))
            self.assertEqual(api.posted, [])
            rpid = await runner.confirm_share(payload)
            self.assertEqual(rpid, "12345")
            self.assertEqual(len(api.posted), 1)

    async def test_skip_writes_diary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            runner = Runner(base_config(), FakeAPI([]), store, chat=FakeChat())
            result = await runner.run_skip("今天不想看")
            self.assertTrue(result.skip)
            self.assertTrue(store.read_diary("owner"))
            self.assertIn("今天不想看", store.read_report())


if __name__ == "__main__":
    unittest.main()
