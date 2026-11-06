"""Read prAna JSON inside one persistent browser session.

The caller owns the browser lifecycle. A blocked link tries bounded configured
routes with Best Buy's pacing, then retains the manual-verification fallback.
"""
import json
import random
import re
import time
from urllib.parse import parse_qs, urlparse

from sites.base import BrowserLaunchError, Fetcher, SiteWaiting, SkipProduct, is_bot_block
from sites.sfcc import attr


# Match Best Buy's recovery pacing without changing its transport or other sites.
BLOCK_PAUSE = 30
MAX_IPS = 8
MAX_ORDINARY_ATTEMPTS = 3
PROXY_ERR = re.compile(r"NS_ERROR_PROXY|PROXY_CONNECTION|ERR_PROXY", re.I)
CONN_ERR = re.compile(r"NS_ERROR_NET|NS_ERROR_CONNECTION|ERR_CONNECTION|ERR_TIMED_OUT|Timeout \d+ms", re.I)


class _RouteBlocked(Exception):
    """Retry the current request through a different configured route."""


class _ProxyBroken(Exception):
    """The proxy itself failed; this is not a site block."""


class _ManualRequired(SiteWaiting):
    """No automatic route remains for a challenge response."""


class _TransientProductError(SkipProduct):
    """A product response may be retried without classifying an IP as blocked."""


PAGE_STATE = r"""() => {
    const visible = element => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const captcha = [...document.querySelectorAll(
        '#px-captcha, [id^="px-captcha"], iframe[src*="px-cdn.net"], iframe[src*="/captcha"]'
    )].some(visible);
    const text = (document.body?.innerText || '').slice(0, 10000);
    const challenge = captcha || /press\s*(?:&|and)\s*hold|verify (?:that )?you are human|confirm you are human|access to this page has been denied/i.test(text);
    const product = document.querySelector('.product-detail[data-pid]');
    return {
        challenge,
        ready: !!product && !challenge,
        pid: product?.getAttribute('data-pid') || '',
        master_pid: product?.getAttribute('data-master-pid') || ''
    };
}"""

FETCH_JSON = r"""async ({url, headers, timeout}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
        const response = await fetch(url, {
            credentials: 'include', headers, signal: controller.signal
        });
        return {status: response.status, text: await response.text()};
    } catch (error) {
        return {status: 0, error: error.name || 'NetworkError', text: ''};
    } finally {
        clearTimeout(timer);
    }
}"""


def _alert_user():
    """Kêu 1 tiếng và nhấp nháy cửa sổ Camoufox trên thanh taskbar (Windows). Lỗi thì bỏ qua."""
    try:
        import ctypes
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        user32 = ctypes.windll.user32

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p), ("dwFlags", ctypes.c_uint),
                        ("uCount", ctypes.c_uint), ("dwTimeout", ctypes.c_uint)]

        hwnds = []
        cls = ctypes.create_unicode_buffer(64)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def collect(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                user32.GetClassNameW(hwnd, cls, 64)
                if cls.value == "MozillaWindowClass":   # cửa sổ Firefox / Camoufox
                    hwnds.append(hwnd)
            return True

        user32.EnumWindows(collect, 0)
        for hwnd in hwnds:
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 3 | 12, 0, 0)   # FLASHW_ALL | FLASHW_TIMERNOFG
            user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


