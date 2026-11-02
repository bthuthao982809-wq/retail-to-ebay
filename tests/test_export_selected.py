import threading
import unittest
from unittest.mock import patch

import app
import core
from sites import bestbuy


class ExportSelectedTests(unittest.TestCase):
    def setUp(self):
        self.e = app.Engine.__new__(app.Engine)
        self.e.lock, self.e.save_lock, self.e.sessions = threading.Lock(), threading.Lock(), {}
        self.s = app.Session(bestbuy)
        self.e.sessions["bestbuy"] = self.s
        self.s.run_id, self.s.dirty = "x", True
        self.s.results = [{"id": f"r{i}", "status": "ok", "title": f"t{i}"} for i in range(1, 4)]
        self.got = {}

    def run_export(self, ids):
        def fake(items, cfg, run_id, kind, folder=None, log=None, step=None):
            self.got["ids"] = [i["id"] for i in items]
            self.got["titles"] = {i["id"]: i["title"] for i in items}
            return [], []
        with patch.object(core, "export_run", side_effect=fake), patch.object(self.e, "_track_exported"), \
             patch.object(self.e, "save_session"):
            self.e.export("bestbuy", {"r2": "moi", "r3": "khac"}, "C:/tmp", ids)

    def test_only_ticked_rows_exported_and_full_export_state_kept(self):
        self.run_export(["r2"])
        self.assertEqual(self.got["ids"], ["r2"])
        self.assertEqual(self.got["titles"], {"r2": "moi"})
        self.assertTrue(self.s.dirty)                       # bộ đầy đủ vẫn "chưa xuất"
        self.assertEqual(self.s.results[2]["title"], "t3")  # tiêu đề đang sửa của dòng khác không bị lấy
        self.assertIn("Đang xuất CSV cho 1 dòng đã tích…", [l["text"] for l in self.s.logs])

    def test_no_tick_exports_all(self):
        self.run_export(None)
        self.assertEqual(self.got["ids"], ["r1", "r2", "r3"])
        self.assertFalse(self.s.dirty)


if __name__ == "__main__":
    unittest.main()
