"""prAna (prana.com) — SFCC SFRA, site Sites-Prana_US-Site. IP Việt Nam vẫn vào được, giá USD sẵn.

Product-Variation?pid=<mã>&dwvar_<mã>_color=<tên màu>&format=ajax -> JSON. Mỗi màu 1 request (lấy ảnh + giá
của màu đó). Tồn kho từng màu × size nằm sẵn trong product.variantData {"<mã>-<màu>-<size>": {color, size, inventory}}.
Link ghi màu bằng tên (dwvar_2163601_color=Heather%20Grey). Ảnh Amplience media.prana.com/i/prana/<tên>.
Luồng app đọc JSON trong phiên Camoufox của prAna và chờ người dùng xác minh khi cần.
Link có màu chỉ đọc màu đó; dữ liệu được dùng lại theo sản phẩm, màu và Inseam trong cùng lượt.
Giãn cách request bằng MIN_GAP. Tình trạng chờ xác minh được giữ riêng với lỗi sản phẩm.
"""
import re
import urllib.parse

from sites.base import SiteUnavailable, SkipProduct, dedup, strip_brand, validate
from sites.sfcc import amplience, attr, get_variation, make_bullets, sfra_variants, titlecase, usd_price

KEY = "prana"
NAME = "prAna"
URL_RE = re.compile(r"https?://(www\.)?prana\.com/", re.I)
NEEDS_PROXY = False
EXAMPLES = ["https://www.prana.com/p/sunday-river-crewneck-sweater/2163601.html?dwvar_2163601_color=Heather%20Grey"]

HOST = "www.prana.com"
MIN_GAP = 2.0   # giây tối thiểu giữa 2 request tới prAna
SITE = "Sites-Prana_US-Site"
BRANDS = ("prAna", "Prana", "PRANA")
DEPT = {"women": "Women", "womens": "Women", "men": "Men", "mens": "Men"}
PREFERRED_INSEAMS = (8, 3)   # short nhiều chiều dài: 8/10 -> 8, 3/5 -> 3; không có thì theo link
PANTS = re.compile(r"\b(?:pants?|trousers?|jeans?|leggings?|joggers?)\b", re.I)


def product_code(url):
    m = re.search(r"/(\d{5,9})\.html", url) or re.search(r"[?&]pid=(\d{5,9})", url)
    return m.group(1) if m else None


def _images(p):
    imgs = p.get("images") or {}
    lst = imgs.get("hi-res") or imgs.get("large") or imgs.get("small") or []
    # ..._v_pdp là video sản phẩm (không tải được dạng ảnh, báo 404) -> bỏ
    return dedup(amplience(i.get("url")) for i in lst
                 if i.get("url") and not re.search(r"_v_[a-z]+(\?|$)", i["url"]))[:12]


def _attrs(p):
    """{ID: [giá trị]} từ product.attributes (các nhóm productCopy, productFeatures...)."""
    out = {}
    for g in p.get("attributes") or []:
        for a in g.get("attributes") or []:
            vals = [str(v).strip() for v in a.get("value") or [] if str(v).strip()]
            if vals and a.get("ID") not in out:
                out[a["ID"]] = vals
    return out


def _link_colors(first, url_color):
    """Mã màu của màu ghi trên link (so tên / mã, không phân biệt hoa thường). None = đọc mọi màu."""
    want = (url_color or "").strip().casefold()
    if not want:
        return None
    for cv in (attr(first, "color") or {}).get("values") or []:
        names = (cv.get("value"), cv.get("id"), cv.get("displayValue"))
        if any(str(n or "").strip().casefold() == want for n in names):
            return {str(cv.get("value") or cv.get("id"))}
    return None   # màu trên link không còn: đọc mọi màu như cũ


