"""prAna product data is reused within a run without reusing a row's color choice."""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
import core
from sites import prana
from sites.base import SiteUnavailable


BLUE_URL = "https://www.prana.com/p/shea-soft-full-zip/2080891.html?dwvar_2080891_color=Bluff%20Blue"
BLACK_URL = "https://www.prana.com/p/shea-soft-full-zip/2080891.html?dwvar_2080891_color=Black"
ALL_URL = "https://www.prana.com/p/shea-soft-full-zip/2080891.html"


def product(code="2080891", colors=("Bluff Blue", "Black")):
    return {
        "site": "prana", "code": code, "title": "Shea Soft Full Zip", "brand": "prAna",
        "url": ALL_URL, "url_color": colors[0], "department": "Women", "size_name": "Size",
        "colors": [{"name": color, "images": [f"https://example.test/{index}.jpg"]}
                   for index, color in enumerate(colors)],
        "images": [], "details": {}, "bullets": [], "breadcrumbs": ["Women", "Tops"],
        "variants": [{"color": color, "size": "S", "price": 108 if color == "Bluff Blue" else 54, "list_price": None,
                      "available": True, "id": f"{code}-{index}-S"}
                     for index, color in enumerate(colors)],
    }


class PranaProductCacheTests(unittest.TestCase):
    def setUp(self):
        self.engine = app.Engine.__new__(app.Engine)
        self.engine.log = Mock()
        self.mod = SimpleNamespace(KEY="prana", NAME="prAna", product_code=prana.product_code,
                                   scrape=Mock(side_effect=lambda fetcher, url: product()))
        self.session = SimpleNamespace(kind="prana", mod=self.mod)
        self.fetcher = SimpleNamespace()

    def read(self, url, cache):
        return self.engine._read_product(self.session, self.fetcher, url, cache)

    def test_multiple_colors_and_all_color_link_share_one_scrape_but_select_current_url(self):
        cache = {}
        blue = self.read(BLUE_URL, cache)
        black = self.read(BLACK_URL, cache)
        all_colors = self.read(ALL_URL, cache)
        self.assertEqual(self.mod.scrape.call_count, 1)
        self.assertEqual(core.url_filters(BLUE_URL, blue), {"colors": ["Bluff Blue"]})
        self.assertEqual(core.url_filters(BLACK_URL, black), {"colors": ["Black"]})
        self.assertIsNone(core.url_filters(ALL_URL, all_colors))
        # A profile/row may change its copy without mutating subsequent rows.
        black["variants"][0]["price"] = 1
        cached = cache[app.Engine._prana_cache_key(self.mod, BLUE_URL, blue)]
        self.assertEqual(cached["variants"][0]["price"], 108)
        self.assertEqual(all_colors["variants"][0]["price"], 108)
        self.assertEqual(cached["url_color"], "Bluff Blue")

    def test_partial_colors_coexist_and_repeat_profiles_do_not_refetch(self):
        def scrape(fetcher, url):
            color = "Black" if "=Black" in url else "Bluff Blue"
            prod = product(colors=(color,))
            prod.update(partial_colors=True, fixed_inseam="32 in")
            return prod

        self.mod.scrape.side_effect = scrape
        cache = {}
        for _profile in ("US1", "US3", "US4"):
            blue = self.read(BLUE_URL, cache)
            black = self.read(BLACK_URL, cache)
            self.assertEqual({v["color"] for v in blue["variants"]}, {"Bluff Blue"})
            self.assertEqual({v["color"] for v in black["variants"]}, {"Black"})
        self.assertEqual(self.mod.scrape.call_count, 2)
        self.assertEqual(set(cache), {("prana", "2080891", "bluff blue", "32 in"),
                                      ("prana", "2080891", "black", "32 in")})
        self.assertEqual(cache[("prana", "2080891", "black", "32 in")]["variants"][0]["price"], 54)

    def test_cache_uses_fixed_inseam_instead_of_overridden_url_dimension(self):
        prod = product(colors=("Bluff Blue",))
        prod.update(partial_colors=True, fixed_inseam="32 in")
        self.mod.scrape.side_effect = lambda fetcher, url: copy.deepcopy(prod)
        cache = {}
        self.read(BLUE_URL + "&dwvar_2080891_dimension=30", cache)
        self.read(BLUE_URL + "&dwvar_2080891_dimension=32", cache)
        self.assertEqual(self.mod.scrape.call_count, 1)
        self.assertEqual(set(cache), {("prana", "2080891", "bluff blue", "32 in")})

    def test_browser_is_prepared_only_when_a_product_must_be_read(self):
        self.fetcher.prepare_product = Mock()
        cache = {}
        self.read(BLUE_URL, cache)
        self.read(BLACK_URL, cache)
        self.fetcher.prepare_product.assert_called_once_with(BLUE_URL)

    def test_requested_color_missing_from_cached_variants_is_read_fresh(self):
        for parameter in ("dwvar_2080891_color", "color", "colour"):
            with self.subTest(parameter=parameter):
                self.mod.scrape.reset_mock()
                cache = {"2080891": product(colors=("Bluff Blue",))}
                url = ALL_URL + "?" + parameter + "=Black"
                black = self.read(url, cache)
                self.mod.scrape.assert_called_once_with(self.fetcher, url)
                self.assertIn("Black", {v["color"] for v in black["variants"]})

    def test_different_product_and_new_run_are_read_fresh(self):
        cache = {}
        self.read(BLUE_URL, cache)
        other = "https://www.prana.com/p/other/2145891.html?dwvar_2145891_color=Black"
        self.mod.scrape.side_effect = lambda fetcher, url: product(prana.product_code(url))
        self.read(other, cache)
        self.read(BLUE_URL, {})
        self.assertEqual(self.mod.scrape.call_count, 3)
        self.assertEqual({key[1] for key in cache}, {"2080891", "2145891"})

    def test_cached_colors_keep_their_own_source_prices_when_percent_changes(self):
        cache = {}
        self.read(BLUE_URL, cache)
        black = self.read(BLACK_URL, cache)
        chosen = {"variant_sel": ["Black||S"]}
        cfg = {"sites": {"prana": {"price_percent": "70"}}, "price_percent": 100, "round_99": False}
        self.assertEqual(app.price_info(black, cfg, chosen)["price_text"], "$54.00 → $37.80")
        cfg["sites"]["prana"]["price_percent"] = "80"
        self.assertEqual(app.price_info(black, cfg, chosen)["price_text"], "$54.00 → $43.20")
        cached = next(iter(cache.values()))
        self.assertEqual([v["price"] for v in cached["variants"]], [108, 54])

    def test_other_site_does_not_gain_prana_cache_and_bestbuy_still_requires_live_data(self):
        for site, live, expected_calls in (("hollister", True, 2), ("bestbuy", False, 2), ("bestbuy", True, 1)):
            mod = SimpleNamespace(product_code=lambda url: "2080891", scrape=Mock(side_effect=lambda f, u: product()))
            session = SimpleNamespace(kind=site, mod=mod)
            fetcher = SimpleNamespace(bestbuy_data_live=live)
            cache = {}
            for _ in range(2):
                self.engine._read_product(session, fetcher, BLUE_URL, cache)
            with self.subTest(site=site, live=live):
                self.assertEqual(mod.scrape.call_count, expected_calls)

    def test_retry_of_same_batch_seeds_cache_from_completed_rows(self):
        engine = app.Engine.__new__(app.Engine)
        engine.lock = threading.Lock()
        session = app.Session(self.mod)
        session.progress = [1, 2]
        completed = {"id": "r1", "status": "ok", "url": BLUE_URL, "profile": "US1", "country": "US",
                     "sku": "US1-PRA-2080891", "title": "Original title", "title_src": "ai", "product": product()}
        session.results = [copy.deepcopy(completed), {"id": "r2", "status": "error", "url": BLACK_URL,
                                                    "profile": "US3", "country": "US"}]
        items = [{"retry_id": "r2", "url": BLACK_URL, "profile": "US3", "country": "US"}]
        cfg = {"sites": {"prana": {"use_proxy": False}}, "price_percent": 70, "ai_titles": False}

        seen = []

        def inspect_cache(s, fetcher, url, cache):
            key = app.Engine._prana_cache_key(self.mod, BLUE_URL, completed["product"])
            seen.append((copy.deepcopy(cache), cache.get(key) is session.results[0]["product"]))
            raise SiteUnavailable("Offline test ended")

        with patch.object(core, "load_config", return_value=cfg), \
             patch.object(core, "load_json", return_value={}), \
             patch.object(core, "proxies_for", return_value=[]), \
             patch.object(core, "site_proxy_lines", return_value=[]), \
             patch.object(engine, "_read_product", side_effect=inspect_cache) as read, \
             patch.object(engine, "_refresh_category"), \
             patch.object(app, "price_info", return_value={}), \
             patch.object(core, "new_run_id", return_value="test-only"), \
             patch.object(core, "export_run", return_value=([], [])):
            engine._run(session, items, upload=False)
        self.assertEqual(read.call_count, 1)
        key = app.Engine._prana_cache_key(self.mod, BLUE_URL, completed["product"])
        self.assertEqual(seen, [({key: completed["product"]}, False)])
        self.assertEqual(session.results[0], completed)


if __name__ == "__main__":
    unittest.main()
