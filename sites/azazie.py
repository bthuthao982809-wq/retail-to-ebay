"""Azazie (azazie.com) — Nuxt SSR.

Cách lấy: 1 request trang sản phẩm (HTTP, giả Chrome, qua proxy Mỹ) -> đọc window.__NUXT__ (dạng hàm JS rút gọn,
tự giải bằng bộ đọc nhỏ bên dưới) -> state.product (baseInfo/styleInfo/imageInfo/makeTimeInfo) + data[0] (mô tả, tồn kho).
Ảnh từng màu: trang chỉ có ảnh màu đang mở, các màu khác gọi /prod/1.0/product/item-info?goods_id=..&color=.. (JSON).
Ảnh lấy từ cdn-1.azazie.com/upimg/azazie/h/ (bản gốc lớn nhất), tải không cần proxy.
Đọc window.__NUXT__ dạng hàm JS rút gọn: (function(a,b,...){ x.y=a; ...; return {...} }(1,"s",{},...)).

Không có Node.js nên tự viết bộ đọc nhỏ: chỉ hiểu chuỗi/số/true/false/null/void 0, object, array,
tên biến (tham số hàm) và lệnh gán dạng  bien.thuoc_tinh=...;  bien[0]=...;  trước return.
"""
import json
import re

from sites.base import (SkipProduct, clean_text, dedup, html_to_lines, strip_brand, strip_brand_desc,
                        to_price)


_ID = re.compile(r"[A-Za-z_$][\w$]*")
_NUM = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_WS = re.compile(r"\s*")


class _P:
    def __init__(self, s, env):
        self.s, self.i, self.env = s, 0, env

    def ws(self):
        self.i = _WS.match(self.s, self.i).end()

    def peek(self):
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def eat(self, ch):
        if self.peek() != ch:
            raise ValueError(f"cần {ch!r} tại {self.i}: {self.s[self.i:self.i + 60]!r}")
        self.i += 1

    def string(self):
        q = self.s[self.i]
        j = self.i + 1
        buf = []
        while True:
            c = self.s[j]
            if c == "\\":
                buf.append(self.s[j:j + 2])
                j += 2
                if buf[-1] == "\\u":
                    buf[-1] += self.s[j:j + 4]
                    j += 4
                continue
            if c == q:
                break
            buf.append('\\"' if c == '"' else c)
            j += 1
        self.i = j + 1
        raw = "".join(buf).replace("\\'", "'")
        raw = re.sub(r"\\x([0-9a-fA-F]{2})", r"\\u00\1", raw)
        try:
            return json.loads('"' + raw + '"')
        except ValueError:
            return raw

    def value(self):
        c = self.peek()
        if c == "{":
            self.i += 1
            obj = {}
            while self.peek() != "}":
                c = self.peek()
                if c in "\"'":
                    k = self.string()
                elif c.isdigit():
                    k = _NUM.match(self.s, self.i).group(0)
                    self.i += len(k)
                else:
                    k = _ID.match(self.s, self.i).group(0)
                    self.i += len(k)
                self.eat(":")
                obj[k] = self.value()
                if self.peek() == ",":
                    self.i += 1
            self.i += 1
            return obj
        if c == "[":
            self.i += 1
            arr = []
            while self.peek() != "]":
                arr.append(self.value())
                if self.peek() == ",":
                    self.i += 1
            self.i += 1
            return arr
        if c in "\"'":
            return self.string()
        m = _NUM.match(self.s, self.i)
        if m and (c.isdigit() or c in "-."):
            self.i = m.end()
            t = m.group(0)
            return float(t) if any(x in t for x in ".eE") else int(t)
        m = _ID.match(self.s, self.i)
        if not m:
            raise ValueError(f"không hiểu giá trị tại {self.i}: {self.s[self.i:self.i + 60]!r}")
        name = m.group(0)
        self.i = m.end()
        if name == "true":
            return True
        if name == "false":
            return False
        if name in ("null", "undefined"):
            return None
        if name == "void":
            self.value()
            return None
        if name == "new" or name == "Array" or name == "Object":
            # vd new Date(1234) / Array(3): bỏ qua, trả None
            if name == "new":
                self.value()
            if self.peek() == "(":
                self._skip_parens()
            return None
        v = self.env.get(name)
        # truy cập thuộc tính trong biểu thức (hiếm): a.b / a[0]
        while self.peek() in (".", "["):
            v = self._access(v)
        if self.peek() == "(":
            self._skip_parens()
            return None
        return v

    def _skip_parens(self):
        depth = 0
        while True:
            c = self.s[self.i]
            if c in "\"'":
                self.string()
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return
            self.i += 1

    def _access(self, v):
        c = self.peek()
        self.i += 1
        if c == ".":
            m = _ID.match(self.s, self.i)
            self.i = m.end()
            k = m.group(0)
        else:
            k = self.value()
            self.eat("]")
        try:
            if isinstance(v, list):
                return v[int(k)]
            return v.get(str(k)) if isinstance(v, dict) else None
        except (IndexError, ValueError, TypeError):
            return None

    def assign_stmt(self):
        """bien(.a|[0])+ = giá trị ;"""
        m = _ID.match(self.s, self.i)
        target = self.env.get(m.group(0))
        self.i = m.end()
        keys = []
        while self.peek() in (".", "["):
            c = self.s[self.i]
            self.i += 1
            if c == ".":
                mm = _ID.match(self.s, self.i)
                self.i = mm.end()
                keys.append(mm.group(0))
            else:
                keys.append(self.value())
                self.eat("]")
        self.eat("=")
        val = self.value()
        for k in keys[:-1]:
            target = target[int(k)] if isinstance(target, list) else target.setdefault(str(k), {})
        if keys:
            k = keys[-1]
            if isinstance(target, list):
                k = int(k)
                while len(target) <= k:
                    target.append(None)
                target[k] = val
            elif isinstance(target, dict):
                target[str(k)] = val
        if self.peek() == ";":
            self.i += 1


