"""Phần dùng chung cho các bộ cào site: tải trang (HTTP / Chrome, có proxy), định dạng dữ liệu sản phẩm.

Mỗi site là 1 module trong thư mục sites/, có:
    KEY = "theory"                      # mã site, dùng trong SKU và tên tab
    NAME = "Theory"                     # tên hiện trên giao diện
    URL_RE = re.compile(...)            # nhận link sản phẩm của site
    NEEDS_PROXY = True/False            # True = IP Việt Nam bị chặn, phải đi qua proxy Mỹ
    def product_code(url) -> str|None   # mã sản phẩm đọc thẳng từ link (để bắt trùng trước khi tải), không có thì None
    def scrape(f: Fetcher, url) -> dict # trả về dict theo PRODUCT_SCHEMA bên dưới

PRODUCT_SCHEMA (mọi giá là USD, số thực):
{
  "site": "theory",
  "code": "Q0801208",            # mã sản phẩm gốc (mẫu cha, chung mọi màu/size) -> dùng làm SKU
  "url": "https://...",          # link sản phẩm gốc (canonical)
  "title": "Marcee Cropped Pant in Sleek Flannel",   # tên sản phẩm KHÔNG kèm tên hãng
  "brand": "Theory",             # chỉ để tham khảo, listing để Unbranded
  "bullets": ["...", "..."],     # mô tả: mỗi phần tử 1 đoạn/1 ý, chữ thuần (không HTML), đã bỏ tên hãng
  "details": {"Material": "...", "Care": "...", "Fit": "..."},   # thông số nếu có (tên -> giá trị chữ)
  "breadcrumbs": ["Women", "Pants"],
  "department": "Women",         # Women | Men | Girls | Boys | Baby Girls | Baby Boys | Unisex Adults | Unisex Kids | "" (đồ nhà)
  "size_name": "Size",           # tên trục size trên site (vd "Size", "Bed Size"); "" nếu sản phẩm không có size
  "colors": [                    # thứ tự như trên site; sản phẩm không có màu -> [] và variant.color = ""
     {"name": "Black", "images": ["https://...jpg", ...]}   # ảnh lớn nhất lấy được của màu đó
  ],
  "images": ["https://..."],     # ảnh chung (khi site không tách ảnh theo màu); có ảnh theo màu thì có thể để []
  "variants": [                  # mỗi tổ hợp màu × size bán trên site
     {"color": "Black", "size": "M", "price": 275.0, "list_price": 325.0 hoặc None,
      "available": True, "id": "mã biến thể trên site"}
  ],
}
"""
import json
import re
import threading
import time
from pathlib import Path


class SkipProduct(Exception):
    """Link này bỏ qua (lý do trong message), các link khác chạy tiếp."""


class Blocked(Exception):
    """Site chặn (403 / trang xác minh) — đổi proxy rồi thử lại."""


class ServerError(Exception):
    """Site trả lỗi 5xx (thường do link sai / sản phẩm đã gỡ) — thử lại vài lần rồi bỏ qua."""


class BrowserLaunchError(Exception):
    """Không mở được trình duyệt (Camoufox / Chrome) — lỗi máy mình, không phải site chặn: không đổi proxy."""


BLOCK_MARKERS = (
    "Access to this page has been denied",   # PerimeterX
    "<TITLE>Access Denied</TITLE>",           # Akamai
    "Just a moment...",                       # Cloudflare
    "awsWafCookieDomainList",                 # AWS WAF
    "The request could not be satisfied",     # CloudFront chặn vùng
)


def is_blocked(text):
    head = text[:20000]
    return any(m in head for m in BLOCK_MARKERS)


