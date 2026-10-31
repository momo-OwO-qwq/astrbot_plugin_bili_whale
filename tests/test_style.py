import unittest

from core.style import normalize_mention, similarity, style_reason


class StyleGateTest(unittest.TestCase):
    def test_lead_cliche_blocked(self):
        self.assertIn("套话", style_reason("值得一看，这期讲得很好", [], []))

    def test_too_similar_to_own_comment(self):
        reason = style_reason("这个转场的设计特别巧妙", ["这个转场的设计很巧妙"])
        self.assertIn("太像", reason)

    def test_too_similar_to_hot_comment(self):
        reason = style_reason("这个转场的设计特别巧妙", [], ["这个转场的设计很巧妙"])
        self.assertIn("撞车", reason)

    def test_emoji_or_tag_blocked(self):
        self.assertIn("emoji", style_reason("这个转场太妙了 😂", [], []))

    def test_clean_comment_passes(self):
        self.assertEqual(style_reason("第三分钟那个镜头切得真舒服", [], []), "")

    def test_mention_normalization(self):
        self.assertEqual(normalize_mention("@ 阿鲸 你好"), "@阿鲸 你好")

    def test_similarity_range(self):
        self.assertEqual(similarity("abc", "abc"), 1.0)
        self.assertGreater(similarity("转场很巧妙", "转场设计很巧妙"), 0)


if __name__ == "__main__":
    unittest.main()
