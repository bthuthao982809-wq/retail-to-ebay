"""Touch of Class (touchofclass.com) — ASP.NET (DMI), sau CloudFront + AWS WAF.

HTTP thường bị trả 202 (trang thử thách JavaScript của AWS WAF). Cách làm:
  1. Lần đầu (hoặc khi token hết hạn): mở trang sản phẩm bằng Chrome thật (page_factory của engine) -> trình duyệt tự
     giải thử thách -> lấy cookie aws-waf-token từ page.context.cookies() (đồng thời dùng luôn HTML trang đó).
  2. Các request sau: HTTP (curl_cffi) kèm header Cookie: aws-waf-token=... — token dùng được cả khi đổi proxy khác.
  3. Giá/tồn kho theo từng lựa chọn: DmiAjaxSecure.aspx?request=ProductCollectionAJAX&attributetype=Buttons&p=<ITEM>
     &csa=<thuộc tính vừa đổi>&csav=<giá trị>&a1=<thuộc tính 1>&av1=<giá trị 1>&a2=...  (trả khối HTML của sản phẩm
     đã chọn: productprice / productOriginalPrice, productSkuNormalized, link schema.org availability, ảnh theo màu).
Trang /p/<STYLE>-<NNN>/: <NNN> là MÓN trong bộ sưu tập (vd -025 bộ chăn, -002 vỏ gối), KHÔNG phải màu.
Màu (nếu có) là nhóm lựa chọn "Color" trong chính món đó; size là nhóm "Size" (và các nhóm khác như Length).
Ảnh: www.touchofclass.com/images/xxl/<ITEM>.jpg (2000x2000) — chặn IP Việt Nam (403), tải qua proxy Mỹ (không cần cookie).
"""
import html as _html
import itertools
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sites.base import SkipProduct, clean_text, dedup, html_to_lines, strip_brand, strip_brand_desc, to_price

KEY = "touchofclass"
NAME = "Touch of Class"
URL_RE = re.compile(r"https?://(?:www\.)?touchofclass\.com/[^?#]*/p/[A-Z0-9]+-[A-Z0-9]+", re.I)
NEEDS_PROXY = True   # IP Việt Nam bị chặn

SITE = "https://www.touchofclass.com"
BRAND = "Touch of Class"
AJAX = SITE + "/DmiAjaxSecure.aspx?request=ProductCollectionAJAX&attributetype=Buttons"
MAX_COMBOS = 60      # tối đa số tổ hợp lựa chọn gọi AJAX cho 1 sản phẩm
WORKERS = 4
TOKEN_TTL = 20 * 60  # dùng lại token tối đa 20 phút rồi lấy lại
ITEM_RE = re.compile(r"/p/([A-Z0-9]+-[A-Z0-9]+)", re.I)
COLOR_AXIS = re.compile(r"(?i)^colou?r")
NOT_AVAIL = re.compile(r"(?i)no longer available|out of stock|sold out|unavailable|discontinued")
AVAIL_TEXT = re.compile(r"(?i)available and ready|ready to ship|in stock|order now for shipment|ships? (in|within|by)"
                        r"|only \d+ left|hurry|pre-?order|back-?order")

_token = {"cookie": "", "t": 0.0}
_token_lock = threading.Lock()


def product_code(url):
    m = ITEM_RE.search(url)
    return m.group(1).upper() if m else None


# ---------------------------------------------------------------- lấy token AWS WAF bằng Chrome

GEO_BLOCK = ("The request could not be satisfied", "Request blocked", "Access Denied")


