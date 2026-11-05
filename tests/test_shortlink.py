"""短链 / av 号解析的单元测试（不联网部分）。"""

import asyncio

from core.util import av2bv, extract_bvid, resolve_bvid


def run(coro):
    return asyncio.run(coro)


def test_av2bv_known_values():
    assert av2bv(170001) == "BV17x411w7KC"
    assert av2bv(2) == "BV1xx411c7mD"
    assert av2bv(0) is None
    assert av2bv("abc") is None


def test_resolve_bvid_accepts_bv_number():
    assert extract_bvid("BV1S6xY65Eey") == "BV1S6xY65Eey"
    assert run(resolve_bvid("看看 BV1S6xY65Eey 这个")) == "BV1S6xY65Eey"


def test_resolve_bvid_accepts_bare_av():
    assert run(resolve_bvid("av170001 挺好看的")) == "BV17x411w7KC"


def test_resolve_bvid_accepts_video_url():
    url = "https://www.bilibili.com/video/BV1S6xY65Eey?share_source=copy"
    assert run(resolve_bvid(url)) == "BV1S6xY65Eey"
    assert run(resolve_bvid("https://m.bilibili.com/video/av170001")) == "BV17x411w7KC"


def test_resolve_bvid_returns_none_for_plain_text():
    assert run(resolve_bvid("今天天气不错")) is None
