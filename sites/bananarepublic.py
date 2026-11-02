"""Banana Republic (bananarepublic.gap.com) — site Next.js của Gap Inc.

Trang sản phẩm tải bằng HTTP thường (curl_cffi) là đủ: dữ liệu đầy đủ nằm trong luồng RSC
(self.__next_f.push([1,"..."])) — 1 object "data" giống API product-discovery của Gap:
  styles{style_id: tên, copy_headers (mô tả theo mục), merchandise_hierarchy}
  customer_choices{cc_id: màu, images[] (mỗi ảnh có nhiều crop, crop ZOOM 1500x2000)}
  variants{REGULAR|PETITE|TALL|LONG...: customer_choices{cc_id: skus[] (size, giá USD, còn hàng)}}
pid trên link = mã màu (cc, 9-10 số) = style + 3 số màu; code = style (chung mọi màu).
1 trang có thể gộp thêm style khác (includes_merged_style, vd bản vải nhung + bản vải thường,
giá khác nhau) — chỉ lấy các màu thuộc đúng style của link.
Mỗi sản phẩm: 1 request.
"""
import json
import re

from sites.base import SkipProduct, dedup, find_ld, strip_brand, strip_brand_desc, to_price

KEY = "bananarepublic"
NAME = "Banana Republic"
URL_RE = re.compile(r"bananarepublic\.gap\.com/.*[?&]pid=\d+", re.I)
NEEDS_PROXY = False
BASE = "https://bananarepublic.gap.com"
BRANDS = ("Banana Republic", "BR")


def _pid(url):
    m = re.search(r"[?&]pid=(\d+)", url)
    return m.group(1) if m else None


def product_code(url):
    """pid 9-10 số = style + 3 số màu -> style; pid ngắn (đã là style) giữ nguyên."""
    pid = _pid(url)
    if not pid:
        return None
    return pid[:-3] if len(pid) >= 9 else pid


def _rsc_text(html):
    """Ghép các đoạn self.__next_f.push([1,"..."]) thành chuỗi RSC đã giải mã."""
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html, re.S):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            pass
    return "".join(parts)


def _product_data(html):
    """Object "data" chứa styles + customer_choices + variants (bản đầy đủ đầu tiên)."""
    s = _rsc_text(html)
    dec = json.JSONDecoder()
    for m in re.finditer(r'"data":\{"brand"', s):
        try:
            obj, _ = dec.raw_decode(s[m.end() - len('{"brand"'):])
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("variants"), dict) and isinstance(obj.get("styles"), dict):
            return obj
    return None


def _abs(u):
    if not u:
        return None
    if u.startswith("http"):
        return u
    return BASE + "/" + u.lstrip("/")


def _images(cc):
    """Ảnh của 1 màu: mỗi ảnh lấy crop rộng nhất (ZOOM 1500px), theo thứ tự sequence_number."""
    out = []
    for img in sorted(cc.get("images") or [], key=lambda i: i.get("sequence_number") or 0):
        crops = [c for c in img.get("crops") or [] if c.get("url") and (c.get("legacy_type") or "") not in ("S", "SWATCH")]
        if crops:
            best = max(crops, key=lambda c: c.get("width") or 0)
            out.append(_abs(best["url"]))
        elif img.get("pristine_url"):
            out.append(_abs(img["pristine_url"]))
    if not out:   # dữ liệu cũ: legacyImages / images_P01
        p = cc.get("images_P01") or {}
        out = [_abs(p.get("ZOOM") or p.get("VIEW_LARGE_IMAGE") or cc.get("largeImagePath"))]
    return dedup(out)[:12]


def _cc_ref_id(ref):
    """color_groups chứa chuỗi tham chiếu '$83:...:customer_choices:<id>' hoặc chính object."""
    if isinstance(ref, dict):
        return ref.get("customer_choice_id")
    if isinstance(ref, str):
        return ref.rsplit(":", 1)[-1]
    return None


def _color_order(d):
    """Thứ tự màu như trên site: nhóm regular rồi markdown, theo từng kiểu dáng (display key trước)."""
    keys = list(d["variants"])
    dk = d.get("display_variant_key")
    if dk in keys:
        keys.remove(dk)
        keys.insert(0, dk)
    order = []
    for k in keys:
        groups = d["variants"][k].get("color_groups") or {}
        for g in ("regular", "markdown"):
            for row in groups.get(g) or []:
                for ref in (row if isinstance(row, list) else [row]):
                    order.append(_cc_ref_id(ref))
        order.extend(d["variants"][k].get("customer_choices") or {})
    order.extend(d.get("customer_choices") or {})
    return dedup(order)


