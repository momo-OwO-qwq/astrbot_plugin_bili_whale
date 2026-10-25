import unittest

from core.models import DeepResult, Video
from core.report import build_digest_text, build_dm_text, build_report, build_skip_note, build_summary
from core.models import Material, RunResult


def video(bvid="BV1xx411c7mD", title="一条视频"):
    return Video(bvid=bvid, title=title, author="UP主", aid=1, cid=2, play=12345, duration=366)


class ReportTest(unittest.TestCase):
    def test_each_video_has_bv_heading(self):
        results = [
            DeepResult(video=video("BV1xx411c7mD", "第一条"), score=16, verdict="值得看", why="讲得清楚", one_liner="可以看"),
            DeepResult(video=video("BV1yy411c7mD", "第二条"), score=9, verdict="可以看", why="有点意思", one_liner="随便看看"),
        ]
        text = build_report("2026-10-25", results, scanned=12, deep_count=2, closing="今天学到一点")
        headings = [line for line in text.splitlines() if line.startswith("### BV")]
        self.assertEqual(headings, ["### BV1xx411c7mD", "### BV1yy411c7mD"])
        self.assertIn("- 档位：值得看", text)
        self.assertIn("## 今天的一句", text)

    def test_thin_and_none(self):
        results = [DeepResult(video=video(), score=13, verdict="值得看", why="理由")]
        thin = build_report("2026-10-25", results, 1, 1, detail="thin")
        self.assertIn("### BV1xx411c7mD", thin)
        self.assertNotIn("- 标题：", thin)
        self.assertEqual(build_report("2026-10-25", results, 1, 1, detail="none"), "")

    def test_summary_and_skip_note(self):
        result = RunResult(scanned=10, deep=2, picked=1, report_path="/tmp/x.md")
        text = build_summary(result, [DeepResult(video=video(), score=15, verdict="值得看", why="理由")])
        self.assertIn("刷了 10 条", text)
        self.assertIn("/tmp/x.md", text)
        self.assertIn("今天不想看", build_skip_note("2026-10-25", "没理由"))

    def test_dm_text_is_short_and_omits_local_path(self):
        result = RunResult(scanned=10, deep=2, picked=1, report_path="/secret/path/report.md")
        result.summary = "🐟 今天刷了 10 条\n值得看：\n· 一条视频（BV1xx411c7mD）\n完整日报：/secret/path/report.md"
        text = build_dm_text(result)
        self.assertNotIn("/secret", text)
        self.assertIn("BV1xx411c7mD", text)
        self.assertLessEqual(len(text), 300)

    def test_digest_text(self):
        material = Material(video=video())
        result = DeepResult(video=video(), score=12, verdict="可以看", why="有信息", one_liner="还行")
        text = build_digest_text(material, result)
        self.assertIn("评分 12/20", text)
        self.assertIn("BV1xx411c7mD", text)


if __name__ == "__main__":
    unittest.main()