def eval_nuxt(html):
    """HTML có window.__NUXT__=(function(...){...}(...)) hoặc window.__NUXT__={...} -> dict Python (hoặc None)."""
    i = html.find("window.__NUXT__")
    if i < 0:
        return None
    end = html.find("</script>", i)
    src = html[html.find("=", i) + 1:end].strip().rstrip(";")
    if src.startswith("{"):
        return _P(src, {}).value()
    m = re.match(r"\(function\(([^)]*)\)\{", src)
    if not m:
        return None
    params = [p.strip() for p in m.group(1).split(",") if p.strip()]
    body_start = m.end()
    # phần đối số nằm sau "}(" cuối cùng; đọc từ cuối: tìm "}(" khớp với ")" ở cuối chuỗi
    k = src.rfind("}(")
    while k > 0:
        try:
            p = _P(src, {})
            p.i = k + 2
            args = []
            while p.peek() != ")":
                args.append(p.value())
                if p.peek() == ",":
                    p.i += 1
            p.i += 1
            if src[p.i:].strip(" )") == "":
                break
        except Exception:
            pass
        k = src.rfind("}(", 0, k)
    else:
        return None
    env = dict(zip(params, args + [None] * (len(params) - len(args))))
    p = _P(src[body_start:k], env)
    while True:
        p.ws()
        if p.s.startswith("return", p.i):
            p.i += len("return")
            return p.value()
        p.assign_stmt()


# ---------------------------------------------------------------- bộ cào Azazie

KEY = "azazie"
NAME = "Azazie"
URL_RE = re.compile(r"https?://(?:www\.)?azazie\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?products/[^/?#]+", re.I)
NEEDS_PROXY = True   # IP Việt Nam bị chặn

