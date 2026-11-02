"""Theo dõi sản phẩm đã list (tracking.json cạnh tool).

Mỗi listing (khoá = SKU listing): Profile, link gốc, ngày list, từng biến thể đã đưa lên eBay (SKU biến thể, giá site +
giá gốc lúc list, giá eBay, số lượng). Nút "Kiểm tra giá & tồn kho" đọc lại link trên site rồi so với lúc list:
tăng / giảm giá quá ALERT đô, hết hàng, có hàng lại. File Revise chỉ đưa số lượng 0 cho size hết hàng.

Nguồn dữ liệu:
  - Xuất CSV (lúc chạy xong + nút Xuất CSV): tự thêm / cập nhật.
  - File list cũ (Excel / CSV / txt có Profile + link): dò dữ liệu lúc list trong các bảng đã lưu (sessions/*.json)
    và file CSV đã xuất (output/**.csv). Không còn dữ liệu thì lấy mốc ở lần kiểm tra đầu tiên.
  - File kết quả upload eBay (Status / CustomLabel / ItemID) hoặc báo cáo Active listings của Seller Hub:
    Success -> đã lên eBay + Item ID; Failure -> gỡ khỏi danh sách.
"""
import csv
import hashlib
import io
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import core

PATH = Path(os.environ.get("RETAIL_TRACKING") or core.ROOT / "tracking.json")   # biến môi trường: chỉ để chạy thử
ALERT = 2.0          # chênh giá (đô) mới báo
_lock = threading.RLock()


def load():
    return core.load_json(PATH, {"items": {}})


def update(fn):
    """Đọc -> sửa -> ghi trong 1 khoá (luồng xuất CSV và luồng kiểm tra không ghi đè nhau)."""
    with _lock:
        data = load()
        out = fn(data)
        core.save_json(PATH, data)
        return out


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def run_time(run_id, year=None):
    """'1014-1135' -> '2026-10-14 11:35' (thư mục lần chạy không ghi năm)."""
    m = re.match(r"(\d{2})(\d{2})-(\d{2})(\d{2})", run_id or "")
    if not m:
        return ""
    return f"{year or datetime.now().year}-{m[1]}-{m[2]} {m[3]}:{m[4]}"


# ---------------------------------------------------------------- dựng 1 dòng theo dõi

def variants_of(item, cfg):
    """Các biến thể đúng như dòng CSV đã xuất (SKU biến thể, giá eBay, số lượng) + giá site lúc đó."""
    rows = core.listing_rows(item, cfg)
    out = []
    for r in rows[1:] if len(rows) > 1 else rows:
        src, lst = r.get("_src") or (None, None)
        color, _, size = (r.get("_vkey") or "||").partition("||")
        out.append({"vsku": r["CustomLabel"], "key": r.get("_vkey", ""), "color": color, "size": size,
                    "rd": r.get("RelationshipDetails", ""), "site_price": src, "site_list": lst,
                    "ebay": float(r["*StartPrice"]), "qty": int(r.get("*Quantity") or 0)})
    return out


def entry_from(item, cfg, site, run_id="", when=""):
    prod = item["product"]
    return {
        "sku": item["sku"], "profile": item.get("profile", ""), "country": item.get("country") or "US",
        "site": site, "code": prod["code"], "url": item["url"],
        "title": core.fit_title(item, cfg.get("title_max_len", 80)), "pic": item.get("pic", ""),
        "pct": core.site_percent(cfg, site), "run_id": run_id, "added": when or _now(), "updated": _now(),
        "status": "exported", "item_id": "", "variants": variants_of(item, cfg), "check": None,
    }


def _merge(old, new):
    """Xuất lại listing đã theo dõi: giữ ngày list đầu tiên, Item ID, kết quả kiểm tra."""
    if old:
        new["added"] = old.get("added") or new["added"]
        new["profile"] = old.get("profile") or new["profile"]   # Profile đã đổi tay (vd EBTR -> TR) giữ nguyên
        if old.get("item_id"):
            new["item_id"], new["status"] = old["item_id"], old.get("status", "live")
        new["check"] = old.get("check")
    return new


