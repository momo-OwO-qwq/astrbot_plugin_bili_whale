import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core import qrlogin
from core.credential import describe_sessdata, inspect_sessdata


class CredentialTest(unittest.TestCase):
    def test_valid_sessdata(self):
        expiry = int((datetime.now() + timedelta(days=30)).timestamp())
        value = "abcdef12%2C" + str(expiry) + "%2C" + "x" * 200
        report = inspect_sessdata(value)
        self.assertEqual(report["segments"], 3)
        self.assertEqual(report["problems"], [])
        self.assertIn("有效期到", describe_sessdata(report))

    def test_expired_and_broken(self):
        expired = int((datetime.now() - timedelta(days=1)).timestamp())
        value = "abcdef12%2C" + str(expired) + "%2C" + "x" * 200 + ","
        report = inspect_sessdata(value)
        self.assertTrue(report["expired"])
        self.assertTrue(any("逗号" in p for p in report["problems"]))
        report2 = inspect_sessdata("SESSDATA=abc;")
        self.assertTrue(any("SESSDATA=" in p for p in report2["problems"]))

    def test_qr_credentials_from_cookie_and_url(self):
        payload = {
            "cookies": {"SESSDATA": "a%2Cb%2Cc", "bili_jct": "j", "buvid3": "b"},
            "url": "https://passport.bilibili.com/x?DedeUserID=1",
        }
        cred = qrlogin.credentials(payload)
        self.assertEqual(cred["sessdata"], "a%2Cb%2Cc")
        self.assertEqual(cred["bili_jct"], "j")
        self.assertEqual(cred["buvid3"], "b")

    def test_qr_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "qr.png"
            qrlogin.render("https://example.com", str(path))
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 200)


if __name__ == "__main__":
    unittest.main()
