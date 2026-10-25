import hashlib
import unittest

from core.bili_api import MIXIN_TABLE, mixin_key, sign_wbi, uploader_key


class WbiTest(unittest.TestCase):
    def test_uploader_key(self):
        self.assertEqual(
            uploader_key("https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"),
            "7cd084941338484aae1ad9425b84077c",
        )

    def test_mixin_key(self):
        raw = "a" * 32 + "b" * 32
        img = "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png"
        sub = "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png"
        expected = "".join(raw[i] for i in MIXIN_TABLE)[:32]
        self.assertEqual(mixin_key(img, sub), expected)

    def test_sign_is_sorted_and_strips_reserved_chars(self):
        signed = sign_wbi({"b": "x!y(z)", "a": "1"}, "0" * 32, ts=1700000000)
        self.assertIn("wts=1700000000", signed)
        self.assertTrue(signed.startswith("a=1&b=xyz&wts="))
        self.assertRegex(signed, r"w_rid=[0-9a-f]{32}$")

    def test_sign_matches_md5(self):
        signed = sign_wbi({"keyword": "测试", "page": 1}, "f" * 32, ts=1700000000)
        query, wid = signed.rsplit("&", 1)
        self.assertEqual(wid, "w_rid=" + hashlib.md5((query + "f" * 32).encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()
