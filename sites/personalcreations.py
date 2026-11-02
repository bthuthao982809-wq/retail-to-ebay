"""Personal Creations (personalcreations.com) — quà cá nhân hoá, nền PlanetArt (PHP).

Trang sản phẩm tải bằng HTTP thường là đủ, dữ liệu nằm trong 2 biến JS:
  var product_options = {...}
     product_selections[]  : các trục lựa chọn (Color / Size / Style / Design...) + nhãn từng giá trị
     product_designs{}     : mỗi tổ hợp lựa chọn (attr2_17934-attr4_17641) -> design_id, sku, giá gốc
                             (startAtPrice = "Comp. Value"), hết hàng, ảnh mẫu
     discount              : % giảm đang áp dụng toàn site (giá bán = giá gốc × (1 - %))
     priceOverrideProducts : giá bán cố định theo design_id (ưu tiên hơn discount)
     pcProductInventory    : tồn kho theo sku
  var PC_UCD_OPTIONS = {perzByDesign: {design_id: {Personalization: [ô người mua phải điền]}}}
Ảnh: thư viện ảnh <img ref="..."> trên trang; bỏ "/thumbs/" trong link -> bản 1500px.
Mỗi sản phẩm: 1 request.

Ngoài PRODUCT_SCHEMA trả thêm:
  "personalized": True/False,
  "personalize_fields": ["Name (max 10 chars)", "Top Left Photo (photo upload)", ...]
"""
import json
import re

from sites.base import SkipProduct, clean_text, dedup, find_ld, html_to_lines, strip_brand, strip_brand_desc, to_price

KEY = "personalcreations"
NAME = "Personal Creations"
URL_RE = re.compile(r"personalcreations\.com/(product/|b2c/product-)", re.I)
NEEDS_PROXY = False

APPAREL_RE = re.compile(r"(?i)\b(sweatshirt|hoodie|t-?shirt|tee|shirt|pajamas?|pj|robe|jacket|apron|onesie|bodysuit|socks)\b")
KIDS_RE = re.compile(r"(?i)\b(kids?|youth|toddler|baby|infant|child(ren)?'?s?|onesie|bodysuit)\b")


def product_code(url):
    """/product/Ten-San-Pham-30487939 -> 30487939; /b2c/product-1194-design-602480 -> 1194-602480."""
    m = re.search(r"/product/[^?#]*?-(\d+)(?:[/?#]|$)", url)
    if m:
        return m.group(1)
    m = re.search(r"/b2c/product-(\d+)-design-(\d+)", url)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def _js_var(html, name):
    m = re.search(rf"var\s+{name}\s*=\s*([\[{{])", html)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[m.start(1):])
        return obj
    except ValueError:
        return None


def _big(u):
    """Ảnh 'thumbs' (1000px) -> bản gốc 1500px cùng thư mục."""
    if not u:
        return u
    u = u.split("?")[0] if "/thumbs/" in u else u
    return u.replace("/pc_product/thumbs/", "/pc_product/")


def _listed_lines(s):
    """listed_description: chuỗi JSON [{title, items[]}] hoặc chữ thường cách dòng."""
    if not s:
        return []
    try:
        data = json.loads(s)
        out = []
        for blk in data if isinstance(data, list) else [data]:
            if isinstance(blk, dict):
                if blk.get("title"):
                    out.append(clean_text(blk["title"]))
                out.extend(clean_text(x) for x in blk.get("items") or [] if x)
        return [x for x in out if x]
    except (ValueError, TypeError):
        return html_to_lines(s)


def _details(lines):
    det = {}
    for x in lines:
        if re.search(r"(?i)\b(measures?|dimensions?|approx\.?)\b", x) or re.search(r'\d\s*(?:"|in\.?|inches)\s*[x×]\s*\d', x):
            det.setdefault("Dimensions", x)
        elif re.search(r"(?i)\b(made of|crafted (of|from)|material|ceramic|acrylic|polyester|cotton|fleece|wood|glass|stainless|metal)\b", x) and "Material" not in det:
            det["Material"] = x
        elif re.search(r"(?i)\b(dishwasher|microwave|machine wash|hand wash|wipe clean|spot clean|care)\b", x) and "Care" not in det:
            det["Care"] = x
        elif re.search(r"(?i)\b(holds|capacity)\b.*\boz\b", x):
            det.setdefault("Capacity", x)
    return det


