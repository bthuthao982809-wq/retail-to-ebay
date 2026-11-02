"""Best Buy (bestbuy.com) — mở trực tiếp bằng Camoufox (Firefox chống nhận diện bot), script đọc trang web/bestbuy_capture.js.

Chạy IP gốc trước; bị ngắt kết nối thì chuyển sang proxy đã cấu hình, proxy cũng bị ngắt thì xoay sang proxy kế tiếp.
Không mở được trình duyệt (lỗi máy mình) thì chỉ báo lỗi dòng đó, không đổi proxy.

Mỗi link /product/<tên>/<BSIN> là 1 mã hàng (SKU) = 1 màu; màu khác là link khác -> mỗi link 1 listing, không có size.
Dữ liệu đọc từ trang: ld+json Product (name "Hãng - Tên - Màu", brand, color, model, sku, offers nhiều người bán:
chỉ lấy ưu đãi Mới do chính "Best Buy" bán), ảnh thư viện (bỏ phần ";maxHeight=..." để lấy ảnh gốc), tính năng, bảng thông số
(Key Specs / Dimensions / Features), nút mua, ô Shipping / Pickup (chỉ quan tâm Shipping).
"""
import re
import time

from sites.base import BrowserLaunchError, SkipProduct, clean_text, dedup, strip_brand, validate
from sites.sfcc import make_bullets, titlecase

KEY = "bestbuy"
NAME = "Best Buy"
URL_RE = re.compile(r"https?://(?:www\.)?bestbuy\.com/(?:product/[^/?#]+/[A-Z0-9]{8,12}|site/[^?#]*\d{7}\.p)", re.I)
NEEDS_PROXY = False
EXAMPLES = ["https://www.bestbuy.com/product/shark-cordless-pet-cordless-stick-vacuum-with-xl-dust-cup-and-led-headlights-"
            "up-to-40-min-runtime-moonlight-blue/JXJVXLX9LF"]

from core import ASSETS

SECTIONS = ("Key Specs", "General", "Dimensions", "Features", "Certifications Listings & Approvals", "Warranty", "Other",
            "Included", "Power", "Performance", "Connectivity", "Compatibility", "Display", "Audio")
SPEC_SECTIONS = ("Key Specs", "Dimensions", "Features")          # đưa vào mô tả
SKIP_SPEC = re.compile(r"(?i)^(upc|bsin|sku|model number|product name|brand|color|warranty.*)$")
MAX_GALLERY = 24
CONN_ERR = re.compile(r"NS_ERROR_NET|NS_ERROR_CONNECTION|ERR_CONNECTION|ERR_TIMED_OUT|Timeout \d+ms", re.I)   # site ngắt kết nối
PROXY_ERR = re.compile(r"NS_ERROR_PROXY|PROXY_CONNECTION|ERR_PROXY", re.I)   # proxy hỏng / từ chối kết nối, không phải site chặn
BLOCK_PAUSE = 30   # bị chặn: nghỉ rồi mới thử IP khác — 30/10 đổi ngay trong 1 giây thì IP kế tiếp hay bị chặn theo
MAX_IPS = 8        # 1 link bị chặn: thử tối đa bấy nhiêu IP (IP gốc + proxy) rồi mới bỏ qua
BLOCK_PAGE = "trang chặn"   # dấu trong lý do lỗi: site trả trang "Access Denied" / xác minh thay vì trang sản phẩm
# Trang chặn của Akamai (Best Buy dùng) / trang xác minh: nhận ở tiêu đề + 1500 ký tự đầu, báo ngay thay vì chờ 20 giây
BLOCK_JS = r"""() => {
  const t = (document.title || '') + ' ' + ((document.body && document.body.innerText) || '').slice(0, 1500);
  const m = t.match(/access denied|pardon our interruption|verify you are (a )?human|are you a (ro)?bot|unusual traffic|reference #\s*\d|request blocked|too many requests|request unsuccessful/i);
  return m ? m[0] : '';
}"""


def product_code(url):
    m = re.search(r"/product/[^/?#]+/([A-Z0-9]{8,12})(?:[/?#]|$)", url, re.I) or re.search(r"/(\d{7})\.p\b", url)
    return m.group(1).upper() if m else None


