"""The prAna transport uses one browser page and waits for human verification."""
import json
import threading
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sites.base import BrowserLaunchError, ProxyPool, SiteWaiting, SkipProduct
from sites.prana_browser import FETCH_JSON, PAGE_STATE, PranaBrowserFetcher
from sites.sfcc import GE_COOKIE, US_HEADERS, get_variation


PDP = "https://www.prana.com/p/koen-mid-rise-pant/1973521.html?dwvar_1973521_color=Grey%20Blue"
API = "https://www.prana.com/on/demandware.store/Sites-Prana_US-Site/en_US/Product-Variation?pid=1973521&format=ajax"
PROXY = {"server": "http://198.51.100.10:5000", "username": "user", "password": "secret"}


def data(product="1973521", color="Grey Blue"):
    return {"status": 200, "text": json.dumps({"product": {"id": product, "color": color}})}


class FakePage:
    def __init__(self, responses=None, states=None):
        self.responses = deque(responses or [data()])
        self.states = deque(states or [])
        self.context = SimpleNamespace(add_cookies=Mock())
        self.pid = "1973521"
        self.closed = False
        self.url = "about:blank"
        self.gotos = []
        self.fetches = []
        self.front = 0
        self.clock = 100.0
        self.on_wait = None
        self.navigation_status = 200
        self.goto_error = None

    def is_closed(self):
        return self.closed

    def goto(self, url, **kwargs):
        self.url = url
        self.gotos.append((url, kwargs))
        if self.goto_error:
            raise self.goto_error
        return SimpleNamespace(status=self.navigation_status)

    def evaluate(self, script, arg=None):
        if script == PAGE_STATE:
            return self.states.popleft() if self.states else {"challenge": False, "ready": True, "pid": self.pid}
        if script == FETCH_JSON:
            self.fetches.append(arg)
            return self.responses.popleft()
        raise AssertionError("Unexpected browser script")

    def bring_to_front(self):
        self.front += 1

    def wait_for_timeout(self, milliseconds):
        self.clock += milliseconds / 1000
        if self.on_wait:
            self.on_wait()