def _chrome_page(f, url, timeout=90):
    """Mở url bằng Chrome thật, chờ qua thử thách WAF; trả về (html, chuỗi cookie aws-waf-token).
    Chạy IP gốc trước; CloudFront chặn theo vùng (IP Việt Nam) thì tự chuyển sang proxy rồi mở lại."""
    if not f.page_factory:
        raise SkipProduct("Touch of Class cần Chrome để qua bước xác minh AWS WAF (chưa có trình duyệt)")
    if not f.proxy_on:
        page = f.page_factory(None)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(1500)
            body = page.content()
        except Exception:
            body = ""
        if any(m in body for m in GEO_BLOCK):
            if not f.switch_to_proxy("Touch of Class chặn theo vùng"):
                raise SkipProduct("Site chặn truy cập (chặn IP Việt Nam) — chưa có proxy nào để tự chuyển")
    page = f.page_factory(f.current_proxy())

    # proxy chịu ít kết nối: chặn ảnh/font/video và mọi host bên thứ ba khi mở trang này
    def route(r):
        req = r.request
        host = re.sub(r"^https?://([^/:]+).*", r"\1", req.url)
        if req.resource_type in ("image", "font", "media"):
            return r.abort()
        if not (host.endswith("touchofclass.com") or host.endswith("awswaf.com")):
            return r.abort()
        return r.continue_()

    page.route("**/*", route)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if f.should_stop():
                raise SkipProduct("Đã dừng")
            try:
                ck = [c for c in page.context.cookies() if c["name"] == "aws-waf-token"]
                ready = page.evaluate("() => document.readyState !== 'loading' && "
                                      "!!document.querySelector('#site_body, .ProductCollectionAJAXButtons')")
                if ck and ready:
                    return page.content(), f"aws-waf-token={ck[0]['value']}"
            except SkipProduct:
                raise
            except Exception:
                pass   # trang đang chuyển hướng sau bước xác minh
            page.wait_for_timeout(1000)
        raise SkipProduct("Hết thời gian chờ Chrome qua bước xác minh AWS WAF")
    finally:
        try:
            page.unroute("**/*", route)
        except Exception:
            pass


def _token_ok():
    return bool(_token["cookie"]) and time.time() - _token["t"] < TOKEN_TTL


def _refresh_token(f, page_url):
    """Lấy token mới bằng Chrome (CHỈ gọi ở luồng chính: Playwright sync không dùng chung giữa các luồng)."""
    with _token_lock:
        html, ck = _chrome_page(f, page_url)
        _token.update(cookie=ck, t=time.time())
        f.log("  ✓ Đã lấy token AWS WAF bằng Chrome", "info")
        return html


def _http(f, url, ajax=False):
    """HTTP kèm cookie token (dùng được ở luồng phụ). Bị chặn -> SkipProduct."""
    hdr = {"Cookie": _token["cookie"]}
    if ajax:
        hdr["X-Requested-With"] = "XMLHttpRequest"
    return f.get(url, headers=hdr)


def _get_page(f, page_url):
    """Trang sản phẩm: có token thì HTTP, chưa có/hết hạn/bị chặn thì mở bằng Chrome (lấy luôn HTML)."""
    if _token_ok():
        try:
            return _http(f, page_url)
        except SkipProduct as e:
            if "chặn" not in str(e):
                raise
            _token.update(cookie="", t=0.0)   # token hỏng -> lấy lại
    return _refresh_token(f, page_url)


# ---------------------------------------------------------------- đọc trang

def _block(html, item):
    """Khối HTML của món chính <div id="ProductCollectionAJAXButtons-ITEM"> tới khối kế tiếp."""
    i = html.find(f'<div id="ProductCollectionAJAXButtons-{item}"')
    if i < 0:
        return ""
    j = html.find('<div id="ProductCollectionAJAXButtons-', i + 20)
    return html[i:j if j > 0 else len(html)]


def _input(blk, name):
    m = re.search(r'name="%s"\s+(?:[^>]*?\s)?value="([^"]*)"' % re.escape(name), blk)
    return _html.unescape(m.group(1)) if m else ""


_ATTRS = r"""(?:[^>"']|"[^"]*"|'[^']*')*"""   # phần thuộc tính của 1 thẻ HTML, cho phép ">" trong ngoặc


