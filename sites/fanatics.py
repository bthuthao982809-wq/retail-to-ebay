"""Fanatics (fanatics.com) — đồ cổ động đội thể thao (MLB, NFL...). Dữ liệu nằm trong var __platform_data__ = {...}
(khoá "pdp-data" -> "pdp"):
  productId, title ("Tampa Bay Rays ... T-Shirt - Heather Charcoal"), brandResource.value (New Era, Antigua, Nike...),
  genderAgeGroup / ageGroupResources, categories (T-Shirts > Short Sleeve), breadcrumb, league, team,
  description, details ["Brand: Fanatics", "Material: ...", ...], media / imageSelector (ảnh),
  sizes[{size, available, price{regular, sale, clearance}}], colors[{productId, name, sizes, media, productUrl}] (mẫu
  nhiều màu: mỗi màu 1 link riêng).
Link: .../<tên>/o-1243+t-25126799+p-79112257247491+z-9-...  (p-... = mã của đúng màu đó, dùng làm mã SKU).
IP Việt Nam bị Akamai chặn (trang kiểm tra / Access Denied) -> tự chuyển proxy (proxy Mỹ đọc được bình thường).
"""
import json
import re
import time

from sites.base import SkipProduct, dedup

KEY = "fanatics"
NAME = "Fanatics"
URL_RE = re.compile(r"https?://(www\.)?fanatics\.com/[^\s?#]*?/o-\d+\+t-\d+\+p-\d+", re.I)
NEEDS_PROXY = True
EXAMPLES = ["https://www.fanatics.com/mlb/tampa-bay-rays/tampa-bay-rays-fanatics-2026-american-league-champions-locker-room-t-shirt-heather-charcoal/o-1243+t-25126799+p-79112257247491+z-9-1169815758"]
BASE = "https://www.fanatics.com"
IMG = "https://fanatics.frgimages.com/"
STATE_RE = re.compile(r"var __platform_data__\s*=\s*")
ONE_SIZE = {"OSFM", "OSFA", "OS", "ONE SIZE", "NO SIZE"}


def product_code(url):
    m = re.search(r"\+p-(\d+)", url or "")
    return m.group(1) if m else None


def _page_url(url):
    m = re.search(r"(https?://(?:www\.)?fanatics\.com/[^\s?#]*?/o-\d+\+t-\d+\+p-\d+[^\s?#]*)", url, re.I)
    if not m:
        raise SkipProduct("Link Fanatics không đúng dạng .../o-...+t-...+p-...")
    return m.group(1)


def _state(html):
    m = STATE_RE.search(html or "")
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[m.end():])
    except ValueError:
        return None
    return obj


# Proxy đang bị Akamai của Fanatics chặn (trả trang kiểm tra bot thay vì sản phẩm): nhớ 30 phút để các link sau bỏ qua
# ngay, không mất 1 lượt tải mỗi link. Akamai chặn theo IP nên phần lớn proxy chết cùng lúc, chỉ vài proxy qua được.
_BLOCKED = {}
_BLOCK_SECS = 1800


def _skip_blocked(f):
    """Xoay tới proxy chưa bị chặn (hết cả danh sách thì giữ nguyên, thử lại proxy cũ)."""
    for _ in range(len(f.pool.proxies)):
        if time.time() - _BLOCKED.get(f.pool.label(), 0) > _BLOCK_SECS:
            return
        f.pool.rotate()


