"""prAna retries the current request with Best Buy's bounded recovery pacing."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sites import bestbuy, prana_browser
from sites.base import BrowserLaunchError, ProxyPool, SiteWaiting, SkipProduct
from sites.prana_browser import PranaBrowserFetcher
from test_prana_browser import API, PDP, FakePage, data


CHALLENGE = {"challenge": True, "ready": False, "pid": ""}
WRONG = {"challenge": False, "ready": True, "pid": "2080891", "master_pid": "2080891"}
LOADING = {"challenge": False, "ready": False, "pid": ""}


def proxy(number):
    return {"server": f"http://198.51.100.{number}:5000"}


def challenged():
    return FakePage(states=[dict(CHALLENGE)] * 100)


class PranaBestBuyRecoveryTests(unittest.TestCase):
    def fetcher(self, pages, proxies=None, **kwargs):
        factory = Mock(side_effect=pages)
        events = []
        close = Mock(side_effect=lambda: events.append("close"))
        f = PranaBrowserFetcher(page_factory=factory, pool=ProxyPool(proxies or []), min_gap=0,
                                reset_browser=close, set_wait=Mock(), clear_wait=Mock(),
                                verification_timeout=0, **kwargs)
        f.prepare_product(PDP)
        f.LOAD_TIMEOUT = 0
        f._pause = Mock(side_effect=lambda seconds: events.append(("pause", seconds)))
        return f, factory, close, events

    def test_pacing_matches_best_buy(self):
        self.assertEqual(prana_browser.BLOCK_PAUSE, bestbuy.BLOCK_PAUSE)
        self.assertEqual(prana_browser.MAX_IPS, bestbuy.MAX_IPS)
        self.assertEqual(prana_browser.MAX_ORDINARY_ATTEMPTS, 3)

    def test_visible_verification_closes_pauses_then_reopens_exact_link(self):
        first, recovered = challenged(), FakePage()
        f, factory, close, events = self.fetcher([first, recovered], [proxy(1)])
        self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
        self.assertEqual([call.args[0] for call in factory.call_args_list], [None, proxy(1)])
        self.assertEqual(events, ["close", ("pause", 30)])
        self.assertEqual(first.gotos[0][0], PDP)
        self.assertEqual(recovered.gotos[0][0], PDP)
        self.assertEqual(recovered.fetches[0]["url"], API)
        self.assertEqual(f.ip_stats["IP gốc"]["blocked"], 1)
        self.assertEqual(f.ip_stats["198.51.100.1:5000"]["ok"], 1)

    def test_json_403_renews_ready_session_once_before_changing_route(self):
        response = {"status": 403, "text": '<div id="px-captcha">Verify</div>'}
        blocked = FakePage([response, response])
        recovered = FakePage()
        f, factory, close, _ = self.fetcher([blocked, recovered], [proxy(1)])
        self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
        self.assertEqual([call["url"] for call in blocked.fetches + recovered.fetches], [API, API, API])
        self.assertEqual(len(blocked.gotos), 2, "Renew the matching product once before changing route")
        self.assertEqual(factory.call_count, 2)
        close.assert_called_once()

    def test_transient_json_block_uses_same_session_without_closing_browser(self):
        for response in ({"status": 403, "text": "blocked"}, {"status": 429, "text": "limited"},
                         {"status": 200, "text": '<div id="px-captcha">Verify</div>'}):
            with self.subTest(response=response):
                page = FakePage([response, data()])
                f, factory, close, _ = self.fetcher([page], [proxy(1)])
                self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
                self.assertEqual([call["url"] for call in page.fetches], [API, API])
                self.assertEqual(len(page.gotos), 2)
                factory.assert_called_once_with(None)
                close.assert_not_called()
                f._pause.assert_not_called()
                self.assertFalse(any(stat["blocked"] for stat in f.ip_stats.values()))

    def test_challenge_during_same_session_refresh_still_changes_route(self):
        ready = {"challenge": False, "ready": True, "pid": "1973521"}
        first = FakePage([{"status": 403, "text": "blocked"}],
                         states=[dict(ready), dict(ready), dict(CHALLENGE)])
        recovered = FakePage()
        f, factory, close, _ = self.fetcher([first, recovered], [proxy(1)])
        self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
        self.assertEqual(len(first.gotos), 2)
        self.assertEqual(len(first.fetches), 1, "Do not read JSON through a visible challenge")
        self.assertEqual(factory.call_count, 2)
        close.assert_called_once()

    def test_same_route_is_not_renewed_twice_after_an_ordinary_retry(self):
        blocked = {"status": 403, "text": "blocked"}
        first = FakePage([blocked, {"status": 503, "text": "unavailable"}, blocked])
        recovered = FakePage()
        f, factory, close, _ = self.fetcher([first, recovered], [proxy(1)])
        self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
        self.assertEqual(len(first.fetches), 3)
        # Initial navigation, one blocked-JSON renewal, one ordinary-error retry.
        self.assertEqual(len(first.gotos), 3)
        self.assertEqual(factory.call_count, 2)
        close.assert_called_once()

    def test_matching_ready_product_ignores_stale_navigation_block_status(self):
        for status in (403, 429):
            with self.subTest(status=status):
                page = FakePage(states=[{"challenge": False, "ready": True,
                                         "pid": "1973521-025-RX-M", "master_pid": "1973521"}])
                page.navigation_status = status
                f, factory, close, _ = self.fetcher([page], [proxy(1)])
                self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
                self.assertEqual(len(page.fetches), 1)
                factory.assert_called_once_with(None)
                close.assert_not_called()
                f._pause.assert_not_called()
                f.set_wait.assert_not_called()
                self.assertFalse(any(stat["blocked"] for stat in f.ip_stats.values()))

    def test_matching_ready_product_survives_navigation_timeout(self):
        page = FakePage()
        page.goto_error = RuntimeError("Timeout 45000ms exceeded at http://user:secret@198.51.100.1:5000")
        log = Mock()
        f, factory, close, _ = self.fetcher([page], [proxy(1)], log=log)
        self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
        self.assertEqual(len(page.fetches), 1)
        factory.assert_called_once_with(None)
        close.assert_not_called()
        f._pause.assert_not_called()
        f.set_wait.assert_not_called()
        self.assertNotIn("secret", str(log.call_args_list))
        self.assertFalse(any(stat["blocked"] for stat in f.ip_stats.values()))

    def test_unready_navigation_block_and_timeout_still_change_route(self):
        for state in (CHALLENGE, WRONG):
            for failure in (403, 429, "timeout"):
                with self.subTest(state=state, failure=failure):
                    first, recovered = FakePage(states=[dict(state)] * 100), FakePage()
                    if failure == "timeout":
                        first.goto_error = RuntimeError("Timeout 45000ms exceeded at http://user:secret@198.51.100.1:5000")
                    else:
                        first.navigation_status = failure
                    log = Mock()
                    f, factory, close, _ = self.fetcher([first, recovered], [proxy(1)], log=log)
                    self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
                    self.assertEqual(first.fetches, [])
                    self.assertEqual(len(recovered.fetches), 1)
                    self.assertEqual(factory.call_count, 2)
                    close.assert_called_once()
                    reasons = [call.args[0] for call in log.call_args_list if "đổi đường truy cập vì" in call.args[0]]
                    self.assertEqual(len(reasons), 1)
                    self.assertNotIn("secret", str(log.call_args_list))
                    self.assertTrue(any(stat["blocked"] for stat in f.ip_stats.values()))

    def test_route_cap_counts_direct_and_deduplicates_proxy_servers(self):
        proxies = [proxy(1), proxy(1)] + [proxy(i) for i in range(2, 13)]
        f, factory, close, _ = self.fetcher([challenged() for _ in range(8)], proxies)
        with self.assertRaises(SiteWaiting):
            f.get_json(API)
        routes = [call.args[0]["server"] if call.args[0] else "direct" for call in factory.call_args_list]
        self.assertEqual(len(routes), 8)
        self.assertEqual(len(set(routes)), 8)
        self.assertEqual(close.call_count, 7)
        self.assertEqual(f._pause.call_count, 7)
        self.assertEqual(len(f._attempted_routes), 8)

    def test_route_budget_is_shared_by_color_requests_for_one_prepared_link(self):
        first = challenged()
        blocked = {"status": 403, "text": '{"appId":"PXTEST"}'}
        second = FakePage([data(), blocked, blocked])
        third = challenged()
        f, factory, close, _ = self.fetcher([first, second, third], [proxy(1), proxy(2), proxy(3)])
        with patch.object(prana_browser, "MAX_IPS", 3):
            f.get_json(API)
            f.prepare_product(PDP)
            with self.assertRaises(SiteWaiting):
                f.get_json(API + "&color=Black")
            with self.assertRaises(SiteWaiting):
                f.get_json(API + "&color=Navy")
        self.assertEqual(factory.call_count, 3)
        self.assertEqual(len(f._attempted_routes), 3)
        self.assertTrue(f.pool.has_ready(exclude=f._attempted_routes), "An untried fourth route exists, but the link cap prevents using it")
        f.prepare_product(PDP + "&new-link=1")
        self.assertEqual(f._attempted_routes, {proxy(2)["server"]})

    def test_no_available_proxy_keeps_existing_manual_wait(self):
        f, factory, close, _ = self.fetcher([challenged()], [proxy(1)])
        f.pool.blocked_servers[proxy(1)["server"]] = time.time() + 60
        with self.assertRaises(SiteWaiting):
            f.get_json(API)
        factory.assert_called_once_with(None)
        close.assert_not_called()
        f._pause.assert_not_called()
        self.assertIn("xác minh", f.set_wait.call_args.args[0])

    def test_wrong_product_is_not_treated_as_an_ip_block(self):
        wrong = FakePage(states=[dict(WRONG)] * 100)
        f, factory, close, _ = self.fetcher([wrong], [proxy(1)])
        with self.assertRaises(SiteWaiting):
            f.get_json(API)
        self.assertEqual(len(wrong.gotos), 3)
        close.assert_not_called()
        f._pause.assert_not_called()
        self.assertFalse(any(stat["blocked"] for stat in f.ip_stats.values()))
        self.assertNotIn("xác minh", f.set_wait.call_args.args[0])

    def test_cancel_during_block_pause_never_opens_the_next_browser(self):
        stopped = threading.Event()
        f, factory, close, _ = self.fetcher([challenged()], [proxy(1)], should_stop=stopped.is_set)
        del f._pause
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds
            stopped.set()

        with patch.object(prana_browser.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(prana_browser.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(SkipProduct, "Đã dừng"):
                f.get_json(API)
        factory.assert_called_once_with(None)
        close.assert_called_once()
        f.clear_wait.assert_called()

    def test_proxy_connection_failure_is_dropped_without_block_pause(self):
        broken, recovered = FakePage(), FakePage()
        broken.goto_error = RuntimeError("NS_ERROR_PROXY_CONNECTION_REFUSED")
        f, factory, close, _ = self.fetcher([broken, recovered], [proxy(1), proxy(2)], use_proxy=True)
        f.get_json(API)
        self.assertEqual(f.pool.proxies, [proxy(2)])
        self.assertEqual(f.current_proxy(), proxy(2))
        f._pause.assert_not_called()
        self.assertFalse(any(stat["blocked"] for stat in f.ip_stats.values()))
        close.assert_called_once()

    def test_broken_last_proxy_is_also_removed(self):
        broken = FakePage()
        broken.goto_error = RuntimeError("NS_ERROR_PROXY_CONNECTION_REFUSED")
        f, _, _, _ = self.fetcher([broken], [proxy(1)], use_proxy=True)
        with self.assertRaises(SiteWaiting):
            f.get_json(API)
        self.assertEqual(f.pool.proxies, [])
        f._pause.assert_not_called()

    def test_browser_launch_failure_does_not_rotate_or_mark_ip_blocked(self):
        f, factory, close, _ = self.fetcher([], [proxy(1)])
        factory.side_effect = BrowserLaunchError("driver unavailable")
        with self.assertRaises(BrowserLaunchError):
            f.get_json(API)
        factory.assert_called_once()
        close.assert_not_called()
        f._pause.assert_not_called()
        self.assertEqual(f.ip_stats, {})

    def test_404_is_a_product_error_without_rotation(self):
        missing = FakePage()
        missing.navigation_status = 404
        f, factory, close, _ = self.fetcher([missing], [proxy(1)])
        with self.assertRaisesRegex(SkipProduct, "404"):
            f.get_json(API)
        factory.assert_called_once()
        close.assert_not_called()
        f._pause.assert_not_called()

    def test_json_404_containing_bot_markup_does_not_rotate(self):
        missing = FakePage([{"status": 404, "text": '<div id="px-captcha">not found</div>'}])
        f, factory, close, _ = self.fetcher([missing], [proxy(1)])
        with self.assertRaisesRegex(SkipProduct, "404"):
            f.get_json(API)
        factory.assert_called_once()
        close.assert_not_called()
        self.assertEqual(f.ip_stats, {})

    def test_transient_server_error_has_three_attempts_without_an_ip_block(self):
        page = FakePage([{"status": 503, "text": "unavailable"}] * 3)
        f, factory, close, _ = self.fetcher([page], [proxy(1)])
        with self.assertRaisesRegex(SkipProduct, "503"):
            f.get_json(API)
        self.assertEqual(len(page.fetches), 3)
        close.assert_not_called()
        f._pause.assert_not_called()
        self.assertEqual(f.ip_stats, {})

    def test_engine_connects_only_prana_to_its_owned_browser_reset(self):
        import app
        engine = app.Engine.__new__(app.Engine)
        engine.lock = threading.Lock()
        engine.log = Mock()
        browser = SimpleNamespace(page=Mock(), close=Mock())
        session = SimpleNamespace(kind="prana", pool=ProxyPool([]), browser=browser,
                                  stop=threading.Event(), mod=SimpleNamespace(MIN_GAP=2))
        fetcher = engine._fetcher(session, {"sites": {"prana": {"use_proxy": False}}})
        self.assertEqual(fetcher.reset_browser, browser.close)
        self.assertEqual(fetcher.verification_timeout, 300)
        self.assertEqual(fetcher.min_gap, 2)
        browser.close.assert_not_called()

    def test_ordinary_load_failure_retries_at_most_three_times_on_same_route(self):
        page = FakePage(states=[dict(LOADING)] * 100)
        page.goto_error = RuntimeError("document could not be read")
        f, factory, close, _ = self.fetcher([page], [proxy(1)])
        with self.assertRaises(SiteWaiting):
            f.get_json(API)
        self.assertEqual(len(page.gotos), 3)
        close.assert_not_called()
        f._pause.assert_not_called()
        self.assertEqual(f.ip_stats, {})


if __name__ == "__main__":
    unittest.main()