def _specs(text):
    """Chữ bảng thông số -> {mục: [(tên, giá trị)]}. Đầu bảng là dãy tên mục (tab), sau đó từng mục: tên / giá trị xen kẽ."""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    i = 0
    while i < len(lines) and (lines[i] in SECTIONS or lines[i] == "Specifications"):
        i += 1
    out, sec = {}, "Key Specs"
    while i < len(lines):
        ln = lines[i]
        if re.match(r"(?i)did we get something wrong|report missing", ln):
            break
        if ln in SECTIONS:
            sec = ln
            i += 1
            continue
        if i + 1 < len(lines):
            out.setdefault(sec, []).append((ln, lines[i + 1]))
        i += 2
    return out


def _capture_with_browser(f, url, code):
    """Mở trang bằng Camoufox, chạy script đọc trang -> dict dữ liệu (None nếu trang không có sản phẩm)."""
    started = time.monotonic()
    page = f.page_factory(f.current_proxy())
    # DOMContentLoaded có thể bị script/phần quảng cáo giữ rất lâu dù sản phẩm
    # đã hiện. Chỉ đợi tài liệu mới bắt đầu, sau đó chờ đúng dữ liệu cần đọc.
    from playwright.sync_api import TimeoutError as PageTimeout
    try:
        page.goto(url, wait_until="commit", timeout=20000)
    except PageTimeout:
        if product_code(page.url) != code:
            raise
    ready = r"""() => {
      const flat = x => Array.isArray(x) ? x.flatMap(flat) :
        x && typeof x === 'object' ? [x, ...flat(x['@graph'] || [])] : [];
      const ld = [...document.querySelectorAll('script[type="application/ld+json"]')]
        .flatMap(s => {try {return flat(JSON.parse(s.textContent));} catch (_) {return [];}});
      return !!document.querySelector('h1') && ld.some(p => [p['@type']].flat().includes('Product')
        && p.name && p.offers && p.image);
    }"""
    for _ in range(40):
        if f.should_stop():
            raise SkipProduct("Đã dừng")
        if page.evaluate(ready):
            break
        hit = page.evaluate(BLOCK_JS)
        if hit:
            raise SkipProduct(f"{BLOCK_PAGE} «{hit}»")
        page.wait_for_timeout(500)
    else:
        raise SkipProduct("Best Buy chưa tải đủ tên, giá và ảnh sản phẩm")
    data = page.evaluate((ASSETS / "web" / "bestbuy_capture.js").read_text(encoding="utf-8"))
    if not data or not data.get("prod"):
        return None
    if product_code(data.get("url", "")) != code:
        raise SkipProduct("Best Buy chuyển sang trang khác mã sản phẩm")
    data["bsin"] = code
    f.log(f"  Đọc trang Best Buy: {time.monotonic() - started:.1f}s")
    return data


def _pause(f, secs):
    """Nghỉ secs giây, bấm Dừng thì thôi ngay."""
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if f.should_stop():
            raise SkipProduct("Đã dừng")
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))