class PranaBrowserFetcher(Fetcher):
    """Fetcher-compatible transport backed by a visible Camoufox page."""

    # Trong trình duyệt, chọn màu = request giống người bấm: đọc đủ màu 1 lần, các dòng màu khác dùng lại
    READ_ALL_COLORS = True
    ALERT_EVERY = 120   # giây: nhắc lại khi vẫn đang chờ xác minh
    LOAD_TIMEOUT = 180  # giây: trang không tải xong (không có ô xác minh) thì thôi chờ

    def __init__(self, *args, set_wait=None, clear_wait=None, verification_timeout=300,
                 request_timeout=25, alert=None, reset_browser=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_wait = set_wait or (lambda reason, until=0: None)
        self.clear_wait = clear_wait or (lambda: None)
        # None = chờ tới khi người dùng xác minh xong (hoặc bấm Dừng)
        self.verification_timeout = None if verification_timeout is None else max(0.0, float(verification_timeout))
        self.alert = alert or (lambda: None)
        self.reset_browser = reset_browser
        self._auto_recovery = False
        self._attempted_routes = set()
        self._blocked_routes = set()
        self._noted_url = ""
        self.request_timeout = max(1.0, float(request_timeout))
        self._route = dict(self.pool.current()) if self.use_proxy and self.pool.current() else None
        self._page = None
        self._product_url = ""
        self._product_pid = ""
        self._loaded_pid = ""
        self._last_color = ""
        self._color_aliases = {}
        self.request_count = 0
        self.navigation_count = 0

    def current_proxy(self):
        return dict(self._route) if self._route else None

    def _route_key(self):
        return self._route["server"] if self._route else "<direct>"

    def _can_change_route(self):
        return (self._auto_recovery and len(self._attempted_routes) < MAX_IPS
                and self.pool.has_ready(exclude=self._attempted_routes))

    def _mark_blocked(self):
        key = self._route_key()
        if key not in self._blocked_routes:
            self._blocked_routes.add(key)
            self.note_ip("blocked")

    def _pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check_stop()
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))
        self._check_stop()

    def _change_route(self, broken=False):
        """Release the owned browser, then retry this JSON/PDP on an untried proxy."""
        if broken:
            proxy = self.pool.drop_current(exclude=self._attempted_routes)
            if len(self._attempted_routes) >= MAX_IPS:
                return False
        elif len(self._attempted_routes) >= MAX_IPS:
            return False
        elif self._route:
            proxy = self.pool.rotate(blocked=True, individual=True, exclude=self._attempted_routes)
        else:
            proxy = self.pool.select_ready(exclude=self._attempted_routes)
        if proxy is None:
            return False
        self._check_stop()
        if self.reset_browser is not None:
            self.reset_browser()
        self._page, self._loaded_pid = None, ""
        self._route = dict(proxy)
        self.switched = True
        self._attempted_routes.add(self._route_key())
        if broken:
            self.log("  prAna: proxy không kết nối được — bỏ proxy hỏng, mở lại cùng link bằng proxy kế tiếp", "warn")
        else:
            reason = f"prAna: nghỉ {BLOCK_PAUSE} giây rồi mở lại cùng link bằng IP kế tiếp"
            self.log("  " + reason + f" ({len(self._attempted_routes)}/{MAX_IPS} IP)", "warn")
            self.set_wait(reason, time.time() + BLOCK_PAUSE)
            self._pause(BLOCK_PAUSE)
            self.clear_wait()
        return True

    def ip_label(self):
        return self._route["server"].split("://", 1)[-1] if self._route else "IP gốc"

    def _proxies(self):
        # Image downloading can use the same pinned route as this browser.
        if self._route is None:
            return None
        from sites.base import proxy_url
        url = proxy_url(self._route)
        return {"http": url, "https": url}

    def prepare_product(self, url):
        self._validate_url(url)
        match = re.search(r"/(\d{5,9})\.html", url)
        pid = match.group(1) if match else parse_qs(urlparse(url).query).get("pid", [""])[0]
        prepared = url.split("#", 1)[0]
        if prepared != self._product_url:
            # Best Buy limits routes per link, not per color/size JSON request.
            self._attempted_routes = {self._route_key()}
            self._blocked_routes = set()
            self._last_color = ""
            self._color_aliases.clear()
        self._product_url = prepared
        self._product_pid = pid

    @staticmethod
    def _validate_url(url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ("www.prana.com", "prana.com") or parsed.username:
            raise SkipProduct("Đường dẫn đọc prAna không hợp lệ")

    def _check_stop(self):
        if self.should_stop():
            self.clear_wait()
            raise SkipProduct("Đã dừng")

    def _browser_page(self, headers):
        self._check_stop()
        if self._page is not None and not self._page.is_closed():
            return self._page
        if self.page_factory is None:
            raise BrowserLaunchError("Chưa có phiên Camoufox cho prAna")
        self._page = self.page_factory(self.current_proxy())
        self._loaded_pid = ""
        # Cookie headers cannot be set by fetch in a page. Install the site's
        # country/currency cookie in the browser context before opening its PDP.
        cookies = []
        for pair in (headers or {}).get("Cookie", "").split(";"):
            name, separator, value = pair.strip().partition("=")
            if separator and name:
                cookies.append({"name": name, "value": value, "url": "https://www.prana.com/"})
        if cookies:
            self._page.context.add_cookies(cookies)
        self.log("  prAna: dùng một phiên Camoufox cho lượt đọc")
        return self._page

    def _state(self):
        self._check_stop()
        try:
            state = self._page.evaluate(PAGE_STATE)
        except Exception as error:
            return {"challenge": False, "ready": False, "pid": "", "error": type(error).__name__}
        return state if isinstance(state, dict) else {"challenge": False, "ready": False, "pid": ""}

    def _ready(self, state):
        if not state.get("ready") or state.get("challenge"):
            return False
        if not self._product_pid:
            return True
        master = str(state.get("master_pid") or "")
        if master:
            return master == self._product_pid
        pid = str(state.get("pid") or "")
        return pid == self._product_pid or bool(re.fullmatch(
            re.escape(self._product_pid) + r"-[A-Za-z0-9][A-Za-z0-9._-]*", pid))

    def _wait_reason(self, state):
        if state.get("challenge"):
            return "prAna đang chờ xác minh trong cửa sổ Camoufox. Hoàn tất xác minh để tool chạy tiếp."
        if state.get("ready"):
            return "prAna đang chờ đúng trang sản phẩm trong Camoufox."
        return "prAna đang chờ trang sản phẩm tải xong trong Camoufox."

    def _log_wait_state(self, state):
        # An explicit identity in diagnostics distinguishes a loading/wrong-page
        # issue from a visible CAPTCHA without logging cookies or page content.
        self.log(f"  prAna: chờ trang, cần mã {self._product_pid or '?'}; "
                 f"mã cha {str(state.get('master_pid') or '')[:80] or '?'}; "
                 f"mã trang {str(state.get('pid') or '')[:80] or '?'}; "
                 f"xác minh {'có' if state.get('challenge') else 'không'}"
                 + (f"; lỗi đọc trang {state['error']}" if state.get("error") else ""), "warn")

    def _wait_verification(self, state=None):
        state = state if state is not None else self._state()
        reason = None
        try:
            self._page.bring_to_front()
        except Exception:
            pass
        deadline = None
        wait_until = 0.0
        next_alert = 0.0
        load_deadline = time.monotonic() + self.LOAD_TIMEOUT
        while True:
            self._check_stop()
            if state.get("challenge") and self._auto_recovery:
                self._mark_blocked()
                if self._can_change_route():
                    raise _RouteBlocked("phát hiện yêu cầu xác minh trên trang sản phẩm")
                self._auto_recovery = False
            if self._ready(state):
                self.clear_wait()
                self.log("  prAna: trang sản phẩm đã sẵn sàng, tiếp tục đọc")
                return
            now = time.monotonic()
            if state.get("challenge") and deadline is None and self.verification_timeout is not None:
                # Start only after automatic recovery has yielded to the user.
                # Keep this deadline if the challenge temporarily disappears.
                deadline = now + self.verification_timeout
                wait_until = time.time() + self.verification_timeout
            current_reason = self._wait_reason(state)
            if current_reason != reason:
                reason = current_reason
                self.set_wait(reason, wait_until)
                self.log("  " + reason, "warn")
                self._log_wait_state(state)
            if deadline is not None and now >= deadline:
                duration = (f"{self.verification_timeout / 60:g} phút" if self.verification_timeout % 60 == 0
                            else f"{self.verification_timeout:g} giây")
                raise _ManualRequired(f"prAna: hết {duration} chờ xác minh. Đã giữ dòng đang chờ; "
                                      "bấm Chạy tiếp khi sẵn sàng xác minh.", retry_after=0)
            if deadline is None and not state.get("challenge") and now >= load_deadline:
                # Không có ô xác minh mà trang mãi không tải: không chờ vô hạn
                raise SiteWaiting(self._wait_reason(state), retry_after=60)
            if state.get("challenge"):
                load_deadline = now + self.LOAD_TIMEOUT
            if state.get("challenge") and now >= next_alert:
                self.alert()
                next_alert = now + self.ALERT_EVERY
            self._page.wait_for_timeout(500)
            state = self._state()

    def _open_product(self, force=False):
        if not self._product_url:
            raise SkipProduct("Chưa có link sản phẩm để mở phiên prAna")
        if not force and self._loaded_pid == self._product_pid and self._ready(self._state()):
            return
        self._check_stop()
        status = 0
        state = None
        self.navigation_count += 1
        try:
            response = self._page.goto(self._product_url, wait_until="domcontentloaded", timeout=45000)
            status = response.status if response else 0
        except Exception as error:
            self._check_stop()
            if self._route and PROXY_ERR.search(str(error)):
                raise _ProxyBroken() from error
            state = self._state()
            if not self._ready(state) and self._auto_recovery and CONN_ERR.search(str(error)):
                raise _RouteBlocked("kết nối tải trang bị gián đoạn và trang sản phẩm chưa sẵn sàng") from error
            if not state.get("challenge") and not self._ready(state):
                raise SiteWaiting("prAna chưa mở được trang sản phẩm trong Camoufox", retry_after=60) from error
        if status == 404:
            raise SkipProduct("Sản phẩm không còn trên site (404)")
        state = state if state is not None else self._state()
        if status in (403, 429) and not self._ready(state) and self._can_change_route():
            raise _RouteBlocked(f"trang sản phẩm trả HTTP {status} và chưa sẵn sàng")
        if status >= 500 and not state.get("challenge"):
            raise SkipProduct(f"prAna báo lỗi máy chủ HTTP {status}")
        if not self._ready(state):
            self._wait_verification(state)
        self._loaded_pid = self._product_pid

    @staticmethod
    def _color_key(value):
        return " ".join(str(value or "").split()).casefold()

    def _remember_color_aliases(self, data):
        product = data.get("product") if isinstance(data, dict) else None
        if not isinstance(product, dict):
            return
        for option in (attr(product, "color") or {}).get("values") or []:
            key = self._color_key(option.get("value") or option.get("id"))
            if not key:
                continue
            for field in ("value", "id", "displayValue", "description"):
                alias = self._color_key(option.get(field))
                if alias:
                    self._color_aliases[alias] = key
            if not self._last_color and option.get("selected"):
                self._last_color = key

    def _pace_request(self, url):
        parsed = urlparse(url)
        color = ""
        if parsed.path.endswith("/Product-Variation"):
            query = parse_qs(parsed.query)
            pid = query.get("pid", [""])[0]
            color = self._color_key(query.get(f"dwvar_{pid}_color", [""])[0])
        previous = self._color_aliases.get(self._last_color, self._last_color)
        current = self._color_aliases.get(color, color)
        if self.min_gap > 0 and previous and current and previous != current:
            remaining = self._last_request + random.uniform(4.0, 7.0) - time.monotonic()
            self._pause(remaining)
        self._wait_gap()
        self._check_stop()
        self._last_color = color

    def _fetch(self, url, headers):
        self._check_stop()
        self._pace_request(url)
        self.request_count += 1
        allowed_headers = {key: value for key, value in (headers or {}).items()
                           if key.lower() not in ("cookie", "host", "user-agent", "origin", "referer", "content-length")}
        allowed_headers.setdefault("Accept", "application/json, text/javascript, */*; q=0.01")
        allowed_headers.setdefault("X-Requested-With", "XMLHttpRequest")
        try:
            result = self._page.evaluate(FETCH_JSON, {"url": url, "headers": allowed_headers,
                                                    "timeout": int(self.request_timeout * 1000)})
        except Exception as error:
            self._check_stop()
            if self._route and PROXY_ERR.search(str(error)):
                raise _ProxyBroken() from error
            if self._auto_recovery and CONN_ERR.search(str(error)):
                raise _RouteBlocked("kết nối đọc dữ liệu sản phẩm bị gián đoạn") from error
            raise SiteWaiting("prAna chưa đọc được dữ liệu trong phiên Camoufox", retry_after=60) from error
        self._check_stop()
        if not isinstance(result, dict) or not result.get("status"):
            if self._route and isinstance(result, dict) and PROXY_ERR.search(str(result.get("error", ""))):
                raise _ProxyBroken()
            self.log(f"  prAna Camoufox: chưa nhận được phản hồi ở request {self.request_count}", "warn")
            raise SiteWaiting("prAna chưa đọc được dữ liệu trong phiên Camoufox", retry_after=60)
        if result["status"] >= 400:
            self.log(f"  prAna Camoufox: HTTP {result['status']} ở request {self.request_count}", "warn")
        return result

    @staticmethod
    def _blocked_response(result):
        return result["status"] in (403, 429) or is_bot_block(result.get("text", ""))

    def _get_json_once(self, url, headers, renewed_routes):
        self._browser_page(headers)
        self._open_product()
        result = self._fetch(url, headers)
        if result["status"] == 404:
            raise SkipProduct("Sản phẩm không còn trên site (404)")
        route = self._route_key()
        renewed = route in renewed_routes
        if self._blocked_response(result) and not renewed and self._ready(self._state()):
            # A visible product may still have a stale challenge on its JSON
            # endpoint. Renew this session once before discarding a usable PDP.
            renewed_routes.add(route)
            renewed = True
            self.log("  prAna: trang sản phẩm vẫn sẵn sàng nhưng dữ liệu bị chặn; "
                     "mở lại trang và thử đúng dữ liệu một lần trong cùng phiên", "warn")
            self._open_product(force=True)
            result = self._fetch(url, headers)
        if result["status"] == 404:
            raise SkipProduct("Sản phẩm không còn trên site (404)")
        # Exhaustion/no configured proxy retains the same-session manual path.
        if self._blocked_response(result):
            self._mark_blocked()
            if self._can_change_route():
                reason = (f"dữ liệu biến thể trả HTTP {result['status']}" if result["status"] in (403, 429)
                          else "dữ liệu biến thể trả trang xác minh")
                if renewed:
                    reason += " sau khi đã thử lại cùng phiên"
                raise _RouteBlocked(reason)
            if not renewed:
                renewed_routes.add(route)
                self.log("  prAna: dữ liệu yêu cầu xác minh, mở lại trang sản phẩm trong cùng phiên", "warn")
                self._open_product(force=True)
                result = self._fetch(url, headers)
        if self._blocked_response(result):
            raise _ManualRequired("prAna vẫn yêu cầu xác minh trong Camoufox; dòng này đang chờ đọc", retry_after=60)
        if result["status"] == 404:
            raise SkipProduct("Sản phẩm không còn trên site (404)")
        if result["status"] >= 500:
            raise _TransientProductError(f"prAna báo lỗi máy chủ HTTP {result['status']}")
        try:
            data = json.loads(result.get("text", ""))
        except (TypeError, ValueError) as error:
            raise _TransientProductError("prAna chưa trả dữ liệu JSON sản phẩm hợp lệ") from error
        self._remember_color_aliases(data)
        self.clear_wait()
        if self._noted_url != self._product_url:
            self._noted_url = self._product_url
            self.note_ip("ok")
        return data

    def get_json(self, url, headers=None):
        self._validate_url(url)
        if not self._product_url:
            pid = parse_qs(urlparse(url).query).get("pid", [""])[0]
            if not re.fullmatch(r"\d{5,9}", pid):
                raise SkipProduct("Không đọc được mã sản phẩm từ đường dẫn prAna")
            self.prepare_product("https://www.prana.com/on/demandware.store/Sites-Prana_US-Site/en_US/Product-Show?pid=" + pid)
        if not self._attempted_routes:
            self._attempted_routes = {self._route_key()}
        self._auto_recovery = True
        ordinary = 0
        renewed_routes = set()
        try:
            while True:
                self._check_stop()
                try:
                    return self._get_json_once(url, headers, renewed_routes)
                except BrowserLaunchError:
                    raise
                except _RouteBlocked as error:
                    self.log(f"  prAna: đổi đường truy cập vì {error}", "warn")
                    self._mark_blocked()
                    if self._change_route():
                        continue
                    # Another route may have become unavailable while pausing.
                    self._auto_recovery = False
                    if self._page is None:
                        raise SiteWaiting("prAna chưa còn đường truy cập sẵn dùng; giữ dòng này để chạy tiếp", retry_after=60)
                    self.log("  prAna: đã thử hết IP sẵn dùng, giữ Camoufox để chờ xác minh", "warn")
                    self._wait_verification()
                except _ProxyBroken:
                    if self._change_route(broken=True):
                        continue
                    raise SiteWaiting("prAna không còn proxy nào kết nối được; kiểm tra proxy rồi chạy tiếp", retry_after=60)
                except _ManualRequired:
                    raise
                except (SiteWaiting, _TransientProductError):
                    self._check_stop()
                    ordinary += 1
                    if ordinary >= MAX_ORDINARY_ATTEMPTS or not self.pool.proxies:
                        raise
                    self._loaded_pid = ""
                    self.log(f"  prAna: trang chưa tải xong, thử lại cùng IP ({ordinary + 1}/{MAX_ORDINARY_ATTEMPTS})", "warn")
        finally:
            self._auto_recovery = False
