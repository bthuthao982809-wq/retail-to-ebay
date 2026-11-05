import copy
import threading
import unittest
from unittest.mock import patch

import app
import core
from sites import bestbuy


class TitleBrandRowsTests(unittest.TestCase):
    def setUp(self):
        self.e = app.Engine.__new__(app.Engine)
        self.e.lock = threading.Lock()
        self.s = app.Session(bestbuy)
        self.e.sessions = {"bestbuy": self.s}
        self.s.results = [
            {"id": "r1", "status": "ok", "title": "AI Vacuum, Blue", "title_extra": "Blue",
             "title_src": "ai", "title_orig": "Original", "variant_sel": ["v1"],
             "product": {"brand": "Shark", "site": "bestbuy"}},
            {"id": "r2", "status": "ok", "title": "Y" * 78, "product": {"brand": "Dyson"}},
            {"id": "r3", "status": "ok", "title": "Unknown", "product": {}},
            {"id": "r4", "status": "error", "title": "Error"},
        ]

    def apply(self, ids=None, add=True, titles=None):
        with patch.object(core, "load_config", return_value={"brand_in_title": False}), \
             patch.object(self.e, "save_session"):
            return self.e.title_brand_rows("bestbuy", ids or [], add, titles)

    def test_selected_preserves_ai_suffix_and_variants(self):
        untouched = copy.deepcopy(self.s.results[1:])
        result = self.apply(["r1"])
        r = self.s.results[0]
        self.assertEqual(r["title"], "Shark AI Vacuum, Blue")
        self.assertEqual(r["title_base"], "Shark AI Vacuum")
        self.assertEqual((r["title_src"], r["title_orig"], r["variant_sel"]), ("ai", "Original", ["v1"]))
        self.assertEqual(self.s.results[1:], untouched)
        self.assertEqual(result["ids"], ["r1"])
        self.assertTrue(self.s.dirty)

    def test_all_skips_unknown_and_error_and_counts_overlength(self):
        result = self.apply()
        self.assertEqual((result["changed"], result["skipped"], result["over"]), (2, 1, 1))
        self.assertEqual(result["ids"], ["r1", "r2"])
        self.assertEqual(self.s.results[2]["title"], "Unknown")

    def test_add_twice_is_idempotent_and_remove_keeps_rest(self):
        self.apply(["r1"])
        self.assertEqual(self.apply(["r1"])["changed"], 0)
        self.apply(["r1"], False)
        self.assertEqual(self.s.results[0]["title"], "AI Vacuum, Blue")
        self.assertFalse(self.s.results[0]["title_brand_override"])

    def test_current_edits_are_used_only_on_selected_rows(self):
        self.apply(["r1"], titles={"r1": "Edited Vacuum, Blue", "r2": "Ignore"})
        self.assertEqual(self.s.results[0]["title"], "Shark Edited Vacuum, Blue")
        self.assertEqual(self.s.results[1]["title"], "Y" * 78)

    def test_busy_rejects_without_changes(self):
        self.s.ai_running = True
        before = copy.deepcopy(self.s.results)
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.s.results, before)

    def test_row_override_preserves_brand_during_ai_after_global_switch_off(self):
        self.apply(["r1"])
        row = self.s.results[0]
        self.assertEqual(core.title_brand(row["product"], {"brand_in_title": False}, row["title_brand_override"]), "Shark")
        self.apply(["r1"], False)
        self.assertEqual(core.title_brand(row["product"], {"brand_in_title": True}, row["title_brand_override"]), "")


if __name__ == "__main__":
    unittest.main()