def _perz_fields(ucd, designs):
    """Ô cá nhân hoá: gộp các ô của mọi lựa chọn (theo thứ tự lựa chọn trên site), bỏ trùng.
    Vd ornament 2-12 người -> Name 1 ... Name 12; người mua điền theo số người đã chọn."""
    pz = (ucd or {}).get("perzByDesign") or {}
    if not pz:
        return []

    def fmt(x):
        name = (x.get("DisplayName") or x.get("GroupName") or "Text").strip()
        t = (x.get("Type") or "").lower()
        if t == "image":
            s = f"{name} (photo upload)"
        elif t == "text":
            mx = x.get("MaxCharacterLimit") or 0
            s = f"{name} (max {mx} chars)" if mx else name
        else:
            s = f"{name} ({x.get('Type')})" if x.get("Type") else name
        if not x.get("IsRequired"):
            s += " (optional)"
        return s

    def fields_of(did):
        p = pz.get(str(did)) or {}
        return [fmt(x) for x in sorted(p.get("Personalization") or [], key=lambda x: (x.get("GroupSortOrder") or 0, x.get("SortOrder") or 0))]

    out = []
    for did in [d.get("design_id") for d in designs] or list(pz):
        out.extend(fields_of(did))
    return dedup(out)


def scrape(f, url):
    url = url.split("#")[0]
    html = f.get(url)
    po = _js_var(html, "product_options")
    if not po:
        raise SkipProduct("Không đọc được dữ liệu sản phẩm (product_options)")
    ucd = _js_var(html, "PC_UCD_OPTIONS")
    ld = find_ld(html, "Product") or {}
    offers = ld.get("offers") or {}
    if isinstance(offers, dict) and offers.get("priceCurrency") and offers["priceCurrency"] != "USD":
        raise SkipProduct(f"Giá không phải USD ({offers['priceCurrency']})")

    canon = re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', html)
    page_url = canon.group(1) if canon and "personalcreations.com" in canon.group(1) else url.split("?")[0]
    code = product_code(page_url) or product_code(url) or str(po.get("related_design_id") or "")

    # ---- trục lựa chọn
    sels = sorted(po.get("product_selections") or [], key=lambda s: s.get("sort_order") or 0)
    sels = [s for s in sels if s.get("select_options")]
    label = {}       # (attr_id, value_id) -> nhãn
    rank = {}        # (attr_id, value_id) -> thứ tự trên site
    for s in sels:
        for i, o in enumerate(s["select_options"]):
            label[(str(s["attr_id"]), str(o["value_id"]))] = (o.get("label") or "").strip()
            rank[(str(s["attr_id"]), str(o["value_id"]))] = i
    color_sel = next((s for s in sels if re.search(r"(?i)\bcolou?r\b", s.get("caption") or "")), None)
    size_sels = [s for s in sels if s is not color_sel]
    # trục chỉ có 1 lựa chọn (vd Design: Astronaut) thì không cần ghi vào nhãn
    size_sels_shown = [s for s in size_sels if len(s["select_options"]) > 1]
    size_name = " / ".join(s.get("caption") or "Option" for s in size_sels_shown)

    def axis(d, s):
        m = d.get("attr_value_mapping") or {}
        return label.get((str(s["attr_id"]), str(m.get(str(s["attr_id"]), ""))), "")

    # ---- giá
    disc = to_price(po.get("discount")) or 0.0
    override = {str(k): to_price(v) for k, v in (po.get("priceOverrideProducts") or {}).items()}
    inv = {str(k): v for k, v in (po.get("pcProductInventory") or {}).items()}

    designs = [d for d in (po.get("product_designs") or {}).values() if isinstance(d, dict)]
    axes = ([color_sel] if color_sel else []) + size_sels
    designs.sort(key=lambda d: tuple(
        rank.get((str(s["attr_id"]), str((d.get("attr_value_mapping") or {}).get(str(s["attr_id"]), ""))), 99) for s in axes))
    variants = []
    color_img = {}
    for d in designs:
        sp = d.get("startAtPrice")
        reg = to_price(sp.get("retail_price") if isinstance(sp, dict) else sp)
        price = override.get(str(d.get("design_id"))) or override.get(str(d.get("pc_product_id")))
        if not price and reg:
            price = round(reg * (1 - disc / 100.0), 2) if disc else reg
        stock = inv.get(str(d.get("id")))
        avail = not d.get("is_out_of_stock") and not (stock and (stock.get("remaining") or 0) <= 0 and not po.get("virtual_qty"))
        color = axis(d, color_sel) if color_sel else ""
        size = " / ".join(x for x in (axis(d, s) for s in size_sels_shown) if x)
        if color and color not in color_img and d.get("design_sample"):
            color_img[color] = _big(d["design_sample"])
        m = d.get("attr_value_mapping") or {}
        sort_key = tuple(rank.get((str(s["attr_id"]), str(m.get(str(s["attr_id"]), ""))), 99)
                         for s in ([color_sel] if color_sel else []) + size_sels)
        variants.append({
            "_rank": sort_key,
            "color": color, "size": size,
            "price": price,
            "list_price": reg if reg and price and reg > price + 0.001 else None,
            "available": bool(avail),
            "id": str(d.get("sku") or d.get("design_id") or ""),
        })
    if not variants:   # sản phẩm không có lựa chọn nào
        price = to_price(offers.get("price") if isinstance(offers, dict) else None)
        if not price:
            raise SkipProduct("Không đọc được giá")
        hi = to_price(offers.get("highPrice")) if isinstance(offers, dict) else None
        variants.append({"_rank": (), "color": "", "size": "", "price": price,
                         "list_price": hi if hi and hi > price else None,
                         "available": not po.get("is_out_of_stock") and "OutOfStock" not in str(offers.get("availability")),
                         "id": str(po.get("related_design_id") or code)})
        size_name = ""
    variants.sort(key=lambda v: v["_rank"])
    for v in variants:
        del v["_rank"]
    # bỏ biến thể trùng nhãn (cùng màu + size), giữ cái đầu
    seen, uniq = set(), []
    for v in variants:
        k = (v["color"], v["size"])
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    variants = uniq

    # ---- ảnh: thư viện trên trang (ảnh lớn), màu nào có ảnh mẫu riêng thì gắn cho màu đó
    gallery = [m.group(1) for m in re.finditer(r'<img[^>]*\bref="(https?://[^"]+)"', html)]
    gallery = dedup(_big(u) for u in gallery if "planetart.com" in u or "cloudfront.net" in u)
    if not gallery:
        img = ld.get("image")
        gallery = dedup(_big(u) for u in (img if isinstance(img, list) else [img]) if u)
        gallery += dedup(_big(d.get("design_sample")) for d in designs if d.get("design_sample"))
    colors = []
    if color_sel:
        for o in color_sel["select_options"]:
            name = (o.get("label") or "").strip()
            if any(v["color"] == name for v in variants):
                colors.append({"name": name, "images": [color_img[name]] if name in color_img else []})

    # ---- chữ
    title = strip_brand(clean_text(ld.get("name") or ""), NAME)
    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        title = strip_brand(clean_text(m.group(1)) if m else "", NAME)
    pdo = po.get("product_design_objects") or {}
    first = (pdo.get(str(designs[0].get("design_id"))) if designs else None) or next(iter(pdo.values()), {}) or {}
    desc = html_to_lines(first.get("description") or ld.get("description") or "")
    listed = _listed_lines(first.get("listed_description"))
    bullets = [strip_brand_desc(x, NAME) for x in dedup(desc + listed)]
    bullets = [x for x in bullets if x and not re.search(r"(?i)comp\.? value|exclusions apply", x)]

    personalized = 'id="btnPersonalize"' in html or bool((ucd or {}).get("perzByDesign"))
    fields = _perz_fields(ucd, designs)
    if personalized and not fields and re.search(r"(?i)\bphoto", title):
        fields = ["Photo (photo upload)"]

    dept = ""
    if APPAREL_RE.search(title):
        text = " ".join([title] + bullets)
        if KIDS_RE.search(title):
            dept = "Unisex Kids"
        elif re.search(r"(?i)\bwomen'?s\b|\bladies\b", text):
            dept = "Women"
        elif re.search(r"(?i)\bmen'?s\b", text):
            dept = "Men"
        else:
            dept = "Unisex Adults"

    return {
        "site": KEY,
        "code": code,
        "url": page_url,
        "title": title,
        "brand": NAME,
        "bullets": bullets,
        "details": _details(listed),
        "breadcrumbs": [],
        "department": dept,
        "size_name": size_name,
        "colors": colors,
        "images": gallery[:12],
        "variants": variants,
        "personalized": personalized,
        "personalize_fields": fields,
    }
