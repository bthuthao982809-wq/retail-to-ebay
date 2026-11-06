"""prAna: ít request mỗi link, giãn cách request, gặp PerimeterX thì dừng (không gọi mạng)."""
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app
from sites import base, prana
from test_prana_rules import NUMERIC_URL, RetailerFixture

URL = "https://www.prana.com/p/test/2145891.html"
PX_BODY = '{"appId":"PXLkXIE7Oj","jsClientSrc":"/LkXIE7Oj/init.js","blockScript":"/LkXIE7Oj/captcha/captcha.js"}'
PROXIES = [{"server": f"http://198.51.100.{n}:50100"} for n in range(1, 21)]


def response(status=200, text="{}"):
    return SimpleNamespace(status_code=status, text=text)


class PranaRequestBudgetTests(unittest.TestCase):
    def test_link_color_reads_only_that_color(self):
        f = RetailerFixture()
        prod = prana.scrape(f, NUMERIC_URL)
        self.assertEqual({v["color"] for v in prod["variants"]}, {"Spruce"})
        self.assertNotIn("Black", {r.get("color") for r in f.requests})
        self.assertTrue(prod.get("partial_colors"))

    def test_link_without_color_and_tracking_read_every_color(self):
        for scrape, url in ((prana.scrape, NUMERIC_URL.split("?")[0]),
                            (prana.scrape_for_tracking, NUMERIC_URL)):
            with self.subTest(scrape=scrape.__name__):
                f = RetailerFixture()
                prod = scrape(f, url)
                self.assertEqual({v["color"] for v in prod["variants"]}, {"Spruce", "Black"})
                self.assertFalse(prod.get("partial_colors"))

    def test_unknown_link_color_falls_back_to_every_color(self):
        f = RetailerFixture()
        prod = prana.scrape(f, NUMERIC_URL.replace("Spruce", "Discontinued"))
        self.assertEqual({v["color"] for v in prod["variants"]}, {"Spruce", "Black"})

    def test_partial_cache_is_not_reused_for_link_without_color(self):
        eng = app.Engine.__new__(app.Engine)
        eng.log = lambda *a, **k: None
        s = SimpleNamespace(kind="prana", mod=prana)
        cached = prana.scrape(RetailerFixture(), NUMERIC_URL)
        cache = {"9993201": copy.deepcopy(cached)}
        f = RetailerFixture()
        prod = eng._read_product(s, f, NUMERIC_URL.split("?")[0], cache)
        self.assertTrue(f.requests)   # đọc lại để có mọi màu
        self.assertEqual({v["color"] for v in prod["variants"]}, {"Spruce", "Black"})
        f2 = RetailerFixture()
        eng._read_product(s, f2, NUMERIC_URL, {"9993201": copy.deepcopy(cached)})
        self.assertEqual(f2.requests, [])   # cùng màu: dùng lại


class PerimeterXStopTests(unittest.TestCase):
    def setUp(self):
        base.BOT_COOLDOWN.clear()

    tearDown = setUp

    def test_px_block_stops_after_two_routes_and_cools_down(self):
        f = base.Fetcher(pool=base.ProxyPool(copy.deepcopy(PROXIES)), fail_fast=True)
        with patch("curl_cffi.requests.get", return_value=response(403, PX_BODY)) as get, \
                patch("sites.base.time.sleep"):
            with self.assertRaises(base.SiteUnavailable) as ctx:
                f.get(URL)
            self.assertEqual(get.call_count, 2)   # IP gốc + 1 proxy, không đốt cả 20 proxy
            self.assertIn("PerimeterX", str(ctx.exception))
            # "Chạy lại" ngay: không gửi request nào nữa trong lúc nghỉ
            f2 = base.Fetcher(pool=base.ProxyPool(copy.deepcopy(PROXIES)), fail_fast=True)
            with self.assertRaises(base.SiteUnavailable):
                f2.get(URL)
            self.assertEqual(get.call_count, 2)
        base.BOT_COOLDOWN["www.prana.com"] = time.time() - 1
        with patch("curl_cffi.requests.get", return_value=response(200, "{}")) as get:
            self.assertEqual(f2.get(URL), "{}")

    def test_plain_403_still_rotates_proxies(self):
        f = base.Fetcher(use_proxy=True, pool=base.ProxyPool(copy.deepcopy(PROXIES[:3])), fail_fast=True)
        replies = [response(403, "Forbidden"), response(403, "Forbidden"), response(200, "ok")]
        with patch("curl_cffi.requests.get", side_effect=replies), patch("sites.base.time.sleep"):
            self.assertEqual(f.get(URL), "ok")
        self.assertEqual(base.BOT_COOLDOWN, {})

    def test_other_sites_ignore_cooldown(self):
        base.BOT_COOLDOWN["www.prana.com"] = time.time() + 600
        f = base.Fetcher()
        with patch("curl_cffi.requests.get", return_value=response(200, "ok")):
            self.assertEqual(f.get(URL), "ok")

    def test_min_gap_spaces_requests(self):
        f = base.Fetcher(min_gap=2.0)
        clock = [100.0]
        waits = []

        def sleep(sec):
            waits.append(sec)
            clock[0] += sec
        with patch("curl_cffi.requests.get", return_value=response(200, "ok")), \
                patch("sites.base.time.monotonic", side_effect=lambda: clock[0]), \
                patch("sites.base.time.sleep", side_effect=sleep):
            f.get(URL)
            f.get(URL)
        self.assertAlmostEqual(sum(waits), 2.0)


if __name__ == "__main__":
    unittest.main()
