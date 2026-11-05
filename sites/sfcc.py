"""Phần dùng chung cho các site chạy Salesforce Commerce Cloud (SFCC / Demandware).

- SFRA (Theory, Janie and Jack, Eileen Fisher): endpoint Product-Variation?format=ajax trả JSON {"product": {...}}.
- SiteGenesis cũ (Talbots): Product-Show / Product-Variation trả HTML — xử lý trong talbots.py, ở đây chỉ có tiện ích chung.

Global-e: từ IP Việt Nam các site này tự đổi giá sang VND. Gửi cookie GlobalE_Data (quốc gia US, tiền USD)
thì server trả lại giá USD. Nếu vẫn ra tiền khác USD thì quy đổi ngược theo originalPrice (USD) hoặc bỏ sản phẩm.
"""
import itertools
import json
import re
import urllib.parse

from sites.base import SkipProduct, clean_text, dedup, html_to_lines, strip_brand_desc, to_price

# Cookie ép Global-e về nước Mỹ / USD (thiếu apiVersion thì server bỏ qua cookie)
GE_COOKIE = "GlobalE_Data=" + urllib.parse.quote(json.dumps(
    {"countryISO": "US", "cultureCode": "en-US", "currencyCode": "USD", "apiVersion": "2.1.4"}, separators=(",", ":")))
US_HEADERS = {"Cookie": GE_COOKIE}

# Dòng mô tả là quảng cáo / chính sách bán hàng -> bỏ
JUNK_RE = re.compile(r"(?i)free (shipping|returns?|exchanges?)|only available online|online exclusive|exclusively online|"
                     r"final sale|gift card|promo(tion)? code|shop the look|see (the )?size (chart|guide)|"
                     r"^imported\.?$|^style\s*#|^item\s*#")

# Mã nước -> tên (cho mục Origin)
COUNTRIES = {
    "US": "USA", "CN": "China", "VN": "Vietnam", "IN": "India", "IT": "Italy", "PE": "Peru", "PT": "Portugal",
    "BD": "Bangladesh", "KH": "Cambodia", "ID": "Indonesia", "TR": "Turkey", "LK": "Sri Lanka", "JP": "Japan",
    "KR": "South Korea", "MX": "Mexico", "GT": "Guatemala", "RO": "Romania", "FR": "France", "ES": "Spain",
    "MA": "Morocco", "TH": "Thailand", "PH": "Philippines", "MY": "Malaysia", "PK": "Pakistan", "MM": "Myanmar",
    "TW": "Taiwan", "HK": "Hong Kong", "GB": "United Kingdom", "MG": "Madagascar", "MU": "Mauritius",
    "BG": "Bulgaria", "LT": "Lithuania", "MN": "Mongolia", "NP": "Nepal", "EG": "Egypt", "TN": "Tunisia",
    "HN": "Honduras", "SV": "El Salvador", "NI": "Nicaragua", "DO": "Dominican Republic", "CA": "Canada",
}


def country(code):
    return COUNTRIES.get((code or "").upper(), code or "")


# ---------------------------------------------------------------- gọi endpoint

def store_url(host, site, action, params, locale="en_US"):
    """https://host/on/demandware.store/Sites-xxx-Site/en_US/<action>?<params>"""
    return f"https://{host}/on/demandware.store/{site}/{locale}/{action}?" + urllib.parse.urlencode(params)


def get_variation(f, host, site, pid, extra=None):
    """Gọi Product-Variation (SFRA) -> dict product. extra = {thuộc tính: giá trị} sẽ thành dwvar_<pid>_<attr>."""
    params = {"pid": pid}
    for k, v in (extra or {}).items():
        params[f"dwvar_{pid}_{k}"] = v
    params["format"] = "ajax"
    data = f.get_json(store_url(host, site, "Product-Variation", params), headers=US_HEADERS)
    prod = data.get("product") if isinstance(data, dict) else None
    if not prod or not prod.get("id"):
        err = (data or {}).get("productError") or (data or {}).get("message") if isinstance(data, dict) else None
        raise SkipProduct(f"Site không trả về dữ liệu sản phẩm {pid}" + (f": {err}" if err else ""))
    return prod


# ---------------------------------------------------------------- giá

def _money(o):
    """{"value":..,"currency":..} -> (số, tiền tệ)"""
    if not isinstance(o, dict):
        return None, None
    v = o.get("value")
    if v is None:
        v = to_price(o.get("decimalPrice") or o.get("formatted"))
    return (float(v) if v is not None else None), (o.get("currency") or "").upper()


