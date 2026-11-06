"""prAna: lựa chọn phụ (kiểu dáng, chiều dài ống) không gộp vào ô Size của eBay (offline)."""
import copy
import unittest

import core
from sites import prana
from test_prana_rules import RetailerFixture, listing, value

SHORT_URL = "https://www.prana.com/p/test-short/9993201.html?dwvar_9993201_color=Spruce"
TOP_URL = "https://www.prana.com/p/test-hoodie/9993201.html?dwvar_9993201_dimension=ST&dwvar_9993201_color=Spruce"


def fixture(name, category, sizes, dimensions):
    f = RetailerFixture()
    f.base.update(productName=name, primaryCategoryID=category)
    f.base["attributes"] = []
    for a in f.base["variationAttributes"]:
        if a["attributeId"] == "size":
            a["values"] = [value(s) for s in sizes]
        elif a["attributeId"] == "dimension":
            a["values"] = [value(vid, label) for vid, label in dimensions]
    return f


ASPECTS = [{"name": "Size", "variation": True}, {"name": "Color", "variation": True},
           {"name": "Inseam", "variation": True}, {"name": "Fit"}]


def csv_rows(prod, url):
    item = copy.deepcopy(listing(prod, url))
    item.update(aspects=ASPECTS, aspects_cat=item["category"])
    return core.listing_rows(item, {"sites": {}, "price_percent": 100})


class PranaExtraOptionTests(unittest.TestCase):
    def test_single_fit_goes_to_fit_not_size(self):
        f = fixture("Fleece Hoodie", "men-tops-hoodies", ["S", "M", "L"], [("ST", "Standard")])
        prod = prana.scrape(f, TOP_URL)
        self.assertEqual({v["size"] for v in prod["variants"]}, {"S", "M", "L"})
        self.assertEqual(prod["details"]["Fit"], "Standard Fit")
        self.assertNotIn("Inseam", prod["details"])
        rows = csv_rows(prod, TOP_URL)
        self.assertEqual(rows[0]["C:Fit"], "Standard Fit")
        self.assertEqual(rows[0]["RelationshipDetails"], "Size=S;M;L")

    def test_existing_site_fit_is_kept(self):
        f = fixture("Crew Sweatshirt", "men-tops-sweatshirts", ["S", "M"], [("RX", "Relaxed")])
        f.base["attributes"] = [{"ID": "productCopy", "attributes": [{"ID": "apparelFit", "value": ["Relaxed Fit"]}]}]
        prod = prana.scrape(f, TOP_URL)
        self.assertEqual(prod["details"]["Fit"], "Relaxed Fit")
        self.assertEqual({v["size"] for v in prod["variants"]}, {"S", "M"})

    def test_single_length_goes_to_inseam(self):
        f = fixture("Light Jogger", "men-bottoms-joggers", ["S", "M", "L"], [("29", "29")])
        prod = prana.scrape(f, SHORT_URL)
        self.assertEqual({v["size"] for v in prod["variants"]}, {"S", "M", "L"})
        self.assertEqual(prod["details"]["Inseam"], "29 in")
        self.assertEqual(csv_rows(prod, SHORT_URL)[0]["C:Inseam"], "29 in")

    def test_8_wins_over_10_even_if_link_says_10(self):
        url = SHORT_URL + "&dwvar_9993201_dimension=10"
        f = fixture("Stretch Zion Short", "men-bottoms-shorts", ["30", "32", "34"], [("08", "8"), ("10", "10")])
        prod = prana.scrape(f, url)
        self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {"08"})
        self.assertEqual({v["size"] for v in prod["variants"]}, {"30", "32", "34"})
        self.assertTrue(all(v["_site_size"] == v["size"] + " 8" for v in prod["variants"]))
        self.assertEqual(prod["details"]["Inseam"], "8 in")
        rows = csv_rows(prod, url)
        self.assertEqual(rows[0]["C:Inseam"], "8 in")
        self.assertEqual(rows[0]["RelationshipDetails"], "Size=30;32;34")

    def test_3_wins_over_5_even_if_link_says_5(self):
        dims = [("03", "3"), ("05", "5")]
        for url, want in ((SHORT_URL + "&dwvar_9993201_dimension=05", "03"), (SHORT_URL, "03")):
            with self.subTest(url=url):
                f = fixture("Halle Short", "women-bottoms-shorts", ["0", "2", "4"], dims)
                prod = prana.scrape(f, url)
                self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {want})
                self.assertEqual({v["size"] for v in prod["variants"]}, {"0", "2", "4"})
                self.assertEqual(prod["details"]["Inseam"], "3 in")

    def test_other_lengths_follow_link_then_first(self):
        dims = [("07", "7"), ("09", "9")]
        for url, want in ((SHORT_URL + "&dwvar_9993201_dimension=09", "09"), (SHORT_URL, "07")):
            with self.subTest(url=url):
                f = fixture("Halle Short", "women-bottoms-shorts", ["0", "2", "4"], dims)
                prod = prana.scrape(f, url)
                self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {want})
                self.assertEqual({v["size"] for v in prod["variants"]}, {"0", "2", "4"})
                self.assertEqual(prod["details"]["Inseam"], f"{int(want)} in")

    def test_tracking_still_reads_every_option(self):
        f = fixture("Stretch Zion Short", "men-bottoms-shorts", ["30", "32"], [("08", "8"), ("10", "10")])
        prod = prana.scrape_for_tracking(f, SHORT_URL)
        self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {"08", "10"})
        self.assertIn("30 10", {v["size"] for v in prod["variants"]})


