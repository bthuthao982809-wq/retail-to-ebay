"""Regression checks for repairing exported image URLs, without network/user data."""
import copy
import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import core


OLD = "https://i.ibb.co/failed/image.jpg"
NEW = "https://iili.io/recovered/image.jpg"
SRC = "https://retailer.invalid/images/original.jpg"
PROXY = {"server": "http://127.0.0.1:8123", "username": "test-user", "password": "test-pass"}
PROXY_URL = "http://test-user:test-pass@127.0.0.1:8123"


def item(site="eileenfisher"):
    return {
        "id": "r1", "title": "Hand edited title, Blue", "title_base": "Hand edited title",
        "title_extra": "Blue", "variant_sel": ["blue-m"], "pic": OLD,
        "product": {"site": site, "url": "https://retailer.invalid/product", "images": [SRC]},
        "pics": {"main": [OLD], "general": [OLD], "colors": {"Blue": [OLD]}},
    }


class ImageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for field, path in {
            "CACHE_PATH": folder / "image_cache.json", "PIC_CHECK_PATH": folder / "pic_check.json",
            "IMG_TMP": folder / "img_tmp",
        }.items():
            self.stack.enter_context(patch.object(core, field, path))
        self.stack.enter_context(patch.object(core.time, "sleep"))
        self.stack.enter_context(patch.object(core, "image_ok", return_value=False))
        self.stack.enter_context(patch.object(core, "process_image", return_value=(b"processed jpeg bytes", {"ok": True})))
        self.upload = self.stack.enter_context(patch.object(core, "upload_to_host", return_value=NEW))
        self.proxies = self.stack.enter_context(patch.object(core, "proxies_for", return_value=[PROXY]))
        self.plan = self.stack.enter_context(patch.object(core, "plan_images", return_value={
            "main": [SRC], "general": [SRC], "colors": {"Blue": [SRC]},
        }))
        self.cfg = {"image_size": 1600, "square_image": True, "image_host": "auto", "sites": {
            "eileenfisher": {"use_proxy": True}, "hernest": {"use_proxy": False},
        }}

    def test_recovery_uses_site_proxy_and_changes_all_slots_only(self):
        row = item()
        original = copy.deepcopy(row)
        calls = []

        def download(src, proxies=None, referer=None):
            calls.append((src, proxies, referer))
            if not proxies:
                raise RuntimeError("Không tải được ảnh gốc: HTTP 403")
            return b"original jpeg bytes"

        with patch.object(core, "download_image", side_effect=download):
            notes = core.fix_bad_pics([row], self.cfg)
        self.assertTrue(any(call[1] == {"http": PROXY_URL, "https": PROXY_URL} for call in calls))
        self.assertTrue(any(call.args[1] == "eileenfisher" for call in self.proxies.call_args_list))
        self.assertEqual(row["pics"], {"main": [NEW], "general": [NEW], "colors": {"Blue": [NEW]}})
        self.assertEqual(row["pic"], NEW)
        for key in ("title", "title_base", "title_extra", "variant_sel", "product"):
            self.assertEqual(row[key], original[key])
        self.assertTrue(any("đã up lại" in note for note in notes))
        self.assertFalse(any("chưa sửa được" in note for note in notes))
        self.assertNotIn("imgbb", self.upload.call_args.args[2])

    def test_direct_site_falls_back_to_proxy_if_original_is_blocked(self):
        calls = []

        def download(src, proxies=None, referer=None):
            calls.append(proxies)
            if proxies is None:
                raise RuntimeError("Không tải được ảnh gốc: HTTP 403")
            return b"original jpeg bytes"

        with patch.object(core, "download_image", side_effect=download):
            row = item("hernest")
            core.fix_bad_pics([row], self.cfg)
        self.assertIsNone(calls[0])
        self.assertEqual(calls[-1], {"http": PROXY_URL, "https": PROXY_URL})
        self.assertEqual(row["pic"], NEW)

    def test_protected_site_uses_proxy_even_if_always_proxy_switch_is_off(self):
        self.cfg["sites"]["eileenfisher"]["use_proxy"] = False
        with patch.object(core, "download_image", return_value=b"raw") as download:
            core.fix_bad_pics([item()], self.cfg)
        self.assertEqual(download.call_args_list[0].args[1], {"http": PROXY_URL, "https": PROXY_URL})

    def test_reverse_cache_restores_source_when_plan_lengths_changed(self):
        row = item()
        source = "https://retailer.invalid/images/exact-blue.jpg"
        cache_key = source + "|sq1600|freeimage"
        core.CACHE_PATH.write_text(json.dumps({cache_key: {"url": OLD, "info": {"width": 1600}}}), encoding="utf-8")
        self.plan.return_value = {"main": ["https://retailer.invalid/new-a.jpg", "https://retailer.invalid/new-b.jpg"],
                                  "general": [], "colors": {}}
        with patch.object(core, "download_image", return_value=b"raw") as download:
            notes = core.fix_bad_pics([row], self.cfg)
        self.assertEqual(download.call_args.args[0], source)
        self.assertEqual(row["pic"], NEW)
        self.assertEqual(json.loads(core.CACHE_PATH.read_text(encoding="utf-8"))[cache_key]["url"], NEW)
        self.assertFalse(any("chưa sửa được" in note for note in notes))

    def test_reverse_cache_overrides_positional_guess_to_avoid_wrong_image(self):
        source = "https://retailer.invalid/images/right-color.jpg"
        core.CACHE_PATH.write_text(json.dumps({source + "|sq1600|freeimage": {"url": OLD}}), encoding="utf-8")
        row = item()
        with patch.object(core, "download_image", return_value=b"raw") as download:
            core.fix_bad_pics([row], self.cfg)
        self.assertEqual(download.call_args.args[0], source)

    def test_local_image_backup_is_used_without_downloading_source(self):
        row = item()
        core.keep_image_bytes(OLD, b"local processed image")
        with patch.object(core, "download_image") as download:
            core.fix_bad_pics([row], self.cfg)
        download.assert_not_called()
        self.assertEqual(self.upload.call_args.args[0], b"local processed image")
        self.assertEqual(row["pic"], NEW)

    def test_referenced_old_local_backup_survives_export_cleanup(self):
        row = item()
        core.keep_image_bytes(OLD, b"local processed image")
        old_time = time.time() - (core.IMG_TMP_DAYS + 1) * 86400
        os.utime(core._tmp_file(OLD), (old_time, old_time))
        with patch.object(core, "download_image") as download:
            core.fix_bad_pics([row], self.cfg)
        download.assert_not_called()
        self.assertEqual(self.upload.call_args.args[0], b"local processed image")
        self.assertEqual(row["pic"], NEW)

    def test_unsuccessful_upload_does_not_mutate_image_slots(self):
        row = item()
        original = copy.deepcopy(row)
        self.upload.side_effect = RuntimeError("All image hosts unavailable")
        with patch.object(core, "download_image", return_value=b"raw"):
            notes = core.fix_bad_pics([row], self.cfg)
        self.assertEqual(row, original)
        self.assertTrue(any("chưa sửa được" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