def _split_details(headers):
    """copy_headers -> (bullets mô tả, dict thông số)."""
    bullets, details = [], {}
    for h in headers or []:
        lines = [re.sub(r"\s+", " ", b).strip() for b in h.get("bullets") or [] if b and b.strip()]
        lines = [x for x in lines if not re.match(r"(?i)^product\s*#", x) and "By purchasing this product" not in x]
        if h.get("description"):
            lines.insert(0, h["description"].strip())
        hid = (h.get("id") or "").lower()
        if hid == "overview":
            bullets.extend(lines)
        elif hid in ("fitandsizing", "fit"):
            if lines:
                details["Fit"] = " ".join(lines)
        elif hid == "fabric":
            mat, care, origin, other = [], [], [], []
            for x in lines:
                if re.search(r"(?i)\b(imported|made in)\b", x) and len(x) < 40:
                    origin.append(x.rstrip("."))
                elif re.search(r"(?i)\b(wash|dry clean|clean only|tumble|iron|bleach|hand wash|spot clean)\b", x):
                    care.append(x)
                elif "%" in x or re.search(r"(?i)\b(shell|lining|upper|sole|leather|suede|cotton|wool|silk|linen|cashmere)\b", x):
                    mat.append(x)
                else:
                    other.append(x)
            if mat:
                details["Material"] = " ".join(mat)
            if care:
                details["Care"] = " ".join(care)
            if origin:
                details["Origin"] = ", ".join(origin)
            if other:
                details["Fabric notes"] = " ".join(other)
        elif lines:
            details[h.get("label") or h.get("name") or hid] = " ".join(lines)
    return [strip_brand_desc(x, *BRANDS[:1]) for x in dedup(bullets)], details


def _department(crumbs, style):
    for c in crumbs[:1]:
        lc = c.lower()
        if lc in ("women", "womens", "women's"):
            return "Women"
        if lc in ("men", "mens", "men's"):
            return "Men"
    text = " ".join([(style.get("merchandise_hierarchy") or {}).get("division_description") or "",
                     (style.get("merchandise_hierarchy") or {}).get("department_description") or "",
                     style.get("web_product_type") or ""]).lower()
    if re.search(r"\bwom[ae]ns?\b|\bw\s", text):
        return "Women"
    if re.search(r"\bm[ae]ns?\b|\bm\s", text):
        return "Men"
    return ""


