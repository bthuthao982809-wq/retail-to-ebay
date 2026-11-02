"""Talbots (talbots.com) — SFCC SiteGenesis cũ, site Sites-talbotsus-Site.

Link sản phẩm thường (…/P264031007.html) bị PerimeterX chặn script, nhưng
/on/demandware.store/Sites-talbotsus-Site/en_US/Product-Show?pid=P264031007 trả nguyên trang (không cần proxy;
proxy datacenter còn bị 403).
Trang có ld+json ProductGroup.hasVariant = mọi biến thể (màu × sizeType × size) kèm giá bán, tình trạng
còn hàng, ảnh chính theo màu; link offer có dwvar_..._sizeType=MS/PT/WO/WP -> gộp vào nhãn size ("8 Petite",
Misses giữ nguyên "8").
Giá gạch (SALE) chỉ có trong khối price-container của lựa chọn đang hiện -> nếu đang sale thì hỏi thêm
Product-Variation (HTML) cho từng sizeType còn lại (Plus thường giá khác Misses). Không sale: 1 request.
Ảnh: ảnh chính từng màu (ld+json) + ảnh phụ chung (thumbnail trên trang), lấy bản gốc demandware.static (~1500×2000).
"""
import html as _html
import re
import urllib.parse

from sites.base import SkipProduct, clean_text, dedup, find_ld, strip_brand, to_price, validate
from sites.sfcc import dw_static, make_bullets, store_url, titlecase

KEY = "talbots"
NAME = "Talbots"
URL_RE = re.compile(r"https?://(www\.)?talbots\.com/", re.I)
NEEDS_PROXY = False

HOST = "www.talbots.com"
SITE = "Sites-talbotsus-Site"
SIZE_TYPES = {"MS": "Misses", "PT": "Petite", "WO": "Plus", "WP": "Plus Petite", "WM": "Woman", "WMP": "Woman Petite"}
IN_STOCK = ("instock", "limitedavailability", "onlineonly")   # "instoreonly" không mua online được


def product_code(url):
    m = re.search(r"/(P\d{6,12})\.html", url, re.I) or re.search(r"[?&]pid=(P\d{6,12})", url, re.I)
    return m.group(1).upper() if m else None


def _pid_from_url(url):
    m = re.search(r"/(P?\d{6,12})\.html", url, re.I) or re.search(r"[?&]pid=(P?\d{6,12})", url, re.I)
    return m.group(1).upper() if m else None


def _section(h, cls):
    """Khối <ul class="swatches <cls>">…</ul> đầu tiên."""
    m = re.search(rf'<ul class="swatches {cls}">(.*?)</ul>', h, re.S)
    return m.group(1) if m else ""


def _swatches(block, attr):
    """[(title, href)] của các swatch trong khối (theo thứ tự trên site)."""
    out = []
    for m in re.finditer(r'<a class="swatchanchor"\s*href="([^"]*)"[^>]*?title="Select ' + attr + r':\s*([^"]+)"', block, re.S):
        out.append((_html.unescape(m.group(2)).strip(), _html.unescape(m.group(1))))
    return out


def _list_price(h):
    """Giá gạch (SALE) của lựa chọn đang hiện; None nếu không sale."""
    m = re.search(r'<div class="price-container">(.*?)</div>\s*</div>', h, re.S)
    block = m.group(1) if m else h
    m = re.search(r'class="striked-price">\s*\$?([\d,.]+)', block)
    return to_price(m.group(1)) if m else None


def _param(url, name):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for k, v in q.items():
        if k.endswith("_" + name):
            return v[0]
    return ""


def _img(static_url):
    """Ảnh gốc qua Image Service, ép JPEG chất lượng 100 (link static trần có lúc CDN trả WebP)."""
    u = static_url.replace(f"https://{HOST}/on/demandware.static/", f"https://{HOST}/dw/image/v2/BKGN_PRD/on/demandware.static/")
    return u + "?sfrm=jpg&q=100" if "/dw/image/" in u else u


def _details(h):
    bullets, details = [], {}
    m = re.search(r'<div class="product-description-content pdp-description">(.*?)</div>', h, re.S)
    if m:
        bullets.append(m.group(1))
    for sec in re.finditer(r'<div class="product-description-container pdp-features-materials-container">\s*<h6>(.*?)</h6>'
                           r'\s*<div class="product-description-content">(.*?)</div>', h, re.S):
        body = sec.group(2)
        kv = re.findall(r"<p>\s*<strong>\s*([^<:]+):?\s*</strong>(.*?)</p>", body, re.S)
        for k, v in kv:
            details[clean_text(k).strip(": ")] = clean_text(v)
        if not kv:
            for li in re.findall(r"<li>(.*?)</li>", body, re.S):
                t = clean_text(li)
                if t.lower().rstrip(".") == "imported":
                    details["Origin"] = "Imported"
                elif t:
                    bullets.append(t)
    return bullets, details


def _get(f, url):
    """GET qua HTTP. Bị PerimeterX chặn -> báo lỗi rõ ràng (tool tự dừng sau 2 link bị chặn liền nhau).

    Không mở Chrome để người dùng xác minh: PerimeterX nhận ra Chrome do tool điều khiển, nhấn "Press & Hold"
    bao nhiêu lần cũng không qua (đã thử 12/10/2026)."""
    try:
        return f.get(url)
    except SkipProduct as e:
        if "chặn" in str(e):
            hint = ("chưa có proxy để tự chuyển — chờ 30–60 phút" if not f.proxy_on
                    else "các proxy đang thử đều bị chặn — chờ 30–60 phút")
            raise SkipProduct(f"Talbots tạm chặn truy cập — {hint} rồi bấm \"Chạy lại dòng lỗi\"")
        raise


