"""eBay Taxonomy API: gợi ý Category theo tên sản phẩm + danh sách item specifics (aspects) của Category.

Dùng key app eBay Developer (Client ID / Client secret, loại Production) — chỉ đọc danh mục,
không đụng tới tài khoản bán hàng nào. Kết quả lưu vào ebay_cache.json để lần sau không phải hỏi lại.
"""
import base64
import calendar
import re
import threading
import time
from urllib.parse import quote

import core

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
TREE = "https://api.ebay.com/commerce/taxonomy/v1/category_tree/0"   # 0 = eBay US
CACHE_PATH = core.ROOT / "ebay_cache.json"
CACHE_DAYS = 14

_lock = threading.Lock()
_token = {"value": "", "exp": 0, "key": ""}


def configured(cfg):
    return bool((cfg.get("ebay_client_id") or "").strip() and (cfg.get("ebay_client_secret") or "").strip())


class RateLimited(RuntimeError):
    """eBay trả HTTP 429: key đã dùng hết lượt gọi trong ngày (Taxonomy: 5.000 lượt / 24 giờ)."""


_limit = {"until": 0.0}


def limit_message():
    """Lý do + giờ eBay tính lại hạn mức (07:00 UTC mỗi ngày, đổi ra giờ máy)."""
    now = time.time()
    g = time.gmtime(now)
    reset = calendar.timegm((g.tm_year, g.tm_mon, g.tm_mday, 7, 0, 0, 0, 0, 0))
    if reset <= now:
        reset += 86400
    return ("eBay API đã hết hạn mức gọi trong ngày (HTTP 429) — hạn mức tính lại lúc "
            f"{time.strftime('%H:%M', time.localtime(reset))} (giờ máy này), hoặc đổi key eBay trong Cài đặt")


def reset_limit():
    """Người dùng bấm 'Hỏi lại' (hoặc đã đổi key): thử gọi eBay lại ngay."""
    _limit["until"] = 0.0


def _request(method, url, **kw):
    from curl_cffi import requests

    if time.time() < _limit["until"]:   # vừa bị 429: khỏi chờ thử lại từng dòng
        raise RateLimited(limit_message())
    last = None
    for attempt in range(3):
        try:
            r = requests.request(method, url, timeout=30, **kw)
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            if attempt == 0:   # có thể chỉ gọi dồn dập: chờ chút thử lại 1 lần
                time.sleep(3)
                last = RuntimeError("HTTP 429")
                continue
            _limit["until"] = time.time() + 600
            raise RateLimited(limit_message())
        if r.status_code in (500, 502, 503, 504):
            last = RuntimeError(f"HTTP {r.status_code}")
            time.sleep(2 * (attempt + 1))
            continue
        return r
    raise RuntimeError(f"Không kết nối được eBay API: {last}")


def token(cfg):
    """Application token (client credentials), giữ lại tới gần hết hạn (thường 2 giờ)."""
    cid, secret = cfg["ebay_client_id"].strip(), cfg["ebay_client_secret"].strip()
    key = cid + "|" + secret
    with _lock:
        if _token["value"] and _token["key"] == key and time.time() < _token["exp"] - 120:
            return _token["value"]
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        r = _request("POST", TOKEN_URL,
                     headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                     data="grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope")
        j = r.json() if r.text.strip().startswith("{") else {}
        if r.status_code != 200 or not j.get("access_token"):
            why = j.get("error_description") or j.get("error") or r.text[:200]
            raise RuntimeError(f"eBay từ chối key (HTTP {r.status_code}): {why} — kiểm tra Client ID / Client secret (loại Production)")
        _token.update(value=j["access_token"], exp=time.time() + int(j.get("expires_in", 7200)), key=key)
        return _token["value"]


