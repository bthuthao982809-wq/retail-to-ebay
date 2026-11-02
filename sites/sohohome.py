"""Soho Home (sohohome.com) — nền D3R, sau Cloudflare. Bắt buộc dùng cửa hàng Mỹ /us/ (giá USD).

Cách lấy (HTTP giả Chrome qua proxy Mỹ, không cần cookie country):
  - Mỗi biến thể là 1 trang riêng /us/products/<slug>/<sku> có ld+json Product (giá USD, tồn kho, màu).
  - Trục màu/vải: các ô swatch  <a class="swatch swatch--pdp" aria-label="Velvet Seafoam" href=".../<sku>">
  - Trục còn lại (thường Size): các nút pill <input name="variant-pill" value="/products/<slug>/<sku>"> + <label>
    nhãn trục lấy ở <dt class="product-detail__label"> ngay trước (Size / Colour / Finish ...).
  - Trang 1 màu -> các size của màu đó; nên tải: trang gốc + mỗi màu 1 trang + mỗi size của mỗi màu 1 trang.
  - Ảnh: link phóng to trong gallery (cdn-cgi/image ... width=2250), đổi format=auto -> format=jpeg. Tải ảnh không cần proxy.
  - Giá: ld+json offers.price (giá thường, KHÔNG lấy giá hội viên); giá gốc khi giảm giá = product-detail__value--was đầu tiên.
"""
import html as _html
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from sites.base import SkipProduct, clean_text, dedup, find_ld, html_to_lines, strip_brand, strip_brand_desc, to_price

KEY = "sohohome"
NAME = "Soho Home"
URL_RE = re.compile(r"https?://(?:www\.)?sohohome\.com/(?:[a-z]{2}/)?products/[^/?#]+", re.I)
NEEDS_PROXY = True   # IP Việt Nam bị chặn

SITE = "https://www.sohohome.com"
BRAND = "Soho Home"
MAX_PAGES = 80       # giới hạn số trang biến thể tải cho 1 sản phẩm
MAX_IMAGES = 12
WORKERS = 5
COLOR_AXIS = re.compile(r"(?i)colou?r|finish|fabric|shade|metal|wood|marble|glass|tone")
ITEM_RE = re.compile(r"/products/([a-z0-9\-]+)", re.I)


def product_code(url):
    # Mã gốc (parentProductId) chỉ có trong trang -> không đoán từ link
    return None


def _us_url(href):
    """Đưa link (vd /products/x/123 hoặc /gb/products/...) về cửa hàng Mỹ đầy đủ."""
    href = _html.unescape(href or "").split("?")[0].split("#")[0]
    href = re.sub(r"^https?://(?:www\.)?sohohome\.com", "", href)
    href = re.sub(r"^/[a-z]{2}(?=/products/)", "", href)
    return SITE + "/us" + href if href.startswith("/products/") else None


def _big_image(u):
    u = _html.unescape(u)
    return u.replace("format=auto", "format=jpeg")


