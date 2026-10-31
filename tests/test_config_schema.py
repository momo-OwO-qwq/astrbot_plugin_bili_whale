import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"string", "text", "int", "float", "bool", "object", "list", "dict", "template_list", "file"}


class ConfigSchemaTest(unittest.TestCase):
    def test_schema_shape(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("account", schema)
        self.assertIn("rhythm", schema)
        for key, item in schema.items():
            self.assertIn(item["type"], ALLOWED, key)
            self.assertTrue(item.get("description"), key)
            if item["type"] == "object":
                self.assertIn("items", item, key)
                for child_key, child in item["items"].items():
                    self.assertIn(child["type"], ALLOWED, f"{key}.{child_key}")
                    self.assertTrue(child.get("description"), f"{key}.{child_key}")
            options = item.get("options")
            labels = item.get("labels")
            if options and labels:
                self.assertEqual(len(options), len(labels), key)

    def test_share_switches_are_visible(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["share"]["items"]
        self.assertEqual(list(items)[:3], ["enabled", "auto_send", "auto_on_manual"])
        self.assertTrue(items["mode"].get("invisible"))
        for key in ("enabled", "auto_send", "auto_on_manual"):
            self.assertFalse(items[key].get("invisible", False), key)
            self.assertTrue(items[key].get("description"), key)

    def test_required_metadata_exists(self):
        text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        for field in ("name: astrbot_plugin_bili_whale", "version:", "author:", "desc:"):
            self.assertIn(field, text)


if __name__ == "__main__":
    unittest.main()
