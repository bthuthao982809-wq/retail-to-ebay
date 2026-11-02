import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import core
from sites import bestbuy
from sites.base import Fetcher


def engine():
    e = app.Engine.__new__(app.Engine)
    e.lock = threading.Lock()
    e.jobs = queue.Queue()
    e.sessions = {}
    return e


class BestBuySpeedTests(unittest.TestCase):
    def setUp(self):   # không nghỉ 30 giây thật khi bị chặn trong test
        p = patch.object(bestbuy, 'BLOCK_PAUSE', 0)
        p.start()
        self.addCleanup(p.stop)

    def test_blocked_link_keeps_trying_other_ips_and_counts_per_ip(self):
        from sites.base import ProxyPool
        used = []
        class Page:
            url = bestbuy.EXAMPLES[0]
            def goto(self, url, **kwargs): pass
            def evaluate(self, script):
                return {'prod': {'name': 'ok'}, 'url': self.url} if 'saved_at' in script else True
            def wait_for_timeout(self, ms): pass
        def factory(proxy):
            server = proxy['server'] if proxy else None
            used.append(server)
            if server != 'http://4.4.4.4:4':
                raise RuntimeError('NS_ERROR_NET_RESET')   # IP gốc + 2 proxy đầu bị chặn, proxy thứ 3 vào được
            return Page()
        pool = ProxyPool([{'server': f'http://{n}.{n}.{n}.{n}:{n}'} for n in (2, 3, 4, 5)])
        f = Fetcher(pool=pool, page_factory=factory)
        with patch.object(bestbuy, 'BLOCK_PAUSE', 30), patch.object(bestbuy, '_pause') as pause:
            data = bestbuy._load(f, bestbuy.EXAMPLES[0])   # trước đây hết 3 lượt là bỏ qua link
        self.assertEqual(data['bsin'], 'JXJVXLX9LF')
        self.assertEqual(used, [None, 'http://2.2.2.2:2', 'http://3.3.3.3:3', 'http://4.4.4.4:4'])
        self.assertEqual(pause.call_count, 3)   # nghỉ trước mỗi lần đổi IP
        self.assertEqual(f.ip_summary(), 'IP gốc bị chặn 1 lần · 2.2.2.2:2 bị chặn 1 lần · 3.3.3.3:3 bị chặn 1 lần · 4.4.4.4:4 1 link')

    def test_three_uploads_in_flight_preserve_gallery_and_main_info(self):
        e = engine()
        s = app.Session(bestbuy)
        active = peak = 0
        guard = threading.Lock()
        def upload(src, *args, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(.06 if src == 'a' else .02)
            with guard:
                active -= 1
            return 'https://img/' + src, {'source': src}, False
        plan = {'main': list('abcdef'), 'colors': {'Blue': ['b', 'a']}, 'general': ['f']}
        with patch.object(core, 'rehost_image', side_effect=upload), patch.object(core, 'host_usage', return_value={}):
            pics, info, _ = e._upload_pics(s, {'url': 'https://bestbuy.com'}, {}, {}, plan=plan)
        self.assertEqual(peak, 3)
        self.assertEqual(pics['main'], ['https://img/' + x for x in 'abcdef'])
        self.assertEqual(pics['colors']['Blue'], ['https://img/b', 'https://img/a'])
        self.assertEqual(info, {'source': 'a'})

    def test_cache_file_keeps_all_concurrent_uploads(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            cache, path = {}, Path(tmp) / 'cache.json'
            with patch.object(core, 'CACHE_PATH', path), patch.object(core, 'download_image', return_value=b'raw'), \
                 patch.object(core, 'process_image', return_value=(b'jpg', {'w': 800})), \
                 patch.object(core, 'upload_to_host', return_value='https://img/result'), \
                 patch.object(core, 'keep_image_bytes'):
                with ThreadPoolExecutor(3) as pool:
                    list(pool.map(lambda n: core.rehost_image(f'https://cdn/{n}', {}, cache), range(18)))
            self.assertEqual(len(json.loads(path.read_text())), 18)
            self.assertEqual(len(cache), 18)

    def test_repeated_product_reads_once_and_returns_isolated_copy(self):
        e, s = engine(), app.Session(bestbuy)
        f = SimpleNamespace(bestbuy_data_live=True)
        data = {'code': 'JXJVXLX9LF', 'variants': [{'price': 159.99}]}
        cache = {}
        with patch.object(bestbuy, 'scrape', return_value=data) as scrape, patch.object(app, 'validate', side_effect=lambda p: p):
            first = e._read_product(s, f, bestbuy.EXAMPLES[0], cache)
            first['variants'][0]['price'] = 1
            second = e._read_product(s, f, bestbuy.EXAMPLES[0] + '?ref=another-profile', cache)
            third = e._read_product(s, f, bestbuy.EXAMPLES[0], cache)
        self.assertEqual(scrape.call_count, 1)
        self.assertEqual(second['variants'][0]['price'], 159.99)
        second['variants'].clear()
        self.assertEqual(len(third['variants']), 1)

    def test_saved_fallback_does_not_enter_live_cache(self):
        e, s = engine(), app.Session(bestbuy)
        cache = {}
        with patch.object(bestbuy, 'scrape', return_value={}) as scrape, patch.object(app, 'validate', side_effect=lambda p: p):
            for _ in range(2):
                e._read_product(s, SimpleNamespace(bestbuy_data_live=False), bestbuy.EXAMPLES[0], cache)
        self.assertEqual(scrape.call_count, 2)
        self.assertFalse(cache)

    def test_resume_preserves_66_ids_and_starts_at_row_four(self):
        e, s = engine(), app.Session(bestbuy)
        e.sessions['bestbuy'] = s
        s.results = [{'id': f'r{i}', 'url': bestbuy.EXAMPLES[0], 'profile': f'US{1 + (i - 1) // 22}',
                      'status': 'ok' if i <= 3 else 'error'} for i in range(1, 67)]
        original = list(s.results)
        self.assertEqual(e.retry_errors('bestbuy', True), 63)
        _, items, _ = e.jobs.get_nowait()
        self.assertEqual((items[0]['retry_id'], items[-1]['retry_id']), ('r4', 'r66'))
        self.assertEqual(s.results, original)
        self.assertEqual(s.progress, [3, 66])

    def test_capture_waits_for_product_not_domcontentloaded(self):
        class Page:
            def __init__(self):
                self.url = bestbuy.EXAMPLES[0]
                self.wait_until = ''
            def goto(self, url, **kwargs):
                self.wait_until = kwargs['wait_until']
            def evaluate(self, script):
                return {'prod': {'name': 'Test'}, 'url': self.url} if 'saved_at' in script else True
        page = Page()
        f = Fetcher(page_factory=lambda proxy: page)
        data = bestbuy._capture_with_browser(f, bestbuy.EXAMPLES[0], 'JXJVXLX9LF')
        self.assertEqual(page.wait_until, 'commit')
        self.assertEqual(data['bsin'], 'JXJVXLX9LF')

    def test_launch_failure_does_not_switch_proxy(self):
        from sites.base import BrowserLaunchError, ProxyPool, SkipProduct
        logs = []
        def factory(proxy):
            raise BrowserLaunchError('Failed to launch the browser process.')
        f = Fetcher(pool=ProxyPool([{'server': 'http://1.1.1.1:1'}, {'server': 'http://2.2.2.2:2'}]),
                    page_factory=factory, log=lambda t, tag='': logs.append(t))
        with self.assertRaises(SkipProduct) as cm:
            bestbuy._load(f, bestbuy.EXAMPLES[0])
        self.assertIn('Không mở được trình duyệt', str(cm.exception))
        self.assertFalse(f.proxy_on)
        self.assertFalse(any('⇄' in t for t in logs))

    def test_connection_reset_rotates_proxy_and_reports_block(self):
        from sites.base import ProxyPool, SkipProduct
        used = []
        def factory(proxy):
            used.append(proxy['server'] if proxy else None)
            raise RuntimeError('NS_ERROR_NET_RESET')
        f = Fetcher(pool=ProxyPool([{'server': 'http://1.1.1.1:1'}, {'server': 'http://2.2.2.2:2'}]), page_factory=factory)
        with self.assertRaises(SkipProduct) as cm:
            bestbuy._load(f, bestbuy.EXAMPLES[0])
        self.assertIn('chặn', str(cm.exception))
        self.assertEqual(used, [None, 'http://1.1.1.1:1', 'http://2.2.2.2:2'])

    def test_dead_proxy_is_dropped_without_using_an_attempt(self):
        from sites.base import ProxyPool
        used = []
        class Page:
            url = bestbuy.EXAMPLES[0]
            def goto(self, url, **kwargs): pass
            def evaluate(self, script):
                return {'prod': {'name': 'ok'}, 'url': self.url} if 'saved_at' in script else True
            def wait_for_timeout(self, ms): pass
        def factory(proxy):
            server = proxy['server'] if proxy else None
            used.append(server)
            if server is None:
                raise RuntimeError('NS_ERROR_NET_RESET')                     # IP gốc bị chặn -> sang proxy
            if server in ('http://1.1.1.1:1', 'http://2.2.2.2:2'):
                raise RuntimeError('NS_ERROR_PROXY_CONNECTION_REFUSED')      # 2 proxy đầu chết
            return Page()
        pool = ProxyPool([{'server': 'http://1.1.1.1:1'}, {'server': 'http://2.2.2.2:2'}, {'server': 'http://3.3.3.3:3'}])
        f = Fetcher(pool=pool, page_factory=factory)
        data = bestbuy._load(f, bestbuy.EXAMPLES[0])
        self.assertEqual(data['bsin'], 'JXJVXLX9LF')
        self.assertEqual(used, [None, 'http://1.1.1.1:1', 'http://2.2.2.2:2', 'http://3.3.3.3:3'])
        self.assertEqual([p['server'] for p in pool.proxies], ['http://3.3.3.3:3'])   # proxy chết bị bỏ khỏi lần chạy

    def test_rotate_after_block_prefers_other_subnet(self):
        from sites.base import ProxyPool
        pool = ProxyPool([{'server': 'http://161.77.1.1:1'}, {'server': 'http://161.77.2.2:1'}, {'server': 'http://77.47.1.1:1'}])
        self.assertEqual(pool.rotate(blocked=True)['server'], 'http://77.47.1.1:1')   # bỏ qua proxy cùng dải 161.77
        self.assertEqual(pool.rotate(blocked=True)['server'], 'http://161.77.1.1:1')  # dải nào cũng bị: lấy kế tiếp
        pool2 = ProxyPool([{'server': 'http://161.77.1.1:1'}, {'server': 'http://161.77.2.2:1'}])
        self.assertEqual(pool2.rotate()['server'], 'http://161.77.2.2:1')              # không chặn: xoay vòng như cũ

    def test_block_page_is_detected_fast_rotates_proxy_and_reports_block(self):
        from sites.base import ProxyPool, SkipProduct
        used, waits = [], []
        class Page:
            url = bestbuy.EXAMPLES[0]
            def goto(self, url, **kwargs): pass
            def evaluate(self, script):
                return 'Access Denied' if 'access denied' in script else False   # ready() = False, BLOCK_JS = trúng
            def wait_for_timeout(self, ms): waits.append(ms)
        def factory(proxy):
            used.append(proxy['server'] if proxy else None)
            return Page()
        f = Fetcher(pool=ProxyPool([{'server': 'http://1.1.1.1:1'}, {'server': 'http://2.2.2.2:2'}]), page_factory=factory)
        with self.assertRaises(SkipProduct) as cm:
            bestbuy._load(f, bestbuy.EXAMPLES[0])
        self.assertIn('chặn', str(cm.exception))
        self.assertIn('Access Denied', str(cm.exception))
        self.assertEqual(used, [None, 'http://1.1.1.1:1', 'http://2.2.2.2:2'])
        self.assertEqual(waits, [])   # báo ngay, không chờ 20 giây

    def test_imgbb_rate_limit_pauses_host_30_minutes_without_retries(self):
        import core
        calls = []
        def imgbb(data, cfg):
            calls.append('imgbb')
            raise core.HostRateLimited('Rate limit reached')
        def free(data, cfg):
            calls.append('free')
            return 'https://iili.io/x.jpg'
        core._HOST_DOWN.clear(); core._HOST_WHY.clear()
        try:
            with patch.object(core, 'upload_to_imgbb', side_effect=imgbb), patch.object(core, 'upload_to_freeimage', side_effect=free):
                self.assertEqual(core.upload_to_host(b'x', {'imgbb_api_key': 'k'}), 'https://iili.io/x.jpg')
                self.assertEqual(calls, ['imgbb', 'free'])
                self.assertGreater(core._HOST_DOWN['imgbb'], time.time() + 25 * 60)
                self.assertIn('ImgBB tạm ngưng', core.host_down_note())
                core.upload_to_host(b'y', {'imgbb_api_key': 'k'})
                self.assertEqual(calls, ['imgbb', 'free', 'free'])   # 30 phút sau mới thử ImgBB lại
        finally:
            core._HOST_DOWN.clear(); core._HOST_WHY.clear()

    def test_resume_button_takes_only_pending_rows(self):
        e, s = engine(), app.Session(bestbuy)
        e.sessions['bestbuy'] = s
        s.results = [{'id': 'r1', 'url': bestbuy.EXAMPLES[0], 'status': 'ok'},
                     {'id': 'r2', 'url': bestbuy.EXAMPLES[0], 'status': 'error', 'error': 'Camoufox không lấy được trang X'},
                     {'id': 'r3', 'url': bestbuy.EXAMPLES[0], 'status': 'error', 'error': 'Đã dừng'},
                     {'id': 'r4', 'url': bestbuy.EXAMPLES[0], 'status': 'error', 'error': 'Chưa chạy — đã bấm Dừng'}]
        self.assertEqual(e.retry_errors('bestbuy', True, only='pending'), 2)
        _, items, _ = e.jobs.get_nowait()
        self.assertEqual([it['retry_id'] for it in items], ['r3', 'r4'])
        s.running = False
        self.assertEqual(e.retry_errors('bestbuy', True, only='failed'), 1)
        _, items, _ = e.jobs.get_nowait()
        self.assertEqual([it['retry_id'] for it in items], ['r2'])


if __name__ == '__main__':
    unittest.main()