def stored(sizes, suffixes, details=None):
    vs = [{"color": "Mud", "size": f"{s} {x}", "price": 84.0, "list_price": None, "available": True, "id": f"{s}-{x}"}
          for x in suffixes for s in sizes]
    return {"site": "prana", "code": "2081231", "title": "Short", "brand": "prAna", "bullets": ['Inseam: 8"'],
            "details": dict(details or {}), "variants": vs, "colors": [{"name": "Mud", "images": []}]}


class PranaStoredRowTests(unittest.TestCase):
    def test_old_row_8_and_10_keeps_8(self):
        prod = stored(["30", "32"], ["8", "10"])
        mapping = prana.fix_stored(prod, "https://www.prana.com/p/x/2081231.html?dwvar_2081231_dimension=10")
        self.assertEqual(mapping, {"30 8": "30", "32 8": "32"})
        self.assertEqual([(v["size"], v["_site_size"]) for v in prod["variants"]], [("30", "30 8"), ("32", "32 8")])
        self.assertEqual(prod["fixed_inseam"], "8 in")
        self.assertEqual(prod["details"]["Inseam"], "8 in")
        self.assertEqual(prod["bullets"], [])
        self.assertIsNone(prana.fix_stored(prod, ""))   # chạy lại không đổi thêm

    def test_old_row_3_and_5_keeps_3(self):
        prod = stored(["0", "2"], ["3", "5"])
        mapping = prana.fix_stored(prod, "https://www.prana.com/p/x/2080141.html?dwvar_2080141_dimension=05")
        self.assertEqual(mapping, {"0 3": "0", "2 3": "2"})
        self.assertEqual(prod["details"]["Inseam"], "3 in")

    def test_old_row_single_fit_and_untouched_rows(self):
        prod = stored(["S", "M"], ["Standard"])
        self.assertEqual(prana.fix_stored(prod, ""), {"S Standard": "S", "M Standard": "M"})
        self.assertEqual(prod["details"]["Fit"], "Standard Fit")
        for sizes, suffixes in ((["S", "M"], ["Standard", "Tall"]), (["S", "M"], [])):
            p = stored(sizes, suffixes)
            if not suffixes:
                p["variants"] = [{"color": "Mud", "size": s, "price": 1, "available": True} for s in sizes]
            self.assertIsNone(prana.fix_stored(p, ""))

    def test_engine_fixes_loaded_rows_and_selection(self):
        from types import SimpleNamespace
        import app
        eng = app.Engine.__new__(app.Engine)
        eng.save_session = lambda s: None
        row = {"url": "https://www.prana.com/p/x/2081231.html", "product": stored(["30", "32"], ["8", "10"]),
               "variant_sel": ["Mud||30 8", "Mud||30 10", "Mud||32 8"]}
        s = SimpleNamespace(kind="prana", mod=prana, results=[row], logs=[], dirty=False)
        eng._fix_prana_rows(s)
        self.assertEqual(row["variant_sel"], ["Mud||30", "Mud||32"])
        self.assertEqual(row["n_variants"], 2)
        self.assertTrue(s.dirty)


if __name__ == "__main__":
    unittest.main()