def _traits(blk, item):
    """-> list trục [(attr_id, tên trục, [(value_id, nhãn), ...])] theo thứ tự trang."""
    axes = {}
    # Thẻ có thể chứa ">" trong ngoặc kép (vd data-msg="Please select a <b>Color</b>" ở HTML gốc qua HTTP)
    # -> đọc theo từng thuộc tính có ngoặc, không dừng ở dấu ">" đầu tiên
    for m in re.finditer(rf'(<label\b{_ATTRS}>)\s*(<input\b{_ATTRS}>)', blk):
        if 'btn-trait' not in m.group(1):
            continue
        lab, inp = m.group(1), m.group(2)
        if f"TraitLookupAttributeChange('{item}'" not in inp:
            continue
        name = re.search(r'data-name="([^"]*)"', lab)
        val = re.search(r'\bvalue="(\d+)"', inp)
        idm = re.search(r'id="%s_(\d+)_\d+_' % re.escape(item), inp)
        an = re.search(r'data-attributename="([^"]+)"', inp)
        if not (name and val and idm):
            continue
        attr = idm.group(1)
        ax = axes.setdefault(attr, [an.group(1) if an else "Option", []])
        ax[1].append((val.group(1), clean_text(_html.unescape(name.group(1)))))
    return [(a, v[0], v[1]) for a, v in axes.items()]


def _status(blk):
    """-> (còn bán?, chữ trạng thái, mã SKU biến thể)"""
    m = re.search(r'class="form-status">(.*?)</div>\s*</div>', blk, re.S)
    seg = m.group(1) if m else ""
    href = re.search(r'itemprop="availability" href="[^"]*/([A-Za-z_]+)"', seg)
    text = clean_text(re.sub(r"<[^>]+>", " ", seg))
    sku = _input(seg, "productSkuNormalized") or _input(seg, "productSku")
    if NOT_AVAIL.search(text) or "inventory-status-outofstock" in seg:
        return False, text, sku
    if href:
        h = href.group(1).lower().replace("_", "")
        # "instoreonly" (chỉ bán tại cửa hàng) không mua online được -> không tính là còn hàng
        return h in ("instock", "preorder", "backorder", "limitedavailability", "onlineonly"), text, sku
    # Không có thẻ schema.org: đọc chữ trạng thái ("Available and ready to ship", "Order now for shipment on ...")
    return bool(AVAIL_TEXT.search(text)), text, sku


def _xxl_jpg(u):
    u = _html.unescape(u).split("?")[0]
    if u.startswith("/"):
        u = SITE + u
    return re.sub(r"\.webp$", ".jpg", u)


def _gallery(html):
    i = html.find('id="js-product-gallery"')
    seg = html[i:html.find("</section>", i)] if i >= 0 else ""
    return dedup(_xxl_jpg(u) for u in re.findall(r'<a href="([^"]+/images/xxl/[^"]+)"', seg))


def _accordion(html):
    """Các tab Overview / Details / Sizes ... -> {tên tab: html}"""
    out = {}
    for m in re.finditer(r'data-target="#(card-[\w\-]+)"[^>]*>\s*([^<]+?)\s*</button>\s*'
                         r'<div class="card-body[^"]*" id="\1">(.*?)</div>\s*</div>', html, re.S):
        out[clean_text(m.group(2))] = m.group(3)
    return out


JUNK = re.compile(r"(?i)designed by touch of class|^designed by\b|swatch|click here|view the entire|exclusive|^imported\.?$"
                  r"|^\W*from\s+[\w&'.\s]+$|direct ship|search box|you might also consider|^enter [A-Z0-9]{3,5}\b"
                  r"|^[\w ]{3,40}(care tips|care and use)$|^shown:")
# Hãng làm ra món (Touch of Class bán lại): "Folding Chairs by Stakmore", "Stakmore Tables" -> bỏ tên hãng đó
MAKER_RE = re.compile(r"\bby ([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?)\b")


def _makers(*texts):
    out = set()
    for t in texts:
        for m in MAKER_RE.finditer(t or ""):
            if m.group(1).lower() not in ("touch", "touch of", "hand", "the", "one", "two"):
                out.add(m.group(1).split()[0])
    return out


