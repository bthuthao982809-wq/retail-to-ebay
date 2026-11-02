"""Tommy Bahama (tommybahama.com) — SAP Hybris. Chặn IP Việt Nam (403) -> tool tự chuyển proxy Mỹ.

Mỗi MÀU là 1 trang riêng: /p/ST228252-18406 (ST228252 = mã kiểu, 18406 = mã màu). Trang màu có ld+json
ProductGroup: tên, mô tả, ảnh của màu đó, hasVariant = từng size kèm giá (offers.price) + tình trạng
(offers.availability). Các màu khác nằm trong ô màu data-swatch-style-code / data-swatch-name -> tải thêm
mỗi màu 1 trang (song song 3 luồng).
Bản Big & Tall là mã kiểu khác (BT...) -> coi là sản phẩm riêng (dán link riêng).
Ảnh scene7: thay tham số preset bằng ?wid=1600&hei=1600&fmt=jpg&qlt=95 -> JPEG 1600x1600, không cần proxy.
"""
import html as _html
import re
from concurrent.futures import ThreadPoolExecutor

from sites.base import SkipProduct, clean_text, dedup, find_ld, strip_brand, strip_brand_desc, to_price

KEY = "tommybahama"
NAME = "Tommy Bahama"
URL_RE = re.compile(r"https?://(www\.)?tommybahama\.com/.*?/p/[A-Z]{1,3}\d", re.I)
NEEDS_PROXY = True
EXAMPLES = ["https://www.tommybahama.com/en/en/Emfielder-Icon-Long-Sleeve-Polo/p/ST228252-18406"]

BRAND = "Tommy Bahama"
IN_STOCK = ("instock", "limitedavailability", "onlineonly", "preorder", "backorder")
DEPT = {"mens": "Men", "womens": "Women", "kids": "", "home": ""}


def product_code(url):
    """Mã kiểu (ST228252) — phần mã màu sau dấu "-" không tính."""
    m = re.search(r"/p/([A-Z]{1,3}\d+)", url, re.I)
    return m.group(1).upper() if m else None


def _img(u):
    """Ảnh scene7 bản lớn JPEG."""
    return re.sub(r"\?.*$", "", u) + "?wid=1600&hei=1600&fmt=jpg&qlt=95"


def _data(h, name):
    m = re.search(rf'tbr-pdp--data="{name}">([^<]*)<', h)
    return _html.unescape(m.group(1)).strip() if m else ""


def _color_page(f, url):
    """1 trang màu -> {name, code, images, sizes:[{size, price, avail, sku}]}"""
    h = f.get(url)
    g = find_ld(h, "ProductGroup")
    if not g or not g.get("hasVariant"):
        raise SkipProduct("Trang không có dữ liệu size (ld+json)")
    sizes = []
    for v in g["hasVariant"]:
        o = v.get("offers") or {}
        if isinstance(o, list):
            o = o[0] if o else {}
        if (o.get("priceCurrency") or "USD").upper() != "USD":
            raise SkipProduct("Site không trả giá USD")
        sizes.append({"size": str(v.get("size") or "").strip(), "price": to_price(o.get("price")),
                      "avail": (o.get("availability") or "").rsplit("/", 1)[-1].lower() in IN_STOCK,
                      "sku": str(v.get("sku") or "")})
    imgs = g.get("image") or []
    imgs = [imgs] if isinstance(imgs, str) else imgs
    return {"html": h, "ld": g, "name": str(g.get("color") or "").strip(), "code": str(g.get("sku") or ""),
            "images": dedup(_img(u) for u in imgs)[:12], "sizes": sizes}


def scrape(f, url):
    style = product_code(url)
    if not style:
        raise SkipProduct("Không đọc được mã sản phẩm từ link (/p/ST...)")
    first = _color_page(f, url.split("?")[0])
    h, g = first["html"], first["ld"]

    # Các màu khác của cùng kiểu (ô màu trên trang)
    swatches = re.findall(r'data-swatch-style-code="([^"]+)"\s+data-swatch-name="([^"]+)"', h)
    others = [(code, _html.unescape(name)) for code, name in swatches if code.upper() != first["code"].upper()]
    path = re.sub(r"/p/[^/?#]+.*$", "", url.split("?")[0])

    def load(item):
        code, name = item
        try:
            page = _color_page(f, f"{path}/p/{code}")
            page["name"] = page["name"] or name
            return page
        except SkipProduct as e:
            if "Đã dừng" in str(e):
                raise
            f.log(f"  ⚠ màu {name}: {e}", "warn")
            return None

    with ThreadPoolExecutor(3) as ex:
        pages = [first] + [p for p in ex.map(load, others) if p]
    # Giữ thứ tự ô màu trên site
    order = {code.upper(): i for i, (code, _) in enumerate(swatches)}
    pages.sort(key=lambda p: order.get(p["code"].upper(), 999))

    colors, variants = [], []
    for p in pages:
        colors.append({"name": p["name"], "images": p["images"]})
        for s in p["sizes"]:
            if not s["price"]:
                continue
            variants.append({"color": p["name"], "size": s["size"], "price": s["price"], "list_price": None,
                             "available": s["avail"], "id": s["sku"]})

    m = re.search(r'"family"\s*:\s*"([^"]+)"', h)
    family = m.group(1) if m else ""
    dept = DEPT.get(family.split("|")[0].lower(), "") if family else ""
    cats = [c for c in re.findall(r'"category[23]"\s*:\s*"([^"]+)"', h)]
    crumbs = dedup([dept] + [c.replace("_", " ").title() for c in cats if "collection" not in c])
    fit = _data(h, "activeFit")
    desc = strip_brand_desc(clean_text(g.get("description") or ""), BRAND)
    bullets = [x.strip() for x in re.split(r"(?<=[.!?])\s+", desc) if x.strip()]
    details = {"Fit": fit} if fit else {}
    return {
        "site": KEY,
        "code": style,
        "url": url,
        "title": strip_brand(clean_text(g.get("name") or ""), BRAND),
        "brand": BRAND,
        "bullets": bullets,
        "details": details,
        "breadcrumbs": [c for c in crumbs if c],
        "department": dept,
        "size_name": "Size",
        "colors": colors,
        "images": [],
        "variants": variants,
        "url_color": first["name"],   # màu trong link -> tool chọn sẵn màu này
    }