def scrape(f, url):
    pid = _pid(url)
    if not pid:
        raise SkipProduct("Link không có pid sản phẩm")
    page_url = f"{BASE}/browse/product.do?pid={pid}"
    html = f.get(page_url)
    d = _product_data(html)
    if not d:
        raise SkipProduct("Không đọc được dữ liệu sản phẩm (trang đổi cấu trúc?)")

    alt = d.get("input_alternate_ids") or {}
    style_id = alt.get("legacy_style_number") or product_code(url)
    style = d["styles"].get(style_id) or next(iter(d["styles"].values()))
    style_id = style.get("style_id") or style_id

    # breadcrumbs từ ld+json
    crumbs = []
    bc = find_ld(html, "BreadcrumbList")
    for it in (bc or {}).get("itemListElement") or []:
        name = (it.get("item") or {}).get("name") if isinstance(it.get("item"), dict) else it.get("name")
        if name:
            crumbs.append(name)

    bullets, details = _split_details(style.get("copy_headers"))
    title = strip_brand(style.get("description") or "", *BRANDS[:1])

    # tên kiểu dáng (Regular / Petite / Tall / Long ...)
    fit_names = {}
    for vd in d.get("variant_definition") or []:
        for v in vd.get("values") or []:
            fit_names[v.get("id")] = v.get("name") or ""

    # thứ tự kiểu dáng + thứ tự size của site (để sắp variant)
    fit_rank = {vk: i for i, vk in enumerate(d["variants"])}
    size_rank = {}
    for vdata in d["variants"].values():
        for x in vdata.get("size_dimension1_list") or []:
            size_rank.setdefault(x.get("dimension"), x.get("sort_order") or 0)

    ccs = d.get("customer_choices") or {}

    def _mine(cc_id, c):
        """Màu thuộc đúng style của link (bỏ màu của style khác gộp chung trang)."""
        return str(c.get("style_id") or (ccs.get(cc_id) or {}).get("style_id") or style_id) == str(style_id)

    # key kiểu dáng có thể ghép nhiều trục: "REGULAR", "PETITE", 'REGULAR|7"' (Fit|Length).
    # Chỉ ghi vào nhãn size những phần thực sự khác nhau giữa các key của style này; "Regular" luôn bỏ.
    used_keys = [vk for vk, vd in d["variants"].items()
                 if any(_mine(i, c) for i, c in (vd.get("customer_choices") or {}).items())]
    parts = [vk.split("|") for vk in used_keys]
    varying = {i for i in range(max((len(p) for p in parts), default=0))
               if len({p[i] if i < len(p) else "" for p in parts}) > 1}

    # Nhóm chọn phụ cố định của style này (mỗi lựa chọn là 1 mã sản phẩm riêng): Neckline = Crew Neck,
    # Pant Fit = Slim, Shirt Fit = Standard, Length = 7" -> ghi thành thông số để điền item specifics
    for i, vdef in enumerate(d.get("variant_definition") or []):
        vals = {p[i] for p in parts if i < len(p)}
        if i in varying or len(vals) != 1 or vdef.get("id") == "fitVariant" or (vdef.get("name") or "").lower() == "fit":
            continue
        value = fit_names.get(next(iter(vals))) or ""
        dim = (vdef.get("name") or "").strip()
        inch = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(?:"|”|in\.?)', value)
        if dim.lower() == "length" and inch:
            details.setdefault("Inseam", f"{inch.group(1)} in")   # quần short 7" -> Inseam 7 in
        elif dim.lower().endswith(" fit") and value:
            # Pant Fit / Shirt Fit -> Fit; đoạn mô tả dáng sẵn có của site ("Standard Fit: A straight...") đổi tên
            if details.get("Fit") and details["Fit"] != value:
                details.setdefault("Fit & Sizing", details["Fit"])
            details["Fit"] = value
        elif dim and value:
            details.setdefault(dim, value)                        # Neckline -> Neckline

    def _fit_label(vk):
        out = []
        for i, x in enumerate(vk.split("|")):
            if i in varying and x != "REGULAR":
                out.append(fit_names.get(x) or x.title())
        return " ".join(out)

    # 2 màu khác nhau trùng tên (vd "Khaki Beige" giá gốc và bản sale, khác mã) -> màu của link giữ tên,
    # màu kia thêm số ("Khaki Beige 2"); không thì 2 màu bị gộp làm 1, lẫn giá / tồn kho
    name_of, seen_names = {}, {}
    for cc_id in sorted((i for i in ccs if _mine(i, ccs[i])), key=lambda i: str(i) != str(pid)):
        base = (ccs[cc_id].get("description") or "").strip()
        seen_names[base] = seen_names.get(base, 0) + 1
        name_of[cc_id] = base if seen_names[base] == 1 else f"{base} {seen_names[base]}"

    variants, used_cc = [], []
    size_name = ""
    for vk, vdata in d["variants"].items():
        fit = _fit_label(vk)
        lab1 = vdata.get("size_dimension1_label") or ""
        size_name = size_name or lab1
        for cc_id, c in (vdata.get("customer_choices") or {}).items():
            if not _mine(cc_id, c):
                continue
            color = name_of.get(cc_id) or (ccs.get(cc_id) or {}).get("description") or c.get("description") or ""
            for k in c.get("skus") or []:
                p = k.get("price") or {}
                if p.get("currency") and p["currency"] != "USD":
                    raise SkipProduct(f"Giá không phải USD ({p['currency']})")
                price = to_price(p.get("effective_price"))
                reg = to_price((p.get("price_details") or {}).get("regular_price"))
                s1, s2 = k.get("size_dimension1") or k.get("description") or "", k.get("size_dimension2")
                size = f"{s1} x {s2}" if s2 else s1
                if fit:
                    size = f"{size} {fit}".strip()
                variants.append({
                    "_rank": (fit_rank.get(vk, 0), size_rank.get(s1, 999), str(s2 or "")),
                    "color": color, "size": size, "price": price,
                    "list_price": reg if reg and price and reg > price + 0.001 else None,
                    "available": bool(k.get("isInstock")) and not k.get("isOutOfStock"),
                    "id": k.get("sku_id") or "",
                })
            used_cc.append(cc_id)

    if not variants:
        raise SkipProduct("Sản phẩm không có size nào đang bán")

    colors = []
    for cc_id in _color_order(d):
        if cc_id not in used_cc or cc_id not in ccs:
            continue
        name = name_of.get(cc_id) or ccs[cc_id].get("description") or ""
        if any(c["name"] == name for c in colors):
            continue
        colors.append({"name": name, "images": _images(ccs[cc_id])})

    # sắp variant theo thứ tự màu, rồi kiểu dáng, rồi thứ tự size của site
    color_rank = {c["name"]: i for i, c in enumerate(colors)}
    variants.sort(key=lambda v: (color_rank.get(v["color"], 99),) + v["_rank"])
    for v in variants:
        del v["_rank"]

    out = {
        "site": KEY,
        "code": str(style_id),
        "url": page_url,
        "title": title,
        "brand": NAME,
        "bullets": bullets,
        "details": details,
        "breadcrumbs": crumbs,
        "department": _department(crumbs, style),
        "size_name": "Size" if size_name else "",
        "colors": colors,
        "images": [],
        "variants": variants,
    }
    # pid trên link là mã màu (893183032 = kiểu 893183 + màu 032) -> chọn sẵn đúng màu đó, các màu khác vẫn hiện để chọn
    if str(pid) != str(style_id) and str(pid) in ccs and len(colors) > 1:
        out["url_color"] = name_of.get(str(pid)) or ccs[str(pid)].get("description") or ""
    # vid trên link = nhóm Fit đang chọn (vid=1 Regular, vid=2 Tall) -> chọn sẵn đúng nhóm đó
    m = re.search(r"[?&]vid=(\d+)", url)
    if m:
        for vd in d.get("variant_definition") or []:
            hit = next((v.get("name") for v in vd.get("values") or [] if str(v.get("variant_number") or "") == m.group(1)), None)
            if hit:
                out["url_group"] = hit
                break
    return out
