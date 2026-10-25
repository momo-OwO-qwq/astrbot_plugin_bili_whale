import json
import unittest
from urllib.parse import parse_qs, urlsplit

from core.bili_api import BiliAPI
from core.errors import BiliAuthError


class FakeGuard:
    def __init__(self):
        self.reads = 0
        self.writes = 0

    async def before_read(self):
        self.reads += 1

    async def before_write(self):
        self.writes += 1

    def note_success(self):
        pass

    def hit_risk(self, code, message=""):
        return 300


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self, errors="strict"):
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload)


class FakeSession:
    def __init__(self):
        self.requests = []
        self.closed = False
        self.nav = {
            "code": 0,
            "data": {
                "isLogin": True,
                "uname": "tester",
                "mid": 42,
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                },
            },
        }
        self.view = {
            "code": 0,
            "data": {
                "bvid": "BV1xx411c7mD",
                "aid": 99,
                "cid": 100,
                "title": "一条测试视频",
                "desc": "简介",
                "owner": {"name": "UP", "mid": 7},
                "stat": {"view": 12345},
                "pubdate": 1700000000,
                "duration": 600,
                "pages": [{"cid": 100}],
            },
        }
        self.search = {
            "code": 0,
            "data": {
                "result": [
                    {
                        "bvid": "BV1xx411c7mD",
                        "title": "<em>测试</em>视频",
                        "author": "UP",
                        "mid": 7,
                        "play": 12345,
                        "pubdate": 1700000000,
                        "duration": "10:00",
                        "description": "简介",
                    }
                ]
            },
        }
        self.relation = {"code": 0, "data": {"attribute": 6}}
        self.replies = {
            "code": 0,
            "data": {
                "top": [],
                "upper": {"top": None},
                "replies": [
                    {
                        "rpid": 1,
                        "like": 9,
                        "member": {"uname": "观众", "mid": 2},
                        "content": {"message": "讲得清楚"},
                    }
                ],
            },
        }

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append(("GET", url, params, headers))
        path = urlsplit(url).path
        if path == "/x/web-interface/nav":
            return FakeResponse(self.nav)
        if path == "/x/web-interface/view":
            return FakeResponse(self.view)
        if path == "/x/web-interface/wbi/search/type":
            return FakeResponse(self.search)
        if path == "/x/player/wbi/v2":
            return FakeResponse(
                {"code": 0, "data": {"subtitle": {"subtitles": [
                    {"lan": "ai-zh", "subtitle_url": "//sub.example/x.json"}
                ]}}}
            )
        if path == "/x/v2/reply":
            return FakeResponse(self.replies)
        if path == "/x/relation":
            return FakeResponse(self.relation)
        if path == "/x.json":
            return FakeResponse({"body": [{"from": 1, "content": "第一句字幕"}, {"from": 2, "content": "第二句字幕"}]})
        return FakeResponse({"code": -404, "message": "not found"}, status=404)

    def post(self, url, data=None, headers=None):
        self.requests.append(("POST", url, data, headers))
        if urlsplit(url).path == "/web_im/v1/web_im/send_msg":
            return FakeResponse({"code": 0, "data": {"msg_key": "x"}})
        return FakeResponse({"code": 0, "data": {"rpid": 123}})

    async def close(self):
        self.closed = True


class BiliApiFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = FakeGuard()
        self.api = BiliAPI(
            {"sessdata": "s", "bili_jct": "j", "buvid3": "b"},
            self.guard,
        )
        self.session = FakeSession()
        self.api._session = self.session

    async def test_login_search_view_subtitle_comment(self):
        info = await self.api.login_info()
        self.assertTrue(info["is_login"])
        self.assertEqual(info["mid"], 42)

        videos = await self.api.search("测试")
        self.assertEqual(videos[0].bvid, "BV1xx411c7mD")
        self.assertEqual(videos[0].title, "测试视频")
        self.assertEqual(videos[0].duration, 600)

        video = await self.api.video_info("BV1xx411c7mD")
        self.assertEqual(video.aid, 99)
        self.assertEqual(video.cid, 100)

        subtitle = await self.api.subtitles(video)
        self.assertIsNotNone(subtitle)
        self.assertIn("第一句字幕", subtitle.text)

        comments = await self.api.hot_comments(video)
        self.assertEqual(comments[0].user, "观众")

        rpid = await self.api.post_comment(video.aid, "@阿鲸 这条不错")
        self.assertEqual(rpid, "123")
        self.assertEqual(self.guard.writes, 1)
        post = [r for r in self.session.requests if r[0] == "POST"][0]
        self.assertIn("w_rid=", post[1])
        body = post[2].decode() if isinstance(post[2], bytes) else str(post[2])
        self.assertIn("csrf=j", body)
        self.assertIn("%40%E9%98%BF%E9%B2%B8", body)

    async def test_search_request_is_wbi_signed(self):
        await self.api.search("测试")
        request = [r for r in self.session.requests if urlsplit(r[1]).path.endswith("/search/type")][0]
        query = parse_qs(urlsplit(request[1]).query)
        self.assertIn("w_rid", query)
        self.assertIn("wts", query)
        self.assertEqual(query["keyword"], ["测试"])

    async def test_send_dm_requires_mutual_follow(self):
        self.api.dev_id = "WEB-TEST-DEVICE"
        await self.api.send_dm(99, "今天的摘要")
        self.assertEqual(self.guard.writes, 1)
        post = [r for r in self.session.requests if r[0] == "POST"][-1]
        body = post[2].decode() if isinstance(post[2], bytes) else str(post[2])
        self.assertIn("msg%5Breceiver_id%5D=99", body)
        self.assertIn("msg%5Bdev_id%5D=WEB-TEST-DEVICE", body)
        self.assertIn("csrf=j", body)
        self.assertIn("%E4%BB%8A%E5%A4%A9%E7%9A%84%E6%91%98%E8%A6%81", body)

    async def test_send_dm_rejects_not_mutual(self):
        self.session.relation = {"code": 0, "data": {"attribute": 2}}
        with self.assertRaises(BiliAuthError):
            await self.api.send_dm(99, "摘要")
        self.assertEqual(self.guard.writes, 0)

    async def test_send_dm_rejects_self(self):
        with self.assertRaises(BiliAuthError):
            await self.api.send_dm(42, "摘要")
        self.assertEqual(self.guard.writes, 0)

    async def test_nav_without_login_is_reported(self):
        self.session.nav = {"code": -101, "message": "账号未登录", "data": {"isLogin": False}}
        info = await self.api.login_info()
        self.assertFalse(info["is_login"])


if __name__ == "__main__":
    unittest.main()
