"""Offline regression coverage for prAna listing length and size rules.

Fixtures use a synthetic numeric-size pant plus the relevant shapes of the
2147861 and 1973521 API responses. They do not contact the retailer.
"""
import copy
import unittest
from urllib.parse import parse_qs, urlparse

import app
import core
from sites import prana, sfcc
from sites.base import SkipProduct


NUMERIC_URL = "https://www.prana.com/p/test-numeric-pant/9993201.html?dwvar_9993201_color=Spruce"
MENS_URL = "https://www.prana.com/p/stretch-zion-5-pocket-standard-pant/2147861.html?dwvar_2147861_dimension=32&dwvar_2147861_color=Mud"
KOEN_URL = "https://www.prana.com/p/koen-mid-rise-pant/1973521.html?dwvar_1973521_color=Grey%20Blue"


def value(vid, label=None):
    return {"id": vid, "value": vid, "displayValue": label or vid, "selectable": True, "selected": False}


def product_fixture(koen=False):
    code = "1973521" if koen else "9993201"
    sizes = ["XS", "S", "M", "L", "XL", "XXL"] if koen else [str(n) for n in range(0, 19, 2)]
    dimensions = [("RG", "Reg"), ("SH", "Short"), ("TL", "Tall")] if koen else [(s, s) for s in ("30", "32", "34")]
    inseams = 'Short 28", Reg 30", Tall 32"' if koen else 'Short 30", Reg 32", Tall 34"'
    features = 'Zipper pocket|Inseam: SH-28" RG-30" TL-32"' if koen else 'Cargo pocket|Inseam: 30", 32", 34"'
    return {
        "id": code, "online": True, "available": True,
        "productName": "Koen Mid Rise Pant" if koen else "Test Numeric Pant",
        "primaryCategoryID": "women-bottoms-pants-and-leggings",
        "selectedProductUrl": f"/p/pants/{code}.html",
        "productPitch": "Comfortable stretch pants.",
        "images": {"large": [{"url": f"https://media.prana.com/i/prana/{code}_front"}]},
        "attributes": [
            {"ID": "productCopy", "attributes": [
                {"ID": "features", "value": [features]},
                {"ID": "fabric", "value": ["95% Nylon, 5% Elastane"]}]},
            {"ID": "productFeatures", "attributes": [{"ID": "inseamLength", "value": [inseams]}]},
        ],
        "variationAttributes": [
            {"attributeId": "color", "id": "color", "displayName": "Color",
             "values": [value("Grey Blue" if koen else "Spruce"), value("Black")]},
            {"attributeId": "size", "id": "size", "displayName": "Size", "values": [value(s) for s in sizes]},
            {"attributeId": "dimension", "id": "dimension", "displayName": "Dimension",
             "values": [value(vid, label) for vid, label in dimensions]},
        ],
        "variantData": {},
    }


class RetailerFixture:
    """Generate color/dimension/size responses, including an intentionally lower range price."""
    def __init__(self, koen=False):
        self.koen = koen
        self.base = product_fixture(koen)
        self.requests = []
        self.logs = []
        self.sold_out = set()
        if not koen:
            self.sold_out.add(("Spruce", "32", "16"))

    def log(self, *args, **kwargs):
        self.logs.append(args)

    def get_json(self, url, **kwargs):
        params = parse_qs(urlparse(url).query)
        code = self.base["id"]
        sel = {k.removeprefix(f"dwvar_{code}_"): v[0] for k, v in params.items() if k.startswith(f"dwvar_{code}_")}
        self.requests.append(sel)
        p = copy.deepcopy(self.base)
        color = sel.get("color", "Grey Blue" if self.koen else "Spruce")
        dim = sel.get("dimension", "RG" if self.koen else "32")
        for a in p["variationAttributes"]:
            aid = a["attributeId"]
            for v in a["values"]:
                v["selected"] = v["value"] == {"color": color, "dimension": dim, "size": sel.get("size")}.get(aid)
                if aid == "size":
                    v["selectable"] = (color, dim, v["value"]) not in self.sold_out
        # The original API range min is not the price of the selected Spruce SKU.
        if not self.koen and "size" not in sel:
            p["price"] = {"type": "range", "min": {"sales": {"value": 62.4, "currency": "USD"}},
                          "max": {"sales": {"value": 104, "currency": "USD"}}}
        else:
            p["price"] = {"sales": {"value": 98 if self.koen else 104, "currency": "USD"}, "list": None}
        return {"product": p}


def listing(prod, url=NUMERIC_URL):
    item = {"product": prod, "url": url, "title": prod["title"], "sku": "US1-PRA-" + prod["code"],
            "category": "63863", "pics": {"main": ["https://example.com/image.jpg"], "colors": {}, "general": []}}
    app.Engine.__new__(app.Engine)._preselect_from_url(item, {})
    return item