def load_proxies(path):
    """File proxy: mỗi dòng ip:port:user:pass (hoặc ip:port). Trả về list dict {server, username, password}."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    return parse_proxy_lines(lines)


def parse_proxy_line(line):
    """1 dòng ip:port:user:pass (hoặc ip:port) -> dict {server, username, password}; sai dạng -> None."""
    parts = line.strip().split(":")
    if len(parts) == 4 and all(parts) and parts[1].isdigit():
        return {"server": f"http://{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]}
    if len(parts) == 2 and all(parts) and parts[1].isdigit():
        return {"server": f"http://{parts[0]}:{parts[1]}"}
    return None


def parse_proxy_lines(lines):
    return [p for p in (parse_proxy_line(x) for x in lines) if p]


def proxy_url(p):
    if not p:
        return None
    host = p["server"].split("://", 1)[1]
    return f"http://{p['username']}:{p['password']}@{host}" if p.get("username") else f"http://{host}"


class ProxyPool:
    """Xoay vòng proxy: proxy lỗi/bị chặn thì chuyển sang proxy kế tiếp."""

    SUBNET_COOLDOWN = 1800   # dải mạng vừa bị site chặn: 30 phút không lấy proxy cùng dải

    def __init__(self, proxies):
        self.proxies = list(proxies)
        self.i = 0
        self.lock = threading.Lock()
        self.cooldown = {}   # dải mạng (2 số đầu IP) -> thời điểm được dùng lại

    def current(self):
        with self.lock:
            return self.proxies[self.i % len(self.proxies)] if self.proxies else None

    def rotate(self, blocked=False):
        """Sang proxy kế tiếp. blocked=True: proxy hiện tại vừa bị site chặn -> tránh cả dải mạng đó một lúc, vì site
        thường chặn nguyên dải (29/10: 5 proxy 161.77.x.x liền nhau đều bị Best Buy ngắt ngay, chưa đọc được link nào)."""
        with self.lock:
            n = len(self.proxies)
            if not n:
                return None
            now = time.time()
            if blocked:
                self.cooldown[self._subnet(self.proxies[self.i % n])] = now + self.SUBNET_COOLDOWN
            for step in range(1, n + 1):
                j = (self.i + step) % n
                if self.cooldown.get(self._subnet(self.proxies[j]), 0) <= now:
                    self.i = j
                    return self.current_unlocked()
            self.i = (self.i + 1) % n   # dải nào cũng vừa bị chặn: đành lấy proxy kế tiếp
            return self.current_unlocked()

    def drop_current(self):
        """Proxy hỏng (từ chối kết nối): bỏ khỏi lần chạy này. Trả về proxy kế tiếp, None nếu hết proxy."""
        with self.lock:
            if not self.proxies:
                return None
            self.proxies.pop(self.i % len(self.proxies))
            if not self.proxies:
                return None
            self.i %= len(self.proxies)
            return self.current_unlocked()

    @staticmethod
    def _subnet(p):
        host = p["server"].split("://")[-1].split("@")[-1].split(":")[0]
        return ".".join(host.split(".")[:2])

    def current_unlocked(self):
        return self.proxies[self.i % len(self.proxies)] if self.proxies else None

    def label(self):
        p = self.current()
        return p["server"].split("://", 1)[1] if p else "không proxy"


class Fetcher:
    """Tải trang cho bộ cào. use_proxy=True thì mọi request đi qua ProxyPool.

    get(url) / get_json(url): HTTP bằng curl_cffi giả Chrome (nhanh, không mở trình duyệt).
    page_factory: Chrome thật (Playwright) cho site cần qua bước chống bot (vd Touch of Class).
    """

    def __init__(self, use_proxy=False, pool=None, page_factory=None, log=None, should_stop=None, tries=4):
        self.use_proxy = use_proxy           # True = luôn đi qua proxy ngay từ đầu; False = IP gốc, bị chặn tự chuyển
        self.switched = False                # đã tự chuyển sang proxy trong lần chạy này
        self.pool = pool or ProxyPool([])
        self.page_factory = page_factory   # hàm (proxy|None) -> Playwright page; engine cấp
        self.log = log or (lambda *a, **k: None)
        self.should_stop = should_stop or (lambda: False)
        self.tries = tries
        self._switch_lock = threading.Lock()   # nhiều luồng cùng bị chặn -> chỉ chuyển (và báo) 1 lần
        self.ip_stats = {}   # IP -> {"ok": số link đọc được, "blocked": số lần bị chặn} (log cuối lần chạy)

    @property
    def proxy_on(self):
        return self.use_proxy or self.switched

    def ip_label(self):
        return self.pool.label() if self.proxy_on else "IP gốc"

    def note_ip(self, result):
        """Đếm theo IP đang dùng: result = "ok" (đọc được 1 link) hoặc "blocked" (bị site chặn 1 lần)."""
        d = self.ip_stats.setdefault(self.ip_label(), {"ok": 0, "blocked": 0})
        d[result] += 1

    def ip_summary(self):
        """vd "IP gốc 50 link (bị chặn 1 lần) · 103.180.152.57:4222 11 link · 103.177.109.182:34797 bị chặn 2 lần"."""
        parts = []
        for ip, d in self.ip_stats.items():
            s = f"{ip} {d['ok']} link" if d["ok"] else ip
            if d["blocked"]:
                s += f" (bị chặn {d['blocked']} lần)" if d["ok"] else f" bị chặn {d['blocked']} lần"
            parts.append(s)
        return " · ".join(parts)

    def current_proxy(self):
        """Proxy đang dùng (dict cho Playwright) hoặc None nếu đang chạy IP gốc."""
        return self.pool.current() if self.proxy_on else None

    def switch_to_proxy(self, why=""):
        """IP gốc bị chặn -> dùng proxy cho các request còn lại của lần chạy. Trả về False nếu không có proxy."""
        with self._switch_lock:
            if self.proxy_on:
                return True   # luồng khác vừa chuyển rồi: dùng luôn proxy
            if not self.pool.proxies:
                return False
            self.switched = True
        self.log(f"  ⇄ IP gốc bị chặn{(' (' + why + ')') if why else ''} — tự chuyển sang proxy {self.pool.label()} "
                 "cho các link còn lại", "warn")
        return True

    def _proxies(self):
        if not self.proxy_on:
            return None
        u = proxy_url(self.pool.current())
        return {"http": u, "https": u} if u else None

    def get(self, url, headers=None, allow_404=False):
        from curl_cffi import requests

        last = None
        attempt, tries = 0, self.tries
        while attempt < tries:
            attempt += 1
            # Có proxy: thử nhiều proxy hơn (vài proxy liền nhau có thể cùng bị site chặn)
            tries = max(self.tries, min(8, len(self.pool.proxies))) if self.proxy_on else self.tries
            if self.should_stop():
                raise SkipProduct("Đã dừng")
            try:
                h = {"Accept-Language": "en-US,en;q=0.9"}
                h.update(headers or {})
                r = requests.get(url, impersonate="chrome", timeout=60, proxies=self._proxies(), headers=h)
                if r.status_code == 404 and allow_404:
                    return None
                if r.status_code == 404:
                    raise SkipProduct("Sản phẩm không còn trên site (404)")
                if r.status_code in (403, 429) or is_blocked(r.text):
                    raise Blocked(f"HTTP {r.status_code}")
                if r.status_code >= 500:
                    raise ServerError(f"HTTP {r.status_code}")
                return r.text
            except SkipProduct:
                raise
            except Exception as e:
                last = e
                if isinstance(e, Blocked) and not self.proxy_on:
                    # IP gốc bị chặn -> chuyển ngay sang proxy, thử lại từ đầu
                    if self.switch_to_proxy(str(e)):
                        attempt = 0
                        continue
                    if attempt >= 2:
                        break   # không có proxy: thử thêm chỉ làm site chặn lâu hơn
                if self.proxy_on and self.pool.proxies:
                    self.pool.rotate()
                    self.log(f"  ⚠ {type(e).__name__}: {e} — đổi sang proxy {self.pool.label()}", "warn")
                else:
                    self.log(f"  ⚠ {type(e).__name__}: {e} — thử lại", "warn")
                time.sleep(2 + attempt * 2)
        if isinstance(last, ServerError):
            raise SkipProduct(f"Site báo lỗi ({last}) — link có thể sai hoặc sản phẩm đã gỡ; mở link kiểm tra lại")
        if isinstance(last, Blocked):
            hint = " — các proxy thử đều bị chặn" if self.proxy_on else " — chưa có proxy nào để tự chuyển (dán proxy trong Cài đặt)"
            raise SkipProduct(f"Site chặn truy cập ({last}){hint}")
        raise SkipProduct(f"Không tải được trang: {last}")

    def get_json(self, url, headers=None):
        h = {"Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest"}
        h.update(headers or {})
        text = self.get(url, headers=h)
        try:
            return json.loads(text)
        except ValueError:
            raise SkipProduct(f"Site không trả về JSON: {text[:120]!r}")


# ---------------------------------------------------------------- tiện ích cho bộ cào

def unescape_all(s):
    """Giải mã HTML tới khi hết (site hay mã hoá 2 lần: "&amp;nbsp;" -> "&nbsp;" -> " "), \xa0 thành dấu cách."""
    import html as _html

    s = s or ""
    for _ in range(3):
        u = _html.unescape(s)
        if u == s:
            break
        s = u
    return s.replace("\xa0", " ")


# eBay cấm link ra site khác trong mô tả / thông số: bỏ cả thẻ link (kèm chữ của link), địa chỉ web, tên miền
_LINK_TAG = re.compile(r"(?is)<\s*(hyperlink|a)\b[^>]*>.*?<\s*/\s*\1\s*>|<\s*(?:hyperlink|a)\b[^>]*/?>")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|co|io|us|shop|store)\b(?:/\S*)?")


def strip_links(s):
    """Bỏ link / địa chỉ web khỏi 1 đoạn chữ của site (vd '<hyperlink target="https://...">Glossary</hyperlink>')."""
    s = unescape_all(str(s or ""))
    if not re.search(r"(?i)<|https?:|www\.|\.(?:com|net|org|co|io|us|shop|store)\b", s):
        return s
    s = _LINK_TAG.sub(" ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _URL.sub(" ", s)
    s = re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s{2,}", " ", s))
    return s.strip(" |-–—").strip()


def clean_text(s):
    s = unescape_all(s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t\r\f\v]+", " ", s).strip()


def html_to_lines(fragment):
    """Đoạn HTML mô tả -> list dòng chữ (mỗi <p>, <li>, <br> 1 dòng), bỏ dòng trống."""
    s = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h\d|/tr)\s*/?>", "\n", fragment or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape_all(s)
    return [re.sub(r"\s+", " ", x).strip(" •-•") for x in s.splitlines() if x.strip(" •-•\t ")]


def ld_json_blocks(html):
    """Mọi khối <script type=application/ld+json> đã parse (bỏ khối lỗi). Khối dạng list/@graph được trải phẳng."""
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        out.extend(x for x in items if isinstance(x, dict))
    return out


def find_ld(html, *types):
    for b in ld_json_blocks(html):
        t = b.get("@type")
        t = t if isinstance(t, list) else [t]
        if any(x in types for x in t):
            return b
    return None


def to_price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(v))
    return float(m.group(0).replace(",", "")) if m else None


def strip_brand(text, *brands):
    """Bỏ tên hãng khỏi chữ (listing để Unbranded). Bỏ cả cụm đi kèm ("designed by X", "in partnership with X",
    "from X") để không còn câu cụt kiểu "Outdoor Speakers by ."."""
    for b in brands:
        if b:
            eb = re.escape(b)
            text = re.sub(rf"(?i)\s*\b(?:(?:designed|made|crafted|created|exclusively)\s+)?by\s+{eb}\b[’'`]?s?", "", text)
            text = re.sub(rf"(?i)\s*\b(?:in partnership|in collaboration|together)\s+with\s+{eb}\b", "", text)
            text = re.sub(rf"(?i)\b{eb}\b[’'`]?s?\s*", "", text)
    text = re.sub(r"\s+([,.;:!?)])", r"\1", text)   # khoảng trắng thừa trước dấu câu sau khi bỏ tên
    text = re.sub(r"\(\s*\)", "", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -|,")


def strip_brand_desc(text, *brands):
    """Mô tả / thông số: GIỮ tên hãng (chốt 14/10: tiêu đề đã có hãng nên mô tả cũng giữ, không bị câu cụt
    kiểu "designed by Sanus in partnership with ."). Chỉ dọn khoảng trắng; brands giữ lại cho đồng bộ cách gọi."""
    return re.sub(r"\s{2,}", " ", text or "").strip()


def dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def validate(prod):
    """Kiểm tra dict sản phẩm trước khi trả về; lỗi thì SkipProduct với lý do rõ ràng."""
    for k in ("site", "code", "url", "title", "variants"):
        if not prod.get(k):
            raise SkipProduct(f"Không đọc được {k} của sản phẩm")
    # Biến thể không có giá (thường là màu / size đã hết, site không ghi giá) -> bỏ riêng biến thể đó,
    # không bỏ cả sản phẩm
    priced = [v for v in prod["variants"] if v.get("price")]
    if not priced:
        raise SkipProduct("Không đọc được giá của biến thể nào")
    prod["variants"] = priced
    if prod.get("colors"):   # màu không còn biến thể nào (bị bỏ vì không có giá) -> bỏ khỏi danh sách màu
        left = {v.get("color") for v in priced}
        prod["colors"] = [c for c in prod["colors"] if c.get("name") in left]
    if not any(v.get("available") for v in prod["variants"]):
        raise SkipProduct("Hết hàng toàn bộ size/màu")
    if not (prod.get("images") or any(c.get("images") for c in prod.get("colors", []))):
        raise SkipProduct("Không tìm thấy ảnh sản phẩm")
    prod["bullets"] = [x for x in (strip_links(b) for b in prod.get("bullets") or []) if x]
    prod["details"] = {k: v for k, v in ((strip_links(k), strip_links(v)) for k, v in (prod.get("details") or {}).items())
                       if k and v}
    prod.setdefault("breadcrumbs", [])
    prod.setdefault("department", "")
    prod.setdefault("size_name", "Size")
    prod.setdefault("colors", [])
    prod.setdefault("images", [])
    prod.setdefault("brand", "")
    return prod
