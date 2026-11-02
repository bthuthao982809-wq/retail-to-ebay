import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import app
import browser_support
import core
from sites import bestbuy
from sites.base import BrowserLaunchError, Fetcher, ProxyPool


class BrowserRecoveryTests(unittest.TestCase):
    def make_browser(self, folder):
        browser = app._Browser(MagicMock(), Path(folder) / 'profile', camoufox=True)
        browser.owned = MagicMock()
        browser.owned.handles = {}
        browser.owned.finish.return_value = ([], [])
        return browser

    def test_timeout_restarts_driver_and_retries_same_proxy_before_success(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'ROOT', Path(tmp)), patch('app.time.sleep'):
            b = self.make_browser(tmp)
            drivers = [MagicMock() for _ in range(3)]
            context = MagicMock()
            context.pages = [MagicMock()]
            proxy = {'server': 'http://example.invalid:123', 'username': 'secretuser', 'password': 'secretpass'}
            kw = {'headless': False, 'proxy': proxy}
            with patch.object(b, '_start_driver', side_effect=drivers) as start, \
                 patch.object(b, '_wait_profile_free') as wait, \
                 patch.object(b, '_launch_once', side_effect=[RuntimeError('Timeout 90000ms exceeded.\nBrowser logs:\nFULL STDERR'),
                                                            RuntimeError('Failed to launch'), context]) as launch:
                self.assertIs(b._launch(kw), context)
            self.assertEqual(start.call_count, 3)
            self.assertEqual(wait.call_count, 3)
            self.assertEqual(launch.call_args_list, [call(kw)] * 3)
            drivers[0].stop.assert_called_once()
            drivers[1].stop.assert_called_once()
            drivers[2].stop.assert_not_called()
            details = [json.loads(line) for line in (Path(tmp) / 'browser_diagnostics.log').read_text().splitlines()]
            self.assertIn('FULL STDERR', details[0]['detail'])
            self.assertEqual(details[0]['attempt'], 1)

    def test_exhausted_launches_are_typed_and_credential_free(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'ROOT', Path(tmp)), patch('app.time.sleep'):
            b = self.make_browser(tmp)
            proxy = {'server': 'http://user123:password123@host.invalid:12',
                     'username': 'user123', 'password': 'password123'}
            exc = RuntimeError('Failed to launch\nBrowser stderr secret: password123 user123\nhttp://user123:password123@host.invalid:12')
            with patch.object(b, '_wait_profile_free'), patch.object(b, '_start_driver', side_effect=lambda: MagicMock()), \
                 patch.object(b, '_launch_once', side_effect=exc) as launch:
                with self.assertRaisesRegex(BrowserLaunchError, 'browser_diagnostics.log'):
                    b._launch({'proxy': proxy})
            self.assertEqual(launch.call_count, 3)
            log = (Path(tmp) / 'browser_diagnostics.log').read_text()
            self.assertNotIn('password123', log)
            self.assertNotIn('user123', log)
            self.assertIn('Browser stderr', log)
            self.assertIsNone(b.pw)

    def test_busy_profile_is_never_launched_over_or_deleted(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'ROOT', Path(tmp)):
            b = self.make_browser(tmp)
            b.profile.mkdir()
            lock = b.profile / 'parent.lock'
            lock.write_bytes(b'keep')
            with patch('app.profile_is_free', return_value=False), patch('app.time.monotonic', side_effect=[0, 16]):
                with self.assertRaisesRegex(BrowserLaunchError, 'Hồ sơ'):
                    b._wait_profile_free()
            self.assertEqual(lock.read_bytes(), b'keep')

    def test_close_failure_resets_driver_and_preserves_full_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, 'ROOT', Path(tmp)):
            b = self.make_browser(tmp)
            driver, context = MagicMock(), MagicMock()
            b.pw, b.ctx = driver, context
            context.close.side_effect = RuntimeError('Close failed\nfull close stderr')
            b._close_ctx()
            driver.stop.assert_called_once()
            self.assertIsNone(b.pw)
            self.assertIsNone(b.ctx)
            self.assertIn('full close stderr', (Path(tmp) / 'browser_diagnostics.log').read_text())

    def test_browser_launch_failure_never_rotates_or_blames_proxy(self):
        f = Fetcher(page_factory=MagicMock(), pool=ProxyPool([{'server': 'http://a:1'}]))
        with patch.object(bestbuy, '_capture_with_browser', side_effect=BrowserLaunchError('Timeout')), \
             patch.object(f, 'switch_to_proxy') as switch, patch.object(f.pool, 'rotate') as rotate, \
             patch.object(f, 'note_ip') as note:
            with self.assertRaisesRegex(Exception, 'Không mở được trình duyệt'):
                bestbuy._load(f, bestbuy.EXAMPLES[0])
        switch.assert_not_called()
        rotate.assert_not_called()
        note.assert_not_called()

    def test_site_connection_reset_still_switches_proxy_and_retries_same_link(self):
        f = Fetcher(page_factory=MagicMock(), pool=ProxyPool([{'server': 'http://a:1'}, {'server': 'http://b:2'}]))
        with patch.object(bestbuy, '_capture_with_browser', side_effect=[RuntimeError('NS_ERROR_NET_RESET'), {'prod': 'live'}]) as capture, \
             patch.object(bestbuy, '_pause'):
            result = bestbuy._load(f, bestbuy.EXAMPLES[0])
        self.assertEqual(result, {'prod': 'live'})
        self.assertTrue(f.proxy_on)
        self.assertEqual(capture.call_args_list[0], capture.call_args_list[1])

    def test_process_cleanup_uses_owned_handles_only(self):
        guard = browser_support.OwnedBrowserProcesses()
        guard.handles = {111: 901, 222: 902}
        api = MagicMock()
        # First handle exited normally; second lingers, then exits after terminate.
        api.WaitForSingleObject.side_effect = [0, 258, 0, 0]
        api.TerminateProcess.return_value = True
        with patch('browser_support._windows_api', return_value=(None, None, api)):
            killed, remaining = guard.finish(grace=0)
        self.assertEqual(killed, [222])
        self.assertFalse(remaining)
        api.TerminateProcess.assert_called_once_with(902, 1)
        self.assertEqual(api.CloseHandle.call_args_list, [call(901), call(902)])
        self.assertFalse(guard.handles)


if __name__ == '__main__':
    unittest.main()