def _load(f, url):
    """Dữ liệu trang. Trang kiểm tra bot của Akamai (HTTP 200, không có dữ liệu) -> đánh dấu proxy đó bị chặn, đổi proxy;
    thử qua đủ cả danh sách proxy (không chỉ 4 lần). IP gốc bị chặn -> tự chuyển proxy."""
    page = _page_url(url)
    tries = max(4, len(f.pool.proxies) + 2)
    for _ in range(tries):
        if f.proxy_on:
            _skip_blocked(f)
        html = f.get(page)
        st = _state(html)
        pdp = ((st or {}).get("pdp-data") or {}).get("pdp")
        if pdp:
            return pdp
        bot = "sec-if-cpt" in (html or "") or "akamai" in (html or "")[:3000].lower()
        if f.proxy_on:
            if bot:
                _BLOCKED[f.pool.label()] = time.time()
                left = sum(1 for p in f.pool.proxies if time.time() - _BLOCKED.get(p["server"].split("://", 1)[1], 0) > _BLOCK_SECS)
                f.log(f"  ⚠ Fanatics chặn proxy {f.pool.label()} (trang kiểm tra bot) — đổi proxy, còn {left} proxy chưa bị chặn", "warn")
                if not left:
                    raise SkipProduct("Fanatics (Akamai) đang chặn toàn bộ proxy — chờ 30–60 phút hoặc thêm proxy Mỹ khác")
            f.pool.rotate()
        elif not f.switch_to_proxy("Fanatics chặn IP gốc"):
            raise SkipProduct("Fanatics chặn IP Việt Nam — dán proxy Mỹ trong Cài đặt rồi chạy lại")
    raise SkipProduct("Không đọc được dữ liệu sản phẩm Fanatics (các proxy đều bị chặn hoặc trang đổi cấu trúc)")


def _money(price, kind):
    try:
        return float(((price or {}).get(kind) or {}).get("money", {}).get("userCurrencyValue") or 0) or None
    except (TypeError, ValueError):
        return None


def _prices(price):
    """(giá đang bán, giá gốc nếu cao hơn)."""
    reg = _money(price, "regular")
    cur = [x for x in (_money(price, "sale"), _money(price, "clearance"), reg) if x]
    now = min(cur) if cur else None
    return now, (reg if reg and now and reg > now + 0.001 else None)


def _img(src):
    if not src:
        return ""
    u = src if src.startswith("http") else IMG + src.lstrip("/")
    u = re.sub(r"([?&])w=\d+", r"\1w=1500", u)
    return u if "w=" in u else u + ("&" if "?" in u else "?") + "w=1500"


def _images(node):
    sel = node.get("imageSelector") or {}
    out = [(sel.get("defaultImage") or {}).get("image", {}).get("src") or (node.get("image") or {}).get("src")]
    out += [(a.get("image") or {}).get("src") for a in (sel.get("additionalImages") or node.get("additionalImages") or [])]
    out += [m.get("url") for m in node.get("media") or [] if str(m.get("type") or "image").lower() == "image"]
    return dedup(_img(u) for u in out if u)[:12]


def _color_of(title):
    m = re.search(r"\s+-\s+([^-]+)$", title or "")
    return m.group(1).strip() if m else ""


def _dept(pdp):
    ages = {(a.get("value") or "").lower() for a in pdp.get("ageGroupResources") or []}
    genders = {(g or "").lower() for g in pdp.get("genderAgeGroup") or []}
    if ages & {"youth", "kids", "toddler", "infant", "newborn", "preschool"} or genders & {"youth", "kids", "boys", "girls"}:
        if "girls" in genders:
            return "Girls"
        if "boys" in genders:
            return "Boys"
        return "Unisex Kids"
    if "women" in genders:
        return "Women"
    if "men" in genders:
        return "Men"
    return "Unisex Adults" if genders else ""


def _variants(color, sizes):
    out = []
    for s in sizes or []:
        now, reg = _prices(s.get("price"))
        if not now:
            continue
        label = (s.get("size") or "").strip()
        # Size có chuông "báo khi có hàng" vẫn nằm trong danh sách size.
        # Fanatics trả available=false / quantityLevel=no_inventory cho các size này.
        # Kiểm tra cả dấu tồn kho để không coi size nhận thông báo là đặt mua được.
        in_stock = bool(s.get("available")) and str(s.get("quantityLevel") or "").lower() != "no_inventory"
        out.append({"color": color, "size": "" if label.upper() in ONE_SIZE else label, "price": now, "list_price": reg,
                    "available": in_stock, "id": str(s.get("itemId") or "")})
    return out


