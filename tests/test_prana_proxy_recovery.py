"""Regression checks for the prAna-only proxy recovery policy.

All HTTP responses and failures are local mocks. These tests never contact a
retailer or the running application.
"""
import copy
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from curl_cffi.requests import exceptions

import app
import core
import tracking
from sites import base, prana, sfcc


URL = "https://www.prana.com/p/test/2145891.html"
PROXIES = [
    {"server": "http://198.51.100.10:50100"},
    {"server": "http://203.0.113.20:50100"},
]


def response(status=200, text="product data"):
    return SimpleNamespace(status_code=status, text=text)


def clothing():
    return {
        "id": "2145891", "available": True,
        "price": {"type": "range", "min": {"sales": {"value": 50, "currency": "USD"}}},
        "variationAttributes": [
            {"attributeId": "color", "displayName": "Color", "values": [
                {"id": "Black", "value": "Black", "displayValue": "Black", "selected": True},
                {"id": "Almond", "value": "Almond", "displayValue": "Almond"}]},
            {"attributeId": "size", "displayName": "Size", "values": [
                {"id": "S", "value": "S", "displayValue": "S", "selectable": True},
                {"id": "M", "value": "M", "displayValue": "M", "selectable": True}]},
        ],
        "variantData": {
            "2145891-Black-S": {"color": "Black", "size": "S", "inventory": 5},
            "2145891-Almond-S": {"color": "Almond", "size": "S", "inventory": 3},
        },
    }