def usd_price(price):
    """price (object SFRA) -> (giá bán USD, giá gốc USD | None).

    Nếu là khoảng giá (type=range) thì lấy giá thấp nhất (người gọi nên tải theo từng size nếu cần chính xác).
    Global-e trả VND: dùng originalPrice.sales (USD) và quy đổi giá gạch theo đúng tỷ lệ của site.
    """
    if not isinstance(price, dict):
        return None, None
    if price.get("type") == "range" and isinstance(price.get("min"), dict):
        return usd_price(price["min"])
    sale, cur = _money(price.get("sales"))
    lst, lcur = _money(price.get("list"))
    if sale is None:
        return None, None
    if cur != "USD":
        orig, ocur = _money((price.get("originalPrice") or {}).get("sales"))
        if ocur != "USD" or not orig or not sale:
            raise SkipProduct(f"Site chỉ trả giá {cur or '?'} (Global-e), không có giá USD")
        rate = orig / sale
        sale = orig
        lst = round(lst * rate, 2) if lst else None
    elif lst is not None and lcur and lcur != "USD":
        lst = None
    if lst is not None and lst <= sale + 0.005:
        lst = None
    return round(sale, 2), (round(lst, 2) if lst else None)


# ---------------------------------------------------------------- ảnh

def dw_image(url, w=1600):
    """Ảnh Demandware Image Service (/dw/image/v2/...): bỏ cỡ nhỏ, xin cạnh rộng w, chất lượng 100."""
    if not url:
        return url
    url = url.replace("&amp;", "&")
    if url.startswith("//"):
        url = "https:" + url
    if "/dw/image/v2/" not in url:
        return url
    base, _, qs = url.partition("?")
    q = dict(urllib.parse.parse_qsl(qs))
    sw, sh = to_price(q.get("sw")), to_price(q.get("sh"))
    q = {k: v for k, v in q.items() if k not in ("sw", "sh", "sm", "q", "strip", "cx", "cy", "cw", "ch")}
    if sw and sh:   # giữ đúng tỉ lệ khung của site (không để bị đệm thành ảnh vuông)
        if sw < w:
            sh, sw = sh * w / sw, w
        q.update({"sw": str(int(sw)), "sh": str(int(round(sh)))})
    else:
        q.update({"sw": str(w)})
    q["q"] = "100"
    return base + "?" + urllib.parse.urlencode(q)


def dw_static(url):
    """/dw/image/v2/<REALM>/on/demandware.static/... -> /on/demandware.static/... (ảnh gốc, cỡ lớn nhất)."""
    if not url:
        return url
    url = url.replace("&amp;", "&")
    if url.startswith("//"):
        url = "https:" + url
    url = re.sub(r"/dw/image/v2/[A-Z0-9_]+/on/demandware\.static/", "/on/demandware.static/", url)
    return url.split("?")[0] if "/on/demandware.static/" in url else url


def amplience(url, preset=None, w=1600):
    """Ảnh Amplience (i/<account>/<tên>): ép JPEG chất lượng cao. preset = '$...$' của site (giữ khung cắt của site)."""
    if not url:
        return url
    if url.startswith("//"):
        url = "https:" + url
    base = url.split("?")[0]
    if preset:
        return f"{base}?{preset}&fmt=jpg&qlt=95"
    return f"{base}?w={w}&fmt=jpg&qlt=95"


# ---------------------------------------------------------------- chữ

def make_bullets(chunks, brands=(), skip=()):
    """Nhiều đoạn HTML/chữ -> list dòng chữ thuần, bỏ tên hãng, bỏ dòng quảng cáo/trùng."""
    out = []
    for c in chunks:
        if not c:
            continue
        lines = html_to_lines(c) if "<" in c else [x.strip() for x in str(c).splitlines()]
        # đoạn chữ thuần bị xuống dòng giữa câu (vd Eileen Fisher) -> nối dòng chưa hết câu với dòng sau
        if "<" not in c and len(lines) > 1:
            merged = []
            for x in lines:
                if merged and not merged[-1].endswith((".", ":", "!", "?", "”", '"')):
                    merged[-1] += " " + x
                else:
                    merged.append(x)
            lines = merged
        for ln in lines:
            ln = strip_brand_desc(clean_text(ln), *brands)
            if len(ln) < 2 or JUNK_RE.search(ln) or any(s.lower() in ln.lower() for s in skip):
                continue
            out.append(ln)
    return dedup(out)


def titlecase(s):
    """'SEA SPRAY GREEN' -> 'Sea Spray Green' (giữ nguyên nếu đã có chữ thường)."""
    s = re.sub(r"\s+", " ", (s or "").strip())
    if s and s.upper() == s:
        s = re.sub(r"[A-Za-z][A-Za-z']*", lambda m: m.group(0).capitalize(), s.lower())
    return s


# ---------------------------------------------------------------- SFRA: master -> mọi màu × size

def attr(prod, attr_id):
    for a in prod.get("variationAttributes") or []:
        if (a.get("attributeId") or a.get("id")) == attr_id:
            return a
    return None


