"""Sonos (sonos.com) — Next.js trên Vercel, không chống bot, IP Việt Nam vào được.

Trang /en-us/shop/<tên>: ld+json ProductGroup có hasVariant = từng MÀU (tên, sku, giá offers.price, tình trạng
offers.availability, link trang của màu đó). Mỗi màu có trang riêng (/shop/era-100-white) với bộ ảnh riêng:
các <img sizes="var(--tw-xGrid-x20)"> là thư viện ảnh sản phẩm. Thông số kỹ thuật ở các tab "Tech Specs"
(<div role="tabpanel" id="tech-spec-...">, từng cặp subhead-sm / para-sm-pt-text).
Link không ghi màu (/shop/era-100) -> chọn hết các màu; link có màu (/shop/era-100-white) -> chọn sẵn màu đó.
"""
import html as _html
import re
from concurrent.futures import ThreadPoolExecutor

from sites.base import SkipProduct, clean_text, dedup, find_ld, strip_brand, to_price, validate
from sites.sfcc import make_bullets

KEY = "sonos"
NAME = "Sonos"
URL_RE = re.compile(r"https?://(www\.)?sonos\.com/[a-z]{2}-[a-z]{2}/shop/[\w-]+", re.I)
NEEDS_PROXY = False
EXAMPLES = ["https://www.sonos.com/en-us/shop/era-100"]

# Sonos + hãng làm phụ kiện / loa âm tường bán kèm trên site (Belkin Wall Hook, Sanus Stand, "by Sonance")
BRANDS = ("Sonos", "Belkin", "Sanus", "Sonance")
IN_STOCK = ("instock", "limitedavailability", "onlineonly", "preorder", "backorder")
# Trang tân trang ghi: "includes: ... All accessories, manuals, and other documentation" -> ghi chú tình trạng
REFURB_NOTE_FULL = ("Manufacturer refurbished: inspected and tested by the manufacturer. "
                    "Includes all accessories, manuals and documentation.")
REFURB_NOTE_SHORT = "Manufacturer refurbished: inspected and tested by the manufacturer."
# Tab thông số đưa vào mô tả (bỏ Box Contents / Requirements / Downloads: tên phụ kiện kèm tên hãng, link tải)
SPEC_TABS = ("audio", "details-and-dimensions", "controls", "power-and-connectivity")
# Nhóm hàng của site (dữ liệu analytics item_category / item_list_name) -> chữ thêm vào tên
# (tên Sonos chỉ là tên mẫu: "Era 100", "Ace", "Arc Ultra" — người mua tìm theo loại hàng)
KINDS = [("headphone", "Wireless Headphones"), ("set", "Home Theater System"), ("soundbar", "Soundbar"),
         ("architectural", "Speakers"), ("in-wall", "Speakers"), ("in-ceiling", "Speakers"),   # loa âm tường (có dây)
         ("home theater", "Soundbar"), ("subwoofer", "Wireless Subwoofer"), ("portable", "Portable Speaker"),
         ("amplifier", "Amplifier"), ("component", "Streaming Component"), ("speaker", "Wireless Speaker"),
         ("all in one", "Wireless Speaker")]


def product_code(url):
    m = re.search(r"/shop/([\w-]+)", url)
    return m.group(1).lower() if m else None


def _gallery(html):
    """Ảnh thư viện sản phẩm của trang (đúng màu của trang), link gốc cỡ lớn nhất, bỏ gif/svg."""
    out = []
    for tag in re.findall(r"<img\b[^>]*>", html):
        if 'sizes="var(--tw-xGrid-x20)"' not in tag:
            continue
        m = re.search(r'\ssrc="(https://media\.sonos\.com/images/[^"?]+)', tag)
        if m and not re.search(r"\.(gif|svg)$", m.group(1), re.I):
            out.append(m.group(1))
    return dedup(out)[:12]


def _specs(html):
    """Tab Tech Specs -> list (tên, giá trị) theo thứ tự trang."""
    out = []
    for m in re.finditer(r'<div role="tabpanel" id="tech-spec-([\w-]+)"[^>]*>(.*?)(?=<div role="tabpanel"|</section>)',
                         html, re.S):
        if m.group(1) not in SPEC_TABS:
            continue
        # tên thông số không được vượt qua </p> của chính nó (tên + hình minh hoạ, không có chữ -> bỏ, không ghép nhầm)
        for p in re.finditer(r'data-testid="subhead-sm"[^>]*>((?:(?!</p>).)*)</p>\s*'
                             r'<p data-testid="para-sm-pt-text"[^>]*>((?:(?!</p>).)*)</p>', m.group(2), re.S):
            name = clean_text(p.group(1))
            val = re.sub(r"<br\s*/?>", "; ", p.group(2))
            val = _html.unescape(_html.unescape(re.sub(r"<[^>]+>", "", val)))   # trang mã hoá 2 lần (&amp;#x27;)
            val = re.sub(r"\s*;\s*(;\s*)*", "; ", re.sub(r"\bLearn more\b", "", val))
            val = re.sub(r"\s{2,}", " ", val).strip(" ;")
            if name and val:
                out.append((name, val))
    return out


