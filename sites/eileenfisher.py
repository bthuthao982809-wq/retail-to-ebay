"""Eileen Fisher (eileenfisher.com) — SFCC SFRA, site Sites-ef-Site.

Akamai chặn IP Việt Nam -> phải đi proxy Mỹ (cả JSON lẫn ảnh dw/image).
Dữ liệu: Product-Variation?pid=<mã>&dwvar_<mã>_color=<màu>&format=ajax — 1 request mỗi màu
(JSON của master không chọn màu trả ảnh của màu khác nên luôn hỏi theo màu).
Size petite (PP/PS/...) nằm chung trục size; nếu có thuộc tính thứ 3 thì gộp vào nhãn size ("M Petite").
"""
import re
import urllib.parse

from sites.base import SkipProduct, dedup, strip_brand, validate
from sites.sfcc import attr, dw_image, get_variation, make_bullets, sfra_variants, titlecase

KEY = "eileenfisher"
NAME = "Eileen Fisher"
URL_RE = re.compile(r"https?://(www\.)?eileenfisher\.com/", re.I)
NEEDS_PROXY = True

HOST = "www.eileenfisher.com"
SITE = "Sites-ef-Site"
BRANDS = ("Eileen Fisher", "EILEEN FISHER", "EF")


def product_code(url):
    """Mã sản phẩm = đoạn cuối đường dẫn trước .html: thường 2 đoạn (F6KRS-T6539), đồ ngủ / hàng mới có 3 đoạn
    (SLOEE-G131-BLUEBERRY) — site gọi đúng mã đó trong dwvar_<mã>_color."""
    m = (re.search(r"/([A-Z0-9]{3,8}(?:-[A-Z0-9]{2,12}){1,3})\.html", url, re.I)
         or re.search(r"[?&]pid=([A-Z0-9]+(?:-[A-Z0-9]+){1,3})", url, re.I))
    return m.group(1).upper() if m else None


def _images(p):
    imgs = p.get("images") or {}
    lst = imgs.get("pdpZoomDesktop") or imgs.get("pdpMainDesktop") or imgs.get("large") or []
    urls = [dw_image(i.get("url") or i.get("absURL")) for i in lst if not i.get("noImage")]
    return dedup(u for u in urls if u and "noimage" not in u.lower())[:12]


def _kv(lst):
    return [str(x.get("value") or "").strip() for x in lst or [] if isinstance(x, dict) and x.get("value")]


def scrape(f, url):
    code = product_code(url)
    if not code:
        raise SkipProduct("Không đọc được mã sản phẩm từ link")
    first = get_variation(f, HOST, SITE, code)
    master = first.get("masterPid") or first.get("id") or code
    if master != first.get("id"):
        first = get_variation(f, HOST, SITE, master)
    if first.get("online") is False:
        raise SkipProduct("Sản phẩm không còn bán")

    colors, variants, size_name, common = sfra_variants(f, HOST, SITE, master, first, images_fn=_images)

    title = strip_brand(first.get("productName") or "", *BRANDS)
    cu = first.get("custom") or {}
    details, desc_lines = {}, []
    if cu.get("stylingInfo"):
        details["Fit"] = cu["stylingInfo"]
    if cu.get("stylingMaterial"):
        details["Fabric"] = cu["stylingMaterial"]
    for ln in _kv(cu.get("fabric")):
        if re.search(r"(?i)\b(wash|dry clean|tumble|iron)\b", ln) and "Care" not in details:
            details["Care"] = ln
            continue
        m = re.match(r"(?i)made in ([^.]+)\.\s*(.*)", ln)
        if m:
            details["Origin"] = "Made in " + m.group(1).strip()
            if m.group(2):
                details["Material"] = m.group(2).strip().rstrip(".")
            continue
        desc_lines.append(ln)
    bullets = make_bullets([first.get("longDescription") or first.get("shortDescription")]
                           + _kv(cu.get("design")) + desc_lines + _kv(cu.get("sustainability")), BRANDS)

    cat = first.get("category") or {}
    cat_name = cat.get("name") or ""
    dept = "Men" if re.search(r"(?i)\bmen'?s?\b|^mens-", (cat.get("id") or "") + " " + cat_name) else "Women"
    crumbs = [x for x in [dept, cat_name] if x]

    path = (first.get("selectedProductUrl") or "").split("?")[0]
    prod = {
        "site": KEY, "code": master,
        "url": f"https://{HOST}{path}" if path else url.split("?")[0],
        "title": title, "brand": NAME, "bullets": bullets, "details": details,
        "breadcrumbs": crumbs, "department": dept, "size_name": size_name or "Size",
        "colors": colors, "images": common, "variants": variants,
    }
    # Link ghi màu bằng mã số (dwvar_..._color=001) -> đổi ra tên màu để tool chọn sẵn màu đó
    m = re.search(r"dwvar_[^=&]*_color=([^&#]+)", url)
    if m:
        cid = urllib.parse.unquote_plus(m.group(1))
        cv = next((v for v in (attr(first, "color") or {}).get("values") or []
                   if str(v.get("value") or v.get("id")) == cid), None)
        if cv:
            cname = titlecase(cv.get("displayValue") or cv.get("description") or cid)
            if cname.upper() == cid.upper() and cv.get("description"):
                cname = titlecase(cv["description"])
            prod["url_color"] = cname
    return validate(prod)