class PranaProxyRecoveryTests(unittest.TestCase):
    def test_dead_connection_proxies_are_each_tried_once_then_abort(self):
        pool = base.ProxyPool(copy.deepcopy(PROXIES))
        fetcher = base.Fetcher(use_proxy=True, pool=pool, fail_fast=True)
        failure = exceptions.Timeout("Failed to connect to www.prana.com:443 over proxy after 8000 ms", code=28)
        with patch("curl_cffi.requests.get", side_effect=failure) as get, patch("sites.base.time.sleep"):
            with self.assertRaises(base.SiteUnavailable):
                fetcher.get(URL)
        self.assertEqual(get.call_count, 2)
        self.assertEqual({c.kwargs["proxies"]["https"] for c in get.call_args_list},
                         {p["server"] for p in PROXIES})
        self.assertEqual(pool.proxies, [])
        self.assertTrue(all(c.kwargs["timeout"] == (8, 25) for c in get.call_args_list))

    def test_dead_proxy_removal_skips_a_cooled_route_and_reaches_working_proxy(self):
        cooled = {"server": "http://192.0.2.30:50100"}
        pool = base.ProxyPool([copy.deepcopy(PROXIES[0]), cooled, copy.deepcopy(PROXIES[1])])
        pool.cooldown["192.0"] = time.time() + 1800
        routes = []

        def get(url, **kwargs):
            route = kwargs["proxies"]["https"]
            routes.append(route)
            if route == PROXIES[0]["server"]:
                raise exceptions.ConnectTimeout("Failed to connect", code=28)
            self.assertEqual(route, PROXIES[1]["server"])
            return response()

        fetcher = base.Fetcher(use_proxy=True, pool=pool, fail_fast=True)
        with patch("curl_cffi.requests.get", side_effect=get), patch("sites.base.time.sleep"):
            self.assertEqual(fetcher.get(URL), "product data")
        self.assertEqual(routes, [PROXIES[0]["server"], PROXIES[1]["server"]])
        self.assertNotIn(PROXIES[0], pool.proxies)

    def test_all_cooled_proxies_abort_without_any_http_request(self):
        pool = base.ProxyPool(copy.deepcopy(PROXIES))
        pool.cooldown.update({"198.51": time.time() + 1800, "203.0": time.time() + 1800})
        fetcher = base.Fetcher(use_proxy=True, pool=pool, fail_fast=True)
        with patch("curl_cffi.requests.get") as get:
            with self.assertRaises(base.SiteUnavailable):
                fetcher.get(URL)
        get.assert_not_called()

    def test_cancelled_run_is_reported_as_stopped_even_if_every_proxy_is_cooled(self):
        pool = base.ProxyPool(copy.deepcopy(PROXIES))
        pool.cooldown.update({"198.51": time.time() + 1800, "203.0": time.time() + 1800})
        fetcher = base.Fetcher(use_proxy=True, pool=pool, fail_fast=True, should_stop=lambda: True)
        with patch("curl_cffi.requests.get") as get:
            with self.assertRaises(base.SkipProduct) as caught:
                fetcher.get(URL)
        self.assertNotIsInstance(caught.exception, base.SiteUnavailable)
        self.assertIn("Đã dừng", str(caught.exception))
        get.assert_not_called()

    def test_cancellation_during_last_proxy_failure_is_reported_as_stopped(self):
        stopped = threading.Event()
        fetcher = base.Fetcher(use_proxy=True, pool=base.ProxyPool([copy.deepcopy(PROXIES[0])]),
                               fail_fast=True, should_stop=stopped.is_set)

        def fail_after_stop(*args, **kwargs):
            stopped.set()
            raise exceptions.Timeout("Failed to connect", code=28)

        with patch("curl_cffi.requests.get", side_effect=fail_after_stop):
            with self.assertRaises(base.SkipProduct) as caught:
                fetcher.get(URL)
        self.assertNotIsInstance(caught.exception, base.SiteUnavailable)
        self.assertIn("Đã dừng", str(caught.exception))

    def test_http403_cools_only_individual_addresses_and_does_not_cycle_back(self):
        pool = base.ProxyPool([
            copy.deepcopy(PROXIES[0]), {"server": "http://198.51.100.11:50100"},
            copy.deepcopy(PROXIES[1]),
        ])
        fetcher = base.Fetcher(use_proxy=True, pool=pool, fail_fast=True)
        with patch("curl_cffi.requests.get", return_value=response(403, "Access Denied")) as get, \
             patch("sites.base.time.sleep"):
            with self.assertRaises(base.SiteUnavailable):
                fetcher.get(URL)
        self.assertEqual(get.call_count, 3)
        self.assertEqual([c.kwargs["proxies"]["https"] for c in get.call_args_list],
                         [PROXIES[0]["server"], "http://198.51.100.11:50100", PROXIES[1]["server"]])
        self.assertFalse(pool.has_ready())
        self.assertEqual(pool.cooldown, {})

    def test_prana_reaches_working_proxy_in_same_subnet_as_blocked_proxy(self):
        first = {"server": "http://161.77.141.39:50100"}
        working = {"server": "http://161.77.142.40:50100"}
        pool = base.ProxyPool([first, working])
        fetcher = base.Fetcher(use_proxy=True, pool=pool, fail_fast=True)
        with patch("curl_cffi.requests.get", side_effect=[response(403), response()]) as get, \
             patch("sites.base.time.sleep"):
            self.assertEqual(fetcher.get(URL), "product data")
        self.assertEqual([c.kwargs["proxies"]["https"] for c in get.call_args_list],
                         [first["server"], working["server"]])
        self.assertEqual(pool.cooldown, {})
        self.assertIn(first["server"], pool.blocked_servers)
        self.assertNotIn(working["server"], pool.blocked_servers)

    def test_default_fetcher_still_skips_entire_blocked_subnet(self):
        first = {"server": "http://161.77.141.39:50100"}
        same_subnet = {"server": "http://161.77.142.40:50100"}
        other = {"server": "http://77.47.1.1:50100"}
        pool = base.ProxyPool([first, same_subnet, other])
        fetcher = base.Fetcher(use_proxy=True, pool=pool)
        with patch("curl_cffi.requests.get", side_effect=[response(403), response()]) as get, \
             patch("sites.base.time.sleep"):
            self.assertEqual(fetcher.get("https://www.example.test/product"), "product data")
        self.assertEqual([c.kwargs["proxies"]["https"] for c in get.call_args_list],
                         [first["server"], other["server"]])
        self.assertIn("161.77", pool.cooldown)
        self.assertEqual(pool.blocked_servers, {})

    def test_all_21_proxy_failures_are_tried_once_even_while_dead_routes_are_removed(self):
        proxies = [{"server": f"http://161.77.1.{number}:50100"} for number in range(1, 22)]
        pool = base.ProxyPool(copy.deepcopy(proxies))
        fetcher = base.Fetcher(use_proxy=True, pool=pool, fail_fast=True)
        routes = []

        def get(url, **kwargs):
            route = kwargs["proxies"]["https"]
            routes.append(route)
            if len(routes) % 2 == 0:
                raise exceptions.Timeout("Failed to connect", code=28)
            return response(403)

        with patch("curl_cffi.requests.get", side_effect=get), patch("sites.base.time.sleep"):
            with self.assertRaises(base.SiteUnavailable):
                fetcher.get(URL)
        self.assertEqual(routes, [p["server"] for p in proxies])
        self.assertEqual(len(set(routes)), 21)
        self.assertFalse(pool.has_ready())

    def test_prana_tries_past_eight_proxy_routes_before_reaching_working_route(self):
        proxies = [{"server": f"http://161.77.1.{number}:50100"} for number in range(1, 22)]
        fetcher = base.Fetcher(use_proxy=True, pool=base.ProxyPool(proxies), fail_fast=True)
        responses = [response(403) for _ in range(20)] + [response()]
        with patch("curl_cffi.requests.get", side_effect=responses) as get, patch("sites.base.time.sleep"):
            self.assertEqual(fetcher.get(URL), "product data")
        self.assertEqual(get.call_count, 21)
        self.assertEqual(get.call_args.kwargs["proxies"]["https"], proxies[-1]["server"])

    def test_prana_starts_with_an_individually_ready_proxy(self):
        pool = base.ProxyPool(copy.deepcopy(PROXIES))
        pool.blocked_servers[PROXIES[0]["server"]] = time.time() + 1800
        fetcher = base.Fetcher(use_proxy=True, pool=pool, fail_fast=True)
        with patch("curl_cffi.requests.get", return_value=response()) as get:
            self.assertEqual(fetcher.get(URL), "product data")
        self.assertEqual(get.call_args.kwargs["proxies"]["https"], PROXIES[1]["server"])

    def test_direct_block_switches_to_full_prana_proxy_budget(self):
        proxies = [{"server": f"http://161.77.1.{number}:50100"} for number in range(1, 22)]
        fetcher = base.Fetcher(pool=base.ProxyPool(proxies), fail_fast=True)
        responses = [response(403) for _ in range(21)] + [response()]
        with patch("curl_cffi.requests.get", side_effect=responses) as get, patch("sites.base.time.sleep"):
            self.assertEqual(fetcher.get(URL), "product data")
        self.assertIsNone(get.call_args_list[0].kwargs["proxies"])
        self.assertEqual(get.call_count, 22)
        self.assertEqual(get.call_args.kwargs["proxies"]["https"], proxies[-1]["server"])

    def test_default_fetcher_still_limits_large_proxy_pool_to_eight_attempts(self):
        proxies = [{"server": f"http://161.77.1.{number}:50100"} for number in range(1, 22)]
        fetcher = base.Fetcher(use_proxy=True, pool=base.ProxyPool(proxies))
        with patch("curl_cffi.requests.get", return_value=response(403)) as get, patch("sites.base.time.sleep"):
            with self.assertRaises(base.SkipProduct) as caught:
                fetcher.get("https://www.example.test/product")
        self.assertNotIsInstance(caught.exception, base.SiteUnavailable)
        self.assertEqual(get.call_count, 8)

    def test_default_fetcher_retains_its_existing_retry_policy(self):
        pool = base.ProxyPool(copy.deepcopy(PROXIES))
        fetcher = base.Fetcher(use_proxy=True, pool=pool)
        failure = exceptions.Timeout("Failed to connect", code=28)
        with patch("curl_cffi.requests.get", side_effect=failure) as get, patch("sites.base.time.sleep"):
            with self.assertRaises(base.SkipProduct) as caught:
                fetcher.get("https://www.example.test/product")
        self.assertNotIsInstance(caught.exception, base.SiteUnavailable)
        self.assertEqual(get.call_count, 4)
        self.assertEqual(pool.proxies, PROXIES)
        self.assertTrue(all(c.kwargs["timeout"] == 60 for c in get.call_args_list))

    def test_product_server_error_is_not_reported_as_proxy_exhaustion(self):
        fetcher = base.Fetcher(use_proxy=True, pool=base.ProxyPool(copy.deepcopy(PROXIES)),
                               fail_fast=True, tries=1)
        with patch("curl_cffi.requests.get", return_value=response(500, "Server error")), \
             patch("sites.base.time.sleep"):
            with self.assertRaises(base.SkipProduct) as caught:
                fetcher.get(URL)
        self.assertNotIsInstance(caught.exception, base.SiteUnavailable)
        self.assertIn("500", str(caught.exception))

    def test_normal_run_enables_recovery_only_for_prana(self):
        engine = app.Engine.__new__(app.Engine)
        for site in ("prana", "hollister", "eileenfisher", "bestbuy"):
            session = SimpleNamespace(kind=site, pool=base.ProxyPool([]), browser=None, stop=threading.Event())
            with self.subTest(site=site), patch.object(app, "Fetcher") as http_factory, \
                    patch.object(app, "PranaBrowserFetcher") as browser_factory:
                actual = engine._fetcher(session, {"sites": {site: {"use_proxy": False}}})
                factory = browser_factory if site == "prana" else http_factory
                unused = http_factory if site == "prana" else browser_factory
                self.assertIs(actual, factory.return_value)
                unused.assert_not_called()
                self.assertEqual(bool(factory.call_args.kwargs.get("fail_fast")), site == "prana")

    def test_site_unavailable_escapes_color_loop_in_shared_sfra(self):
        first = clothing()
        failure = base.SiteUnavailable("No usable proxy")
        fetcher = SimpleNamespace(log=Mock())
        with patch.object(sfcc, "get_variation", side_effect=failure) as get:
            with self.assertRaises(base.SiteUnavailable):
                sfcc.sfra_variants(fetcher, prana.HOST, prana.SITE, "2145891", first)
        self.assertEqual(get.call_count, 1)

    def test_site_unavailable_escapes_size_loop_in_shared_sfra(self):
        first = clothing()
        failure = base.SiteUnavailable("No usable proxy")
        fetcher = SimpleNamespace(log=Mock())
        with patch.object(sfcc, "get_variation", side_effect=[first, failure]) as get:
            with self.assertRaises(base.SiteUnavailable):
                sfcc.sfra_variants(fetcher, prana.HOST, prana.SITE, "2145891", first)
        self.assertEqual(get.call_count, 2)

    def test_site_unavailable_escapes_prana_fast_color_loop(self):
        first = clothing()
        first["price"] = {"sales": {"value": 100, "currency": "USD"}}
        failure = base.SiteUnavailable("No usable proxy")
        with patch.object(prana, "get_variation", side_effect=failure) as get:
            with self.assertRaises(base.SiteUnavailable):
                prana._variants(SimpleNamespace(log=Mock()), "2145891", first)
        self.assertEqual(get.call_count, 1)

    def test_tracking_gives_prana_its_own_proxy_pool_and_recovery_flag(self):
        engine = app.Engine.__new__(app.Engine)
        engine.lock = threading.Lock()
        engine.track = {"progress": [0, 2], "logs": [], "stop": threading.Event(), "current": ""}
        mods = {site: SimpleNamespace(KEY=site, NAME=site, scrape=Mock(side_effect=base.SkipProduct("offline test")))
                for site in ("prana", "hollister")}
        engine.sessions = {site: SimpleNamespace(mod=mod) for site, mod in mods.items()}
        data = {"items": {site: {"sku": site, "site": site, "url": f"https://www.{site}.com/product"}
                          for site in mods}}
        cfg = {"sites": {site: {"use_proxy": False, "delay": "0"} for site in mods}}
        with patch.object(core, "load_config", return_value=cfg), \
             patch.object(core, "proxies_for", return_value=copy.deepcopy(PROXIES)), \
             patch.object(core, "site_proxy_lines", return_value=[]), \
             patch.object(core, "site_delay", return_value=0), \
             patch.object(app, "Fetcher", return_value=object()) as http_factory, \
             patch.object(app, "PranaBrowserFetcher") as browser_factory, \
             patch.object(tracking, "load", return_value=data), \
             patch.object(tracking, "update", side_effect=lambda fn: fn(data)), \
             patch.object(tracking, "run_left", return_value=[]), \
             patch.object(tracking, "run_clear"):
            engine._track_run(list(data["items"]), Mock())
        browser_factory.assert_called_once()
        http_factory.assert_called_once()
        self.assertTrue(browser_factory.call_args.kwargs.get("fail_fast"))
        self.assertFalse(http_factory.call_args.kwargs.get("fail_fast", False))
        self.assertIsNot(browser_factory.call_args.kwargs["pool"], http_factory.call_args.kwargs["pool"])
        browser_factory.return_value.prepare_product.assert_called_once_with("https://www.prana.com/product")

    def test_run_stops_on_network_exhaustion_and_keeps_all_remaining_rows_pending(self):
        engine = app.Engine.__new__(app.Engine)
        engine.lock = threading.Lock()
        session = app.Session(prana)
        session.progress = [2, 165]
        for number in (1, 2):
            session.results.append({
                "id": f"r{number}", "status": "ok", "sku": f"US1-PRA-{number}",
                "title": f"Completed item {number}", "title_src": "ai", "profile": "US1",
                "url": f"https://www.prana.com/p/old/{number}.html", "country": "US",
                "product": {"site": "prana", "code": str(number), "variants": [
                    {"color": "Black", "size": "S", "price": 100, "available": True}]},
            })
        completed = copy.deepcopy(session.results)
        remaining = [{"url": f"https://www.prana.com/p/item/{n}.html", "profile": "US1",
                      "country": "US", "append": True} for n in range(3, 166)]
        cfg = {"sites": {"prana": {"use_proxy": False}}, "price_percent": 80, "ai_titles": False}
        failure = base.SiteUnavailable("prAna: no usable network route")
        with patch.object(core, "load_config", return_value=cfg), \
             patch.object(core, "load_json", return_value={}), \
             patch.object(core, "proxies_for", return_value=[]), \
             patch.object(core, "site_proxy_lines", return_value=[]), \
             patch.object(engine, "_read_product", side_effect=failure) as read, \
             patch.object(engine, "_refresh_category"), \
             patch.object(app, "price_info", return_value={}), \
             patch.object(core, "new_run_id", return_value="test-only"), \
             patch.object(core, "export_run", return_value=([], [])):
            engine._run(session, remaining, upload=False)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(session.results[:2], completed)
        self.assertEqual(len(session.results), 165)
        self.assertEqual(len({r["id"] for r in session.results}), 165)
        self.assertFalse(app.is_pending(session.results[2]))
        self.assertTrue(all(app.is_pending(r) for r in session.results[3:]))
        self.assertEqual(session.progress, [165, 165])


if __name__ == "__main__":
    unittest.main()