SITE = "https://www.azazie.com"
API = SITE + "/prod/1.0/product/item-info"
IMG_PREFIX = "https://cdn-1.azazie.com/upimg/azazie/h/"   # ảnh gốc lớn nhất (~1140x1578)
MAX_IMAGES = 12
WORKERS = 6        # số luồng tải ảnh từng màu (mỗi màu 1 request item-info ~60 KB)


def product_code(url):
    # Link chỉ có goodsStyleId (riêng từng màu), mã gốc goodsId chỉ có sau khi tải trang -> không đoán được
    return None


def _title_case(s):
    return " ".join("-".join(p[:1].upper() + p[1:] for p in w.split("-")) for w in s.split())


def _gallery_urls(gallery):
    """goodsGallery[color] -> list URL ảnh lớn: ảnh người mẫu thường trước, ảnh mẫu plus-size sau."""
    imgs = [x for x in (gallery or {}).get("images", []) if x.get("imgUrl") and x.get("imgType", "photo") == "photo"]
    imgs.sort(key=lambda x: (bool(x.get("isPlus")), x.get("sequence") or 0))
    out = []
    for x in imgs:
        u = x["imgUrl"]
        if not u.startswith("http"):
            u = IMG_PREFIX + u.lstrip("/")
        out.append(u)
    return dedup(out)[:MAX_IMAGES]


def _color_info(f, goods_id, dress_type, color_key):
    """1 request item-info cho 1 màu -> (ảnh, baseInfo). Lỗi thì ([], {})."""
    try:
        d = f.get_json(f"{API}?goods_id={goods_id}&dress_type={dress_type}&color={color_key}&sodGalleryVersion=b")
    except SkipProduct as e:
        if "Đã dừng" in str(e):
            raise   # bấm Dừng: dừng hẳn
        f.log(f"  ⚠ không đọc được ảnh / giá màu {color_key}: {e}", "warn")
        return [], {}
    data = (d or {}).get("data") or {}
    gal = ((data.get("imageInfo") or {}).get("goodsGallery") or {}).get(color_key)
    return _gallery_urls(gal), data.get("baseInfo") or {}


def _prices(base):
    price = to_price(base.get("shopPrice"))
    old = to_price(base.get("noDealPrice"))
    return price, (old if old and price and old > price + 0.009 else None)


