import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
import core
import tracking


URL = "https://www.prana.com/p/pants/2080191.html?dwvar_2080191_color=Spruce"
CFG = {"price_percent": 100, "round_99": False, "quantity": 2, "category_rules": [],
       "sites": {"prana": {"use_proxy": False}}}


def product(variants):
    return {"site": "prana", "code": "2080191", "url": URL, "title": "Cargo Pants",
            "brand": "prAna", "department": "Women", "size_name": "Size", "bullets": [],
            "details": {}, "breadcrumbs": ["Women", "Pants"], "colors": [],
            "images": ["https://example.test/pants.jpg"], "variants": variants}


def variant(size, source_size=None):
    v = {"id": "site-" + size, "color": "Spruce", "size": size, "price": 104,
         "list_price": None, "available": True}
    if source_size is not None:
        v["_site_size"] = source_size
    return v


def item(prod, sku="TEST-PRA-2080191"):
    return {"sku": sku, "profile": "TEST", "country": "US", "site": "prana", "url": URL,
            "title": "Cargo Pants, Spruce", "product": prod, "category": "63863",
            "pics": {"main": ["https://example.test/pants.jpg"]}}


class PranaTrackingTests(unittest.TestCase):
    def test_clean_export_sizes_keep_source_keys_for_single_and_multiple_variants(self):
        for suffix, sizes in (("31", ("2",)), ("31", ("2", "4")),
                              ("Reg", ("S",)), ("Reg", ("S", "M"))):
            with self.subTest(suffix=suffix, sizes=sizes):
                clean = product([variant(s, f"{s} {suffix}") for s in sizes])
                current = product([variant(f"{s} {suffix}") for s in sizes])
                listed = item(clean)
                rows = core.listing_rows(listed, CFG)
                rows = rows[1:] if len(rows) > 1 else rows
                self.assertEqual([r["_vkey"] for r in rows],
                                 [f"Spruce||{s} {suffix}" for s in sizes])
                if len(sizes) == 1:
                    self.assertEqual(rows[0]["*C:Size"], sizes[0])
                else:
                    self.assertEqual([r["RelationshipDetails"] for r in rows],
                                     [f"Size={s}" for s in sizes])
                tracked = tracking.entry_from(listed, CFG, "prana")
                checked = tracking.check_listing(tracked, current, CFG)
                self.assertEqual({r["state"] for r in checked.values()}, {"same"})
                self.assertTrue(all(r["available"] for r in checked.values()))

    def run_tracking(self, mod, entries):
        engine = app.Engine.__new__(app.Engine)
        engine.lock = threading.Lock()
        engine.sessions = {"prana": app.Session(mod)}
        engine.track = {"progress": [0, len(entries)], "logs": [], "stop": threading.Event(), "current": ""}
        data = {"items": {e["sku"]: copy.deepcopy(e) for e in entries}}
        fetcher = object()
        with patch.object(core, "load_config", return_value=CFG), \
             patch.object(core, "proxies_for", return_value=[]), \
             patch.object(core, "site_proxy_file", return_value=None), \
             patch.object(app, "Fetcher", return_value=fetcher), \
             patch.object(tracking, "load", return_value=data), \
             patch.object(tracking, "update", side_effect=lambda fn: fn(data)), \
             patch.object(tracking, "run_left", return_value=[]), \
             patch.object(tracking, "run_clear"):
            engine._track_run(list(data["items"]), Mock())
        return data, fetcher

    def test_tracking_hook_keeps_legacy_lengths_and_sizes_excluded_by_new_rule(self):
        raw = product([variant("0 29"), variant("2 31"), variant("18 33")])
        clean = product([variant("2", "2 31")])
        legacy = tracking.entry_from(item(raw, "OLD-PRA-2080191"), CFG, "prana")
        new = tracking.entry_from(item(clean, "NEW-PRA-2080191"), CFG, "prana")
        mod = SimpleNamespace(KEY="prana", NAME="prAna", scrape=Mock(return_value=clean),
                              scrape_for_tracking=Mock(return_value=raw))
        data, fetcher = self.run_tracking(mod, [legacy, new])
        mod.scrape.assert_not_called()
        mod.scrape_for_tracking.assert_called_once_with(fetcher, URL)
        for entry in data["items"].values():
            self.assertEqual(entry["check"]["error"], "")
            checks = entry["check"]["vars"]
            self.assertEqual(len(checks), len(entry["variants"]))
            self.assertEqual({r["state"] for r in checks.values()}, {"same"})
            self.assertTrue(all(r["available"] for r in checks.values()))

    def test_csv_only_import_can_match_new_variants_without_saved_source_keys(self):
        clean = product([variant("2", "2 31"), variant("4", "4 31")])
        raw = product([variant("0 29"), variant("2 31"), variant("4 31"), variant("18 33")])
        imported = tracking.entry_from(item(clean), CFG, "prana")
        # Import CSV ở máy khác chỉ có SKU biến thể, không có khóa màu/size gốc trong session.
        for v in imported["variants"]:
            v.update(key="", color="", size="", site_price=None, site_list=None)
        checked = tracking.check_listing(imported, raw, CFG)
        self.assertEqual({r["state"] for r in checked.values()}, {"same"})
        self.assertTrue(all(r["available"] for r in checked.values()))

    def test_site_without_tracking_hook_still_uses_normal_scraper(self):
        raw = product([variant("M")])
        entry = tracking.entry_from(item(raw), CFG, "prana")
        mod = SimpleNamespace(KEY="prana", NAME="prAna", scrape=Mock(return_value=raw))
        data, fetcher = self.run_tracking(mod, [entry])
        mod.scrape.assert_called_once_with(fetcher, URL)
        checks = data["items"][entry["sku"]]["check"]["vars"]
        self.assertEqual({r["state"] for r in checks.values()}, {"same"})


if __name__ == "__main__":
    unittest.main()