def scrape(f, url):
    pdp = _load(f, url)
    title = (pdp.get("title") or "").strip()
    me_color = _color_of(title)
    name = re.sub(r"\s+-\s+[^-]+$", "", title).strip() if me_color else title

    # mẫu nhiều màu: mỗi màu 1 nút trong pdp.colors (có size + giá + ảnh + link riêng); link đang mở là màu chính
    colors_src = pdp.get("colors") or []
    me_id = str(pdp.get("productId") or "")
    colors, variants, url_color = [], [], ""
    seen = {}
    for c in colors_src or [{**pdp, "name": me_color}]:
        cname = (c.get("name") or _color_of(c.get("title")) or me_color or "").strip().title()
        seen[cname] = seen.get(cname, 0) + 1
        if seen[cname] > 1:
            cname = f"{cname} {seen[cname]}"
        if str(c.get("productId") or me_id) == me_id:
            url_color = cname
            node = pdp
        else:
            node = c
        colors.append({"name": cname, "images": _images(node)})
        variants += _variants(cname, c.get("sizes") if colors_src else pdp.get("sizes"))
    if not variants:
        raise SkipProduct("Sản phẩm không có size nào đang bán")
    # màu của link xếp đầu
    colors.sort(key=lambda c: c["name"] != url_color)
    variants.sort(key=lambda v: v["color"] != url_color)

    desc = (pdp.get("description") or "").strip()
    bullets = [s.strip() for s in re.split(r"(?<=[.!?])\s+", desc) if s.strip()]
    details = {}
    for d in pdp.get("details") or []:
        d = str(d).strip()
        k, sep, v = d.partition(":")
        if sep and len(k) <= 30 and v.strip():
            if k.strip().lower() != "brand":
                details[k.strip()] = v.strip()
        elif d.lower() == "imported":
            details["Country of Origin"] = "Imported"
        elif re.match(r"(?i)made in ", d):
            details["Country of Origin"] = re.sub(r"(?i)^made in (the )?", "", d).strip()
        elif d:
            bullets.append(d.rstrip(".") + ".")
    # "MLB Merchandise" là hàng chung của giải, không phải đội; hàng chung nhiều đội (bộ thẻ Topps đủ 30 đội) không ghi Team
    teams = [t for t in (pdp.get("team") or []) if t and not re.search(r"(?i)merchandise", t)]
    if 1 <= len(teams) <= 2:   # 1 đội, hoặc áo matchup 2 đội (eBay chỉ nhận 1 Team -> tool lấy đội đầu)
        details.setdefault("Team", ", ".join(teams))
    league = ", ".join(pdp.get("league") or [])
    if league:
        details.setdefault("League", league)
    cats = [c.get("categoryName") for c in pdp.get("categories") or [] if c.get("categoryName")]
    crumbs = dedup([b.get("name") for b in pdp.get("breadcrumb") or [] if b.get("name")] + cats[:1])

    canon = (pdp.get("seoContext") or {}).get("canonicalUrl") or pdp.get("url") or ""
    page = BASE + "/" + canon.lstrip("/") if canon else _page_url(url)
    out = {
        "site": KEY,
        "code": product_code(page) or product_code(url) or me_id,
        "url": page,
        "title": name,
        # "Fanatics MTO Label" = hàng Fanatics in theo đơn (Made To Order): tiêu đề chỉ ghi "Fanatics"
        "brand": re.sub(r"(?i)\s+MTO Label$", "", (pdp.get("brandResource") or {}).get("value") or ""),
        "mto": bool(re.search(r"(?i)\bMTO\b", (pdp.get("brandResource") or {}).get("value") or "")),
        "bullets": bullets,
        "details": details,
        "breadcrumbs": crumbs,
        "department": _dept(pdp),
        "size_name": "Size",
        "colors": colors,
        "images": [],
        "variants": variants,
        "teams": teams,   # tên đội (đủ, kể cả > 2): AI viết tiêu đề bắt buộc giữ
    }
    if len(colors) > 1:
        out["url_color"] = url_color   # màu của link chọn sẵn, các màu khác vẫn hiện để chọn
    return out