def scrape(f, url):
    html = f.get(url)
    nuxt = eval_nuxt(html)
    if not nuxt:
        raise SkipProduct("Không đọc được dữ liệu __NUXT__ của trang")
    state = nuxt.get("state") or {}
    prod = state.get("product") or {}
    page = (nuxt.get("data") or [{}])[0] or {}
    base = prod.get("baseInfo") or {}
    style = prod.get("styleInfo") or {}
    if page.get("show404") or not base.get("goodsId"):
        raise SkipProduct("Không thấy sản phẩm (404 hoặc trang không phải sản phẩm)")
    cur = (base.get("currencyPricesCurrent") or {}).get("currency") or state.get("currencyCode")
    if cur and cur != "USD":
        raise SkipProduct(f"Giá không phải USD ({cur})")
    if not base.get("isOnSale") or base.get("isDelete") or base.get("available") is False:
        raise SkipProduct("Sản phẩm ngừng bán")

    goods_id = base["goodsId"]
    dress_type = base.get("dressType") or "dress"
    brand = "Azazie"

    # ---- tên: từ mẫu link "azazie azaria {color} a-line ... bridesmaid dress" bỏ màu + hãng
    name = (base.get("goodsUrlName") or "").replace("{color}", " ")
    name = re.sub(r"\s+", " ", name).strip() or base.get("originGoodsUrlName") or base.get("goodsName") or ""
    title = strip_brand(_title_case(name), brand)

    # ---- mô tả + thông số
    det = page.get("detailsInfo") or {}
    bullets = html_to_lines((det.get("description") or {}).get("txt") or base.get("goodsDetails") or "")
    bullets = [strip_brand_desc(b, brand) for b in bullets if b]
    details = {}
    for x in det.get("showDetails") or []:
        k, v = x.get("name"), x.get("attr_value") or x.get("attr_key_values")
        if k and v:
            details[k] = clean_text(str(v))
    if base.get("fabric") and "Fabric" not in details:
        details["Fabric"] = base["fabric"]
    mt = prod.get("makeTimeInfo") or {}
    made_to_order = bool(base.get("dressOnSale"))
    if made_to_order:
        pt = mt.get("note") or (f"{mt['description']} production" if mt.get("description") else "")
        if pt:
            details["Processing Time"] = clean_text(pt)
        details["Made to Order"] = "Yes"
    rush = base.get("rushSelectItems") or []
    std = next((r for r in rush if r.get("key") == "standard"), None)
    if std and std.get("txt") and made_to_order:
        details["Standard Delivery"] = clean_text(std["txt"])

    # ---- màu (theo thứ tự site) và size chuẩn (không lấy custom size)
    colors_meta = [c for c in (style.get("color") or {}).values() if c.get("isShow", 1)]
    sizes = [s for s in (style.get("size") or []) if s.get("isShow", 1)]
    if not colors_meta:
        colors_meta = [{"key": base.get("color") or "", "name": base.get("shownColor") or ""}]
    size_names = [(s.get("key"), s.get("shownName") or s.get("name")) for s in sizes] or [("", "")]

    # ---- ảnh từng màu: màu đang mở + màu mặc định đã có trong trang, các màu khác gọi item-info song song
    gallery = (prod.get("imageInfo") or {}).get("goodsGallery") or {}
    imgs = {k: _gallery_urls(v) for k, v in gallery.items()}
    color_base = {}
    need = [c["key"] for c in colors_meta if c.get("key") and not imgs.get(c["key"])]
    if need:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(WORKERS) as ex:
            for key, (urls, b) in zip(need, ex.map(lambda k: _color_info(f, goods_id, dress_type, k), need)):
                imgs[key] = urls
                color_base[key] = b

    # ---- tồn kho: hàng may theo đơn -> mọi size chuẩn đều đặt được; hàng có sẵn (RTS) -> theo stockInfo
    stock = (page.get("stockInfo") or {}).get("colorMap") or {}

    def avail(ck, sk):
        if made_to_order:
            return True
        return bool(stock.get(ck, {}).get("hasStock") and sk in (stock.get(ck, {}).get("inStockSize") or {}))

    colors, variants = [], []
    for c in colors_meta:
        ck = c.get("key") or ""
        cname = _title_case(c.get("name") or ck.replace("_", " "))
        colors.append({"name": cname, "images": imgs.get(ck, [])})
        b = color_base.get(ck) or base
        if b.get("available") is False or (b.get("isOnSale") == 0):
            continue
        price, old = _prices(b if b.get("shopPrice") else base)
        for sk, sname in size_names:
            variants.append({"color": cname, "size": sname or "", "price": price, "list_price": old,
                             "available": avail(ck, sk), "id": f"{goods_id}-{ck}-{sk}".strip("-")})

    cat = base.get("catName") or base.get("defaultCatName") or state.get("catName") or ""
    low = cat.lower()
    if "flower girl" in low or "junior" in low or "girl" in low:
        dept = "Girls"
    elif "boy" in low or "ring bearer" in low:
        dept = "Boys"
    elif base.get("isGroomsmen") or "groom" in low or "men" == low[:3] or "suit" in low and "women" not in low:
        dept = "Men"
    else:
        dept = "Women"

    canon = base.get("goodsUrl") or ""
    return {
        "site": KEY,
        "code": str(goods_id),
        "url": SITE + canon if canon.startswith("/") else (canon or url),
        "title": title,
        "brand": brand,
        "bullets": bullets,
        "details": details,
        "breadcrumbs": [x for x in (cat,) if x],
        "department": dept,
        "size_name": "Size" if sizes else "",
        "colors": colors,
        "images": [],
        "variants": variants,
    }
