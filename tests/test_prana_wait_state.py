"""Waiting for prAna verification does not consume pending product rows."""
import copy
import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
import core
from sites import prana
from sites.base import SiteWaiting


URL = "https://www.prana.com/p/test/2080891.html?dwvar_2080891_color=Black"


def pending(number):
    return {"id": f"r{number}", "url": URL, "status": "error", "error": "Chưa chạy — chờ truy cập",
            "profile": "US1", "country": "US", "category": "", "custom_note": f"keep {number}"}


class PranaWaitStateTests(unittest.TestCase):
    def setUp(self):
        self.engine = app.Engine.__new__(app.Engine)
        self.engine.lock = threading.Lock()
        self.engine.jobs = queue.Queue()
        self.session = app.Session(prana)
        self.session.results = [pending(1), pending(2)]
        self.session.progress = [0, 2]
        self.engine.sessions = {"prana": self.session}

    def wait(self):
        self.session.wait_reason = "prAna đang chờ xác minh"
        self.session.wait_until = time.time() + 60

    def assert_no_mutation(self, callback):
        before = copy.deepcopy(self.session.results)
        progress = self.session.progress[:]
        with self.assertRaisesRegex(RuntimeError, "chờ xác minh"):
            callback()
        self.assertEqual(self.session.results, before)
        self.assertEqual(self.session.progress, progress)
        self.assertFalse(self.session.running)
        self.assertTrue(self.engine.jobs.empty())

    def test_resume_and_single_row_retry_are_rejected_before_queue_or_row_changes(self):
        self.wait()
        for arguments in ({"only": "pending"}, {"rid": "r1"}):
            with self.subTest(arguments=arguments):
                self.assert_no_mutation(lambda: self.engine.retry_errors("prana", False, **arguments))

    def test_new_run_is_rejected_before_clearing_the_existing_list(self):
        self.wait()
        self.assert_no_mutation(lambda: self.engine.submit("prana", [{"url": URL}], False))

    def test_append_is_rejected_before_queue_or_row_changes(self):
        self.wait()
        self.session.results.append({"id": "r3", "status": "ok", "url": URL})
        self.assert_no_mutation(lambda: self.engine.append_links("prana", "r3", [URL], False))

    def test_expired_wait_allows_the_same_pending_rows_to_resume(self):
        self.session.wait_reason = "prAna đang chờ xác minh"
        self.session.wait_until = time.time() - 1
        self.assertEqual(self.engine.retry_errors("prana", False, only="pending"), 2)
        queued_session, items, upload = self.engine.jobs.get_nowait()
        self.assertIs(queued_session, self.session)
        self.assertEqual([item["retry_id"] for item in items], ["r1", "r2"])
        self.assertFalse(upload)
        self.assertEqual(self.session.wait_reason, "")

    def test_snapshot_distinguishes_manual_wait_and_timed_wait(self):
        self.session.running = True
        self.session.wait_reason = "Xác minh trong cửa sổ prAna"
        state = self.engine.snapshot("prana", 0)
        self.assertEqual(state["wait"], {"reason": self.session.wait_reason, "until": 0.0, "active": True})
        self.session.running = False
        self.wait()
        state = self.engine.snapshot("prana", 0)
        self.assertTrue(state["wait"]["active"])
        self.assertGreater(state["wait"]["until"], time.time())

    def test_other_sites_are_not_blocked_by_prana_wait_fields(self):
        mod = SimpleNamespace(KEY="hollister", NAME="Hollister")
        other = app.Session(mod)
        other.wait_reason, other.wait_until = "prAna đang chờ xác minh", time.time() + 600
        other.results = [pending(1)]
        self.engine.sessions["hollister"] = other
        self.assertEqual(self.engine.retry_errors("hollister", False, only="pending"), 1)
        self.assertFalse(self.engine.snapshot("hollister", 0)["wait"]["active"])

    def run_until_waiting(self, items):
        cfg = {"sites": {"prana": {"use_proxy": False}}, "price_percent": 70, "ai_titles": False}
        error = SiteWaiting("prAna đang chờ xác minh", retry_after=60)
        with patch.object(core, "load_config", return_value=cfg), \
             patch.object(core, "load_json", return_value={}), \
             patch.object(core, "proxies_for", return_value=[]), \
             patch.object(core, "site_proxy_lines", return_value=[]), \
             patch.object(self.engine, "_read_product", side_effect=error) as read, \
             patch.object(self.engine, "_fetcher", return_value=Mock()):
            self.engine._run(self.session, items, upload=False)
        self.assertEqual(read.call_count, 1)

    def test_waiting_preserves_current_pending_row_and_remaining_retry_rows_exactly(self):
        before = copy.deepcopy(self.session.results)
        items = [{"url": row["url"], "profile": row["profile"], "country": row["country"], "retry_id": row["id"]}
                 for row in before]
        self.run_until_waiting(items)
        self.assertEqual(self.session.results, before)
        self.assertTrue(all(app.is_pending(row) for row in self.session.results))
        self.assertGreater(self.session.wait_until, time.time())
        self.assertEqual(self.session.progress, [0, 2])

    def test_new_rows_interrupted_by_verification_are_saved_as_pending(self):
        self.session.results = []
        self.run_until_waiting([{"url": URL, "profile": "US1", "country": "US"},
                                {"url": URL, "profile": "US3", "country": "US"}])
        self.assertEqual(len(self.session.results), 2)
        self.assertEqual(len({row["id"] for row in self.session.results}), 2)
        self.assertTrue(all(app.is_pending(row) for row in self.session.results))


if __name__ == "__main__":
    unittest.main()
