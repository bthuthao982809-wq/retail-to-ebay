"""Janie and Jack (janieandjack.com) — SFCC SFRA, site Sites-JanieAndJackSFRA-Site.

Dữ liệu: Product-Variation?pid=<mã 9 số>&format=ajax (JSON, không cần proxy). Mỗi mã sản phẩm thường chỉ có 1 màu,
nên thường chỉ 1 request. Ảnh Amplience (i1.adis.ws) -> xin JPEG 1600px.
Đồ trẻ em: department lấy từ thuộc tính gender/age/productPath (Girls / Boys / Baby Girls / Baby Boys).
"""
import re

from sites.base import SkipProduct, dedup, strip_brand, validate
from sites.sfcc import amplience, country, get_variation, make_bullets, sfra_variants

KEY = "janieandjack"
NAME = "Janie and Jack"
URL_RE = re.compile(r"https?://(www\.)?janieandjack\.com/", re.I)
NEEDS_PROXY = False

HOST = "www.janieandjack.com"
SITE = "Sites-JanieAndJackSFRA-Site"
BRANDS = ("Janie and Jack", "Janie & Jack", "Janie And Jack", "JanieAndJack")


def product_code(url):
    m = re.search(r"-(\d{6,12})\.html", url) or re.search(r"[?&]pid=(\d{6,12})", url)
    return m.group(1) if m else None


def _attrs(prod):
    """Gom nhóm 'attributes' của SFRA thành dict nhãn -> giá trị chữ."""
    out = {}
    for g in prod.get("attributes") or []:
        for a in g.get("attributes") or []:
            v = a.get("value")
            v = ", ".join(str(x) for x in v) if isinstance(v, list) else str(v or "")
            if a.get("label") and v:
                out[a["label"]] = v
    return out


def _images(p):
    urls = [amplience(i.get("url") or i.get("absURL")) for i in (p.get("images") or {}).get("large") or []]
    return dedup(u for u in urls if u)[:12]


def _department(at, name):
    text = " ".join([at.get("gender", ""), at.get("webDepartment", ""), at.get("department", ""),
                     at.get("productPath", ""), at.get("age", ""), name]).upper()
    baby = "BABY" in text or at.get("age", "").lower() in ("baby", "infant", "newborn")
    if re.search(r"\bGIRLS?\b|_GIRL", text):
        return "Baby Girls" if baby else "Girls"
    if re.search(r"\bBOYS?\b|_BOY", text):
        return "Baby Boys" if baby else "Boys"
    return "Unisex Kids"


def _seller_brand(p):
    """Hãng thật của sản phẩm. Hàng "Brands We Love" (marketplace, trang ghi "Sold & Shipped By LORENA CANALS"):
    ô brand / tên shop là hãng khác -> dùng hãng đó; còn lại là Janie and Jack. "LORENA CANALS" -> "Lorena Canals"."""
    raw = ""
    if p.get("isMarketplaceProduct") or p.get("marketplaceProduct"):
        raw = p.get("brand") or p.get("miraklOperatorShopName") or ""
    raw = re.sub(r"[®™©]", "", str(raw)).strip()
    if not raw or re.sub(r"[^a-z]", "", raw.lower()) in ("janieandjack", "janiejack"):
        return NAME
    if raw.isupper() and len(raw) > 4:   # chữ hoa toàn bộ -> dạng thường; viết tắt ngắn (VVC) giữ nguyên
        raw = " ".join(w if len(w) <= 3 else w.capitalize() for w in raw.split())
    return raw


def scrape(f, url):
    code = product_code(url)
    if not code:
        raise SkipProduct("Không đọc được mã sản phẩm từ link")
    first = get_variation(f, HOST, SITE, code)
    master = first.get("masterProductID") or first.get("id")
    if master != code:
        first = get_variation(f, HOST, SITE, master)
    if not first.get("online", True):
        raise SkipProduct("Sản phẩm không còn bán")
    at = _attrs(first)
    brand = _seller_brand(first)
    title = strip_brand(first.get("productName") or "", *BRANDS, *([brand] if brand != NAME else [])).strip(" :-–")

    colors, variants, size_name, common = sfra_variants(
        f, HOST, SITE, master, first, images_fn=_images, reuse_first=True)

    # Mô tả: customProductDetails = [đoạn mô tả, chất liệu, đặc điểm..., giặt; xuất xứ]
    det = first.get("customProductDetails") or [at[k] for k in sorted(at) if k.startswith("productDetails")]
    details, rest = {}, []
    for ln in det:
        s = str(ln).strip()
        if re.search(r"(?i)\b(wash|dry clean|hand wash|tumble)\b", s):
            care, _, origin = s.partition(";")
            details["Care"] = care.strip()
            if "import" in origin.lower():
                details["Origin"] = "Imported"
            continue
        if re.search(r"\d+\s*%", s) and "Material" not in details:
            details["Material"] = s
        rest.append(s)
    bullets = make_bullets([first.get("longDescription"), first.get("shortDescription")] + rest, BRANDS)
    if at.get("countryOfOrigin"):
        details["Origin"] = "Made in " + country(at["countryOfOrigin"])
    if at.get("ageGroup"):
        details["Age Group"] = at["ageGroup"]

    dept = _department(at, title)
    crumbs = [x for x in [dept, at.get("webCategory") or first.get("primaryCategory")] if x]

    prod = {
        "site": KEY, "code": master,
        "url": f"https://{HOST}" + (first.get("selectedProductUrl") or "").split("?")[0] if first.get("selectedProductUrl") else url.split("?")[0],
        "title": title, "brand": brand, "bullets": bullets, "details": details,
        "marketplace": brand != NAME,   # hàng "Brands We Love": hãng khác tự bán và tự giao (Sold & Shipped By ...)
        "breadcrumbs": crumbs, "department": dept, "size_name": size_name or "Size",
        "colors": colors, "images": common, "variants": variants,
    }
    return validate(prod)