class PranaBrowserTests(unittest.TestCase):
    def fetcher(self, page=None, **kwargs):
        page = page or FakePage()
        factory = Mock(return_value=page)
        wait, clear = Mock(), Mock()
        fetcher = PranaBrowserFetcher(page_factory=factory, set_wait=wait, clear_wait=clear,
                                      min_gap=0, **kwargs)
        fetcher.prepare_product(PDP)
        return fetcher, page, factory, wait, clear

    def test_variation_requests_share_pdp_browser_and_cookie_storage(self):
        page = FakePage([data(), data(color="Black")])
        f, page, factory, wait, clear = self.fetcher(page)
        with patch("curl_cffi.requests.get") as http:
            first = get_variation(f, "www.prana.com", "Sites-Prana_US-Site", "1973521", {"color": "Grey Blue"})
            f.prepare_product(PDP.replace("Grey%20Blue", "Black"))
            second = get_variation(f, "www.prana.com", "Sites-Prana_US-Site", "1973521", {"color": "Black"})
        self.assertEqual((first["color"], second["color"]), ("Grey Blue", "Black"))
        http.assert_not_called()
        factory.assert_called_once_with(None)
        self.assertEqual(len(page.gotos), 1)
        self.assertEqual(f.request_count, 2)
        cookie = page.context.add_cookies.call_args.args[0][0]
        self.assertEqual(cookie["name"], "GlobalE_Data")
        self.assertEqual(cookie["value"], GE_COOKIE.partition("=")[2])
        self.assertNotIn("Cookie", page.fetches[0]["headers"])
        self.assertEqual(page.fetches[0]["headers"]["X-Requested-With"], "XMLHttpRequest")
        self.assertIn("credentials: 'include'", FETCH_JSON)
        wait.assert_not_called()
        self.assertEqual(clear.call_count, 2)

    def test_color_changes_sample_a_gap_and_same_color_size_reads_keep_two_seconds(self):
        first = {"status": 200, "text": json.dumps({"product": {
            "id": "1973521",
            "variationAttributes": [{"attributeId": "color", "values": [
                {"value": "025", "displayValue": "Grey Blue", "selected": True},
                {"value": "001", "displayValue": "Black"},
            ]}],
        }})}
        page = FakePage([first, data(), data(color="Black"), data(color="Black"), data(),
                         data(product="2080891")])
        f, _, _, _, _ = self.fetcher(page)
        f.min_gap = 2.0
        times = []
        requests = [{"color": "Grey Blue"}, {"color": "025", "size": "M"},
                    {"color": "Black"}, {"color": "001", "size": "L"}, {"color": "025"}]
        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock), \
             patch("sites.prana_browser.time.sleep", side_effect=lambda sec: setattr(page, "clock", page.clock + sec)), \
             patch("sites.prana_browser.random.uniform", side_effect=[4.0, 7.0]) as gap:
            for selection in requests:
                get_variation(f, "www.prana.com", "Sites-Prana_US-Site", "1973521", selection)
                times.append(page.clock)
            page.pid = "2080891"
            f.prepare_product("https://www.prana.com/p/shea-soft-full-zip/2080891.html")
            get_variation(f, "www.prana.com", "Sites-Prana_US-Site", "2080891", {"color": "Black"})
            times.append(page.clock)
        self.assertEqual(times, [100.0, 102.0, 106.0, 108.0, 115.0, 117.0])
        self.assertEqual([call.args for call in gap.call_args_list], [(4.0, 7.0), (4.0, 7.0)])
        self.assertEqual(len(page.fetches), 6)

    def test_stop_during_color_gap_sends_no_next_request(self):
        stop = threading.Event()
        f, page, _, _, _ = self.fetcher(should_stop=stop.is_set)
        f.min_gap = 2.0

        def stop_during_sleep(seconds):
            page.clock += seconds
            stop.set()

        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock), \
             patch("sites.prana_browser.time.sleep", side_effect=stop_during_sleep), \
             patch("sites.prana_browser.random.uniform", return_value=6.0):
            get_variation(f, "www.prana.com", "Sites-Prana_US-Site", "1973521", {"color": "Grey Blue"})
            with self.assertRaisesRegex(SkipProduct, "Đã dừng"):
                get_variation(f, "www.prana.com", "Sites-Prana_US-Site", "1973521", {"color": "Black"})
        self.assertEqual(len(page.fetches), 1)

    def test_pinned_proxy_does_not_follow_pool_rotation(self):
        other = {"server": "http://203.0.113.20:5000"}
        pool = ProxyPool([dict(PROXY), other])
        f, page, factory, _, _ = self.fetcher(use_proxy=True, pool=pool)
        pool.rotate()
        f.get_json(API)
        factory.assert_called_once_with(PROXY)
        self.assertEqual(f.current_proxy(), PROXY)
        self.assertEqual(f.ip_label(), "198.51.100.10:5000")
        self.assertEqual(f._proxies()["https"], "http://user:secret@198.51.100.10:5000")

    def test_new_product_navigates_in_the_same_browser(self):
        f, page, factory, _, _ = self.fetcher(FakePage([data(), data(product="2080891")]))
        f.get_json(API)
        page.pid = "2080891"
        f.prepare_product("https://www.prana.com/p/shea-soft-full-zip/2080891.html")
        f.get_json(API.replace("1973521", "2080891"))
        self.assertEqual(len(page.gotos), 2)
        self.assertEqual(factory.call_count, 1)

    def test_selected_size_page_uses_its_explicit_master_product_id(self):
        # Actual DreamFleece markup uses a variant PID once size M is selected.
        page = FakePage([data(product="2162341")], states=[{
            "challenge": False, "ready": True, "pid": "2162341-020-RX-M", "master_pid": "2162341"
        }])
        f, page, _, wait, _ = self.fetcher(page)
        f.prepare_product("https://www.prana.com/p/dreamfleece/2162341.html?dwvar_2162341_size=M&dwvar_2162341_dimension=RX")
        result = f.get_json(API.replace("1973521", "2162341"))
        self.assertEqual(result["product"]["id"], "2162341")
        wait.assert_not_called()
        self.assertEqual(len(page.fetches), 1)
        self.assertIn("data-master-pid", PAGE_STATE)

    def test_variant_pid_fallback_requires_the_exact_master_delimiter(self):
        f, _, _, _, _ = self.fetcher()
        f.prepare_product("https://www.prana.com/p/dreamfleece/2162341.html")
        for pid in ("2162341", "2162341-020-RX-M"):
            with self.subTest(pid=pid):
                self.assertTrue(f._ready({"ready": True, "challenge": False, "pid": pid}))
        for pid in ("21623410-020-RX-M", "2162341", "2162341-020-RX-M"):
            with self.subTest(unrelated_master_pid=pid):
                self.assertFalse(f._ready({"ready": True, "challenge": False, "pid": pid,
                                           "master_pid": "21623410"}))
        self.assertFalse(f._ready({"ready": True, "challenge": False, "pid": "21623410-020-RX-M"}))
        self.assertFalse(f._ready({"ready": True, "challenge": False, "pid": "2162341-"}))

    def test_visible_captcha_over_matching_variant_still_waits(self):
        state = {"challenge": True, "ready": True, "pid": "2162341-020-RX-M", "master_pid": "2162341"}
        f, page, _, wait, _ = self.fetcher(FakePage(states=[state] * 5), verification_timeout=0)
        f.prepare_product("https://www.prana.com/p/dreamfleece/2162341.html")
        with self.assertRaises(SiteWaiting):
            f.get_json(API.replace("1973521", "2162341"))
        self.assertIn("xác minh", wait.call_args.args[0])
        self.assertEqual(page.fetches, [])

    def test_wrong_product_without_captcha_reports_waiting_for_product(self):
        state = {"challenge": False, "ready": True, "pid": "2080891", "master_pid": "2080891"}
        f, page, _, wait, _ = self.fetcher(FakePage(states=[state] * 5), verification_timeout=0)
        f.LOAD_TIMEOUT = 0
        with self.assertRaises(SiteWaiting) as caught:
            f.get_json(API)
        self.assertIn("đúng trang sản phẩm", str(caught.exception))
        self.assertNotIn("xác minh", wait.call_args.args[0])
        self.assertEqual(page.fetches, [])

    def test_visible_challenge_waits_for_user_then_reads_same_session(self):
        challenge = {"challenge": True, "ready": False, "pid": ""}
        page = FakePage(states=[challenge, challenge, {"challenge": False, "ready": True, "pid": "1973521"}])
        f, page, factory, wait, clear = self.fetcher(page)
        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock):
            self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
        wait.assert_called_once()
        self.assertGreaterEqual(clear.call_count, 1)
        self.assertEqual(page.front, 1)
        self.assertEqual(len(page.fetches), 1)
        self.assertEqual(factory.call_count, 1)

    def test_challenge_timeout_keeps_request_pending_without_api_retries(self):
        challenge = {"challenge": True, "ready": False, "pid": ""}
        page = FakePage(states=[challenge] * 10)
        f, page, factory, wait, clear = self.fetcher(page, verification_timeout=1)
        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock):
            with self.assertRaises(SiteWaiting) as caught:
                f.get_json(API)
        self.assertEqual(caught.exception.retry_after, 0)
        self.assertEqual(page.fetches, [])
        self.assertEqual(factory.call_count, 1)
        wait.assert_called_once()

    def test_stop_during_user_verification_exits_and_clears_wait(self):
        challenge = {"challenge": True, "ready": False, "pid": ""}
        stopped = threading.Event()
        page = FakePage(states=[challenge] * 5)
        page.on_wait = stopped.set
        f, page, _, _, clear = self.fetcher(page, should_stop=stopped.is_set)
        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock):
            with self.assertRaises(SkipProduct) as caught:
                f.get_json(API)
        self.assertNotIsInstance(caught.exception, SiteWaiting)
        self.assertEqual(str(caught.exception), "Đã dừng")
        clear.assert_called()
        self.assertEqual(page.fetches, [])

    def test_ajax_challenge_opens_pdp_once_and_retries_in_same_session(self):
        page = FakePage([{"status": 403, "text": '<div id="px-captcha">Verify</div>'}, data()])
        f, page, factory, _, _ = self.fetcher(page)
        self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
        self.assertEqual(len(page.gotos), 2)
        self.assertEqual(len(page.fetches), 2)
        self.assertEqual(factory.call_count, 1)

    def test_ajax_still_blocked_stops_after_one_retry(self):
        blocked = {"status": 403, "text": '{"appId":"PXTEST"}'}
        f, page, factory, _, _ = self.fetcher(FakePage([blocked, blocked]))
        with self.assertRaises(SiteWaiting):
            f.get_json(API)
        self.assertEqual(len(page.gotos), 2)
        self.assertEqual(len(page.fetches), 2)
        factory.assert_called_once()

    def test_server_error_and_malformed_json_are_product_errors(self):
        for payload in ({"status": 503, "text": "unavailable"}, {"status": 200, "text": "not json"}):
            with self.subTest(payload=payload):
                f, page, _, _, _ = self.fetcher(FakePage([payload]))
                with self.assertRaises(SkipProduct) as caught:
                    f.get_json(API)
                self.assertNotIsInstance(caught.exception, SiteWaiting)
                self.assertEqual(len(page.fetches), 1)

    def test_navigation_failure_is_waiting_with_sanitized_error(self):
        page = FakePage(states=[{"challenge": False, "ready": False, "pid": ""}])
        page.goto_error = RuntimeError("http://user:secret@198.51.100.10:5000 failed")
        f, page, _, _, _ = self.fetcher(page)
        with self.assertRaises(SiteWaiting) as caught:
            f.get_json(API)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(page.fetches, [])

    def test_unsafe_host_is_rejected_before_browser_starts(self):
        f, page, factory, _, _ = self.fetcher()
        with self.assertRaises(SkipProduct):
            f.get_json("https://prana.com.example.test/product?pid=1973521")
        factory.assert_not_called()

    def test_browser_factory_required(self):
        f = PranaBrowserFetcher()
        f.prepare_product(PDP)
        with self.assertRaises(BrowserLaunchError):
            f.get_json(API)


