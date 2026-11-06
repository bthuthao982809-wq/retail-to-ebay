import json
import unittest

import core
import sites
from sites import hernest
from sites.base import SkipProduct

URL = "https://www.hernest.com/products/tree-p-10229.html"


def info():
    return {"productsSkuId": "10229", "productsId": "6908", "productsName": "Hernest Tree Full Green",
            "brandName": "Hernest", "currency": "USD", "salePrice": "459", "marketPrice": "699",
            "productsStatus": 1, "stocks": 3, "isOutStockCanBuy": 0,
            "saleAttrs": [{"productsOptionsName": "Height", "productsOptionsValuesName": "6.5 ft"},
                          {"productsOptionsName": "Shape", "productsOptionsValuesName": "Full"}],
            "paramsAttrs": [{"productsOptionsName": "Tree Color", "productsOptionsValuesName": "Green"}],
            "specInfo": {"specItemList": [{"name": "Dimension", "value": ['54\"W x 78\"H']}]},
            "collectionDesc": "A full tree {with lights}.", "sellingPoint": [{"title": "100% PE needles"}],
            "sourceImageList": [{"type": 1, "largeImage": "https://img.yitashop.com/product/tree.jpg"},
                                {"type": 2, "imagePath": "https://img.yitashop.com/video/tree.mp4"}]}


def scrape(product):
    state = {"page": {"/products/tree-p-10229.html": {"data": {"productInfo": product,
              "skuList": {"other": {"productsSkuId": "10265", "skuStatus": "1"}},
              "navBread": [{"title": "Home"}, {"title": "Christmas Tree"}, {"title": "SKU10229"}]}}}}
    html = "<script>window.__sandwich__=" + json.dumps(state) + "\nwindow.__sandwich_d__={}</script>"
    return hernest.scrape(type("F", (), {"get": lambda self, url: html})(), URL)


class HernestTests(unittest.TestCase):
    def test_exact_sku_price_attributes_specs_and_photos(self):
        prod = scrape(info())
        self.assertEqual(prod["code"], "10229")
        self.assertEqual(prod["title"], "Tree Full Green")
        self.assertEqual(prod["variants"], [{"id": "10229", "color": "Green", "size": "6.5 ft",
                                            "price": 459, "list_price": 699, "available": True}])
        self.assertEqual(prod["details"]["Shape"], "Full")
        self.assertEqual(prod["details"]["Dimension"], '54"W x 78"H')
        self.assertEqual(prod["breadcrumbs"], ["Christmas Tree"])
        self.assertEqual(prod["images"], ["https://img.yitashop.com/product/tree.jpg"])

    def test_wrong_sku_never_reads_sibling(self):
        p = info(); p["productsSkuId"] = "10265"
        with self.assertRaises(SkipProduct):
            scrape(p)

    def test_out_of_stock_or_unknown_stock_never_assumed_available(self):
        for stock in (0, None):
            p = info(); p["stocks"] = stock
            with self.subTest(stock=stock), self.assertRaises(SkipProduct):
                scrape(p)

    def test_explicit_backorder_supported(self):
        p = info(); p.update(stocks=0, isOutStockCanBuy=1)
        self.assertTrue(scrape(p)["variants"][0]["available"])

    def test_delisted_not_available_even_if_stock_positive(self):
        p = info(); p["productsStatus"] = 0
        with self.assertRaises(SkipProduct):
            scrape(p)

    def test_status_three_explicitly_orderable_is_not_sold_out(self):
        p = info(); p.update(productsStatus=3, isOutStockCanBuy=1)
        self.assertTrue(scrape(p)["variants"][0]["available"])
        p["productsStatus"] = 2
        with self.assertRaises(SkipProduct):
            scrape(p)

    def test_wrong_currency_and_empty_price_are_rejected(self):
        for field, value in (("currency", "EUR"), ("salePrice", ""), ("salePrice", 0)):
            p = info(); p[field] = value
            with self.subTest(field=field), self.assertRaises(SkipProduct):
                scrape(p)

    def test_image_url_rejects_fake_domain_and_video(self):
        for url in ("https://fakeyitashop.com/a.jpg", "https://img.yitashop.com/a.mp4", "http://img.yitashop.com/a.jpg"):
            self.assertEqual(hernest._image(url), "")

    def test_finish_is_color_and_no_fake_size(self):
        p = info(); p["saleAttrs"] = [{"productsOptionsName": "Finish", "productsOptionsValuesName": "Oak"}]
        prod = scrape(p)
        self.assertEqual((prod["variants"][0]["color"], prod["variants"][0]["size"], prod["size_name"]), ("Oak", "", ""))

    def test_registration_import_and_distinct_skus(self):
        mods = sites.load_all()
        text = "Profile,Action Country,Link sản phẩm,Site\nJP4,US," + URL + ",Hernest\nJP4,US," + URL.replace("10229", "10265") + ",Hernest\n"
        rows, warns = core.parse_import("links.csv", text.encode("utf-8-sig"), hernest, mods)
        self.assertEqual(len(rows), 2)
        self.assertEqual(warns, [])
        self.assertEqual(core.site_cfg(core.DEFAULT_CONFIG, hernest.KEY)["prefix"], "HER-")


if __name__ == "__main__":
    unittest.main()
