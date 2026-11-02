import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core


def servers(cfg, site):
    return [p["server"] for p in core.proxies_for(cfg, site)]


class SiteProxyTests(unittest.TestCase):
    def test_site_file_goes_first_then_shared_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "Proxy.txt"
            shared.write_text("1.1.1.1:1:u:p\n2.2.2.2:2\n", encoding="utf-8")
            (Path(tmp) / "Proxy-bestbuy.txt").write_text("9.9.9.9:9:u:p\n1.1.1.1:1:u:p\n", encoding="utf-8")
            cfg = {"proxy_file": str(shared), "proxies_enc": "", "sites": {}}
            self.assertEqual(servers(cfg, "bestbuy"), ["http://9.9.9.9:9", "http://1.1.1.1:1", "http://2.2.2.2:2"])
            self.assertEqual(servers(cfg, "theory"), ["http://1.1.1.1:1", "http://2.2.2.2:2"])   # không có proxy riêng
            self.assertEqual(servers(cfg, None), ["http://1.1.1.1:1", "http://2.2.2.2:2"])

    def test_settings_list_wins_over_file_and_clear_disables_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, "CONFIG_PATH", Path(tmp) / "config.json"):
            shared = Path(tmp) / "Proxy.txt"
            shared.write_text("1.1.1.1:1\n", encoding="utf-8")
            (Path(tmp) / "Proxy-bestbuy.txt").write_text("9.9.9.9:9\n", encoding="utf-8")
            cfg = {"proxy_file": str(shared), "proxies_enc": "", "sites": {"bestbuy": {"prefix": "BBY-"}}}
            cfg, ok, bad = core.edit_site_proxies(cfg, "bestbuy", "add", "5.5.5.5:5:u:p\nsai dang\n")
            self.assertEqual((ok, bad), (1, 1))
            self.assertEqual(servers(cfg, "bestbuy"), ["http://9.9.9.9:9", "http://5.5.5.5:5", "http://1.1.1.1:1"])  # file + dán
            cfg, _, _ = core.edit_site_proxies(cfg, "bestbuy", "remove", index=0)
            self.assertEqual(servers(cfg, "bestbuy"), ["http://5.5.5.5:5", "http://1.1.1.1:1"])
            cfg, _, _ = core.edit_site_proxies(cfg, "bestbuy", "clear")
            self.assertEqual(servers(cfg, "bestbuy"), ["http://1.1.1.1:1"])   # xoá hết: không đọc lại file riêng nữa
            self.assertEqual(cfg["sites"]["bestbuy"]["prefix"], "BBY-")        # các ô khác của site giữ nguyên
            with self.assertRaises(RuntimeError):
                core.edit_site_proxies(cfg, "khongco", "add", "1.1.1.1:1")

    def test_public_config_masks_site_proxies(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, "CONFIG_PATH", Path(tmp) / "config.json"):
            cfg = dict(core.DEFAULT_CONFIG, sites={"bestbuy": {}}, proxy_file="", proxies_enc="")
            cfg, _, _ = core.edit_site_proxies(cfg, "bestbuy", "add", "5.5.5.5:5:user:matkhau9x\n6.6.6.6:6")
            pub = core.public_config(cfg)
            self.assertEqual(pub["site_proxy_lists"], {"bestbuy": [{"addr": "5.5.5.5:5", "auth": True}, {"addr": "6.6.6.6:6", "auth": False}]})
            self.assertNotIn("proxies_enc", pub["sites"]["bestbuy"])
            self.assertNotIn("matkhau9x", str(pub))   # mật khẩu proxy không bao giờ ra giao diện


if __name__ == "__main__":
    unittest.main()