def _texts(acc, makers=()):
    bullets, details = [], {}
    for name, frag in acc.items():
        low = name.lower()
        if low in ("overview", "details", "description"):
            for line in html_to_lines(frag):
                line = re.sub(r"\s*\(R\)|®|™", "", line).strip()   # bỏ ký hiệu trước khi lọc ("From Waterford(R) Linens")
                if JUNK.search(line):
                    continue
                line = strip_brand_desc(line, BRAND, *makers)
                # câu dặn chọn lựa chọn trên form đặt hàng của site ("Specify Midnight, Ivory, or Mocha.")
                line = re.sub(r"(?:^|(?<=[.!?]))\s*Specify\b[^.!?]*[.!?]?", "", line).strip()
                if line:
                    bullets.append(line)
        elif low in ("sizes", "size", "dimensions", "specifications", "care", "materials", "material"):
            txt = " ".join(x for x in html_to_lines(frag) if not JUNK.search(x))
            if txt:
                details["Dimensions" if low in ("sizes", "size") else name] = strip_brand_desc(txt, BRAND, *makers)
    return dedup(bullets), details


def _parse_ajax(text, item):
    blk = _block(text, item) or text
    price = to_price(_input(blk, "productprice"))
    orig = to_price(_input(blk, "productOriginalPrice"))
    avail, st, sku = _status(blk)
    imgs = [_xxl_jpg(u) for u in re.findall(r'(https?://www\.touchofclass\.com/images/xxl/[^"\'\s)]+)', blk)]
    imgs = [u for u in dedup(imgs) if not u.lower().endswith(f"/{item.lower()}.jpg")]
    return {"price": price, "orig": orig, "available": avail, "status": st, "sku": sku, "images": imgs}


# ---------------------------------------------------------------- bộ cào

NOT_COLOR = re.compile(r"(?i)\b(sham|comforter|duvet|quilt|coverlet|sheet|pillow|cushion|curtain|panel|valance|swag|"
                       r"tier|runner|placemat|napkin|towel|rug|mat|mug|plate|bowl|set|pair|piece|pc|lamp|shade|"
                       r"twin|full|queen|king|standard|euro|small|medium|large|petite|grande|\d)s?\b")
# (\b ở cuối: "Matte Black" không bị coi là "mat", "Twine" không phải "twin", "Kingston" không phải "king")


def _color_siblings(html, item, name):
    """Cùng trang có các món chỉ khác nhau phần màu ở cuối tên (vd ...Holder Black / ...Holder Brown, mã BU29-001/-002)
    -> [(item, màu, khối HTML)] gồm cả món chính, và tên chung. Không phải kiểu đó (bộ sưu tập chăn/gối/rèm) -> ([], "")."""
    words = name.split()
    group = []
    for it in dict.fromkeys(re.findall(r'<div id="ProductCollectionAJAXButtons-([A-Z0-9]+-[A-Z0-9]+)"', html)):
        if it.split("-")[0] != item.split("-")[0]:
            continue
        blk = _block(html, it)
        nm = _name(blk)
        if not nm or _traits(blk, it):
            continue
        group.append((it, nm.split(), blk))
    if len(group) < 2 or not any(it == item for it, _, _ in group):
        return [], ""
    n = 0   # số từ chung ở đầu tên của mọi món
    while n < len(words) and all(len(w) > n and w[n].lower() == words[n].lower() for _, w, _ in group):
        n += 1
    out = []
    for it, w, blk in group:
        rest = " ".join(w[n:])
        if not rest or len(w) - n > 3 or NOT_COLOR.search(rest):
            return [], ""
        out.append((it, rest, blk))
    if n < 2 or len({c.lower() for _, c, _ in out}) < len(out):
        return [], ""
    # "Ashton Bath Tissue Holder Black" / "Ashton Bath Soap Dish Black": phần khác nhau cùng kết thúc bằng 1 từ
    # -> khác LOẠI món chứ không phải khác màu -> không gộp
    if len({c.split()[-1].lower() for _, c, _ in out}) == 1:
        return [], ""
    return out, " ".join(words[:n])


OTHER_LINE = re.compile(r"(?i)also available|\(see details\)|click (on|the)\b|personaliz|monogram|specify (one|up to|your)")
NAME_STOP = {"set", "of", "two", "three", "four", "six", "eight", "pair", "the", "and", "with", "a", "in", "each"}


