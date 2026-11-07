"""短链 / av 号解析的单元测试（不联网：短链部分用假 aiohttp）。"""

import asyncio
import sys
import types
import unittest
from unittest import mock

from core.util import av2bv, extract_bvid, normalize_bvid, resolve_bvid


class _FakeResponse:
    def __init__(self, url, status=200, body="", content_type="text/html"):
        self.url = url
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = body

    async def text(self, errors="replace"):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    def get(self, url, allow_redirects=True, headers=None):
        if self._exc is not None:
            raise self._exc
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_aiohttp(response=None, exc=None):
    module = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    def ClientSession(timeout=None):
        return _FakeSession(response=response, exc=exc)

    module.ClientTimeout = ClientTimeout
    module.ClientSession = ClientSession
    return module


class AvCodeTest(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(av2bv(170001), "BV17x411w7KC")
        self.assertEqual(av2bv(2), "BV1xx411c7mD")

    def test_invalid_values(self):
        self.assertIsNone(av2bv(0))
        self.assertIsNone(av2bv("abc"))


class ResolveDirectTest(unittest.TestCase):
    def run_resolve(self, text):
        return asyncio.run(resolve_bvid(text))

    def test_accepts_bv_number(self):
        self.assertEqual(extract_bvid("BV1S6xY65Eey"), "BV1S6xY65Eey")
        self.assertEqual(self.run_resolve("看看 BV1S6xY65Eey 这个"), "BV1S6xY65Eey")

    def test_normalizes_lowercase_bv(self):
        self.assertEqual(normalize_bvid("bv1S6xY65Eey"), "BV1S6xY65Eey")
        self.assertEqual(extract_bvid("看看 bv1S6xY65Eey"), "BV1S6xY65Eey")
        self.assertEqual(
            self.run_resolve("https://www.bilibili.com/video/bv1S6xY65Eey"),
            "BV1S6xY65Eey",
        )

    def test_accepts_bare_av(self):
        self.assertEqual(self.run_resolve("av170001 挺好看的"), "BV17x411w7KC")

    def test_accepts_video_url(self):
        url = "https://www.bilibili.com/video/BV1S6xY65Eey?share_source=copy"
        self.assertEqual(self.run_resolve(url), "BV1S6xY65Eey")
        self.assertEqual(
            self.run_resolve("https://m.bilibili.com/video/av170001"), "BV17x411w7KC"
        )

    def test_accepts_app_uri(self):
        # B 站「@我的」接口给的 uri 就是这种形式。
        self.assertEqual(self.run_resolve("bilibili://video/170001"), "BV17x411w7KC")
        self.assertEqual(
            self.run_resolve("bilibili://video/BV1S6xY65Eey"), "BV1S6xY65Eey"
        )

    def test_plain_text(self):
        self.assertIsNone(self.run_resolve("今天天气不错"))
        self.assertIsNone(self.run_resolve(""))


class ShortLinkTest(unittest.TestCase):
    def run_with(self, module, text="https://b23.tv/imEh4tZ"):
        with mock.patch.dict(sys.modules, {"aiohttp": module}):
            return asyncio.run(resolve_bvid(text))

    def test_follows_redirect_to_video(self):
        response = _FakeResponse("https://www.bilibili.com/video/BV1VqxW6xEJX")
        self.assertEqual(self.run_with(_fake_aiohttp(response)), "BV1VqxW6xEJX")

    def test_reads_canonical_link(self):
        body = (
            '<html><head><link rel="canonical" '
            'href="https://www.bilibili.com/video/BV1VqxW6xEJX" /></head></html>'
        )
        response = _FakeResponse("https://b23.tv/", body=body)
        self.assertEqual(self.run_with(_fake_aiohttp(response)), "BV1VqxW6xEJX")

    def test_reads_og_url(self):
        body = (
            '<meta property="og:url" '
            'content="https://www.bilibili.com/video/BV1VqxW6xEJX">'
        )
        response = _FakeResponse("https://b23.tv/", body=body)
        self.assertEqual(self.run_with(_fake_aiohttp(response)), "BV1VqxW6xEJX")

    def test_ignores_unrelated_bv_in_page(self):
        # 页面里出现别的 BV（推荐位）不算数，只认 canonical / og:url。
        body = '<a href="/video/BV1aaaaaaaaa">推荐</a>'
        response = _FakeResponse("https://b23.tv/", body=body)
        self.assertIsNone(self.run_with(_fake_aiohttp(response)))

    def test_http_error(self):
        response = _FakeResponse("https://b23.tv/", status=404)
        self.assertIsNone(self.run_with(_fake_aiohttp(response)))

    def test_non_html_response(self):
        response = _FakeResponse("https://b23.tv/x.apk", content_type="application/octet-stream")
        self.assertIsNone(self.run_with(_fake_aiohttp(response)))

    def test_network_exception_is_swallowed(self):
        module = _fake_aiohttp(exc=RuntimeError("boom"))
        self.assertIsNone(self.run_with(module))

    def test_missing_aiohttp(self):
        # 没装 aiohttp 时返回 None，而不是抛 ImportError。
        with mock.patch.dict(sys.modules, {"aiohttp": None}):
            self.assertIsNone(asyncio.run(resolve_bvid("https://b23.tv/imEh4tZ")))


if __name__ == "__main__":
    unittest.main()