def _load(f, url):
    f.bestbuy_data_live = False
    code = product_code(url)
    if not code:
        raise SkipProduct("Không đọc được mã sản phẩm từ link Best Buy")
    if not getattr(f, "page_factory", None):
        raise SkipProduct("Best Buy cần mở bằng trình duyệt Camoufox (engine chưa cấp trình duyệt)")
    # Lỗi thường (trang chưa tải đủ…): tối đa 3 lượt. Bị chặn: thử lần lượt mọi IP còn lại (tối đa MAX_IPS) — trước đây
    # cũng chỉ 3 lượt nên 30/10 link 51 bị bỏ qua sau IP gốc + 2 proxy, dù còn proxy khác vào được.
    reason, attempts, blocked_ips = "", 0, 0
    home = 0 if f.proxy_on else 1   # link bắt đầu ở IP gốc thì IP gốc cũng tính là 1 IP đã thử
    while True:
        if f.should_stop():
            raise SkipProduct("Đã dừng")
        try:
            data = _capture_with_browser(f, url, code)
            if data:
                f.bestbuy_data_live = True
                f.note_ip("ok")
                return data
            reason = "không thấy dữ liệu sản phẩm"
        except BrowserLaunchError as exc:
            # _Browser đã thử phục hồi cùng sản phẩm bằng driver mới và cùng proxy.
            # Hết lượt khởi động mới báo lỗi; tuyệt đối không tính thành IP bị chặn.
            f.log(f"  Camoufox: không mở được trình duyệt ({exc})", "warn")
            raise SkipProduct(f"Không mở được trình duyệt Camoufox ({exc}) — bấm ↻ Chạy lại dòng lỗi")
        except Exception as exc:
            if f.should_stop():
                raise SkipProduct("Đã dừng")
            reason = (str(exc).splitlines() or [""])[0].lstrip(": ").strip()[:220] or type(exc).__name__
        f.log(f"  Camoufox: {reason}", "warn")
        if f.proxy_on and PROXY_ERR.search(reason):
            # Proxy hỏng (từ chối kết nối), không phải site chặn: bỏ proxy đó, thử ngay proxy khác, không tính lượt
            # (29/10: proxy 161.77.140.130 chết, tool thử đúng nó 3 lần cho từng link còn lại)
            if f.pool.drop_current() is None:
                reason = "không còn proxy nào kết nối được"
                break
            f.log(f"  ⇄ Proxy không kết nối được — bỏ, dùng proxy {f.pool.label()} (còn {len(f.pool.proxies)})", "warn")
            continue
        if CONN_ERR.search(reason) or BLOCK_PAGE in reason:   # ngắt kết nối hoặc trả trang chặn
            f.note_ip("blocked")
            blocked_ips += 1
            if blocked_ips >= min(MAX_IPS, home + len(f.pool.proxies)):
                break   # đã thử hết IP (hoặc đủ MAX_IPS) cho link này
            if not f.proxy_on:
                if not f.switch_to_proxy("Best Buy ngắt kết nối" if BLOCK_PAGE not in reason else "Best Buy trả trang chặn"):
                    break
            else:
                # Proxy đang dùng cũng bị chặn -> proxy ở dải mạng khác (site chặn nguyên dải: 5 proxy 161.77.x.x cùng bị ngắt)
                f.pool.rotate(blocked=True)
                f.log(f"  ⇄ Proxy bị chặn — đổi sang proxy {f.pool.label()} (ưu tiên dải mạng khác)", "warn")
            if BLOCK_PAUSE:
                f.log(f"  … nghỉ {BLOCK_PAUSE} giây rồi mới mở bằng IP mới (đổi ngay thì IP mới hay bị chặn theo)")
                _pause(f, BLOCK_PAUSE)
            continue
        attempts += 1
        if attempts >= 3:
            break
        if not f.proxy_on and not f.switch_to_proxy("Best Buy ngắt kết nối"):
            break
    # Chữ "chặn" để engine tự dừng sau 2 link liền (không dồn tiếp vào IP đang bị chặn)
    if "không còn proxy" in reason:
        raise SkipProduct(f"Best Buy đang chặn và {reason} — kiểm tra lại proxy, đợi 30–60 phút rồi bấm Chạy tiếp")
    if CONN_ERR.search(reason) or BLOCK_PAGE in reason:
        raise SkipProduct(f"Best Buy đang chặn ({reason}, đã thử {blocked_ips} IP) — đợi 30–60 phút rồi bấm Chạy tiếp")
    raise SkipProduct(f"Camoufox không lấy được trang {code} ({reason}) — bấm ↻ Chạy lại dòng lỗi")


def _features(lines, truncated):
    """Tính năng: "TÊN IN HOA:" rồi đoạn chữ -> "Tên: đoạn chữ". Site cắt cụt mục cuối (có nút See all features) -> bỏ mục đó."""
    lines = [clean_text(x) for x in lines or [] if clean_text(x)]
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if re.fullmatch(r"[A-Z0-9][A-Z0-9 ,&'/™®.\-+]*:?", ln) and i + 1 < len(lines) and not re.fullmatch(r"[A-Z0-9 ,&'/™®.\-+:]+", lines[i + 1]):
            out.append(f"{titlecase(ln.rstrip(':'))}: {lines[i + 1]}")
            i += 2
        else:
            out.append(ln)
            i += 1
    if truncated and out and not out[-1].endswith((".", "!", ")")):
        out.pop()
    return out