def _only_this_item(bullets, html, item, name, keep_items=()):
    """Trang bộ sưu tập (vd Florentina: khăn trải bàn + bộ lót đĩa + khăn ăn + khăn bàn) dùng chung 1 đoạn mô tả
    -> bỏ các câu nói về món KHÁC trên trang (Placemats..., Napkins...) và câu về khắc tên/mua thêm."""
    def words(s):   # từ có nghĩa, số ít (Placemats -> placemat)
        out = set()
        for w in re.findall(r"[A-Za-z]+", s):
            lw = w.lower()
            if lw not in NAME_STOP and len(lw) > 3:
                out.add(lw[:-1] if lw.endswith("s") and not lw.endswith("ss") else lw)
        return out

    def rx(ws):
        return re.compile(r"(?i)\b(" + "|".join(map(re.escape, sorted(ws))) + r")s?\b") if ws else None

    mine, seen = words(name), set()
    for it in dict.fromkeys(re.findall(r'<div id="ProductCollectionAJAXButtons-([A-Z0-9]+-[A-Z0-9]+)"', html)):
        if it != item and it not in keep_items:
            seen |= words(_name(_block(html, it)))
    other_re, own_re = rx(seen - mine), rx(mine - seen)   # từ riêng của món khác / của món này
    out = []
    for b in bullets:
        if re.match(r"(?i)\W*(also available|available separately|sold separately)", b):
            continue   # cả đoạn giới thiệu món mua thêm
        sents = []
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", b):
            if OTHER_LINE.search(s):
                continue
            if other_re and other_re.search(s) and (not (own_re and own_re.search(s)) or re.match(r"(?i)\s*choose\b", s)):
                continue   # câu chỉ nói về món khác, hoặc "Choose Table Runner, Placemat Set of 4, ..."
            if seen and re.match(r"(?i)\s*choose\b", s):
                continue   # trang nhiều món: "Choose Queen, King, or Cal King" là lựa chọn của món khác / cả bộ
            sents.append(s)
        if sents:
            out.append(" ".join(sents))
    return dedup(out)


def _page_items(html, item, skip=()):
    """Các món KHÁC trên cùng trang (trang bộ sưu tập) -> hiện trong cửa sổ Xem để thêm thành listing riêng."""
    out = []
    for it in dict.fromkeys(re.findall(r'<div id="ProductCollectionAJAXButtons-([A-Z0-9]+-[A-Z0-9]+)"', html)):
        if it == item or it in skip:
            continue
        blk = _block(html, it)
        url = _input(blk, "productUrl")
        if not url:
            continue
        traits = _traits(blk, it)
        avail, st, _ = _status(blk)
        img = _input(blk, "productImageUrl") or f"{SITE}/images/xxl/{it}.jpg"
        out.append({"code": it, "name": strip_brand(_name(blk), BRAND), "url": url,
                    "price": to_price(_input(blk, "productprice")),
                    "options": ", ".join(f"{a[1]}: {len(a[2])}" for a in traits),
                    "status": "" if traits else st, "available": None if traits else avail,
                    "image": _xxl_jpg(img) if "/images/" in img else img})
    return out