def _analytics(html, group_id=""):
    """Dữ liệu analytics của ĐÚNG sản phẩm trang (item_id_name = productGroupID): item_category ("All In One"),
    item_list_name ("Speakers"), msrp, price. Mảng items khác (gợi ý, bộ sản phẩm) không lấy nhầm."""
    t = html.replace('\\"', '"')
    found = []
    for m in re.finditer(r'"items":\[(\{"item_name".*?\})\]', t):
        for obj in re.findall(r'\{("item_name".*?)\}', m.group(1)):
            out = {}
            for k, v in re.findall(r'"(\w+)":("(?:[^"\\]|\\.)*"|[\d.]+|null)', obj):
                out[k] = v.strip('"') if v.startswith('"') else (None if v == "null" else to_price(v))
            found.append(out)
    return next((x for x in found if group_id and x.get("item_id_name") == group_id), found[0] if found else {})


def _same_page(a, b):
    """So 2 link bỏ http(s), www, chữ hoa, dấu / cuối."""
    norm = lambda u: re.sub(r"^https?://(www\.)?", "", (u or "").split("?")[0].split("#")[0]).rstrip("/").lower()   # noqa: E731
    return norm(a) == norm(b)


def _kind(text):
    t = f" {text.lower()} "
    return next((k for w, k in KINDS if w in t), "")


def _offer(v):
    o = v.get("offers") or {}
    o = o[0] if isinstance(o, list) and o else o
    avail = str(o.get("availability") or "").rsplit("/", 1)[-1].lower()
    return to_price(o.get("price")), avail in IN_STOCK


