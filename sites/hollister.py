"""Hollister (hollisterco.com) — trang sản phẩm có sẵn dữ liệu trong window['APOLLO_STATE__product-mfe-...'].

ROOT_QUERY.collection(...).collection:
  products[]  : mỗi "màu" (swatch) là 1 mã sản phẩm riêng: productId, productName, swatchName (tên màu),
                defaultSwatchSequence, longDescription, fabricDetails, careDetailsList, orderedImages, garmentMeasurements
  skus[]      : productId, sizePrimary ("27_p" / "M_p"), sizeSecondary ("Regular_s" / "32_s"), inventory,
                inventoryStatus, prices.list.listPriceUSD / offerPriceUSD
Link: .../p/<tên>-<mã>?seq=09 — seq là swatch đang chọn (số trên link có thể là mã chung của nhóm, vd henley);
      trang trả productId của đúng swatch đó. Trang gom cả mẫu khác (vd "Logo Graphic Henley") -> chỉ lấy các
      swatch cùng tên mẫu với link.
IP Việt Nam bị chuyển sang store quốc tế (/shop/wd/, có thể khác giá Mỹ) -> tự chuyển proxy để lấy store Mỹ.
"""
import json
import re

from sites.base import SkipProduct, dedup, strip_brand

KEY = "hollister"
NAME = "Hollister"
URL_RE = re.compile(r"https?://(www\.)?hollisterco\.com/shop/[a-z]{2}/p/[\w-]+-\d{6,10}", re.I)
NEEDS_PROXY = False
EXAMPLES = ["https://www.hollisterco.com/shop/us/p/leopard-print-icon-henley-63757420?seq=21"]
BASE = "https://www.hollisterco.com"
BRANDS = ("Hollister Co.", "Hollister", "Gilly Hicks")
STATE_RE = re.compile(r"window\['APOLLO_STATE__product-mfe-web-service-ProductPageFrontend-config'\]\s*=\s*")
COLOR_WORD = re.compile(
    r"(?i)\b(black|white|gr[ae]y|navy|blue|red|green|pink|purple|yellow|orange|brown|beige|ivory|cream|gold|silver|"
    r"tan|khaki|olive|lime|teal|burgundy|maroon|charcoal|denim|indigo|lavender|lilac|mint|coral|rust|camel|taupe|"
    r"sage|mauve|peach|sky|turquoise|aqua|wine|plum|oatmeal|ecru|multi\w*)\b")
DEPT = {"women's": "Women", "womens": "Women", "girls": "Women", "men's": "Men", "mens": "Men", "guys": "Men"}


def product_code(url):
    m = re.search(r"/p/[\w-]*?-(\d{6,10})(?:[/?#]|$)", url)
    return m.group(1) if m else None


def _seq(url):
    m = re.search(r"[?&]seq=(\d+)", url)
    return m.group(1) if m else None


def _page_url(url):
    """Link chuẩn ở store Mỹ, giữ seq (màu đang chọn)."""
    m = re.search(r"/p/([\w-]+-\d{6,10})", url)
    if not m:
        raise SkipProduct("Link Hollister không đúng dạng .../shop/us/p/<tên>-<mã>")
    seq = _seq(url)
    return f"{BASE}/shop/us/p/{m.group(1)}" + (f"?seq={seq}" if seq else "")


def _state(html):
    m = STATE_RE.search(html or "")
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[m.end():])
    except ValueError:
        return None
    return obj


def _load(f, url):
    """(state, html) của store Mỹ; IP gốc bị chuyển sang store quốc tế thì chuyển proxy rồi tải lại."""
    page = _page_url(url)
    for _ in range(3):
        st = _state(f.get(page))
        if not st:
            raise SkipProduct("Không đọc được dữ liệu sản phẩm Hollister (trang đổi cấu trúc?)")
        if (st.get("store") or "").lower() in ("h-us", "") or (st.get("country") or "").upper() == "US":
            return st
        # store quốc tế (vd h-wd khi vào từ Việt Nam): giá / tồn kho có thể khác Mỹ
        if f.proxy_on:
            f.pool.rotate()
        elif not f.switch_to_proxy(f"Hollister chuyển sang store {st.get('store')}"):
            raise SkipProduct("Hollister chuyển IP Việt Nam sang store quốc tế (giá có thể khác Mỹ) — "
                              "dán proxy Mỹ trong Cài đặt rồi chạy lại")
    raise SkipProduct("Proxy không vào được store Mỹ của Hollister (vẫn bị chuyển sang store quốc tế)")


def _size_text(v):
    return re.sub(r"_[ps]$", "", str(v or "")).strip()


def _size_label(sku):
    """'M' / '27' (Regular) / '27 Short' / '30W x 32L' (quần nam eo x dài bằng số)."""
    p, s = _size_text(sku.get("sizePrimary")), _size_text(sku.get("sizeSecondary"))
    if p.upper() in ("ONE SIZE", "OS", "ONESIZE"):
        return ""   # bộ kit / phụ kiện 1 cỡ: không có lựa chọn cỡ
    if not s:
        return p
    if s.isdigit() and p.isdigit():
        return f"{p}W x {s}L"
    if s.lower() == "regular":
        return p
    return f"{p} {s}"   # Short / Long -> nhóm size (link không ghi -> chỉ lấy Regular)


def _images(p):
    imgs = p.get("orderedImages") or []
    if not imgs:
        sets = p.get("imageSet") or {}
        imgs = (sets.get("model") or []) + (sets.get("prod") or [])
    return dedup(i.get("zoomImageSrc") or i.get("largeMainImageSrc") or i.get("mainImageSrc") for i in imgs
                 if i.get("type") in (None, "prod", "model"))[:12]


