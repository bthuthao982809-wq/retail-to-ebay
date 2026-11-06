import contextlib
import copy
import io
import json
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import core
import sites


ROOT = Path(__file__).resolve().parents[1]


class DistributableConfigTests(unittest.TestCase):
    def test_generation_removes_nested_private_proxies_keeps_public_key_and_site_settings(self):
        source = {
            "freeimage_api_key": core.DEFAULT_CONFIG["freeimage_api_key"],
            "gemini_api_key": "private-ai-key", "proxies_enc": "private-global-proxy",
            "proxy_file": "C:/private/Proxy.txt",
            "sites": {
                "bestbuy": {"prefix": "BBY-", "delay": "15", "use_proxy": True,
                            "proxies_enc": "private-site-proxy", "proxy_file": "C:/private/bestbuy.txt"},
                "hernest": {"prefix": "HER-", "price_percent": "80"},
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            script = folder / "make_default_config.py"
            shutil.copy2(ROOT / script.name, script)
            with patch.object(core, "load_config", return_value=copy.deepcopy(source)), contextlib.redirect_stdout(io.StringIO()):
                runpy.run_path(str(script), run_name="__main__")
            result = json.loads((folder / "build/config.default.json").read_text(encoding="utf-8"))
        self.assertFalse(result["gemini_api_key"])
        self.assertFalse(result["proxies_enc"])
        self.assertNotIn("proxy_file", result)
        self.assertEqual(result["freeimage_api_key"], source["freeimage_api_key"])
        self.assertEqual(result["sites"]["bestbuy"], {"prefix": "BBY-", "delay": "15", "use_proxy": True})
        self.assertEqual(result["sites"]["hernest"], source["sites"]["hernest"])

    def check_fixture(self, config):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            internal = folder / "_internal"
            (internal / "web/fonts").mkdir(parents=True)
            for name in ("web/index.html", "web/fonts/inter-latin.woff2", "web/bestbuy_capture.js"):
                (internal / name).write_bytes(b"fixture")
            (internal / "config.default.json").write_text(json.dumps(config), encoding="utf-8")
            fake_pyz = SimpleNamespace(toc={**{f"sites.{name}": None for name in sites.ORDER},
                                            "sites.prana_browser": None, "camoufox.sync_api": None})
            fake_archive = SimpleNamespace(toc={"PYZ.pyz": None}, open_embedded_archive=lambda name: fake_pyz)
            output = io.StringIO()
            code = 0
            with patch("PyInstaller.archive.readers.CArchiveReader", return_value=fake_archive), \
                 patch("sys.argv", ["check_exe.py", str(folder)]), contextlib.redirect_stdout(output):
                try:
                    runpy.run_path(str(ROOT / "check_exe.py"), run_name="__main__")
                except SystemExit as exc:
                    code = exc.code
        return code, output.getvalue()

    def test_validator_rejects_nested_proxy_and_reports_only_field_paths(self):
        for field in ("proxies_enc", "proxy_file"):
            secret = "PRIVATE-FIXTURE-DO-NOT-PRINT"
            with self.subTest(field=field):
                code, output = self.check_fixture({"sites": {"bestbuy": {field: secret}}})
                self.assertEqual(code, 1)
                self.assertIn(f"sites.bestbuy.{field}", output)
                self.assertNotIn(secret, output)

    def test_validator_allows_empty_nested_proxy_and_public_default_key(self):
        code, output = self.check_fixture({
            "freeimage_api_key": core.DEFAULT_CONFIG["freeimage_api_key"],
            "sites": {"bestbuy": {"proxies_enc": "", "proxy_file": ""}},
        })
        self.assertEqual(code, 0)
        self.assertIn("OK:", output)


if __name__ == "__main__":
    unittest.main()
