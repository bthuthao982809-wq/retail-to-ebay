import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import core


def item(*urls):
    return {"product": {"url": "https://site/p"}, "pics": {"main": list(urls), "colors": {}}, "pic": urls[0]}


class PicCheckTests(unittest.TestCase):
    def test_second_export_skips_images_verified_recently(self):
        it = item("https://img/a", "https://img/b")
        calls, logs = [], []
        def ok(url, tries=1):
            calls.append(url)
            return True
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, "PIC_CHECK_PATH", Path(tmp) / "pc.json"), \
             patch.object(core, "image_ok", side_effect=ok), patch.object(core, "purge_image_tmp"), \
             patch.object(core, "plan_images", return_value={"main": ["s1", "s2"], "colors": {}, "general": []}):
            self.assertEqual(core.fix_bad_pics([it], {}, log=logs.append), [])
            self.assertEqual(sorted(calls), ["https://img/a", "https://img/b"])
            self.assertIn("Kiểm 2 ảnh", logs[-1])
            core.fix_bad_pics([it], {}, log=logs.append)
            self.assertEqual(len(calls), 2)   # lần xuất sau không tải lại
            self.assertIn("Kiểm 0 ảnh", logs[-1])
            self.assertIn("bỏ qua 2 ảnh", logs[-1])

    def test_flaky_host_is_rechecked_before_counting_as_bad(self):
        it = item("https://img/a", "https://img/b")
        seen, steps = {}, []
        def flaky(url, tries=1):   # lần đầu ảnh b lỗi (host chập chờn), kiểm lại thì tốt
            seen[url] = seen.get(url, 0) + 1
            return url != "https://img/b" or seen[url] > 1
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, "PIC_CHECK_PATH", Path(tmp) / "pc.json"), \
             patch.object(core, "image_ok", side_effect=flaky), patch.object(core, "purge_image_tmp"), \
             patch.object(core.time, "sleep"), \
             patch.object(core, "plan_images", return_value={"main": ["s1", "s2"], "colors": {}, "general": []}):
            self.assertEqual(core.fix_bad_pics([it], {}, step=steps.append), [])
            self.assertEqual(sorted(core.load_pic_checks()), ["https://img/a", "https://img/b"])
        self.assertEqual(steps[0], "Kiểm ảnh đã up 0/2")
        self.assertIn("Kiểm ảnh đã up 2/2", steps)
        self.assertIn("Kiểm lại 1 ảnh vừa báo lỗi", steps)

    def test_bad_image_is_not_remembered_as_good(self):
        it = item("https://img/bad")
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, "PIC_CHECK_PATH", Path(tmp) / "pc.json"), \
             patch.object(core, "image_ok", return_value=False), patch.object(core, "purge_image_tmp"), \
             patch.object(core.time, "sleep"), \
             patch.object(core, "plan_images", return_value={"main": [], "colors": {}, "general": []}):
            notes = core.fix_bad_pics([it], {})
            self.assertTrue(any("chưa sửa được" in n for n in notes))
            self.assertEqual(core.load_pic_checks(), {})

    def test_old_checks_expire(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, "PIC_CHECK_PATH", Path(tmp) / "pc.json"):
            core.save_pic_checks({"https://img/old": time.time() - 4 * 86400, "https://img/new": time.time()})
            self.assertEqual(list(core.load_pic_checks()), ["https://img/new"])


if __name__ == "__main__":
    unittest.main()