def _name(blk):
    """Tên món: lấy ở <h2 itemprop="name"> (đủ chữ) — ô productName bị cắt ở dấu inch ('Wall Shelf 24" Wide')."""
    m = re.search(r'<h2[^>]*itemprop="name"[^>]*>(.*?)</h2>', blk, re.S)
    return clean_text(m.group(1)) if m and clean_text(m.group(1)) else _input(blk, "productName")


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def scrape(f, url):
    item = product_code(url)
    if not item:
        raise SkipProduct("Link không phải trang sản phẩm Touch of Class (/p/<mã>/)")
    page_url = url.split("?")[0].split("#")[0]
    if not page_url.endswith("/"):
        page_url += "/"
    html = _get_page(f, page_url)
    blk = _block(html, item)
    if not blk:
        raise SkipProduct("Không thấy khối sản phẩm trên trang (có thể đã ngừng bán)")

    name = _name(blk)
    title = strip_brand(name, BRAND)
    crumbs = [clean_text(x) for x in re.findall(r'<li class="breadcrumb-item"[^>]*>.*?<span itemprop="name">(.*?)</span>',
                                                html, re.S)]
    crumbs = [c for c in crumbs[1:-1] if c]   # bỏ Home và tên sản phẩm
    acc = _accordion(html)
    makers = _makers(name, clean_text(" ".join(acc.values())))   # "by Stakmore" -> bỏ "Stakmore" khỏi mô tả
    bullets, details = _texts(acc, makers)
    title = strip_brand(title, *makers)
    details["Item Number"] = item
    page_imgs = _gallery(html) or [_xxl_jpg(u) for u in re.findall(r'href="([^"]+/images/xxl/[^"]+)"', blk)][:1]

    axes = _traits(blk, item)
    color_axes = [a for a in axes if COLOR_AXIS.search(a[1])]
    size_axes = [a for a in axes if a not in color_axes]
    color_ax = color_axes[0] if color_axes else None
    size_axes += color_axes[1:]

    variants, color_imgs = [], {}
    siblings, base = ([], "") if axes else _color_siblings(html, item, name)
    url_color = ""
    if siblings:
        # Mỗi màu là 1 món riêng trên cùng trang (mã -001, -002...) -> gộp thành biến thể màu, chọn sẵn màu của link
        title = strip_brand(base, BRAND)
        for it, color, sblk in siblings:
            avail, st, sku = _status(sblk)
            p = to_price(_input(sblk, "productprice"))
            o = to_price(_input(sblk, "productOriginalPrice"))
            variants.append({"color": color, "size": "", "price": p, "list_price": o if o and p and o > p else None,
                             "available": avail, "id": sku or it})
            own = [_xxl_jpg(u) for u in re.findall(r'["\'(]([^"\'\s()]*/images/xxl/[^"\'\s)]+)', sblk)]
            # món khác không có link ảnh trong khối -> ảnh chuẩn theo mã của chính món đó (không mượn ảnh món chính)
            color_imgs[color] = (page_imgs if it == item and page_imgs
                                 else dedup(own)[:12] or [f"{SITE}/images/xxl/{it}.jpg"])
            if it == item:
                url_color = color
                if st:
                    details["Availability"] = st
        color_ax = ("sib", "Color", [(it, c) for it, c, _ in siblings])
    elif not axes:
        avail, st, sku = _status(blk)
        if re.search(r"(?i)make a selection", st):
            # Trang có lựa chọn màu/size nhưng tool không đọc ra -> báo lỗi, không coi là hết hàng
            raise SkipProduct("Không đọc được các lựa chọn màu/size trên trang — báo lại để sửa bộ cào")
        p = to_price(_input(blk, "productprice"))
        o = to_price(_input(blk, "productOriginalPrice"))
        if st:
            details["Availability"] = st
        variants.append({"color": "", "size": "", "price": p, "list_price": o if o and p and o > p else None,
                         "available": avail, "id": sku or item})
    else:
        ordered = ([color_ax] if color_ax else []) + size_axes
        combos = list(itertools.product(*[ax[2] for ax in ordered]))
        if len(combos) > MAX_COMBOS:
            f.log(f"  ⚠ sản phẩm có {len(combos)} tổ hợp lựa chọn — chỉ đọc {MAX_COMBOS} tổ hợp đầu, "
                  "các màu / size cuối có thể bị thiếu", "warn")
            combos = combos[:MAX_COMBOS]

        def ajax(combo):
            q = "".join(f"&a{i}={ax[0]}&av{i}={v[0]}" for i, (ax, v) in enumerate(zip(ordered, combo), 1))
            last_ax, last_v = ordered[-1][0], combo[-1][0]
            u = f"{AJAX}&p={item}&csa={last_ax}&csav={last_v}{q}&extra={int(time.time() * 1000)}"
            try:
                return combo, _parse_ajax(_http(f, u, ajax=True), item)
            except SkipProduct as e:
                if "Đã dừng" in str(e):
                    raise
                return combo, None

        with ThreadPoolExecutor(WORKERS) as ex:
            results = list(ex.map(ajax, combos))
        failed = [c for c, r in results if r is None]
        if failed:
            # có thể token hết hạn giữa chừng: lấy lại bằng Chrome ở luồng chính rồi thử lại tuần tự.
            # Lấy lại token lỗi (Chrome hết giờ...) -> giữ các lựa chọn đã đọc được, không bỏ cả sản phẩm
            try:
                _refresh_token(f, page_url)
                redo = dict(ajax(c) for c in failed)
                results = [(c, r if r is not None else redo.get(c)) for c, r in results]
            except SkipProduct as e:
                if "Đã dừng" in str(e):
                    raise
                f.log(f"  ⚠ không lấy lại được token để đọc {len(failed)} lựa chọn còn thiếu: {e}", "warn")
        failed = [c for c, r in results if r is None]
        if failed and len(failed) == len(results):
            # Không đọc được tổ hợp nào: site chặn tạm thời, KHÔNG phải hết hàng
            raise SkipProduct("Không đọc được giá / tình trạng hàng (site tạm chặn) — bấm \"Chạy lại dòng lỗi\"")
        if failed:
            f.log(f"  ⚠ {len(failed)}/{len(results)} lựa chọn không đọc được tình trạng — bỏ qua các lựa chọn đó", "warn")
        statuses = set()
        for combo, r in results:
            if not r or not r["price"]:
                continue
            if not r["sku"] and not r["available"]:
                continue   # tổ hợp không tồn tại (site trả giá của lựa chọn khác, không có SKU)
            labels = [v[1] for v in combo]
            color = labels[0] if color_ax else ""
            size = " / ".join(labels[1:] if color_ax else labels)
            if color and r["images"] and color not in color_imgs:
                color_imgs[color] = r["images"]
            o = r["orig"]
            variants.append({"color": color, "size": size, "price": r["price"],
                             "list_price": o if o and o > r["price"] else None,
                             "available": r["available"], "id": r["sku"] or f"{item}-{'-'.join(v[0] for v in combo)}"})
            if r["available"] and r["status"]:
                statuses.add(r["status"])
        if len(statuses) == 1:
            details["Availability"] = statuses.pop()

    colors, images = [], page_imgs
    if color_ax:
        for _vid, cname in color_ax[2]:
            if any(v["color"] == cname for v in variants):
                own = color_imgs.get(cname, [])
                colors.append({"name": cname, "images": dedup(own + ([] if own else page_imgs))[:12]})
    # Link ghi sẵn màu / kích thước trong đường dẫn (vd .../florentina-table-runner-antique-gold-16-x-72/p/AB44-002/)
    # -> báo cho tool chọn sẵn đúng lựa chọn đó
    slug = "-" + _slug((re.search(r"touchofclass\.com/([^/?#]+)/p/", url) or ["", ""])[1]) + "-"
    pick = {}
    for key, labels in (("url_color", [c["name"] for c in colors]),
                        ("url_size", list(dict.fromkeys(v["size"] for v in variants if v["size"])))):
        hits = [x for x in labels if _slug(x) and f"-{_slug(x)}-" in slug]
        # "Gold" và "Antique Gold" cùng khớp ...-antique-gold-... -> bỏ tên nằm gọn trong tên dài hơn đã khớp
        hits = [x for x in hits if not any(y != x and f"-{_slug(x)}-" in f"-{_slug(y)}-" for y in hits)]
        if len(labels) > 1 and len(hits) == 1:
            pick[key] = hits[0]
    if siblings:
        pick["url_color"] = url_color   # màu của đúng món trong link
        images = []
    bullets = _only_this_item(bullets, html, item, name, [it for it, _, _ in siblings])
    pick["page_items"] = _page_items(html, item, [it for it, _, _ in siblings])
    return {
        **pick,
        "site": KEY,
        "code": item,
        "url": _input(blk, "productUrl") or page_url,
        "title": title,
        "brand": BRAND,
        "bullets": bullets,
        "details": details,
        "breadcrumbs": crumbs,
        "department": "",
        "size_name": " / ".join(a[1] for a in size_axes) if size_axes else "",
        "colors": colors,
        "images": images[:12],
        "variants": variants,
    }