def scrape(f, url):
    page_url = url.split("?")[0].split("#")[0]
    html = f.get(page_url)
    ld = find_ld(html, "ProductGroup") or find_ld(html, "Product")
    if not ld:
        raise SkipProduct("Trang không có dữ liệu sản phẩm (có thể không phải trang sản phẩm)")
    name = clean_text(ld.get("name") or "")
    # Hàng hãng tân trang (link ...-b-stock, tên "Refurbished Era 100"): vẫn list, tình trạng không phải New
    refurb = bool(re.search(r"(?i)refurbish|b-stock", name + " " + page_url))
    if re.search(r"(?i)gift card", name):
        raise SkipProduct("Thẻ quà tặng — không list")

    vlist = ld.get("hasVariant") or [ld]
    variants, color_pages, color_img = [], {}, {}
    for v in vlist:
        price, avail = _offer(v)
        color = clean_text(v.get("color") or "") or clean_text((v.get("name") or "").rsplit(" - ", 1)[-1]
                                                                if " - " in (v.get("name") or "") else "")
        variants.append({"color": color, "size": "", "price": price, "list_price": None, "available": avail,
                         "id": v.get("sku") or v.get("gtin13") or color})
        if color and v.get("url"):
            color_pages[color] = v["url"].split("?")[0]
        if color and v.get("image"):
            color_img[color] = v["image"] if isinstance(v["image"], str) else (v["image"] or [""])[0]

    # Ảnh: trang đang mở là của màu có link trùng (hoặc màu mặc định ld.url); màu khác tải trang riêng (2 luồng)
    here = (ld.get("url") or page_url).split("?")[0]
    imgs = {c: _gallery(html) for c, u in color_pages.items() if _same_page(u, page_url) or _same_page(u, here)}

    def load(item):
        c, u = item
        try:
            return c, _gallery(f.get(u))
        except SkipProduct as e:
            if "Đã dừng" in str(e):
                raise
            f.log(f"  ⚠ không tải được trang màu {c}: {e}", "warn")
            return c, []

    todo = [(c, u) for c, u in color_pages.items() if c not in imgs]
    with ThreadPoolExecutor(2) as ex:
        imgs.update(dict(ex.map(load, todo)))
    # Trang màu không đọc được ảnh -> ít nhất có ảnh chính của màu đó trong ld+json
    colors = [{"name": c, "images": imgs.get(c) or ([color_img[c]] if color_img.get(c) else [])}
              for c in dict.fromkeys(v["color"] for v in variants if v["color"])]
    common = [] if colors else (_gallery(html) or ([ld["image"]] if ld.get("image") else []))

    specs = _specs(html)
    details = {}
    for k, v in specs:
        if k in ("Dimensions", "Weight", "Finish") and k not in details:
            details[k] = v
    spec_lines = [f"{k}: {v}" for k, v in specs if k not in details and k != "Colors"]
    desc = clean_text(_html.unescape(ld.get("description") or ""))
    bullets = make_bullets([desc] + spec_lines, BRANDS)

    ga = _analytics(html, ld.get("productGroupID") or "")
    # nhóm rộng ("Speakers") để cuối: tool hỏi eBay Category bằng tên + nhóm cuối -> ra Home Speakers & Subwoofers
    raw = [g.strip() for x in (ga.get("item_category"), ga.get("item_list_name")) for g in str(x or "").split(",")]
    groups = [x for x in dict.fromkeys(raw)   # "All In One,Bundle" -> 2 nhóm
              if x and "refurb" not in x.lower()]   # nhóm "Certified Refurbished" không nói là loại hàng gì
    # Bộ sản phẩm: ld+json chỉ ghi "Immersive Set with Beam Ultra", analytics ghi đủ "…, Sub Mini & Era 100"
    full = clean_text(ga.get("item_name") or "")
    if full and len(full) > len(name) and full.lower().startswith(name.lower()):
        name = full
    title = strip_brand(name, *BRANDS)
    # Hàng tân trang: KHÔNG ghi "Refurbished" vào tiêu đề (tình trạng nằm ở Condition + ghi chú trong mô tả)
    title = re.sub(r"(?i)\b(certified\s+)?refurbished\s*", "", title).strip()
    model = title
    gtext = " ".join(groups).lower()
    is_set = "set" in gtext.split() or "sets" in gtext.split() or "bundle" in gtext
    if "accessor" in gtext:
        kind = ""   # phụ kiện (lưới loa, đệm tai, giá treo, pin...): tên đã tả đúng món, không ghép thêm loại loa
    elif re.search(r"(?i)^sub\b|\bsub (mini|\d)", model):
        kind = "Wireless Subwoofer"   # "Sub Mini" / "Sub 4" nằm trong nhóm Home Theater -> nhận theo tên trước
    elif is_set:
        # bộ có soundbar (Arc / Beam / Ray) -> dàn xem phim; còn lại (vd 2 loa Play) -> bộ loa
        kind = ("Home Theater System" if re.search(r"(?i)\b(arc|beam|ray|sub)\b|home theater", name + " " + gtext)
                else "Speaker Bundle")
    else:
        kind = _kind(" ".join([name] + groups)) if groups else ""
    if kind and kind.split()[-1].lower() not in title.lower():
        title = f"{title} {kind}"
    # Item specifics bắt buộc của nhóm loa / tai nghe: Model (tên mẫu trên site), Type (loại hàng theo nhóm của site)
    if model and not is_set:   # bộ nhiều món không có 1 "Model"
        details["Model"] = model
    if kind:
        details["Type"] = kind.replace("Wireless ", "")
    conn = [label for label, pat in (("Bluetooth", r"(?i)bluetooth"), ("Wi-Fi", r"(?i)\bwi-?fi\b"))
            if any(re.search(pat, f"{k} {v}") for k, v in specs)]
    if conn:
        details["Connectivity"] = ", ".join(conn)
    # Giá gốc (msrp) cao hơn giá đang bán -> đang giảm giá
    msrp = ga.get("msrp")
    for v in variants:
        if msrp and v["price"] and msrp > v["price"] + 0.005:
            v["list_price"] = msrp
    prod = {
        "site": KEY, "code": (ld.get("productGroupID") or product_code(url) or "").upper(),
        "url": page_url, "title": title, "brand": NAME, "bullets": bullets, "details": details,
        "breadcrumbs": groups, "department": "", "size_name": "",
        "title_add": kind if kind and title.endswith(kind) else "",   # bảng Kết quả gom / kiểm tra theo chữ này
        "condition": "refurbished" if refurb else "new",
        "condition_note": ("" if not refurb else REFURB_NOTE_FULL
                           if re.search(r"(?i)all accessories, manuals", html) else REFURB_NOTE_SHORT),
        "colors": colors, "images": common, "variants": variants,
    }
    # Link chọn sẵn màu: /shop/era-100-white
    for c, u in color_pages.items():
        if len(color_pages) > 1 and _same_page(u, page_url):
            prod["url_color"] = c
    return validate(prod)