def sfra_variants(f, host, site, master, first, color_id="color", size_id="size",
                  images_fn=None, reuse_first=False, fixed_attributes=None, size_values=None):
    """Đi qua từng màu (và thuộc tính phụ nếu có) của sản phẩm SFRA, trả về (colors, variants, size_name).

    first      : product JSON của master (đã tải) — lấy danh sách màu/thuộc tính.
    images_fn  : hàm(product JSON đã chọn màu) -> list URL ảnh lớn.
    reuse_first: True = dùng lại `first` cho màu đang được chọn sẵn trong nó (đỡ 1 request).
    Thuộc tính phụ (vd độ dài, petite) được gộp vào nhãn size: "M Petite".
    fixed_attributes: chỉ đọc các giá trị phụ đã chốt; giữ nhãn nguồn trong _site_size cho tracking.
    size_values: các mã size được phép đọc, None = giữ tất cả.
    """
    ca = attr(first, color_id)
    sa = attr(first, size_id)
    extras = [a for a in first.get("variationAttributes") or []
              if (a.get("attributeId") or a.get("id")) not in (color_id, size_id) and a.get("values")]
    color_vals = [v for v in (ca or {}).get("values") or [] if v.get("value") or v.get("id")] or [None]
    fixed_attributes = fixed_attributes or {}
    extra_values = []
    for a in extras:
        aid = a.get("attributeId") or a.get("id")
        values = a["values"]
        if aid in fixed_attributes:
            values = [v for v in values if str(v.get("value") or v.get("id")) == str(fixed_attributes[aid])]
            if not values:
                raise SkipProduct(f"Không có lựa chọn {aid}={fixed_attributes[aid]}")
        extra_values.append(values)
    extra_combos = list(itertools.product(*extra_values)) or [()]

    colors, variants = [], []
    for cv in color_vals:
        cid = (cv.get("value") or cv.get("id")) if cv else None
        cname = titlecase((cv.get("displayValue") or cv.get("description") or cid) if cv else "")
        if cv and cname.upper() == str(cid).upper() and cv.get("description"):   # displayValue chỉ là mã màu
            cname = titlecase(cv["description"])
        imgs = []
        for combo in extra_combos:
            sel = {}
            if cid:
                sel[color_id] = cid
            for a, ev in zip(extras, combo):
                sel[a.get("attributeId") or a.get("id")] = ev.get("value") or ev.get("id")
            if reuse_first and not extras and (cv is None or cv.get("selected")):
                p = first
            else:
                try:
                    p = get_variation(f, host, site, master, sel)
                except SkipProduct as e:   # 1 màu tải lỗi -> bỏ màu đó, không bỏ cả sản phẩm
                    if "Đã dừng" in str(e):
                        raise
                    f.log(f"  ⚠ không đọc được lựa chọn {cname or ''} {sel}: {e} — bỏ qua", "warn")
                    continue
            if not imgs and images_fn:
                imgs = images_fn(p)
            price, lst = usd_price(p.get("price"))
            psa = attr(p, size_id) or sa
            source_suffix = " ".join(str(ev.get("displayValue") or ev.get("value") or "") for ev in combo).strip()
            suffix = " ".join(str(ev.get("displayValue") or ev.get("value") or "")
                              for a, ev in zip(extras, combo)
                              if (a.get("attributeId") or a.get("id")) not in fixed_attributes).strip()
            svals = (psa or {}).get("values") or [None]
            if size_values is not None:
                svals = [v for v in svals if v is not None and str(v.get("value") or v.get("id")) in size_values]
            for sv in svals:
                sp, sl = price, lst
                if sv is not None and (p.get("price") or {}).get("type") == "range":
                    # giá khác nhau theo size -> hỏi riêng size này
                    try:
                        q = get_variation(f, host, site, master, {**sel, size_id: sv.get("value") or sv.get("id")})
                    except SkipProduct as e:   # 1 size tải lỗi -> bỏ size đó (không đoán giá), không bỏ cả sản phẩm
                        if "Đã dừng" in str(e):
                            raise
                        f.log(f"  ⚠ không đọc được giá size {sv.get('displayValue') or sv.get('value')} "
                              f"màu {cname or ''}: {e} — bỏ size này", "warn")
                        continue
                    sp, sl = usd_price(q.get("price"))
                label = (str(sv.get("displayValue") or sv.get("value") or "") if sv else "")
                source_label = (label + " " + source_suffix).strip() if source_suffix else label
                label = (label + " " + suffix).strip() if suffix else label
                avail = bool(p.get("available", True))
                if sv is not None:
                    avail = bool(sv.get("selectable")) and not sv.get("isBIS") and sv.get("available", True) is not False
                variant = {
                    "color": cname, "size": label, "price": sp, "list_price": sl, "available": avail,
                    "id": "-".join(x for x in [master, cid or "", (sv.get("value") or sv.get("id")) if sv else ""]
                                   + [str(ev.get("value") or ev.get("id")) for ev in combo] if x),
                }
                if source_label != label:
                    variant["_site_size"] = source_label
                variants.append(variant)
        colors.append({"name": cname, "images": imgs})
    size_name = (sa or {}).get("displayName") or ("Size" if sa else "")
    size_name = size_name[:1].upper() + size_name[1:] if size_name else ""
    common = []
    if not ca:   # sản phẩm không có màu: ảnh để chung, variant.color = ""
        common = colors[0]["images"] if colors else []
        colors = []
        for v in variants:
            v["color"] = ""
    return colors, variants, size_name, common