def _variants(f, master, first, color_values=None):
    """(colors, variants, size_name) theo variantData (tồn kho thật). Trả None nếu cấu trúc lạ -> dùng sfra_variants."""
    ca, sa = attr(first, "color"), attr(first, "size")
    vdata = first.get("variantData") or {}
    extras = [a for a in first.get("variationAttributes") or []
              if a.get("attributeId") not in ("color", "size") and a.get("values")]
    if not ca or not sa or not vdata or extras:
        return None
    sizes = [str(v.get("value") or v.get("id")) for v in sa.get("values") or []]
    colors, variants = [], []
    for cv in ca.get("values") or []:
        cid = cv.get("value") or cv.get("id")
        if color_values is not None and str(cid) not in color_values:
            continue
        cname = titlecase(cv.get("displayValue") or cid)
        try:
            p = first if cv.get("selected") else get_variation(f, HOST, SITE, master, {"color": cid})
        except SkipProduct as e:   # 1 màu tải lỗi -> bỏ màu đó, không bỏ cả sản phẩm
            if isinstance(e, SiteUnavailable) or "Đã dừng" in str(e):
                raise
            f.log(f"  ⚠ không đọc được màu {cname}: {e} — bỏ màu này", "warn")
            continue
        if (p.get("price") or {}).get("type") == "range":
            return None   # giá khác nhau theo size -> sfra_variants hỏi giá từng size
        price, lst = usd_price(p.get("price"))
        stock = {str(d.get("size")): (k, d) for k, d in vdata.items() if d.get("color") == cid}
        if not stock:
            continue
        for s in sizes:
            if s not in stock:
                continue
            vid, d = stock[s]
            variants.append({"color": cname, "size": s, "price": price, "list_price": lst,
                             "available": (d.get("inventory") or 0) > 0, "id": vid})
        colors.append({"name": cname, "images": _images(p)})
    return colors, variants, (sa.get("displayName") or "Size")


def _number(v):
    """'08' -> 8; giá trị không phải số (ST, RX, RG) -> None."""
    s = str(v.get("value") or v.get("id") or "")
    return int(s) if re.fullmatch(r"\d{1,2}", s) else None


def _fixed_extras(first, fixed, url_dims):
    """Lựa chọn phụ ngoài màu/size (kiểu dáng Standard/Relaxed, chiều dài ống) không gộp vào ô Size của eBay.

    Chỉ 1 giá trị -> chốt giá trị đó. Nhiều chiều dài số -> chốt 8" (8/10), 3" (3/5); không có thì theo link, không thì giá trị đầu.
    Trả về {"Inseam": "8 in"} / {"Fit": "Standard Fit"} để ghi vào item specifics.
    """
    notes = {}
    for a in first.get("variationAttributes") or []:
        aid = a.get("attributeId") or a.get("id")
        values = a.get("values") or []
        if aid in ("color", "size") or aid in fixed or not values:
            continue
        nums = [_number(v) for v in values]
        if len(values) == 1:
            chosen = values[0]
        elif all(n is not None for n in nums):
            want = str(url_dims.get(aid) or "")
            chosen = (next((v for p in PREFERRED_INSEAMS for v, n in zip(values, nums) if n == p), None)
                      or next((v for v in values if str(v.get("value") or v.get("id")) == want), None)
                      or values[0])
        else:
            continue   # nhiều kiểu dáng chữ (vd Standard / Tall): giữ làm biến thể như cũ
        fixed[aid] = str(chosen.get("value") or chosen.get("id"))
        n = _number(chosen)
        if n is not None:
            notes.setdefault("Inseam", f"{n} in")
        else:
            label = titlecase(str(chosen.get("displayValue") or chosen.get("value") or "")).strip()
            if label:
                notes.setdefault("Fit", label if label.lower().endswith("fit") else f"{label} Fit")
    return notes