def scrape(f, url):
    pid = _pid_from_url(url)
    if not pid:
        raise SkipProduct("Không đọc được mã sản phẩm từ link")
    h = _get(f, store_url(HOST, SITE, "Product-Show", {"pid": pid}))
    g = find_ld(h, "ProductGroup", "Product")
    if not g or not g.get("hasVariant"):
        raise SkipProduct("Trang không có dữ liệu biến thể (ld+json)")
    master = g.get("productGroupID") or product_code(url) or pid

    # Thứ tự sizeType / màu như trên site
    st_sw = _swatches(_section(h, "sizetype"), "sizeType")
    color_sw = [t for t, _ in _swatches(_section(h, "color"), "color")]

    rows = []
    for v in g["hasVariant"]:
        o = v.get("offers") or {}
        if isinstance(o, list):
            o = o[0] if o else {}
        ou = o.get("url") or ""
        color = _param(ou, "color") or str(v.get("color") or "").replace("-", " ")
        stc = _param(ou, "sizeType")
        rows.append({
            "id": str(v.get("sku") or ""), "color": color.strip(), "st": stc, "size": str(v.get("size") or "").strip(),
            "price": to_price(o.get("price")), "cur": (o.get("priceCurrency") or "USD").upper(),
            "avail": (o.get("availability") or "").rsplit("/", 1)[-1].lower() in IN_STOCK,
            "image": v.get("image") or o.get("image") or "",
        })
    if any(r["cur"] != "USD" for r in rows):
        raise SkipProduct("Site không trả giá USD")

    # Mã sizeType -> tên: swatch có href chứa mã; swatch đang chọn có href rỗng -> mã còn thiếu trong ld
    st_name = dict(SIZE_TYPES)
    st_order, unnamed = [], []
    for title, href in st_sw:
        code = _param(href, "sizeType")
        if code:
            st_name[code] = title
            st_order.append(code)
        else:
            unnamed.append(title)
    codes_ld = dedup(r["st"] for r in rows if r["st"])
    missing = [c for c in codes_ld if c not in st_order]
    selected_st = None
    if unnamed and missing:
        selected_st = missing[0]
        st_name[selected_st] = unnamed[0]
        idx = [t for t, _ in st_sw].index(unnamed[0])
        st_order.insert(idx, selected_st)
    st_order += [c for c in codes_ld if c not in st_order]
    only_misses = set(codes_ld) <= {"MS"}

    # Giá gạch theo sizeType (chỉ khi trang đang báo SALE)
    lists = {}
    lp = _list_price(h)
    if lp:
        lists[selected_st or (st_order[0] if st_order else "")] = lp
        for code in codes_ld:
            if code in lists:
                continue
            col = next(r["color"] for r in rows if r["st"] == code)
            frag = _get(f, store_url(HOST, SITE, "Product-Variation", {
                "pid": master, f"dwvar_{master}_color": col, f"dwvar_{master}_sizeType": code, "format": "ajax"}))
            lists[code] = _list_price(frag)

    # Màu: theo swatch, thêm màu chỉ có ở sizeType khác
    color_keys = dedup([c.upper() for c in color_sw] + [r["color"].upper() for r in rows])
    cname = {k: titlecase(k) for k in color_keys}

    # Ảnh phụ chung (thumbnail không gắn mã màu)
    thumbs = [dw_static(_html.unescape(u)) for u in re.findall(r'<img class="productthumbnail" src="([^"]+)"', h)]
    color_imgs = {dw_static(r["image"]) for r in rows if r["image"]}
    shared = [u for u in dedup(thumbs) if u not in color_imgs and not re.search(r"/\d+W?_\d+\.jpg$", u)]

    colors = []
    for k in color_keys:
        own = dedup(dw_static(r["image"]) for r in rows if r["color"].upper() == k and r["image"])
        colors.append({"name": cname[k], "images": [_img(u) for u in (own + shared)[:12]]})

    # Thứ tự size: theo sizeType rồi theo thứ tự trong ld
    size_order = {}
    for code in st_order + [""]:
        for r in rows:
            if r["st"] == code:
                size_order.setdefault((code, r["size"]), len(size_order))
    variants = []
    for r in sorted(rows, key=lambda r: (color_keys.index(r["color"].upper()),
                                         size_order.get((r["st"], r["size"]), 9999))):
        label = r["size"]
        if r["st"] and r["st"] != "MS" and not only_misses:
            label = f"{label} {st_name.get(r['st'], r['st'])}".strip()
        lst = lists.get(r["st"])
        variants.append({
            "color": cname[r["color"].upper()], "size": label, "price": r["price"],
            "list_price": lst if lst and r["price"] and lst > r["price"] + 0.005 else None,
            "available": r["avail"], "id": r["id"],
        })

    title = strip_brand(clean_text(g.get("name") or ""), NAME)
    bl, details = _details(h)
    bullets = make_bullets(bl, [NAME])
    crumbs = [clean_text(x) for x in re.findall(
        r'class="breadcrumb-element"[^>]*>\s*<span itemprop="name">(.*?)</span>', h, re.S)]
    crumbs = [c for c in crumbs if c and c.lower() not in ("home", title.lower())]

    canon = g.get("url") or f"https://{HOST}/{master}.html"
    prod = {
        "site": KEY, "code": master, "url": canon, "title": title, "brand": NAME,
        "bullets": bullets, "details": details, "breadcrumbs": crumbs, "department": "Women",
        "size_name": "Size", "colors": colors, "images": [], "variants": variants,
    }
    return validate(prod)
