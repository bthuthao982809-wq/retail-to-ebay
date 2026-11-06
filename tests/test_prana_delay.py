"""prAna pacing defaults preserve explicit user settings and other sites."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core


class PranaDelayTests(unittest.TestCase):
    def test_old_config_missing_prana_delay_gets_15_without_changing_other_sites(self):
        original = {
            "proxy_file": "", "delay_seconds": 9,
            "sites": {"prana": {"prefix": "CUSTOM-"},
                      "bestbuy": {"delay": "15"}, "talbots": {"delay": "8"},
                      "theory": {"delay": ""}},
        }
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            config_path = folder / "config.json"
            config_path.write_text(json.dumps(original), encoding="utf-8")
            before = config_path.read_bytes()
            with patch.object(core, "CONFIG_PATH", config_path), patch.object(core, "ASSETS", folder):
                cfg = core.load_config()
            self.assertEqual(config_path.read_bytes(), before)
        self.assertEqual(core.site_delay(cfg, "prana"), 15)
        self.assertEqual(cfg["sites"]["prana"]["prefix"], "CUSTOM-")
        self.assertEqual(core.site_delay(cfg, "bestbuy"), 15)
        self.assertEqual(core.site_delay(cfg, "talbots"), 8)
        self.assertEqual(core.site_delay(cfg, "theory"), 9)
        self.assertEqual(core.SITE_DEFAULTS["bestbuy"]["delay"], "0")

    def test_prana_explicit_override_is_kept_and_blank_uses_global_delay(self):
        cfg = {"delay_seconds": 9, "sites": {"prana": {"delay": "4"}, "bestbuy": {"delay": "15"}}}
        original = copy.deepcopy(cfg)
        self.assertEqual(core.site_delay(cfg, "prana"), 4)
        self.assertEqual(core.site_delay(cfg, "bestbuy"), 15)
        self.assertEqual(cfg, original)
        for value, expected in (("22.5", 22.5), ("", 9), ("invalid", 9), ("-1", 0)):
            with self.subTest(value=value):
                cfg["sites"]["prana"]["delay"] = value
                self.assertEqual(core.site_delay(cfg, "prana"), expected)


if __name__ == "__main__":
    unittest.main()