def _best_offer(prod):
    """Ưu đãi Mới do chính Best Buy bán (marketplace giá cao hơn nhiều, Open-Box / Refurbished không phải hàng mới)."""
    offers = prod.get("offers") or []
    offers = [offers] if isinstance(offers, dict) else offers
    mine = [o for o in offers if str((o.get("seller") or {}).get("name") or "").strip().lower() == "best buy"]
    new = [o for o in mine if str(o.get("description") or "").strip().lower() == "new"]
    return new[0] if new else None


def scrape(f, url):
    data = _load(f, url)
    prod = data["prod"]
    offer = _best_offer(prod)
    if not offer:
        raise SkipProduct("Best Buy không bán hàng mới (chỉ có hàng Open-Box / Refurbished hoặc người bán khác)")
    price = offer.get("price")
    try:
        price = float(price)
    except (TypeError, ValueError):
        raise SkipProduct("Không đọc được giá của Best Buy")
    avail = str(offer.get("availability") or "").rsplit("/", 1)[-1].lower() in ("instock", "limitedavailability", "onlineonly")
    buttons = data.get("buy") or []
    if buttons and not any(re.search(r"(?i)add to cart", b["t"]) and not b["d"] for b in buttons):
        avail = False   # trang chỉ còn nút "Sold Out" / "Check Stores"
    # Chỉ quan tâm Shipping: Pickup còn hay hết ở cửa hàng không liên quan. Ô Shipping báo không giao được -> hết hàng
    ship = [s for s in data.get("ship") or [] if re.sub(r"(?i)^shipping\W*", "", s)]   # bỏ nhãn trống "Shipping"
    if ship and all(re.search(r"(?i)unavailable|sold out|not available|out of stock|not eligible", s) for s in ship):
        avail = False

    brand = clean_text((prod.get("brand") or {}).get("name") if isinstance(prod.get("brand"), dict) else prod.get("brand") or "")
    color = titlecase(clean_text(prod.get("color") or ""))
    model = clean_text(prod.get("model") or "")
    name = clean_text(prod.get("name") or data.get("h1") or "")
    if brand:
        name = re.sub(rf"(?i)^{re.escape(brand)}\s*[-–:]\s*", "", name)
    if color:   # "… - Moonlight Blue" / "… silver/purple": màu là phần phân biệt của link, nằm ở ô Color
        name = re.sub(rf"(?i)\s*(?:[-–]\s*)?{re.escape(color)}\s*$", "", name)
    title = strip_brand(name, brand) if brand else name
    title = re.sub(r"\s+-\s+", ", ", title)

    specs = _specs(data.get("specs_text"))
    flat = {k: v for sec in specs.values() for k, v in sec}
    spec_lines, seen = [], set()
    for sec in SPEC_SECTIONS:
        for k, v in specs.get(sec, []):
            if SKIP_SPEC.match(k) or k.lower() in seen:
                continue
            seen.add(k.lower())
            spec_lines.append(f"{k}: {v}")
    feats = _features(data.get("feat"), data.get("more"))
    desc = clean_text(prod.get("description") or "")
    bullets = make_bullets([desc] + feats + spec_lines, (brand,) if brand else ())

    details = {}
    if model or flat.get("Model Number"):
        details["Model"] = model or flat["Model Number"]
    if color:
        details["Color"] = color
    if flat.get("Vacuum Type"):
        details["Type"] = flat["Vacuum Type"]
    for k in ("Voltage", "Wattage"):
        if flat.get(k):
            details[k] = flat[k]

    images = dedup(data.get("gal") or [])[:MAX_GALLERY]
    if not images and prod.get("image"):
        img = prod["image"]
        img = img if isinstance(img, list) else [img]
        images = dedup((i.get("url") if isinstance(i, dict) else i) for i in img)
    code = product_code(url) or clean_text(str(prod.get("sku") or ""))
    result = {
        "site": KEY, "code": code,
        "url": url.split("?")[0].split("#")[0], "title": title, "brand": brand, "bullets": bullets, "details": details,
        "breadcrumbs": [c for c in (data.get("crumbs") or []) if c and c.lower() != "best buy"], "department": "",
        "size_name": "", "colors": [], "images": images,
        "variants": [{"color": "", "size": "", "price": price, "list_price": None, "available": avail,
                      "id": str(prod.get("sku") or code)}],
        "condition": "new",
    }
    return validate(result)
