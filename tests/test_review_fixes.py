import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_title
import tracking
from sites.base import Blocked, Fetcher, ProxyPool


def listing(sku, variants, states):
    return {"sku": sku, "profile": "P1", "country": "US", "item_id": "11" + sku[-1], "site": "prana",
            "variants": variants, "check": {"vars": {v: {"state": s} for v, s in states.items()}}}


class ReviseTests(unittest.TestCase):
    def run_revise(self, items):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracking.json"
            path.write_text(json.dumps({"items": items}), encoding="utf-8")
            with patch.object(tracking, "PATH", path):
                files, no_id, n, no_rd = tracking.write_revise(Path(tmp) / "out")
                rows = []
                for f in files:
                    with open(f, encoding="utf-8-sig", newline="") as fh:
                        rows += list(csv.reader(fh))
                data = json.loads(path.read_text(encoding="utf-8"))
        return rows, no_rd, n, data

    def test_variation_without_relationship_details_is_not_written(self):
        items = {
            "A": listing("A", [{"vsku": "A-RED-S", "rd": "Color=Red|Size=S", "qty": 2},
                               {"vsku": "A-RED-M", "rd": "", "qty": 2}], {"A-RED-S": "oos", "A-RED-M": "oos"}),
            "B": listing("B", [{"vsku": "B-X-S", "rd": "", "qty": 2, "baseline": True},
                               {"vsku": "B-X-M", "rd": "", "qty": 2, "baseline": True}], {"B-X-S": "oos"}),
        }
        rows, no_rd, n, data = self.run_revise(items)
        variation = [r for r in rows if len(r) > 3 and r[3] == "Variation"]
        self.assertEqual([r[2] for r in variation], ["A-RED-S"])
        self.assertTrue(all(r[4] for r in variation))
        self.assertEqual(sorted(no_rd), ["A", "B"])
        self.assertEqual(n, 1)
        self.assertEqual(data["items"]["A"]["variants"][1]["qty"], 2)   # chưa ghi vào file -> giữ số lượng

    def test_single_listing_still_revised(self):
        items = {"C": listing("C", [{"vsku": "C", "rd": "", "qty": 2}], {"C": "oos"})}
        rows, no_rd, n, _ = self.run_revise(items)
        self.assertIn(["Revise", "11C", "C", "", "", "0"], rows)
        self.assertEqual(no_rd, [])

    def test_same_variant_sku_in_other_listing_keeps_quantity(self):
        items = {
            "D": listing("D", [{"vsku": "X-S", "rd": "Size=S", "qty": 2}, {"vsku": "X-M", "rd": "Size=M", "qty": 2}],
                         {"X-S": "oos"}),
            "E": listing("E", [{"vsku": "X-S", "rd": "Size=S", "qty": 2}, {"vsku": "X-M", "rd": "Size=M", "qty": 2}], {}),
        }
        _, _, _, data = self.run_revise(items)
        self.assertEqual(data["items"]["D"]["variants"][0]["qty"], 0)
        self.assertEqual(data["items"]["E"]["variants"][0]["qty"], 2)


class FetcherBlockTests(unittest.TestCase):
    def test_blocked_proxy_puts_subnet_on_cooldown(self):
        pool = ProxyPool([{"server": "http://161.77.1.1:1"}, {"server": "http://161.77.2.2:1"},
                          {"server": "http://77.47.1.1:1"}])
        f = Fetcher(use_proxy=True, pool=pool, tries=2)

        class Resp:
            status_code, text = 403, ""

        with patch("curl_cffi.requests.get", return_value=Resp()), patch("time.sleep"):
            with self.assertRaises(Exception):
                f.get("https://example.test/p")
        self.assertIn("161.77", pool.cooldown)


class AiTitleBrandTests(unittest.TestCase):
    def item(self, site, brand):
        return {"product": {"site": site, "brand": brand, "title": "Leggings"}}

    def test_generic_brand_word_kept_for_other_sites(self):
        t = ai_title.clean_title("Women's Thigh Gap Color Theory Leggings", self.item("prana", "prAna"))
        self.assertIn("Gap", t)
        self.assertIn("Theory", t)

    def test_generic_brand_removed_on_its_own_site(self):
        t = ai_title.clean_title("Theory Women's Wool Blazer Jacket", self.item("theory", "Theory"))
        self.assertNotIn("Theory", t)


if __name__ == "__main__":
    unittest.main()