def add_exported(items, cfg, site, run_id):
    """Gọi sau mỗi lần ghi CSV (chạy xong / Xuất CSV). Dòng không xuất được (thiếu Category...) thì bỏ qua."""
    new = {}
    for it in items:
        try:
            e = entry_from(it, cfg, site, run_id)
        except Exception:
            continue
        new[e["sku"]] = e

    def fn(d):
        for sku, e in new.items():
            d["items"][sku] = _merge(d["items"].get(sku), _alias(d, e))
    update(fn)
    return len(new)


# ---------------------------------------------------------------- nạp file list cũ

def _site_of(url, mods):
    """Module site của link; link bị dán hỏng (mất '://' và '/') thì dò theo tên miền."""
    for m in mods.values():
        if m.URL_RE.search(url):
            return m
    flat = re.sub(r"[^a-z0-9]", "", url.lower())
    for m in mods.values():
        dom = re.sub(r"[^a-z0-9]", "", (getattr(m, "BASE", "") or "").lower().replace("https://", "").replace("www.", ""))
        if dom and dom in flat:
            return m
    return None


def _code(mod, url):
    code = mod.product_code(url) or mod.product_code(url.lower())
    if not code and mod.KEY == "bananarepublic":   # link hỏng "…product.dopid=865416082&vid=1"
        m = re.search(r"pid=(\d{6,})", url)
        code = m.group(1) if m else None
    if not code and mod.KEY == "hollister":   # ".../super-baggy-jeans-63145079-1?..." (đuôi -1)
        m = re.search(r"-(\d{6,10})-\d{1,2}(?:[/?#]|$)", url)
        code = m.group(1) if m else None
    return code


def _csv_entry(sku, c, base):
    """Dòng theo dõi dựng từ file CSV đã xuất (không có giá site lúc list)."""
    return dict(base, sku=sku, title=c[3] or base.get("title", ""), run_id=c[0], added=run_time(c[0], c[1]),
                variants=[dict(v, key="", color="", size="", site_price=None, site_list=None) for v in c[2]],
                source="list cũ + file CSV đã xuất")