def _price(sku):
    lst = ((sku.get("prices") or {}).get("list") or {})
    return lst.get("offerPriceUSD") or lst.get("listPriceUSD"), lst.get("listPriceUSD")


def scrape(f, url):
    st = _load(f, url)
    rq = (st.get("CACHE") or {}).get("ROOT_QUERY") or {}
    col = next((v.get("collection") for k, v in rq.items() if k.startswith("collection(") and isinstance(v, dict)), None)
    if not col or not col.get("products"):
        raise SkipProduct("Không đọc được danh sách màu / size của sản phẩm Hollister")
    products = col["products"]
    sel_id = str(st.get("productId") or "")
    seq = _seq(url)
    me = (next((p for p in products if seq and p.get("defaultSwatchSequence") == seq), None)
          or next((p for p in products if str(p.get("productId")) == sel_id), None) or products[0])
    name = me.get("productNameEn") or me.get("productName") or ""
    # chỉ các swatch cùng mẫu với link (trang gom cả mẫu khác, giá khác, vào chung 1 trang)
    same = [p for p in products if (p.get("productNameEn") or p.get("productName")) == name] or [me]

    # tên màu: swatchName ("tan stripe"); quần jean đặt tên theo độ wash ("light", "medium") -> ghép họ màu của
    # trang ("Light Blue") để ô Color là màu thật; 2 swatch trùng tên thì thêm số thứ tự
    # màu của link xét trước -> luôn giữ tên gốc (không thành "Dark Blue 4" trong tiêu đề); màu trùng tên khác mới đánh số
    color_of, seen = {}, {}
    for p in sorted(same, key=lambda x: x is not me):
        sw, fam = (p.get("swatchName") or "").strip(), (p.get("colorFamily") or "").strip()
        if sw and fam and not COLOR_WORD.search(sw):
            sw = f"{sw} {fam}"
        c = (sw or fam).title()
        seen[c] = seen.get(c, 0) + 1
        color_of[str(p["productId"])] = c if seen[c] == 1 else f"{c} {seen[c]}"

    order = {s.get("value"): i for i, s in enumerate((col.get("primarySizeArray") or []) + (col.get("secondarySizeArray") or []))}
    variants = []
    for sku in col.get("skus") or []:
        pid = str(sku.get("productId") or "")
        if pid not in color_of:
            continue
        price, reg = _price(sku)
        if not price:
            continue
        variants.append({
            "_rank": (order.get(sku.get("sizeSecondary"), 0), order.get(sku.get("sizePrimary"), 999)),
            "color": color_of[pid], "size": _size_label(sku), "price": float(price),
            "list_price": float(reg) if reg and float(reg) > float(price) + 0.001 else None,
            "available": (sku.get("inventory") or 0) > 0 and (sku.get("inventoryStatus") or "").lower() != "unavailable",
            "id": sku.get("shortSku") or "",
        })
    if not variants:
        raise SkipProduct("Sản phẩm không có size nào đang bán")
    corder = {color_of[str(p["productId"])]: i for i, p in enumerate(same)}
    variants.sort(key=lambda v: (corder.get(v["color"], 99),) + v["_rank"])
    for v in variants:
        del v["_rank"]

    desc = (me.get("longDescription") or "").strip()
    bullets = [s.strip() for s in re.split(r"(?<=[.!?])\s+", desc) if s.strip() and s.strip().lower() != "imported."]
    details = {}
    if me.get("fabricDetails"):
        details["Material"] = re.sub(r"\s*/\s*", "; ", me["fabricDetails"]).title()
    care = [c.get("value", "").strip().rstrip(",").capitalize() for c in me.get("careDetailsList") or [] if c.get("value")]
    if care:
        details["Care"] = "; ".join(care)
    fit = re.search(r"\b(Slim|Relaxed|Oversized|Regular|Loose|Boxy|Skinny|Straight|Baggy|Super Baggy|Wide)[- ]Fit\b", desc, re.I)
    if fit:
        details["Fit"] = fit.group(1).title()
    gm = me.get("garmentMeasurements") or {}
    if gm.get("baseInseamRegularMeasurement"):   # quần nữ Short / Regular / Long: chỉ bán Regular -> Inseam của Regular
        details["Inseam"] = f"{float(gm['baseInseamRegularMeasurement']):g} in"
    if "imported" in desc.lower():
        details["Country of Origin"] = "Imported"
    crumbs = [b.get("categoryName_EN") or b.get("categoryName") for b in me.get("breadcrumbCategoryHierarchy") or []]
    crumbs = [c for c in crumbs if c and c.lower() not in ("hol", "hollister")]
    dept = DEPT.get((me.get("departmentName") or "").lower(), "")
    if not dept:
        dept = {"F": "Women", "M": "Men"}.get((me.get("gender") or "").upper(), "")

    colors = [{"name": color_of[str(p["productId"])], "images": _images(p)} for p in same]
    out = {
        "site": KEY,
        "code": str(me.get("productId")),
        "url": _page_url(url),
        "title": strip_brand(name, *BRANDS),
        "brand": NAME,
        "bullets": bullets,
        "details": details,
        "breadcrumbs": crumbs,
        "department": dept,
        "size_name": "Size",
        "colors": colors,
        "images": [],
        "variants": variants,
    }
    if len(colors) > 1:
        out["url_color"] = color_of[str(me["productId"])]   # màu của link chọn sẵn, các màu khác vẫn hiện để chọn
    return out