class PranaRulesTests(unittest.TestCase):
    def test_numeric_pants_only_fetch_32_and_sizes_2_through_16(self):
        f = RetailerFixture()
        prod = prana.scrape(f, NUMERIC_URL)
        self.assertEqual({v["size"] for v in prod["variants"]}, {str(n) for n in range(2, 17, 2)})
        self.assertTrue(all(v.get("_site_size") == v["size"] + " 32" for v in prod["variants"]))
        self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {"32"})
        self.assertEqual({r["size"] for r in f.requests if "size" in r}, {str(n) for n in range(2, 17, 2)})
        self.assertTrue(all(v["price"] == 104 for v in prod["variants"]))
        item = listing(prod)
        self.assertEqual(item["variant_sel"], [f"Spruce||{n}" for n in range(2, 15, 2)])
        self.assertEqual(prod["details"]["Inseam"], "32 in")
        self.assertNotIn('30"', " ".join(prod["bullets"]))
        self.assertNotIn('34"', " ".join(prod["bullets"]))

    def test_explicit_short_url_does_not_override_fixed_listing_length(self):
        f = RetailerFixture()
        prod = prana.scrape(f, NUMERIC_URL + "&dwvar_9993201_dimension=30")
        self.assertTrue(all(v.get("_site_size", "").endswith(" 32") for v in prod["variants"]))
        self.assertNotIn("30", {r.get("dimension") for r in f.requests})

    def test_numeric_pants_without_32_use_31_with_clean_csv_sizes(self):
        f = RetailerFixture()
        for a in f.base["variationAttributes"]:
            if a["attributeId"] == "dimension":
                a["values"] = [value(s) for s in ("29", "31", "33")]
        f.sold_out.add(("Spruce", "31", "16"))
        prod = prana.scrape(f, NUMERIC_URL)
        self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {"31"})
        self.assertEqual({v["size"] for v in prod["variants"]}, {str(n) for n in range(2, 17, 2)})
        self.assertTrue(all(v["_site_size"] == v["size"] + " 31" for v in prod["variants"]))
        self.assertEqual(prod["details"]["Inseam"], "31 in")
        rows = core.listing_rows(listing(prod), {"sites": {}, "price_percent": 100})
        self.assertEqual(rows[0]["C:Inseam"], "31 in")
        self.assertEqual(rows[0]["RelationshipDetails"], "Size=2;4;6;8;10;12;14")
        self.assertEqual([r["_vkey"] for r in rows[1:]], [f"Spruce||{n} 31" for n in range(2, 15, 2)])

    def test_32_wins_over_31_even_if_32_is_sold_out(self):
        for sold_out in (False, True):
            with self.subTest(sold_out=sold_out):
                f = RetailerFixture()
                for a in f.base["variationAttributes"]:
                    if a["attributeId"] == "dimension":
                        a["values"] = [value(s) for s in ("29", "31", "32", "33")]
                if sold_out:
                    f.sold_out.update((c, "32", str(n)) for c in ("Spruce", "Black") for n in range(0, 19, 2))
                    with self.assertRaises(SkipProduct):
                        prana.scrape(f, NUMERIC_URL)
                else:
                    prod = prana.scrape(f, NUMERIC_URL)
                    self.assertEqual(prod["details"]["Inseam"], "32 in")
                self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {"32"})

    def test_sold_out_31_never_falls_back_to_stocked_29_or_33(self):
        f = RetailerFixture()
        for a in f.base["variationAttributes"]:
            if a["attributeId"] == "dimension":
                a["values"] = [value(s) for s in ("29", "31", "33")]
        f.sold_out.update((c, "31", str(n)) for c in ("Spruce", "Black") for n in range(0, 19, 2))
        with self.assertRaises(SkipProduct):
            prana.scrape(f, NUMERIC_URL)
        self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {"31"})

    def test_sold_out_32_never_falls_back_to_stocked_30_or_34(self):
        f = RetailerFixture()
        f.sold_out.update((c, "32", str(n)) for c in ("Spruce", "Black") for n in range(0, 19, 2))
        with self.assertRaises(SkipProduct):
            prana.scrape(f, NUMERIC_URL)
        self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {"32"})

    def test_sold_out_url_color_does_not_select_another_color(self):
        f = RetailerFixture()
        f.sold_out.update(("Spruce", "32", str(n)) for n in range(0, 19, 2))
        # Chỉ đọc màu trên link: màu đó hết hàng thì bỏ link, không đọc / chọn màu khác
        with self.assertRaises(SkipProduct):
            prana.scrape(f, NUMERIC_URL)
        self.assertNotIn("Black", {r.get("color") for r in f.requests})

    def test_koen_uses_reg_and_keeps_alpha_sizes_with_actual_30_inseam(self):
        f = RetailerFixture(koen=True)
        prod = prana.scrape(f, KOEN_URL)
        self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {"RG"})
        self.assertEqual([v["size"] for v in prod["variants"] if v["color"] == "Grey Blue"],
                         ["XS", "S", "M", "L", "XL", "XXL"])
        self.assertEqual(prod["details"]["Inseam"], "30 in")
        self.assertEqual(len(listing(prod, KOEN_URL)["variant_sel"]), 6)
        self.assertTrue(all(v.get("_site_size") == v["size"] + " Reg" for v in prod["variants"]))
        self.assertNotIn("SH-28", " ".join(prod["bullets"]))
        self.assertNotIn("TL-32", " ".join(prod["bullets"]))

    def test_other_prana_style_is_not_given_a_pants_length_policy(self):
        f = RetailerFixture()
        f.base["productName"] = "Sunday River Sweater"
        f.base["primaryCategoryID"] = "women-tops-sweaters"
        f.base["productPitch"] = "A comfortable sweater."
        prod = prana.scrape(f, NUMERIC_URL)
        # không áp quy tắc quần (size 2–16, 32/31); chiều dài vẫn chốt 1 giá trị (không có 8 -> giá trị đầu)
        self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {"30"})
        self.assertIn("0", {v["size"] for v in prod["variants"]})
        self.assertIn("18", {v["size"] for v in prod["variants"]})
        self.assertEqual(prod["details"]["Inseam"], "30 in")

    def test_shared_sfra_without_policy_keeps_all_dimensions_and_labels(self):
        f = RetailerFixture()
        first = f.get_json("https://example.com?pid=9993201")["product"]
        _, vs, _, _ = sfcc.sfra_variants(f, prana.HOST, prana.SITE, "9993201", first, images_fn=prana._images)
        self.assertEqual(len(vs), 60)
        self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {"30", "32", "34"})
        self.assertIn("0 30", {v["size"] for v in vs})
        self.assertTrue(all("_site_size" not in v for v in vs))

    def test_csv_fixed_inseam_clean_size_and_source_key_with_or_without_aspects(self):
        prod = prana.scrape(RetailerFixture(), NUMERIC_URL)
        item = listing(prod)
        aspects = [
            {"name": "Size", "variation": True, "values": [str(n) for n in range(2, 17, 2)]},
            {"name": "Color", "variation": True},
            {"name": "Inseam", "variation": True},
            {"name": "Size Type", "values": ["Regular"], "mode": "SELECTION_ONLY"},
        ]
        for known in (False, True):
            it = copy.deepcopy(item)
            if known:
                it.update(aspects=aspects, aspects_cat="63863")
            with self.subTest(known_aspects=known):
                rows = core.listing_rows(it, {"sites": {}, "price_percent": 100})
                self.assertEqual(rows[0]["C:Inseam"], "32 in")
                self.assertEqual(rows[0]["*C:Size Type"], "Regular")
                self.assertEqual(rows[0]["RelationshipDetails"], "Size=2;4;6;8;10;12;14")
                self.assertEqual([r["_vkey"] for r in rows[1:]], [f"Spruce||{n} 32" for n in range(2, 15, 2)])
                self.assertEqual({r["RelationshipDetails"] for r in rows[1:]}, {f"Size={n}" for n in range(2, 15, 2)})

    def test_mens_waists_keep_all_sizes_and_only_32_inseam(self):
        f = RetailerFixture()
        f.base.update(id="2147861", productName="Stretch Zion 5 Pocket Standard Pant",
                      primaryCategoryID="men-bottoms-pants")
        waists = ["28", "30", "31", "32", "33", "34", "35", "36", "38", "40", "42"]
        for a in f.base["variationAttributes"]:
            if a["attributeId"] == "size":
                a["values"] = [value(s) for s in waists]
            elif a["attributeId"] == "color":
                a["values"] = [value("Mud"), value("Black")]
        short_url = MENS_URL.replace("_dimension=32", "_dimension=30")
        prod = prana.scrape(f, short_url)
        selected = listing(prod, short_url)["variant_sel"]
        self.assertEqual(selected, [f"Mud||{s}" for s in waists])
        self.assertEqual({r["dimension"] for r in f.requests if "dimension" in r}, {"32"})
        self.assertEqual({r["size"] for r in f.requests if "size" in r}, set(waists))
        self.assertEqual(prod["details"]["Inseam"], "32 in")
        self.assertEqual(prod["department"], "Men")


if __name__ == "__main__":
    unittest.main()
