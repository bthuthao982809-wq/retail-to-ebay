import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core


def prod(site="eileenfisher", brand="Eileen Fisher"):
    return {"site": site, "brand": brand, "title": "Organic Linen Box-Top", "department": "women"}


class SiteBrandTitleTests(unittest.TestCase):
    def test_site_switch_off_drops_brand_only_for_that_site(self):
        cfg = {"brand_in_title": True, "sites": {"eileenfisher": {"brand_in_title": False}, "theory": {}}}
        self.assertEqual(core.make_title(prod(), cfg), "Women's Organic Linen Box-Top")
        self.assertEqual(core.make_title(prod("theory", "Theory"), cfg), "Theory Women's Organic Linen Box-Top")

    def test_missing_site_value_keeps_brand(self):   # config cũ chưa có ô này -> như trước (có tên hãng)
        cfg = {"brand_in_title": True, "sites": {"eileenfisher": {"prefix": "EF-"}}}
        self.assertEqual(core.title_brand(prod(), cfg), "Eileen Fisher")

    def test_general_off_wins_over_site_on(self):
        cfg = {"brand_in_title": False, "sites": {"eileenfisher": {"brand_in_title": True}}}
        self.assertEqual(core.title_brand(prod(), cfg), "")

    def test_disabled_also_removes_brand_left_in_source_name(self):
        product = dict(prod(), title="Eileen Fisher Organic Linen Box-Top")
        cfg = {"brand_in_title": True, "sites": {"eileenfisher": {"brand_in_title": False}}}
        self.assertEqual(core.make_title(product, cfg), "Women's Organic Linen Box-Top")
        cfg["sites"]["eileenfisher"]["brand_in_title"] = True
        self.assertEqual(core.make_title(product, cfg), "Eileen Fisher Women's Organic Linen Box-Top")

    def test_update_config_saves_site_switch_and_keeps_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, "CONFIG_PATH", Path(tmp) / "config.json"):
            cfg = core.update_config({"sites": {"eileenfisher": {"brand_in_title": False}}})
            self.assertIs(cfg["sites"]["eileenfisher"]["brand_in_title"], False)
            self.assertEqual(cfg["sites"]["eileenfisher"]["prefix"], "EF-")
            saved = json.loads((Path(tmp) / "config.json").read_text(encoding="utf-8"))
            self.assertIs(saved["sites"]["eileenfisher"]["brand_in_title"], False)
            self.assertNotIn("brand_in_title", saved["sites"]["theory"])   # site khác không bị đổi


if __name__ == "__main__":
    unittest.main()
