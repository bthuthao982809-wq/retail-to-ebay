import unittest

import core


def prod(title="", bullets=(), details=None):
    return {"title": title, "bullets": list(bullets), "details": details or {}, "variants": [], "site": "bestbuy"}


class FccTests(unittest.TestCase):
    def test_wireless_products_need_fcc(self):
        for p in (prod("Shark Matrix Plus Robot Vacuum WiFi"),
                  prod("Speaker", ["Bluetooth 5.3 connection"]),
                  prod("Air Purifier", ["App Compatible: Yes"]),
                  prod("Robot", ["Voice Control", "Convenient hands-free cleaning"]),
                  # chữ dính trên trang Best Buy
                  prod("Levoit Eleva 320", ["With smart controlthrough the VeSync app"]),
                  prod("Sonos Beam", ["Control everything with the Sonos app"]),
                  prod("Sonos Ace", details={"Type": "Wireless Headphones"})):
            self.assertTrue(core.fcc_needed(p), p)

    def test_non_wireless_products_do_not_need_fcc(self):
        for p in (prod("Shark Cordless Stick Vacuum"),
                  prod("DREO Space Heater", ["12-hour timer and included remote control", "Remote Control: Yes",
                                             "App Compatible: No"]),
                  prod("Shark Air Purifier", ["App Compatible: No", "Wireless Connectivity: None", "Wi-Fi Enabled: No"]),
                  prod("Tineco Floor Washer", ["the smarter choice for busy pet households"]),
                  prod("Epson 302XL Ink Cartridges")):
            self.assertFalse(core.fcc_needed(p), p)

    def test_filled_fcc_id_clears_missing_and_goes_to_csv_column(self):
        item = {"product": prod("Robot Vacuum WiFi"), "specifics": "FCC ID=2ABCD-RV2110"}
        self.assertEqual(core.fcc_id(item), "2ABCD-RV2110")
        self.assertFalse(core.fcc_missing(item))
        self.assertTrue(core.fcc_missing({"product": prod("Robot Vacuum WiFi"), "specifics": ""}))
        spec, missing, _, _ = core.build_specifics(item, {}, "20614", {}, None, False, False)
        self.assertEqual(spec.get("FCC ID"), "2ABCD-RV2110")
        self.assertEqual(core.spec_col("FCC ID"), "C:FCC ID")

    def test_fcc_export_note_is_warning_not_skip(self):
        self.assertTrue(core.export_note("FCC ID: 3 listing có Wi-Fi / Bluetooth chưa điền FCC ID"))
        self.assertTrue(core.export_note("Danh sách ảnh lỗi: 1 ảnh"))
        self.assertTrue(core.export_note("US1-X: SKU đang dùng cho listing khác -> vẫn xuất, đổi SKU thành US1-X-1"))
        self.assertFalse(core.export_note("US1-X: Thiếu item specifics bắt buộc"))


if __name__ == "__main__":
    unittest.main()