def _listing_options(first):
    """Quần: chỉ REG nếu site dùng tên; chiều dài số chọn 32, không có thì 31.

    Size số quần nữ chỉ 2–16. Size chữ và size theo vòng eo không đổi.
    """
    text = " ".join(str(first.get(k) or "") for k in
                    ("productName", "tileName", "variantName", "primaryCategoryID", "parentCategoryID"))
    if not PANTS.search(text):
        return {}, None, ""
    fixed, inseam = {}, ""
    dimensions = (attr(first, "dimension") or {}).get("values") or []
    chosen = next((v for v in dimensions if any(str(v.get(k) or "").lower() in ("rg", "reg", "regular")
                                               for k in ("value", "id", "displayValue"))), None)
    if chosen is None:
        for length in ("32", "31"):
            chosen = next((v for v in dimensions if str(v.get("value") or v.get("id")) == length), None)
            if chosen is not None:
                break
    if chosen is not None:
        value = str(chosen.get("value") or chosen.get("id"))
        fixed["dimension"] = value
        if value in ("32", "31"):
            inseam = f"{value} in"
        else:
            attrs = _attrs(first)
            for text in attrs.get("inseamLength", []) + attrs.get("features", []):
                m = re.search(r'\b(?:RG|REG|REGULAR)\b[\s:=–-]*(\d+(?:\.\d+)?)\s*(?:["″]|in\b|inch)', text, re.I)
                if m:
                    inseam = f"{float(m.group(1)):g} in"
                    break
    sizes = [str(v.get("value") or v.get("id")) for v in (attr(first, "size") or {}).get("values") or []]
    allowed = None
    if sizes and all(s.isdigit() for s in sizes) and any(int(s) <= 16 for s in sizes):
        allowed = {s for s in sizes if 2 <= int(s) <= 16}
    return fixed, allowed, inseam


def scrape(f, url, apply_listing_rules=True, all_colors=False):
    code = product_code(url)
    if not code:
        raise SkipProduct("Không đọc được mã sản phẩm từ link")
    m = re.search(r"dwvar_[^=&]*_color=([^&#]+)", url)
    url_color = urllib.parse.unquote_plus(m.group(1)) if m else ""
    first = get_variation(f, HOST, SITE, code, {"color": url_color} if url_color else None)
    master = first.get("masterPid") or code
    if first.get("online") is False:
        raise SkipProduct("Sản phẩm không còn bán")

    fixed, allowed_sizes, inseam = _listing_options(first) if apply_listing_rules else ({}, None, "")
    pants_fixed = bool(fixed)
    extra_notes = {}
    if apply_listing_rules:
        url_dims = {m.group(1): urllib.parse.unquote_plus(m.group(2))
                    for m in re.finditer(r"dwvar_[^_=&]+_([a-zA-Z]+)=([^&#]+)", url)}
        extra_notes = _fixed_extras(first, fixed, url_dims)
        inseam = inseam or extra_notes.get("Inseam", "")
    # Link có màu -> chỉ đọc màu đó (mỗi màu thêm 1 request, đọc dồn là bị PerimeterX chặn)
    # Đọc trong Camoufox: đọc đủ màu 1 lần, các dòng màu khác của cùng sản phẩm dùng lại (ít lần mở trang hơn)
    all_colors = all_colors or getattr(f, "READ_ALL_COLORS", False)
    color_values = None if all_colors else _link_colors(first, url_color)
    res = _variants(f, master, first, color_values)
    if res:
        colors, variants, size_name = res
        common = []
    else:
        colors, variants, size_name, common = sfra_variants(
            f, HOST, SITE, master, first, images_fn=_images,
            fixed_attributes=fixed, size_values=allowed_sizes, color_values=color_values)
    if allowed_sizes is not None:
        variants = [v for v in variants if (v["size"].split() or [""])[0] in allowed_sizes]
    if not variants and (fixed or allowed_sizes is not None):
        raise SkipProduct("Không có biến thể phù hợp quy tắc prAna: chiều dài REG/32/31, size số 2–16")

    a = _attrs(first)
    details = {}
    if a.get("fabric"):
        details["Fabric"] = "; ".join(a["fabric"])
    if a.get("apparelFit"):
        details["Fit"] = a["apparelFit"][0]
    if a.get("activity"):
        details["Activity"] = ", ".join(a["activity"])
    features = [x.strip() for v in a.get("features") or [] for x in v.split("|") if x.strip()]
    bullets = make_bullets([first.get("productPitch") or "; ".join(a.get("productPitch") or []),
                            first.get("longDescription")] + features, BRANDS)
    if inseam:
        bullets = [b for b in bullets if not re.match(r"(?i)^inseams?\s*:", b)]
        details["Inseam"] = inseam
    elif pants_fixed:
        bullets = [b for b in bullets if not re.match(r"(?i)^inseams?\s*:", b)]
        details["Length"] = "Regular"
    if extra_notes.get("Fit") and not details.get("Fit"):
        details["Fit"] = extra_notes["Fit"]

    # primaryCategoryID: "women-tops-sweaters-and-hoodies" hoặc "the-zion-family-collection-women"
    cat_id = (first.get("primaryCategoryID") or first.get("parentCategoryID") or "").lower()
    parts = [x for x in cat_id.split("-") if x]
    hint = parts + [x for v in a.get("sizeFitAssets") or [] for x in re.split(r"[-\s]+", v.lower())]
    dept = next((DEPT[x] for x in hint if x in DEPT), "")
    rest = [x for x in parts if x not in DEPT and x != "the"]
    crumbs = [dept] if dept else []
    if rest:
        crumbs.append(" ".join(rest).replace(" and ", " & ").title())

    path = (first.get("selectedProductUrl") or "").split("?")[0]
    title = strip_brand(first.get("tileName") or first.get("variantName") or first.get("productName") or "", *BRANDS)
    title = re.sub(r"\s*[™®©]\s*", " ", title).strip()
    prod = {
        "site": KEY, "code": master,
        "url": f"https://{HOST}{path}" if path else url.split("?")[0],
        "title": title, "brand": NAME, "bullets": bullets, "details": details,
        "breadcrumbs": crumbs, "department": dept, "size_name": size_name or "Size",
        "colors": colors, "images": common, "variants": variants,
    }
    if url_color:
        prod["url_color"] = titlecase(url_color)
    if color_values is not None:
        prod["partial_colors"] = True   # chỉ có màu của link: link khác cùng mã phải đọc lại, không dùng cache
    if inseam:
        prod["fixed_inseam"] = inseam
    return validate(prod)