def _stored_rows():
    """Mọi dòng đã chạy xong trong các bảng đã lưu (kể cả bản sao lưu): [(site, run_id, năm, dòng)]."""
    out = []
    for f in sorted((core.ROOT / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime):
        site = f.stem.split("_")[0]
        d = core.load_json(f, {}) or {}
        year = datetime.fromtimestamp(f.stat().st_mtime).year
        for r in d.get("results", []):
            if r.get("status") == "ok" and r.get("product"):
                out.append((site, d.get("run_id") or "", year, r))
    return out


def _csv_index():
    """SKU listing -> (run_id, năm, [dòng biến thể], tiêu đề) từ các file CSV tool đã xuất (lấy file mới nhất)."""
    idx = {}
    for f in sorted(core.OUTPUT_DIR.rglob("*.csv"), key=lambda p: p.stat().st_mtime):
        if "revise" in str(f).lower() or "theo-doi" in str(f).lower():
            continue
        try:
            rows = list(csv.reader(open(f, encoding="utf-8-sig")))
        except (OSError, UnicodeDecodeError):
            continue
        hi = next((i for i, r in enumerate(rows) if r and r[0].startswith("*Action")), None)
        if hi is None:
            continue
        h = {k: i for i, k in enumerate(rows[hi])}
        if "CustomLabel" not in h or "*StartPrice" not in h:
            continue
        run_id = next((p for p in f.parts[::-1] if re.match(r"\d{4}-\d{4}", p)), "")
        year = datetime.fromtimestamp(f.stat().st_mtime).year
        cur = None
        for r in rows[hi + 1:]:
            if not r or len(r) < len(h):
                continue
            g = lambda k: r[h[k]] if k in h else ""   # noqa: E731
            try:
                if r[0] == "Add":
                    cur = [g("CustomLabel"), []]
                    idx[cur[0]] = (run_id, year, cur[1], g("*Title"))
                    if g("*StartPrice"):   # listing 1 biến thể
                        cur[1].append({"vsku": cur[0], "rd": "", "ebay": float(g("*StartPrice")), "qty": int(g("*Quantity") or 0)})
                elif cur and g("Relationship") == "Variation":
                    cur[1].append({"vsku": g("CustomLabel"), "rd": g("RelationshipDetails"),
                                   "ebay": float(g("*StartPrice") or 0), "qty": int(g("*Quantity") or 0)})
            except ValueError:
                continue
    return idx


class DataIndex:
    """Dữ liệu lúc list còn lưu trên máy (đọc 1 lần): bảng đã lưu (sessions/*.json) + file CSV đã xuất."""

    def __init__(self):
        self.by_sku = {}
        for site, run_id, year, r in _stored_rows():
            self.by_sku.setdefault(r.get("sku", ""), []).append((site, run_id, year, r))
        self.csv = _csv_index()

    @staticmethod
    def _keys(d, base):
        """SKU đúng mã + SKU có đuôi màu (-WHITE / -ALL: 1 acc list nhiều màu cùng mẫu)."""
        return ([base] if base in d else []) + sorted(k for k in d if k.startswith(base + "-"))

    def find(self, base_sku, url, mod, cfg, skeleton, tag=None):
        """Dữ liệu lúc list của 1 link. tag = màu của link (chỉ biết sau khi đọc link) để chọn đúng SKU có đuôi màu."""
        key = core.link_key(url, mod)
        cands = [x for k in self._keys(self.by_sku, base_sku) for x in self.by_sku[k]]
        hit = next((c for c in cands if core.link_key(c[3]["url"], mod) == key), None)
        if not hit and tag:
            hit = next((c for c in cands if c[3]["sku"] in (f"{base_sku}-{tag}", base_sku)), None)
        if not hit and len({c[3]["sku"] for c in cands}) == 1:
            hit = cands[-1]
        if hit:
            site, run_id, year, r = hit
            try:
                e = entry_from(r, cfg, mod.KEY, run_id, run_time(run_id, year))
            except Exception:
                e = None
            if e:
                e.update(profile=skeleton["profile"] or e["profile"], url=url, source="bảng đã lưu")
                c = self.csv.get(e["sku"])
                if c:   # giá eBay đúng như file đã up (cài đặt % có thể đã đổi từ hôm đó)
                    price = {v["vsku"]: v for v in c[2]}
                    for v in e["variants"]:
                        if v["vsku"] in price:
                            v["ebay"], v["qty"] = price[v["vsku"]]["ebay"], price[v["vsku"]]["qty"]
                return e
        keys = self._keys(self.csv, base_sku)
        pick = keys[0] if len(keys) == 1 else next((k for k in (f"{base_sku}-{tag}", base_sku) if tag and k in keys), None)
        return _csv_entry(pick, self.csv[pick], skeleton) if pick else None


def resolve(it, prod, cfg, idx, mod):
    """Dòng list cũ chưa tìm được dữ liệu lúc list (Hollister: số trên link khác mã từng màu trong SKU): đọc link
    xong biết mã + màu thật -> tìm lại. Không có thì lấy mốc = các size còn hàng bây giờ. Trả về dòng mới."""
    base = core.make_sku(cfg, it["site"], prod["code"], it["profile"])
    tag = re.sub(r"[^A-Za-z0-9]", "", prod.get("url_color") or "ALL")[:8].upper()
    skel = {k: v for k, v in it.items() if k not in ("resolve", "variants")}
    e = idx.find(base, it["url"], mod, cfg, skel, tag)
    if e is None:
        e = dict(skel, sku=base, variants=[], source="mốc lần kiểm tra đầu (không còn dữ liệu lúc list)")
    e["title"] = e.get("title") or prod.get("title", "")
    return e


IMG_URL = re.compile(r"(?i)\.(jpe?g|png|webp|gif)(\?|$|\|)|\|https?://")


def parse_export_csv(name, raw):
    """File CSV tool đã xuất để up eBay (dòng *Action...): [{sku, profile, country, site, run_id, title, variants}].
    Không phải loại file này -> None."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if "*Action(" not in text[:5000]:
        return None
    rows = list(csv.reader(io.StringIO(text)))
    hi = next((i for i, r in enumerate(rows) if r and r[0].startswith("*Action")), None)
    if hi is None:
        return None
    h = {k: i for i, k in enumerate(rows[hi])}
    m = re.search(r"Country=([A-Z]{2})", rows[hi][0])
    country = m.group(1) if m else "US"
    fm = re.match(r"([a-z]+)_(.+?)_\d+sp_(\d{4}-\d{4})", Path(name).stem)
    out, cur = [], None
    for r in rows[hi + 1:]:
        if not r or len(r) < len(h):
            continue
        g = lambda k: r[h[k]] if k in h else ""   # noqa: E731
        try:
            if r[0] == "Add":
                sku = g("CustomLabel")
                cur = {"sku": sku, "profile": fm.group(2) if fm else sku.split("-")[0], "country": country,
                       "site": fm.group(1) if fm else "", "run_id": fm.group(3) if fm else "", "title": g("*Title"),
                       "variants": []}
                out.append(cur)
                if g("*StartPrice"):
                    cur["variants"].append({"vsku": sku, "rd": "", "ebay": float(g("*StartPrice")), "qty": int(g("*Quantity") or 0)})
            elif cur and g("Relationship") == "Variation":
                cur["variants"].append({"vsku": g("CustomLabel"), "rd": g("RelationshipDetails"),
                                        "ebay": float(g("*StartPrice") or 0), "qty": int(g("*Quantity") or 0)})
        except ValueError:
            continue
    return out


def _url_from_code(site, code):
    """Site mở được trang sản phẩm chỉ bằng mã (khi không còn link gốc trong dữ liệu đã lưu)."""
    if site == "bananarepublic" and re.fullmatch(r"\d{6,9}", code):
        return f"https://bananarepublic.gap.com/browse/product.do?pid={code}&vid=1"
    if site == "sonos" and re.fullmatch(r"[a-z0-9-]+", code.lower()):
        return f"https://www.sonos.com/en-us/shop/{code.lower()}"
    return ""


def parse_list_file(name, raw):
    """[(Profile, link)] từ 1 file list: Excel / CSV (cột Profile + cột link) hoặc .txt (Profile = tên file)."""
    out = []
    if name.lower().endswith(".txt"):
        prof = Path(name).stem.strip()
        for line in raw.decode("utf-8-sig", "ignore").splitlines():
            line = line.strip()
            if line.lower().startswith("http"):
                out.append((prof, line.split()[0]))
        return out
    rows = core.read_table(name, raw)
    head = [str(c).strip().lower() for c in (rows[0] if rows else [])]
    pcol = next((i for i, c in enumerate(head) if c in ("profile", "acc", "account")), None)
    for r in rows:
        cells = [str(c).strip() for c in r]
        url = next((c for c in cells if c.lower().startswith("http") and not IMG_URL.search(c)), "")   # bỏ link ảnh
        if not url:
            continue
        if pcol is not None and pcol < len(cells):
            prof = cells[pcol]
        else:   # không có dòng tên cột: ô ngắn đầu tiên không phải link / mã nước / tên site
            prof = next((c for c in cells if c and not c.lower().startswith("http") and len(c) <= 12
                         and not re.fullmatch(r"[A-Z]{2}", c)), "")
        out.append((prof, url))
    return out


def _from_export(x, idx, cfg, mods):
    """1 listing trong file CSV đã xuất -> dòng theo dõi. Link + giá site lúc list lấy từ bảng đã lưu (cùng SKU);
    giá eBay / số lượng lấy đúng như trong file."""
    sku, site = x["sku"], x["site"]
    if site not in mods:   # tên file không đúng mẫu: đoán site theo tiền tố SKU (…-HOL-…)
        site = next((k for k in mods if (core.site_cfg(cfg, k).get("prefix") or "#") in sku), site)
    hit = (idx.by_sku.get(sku) or [None])[-1]
    e = None
    if hit:
        try:
            e = entry_from(hit[3], cfg, hit[0], hit[1] or x["run_id"], run_time(hit[1] or x["run_id"], hit[2]))
            e["source"] = "file CSV đã xuất + bảng đã lưu"
        except Exception:
            e = None
    if e:
        price = {v["vsku"]: v for v in x["variants"]}
        for v in e["variants"]:
            if v["vsku"] in price:
                v["ebay"], v["qty"] = price[v["vsku"]]["ebay"], price[v["vsku"]]["qty"]
    else:
        prefix = core.site_cfg(cfg, site).get("prefix", "") if site in mods else ""
        code = sku.split("-", 1)[1] if "-" in sku else sku
        code = code[len(prefix):] if prefix and code.startswith(prefix) else code
        url = _url_from_code(site, code)
        e = {"sku": sku, "profile": x["profile"], "country": x["country"], "site": site, "code": code, "url": url,
             "title": x["title"], "pic": "", "pct": core.site_percent(cfg, site) if site in mods else 0,
             "run_id": x["run_id"], "added": run_time(x["run_id"]), "updated": _now(), "status": "exported",
             "item_id": "", "check": None,
             "variants": [dict(v, key="", color="", size="", site_price=None, site_list=None) for v in x["variants"]],
             "source": "file CSV đã xuất" if url else "file CSV đã xuất — chưa có link gốc (nạp file list có link)"}
    e.update(profile=x["profile"] or e["profile"], country=x["country"] or e["country"])
    return e


def import_lists(files, cfg):
    """files: [(tên file, bytes)]. Trả về {added, updated, found, waiting, bad: [..]}."""
    import sites as registry

    mods = registry.load_all()
    idx = DataIndex()
    rep = {"added": 0, "updated": 0, "found": 0, "waiting": 0, "no_link": 0, "bad": []}
    new = {}
    for name, raw in files:
        exported = parse_export_csv(name, raw) if name.lower().endswith(".csv") else None
        if exported is not None:   # file CSV tool đã xuất để up eBay: có SKU + giá, không có link -> tra link đã lưu
            for x in exported:
                e = _from_export(x, idx, cfg, mods)
                rep["found" if e["url"] else "no_link"] = rep.get("found" if e["url"] else "no_link", 0) + 1
                new.setdefault(e["sku"], e)
            continue
        try:
            pairs = parse_list_file(name, raw)
        except Exception as e:
            rep["bad"].append(f"{name}: không đọc được ({e})")
            continue
        for prof, url in pairs:
            prof = core.sku_profile(prof) or prof   # "US 1" -> "US1" (giống SKU và file CSV)
            mod = _site_of(url, mods)
            code = _code(mod, url) if mod else None
            if not mod or not code:
                rep["bad"].append(f"{name}: link không nhận ra site / mã sản phẩm — {url[:80]}")
                continue
            if "://" not in url[:12]:   # link dán hỏng ("httpsbananarepublic.gap.combrowse…") -> dựng lại từ mã nếu được
                url = _url_from_code(mod.KEY, code) or url
            sku = core.make_sku(cfg, mod.KEY, code, prof)
            skel = {"sku": sku, "profile": prof, "country": "US", "site": mod.KEY, "code": code, "url": url,
                    "title": "", "pic": "", "pct": core.site_percent(cfg, mod.KEY), "run_id": "", "added": "",
                    "updated": _now(), "status": "exported", "item_id": "", "check": None, "variants": []}
            e = idx.find(sku, url, mod, cfg, skel)
            if e:
                rep["found"] += 1
            else:   # chưa biết SKU thật: lần kiểm tra đầu đọc link rồi tìm lại (khoá tạm theo link)
                lk = hashlib.md5(core.link_key(url, mod).encode()).hexdigest()[:6]
                e = dict(skel, sku=f"{sku}~{lk}", resolve=True, source="list cũ — chờ kiểm tra để tìm dữ liệu lúc list")
                rep["waiting"] += 1
            new.setdefault(e["sku"], e)

    def fn(d):
        for sku, e in new.items():
            if sku in d["items"]:
                rep["updated"] += 1
            else:
                rep["added"] += 1
            d["items"][sku] = _merge(d["items"].get(sku), _alias(d, e))
    update(fn)
    return rep


# ---------------------------------------------------------------- file kết quả upload / Active listings của eBay

def import_results(name, raw):
    """Success -> đã lên eBay (+ Item ID); Failure -> gỡ khỏi danh sách. Báo cáo Active listings: cập nhật Item ID,
    số lượng và giá đang bán của từng biến thể."""
    rows = core.read_table(name, raw)
    hi = next((i for i, r in enumerate(rows[:10]) if any(str(c).strip().lower() in ("customlabel", "custom label (sku)")
                                                        for c in r)), None)
    if hi is None:
        raise RuntimeError("Không thấy cột CustomLabel / Custom label (SKU) — cần file kết quả upload của eBay "
                           "hoặc báo cáo Active listings")
    h = {str(c).strip().lower(): i for i, c in enumerate(rows[hi])}
    col = lambda *names: next((h[n] for n in names if n in h), None)   # noqa: E731
    c_sku, c_id, c_st = col("customlabel", "custom label (sku)"), col("itemid", "item number"), col("status")
    c_qty, c_price = col("available quantity", "quantity"), col("current price", "start price")
    rep = {"live": 0, "removed": 0, "unknown": 0, "qty_updated": 0}

    def fn(d):
        items = d["items"]
        vidx = {v["vsku"]: (sku, v) for sku, it in items.items() for v in it.get("variants", [])}
        for r in rows[hi + 1:]:
            get = lambda i: str(r[i]).strip() if i is not None and i < len(r) else ""   # noqa: E731
            sku, iid, st = get(c_sku), get(c_id), get(c_st).lower()
            if not sku:
                continue
            if sku in items:
                it = items[sku]
                if st and st not in ("success", "warning"):
                    if it.get("status") != "live":
                        del items[sku]
                        rep["removed"] += 1
                    continue
                it["status"], rep["live"] = "live", rep["live"] + 1
                if iid:
                    it["item_id"] = iid
            elif sku in vidx and (c_qty is not None or c_price is not None):
                psku, v = vidx[sku]
                if get(c_qty).isdigit():
                    v["qty"] = int(get(c_qty))
                    rep["qty_updated"] += 1
                p = re.sub(r"[^\d.]", "", get(c_price))
                if p:
                    v["ebay"] = float(p)
                items[psku]["status"] = "live"
                if iid:
                    items[psku]["item_id"] = iid
            else:
                rep["unknown"] += 1
    update(fn)
    return rep


def set_profile(skus, profile, remember=True):
    """Đổi Profile các dòng (SKU trên eBay giữ nguyên). remember: ghi nhớ tên cũ -> tên mới (vd EBTR = TR) để các
    lần xuất / nạp file sau tự gộp."""
    profile = (profile or "").strip()

    def fn(d):
        al = d.setdefault("aliases", {})
        olds, n = set(), 0
        for s in skus:
            it = d["items"].get(s)
            if it and it.get("profile") != profile:
                olds.add(it.get("profile") or "")
                it["profile"] = profile
                n += 1
        if remember:
            for o in olds - {"", profile}:
                al[o] = profile
            for o, v in list(al.items()):   # TR -> EBTR rồi EBTR -> TR: bỏ vòng lặp
                if v == o:
                    del al[o]
        return n
    return update(fn)


def add_alias(name, to):
    """Gộp Profile: name = to (vd EBTR = TR). Đổi luôn các dòng đang theo dõi, lần xuất / nạp file sau tự gộp."""
    name, to = (name or "").strip(), (to or "").strip()
    if not name or not to or name == to:
        raise RuntimeError("Điền 2 tên Profile khác nhau, vd EBTR = TR")

    def fn(d):
        d.setdefault("aliases", {})[name] = to
        n = 0
        for it in d["items"].values():
            if (it.get("profile") or "") == name:
                it["profile"] = to
                n += 1
        return n
    return update(fn)


def drop_alias(name):
    def fn(d):
        return (d.get("aliases") or {}).pop(name, None) is not None
    return update(fn)


def _alias(d, e):
    e["profile"] = (d.get("aliases") or {}).get(e.get("profile") or "", e.get("profile") or "")
    return e


# ---- lượt kiểm tra đang dở (lưu trong tracking.json: tắt tool rồi bật lại vẫn "Chạy tiếp" được)

def run_start(skus):
    def fn(d):
        d["run"] = {"skus": list(skus), "done": [], "started": _now()}
    update(fn)


def run_info():
    r = load().get("run")
    if not r:
        return None
    return {"total": len(r["skus"]), "done": len(set(r["done"])), "started": r.get("started", "")}


def run_left():
    """Các SKU của lượt dở chưa kiểm tra, đúng thứ tự (dòng list cũ đã được gộp vào SKU thật thì bỏ)."""
    d = load()
    r = d.get("run") or {}
    done = set(r.get("done") or [])
    return [k for k in r.get("skus") or [] if k not in done and k in d["items"]]


def run_clear():
    update(lambda d: d.pop("run", None))


def remove(skus):
    def fn(d):
        n = 0
        for s in skus:
            if d["items"].pop(s, None) is not None:
                n += 1
        return n
    return update(fn)


# ---------------------------------------------------------------- kiểm tra lại trên site

def judge(v, now):
    """So 1 biến thể với site bây giờ. now = biến thể site (None = không còn trên site)."""
    if now is None or not now.get("available"):
        return {"now": now.get("price") if now else None, "available": False,
                "state": "oos" if v.get("qty", 0) > 0 else "same", "gone": now is None}
    return {"now": now["price"], "list": now.get("list_price"), "available": True}


def check_listing(it, prod, cfg):
    """Kết quả kiểm tra 1 listing với dữ liệu site vừa đọc. Listing chưa có biến thể (list cũ không còn dữ liệu):
    lấy mốc = các biến thể còn hàng bây giờ."""
    pct, rnd = core.site_percent(cfg, it["site"]), cfg.get("round_99", True)
    by_key = {core.vkey(v): v for v in prod["variants"]}
    by_vsku = {}
    for n, v in enumerate(prod["variants"], 1):
        by_vsku.setdefault(core.variant_sku(it["sku"], v["color"], v["size"], n), v)
    if not it.get("variants"):
        it["variants"] = [{"vsku": core.variant_sku(it["sku"], v["color"], v["size"], n), "key": core.vkey(v),
                           "color": v["color"], "size": v["size"], "rd": "", "site_price": v["price"],
                           "site_list": v.get("list_price"), "ebay": core.ebay_price(core.base_price(v, it["site"]), pct, rnd),
                           "qty": int(cfg.get("quantity", 1)), "baseline": True}
                          for n, v in enumerate(prod["variants"], 1) if v.get("available")]
    res = {}
    for v in it["variants"]:
        now = by_key.get(v.get("key")) if v.get("key") else by_vsku.get(v["vsku"])
        r = judge(v, now)
        if r["available"]:
            # Hollister / Eileen Fisher: giá eBay theo giá gốc (không theo giá sale)
            want = core.ebay_price(r["list"] if core.price_basis(it["site"]) == "list" and r.get("list") else r["now"], pct, rnd)
            diff = round(want - v["ebay"], 2)
            r.update(want=want, diff=diff,
                     state="back" if v.get("qty", 0) == 0 else "up" if diff > ALERT else "down" if diff < -ALERT else "same")
        res[v["vsku"]] = r
    it["title"] = it.get("title") or prod.get("title", "")
    return res


# ---------------------------------------------------------------- file Revise: số lượng 0 cho size hết hàng

ACTION = "*Action(SiteID=US|Country={country}|Currency=USD|Version=1193|CC=UTF-8)"


def revise_plan(data):
    """{Profile: [(listing, [biến thể hết hàng])]} + danh sách listing hết hàng nhưng chưa có Item ID."""
    plan, no_id = {}, []
    for it in data["items"].values():
        chk = (it.get("check") or {}).get("vars") or {}
        oos = [v for v in it.get("variants", []) if chk.get(v["vsku"], {}).get("state") == "oos"]
        if not oos:
            continue
        if not it.get("item_id"):
            no_id.append(it["sku"])
            continue
        plan.setdefault((it.get("profile", ""), it.get("country") or "US"), []).append((it, oos))
    return plan, no_id


def write_revise(folder):
    """Mỗi Profile 1 file. Ghi xong: các biến thể đó coi như số lượng 0 (lần kiểm tra sau có hàng lại sẽ báo)."""
    data = load()
    plan, no_id = revise_plan(data)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%m%d-%H%M")
    files, done = [], set()
    for (prof, country), lst in plan.items():
        path = core.unique_path(folder / f"revise-SL0_{core.sku_profile(prof) or 'khong-profile'}_{len(lst)}sp_{stamp}.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, lineterminator="\r\n")
            w.writerow(["Info", "Version=1.0.0", "Template=fx_category_template_EBAY_US", "", "", ""])
            w.writerow([ACTION.format(country=country), "ItemID", "CustomLabel", "Relationship", "RelationshipDetails",
                        "*Quantity"])
            for it, oos in lst:
                single = len(it["variants"]) == 1 and it["variants"][0]["vsku"] == it["sku"]
                if single:
                    w.writerow(["Revise", it["item_id"], it["sku"], "", "", "0"])
                else:
                    w.writerow(["Revise", it["item_id"], it["sku"], "", "", ""])
                    for v in oos:
                        w.writerow(["", "", v["vsku"], "Variation", v["rd"], "0"])
                done.update(v["vsku"] for v in oos)
        files.append(path)

    def fn(d):
        for it in d["items"].values():
            for v in it.get("variants", []):
                if v["vsku"] in done:
                    v["qty"] = 0
                    st = ((it.get("check") or {}).get("vars") or {}).get(v["vsku"])
                    if st:
                        st["state"] = "same"
                        st["revised"] = _now()
    update(fn)
    return files, no_id, len(done)
