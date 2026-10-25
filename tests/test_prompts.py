import unittest

from core.models import Material, Subtitle, Video
from core.prompts import (
    coerce_deep,
    system_prompt,
    coerce_diary,
    coerce_screen,
    fallback_comment,
    parse_json_obj,
    sanitize_comment,
)


class PromptTest(unittest.TestCase):
    def test_parse_json_with_fence_and_noise(self):
        self.assertEqual(parse_json_obj('好的，结果是：```json\n{"score": 12}\n```')["score"], 12)
        self.assertEqual(parse_json_obj('前言 {"picks": []} 后记'), {"picks": []})
        self.assertIsNone(parse_json_obj("没有 JSON"))

    def test_screen_filters_unknown_bvids(self):
        candidates = [Video(bvid="BV1xx411c7mD"), Video(bvid="BV1yy411c7mD")]
        picks = coerce_screen({"picks": [{"bvid": "BV1yy411c7mD"}, {"bvid": "BVbad"}]}, candidates, 2)
        self.assertEqual(picks, ["BV1yy411c7mD"])

    def test_no_subtitle_caps_verdict(self):
        video = Video(bvid="BV1xx411c7mD", title="标题")
        material = Material(video=video)
        result = coerce_deep({"score": 18, "verdict": "值得看", "why": "理由", "one_liner": "一句"}, material)
        self.assertEqual(result.verdict, "可以看")
        self.assertLessEqual(result.score, 12)

    def test_subtitle_credit_is_not_content(self):
        junk = Subtitle("ai-zh", ["[00:01] 字幕byXX"] * 10)
        self.assertFalse(junk.meaningful)
        real = Subtitle("ai-zh", ["[00:01] 今天讲三个问题，先看第一个：为什么字幕总是对不上画面。"])
        self.assertTrue(real.meaningful)

    def test_with_subtitle_keeps_verdict(self):
        video = Video(bvid="BV1xx411c7mD", title="标题")
        material = Material(video=video, subtitle=Subtitle("ai-zh", ["这是一段足够长的字幕内容用来证明读过视频"]))
        result = coerce_deep({"score": 17, "verdict": "值得看", "why": "理由", "one_liner": "一句"}, material)
        self.assertEqual(result.verdict, "值得看")

    def test_comment_sanitize(self):
        text = sanitize_comment('评论：@旧名字 你好 https://example.com', "阿鲸")
        self.assertTrue(text.startswith("@阿鲸 "))
        self.assertNotIn("http", text)

    def test_fallback_comment_mentions_owner(self):
        video = Video(bvid="BV1xx411c7mD", title="标题")
        result = coerce_deep({"score": 14, "verdict": "值得看", "why": "理由", "one_liner": "一句话"}, Material(video=video))
        text = fallback_comment(result, "阿鲸")
        self.assertTrue(text.startswith("@阿鲸"))

    def test_diary_coerce(self):
        data = coerce_diary({"letter": "信", "diary": "日记", "closing": "结"})
        self.assertEqual(data["letter"], "信")
        self.assertEqual(data["closing"], "结")

    def test_astrbot_persona_overrides_custom_text(self):
        cfg = {"persona": {"text": "这是插件自定义人设"}}
        prompt = system_prompt(cfg, "这是 AstrBot 人格正文")
        self.assertIn("这是 AstrBot 人格正文", prompt)
        self.assertNotIn("这是插件自定义人设", prompt)
        fallback = system_prompt(cfg)
        self.assertIn("这是插件自定义人设", fallback)


if __name__ == "__main__":
    unittest.main()