STORED_LABEL = re.compile(r"(\S+) (\S+)")


def fix_stored(prod, url):
    """Dữ liệu đã đọc trước bản sửa: size dạng "M Standard" / "33 8" (lựa chọn phụ gộp vào size).

    Chuyển sang cách mới không cần đọc lại site: chốt 1 lựa chọn phụ như _fixed_extras, size còn "M" / "33",
    nhãn nguồn giữ ở _site_size. Trả về {nhãn cũ: nhãn mới} của các biến thể giữ lại, hoặc None nếu không đổi.
    """
    vs = prod.get("variants") or []
    if not vs or any("_site_size" in v for v in vs):
        return None
    parts = [STORED_LABEL.fullmatch(str(v.get("size") or "")) for v in vs]
    if not all(parts):
        return None
    suffixes = list(dict.fromkeys(m.group(2) for m in parts))
    numeric = all(re.fullmatch(r"\d{1,2}", x) for x in suffixes)
    if len(suffixes) == 1:
        chosen = suffixes[0]
    elif numeric:
        m = re.search(r"dwvar_[^_=&]+_dimension=(\d{1,2})(?:&|#|$)", url or "")
        link = int(m.group(1)) if m else None
        chosen = (next((x for p in PREFERRED_INSEAMS for x in suffixes if int(x) == p), None)
                  or next((x for x in suffixes if int(x) == link), None) or suffixes[0])
    else:
        return None   # nhiều kiểu dáng chữ: giữ như cũ
    mapping, kept = {}, []
    for v, m in zip(vs, parts):
        if m.group(2) != chosen:
            continue
        mapping[v["size"]] = m.group(1)
        kept.append(dict(v, size=m.group(1), _site_size=v["size"]))
    prod["variants"] = kept
    details = prod.setdefault("details", {})
    if numeric:
        prod["fixed_inseam"] = details["Inseam"] = f"{int(chosen)} in"
        prod["bullets"] = [b for b in prod.get("bullets") or [] if not re.match(r"(?i)^inseams?\s*:", b)]
    elif not details.get("Fit"):
        label = titlecase(chosen)
        details["Fit"] = label if label.lower().endswith("fit") else f"{label} Fit"
    return mapping


def scrape_for_tracking(f, url):
    """Đọc đủ màu, chiều dài, size nguồn để kiểm tra đúng cả các listing đã xuất trước quy tắc mới."""
    return scrape(f, url, apply_listing_rules=False, all_colors=True)
