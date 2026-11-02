"""Theory (theory.com) — SFCC SFRA, site Sites-theory2_US-Site.

Trang HTML bị Akamai chặn với script, chỉ dùng JSON Product-Variation?pid=...&format=ajax (không cần proxy).
Theory tách mỗi màu thành 1 "variation group" (Q0801208_G0F, Q0801208_0VM, ...) chung mã master Q0801208.
Listing eBay = 1 master gồm mọi màu × size -> tải JSON của từng variation group (1 request / màu)
để lấy ảnh, giá, size còn hàng của màu đó.

Global-e: IP Việt Nam bị đổi giá sang VND -> gửi cookie GlobalE_Data (US/USD), nếu vẫn ra VND thì quy đổi
theo originalPrice (USD) của chính site (xem sfcc.usd_price).
Ảnh: CDN Amplience ak-media.theory.com, preset $TH-pdp-large-desktop$ (1800×2250) ép JPEG.
"""
import json
import re

from sites.base import SkipProduct, dedup, strip_brand, validate
from sites.sfcc import amplience, country, get_variation, make_bullets, titlecase, usd_price

KEY = "theory"
NAME = "Theory"
URL_RE = re.compile(r"https?://(www\.)?theory\.com/", re.I)
NEEDS_PROXY = False

HOST = "www.theory.com"
SITE = "Sites-theory2_US-Site"
PRESET = "$TH-pdp-large-desktop$"


def _pid(url):
    m = re.search(r"/([A-Z]\d{7}(?:_[A-Z0-9]{2,4})?)\.html", url, re.I) or re.search(r"[?&]pid=([A-Z]\d{7}(?:_\w+)?)", url, re.I)
    return m.group(1).upper() if m else None


def product_code(url):
    pid = _pid(url)
    return pid.split("_")[0] if pid else None


def _images(g):
    out = []
    for i in (g.get("images") or {}).get("large") or []:
        if (i.get("type") or "img") != "img":
            continue   # bỏ video
        u = i.get("urllarge") or i.get("url") or i.get("absURL")
        if u and "/v/" not in u:
            out.append(amplience(u, PRESET))
    # Đôi khi JSON lẫn ảnh của màu khác (vd TH_Q0801208_DVJ_IO trong nhóm 0VM) -> chỉ giữ ảnh đúng mã màu
    code = (g.get("id") or "").split("_")[-1]
    own = [u for u in out if f"_{code}_" in u]
    return dedup(own or out)[:12]


def _groups(p):
    g = p.get("productVariationGroups")
    if isinstance(g, str):
        try:
            g = json.loads(g)
        except ValueError:
            g = []
    return [x for x in g or [] if isinstance(x, str)]


def scrape(f, url):
    pid = _pid(url)
    if not pid:
        raise SkipProduct("Không đọc được mã sản phẩm từ link")
    first = get_variation(f, HOST, SITE, pid)
    master = first.get("masterId") or pid.split("_")[0]
    groups = _groups(first)

    # Danh sách màu theo thứ tự trên site; bỏ màu trỏ sang mẫu khác (vd bản vải khác, mã master khác)
    cvals = (first.get("colorVariationAttributes") or {}).get("values") or []
    colors_meta = []
    for cv in cvals:
        gid = cv.get("varGroupId") or ""
        if not gid.startswith(master + "_") or (groups and gid not in groups):
            continue
        if cv.get("isOnline") is False or cv.get("show") is False:
            continue
        colors_meta.append((gid, titlecase(cv.get("displayValue") or cv.get("value") or gid.split("_")[-1])))
    if not colors_meta and first.get("productType") == "variationGroup":
        colors_meta = [(first["id"], titlecase(first.get("selectedColorID") or ""))]
    if not colors_meta:
        raise SkipProduct("Không tìm thấy màu nào của sản phẩm")

    colors, variants, canon = [], [], ""
    for gid, cname in colors_meta:
        try:
            g = first if first.get("id") == gid else get_variation(f, HOST, SITE, gid)
        except SkipProduct as e:   # 1 màu tải lỗi -> bỏ màu đó, không bỏ cả sản phẩm
            if "Đã dừng" in str(e):
                raise
            f.log(f"  ⚠ không đọc được màu {cname}: {e} — bỏ màu này", "warn")
            continue
        price, lst = usd_price(g.get("price"))
        if not price:
            f.log(f"  ⚠ màu {cname} không có giá trên site (thường là đã hết) — bỏ màu này", "warn")
            continue
        colors.append({"name": cname, "images": _images(g)})
        if not canon and g.get("selectedProductUrl"):
            canon = "https://" + HOST + g["selectedProductUrl"].split("?")[0]
        orderable = g.get("online", True) and not (g.get("availability") or {}).get("isPreOrder")
        for sv in (g.get("sizeVariationAttributes") or {}).get("values") or []:
            variants.append({
                "color": cname, "size": str(sv.get("displayValue") or sv.get("value") or ""),
                "price": price, "list_price": lst,
                "available": bool(sv.get("selectable")) and bool(orderable),
                "id": sv.get("vpid") or f"{gid}-{sv.get('value')}",
            })

    title = strip_brand(first.get("productName") or first.get("nameDW") or "", NAME)
    details = {}
    if first.get("material"):
        details["Material"] = first["material"]
    if first.get("fabric"):
        details["Fabric"] = titlecase(first["fabric"])
    if first.get("care"):
        care = re.sub(r"\.?\s*Imported\.?$", "", first["care"].strip()).strip()
        details["Care"] = care
    if first.get("fitMessage"):
        details["Fit"] = first["fitMessage"]
    if first.get("rise"):
        details["Rise"] = titlecase(first["rise"]).capitalize()
    if first.get("bodyLength"):
        details["Length"] = first["bodyLength"]
    made = {x.get("label"): x.get("countryCode") for x in first.get("sustainabilityValues") or [] if isinstance(x, dict)}
    if made.get("manufactureCountry"):
        details["Origin"] = "Made in " + country(made["manufactureCountry"])
    elif "imported" in (first.get("care") or "").lower():
        details["Origin"] = "Imported"

    bullets = make_bullets([first.get("shortDescription"), first.get("longDescription"),
                            first.get("marketingFabricDescription"), first.get("fitLink"),
                            first.get("sizingMessage"), first.get("fitModelMsg")], [NAME])

    gender = (first.get("gender") or "").lower()
    path = [p for p in (canon.split(HOST, 1)[-1].split("/")[1:-2] if canon else [])]
    crumbs = [titlecase(p.replace("-", " ")).title() for p in path]
    dept = {"woman": "Women", "women": "Women", "man": "Men", "men": "Men", "unisex": "Unisex Adults"}.get(gender)
    if not dept:
        dept = "Men" if crumbs[:1] == ["Men"] else "Women"

    prod = {
        "site": KEY, "code": master, "url": canon or url.split("?")[0], "title": title, "brand": NAME,
        "bullets": bullets, "details": details, "breadcrumbs": crumbs or [dept], "department": dept,
        "size_name": (first.get("sizeVariationAttributes") or {}).get("displayName") or "Size",
        "colors": colors, "images": [], "variants": variants,
    }
    return validate(prod)
