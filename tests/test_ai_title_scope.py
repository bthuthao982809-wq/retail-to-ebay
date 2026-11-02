import threading
import unittest
from unittest.mock import patch

import app
import core
from sites import bestbuy


class AiTitleScopeTests(unittest.TestCase):
    def rewrite(self, ids):
        engine = app.Engine.__new__(app.Engine)
        engine.lock = threading.Lock()
        session = app.Session(bestbuy)
        session.results = [
            {"id": "normal", "status": "ok", "title": "Normal"},
            {"id": "long", "status": "ok", "title": "X" * 81},
            {"id": "warning", "status": "ok", "title": "Short", "title_warn": "Check category"},
            {"id": "failed", "status": "error", "title": "X" * 90},
        ]
        engine.sessions = {"bestbuy": session}
        with patch.object(core, "load_config", return_value={}), \
             patch.object(app.ai_title, "configured", return_value=True), \
             patch.object(threading.Thread, "start"):
            return engine.ai_rewrite("bestbuy", ids)

    def test_default_includes_long_and_warning_only(self):
        self.assertEqual(self.rewrite([]), 2)

    def test_selected_normal_row_is_allowed(self):
        self.assertEqual(self.rewrite(["normal"]), 1)

    def test_selected_two_rows_do_not_expand_to_other_warnings(self):
        self.assertEqual(self.rewrite(["normal", "long"]), 2)


if __name__ == "__main__":
    unittest.main()