def _parse(html):
    """1 trang biến thể -> dict thông tin của biến thể đó + link các biến thể khác."""
    ld = find_ld(html, "Product") or {}
    offer = ld.get("offers") or {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    out = {
        "sku": str(ld.get("sku") or ""),
        "ld": ld,
        "price": to_price(offer.get("price")),
        "currency": offer.get("priceCurrency") or "",
        "available": not re.search(r"OutOfStock|Discontinued|SoldOut", str(offer.get("availability") or ""), re.I),
    }
    # khối giá: giá gốc (gạch ngang) nếu đang giảm
    m = re.search(r'class="product-detail__prices(.*?)</dd>', html, re.S)
    was = re.search(r'product-detail__value--was">\s*([^<]+)<', m.group(1)) if m else None
    out["was"] = to_price(was.group(1)) if was else None
    # swatch màu
    sw = []
    for a in re.finditer(r'<a\b[^>]*class="swatch swatch--pdp[^"]*"[^>]*>', html):
        tag = a.group(0)
        href = re.search(r'href="([^"]+)"', tag)
        label = re.search(r'aria-label="([^"]+)"', tag)
        if href and label:
            sw.append({"name": clean_text(label.group(1)), "url": _us_url(href.group(1)),
                       "current": 'aria-current="true"' in tag})
    out["swatches"] = sw
    # trục pill (Size/...)
    axis, pills = "", []
    dl = re.search(r'<dl class="product-detail__options product-detail__options--variants">(.*?)</dl>', html, re.S)
    if dl:
        lab = re.search(r'<dt class="product-detail__label">\s*([^<]+?)\s*</dt>', dl.group(1))
        axis = clean_text(lab.group(1)) if lab else ""
        for inp in re.finditer(r'<input\b([^>]*name="variant-pill"[^>]*)>\s*<label[^>]*>(.*?)</label>', dl.group(1), re.S):
            attrs = inp.group(1)
            v = re.search(r'value="([^"]+)"', attrs)
            pills.append({"name": clean_text(inp.group(2)), "url": _us_url(v.group(1)) if v else None,
                          "current": re.search(r"\bchecked\b", attrs) is not None})
    out["axis"], out["pills"] = axis, pills
    # ảnh gallery (link phóng to)
    g = html.find("data-product-gallery-lightbox")
    gal = html[g:html.find("</ul>", g)] if g >= 0 else ""
    imgs = [_big_image(u) for u in re.findall(r'<a\s+class="carousel-item[^"]*"\s+href="([^"]+)"', gal)]
    if not imgs:
        imgs = [_big_image(u) for u in re.findall(r'href="(https://assets\.sohohome\.com/[^"]+)"\s+data-pswp-width', gal)]
    if not imgs and ld.get("image"):
        im = ld["image"] if isinstance(ld["image"], list) else [ld["image"]]
        imgs = [_big_image(x) for x in im if isinstance(x, str)]
    out["images"] = dedup(imgs)[:MAX_IMAGES]
    t = re.search(r'<h1 class="product-detail__title[^"]*">(.*?)</h1>', html, re.S)
    out["title"] = clean_text(t.group(1)) if t else clean_text(str(ld.get("name") or "").split(",")[0])
    return out


def _details(html, ld):
    """Mô tả + bảng Details & Dimensions -> (bullets, details)."""
    bullets, details = [], {}
    tpl = re.search(r'data-product-detail-panel-template="details-dimensions">(.*?)</template>', html, re.S)
    body = tpl.group(1) if tpl else ""
    d = re.search(r'details-dimensions__description[^>]*>(.*?)</div>', body, re.S)
    if d:
        bullets = [strip_brand_desc(x, BRAND) for x in html_to_lines(d.group(1).replace("\n", " "))
                   # bỏ câu quảng cáo về các nhà Soho House (kể cả "Soho Roc House", "Soho Farmhouse"...)
                   if not re.search(r"(?i)\bin our (?:\w+ )?Houses\b|\bSoho(?:\s+\w+)?\s+House\b|\bSoho Farmhouse\b", x)]
    for k, v in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", body, re.S):
        k = clean_text(k).rstrip(":").strip()
        v = re.sub(r"\s+", " ", clean_text(v)).strip()
        if not k or not v:
            continue
        if k.lower().startswith("care"):
            k = "Care"
        details[k] = strip_brand_desc(v, BRAND)
    if ld.get("material") and "Material" not in details:
        details["Material"] = str(ld["material"])
    fab = re.search(r'data-product-detail-panel-template="fabric-details">(.*?)</template>', html, re.S)
    if fab:
        for k, v in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", fab.group(1), re.S):
            k = clean_text(k).rstrip(":").strip()
            if k and clean_text(v) and k not in details:
                details[k] = clean_text(v)
    lt = re.search(r'class="product-detail__subtitle js-lead-time">\s*(.*?)\s*</p>', html, re.S)
    if lt and clean_text(lt.group(1)):
        details["Lead Time"] = clean_text(lt.group(1))
    lay = re.search(r'data-pdp-modular-summary-value[^>]*>\s*(.*?)\s*</p>', html, re.S)
    if lay and clean_text(lay.group(1)):
        details["Layout"] = re.sub(r",\s*US$", "", clean_text(lay.group(1)))
    return bullets, details


def scrape(f, url):
    m = ITEM_RE.search(url)
    if not m:
        raise SkipProduct("Link không phải trang sản phẩm Soho Home")
    # luôn mở bản Mỹ (/us/) để có giá USD
    tail = re.search(r"/products/([a-z0-9\-]+(?:/\d+)?)", url, re.I).group(1)
    start = f"{SITE}/us/products/{tail}"
    html0 = f.get(start)
    p0 = _parse(html0)
    if not p0["sku"]:
        raise SkipProduct("Không thấy dữ liệu sản phẩm (ld+json) trên trang")
    if p0["currency"] and p0["currency"] != "USD":
        raise SkipProduct(f"Giá không phải USD ({p0['currency']})")

    pages = {p0["sku"]: (p0, html0)}
    fetched_urls = {start}
    state = {"n": 1}
    guard = threading.Lock()   # 5 luồng cùng đếm số trang: khoá để không vượt MAX_PAGES / tải trùng

    def load(u):
        with guard:
            if not u or u in fetched_urls or state["n"] >= MAX_PAGES:
                return None
            fetched_urls.add(u)
            state["n"] += 1
        try:
            h = f.get(u, allow_404=True)
        except SkipProduct as e:
            if "Đã dừng" in str(e):
                raise   # bấm Dừng: dừng hẳn, không trả về sản phẩm thiếu size
            f.log(f"  ⚠ không đọc được 1 lựa chọn ({e}) — bỏ qua lựa chọn đó", "warn")
            return None
        if not h:
            return None
        p = _parse(h)
        return (p, h) if p["sku"] else None

    def load_many(urls):
        urls = [u for u in dedup(urls) if u not in fetched_urls]
        with ThreadPoolExecutor(WORKERS) as ex:
            for r in ex.map(load, urls):
                if r:
                    pages.setdefault(r[0]["sku"], r)

    # pill là trục màu khi nhãn trục giống màu/chất liệu và trang không có swatch
    pill_is_color = bool(p0["pills"]) and not p0["swatches"] and bool(COLOR_AXIS.search(p0["axis"] or ""))

    # 1) mỗi màu (swatch) 1 trang
    color_pages = {}   # tên màu -> (parse, html)
    if p0["swatches"]:
        load_many([s["url"] for s in p0["swatches"] if not s["current"]])
        by_url = {}
        for p, h in pages.values():
            cur = next((s for s in p["swatches"] if s["current"]), None)
            if cur:
                by_url.setdefault(cur["name"], (p, h))
        for s in p0["swatches"]:
            if s["name"] in by_url:
                color_pages[s["name"]] = by_url[s["name"]]
    # 2) mỗi size (pill) của mỗi màu 1 trang
    pill_urls = []
    for p, _h in (list(color_pages.values()) or [(p0, html0)]):
        pill_urls += [x["url"] for x in p["pills"] if not x["current"]]
    load_many(pill_urls)

    # ---- dựng biến thể từ mọi trang đã tải
    variants, seen = [], set()
    for p, _h in pages.values():
        color = next((s["name"] for s in p["swatches"] if s["current"]), "")
        pill = next((x["name"] for x in p["pills"] if x["current"]), "")
        size = ""
        if pill_is_color:
            color = pill
        else:
            size = pill
        if (color, size) in seen or not p["price"]:
            continue
        seen.add((color, size))
        old = p["was"] if p["was"] and p["was"] > p["price"] else None
        variants.append({"color": color, "size": size, "price": p["price"], "list_price": old,
                         "available": p["available"], "id": p["sku"]})

    # thứ tự theo site: màu theo swatch, size theo pill
    color_order = [s["name"] for s in p0["swatches"]] or ([x["name"] for x in p0["pills"]] if pill_is_color else [])
    size_order = []
    for p, _h in [(p0, html0)] + list(color_pages.values()):
        if not pill_is_color:
            size_order += [x["name"] for x in p["pills"]]
    size_order = dedup(size_order)
    variants.sort(key=lambda v: (color_order.index(v["color"]) if v["color"] in color_order else 999,
                                 size_order.index(v["size"]) if v["size"] in size_order else 999))

    colors, images = [], []
    if color_order:
        for name in color_order:
            pg = color_pages.get(name)
            if not pg and pill_is_color:
                pg = next(((p, h) for p, h in pages.values()
                           if next((x["name"] for x in p["pills"] if x["current"]), "") == name), None)
            if pg or any(v["color"] == name for v in variants):
                colors.append({"name": name, "images": pg[0]["images"] if pg else []})
    else:
        images = p0["images"]

    ga = re.search(r'"category":\s*"([^"]+)"', html0)
    crumbs = [clean_text(x) for x in ga.group(1).replace("\\/", "/").split("/")] if ga else []
    parent = re.search(r'"parentProductId":\s*"(\d+)"', html0) or re.search(r'data-parent-product-id="(\d+)"', html0)
    bullets, details = _details(html0, p0["ld"])
    if not color_order and p0["ld"].get("color"):
        details.setdefault("Color", str(p0["ld"]["color"]))

    return {
        "site": KEY,
        "code": parent.group(1) if parent else p0["sku"],
        "url": f"{SITE}/us/products/{m.group(1)}",
        "title": strip_brand(p0["title"], BRAND),
        "brand": BRAND,
        "bullets": bullets,
        "details": details,
        "breadcrumbs": crumbs,
        "department": "",
        "size_name": (p0["axis"] or "Size") if size_order else "",
        "colors": colors,
        "images": images,
        "variants": variants,
    }