class PranaVerificationWaitTests(unittest.TestCase):
    """Bound human verification separately from loading; retain optional unlimited waiting."""

    def fetcher(self, page, **kwargs):
        alert = Mock()
        f = PranaBrowserFetcher(page_factory=Mock(return_value=page), set_wait=Mock(), clear_wait=Mock(),
                                min_gap=0, alert=alert, **kwargs)
        f.prepare_product(PDP)
        return f, alert

    def test_unlimited_wait_continues_after_long_verification_and_repeats_alert(self):
        challenge = {"challenge": True, "ready": False, "pid": ""}
        page = FakePage(states=[challenge] * 1300)   # ~650 giây chờ (mỗi vòng 0,5 giây)
        f, alert = self.fetcher(page, verification_timeout=None)
        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock):
            self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
        self.assertGreaterEqual(alert.call_count, 5)   # nhắc lại mỗi 2 phút
        self.assertEqual(len(page.fetches), 1)

    def test_unlimited_wait_still_gives_up_when_page_never_loads_without_challenge(self):
        loading = {"challenge": False, "ready": False, "pid": ""}
        page = FakePage(states=[loading] * 1000)
        f, alert = self.fetcher(page, verification_timeout=None)
        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock):
            with self.assertRaises(SiteWaiting):
                f.get_json(API)
        alert.assert_not_called()

    def test_five_minute_wait_expires_once_even_with_proxies_configured(self):
        challenge = {"challenge": True, "ready": False, "pid": ""}
        page = FakePage(states=[challenge] * 1000)
        f, alert = self.fetcher(page, pool=ProxyPool([dict(PROXY)]))
        f._attempted_routes = {f._route_key(), PROXY["server"]}
        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock), \
             patch("sites.prana_browser.time.time", side_effect=lambda: 1000 + page.clock):
            with self.assertRaisesRegex(SiteWaiting, "hết 5 phút") as caught:
                f.get_json(API)
        self.assertEqual(f.verification_timeout, 300)
        self.assertEqual(page.clock, 400)
        self.assertEqual(len(page.gotos), 1, "Expiry must not restart three ordinary attempts")
        self.assertEqual(page.fetches, [])
        self.assertEqual(caught.exception.retry_after, 0)
        f.set_wait.assert_called_once()
        self.assertEqual(f.set_wait.call_args.args[1], 1400)
        self.assertEqual(alert.call_count, 3)

    def test_verification_completed_before_five_minutes_reads_same_session(self):
        challenge = {"challenge": True, "ready": False, "pid": ""}
        page = FakePage(states=[challenge] * 599)
        f, _ = self.fetcher(page)
        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock):
            self.assertEqual(f.get_json(API)["product"]["id"], "1973521")
        self.assertEqual(page.clock, 399.5)
        self.assertEqual(len(page.gotos), 1)
        self.assertEqual(len(page.fetches), 1)

    def test_human_timer_starts_when_challenge_appears_after_loading(self):
        loading = {"challenge": False, "ready": False, "pid": ""}
        challenge = {"challenge": True, "ready": False, "pid": ""}
        page = FakePage(states=[loading] * 240 + [challenge] * 1000)
        f, _ = self.fetcher(page)
        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock), \
             patch("sites.prana_browser.time.time", side_effect=lambda: 1000 + page.clock):
            with self.assertRaisesRegex(SiteWaiting, "hết 5 phút"):
                f.get_json(API)
        self.assertEqual(page.clock, 520)
        self.assertEqual([call.args[1] for call in f.set_wait.call_args_list], [0, 1520])

    def test_challenge_flicker_keeps_original_five_minute_deadline(self):
        challenge = {"challenge": True, "ready": False, "pid": ""}
        loading = {"challenge": False, "ready": False, "pid": ""}
        page = FakePage(states=[challenge] * 2 + [loading] * 400 + [challenge] * 1000)
        f, _ = self.fetcher(page)
        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock), \
             patch("sites.prana_browser.time.time", side_effect=lambda: 1000 + page.clock):
            with self.assertRaisesRegex(SiteWaiting, "hết 5 phút"):
                f.get_json(API)
        self.assertEqual(page.clock, 400)
        self.assertEqual([call.args[1] for call in f.set_wait.call_args_list], [1400, 1400, 1400])

    def test_finite_human_limit_keeps_loading_timeout_at_three_minutes(self):
        loading = {"challenge": False, "ready": False, "pid": ""}
        page = FakePage(states=[loading] * 1000)
        f, alert = self.fetcher(page)
        with patch("sites.prana_browser.time.monotonic", side_effect=lambda: page.clock):
            with self.assertRaises(SiteWaiting) as caught:
                f.get_json(API)
        self.assertEqual(page.clock, 280)
        self.assertEqual(caught.exception.retry_after, 60)
        alert.assert_not_called()

    def test_ip_stats_count_links_not_color_requests(self):
        page = FakePage([data(), data(color="Black"), data(color="Navy")])
        f, _ = self.fetcher(page)
        for _ in range(3):
            f.get_json(API)
        self.assertEqual(f.ip_stats["IP gốc"]["ok"], 1)


class PranaBrowserColorTests(unittest.TestCase):
    def test_browser_reads_every_color_once_for_the_cache(self):
        from sites import prana
        from test_prana_rules import NUMERIC_URL, RetailerFixture
        f = RetailerFixture()
        f.READ_ALL_COLORS = True
        prod = prana.scrape(f, NUMERIC_URL)
        self.assertEqual({v["color"] for v in prod["variants"]}, {"Spruce", "Black"})
        self.assertFalse(prod.get("partial_colors"))
        self.assertEqual(prod["url_color"], "Spruce")


if __name__ == "__main__":
    unittest.main()