def _get(cfg, url):
    r = _request("GET", url, headers={"Authorization": f"Bearer {token(cfg)}", "Accept-Language": "en-US",
                                      "Content-Type": "application/json"})
    if r.status_code == 401:   # token hết hạn sớm -> xin lại 1 lần
        _token["value"] = ""
        r = _request("GET", url, headers={"Authorization": f"Bearer {token(cfg)}", "Accept-Language": "en-US"})
    if r.status_code == 204:
        return {}
    if r.status_code != 200:
        raise RuntimeError(f"eBay API lỗi HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


# ---------------------------------------------------------------- cache

def _load_cache():
    return core.load_json(CACHE_PATH, {"suggest": {}, "aspects": {}})


def _fresh(entry):
    return isinstance(entry, dict) and time.time() - entry.get("t", 0) < CACHE_DAYS * 86400


def _save(section, key, data):
    """Lưu cache. Lưu lỗi (file bị chương trình khác giữ) thì bỏ qua — eBay đã trả lời, không làm hỏng kết quả."""
    with _lock:
        try:
            cache = _load_cache()
            cache.setdefault(section, {})[key] = {"t": time.time(), "data": data}
            core.save_json(CACHE_PATH, cache)
        except OSError as e:
            print("Không lưu được ebay_cache.json:", e)
            for tmp in CACHE_PATH.parent.glob(CACHE_PATH.name + ".*.tmp"):
                try:
                    tmp.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------- API

def suggest(cfg, query):
    """Các Category eBay gợi ý cho chuỗi tìm kiếm: [{id, name, path}] (tốt nhất đứng đầu)."""
    q = " ".join(query.split())[:300]
    hit = _load_cache()["suggest"].get(q.lower())
    if _fresh(hit):
        return hit["data"]
    j = _get(cfg, f"{TREE}/get_category_suggestions?q={quote(q)}")
    out = []
    for s in j.get("categorySuggestions", []):
        c = s.get("category", {})
        anc = [a.get("categoryName", "") for a in reversed(s.get("categoryTreeNodeAncestors", []))]
        out.append({"id": str(c.get("categoryId", "")), "name": c.get("categoryName", ""),
                    "path": " › ".join([*anc, c.get("categoryName", "")])})
    _save("suggest", q.lower(), out)
    return out


def aspects(cfg, category_id):
    """Item specifics của Category: [{name, required, mode, multi, variation, values}].

    mode: FREE_TEXT (gõ tự do) | SELECTION_ONLY (phải chọn đúng giá trị eBay cho)."""
    cid = str(category_id).strip()
    hit = _load_cache()["aspects"].get(cid)
    if _fresh(hit):
        return hit["data"]
    j = _get(cfg, f"{TREE}/get_item_aspects_for_category?category_id={cid}")
    out = []
    for a in j.get("aspects", []):
        c = a.get("aspectConstraint", {})
        out.append({
            "name": a.get("localizedAspectName", ""),
            "required": bool(c.get("aspectRequired")),
            "usage": c.get("aspectUsage", ""),
            "mode": c.get("aspectMode", "FREE_TEXT"),
            "variation": bool(c.get("aspectEnabledForVariations")),
            "max_len": c.get("aspectMaxLength") or 65,
            "values": [v.get("localizedValue", "") for v in a.get("aspectValues", [])][:400],
        })
    _save("aspects", cid, out)
    return out


def conditions(cfg, category_id):
    """Các tình trạng (Condition) eBay cho phép ở Category: {"1000": "New", "3000": "Used", ...} (có cache).
    Vd loa 14990 không cho "Refurbished" (chỉ người bán trong chương trình eBay Refurbished mới dùng được)."""
    cid = str(category_id).strip()
    hit = _load_cache().get("conditions", {}).get(cid)
    if _fresh(hit):
        return hit["data"]
    j = _get(cfg, "https://api.ebay.com/sell/metadata/v1/marketplace/EBAY_US/get_item_condition_policies"
                  f"?filter=categoryIds:%7B{cid}%7D")
    out = {}
    for p in j.get("itemConditionPolicies", []):
        for c in p.get("itemConditions", []):
            out[str(c.get("conditionId"))] = c.get("conditionDescription", "")
    _save("conditions", cid, out)
    return out


def test(cfg):
    """Nút "Kiểm tra key": xin token + gợi ý thử 1 sản phẩm."""
    if not configured(cfg):
        raise RuntimeError("Chưa điền Client ID / Client secret")
    _token["value"] = ""
    s = suggest(cfg, "girls quilted western dress")
    return s[0]["path"] + f" ({s[0]['id']})" if s else "key OK nhưng eBay không gợi ý được Category"


def query_for(prod):
    """Chuỗi hỏi eBay: tên hãng + Department + tên sản phẩm + nhóm hàng cuối trên site (nếu tên chưa có).

    Tên hãng CHỈ dùng ở đây (để eBay hiểu "Sonos Play" là loa, không phải đồ chơi) — tiêu đề và ô Brand của
    listing vẫn để Unbranded."""
    dept = core.DEPT_WORD.get((prod.get("department") or "").lower(), "")
    title = prod.get("title", "")
    title = title[4:] if title.lower().startswith("the ") else title
    if core.is_toy(prod):
        # Đồ chơi: "Kids <tên>" — tên hãng quần áo (Janie and Jack) / Girls' / "Toys and Books" kéo eBay sang
        # quần áo, sách; chữ Kids cho eBay hiểu đúng là đồ chơi trẻ em ("Camel Tunnel" không ra tượng lạc đà)
        return f"Kids {title}"
    crumbs = [c for c in prod.get("breadcrumbs", []) if c and c.lower() not in ("home", "shop", "all")]
    last = crumbs[-1] if crumbs else ""
    brand = prod.get("brand", "")
    parts = [brand if brand and brand.lower() not in title.lower() else "", dept, title]
    # Danh mục site gộp nhiều loại hàng ("Pants, Shorts + Jumpsuits", "Toys and Books") làm eBay gợi ý sai loại -> bỏ
    if last and last.lower() not in title.lower() and len(last) < 40 and not core.COMBINED_CRUMB.search(last):
        parts.append(last)
    return " ".join(p for p in parts if p)
