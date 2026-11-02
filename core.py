"""Phần xử lý: cài đặt, tính giá, up ảnh (freeimage.host / Cloudflare R2), ghi CSV eBay File Exchange theo Profile."""
import base64
import collections
import copy
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from sites.base import parse_proxy_line, parse_proxy_lines, strip_links

if getattr(sys, "frozen", False):
    # Chạy dạng .exe: dữ liệu (config, output, cache, chrome-profile) nằm cạnh file .exe,
    # còn giao diện + config mặc định đóng gói bên trong exe
    ROOT = Path(sys.executable).resolve().parent
    ASSETS = Path(sys._MEIPASS)
else:
    # Chạy từ mã nguồn (python app.py, script thử): dùng CHUNG dữ liệu với bản exe trong dist\
    ASSETS = Path(__file__).resolve().parent
    ROOT = Path(os.environ["RETAIL_ROOT"]) if os.environ.get("RETAIL_ROOT") else ASSETS / "dist"   # RETAIL_ROOT: chạy thử riêng
    ROOT.mkdir(exist_ok=True)
CONFIG_PATH = ROOT / "config.json"
CACHE_PATH = ROOT / "image_cache.json"
PROFILE_DIR = ROOT / "chrome-profile"
OUTPUT_DIR = ROOT / "output"
TEMPLATE_DIR = ROOT / "file mau"


class Stopped(Exception):
    """Người dùng bấm Dừng."""


# ---------------------------------------------------------------- config mặc định

# Mỗi site: tiền tố SKU, có luôn đi qua proxy không (nút ở đầu tab), nghỉ giữa link
SITE_DEFAULTS = {
    # use_proxy = "Luôn đi qua proxy". Mặc định tắt: chạy IP gốc, bị chặn thì tự chuyển sang proxy
    "theory":            {"prefix": "TH-",  "use_proxy": False},
    "bananarepublic":    {"prefix": "BR-",  "use_proxy": False},
    "touchofclass":      {"prefix": "TOC-", "use_proxy": False},
    "janieandjack":      {"prefix": "JJ-",  "use_proxy": False},
    "eileenfisher":      {"prefix": "EF-",  "use_proxy": False},
    "azazie":            {"prefix": "AZ-",  "use_proxy": False},
    "sohohome":          {"prefix": "SH-",  "use_proxy": False},
    "talbots":           {"prefix": "TB-",  "use_proxy": False, "delay": "8"},   # PerimeterX: đi chậm
    "personalcreations": {"prefix": "PC-",  "use_proxy": False},
    "tommybahama":       {"prefix": "TBH-", "use_proxy": False},
    "prana":             {"prefix": "PRA-", "use_proxy": False, "delay": "4"},
    "sonos":             {"prefix": "SON-", "use_proxy": False},
    "hollister":         {"prefix": "HOL-", "use_proxy": False},
    "fanatics":          {"prefix": "FAN-", "use_proxy": False},   # IP Việt Nam bị chặn -> tự chuyển proxy
    "bestbuy":           {"prefix": "BBY-", "use_proxy": False, "delay": "0"},   # mở từng trang bằng Camoufox
}

# Quy tắc chọn Category theo từ khoá (tìm trong breadcrumb + tên sản phẩm, quy tắc trên cùng khớp trước).
# dept: chỉ áp dụng khi Department sản phẩm khớp (để trống = mọi Department).
# specifics: item specifics thêm cho Category đó, dạng "Type=Dress|Style=A-Line".
# ID Category lấy theo cây danh mục eBay US — kiểm tra lại trên eBay trước khi up số lượng lớn.
DEFAULT_RULES = [
]   # để trống: eBay tự gợi ý Category theo tên sản phẩm; thêm quy tắc khi muốn ép Category cho 1 nhóm hàng

DEFAULT_CONFIG = {
    "price_percent": 130,          # giá eBay = giá site × % (sửa riêng từng site được)
    "round_99": True,              # làm tròn lên x.99
    "quantity": 2,                 # số lượng mỗi biến thể còn hàng
    "skip_out_of_stock": True,     # chọn sẵn: True = chỉ tích biến thể còn hàng; False = tích cả hết hàng (số lượng 0)
    "max_images": 12,              # số ảnh tối đa ở listing chính (eBay cho tối đa 24)
    "max_images_per_color": 4,     # số ảnh tối đa mỗi màu (ảnh biến thể)
    "title_max_len": 80,
    "brand_in_title": True,        # tên hãng ở đầu tiêu đề (ô Brand vẫn Unbranded, mô tả vẫn bỏ tên hãng)
    "condition_id": "1000",
    "brand": "Unbranded",
    "shipping_profile": "",
    "return_profile": "",
    "payment_profile": "",
    "location": "",
    "delay_seconds": 2.0,
    "max_parallel_sites": 3,       # số site chạy cùng lúc (mỗi site 1 luồng riêng)
    "upload_images": True,
    "image_size": 1600,
    "square_image": True,
    "image_host": "auto",          # auto = freeimage.host -> ImgBB -> Cloudflare R2 (hỏng mới chuyển); r2 = chỉ R2
    "freeimage_api_key": "6d207e02198a847aa98d0a2a901485a5",
    "imgbb_api_key": "",
    "r2_account_id": "", "r2_access_key_id": "", "r2_secret_access_key": "",
    "r2_bucket": "", "r2_public_url": "", "r2_folder": "retail",
    "proxy_file": "",              # (cũ) file proxy — chỉ dùng khi chưa dán proxy nào vào tool
    "proxies_enc": "",             # danh sách proxy đã dán, được mã hoá (giao diện chỉ hiện ip:port)
    "ebay_client_id": "", "ebay_client_secret": "",   # eBay Developer app (Production) để tự chọn Category
    "auto_category": True,
    "gemini_api_key": "", "gemini_model": "gemini-flash-lite-latest",   # Google AI Studio (miễn phí) để viết tiêu đề
    "ai_titles": False,            # tự viết tiêu đề bằng AI khi chạy xong
    "ai_title_notes": "",          # yêu cầu thêm cho AI (vd "luôn ghi size range")
    "ai_prompt": "",               # quy tắc viết tiêu đề gửi cho AI (trống = mặc định trong ai_title.py)
    "footer_bullets": ["Brand new, never worn.", "Ships from the USA."],
    "show_details": True,
    "personalize_text": "Enter your personalization text here.",
    "category_rules": DEFAULT_RULES,
    "sites": SITE_DEFAULTS,
    "export_dir": "",
}

# Các ô sửa được từ giao diện, kèm kiểu dữ liệu
EDITABLE = {
    "price_percent": float, "round_99": bool, "quantity": int, "skip_out_of_stock": bool, "brand_in_title": bool,
    "max_images": int, "max_images_per_color": int, "condition_id": str, "brand": str,
    "shipping_profile": str, "return_profile": str, "payment_profile": str, "location": str,
    "delay_seconds": float, "max_parallel_sites": int, "upload_images": bool, "image_host": str, "show_details": bool,
    "personalize_text": str, "ebay_client_id": str, "auto_category": bool,
    "gemini_model": str, "ai_titles": bool, "ai_title_notes": str, "ai_prompt": str,
    "r2_account_id": str, "r2_access_key_id": str, "r2_bucket": str, "r2_public_url": str, "r2_folder": str,
}
SECRET_FIELDS = ("r2_secret_access_key", "ebay_client_secret", "gemini_api_key", "imgbb_api_key")
SITE_FIELDS = {"prefix": str, "use_proxy": bool, "price_percent": str,
               "shipping_profile": str, "delay": str}


def site_delay(cfg, key):
    """Số giây nghỉ giữa 2 link của site (ô riêng của site; trống = dùng số chung)."""
    raw = str(site_cfg(cfg, key).get("delay") or "").strip()
    try:
        return max(0.0, float(raw)) if raw else float(cfg.get("delay_seconds", 2))
    except ValueError:
        return float(cfg.get("delay_seconds", 2))


# ---------------------------------------------------------------- config / cache

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


_SAVE_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()


def save_json(path, data):
    # Nhiều luồng cùng ghi (lần chạy + up ảnh màu mới chọn): khoá + file tạm riêng để không giẫm nhau trên Windows
    with _SAVE_LOCK:
        tmp = path.with_suffix(path.suffix + f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        for attempt in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError:   # Windows: file đang được chương trình khác mở đọc (diệt virus, tool khác) -> chờ chút
                if attempt == 4:
                    tmp.unlink(missing_ok=True)   # không để lại file tạm vài MB
                    raise
                time.sleep(0.2 * (attempt + 1))


def load_config():
    """Config trên đĩa, bù các khoá còn thiếu từ DEFAULT_CONFIG (bản cũ / lần đầu chạy)."""
    if not CONFIG_PATH.exists() and (ASSETS / "config.default.json").exists():
        shutil.copy(ASSETS / "config.default.json", CONFIG_PATH)   # lần đầu chạy exe ở thư mục mới
    cfg = load_json(CONFIG_PATH, {})
    # proxy_file = "" nghĩa là người dùng đã bấm Xoá hết proxy -> không tự lấy lại file Proxy.txt
    first_time = "proxy_file" not in cfg
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, copy.deepcopy(v))
    sites = cfg["sites"]
    for key, d in SITE_DEFAULTS.items():
        sites.setdefault(key, {})
        sites[key].pop("category", None)   # bỏ "Category mặc định của site" (đã có eBay API + quy tắc ép)
        for k, v in d.items():
            sites[key].setdefault(k, v)
    if first_time:
        cfg["proxy_file"] = str(default_proxy_file())
    return cfg


# ---------------------------------------------------------------- proxy dán vào tool

# Mã hoá nhẹ để mở config.json bằng Notepad không đọc được user/pass ngay.
# Không phải bảo mật tuyệt đối: người rành máy tính vẫn giải được từ chính tool này.
_PX_KEY = b"RetailToEbay|proxy|v1"


def _xor(data):
    return bytes(b ^ _PX_KEY[i % len(_PX_KEY)] for i, b in enumerate(data))


def encode_proxies(lines):
    raw = "\n".join(lines).encode("utf-8")
    return "v1:" + base64.urlsafe_b64encode(_xor(raw)).decode() if lines else ""


def decode_proxies(enc):
    if not enc or not enc.startswith("v1:"):
        return []
    try:
        return [x for x in _xor(base64.urlsafe_b64decode(enc[3:])).decode("utf-8").splitlines() if x.strip()]
    except Exception:
        return []


def proxy_lines(cfg):
    """Danh sách proxy đang dùng: proxy đã dán vào tool; chưa dán thì đọc file proxy (cách cũ)."""
    lines = decode_proxies(cfg.get("proxies_enc", ""))
    if lines:
        return lines
    try:
        return [x.strip() for x in Path(cfg.get("proxy_file", "")).read_text(encoding="utf-8", errors="ignore").splitlines()
                if parse_proxy_line(x)]
    except OSError:
        return []


def site_proxy_file(cfg, site):
    """File proxy riêng của 1 site: Proxy-<site>.txt (vd Proxy-bestbuy.txt) cạnh file proxy chung, cạnh tool, hoặc trên Desktop."""
    if not site:
        return None
    name = f"Proxy-{site}.txt"
    places = [Path(cfg["proxy_file"]).parent / name] if cfg.get("proxy_file") else []
    places += [ROOT / name, Path.home() / "Desktop" / name]
    for p in places:
        try:
            if p.is_file():
                return p
        except OSError:
            pass
    return None


def site_proxy_lines(cfg, site):
    """Proxy riêng của 1 site: dán trong Cài đặt -> Proxy riêng theo site (sites[site].proxies_enc). Chưa từng dán trong tool
    thì đọc file Proxy-<site>.txt nếu có (đã dán rồi, kể cả xoá hết, thì không đọc file nữa)."""
    if not site:
        return []
    d = (cfg.get("sites") or {}).get(site) or {}
    if "proxies_enc" in d:
        return decode_proxies(d.get("proxies_enc", ""))
    p = site_proxy_file(cfg, site)
    if not p:
        return []
    try:
        return [x.strip() for x in p.read_text(encoding="utf-8", errors="ignore").splitlines() if parse_proxy_line(x)]
    except OSError:
        return []


def proxies_for(cfg, site=None):
    """Proxy cho 1 lần chạy của site: proxy riêng của site (nếu có) đứng trước, rồi đến proxy chung; trùng thì bỏ.
    Thứ tự dùng: IP gốc -> proxy riêng -> proxy chung. Site không có proxy riêng thì chỉ có proxy chung như cũ."""
    return parse_proxy_lines(list(dict.fromkeys(site_proxy_lines(cfg, site) + proxy_lines(cfg))))


def masked_site_proxies(cfg):
    """Cho giao diện: {site: [{addr, auth}]} của các site đang có proxy riêng — không gửi user/pass."""
    out = {}
    for key in SITE_DEFAULTS:
        items = []
        for line in site_proxy_lines(cfg, key):
            p = parse_proxy_line(line)
            if p:
                items.append({"addr": p["server"].split("://", 1)[1], "auth": bool(p.get("username"))})
        if items:
            out[key] = items
    return out


def edit_site_proxies(cfg, site, action, text="", index=None):
    """Thêm / thay / xoá proxy riêng của 1 site (Cài đặt -> Proxy riêng theo site). Trả về (cfg, số dòng hợp lệ, số dòng sai)."""
    if site not in SITE_DEFAULTS:
        raise RuntimeError(f"Không có site {site!r}")
    cur = site_proxy_lines(cfg, site)
    new = [x.strip() for x in (text or "").splitlines() if x.strip()]
    good = [x for x in new if parse_proxy_line(x)]
    bad = len(new) - len(good)
    if action == "add":
        cur = list(dict.fromkeys(cur + good))
    elif action == "replace":
        cur = list(dict.fromkeys(good))
    elif action == "remove" and index is not None and 0 <= int(index) < len(cur):
        cur.pop(int(index))
    elif action == "clear":
        cur = []
    cfg.setdefault("sites", {}).setdefault(site, {})["proxies_enc"] = encode_proxies(cur)
    save_json(CONFIG_PATH, cfg)
    return cfg, len(good), bad


def masked_proxies(cfg):
    """Cho giao diện: chỉ ip:port + có mật khẩu hay không — không bao giờ gửi user/pass ra ngoài."""
    out = []
    for line in proxy_lines(cfg):
        p = parse_proxy_line(line)
        if p:
            out.append({"addr": p["server"].split("://", 1)[1], "auth": bool(p.get("username"))})
    return out


def edit_proxies(cfg, action, text="", index=None):
    """Thêm / thay / xoá proxy. Trả về (cfg, số dòng hợp lệ, số dòng sai dạng)."""
    cur = decode_proxies(cfg.get("proxies_enc", "")) or proxy_lines(cfg)
    new = [x.strip() for x in (text or "").splitlines() if x.strip()]
    good = [x for x in new if parse_proxy_line(x)]
    bad = len(new) - len(good)
    if action == "add":
        cur = list(dict.fromkeys(cur + good))
    elif action == "replace":
        cur = list(dict.fromkeys(good))
    elif action == "remove" and index is not None and 0 <= int(index) < len(cur):
        cur.pop(int(index))
    elif action == "clear":
        cur = []
    cfg["proxies_enc"] = encode_proxies(cur)
    if not cur:
        cfg["proxy_file"] = ""   # xoá hết = không dùng file cũ nữa
    save_json(CONFIG_PATH, cfg)
    return cfg, len(good), bad


def default_proxy_file():
    """Proxy.txt cạnh tool, không có thì Proxy.txt trên Desktop."""
    here = ROOT / "Proxy.txt"
    if here.exists():
        return here
    return Path.home() / "Desktop" / "Proxy.txt"


def update_config(changes):
    cfg = load_config()
    for key, typ in EDITABLE.items():
        if key in changes:
            val = changes[key]
            cfg[key] = typ(val.strip() if isinstance(val, str) else val)
    if "footer_bullets" in changes:
        cfg["footer_bullets"] = [s.strip() for s in changes["footer_bullets"] if s.strip()]
    if "category_rules" in changes:
        rules = []
        for r in changes["category_rules"]:
            r = {k: str(r.get(k, "")).strip() for k in ("keywords", "dept", "category", "specifics")}
            if r["keywords"] or r["category"]:
                rules.append(r)
        cfg["category_rules"] = rules
    for key, vals in (changes.get("sites") or {}).items():
        if key in cfg["sites"]:
            for k, typ in SITE_FIELDS.items():
                if k in vals:
                    v = vals[k]
                    cfg["sites"][key][k] = typ(v.strip() if isinstance(v, str) else v)
    for key in SECRET_FIELDS:
        # Ô bí mật để trống = giữ giá trị đã lưu
        if (changes.get(key) or "").strip():
            cfg[key] = changes[key].strip()
    save_json(CONFIG_PATH, cfg)
    return cfg


def public_config(cfg):
    """Config gửi ra giao diện: không kèm secret, chỉ báo đã lưu hay chưa."""
    out = {k: v for k, v in cfg.items() if k not in SECRET_FIELDS and k != "freeimage_api_key"}
    for key in SECRET_FIELDS:
        out[key + "_set"] = bool(cfg.get(key))
    out.pop("proxies_enc", None)
    out.pop("proxy_file", None)
    # proxy riêng của site cũng chỉ gửi ip:port (bỏ chuỗi đã mã hoá ra khỏi sites)
    out["sites"] = {k: {f: v for f, v in (d or {}).items() if f != "proxies_enc"} for k, d in (cfg.get("sites") or {}).items()}
    out["site_proxy_lists"] = masked_site_proxies(cfg)
    out["proxy_list"] = masked_proxies(cfg)
    out["proxy_count"] = len(out["proxy_list"])
    out["proxy_source"] = "tool" if decode_proxies(cfg.get("proxies_enc", "")) else ("file" if out["proxy_count"] else "")
    return out


def site_cfg(cfg, key):
    return cfg["sites"].get(key) or dict(SITE_DEFAULTS.get(key, {}))


def site_percent(cfg, key):
    """% giá của site: ô riêng của site (để trống = dùng % chung)."""
    raw = str(site_cfg(cfg, key).get("price_percent") or "").strip()
    try:
        return float(raw) if raw else float(cfg["price_percent"])
    except ValueError:
        return float(cfg["price_percent"])


# ---------------------------------------------------------------- link parsing

def split_urls(text):
    """Tách mọi link trong ô dán: nhiều link trên 1 dòng, link dính liền nhau vẫn tách được.
    Trả về (list link, list dòng không có link nào)."""
    urls, junk = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"(?=https?://)", line) if p.strip()]
        found = [p.split()[0] for p in parts if p.lower().startswith("http")]
        if found:
            urls.extend(u.rstrip(",;)>]\"'") for u in found)   # "…/era-100," / "<…>" / "…" -> bỏ dấu dính cuối
        else:
            junk.append(line)
    return urls, junk


def check_link(url, mod, sites):
    """(mã sản phẩm hoặc url, lý do bỏ) cho 1 link trong tab của site mod."""
    if not mod.URL_RE.search(url):
        other = next((m for m in sites.values() if m.URL_RE.search(url)), None)
        why = f"link {other.NAME} — dùng tab {other.NAME}" if other else f"không phải link sản phẩm {mod.NAME}"
        return None, why
    return link_key(url, mod), ""


def link_key(url, mod):
    """Khoá bắt link trùng: mã sản phẩm + phần link chọn màu / size (2 link cùng sản phẩm khác màu KHÔNG trùng)."""
    from urllib.parse import parse_qsl, urlparse

    u = urlparse(url.strip())
    code = mod.product_code(url) or mod.product_code(url.lower()) or ""
    # tham số chọn màu / size; "pid" của Banana Republic = mã kiểu + mã màu (811426032 = kiểu 811426 màu 032)
    # Hollister: "seq" = màu (swatch) đang chọn — cùng mẫu khác seq là khác màu, không phải link trùng
    pick_re = r"(?i)colou?r|size|fit|^pid$" + (r"|^seq$" if getattr(mod, "KEY", "") == "hollister" else "")
    pick = sorted((k.lower(), v.lower()) for k, v in parse_qsl(u.query) if re.search(pick_re, k))
    path = u.path.lower().rstrip("/")   # màu nằm trong đường dẫn (Tommy Bahama /p/ST228252-18406, Touch of Class slug)
    return f"{code or path}|{path if code else ''}|{pick}"


def parse_link_list(text, mod, sites):
    """Đọc ô dán link của 1 tab. Trả về (list (key, url), list link bỏ qua kèm lý do)."""
    urls, junk = split_urls(text)
    out, skipped, seen = [], [f"{j[:80]} — không phải link" for j in junk], {}
    for url in urls:
        key, why = check_link(url, mod, sites)
        if why:
            skipped.append(f"{url[:90]} — {why}")
            continue
        if key in seen:
            skipped.append(f"{url[:90]} — trùng với link thứ {seen[key]}")
            continue
        seen[key] = len(out) + 1
        out.append((key, url))
    return out, skipped


# ---------------------------------------------------------------- ảnh

# Yêu cầu ảnh của eBay: https, cạnh dài >= 500px, <= 12MB, eBay lưu tối đa 1600x1600, khuyên dùng ảnh vuông 1:1
EBAY_MIN_PX = 500
EBAY_MAX_BYTES = 12 * 1024 * 1024


def download_image(src_url, proxies=None, referer=None):
    from curl_cffi import requests
    from urllib.parse import quote

    # CDN có thể trả URL Unicode (vd Yandy Díaz). curl_cffi cần URL và
    # Referer dạng ASCII; giữ nguyên escape %xx và các dấu phân cách URL.
    src_url = quote(src_url, safe=":/?#[]@!$&'()*+,;=%")
    if referer:
        referer = quote(referer, safe=":/?#[]@!$&'()*+,;=%")

    last = None
    for attempt in range(3):
        try:
            # Không xin AVIF/WebP: vài CDN (vd Gap) trả AVIF theo Accept, Pillow có thể không đọc được
            headers = {"Accept": "image/jpeg,image/png;q=0.9,*/*;q=0.5"}
            if referer:
                headers["Referer"] = referer
            r = requests.get(src_url, impersonate="chrome", timeout=90, proxies=proxies, headers=headers)
            if r.status_code in (403, 404, 410):   # ảnh không có / bị chặn: tải lại cũng vậy
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.content
        except Exception as e:
            last = e
            if str(e).startswith("HTTP 4"):
                break
            if attempt < 2:
                time.sleep(3 * (attempt + 1))  # mạng chập chờn -> tải lại
    raise RuntimeError(f"Không tải được ảnh gốc: {last}")


def process_image(raw, cfg, square):
    """Đưa ảnh bất kỳ về JPEG nền trắng, cạnh dài 500..image_size px (ảnh nhỏ thì phóng lên 500). Trả về (bytes, info)."""
    import io

    from PIL import Image, ImageOps

    px = int(cfg.get("image_size", 1600))
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        raise RuntimeError("File không phải ảnh hoặc định dạng không hỗ trợ")
    im = ImageOps.exif_transpose(im)
    if max(im.size) < EBAY_MIN_PX:
        scale = EBAY_MIN_PX / max(im.size)
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    if max(im.size) > px:
        im.thumbnail((px, px), Image.LANCZOS)
    if square and im.width != im.height:
        side = max(im.size)
        canvas = Image.new("RGB", (side, side), "white")
        canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
        im = canvas
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92, optimize=True)
    data = buf.getvalue()
    if len(data) > EBAY_MAX_BYTES:
        raise RuntimeError(f"Ảnh {len(data) // 1024}KB vượt giới hạn 12MB của eBay")
    return data, {"w": im.width, "h": im.height, "kb": len(data) // 1024, "fmt": "JPG"}


def check_pic_url(url):
    """Các lỗi PicURL khiến eBay từ chối khi upload file."""
    problems = []
    if not url.startswith("https://"):
        problems.append("link ảnh phải là https")
    if ";" in url or "|" in url:
        problems.append("link ảnh không được chứa dấu ; hoặc |")
    if len(url) > 500:
        problems.append("link ảnh quá dài (>500 ký tự)")
    return problems


def rehost_image(src_url, cfg, cache, proxies=None, referer=None):
    """Tải ảnh gốc -> JPEG chuẩn eBay -> up lên host. Trả về (link ảnh mới, info, đã có trong cache hay chưa)."""
    square = cfg.get("square_image", True)
    # Chế độ tự động: ảnh có thể nằm ở freeimage.host / ImgBB / R2 -> giữ khoá cũ "freeimage" để dùng lại ảnh đã up
    key = f"{src_url}|sq{cfg.get('image_size', 1600)}|{'r2' if image_host(cfg) == 'r2' else 'freeimage'}" + ("" if square else "|nosq")
    if image_host(cfg) == "r2":
        # Mỗi nơi up có link riêng; đổi bucket/link công khai thì up lại
        key += f"|{cfg.get('r2_bucket')}|{cfg.get('r2_public_url')}"
    hit = cache.get(key)
    if isinstance(hit, dict):
        return hit["url"], hit["info"], True
    raw = download_image(src_url, proxies, referer)
    data, info = process_image(raw, cfg, square=square)
    new_url = upload_to_host(data, cfg)
    keep_image_bytes(new_url, data)
    with _CACHE_LOCK:   # 2 luồng up ảnh cùng lúc: đọc lại file rồi thêm, không ghi đè mục của luồng kia
        cache[key] = {"url": new_url, "info": info}
        disk = load_json(CACHE_PATH, {})
        disk.update({k: v for k, v in cache.items() if k not in disk})
        disk[key] = cache[key]
        save_json(CACHE_PATH, disk)
    return new_url, info, False


HOST_NAMES = {"freeimage": "freeimage.host", "imgbb": "ImgBB", "r2": "Cloudflare R2"}
HOST_DOWN_SECS = 300      # nơi up vừa hỏng hẳn: 5 phút sau mới thử lại (đỡ mất ~30 giây cho mỗi ảnh)
HOST_LIMIT_SECS = 1800    # nơi up báo hết hạn mức (ImgBB "Rate limit reached"): 30 phút sau mới thử lại
_HOST_DOWN = {}           # nơi up -> thời điểm được thử lại
_HOST_WHY = {}            # nơi up -> lý do tạm ngưng (ghi log)
_HOST_USED = {}           # nơi up -> số ảnh đã up trong lần chạy này (để ghi log)
_HOST_LOCK = threading.Lock()
RATE_LIMIT_RE = re.compile(r"(?i)rate limit|too many requests|quota")


class HostRateLimited(RuntimeError):
    """Nơi up báo hết hạn mức: thử lại ngay cũng vậy, tạm ngưng lâu hơn lỗi mạng thường."""


def host_down_note():
    """Các nơi up đang tạm ngưng, vd "ImgBB tạm ngưng đến 23:58 (Rate limit reached)" — để ghi vào log up ảnh."""
    now = time.time()
    with _HOST_LOCK:
        items = [(h, t, _HOST_WHY.get(h, "")) for h, t in _HOST_DOWN.items() if t > now]
    return "; ".join(f"{HOST_NAMES[h]} tạm ngưng đến {time.strftime('%H:%M', time.localtime(t))}"
                     + (f" ({why})" if why else "") for h, t, why in items)


def image_host(cfg):
    """"r2" = chỉ dùng R2; "free" = chỉ 2 trang free (freeimage.host -> ImgBB); còn lại = tự động (free trước, R2 cuối)."""
    h = cfg.get("image_host")
    return h if h in ("r2", "free") else "auto"


def r2_ready(cfg):
    return all((cfg.get(f) or "").strip() for f in R2_FIELDS)


def upload_hosts(cfg):
    """Các nơi up ảnh theo thứ tự thử. Tự động: ImgBB (nếu đã điền key) -> freeimage.host -> R2 (nếu đã điền đủ).
    ImgBB trước: 22/10 freeimage.host chặn hàng loạt ảnh đã up (trả "403 Registration is required...") sau khi up."""
    if image_host(cfg) == "r2":
        return ["r2"]
    order = ["freeimage"]
    if (cfg.get("imgbb_api_key") or "").strip():
        order.insert(0, "imgbb")
    if r2_ready(cfg) and image_host(cfg) == "auto":
        order.append("r2")
    return order


def host_label(cfg):
    return " → ".join(HOST_NAMES[h] for h in upload_hosts(cfg))


def host_usage():
    """Ảnh đã up theo từng nơi tính tới giờ, vd {"freeimage": 3, "r2": 5}."""
    with _HOST_LOCK:
        return dict(_HOST_USED)


def verify_image_url(url):
    """eBay sẽ tải ảnh từ link này, nên link phải mở được và trả về ảnh."""
    from curl_cffi import requests

    chk = requests.get(url, timeout=60)
    if chk.status_code != 200 or not chk.headers.get("content-type", "").startswith("image/") or len(chk.content) < IMG_MIN_BYTES:
        raise RuntimeError(f"link ảnh mới không mở được (HTTP {chk.status_code})")


def upload_to_host(data, cfg, order=None):
    """Up bytes JPEG: thử lần lượt các nơi trong cài đặt, nơi đầu hỏng thì chuyển sang nơi sau. Trả về link https."""
    order = order or upload_hosts(cfg)
    now = time.time()
    with _HOST_LOCK:
        live = [h for h in order if _HOST_DOWN.get(h, 0) <= now]
    errors = []
    for h in live + [h for h in order if h not in live]:   # nơi vừa hỏng thử sau cùng (vẫn thử nếu mọi nơi đều hỏng)
        try:
            url = {"freeimage": upload_to_freeimage, "imgbb": upload_to_imgbb, "r2": upload_to_r2}[h](data, cfg)
        except Exception as e:
            errors.append(f"{HOST_NAMES[h]}: {e}")
            limited = isinstance(e, HostRateLimited)
            with _HOST_LOCK:
                _HOST_DOWN[h] = time.time() + (HOST_LIMIT_SECS if limited else HOST_DOWN_SECS)
                _HOST_WHY[h] = str(e)[:60] if limited else ""
            continue
        with _HOST_LOCK:
            _HOST_DOWN.pop(h, None)
            _HOST_WHY.pop(h, None)
            _HOST_USED[h] = _HOST_USED.get(h, 0) + 1
        return url
    raise RuntimeError(" | ".join(errors))


# ---- kiểm tra lại ảnh trước khi xuất: ảnh nào host trả lỗi thì up lại sang nơi khác

IMG_TMP = CACHE_PATH.parent / "img_tmp"   # bản ảnh đã xử lý, giữ vài ngày để up lại khi host làm hỏng ảnh (khỏi tải lại từ site)
IMG_TMP_DAYS = 3
IMG_MIN_BYTES = 1500                      # ảnh báo lỗi của host ("service unavailable") chỉ ~1 KB
PIC_CHECK_PATH = CACHE_PATH.parent / "pic_check.json"   # link ảnh đã kiểm tốt -> lúc kiểm; xuất lại trong vài ngày khỏi tải lại
PIC_CHECK_DAYS = 3


def _tmp_file(url):
    return IMG_TMP / (hashlib.sha1(url.encode()).hexdigest()[:24] + ".jpg")


def keep_image_bytes(url, data):
    try:
        IMG_TMP.mkdir(exist_ok=True)
        _tmp_file(url).write_bytes(data)
    except Exception:
        pass


def purge_image_tmp():
    cutoff = time.time() - IMG_TMP_DAYS * 86400
    try:
        for f in IMG_TMP.glob("*.jpg"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except Exception:
        pass


def load_pic_checks():
    """{link ảnh: lúc kiểm tốt} còn trong hạn PIC_CHECK_DAYS (quá hạn thì kiểm lại)."""
    cutoff = time.time() - PIC_CHECK_DAYS * 86400
    data = load_json(PIC_CHECK_PATH, {}) or {}
    return {u: t for u, t in data.items() if isinstance(t, (int, float)) and t >= cutoff}


def save_pic_checks(checks):
    with _CACHE_LOCK:
        try:
            save_json(PIC_CHECK_PATH, checks)
        except Exception:
            pass


def image_ok(url, tries=1):
    """Link ảnh mở được và trả về ảnh thật (không phải ảnh báo lỗi của host)? Mở 1 lần (từng mở 2 lần cách 2 giây để bắt
    host chập chờn, nhưng 500+ ảnh × 2 lượt mất hơn 10 phút mỗi lần xuất); lỗi 1 lần là coi như ảnh không an toàn."""
    from curl_cffi import requests

    for i in range(tries):
        try:
            r = requests.get(url, timeout=40)
            if not (r.status_code == 200 and r.headers.get("content-type", "").startswith("image/")
                    and len(r.content) >= IMG_MIN_BYTES):
                return False
        except Exception:
            return False
        if i < tries - 1:
            time.sleep(2)
    return True


def host_key_of(url, cfg):
    if "ibb.co" in url:
        return "imgbb"
    if "iili.io" in url or "freeimage" in url:
        return "freeimage"
    pub = (cfg.get("r2_public_url") or "").strip().rstrip("/")
    return "r2" if pub and url.startswith(pub) else None


def fix_bad_pics(items, cfg, workers=8, log=None, step=None):
    """Kiểm tra mọi link ảnh của các listing sắp xuất; ảnh lỗi thì up lại sang nơi khác (bỏ nơi vừa lỗi) và đổi link
    trong listing + cache ảnh. Ảnh kiểm tốt được nhớ PIC_CHECK_DAYS ngày (pic_check.json): xuất lại cùng listing khỏi
    tải lại. step: hàm nhận chữ tiến độ ("Kiểm ảnh đã up 40/182") để giao diện hiện tool đang làm gì.
    Trả về danh sách ghi chú (rỗng nếu ảnh đều tốt)."""
    from concurrent.futures import ThreadPoolExecutor

    purge_image_tmp()
    slots = {}   # link -> {"where": [(danh sách, vị trí, listing)], "src": ảnh gốc, "ref": trang sản phẩm}
    for it in items:
        if it.get("_dup"):
            continue
        pics = it.get("pics") or {}
        try:
            plan = plan_images(it["product"], cfg, it)
        except Exception:
            plan = {"main": [], "colors": {}, "general": []}
        groups = [(pics.get("main") or [], plan["main"]), (pics.get("general") or [], plan.get("general", []))]
        groups += [(lst, plan["colors"].get(c) or []) for c, lst in (pics.get("colors") or {}).items()]
        for lst, srcs in groups:
            same = len(lst) == len(srcs)
            for i, u in enumerate(lst):
                e = slots.setdefault(u, {"where": [], "src": "", "ref": ""})
                e["where"].append((lst, i, it))
                if same and not e["src"]:
                    e["src"], e["ref"] = srcs[i], (it.get("product") or {}).get("url", "")
    urls = [u for u in slots if u.startswith("http")]
    if not urls:
        return []
    checks = load_pic_checks()
    todo = [u for u in urls if u not in checks]
    if log:
        log(f"  Kiểm {len(todo)} ảnh đã up" + (f" (bỏ qua {len(urls) - len(todo)} ảnh đã kiểm tốt trong {PIC_CHECK_DAYS} ngày)"
                                              if len(todo) < len(urls) else "") + "…")
    results = []
    if step:
        step(f"Kiểm ảnh đã up 0/{len(todo)}")
    with ThreadPoolExecutor(workers) as ex:
        for u, ok in zip(todo, ex.map(image_ok, todo)):
            results.append((u, ok))
            if step and (len(results) % 5 == 0 or len(results) == len(todo)):
                step(f"Kiểm ảnh đã up {len(results)}/{len(todo)}")
    bad = [u for u, ok in results if not ok]
    if bad:
        # Host hay lỗi thoáng qua khi bị gọi dồn 8 ảnh cùng lúc (29/10: 16 ảnh ImgBB báo lỗi, kiểm lại vài phút sau vẫn tốt)
        # -> kiểm lại riêng ảnh lỗi, ít luồng, mỗi ảnh 2 lần cách 2 giây; vẫn lỗi mới tính là hỏng
        if step:
            step(f"Kiểm lại {len(bad)} ảnh vừa báo lỗi")
        time.sleep(3)
        with ThreadPoolExecutor(2) as ex:
            again = dict(zip(bad, ex.map(lambda u: image_ok(u, tries=2), bad)))
        results = [(u, ok or again.get(u, False)) for u, ok in results]
        bad = [u for u in bad if not again[u]]
    now = time.time()
    checks.update({u: now for u, ok in results if ok})
    notes, moved, failed = [], {}, []
    for k, u in enumerate(bad, 1):
        if step:   # mỗi ảnh lỗi phải tải lại từ site + up nơi khác: chậm hơn kiểm nhiều
            step(f"Up lại ảnh host trả lỗi {k}/{len(bad)}")
        e, hk = slots[u], host_key_of(u, cfg)
        data, f = None, _tmp_file(u)
        try:
            if f.exists():
                data = f.read_bytes()
            elif e["src"]:
                data, _ = process_image(download_image(e["src"], None, e["ref"]), cfg, square=cfg.get("square_image", True))
            if not data:
                raise RuntimeError("không còn bản ảnh để up lại")
            order = [h for h in upload_hosts(cfg) if h != hk] or upload_hosts(cfg)
            new = upload_to_host(data, cfg, order)
        except Exception as ex_:
            failed.append(f"{u} ({str(ex_)[:80]})")
            continue
        for lst, i, it in e["where"]:
            lst[i] = new
            if it.get("pic") == u:
                it["pic"] = new
        keep_image_bytes(new, data)
        moved[u] = new
    if moved:
        with _CACHE_LOCK:
            disk = load_json(CACHE_PATH, {})
            for v in disk.values():
                if isinstance(v, dict) and v.get("url") in moved:
                    v["url"] = moved[v["url"]]
            save_json(CACHE_PATH, disk)
        notes.append(f"Danh sách ảnh lỗi: {len(moved)} ảnh host trả lỗi, đã up lại sang nơi khác")
    if failed:
        notes.append(f"Danh sách ảnh lỗi chưa sửa được ({len(failed)}), eBay sẽ không tải được ảnh: " + "; ".join(failed[:5]))
    save_pic_checks(checks)
    return notes


# ---- Cloudflare R2 (chuẩn S3)

R2_FIELDS = ("r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket", "r2_public_url")


def r2_client(cfg):
    missing = [f for f in R2_FIELDS if not (cfg.get(f) or "").strip()]
    if missing:
        raise RuntimeError("Chưa điền đủ thông tin Cloudflare R2 trong Cài đặt")
    if not cfg["r2_public_url"].startswith("https://"):
        raise RuntimeError("Link công khai R2 phải bắt đầu bằng https://")
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{cfg['r2_account_id'].strip()}.r2.cloudflarestorage.com",
        aws_access_key_id=cfg["r2_access_key_id"].strip(),
        aws_secret_access_key=cfg["r2_secret_access_key"].strip(),
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"},
                      connect_timeout=30, read_timeout=120),
    )


def r2_key(cfg, data):
    # Tên file theo nội dung ảnh: cùng 1 ảnh luôn ra cùng 1 tên, không bị up trùng
    folder = (cfg.get("r2_folder") or "").strip().strip("/")
    name = hashlib.sha1(data).hexdigest()[:24] + ".jpg"
    return f"{folder}/{name}" if folder else name


def upload_to_r2(data, cfg):
    from botocore.exceptions import BotoCoreError, ClientError

    key = r2_key(cfg, data)
    try:
        r2_client(cfg).put_object(
            Bucket=cfg["r2_bucket"].strip(), Key=key, Body=data, ContentType="image/jpeg",
            CacheControl="public, max-age=31536000, immutable",
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        hint = {
            "NoSuchBucket": "không có bucket tên này",
            "AccessDenied": "API token không có quyền ghi vào bucket này",
            "Unauthorized": "Access Key ID / Secret Access Key sai hoặc token không có quyền",
            "InvalidAccessKeyId": "Access Key ID sai",
            "SignatureDoesNotMatch": "Secret Access Key sai",
        }.get(code, code)
        raise RuntimeError(f"Up ảnh lên R2 thất bại: {hint}")
    except BotoCoreError as e:
        if "SSL" in str(e):
            # R2 từ chối bắt tay SSL khi Account ID không tồn tại
            raise RuntimeError("Không kết nối được R2: Account ID sai")
        raise RuntimeError(f"Không kết nối được R2, kiểm tra mạng: {e}")
    url = cfg["r2_public_url"].strip().rstrip("/") + "/" + key
    try:
        verify_image_url(url)
    except Exception:
        raise RuntimeError(f"Đã up lên R2 nhưng link công khai không mở được: {url} — "
                           "kiểm tra bucket đã bật Public access và ô Link công khai đúng chưa")
    return url


def upload_to_freeimage(data, cfg):
    from curl_cffi import requests

    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(
                "https://freeimage.host/api/1/upload",
                data={"key": cfg["freeimage_api_key"], "action": "upload", "format": "json",
                      "source": base64.b64encode(data).decode()},
                timeout=120,
            )
            j = r.json()
            new_url = (j.get("image") or {}).get("url")
            if new_url:
                verify_image_url(new_url)
                return new_url
            last_err = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else (j.get("error") or j)
            if RATE_LIMIT_RE.search(str(last_err)):
                raise HostRateLimited(str(last_err)[:80])   # hết hạn mức: thử lại ngay cũng vậy, không chờ 3/6/9 giây
        except HostRateLimited:
            raise
        except Exception as e:  # mạng chập chờn -> thử lại
            last_err = e
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Up ảnh thất bại: {last_err}")


def upload_to_imgbb(data, cfg):
    from curl_cffi import requests

    key = (cfg.get("imgbb_api_key") or "").strip()
    if not key:
        raise RuntimeError("Chưa điền key ImgBB trong Cài đặt")
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post("https://api.imgbb.com/1/upload", data={"key": key, "image": base64.b64encode(data).decode()},
                              timeout=120)
            j = r.json()
            new_url = (j.get("data") or {}).get("url")
            if new_url:
                verify_image_url(new_url)
                return new_url
            last_err = (j.get("error") or {}).get("message") or j
            if RATE_LIMIT_RE.search(str(last_err)):
                # "Rate limit reached": thử lại ngay cũng vậy — trước đây thử 3 lần + chờ 3/6/9 giây rồi 5 phút sau lại thế,
                # mỗi lần làm ảnh đầu của link chậm 30–40 giây
                raise HostRateLimited(str(last_err)[:80])
            if r.status_code in (400, 403) and "key" in str(last_err).lower():
                break   # key sai: thử lại cũng vậy
        except HostRateLimited:
            raise
        except Exception as e:  # mạng chập chờn -> thử lại
            last_err = e
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Up ảnh thất bại: {last_err}")


def test_image_host(cfg):
    """Nút "Kiểm tra kết nối": up thử 1 ảnh nhỏ lên TỪNG nơi trong danh sách, kiểm tra link công khai (R2 thì xoá luôn).
    Trả về [(tên nơi, link hoặc None, lỗi)]."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (240, 240, 240)).save(buf, "JPEG", quality=80)
    out = []
    for h in upload_hosts(cfg):
        data = buf.getvalue() + str(time.time()).encode()  # nội dung khác nhau mỗi lần để không trùng tên
        try:
            url = {"freeimage": upload_to_freeimage, "imgbb": upload_to_imgbb, "r2": upload_to_r2}[h](data, cfg)
        except Exception as e:
            out.append((HOST_NAMES[h], None, str(e)))
            continue
        if h == "r2":
            try:
                r2_client(cfg).delete_object(Bucket=cfg["r2_bucket"].strip(), Key=r2_key(cfg, data))
            except Exception:
                pass  # không xoá được thì chỉ còn 1 file thử nhỏ, không ảnh hưởng
        out.append((HOST_NAMES[h], url, ""))
    return out


# ---- ảnh thu nhỏ cho bảng Kết quả (ảnh eBay 1600px nặng ~700KB, hiện 64px thì chỉ cần vài KB)

THUMB_DIR = ROOT / "thumb_cache"


def thumbnail(url, cfg, size=160):
    """Bytes JPEG thu nhỏ của ảnh (lưu đĩa, lần sau lấy lại). Tải thẳng; lỗi thì thử qua proxy."""
    import io

    from PIL import Image

    path = THUMB_DIR / (hashlib.sha1(f"{url}|{size}".encode()).hexdigest() + ".jpg")
    if path.exists():
        return path.read_bytes()
    raw = None
    try:
        raw = download_image(url)
    except Exception:
        for p in proxies_for(cfg)[:2]:   # ảnh gốc của site chặn IP Việt Nam (vd Touch of Class)
            try:
                from sites.base import proxy_url
                u = proxy_url(p)
                raw = download_image(url, {"http": u, "https": u})
                break
            except Exception:
                continue
    if raw is None:
        raise RuntimeError("không tải được ảnh")
    im = Image.open(io.BytesIO(raw))
    im = im.convert("RGB")
    im.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    THUMB_DIR.mkdir(exist_ok=True)
    path.write_bytes(buf.getvalue())
    return buf.getvalue()


# ---------------------------------------------------------------- chọn ảnh, giá, category

def plan_images(prod, cfg, item=None, only_colors=None):
    """Ảnh cần up cho 1 sản phẩm: {"main", "colors": {màu: [src...]}, "general": [src...]}.

    Ảnh biến thể: mỗi màu ĐANG CHỌN tối đa max_images_per_color ảnh. only_colors: chỉ lấy các màu này
    (dùng khi người dùng tích thêm màu mới, cần up bổ sung)."""
    per_color = max(1, int(cfg.get("max_images_per_color", 4)))
    limit = max(1, min(24, int(cfg.get("max_images", 12))))
    want = set(only_colors) if only_colors is not None else selling_colors(prod, cfg, item)
    colors = {}
    for c in prod.get("colors", []):
        if c.get("images") and c["name"] in want:
            colors[c["name"]] = c["images"][:per_color]
    all_color_imgs = {u for c in prod.get("colors", []) for u in c.get("images", [])}
    general = [u for u in dict.fromkeys(prod.get("images", [])) if u not in all_color_imgs][:limit]
    return {"main": compose_main(colors, general, limit), "colors": colors, "general": general}


def compose_main(colors, general, limit):
    """Ảnh listing chính: toàn bộ ảnh màu đầu tiên, rồi ảnh đầu của các màu khác, rồi ảnh chung."""
    main = list(general)
    if colors:
        first = next(iter(colors.values()))
        main = first + [imgs[0] for imgs in colors.values() if imgs] + main
    return list(dict.fromkeys(u for u in main if u))[:limit]


def listing_pics(item, cfg):
    """Ảnh khi xuất: ảnh chính dựng lại theo các màu đang chọn (bỏ màu đã bỏ chọn)."""
    pics = item.get("pics") or {}
    limit = max(1, min(24, int(cfg.get("max_images", 12))))
    chosen = [c for c in dict.fromkeys(v["color"] for v in selling_variants(item["product"], cfg, item))]
    colors = {c: pics.get("colors", {}).get(c) for c in chosen if pics.get("colors", {}).get(c)}
    if not colors and not pics.get("general"):
        return pics.get("main") or [], pics.get("colors") or {}
    return compose_main(colors, pics.get("general") or [], limit) or (pics.get("main") or []), colors


# ---- nhóm size (Misses / Petite / Plus / Tall...) tách từ nhãn size, vd "1X Plus Petite" -> ("1X", "Plus Petite")

SIZE_GROUPS = ["Plus Petite", "Woman Petite", "Big & Tall", "Petite", "Plus", "Woman", "Tall", "Long", "Short"]


def base_group_name(site):
    return "Misses" if site == "talbots" else "Regular"


def split_size(label, site=""):
    """(size gốc, nhóm size). Không có chữ nhóm -> nhóm mặc định (Misses với Talbots, Regular với site khác)."""
    s = (label or "").strip()
    for g in SIZE_GROUPS:
        if s.lower().endswith(" " + g.lower()):
            return s[: -len(g)].strip(), g
    if site == "eileenfisher":   # Eileen Fisher ghi nhóm ngay trong nhãn: PP/PS/PM/PL = Petite, 1X/2X/3X = Plus
        if re.fullmatch(r"P(?:P|S|M|L|XL)", s):
            return s, "Petite"
        if re.fullmatch(r"\dX", s):
            return s, "Plus"
    return s, base_group_name(site)


def size_groups(prod):
    """Các nhóm size của sản phẩm theo thứ tự trên site (chỉ 1 nhóm thì coi như không có nhóm)."""
    groups = list(dict.fromkeys(split_size(v["size"], prod["site"])[1] for v in prod["variants"] if v["size"]))
    return groups if len(groups) > 1 else []


# Mã nhóm size trong link (vd Talbots dwvar_..._sizeType=MS)
SIZE_TYPE_CODES = {"ms": "Misses", "pt": "Petite", "wo": "Plus", "wp": "Plus Petite", "rg": "Regular",
                   "tl": "Tall", "tall": "Tall", "petite": "Petite", "plus": "Plus", "regular": "Regular",
                   "misses": "Misses"}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


COLOR_ABBR = {"hthr": "heather", "htr": "heather", "mlt": "multi", "mult": "multi", "dk": "dark", "lt": "light",
               "nvy": "navy", "blk": "black", "wht": "white", "gry": "grey", "brn": "brown", "gol": "gold",
               "lav": "lavender", "burg": "burgundy", "chrcl": "charcoal", "ivry": "ivory", "pnk": "pink"}


def nice_color(name):
    """Tên màu cho tiêu đề: viết hoa đầu từ, mở chữ tắt (vd "CAPPUCCINO HTHR/AMBER GOL" -> "Cappuccino Heather/Amber Gold")."""
    def fix(m):
        w = m.group(0)
        full = COLOR_ABBR.get(w.lower())
        return (full or w.lower()).capitalize() if (full or w.isupper()) else w
    return re.sub(r"[A-Za-z]+", fix, name or "")


def match_color(val, colors):
    """Tên màu trong link (có thể viết tắt / bị cắt, vd "WHITE/INDIGO", "CAPPUCCINO HTHR/AMBER GOL")
    -> tên màu đầy đủ của sản phẩm."""
    import difflib

    if not val or not colors:
        return None
    exact = next((c for c in colors if _norm(c) == _norm(val)), None)
    if exact:
        return exact

    def words(s):
        return [COLOR_ABBR.get(w, w) for w in re.findall(r"[a-z0-9]+", s.lower())]

    vw = words(val)
    best, score = None, 0.0
    for c in colors:
        cw = words(c)
        # mỗi chữ trong link là đầu của 1 chữ trong tên màu (theo thứ tự): "indigo" ~ "indigo blue", "gol" ~ "gold"
        j, ok = 0, True
        for w in vw:
            while j < len(cw) and not cw[j].startswith(w):
                j += 1
            if j == len(cw):
                ok = False
                break
            j += 1
        s = 1.0 + len(vw) / max(len(cw), 1) if ok else difflib.SequenceMatcher(None, " ".join(vw), " ".join(cw)).ratio()
        if s > score:
            best, score = c, s
    return best if score >= 0.75 else None


def url_filters(url, prod):
    """Màu / nhóm size đã chọn sẵn trong link gốc (vd ...color=BLACK&...sizeType=MS) -> {"colors", "groups"}.
    Không đọc được gì thì trả None (mặc định chọn hết)."""
    from urllib.parse import parse_qsl, urlparse

    params = parse_qsl(urlparse(url).query)
    colors = [c for c in dict.fromkeys(v["color"] for v in prod["variants"]) if c]
    groups = size_groups(prod)
    out = {}
    # Site có màu nằm trong đường dẫn (vd Tommy Bahama /p/ST228252-18406) -> bộ cào báo sẵn màu đó
    if prod.get("url_color") in colors:
        out["colors"] = [prod["url_color"]]
    # Site ghi sẵn 1 kích thước trong link (vd Touch of Class ...-16-x-72/p/...) -> chỉ chọn size đó
    if prod.get("url_size") and any(v["size"] == prod["url_size"] for v in prod["variants"]):
        out["sizes"] = [prod["url_size"]]
    # Site ghi nhóm size trong link bằng mã riêng (vd Banana Republic vid=2 = Tall) -> bộ cào báo sẵn tên nhóm
    if prod.get("url_group"):
        hit = next((x for x in groups if x.lower() == prod["url_group"].lower()), None)
        if hit:
            out["groups"] = [hit]
    for k, val in params:
        kl = k.lower()
        if kl.endswith("color") or kl.endswith("colour") or kl == "color":
            hit = match_color(val, colors)
            if hit:
                out["colors"] = [hit]
        elif kl.endswith("sizetype") or kl.endswith("size_type") or kl.endswith("fit"):
            g = SIZE_TYPE_CODES.get(val.strip().lower()) or val.strip()
            hit = next((x for x in groups if x.lower() == g.lower()), None)
            if hit:
                out["groups"] = [hit]
    # Link không ghi nhóm size -> chọn nhóm site mở mặc định (Regular / Misses...), giống khi mở link trên web.
    # Các nhóm khác (Tall, Petite...) vẫn hiện trong cửa sổ Xem để tích thêm.
    if groups and "groups" not in out:
        want_c = set(out.get("colors") or colors)
        has_stock = {split_size(v["size"], prod["site"])[1] for v in prod["variants"]
                     if v.get("available") and (not colors or v["color"] in want_c)}
        pref = [x for x in groups if x.lower() in DEFAULT_GROUPS] + groups
        hit = next((x for x in pref if x in has_stock), None)
        if hit:
            out["groups"] = [hit] + [g for g in EXTRA_DEFAULT_GROUPS.get(prod["site"], ()) if g in groups and g in has_stock]
    return out or None


# Nhóm size site mở sẵn khi link không ghi nhóm nào
DEFAULT_GROUPS = ("regular", "misses", "standard", "women", "womens", "women's")
# Site bán thêm nhóm khác cùng nhóm mặc định trong 1 listing (Eileen Fisher: size thường + Plus 1X/2X/3X, bỏ Petite)
EXTRA_DEFAULT_GROUPS = {"eileenfisher": ("Plus",)}


# ---- size chuẩn eBay: XXS -> 2XS, XXL -> 2XL, size Petite S -> PS, 8 Petite -> 8P, 14 Plus -> 14W ...
ALPHA_STD = {"XXXS": "3XS", "XXS": "2XS", "XXL": "2XL", "XXXL": "3XL", "XXXXL": "4XL", "XXXXXL": "5XL"}
PETITE_ALPHA = {"P": "PP", "PP": "PP", "2XS": "P2XS", "XS": "PXS", "S": "PS", "M": "PM", "L": "PL", "XL": "PXL",
                "2XL": "P2XL"}


def ebay_size(label, site, allowed=None):
    """Nhãn size của site -> giá trị Size chuẩn eBay (chỉ đổi khi giá trị mới nằm trong danh sách eBay cho phép).
    allowed: danh sách Size của Category (None = không biết -> giữ nguyên)."""
    if not allowed:
        return None
    low = {a.lower(): a for a in allowed}
    base, group = split_size(label, site)
    b = ALPHA_STD.get(base.upper(), base.upper() if re.fullmatch(r"[A-Za-z0-9]+", base) else base)
    g = group.lower()
    cands = []
    if g in ("petite", "plus petite", "woman petite"):
        cands += [PETITE_ALPHA.get(b, ""), f"{b}P" if b.isdigit() else ""]
    if g in ("plus", "woman", "plus petite", "woman petite"):
        cands += [f"{b}W" if b.isdigit() else "", "0X" if b == "X" else b]
    if g == "tall":
        cands += [f"{b} Tall", f"{b}T"]   # đồ nữ "M Tall", đồ nam "MT" / "XLT" / "2XLT"
    cands += [b, label]
    if g in ("", "regular") and re.fullmatch(r"\d{2,3}W", b):
        cands.append(b[:-1])   # quần short nam "32W" -> "32" (đồ nữ size Plus "14W" có trong danh sách thì đã khớp ở trên)
    for c in cands:
        if c and c.lower() in low:
            return low[c.lower()]
    return None


GROUP_SIZE_TYPE = {"petite": "Petite", "plus": "Plus", "plus petite": "Plus", "woman": "Plus",
                   "woman petite": "Plus", "tall": "Tall", "big & tall": "Big & Tall"}


def vkey(v):
    """Khoá 1 biến thể (màu + size) để lưu lựa chọn của người dùng."""
    return f"{v.get('color', '')}||{v.get('size', '')}"


def default_selection(prod, cfg):
    """Biến thể được tích sẵn: còn hàng (hoặc tất cả nếu tắt "Bỏ size/màu hết hàng")."""
    return [vkey(v) for v in prod["variants"] if v.get("available") or not cfg.get("skip_out_of_stock", True)]


def selling_variants(prod, cfg, item=None):
    """Biến thể đưa vào listing: đúng những biến thể người dùng đã tích ở cửa sổ Xem
    (chưa chọn lần nào thì theo mặc định). Biến thể hết hàng được chọn -> số lượng 0. Tối đa 250 (giới hạn eBay)."""
    sel = (item or {}).get("variant_sel")
    keys = set(sel) if sel is not None else set(default_selection(prod, cfg))
    vs = [v for v in prod["variants"] if vkey(v) in keys]
    if len(vs) <= 250:
        return vs
    # Quá 250: giữ trọn từng màu (đủ size) theo thứ tự trên site, màu nào làm vượt 250 thì bỏ.
    # Ưu tiên màu còn hàng trước, màu hết sạch chỉ thêm khi còn chỗ.
    by_color = {}
    for v in vs:
        by_color.setdefault(v["color"], []).append(v)
    groups = list(by_color.values())
    groups = [g for g in groups if any(v.get("available") for v in g)] + \
             [g for g in groups if not any(v.get("available") for v in g)]
    keep = set()
    total = 0
    for g in groups:
        if total + len(g) > 250:
            continue
        keep.add(id(g))
        total += len(g)
    if not keep:   # 1 màu đã quá 250 size
        return vs[:250]
    return [v for g in by_color.values() if id(g) in keep for v in g]


def selling_colors(prod, cfg, item=None):
    return {v["color"] for v in selling_variants(prod, cfg, item)}


# Site hay sale chớp nhoáng (1–2 ngày rồi về giá cũ): tính giá eBay theo GIÁ GỐC, không theo giá sale (đỡ lỗ khi hết sale)
LIST_PRICE_SITES = ("hollister",)   # Eileen Fisher: 23/10 chuyển về giá sale


def price_basis(site):
    return "list" if site in LIST_PRICE_SITES else "sale"


def base_price(v, site):
    """Giá site dùng để tính giá eBay: site tính theo giá gốc -> giá gốc (nếu site có ghi), không thì giá đang bán."""
    if price_basis(site) == "list" and v.get("list_price"):
        return v["list_price"]
    return v["price"]


def ebay_price(src_price, percent, round_99=True):
    import math

    p = round(float(src_price) * float(percent) / 100.0, 2)   # làm tròn trước (9.99 - 9 = 0.99000000000002)
    if round_99:
        p = math.floor(p) + 0.99 if round(p - math.floor(p), 2) <= 0.99 else math.ceil(p) + 0.99
    return round(p, 2)


def pick_rule(prod, cfg):
    """Quy tắc Category đầu tiên khớp từ khoá (tìm trong tên + breadcrumb) và Department."""
    hay = " " + " ".join([prod.get("title", "")] + list(prod.get("breadcrumbs", []))).lower() + " "
    dept = (prod.get("department") or "").lower()
    site = prod.get("site") or ""
    rules = cfg.get("category_rules", [])
    # quy tắc riêng của site này trước, rồi quy tắc chung (site trống); quy tắc của site khác bỏ qua
    rules = [r for r in rules if (r.get("site") or "") == site] + [r for r in rules if not r.get("site")]
    for r in rules:
        if not (r.get("category") or "").strip():
            continue   # quy tắc chưa điền Category: bỏ qua (không chặn các quy tắc sau)
        rd = (r.get("dept") or "").strip().lower()
        if rd and rd != dept:
            continue
        words = [w.strip().lower() for w in (r.get("keywords") or "").split(",") if w.strip()]
        # khớp nguyên từ (cho phép số nhiều): "dress" khớp "Dresses" nhưng không khớp "Dresser"; "top" không khớp "Topper"
        if not words or any(re.search(rf"\b{re.escape(w)}(?:s|es)?\b", hay) for w in words):
            return r
    return None


# Nhóm hàng ghép nhiều loại ("Toys and Books", "Bundles & Sets", "Pants, Shorts + Jumpsuits"): không cho biết món
# thuộc loại nào -> không dùng để hỏi eBay Category / đoán item specifics
COMBINED_CRUMB = re.compile(r"(?i)\s(?:and|&)\s|&|/|,|\+")
TOY_BRANCHES = ("Toys & Hobbies", "Baby › Toys for Baby")


def single_crumbs(prod):
    return [c for c in prod.get("breadcrumbs", []) if c and not COMBINED_CRUMB.search(c)]


def is_toy(prod):
    """Sản phẩm nằm trong nhóm đồ chơi của site (vd Janie and Jack "Toys and Books")."""
    return any(re.search(r"(?i)\btoys?\b", c or "") for c in prod.get("breadcrumbs", []))


def best_suggestion(opts, dept, title_add="", title="", toy=False):
    if toy and opts:   # đồ chơi: ưu tiên gợi ý thuộc nhánh đồ chơi, không kéo theo Department sang mục quần áo
        return next((o for o in opts if (o.get("path") or "").startswith(TOY_BRANCHES)), opts[0])
    return _best_suggestion(opts, dept, title_add, title)


def _best_suggestion(opts, dept, title_add="", title=""):
    """Gợi ý Category khớp Department nhất (vd áo blazer nữ mà eBay đưa Men's Suits lên đầu -> chọn mục Women).
    title_add: loại hàng tool ghép vào tiêu đề (vd "Speaker Bundle") -> ưu tiên gợi ý có mục hợp với loại hàng
    (eBay hay hiểu "Adventure Set with Play" là đồ chơi)."""
    if not opts:
        return None
    if title_add:
        fit = [o for o in opts if title_add_fits(title_add, o.get("path") or "")]
        if fit:
            # mục khớp từ khoá quan trọng hơn đứng trước (bộ có soundbar: "Home Theater Systems" trước "Speaker Mounts")
            opts = sorted(fit, key=lambda o: title_add_rank(title_add, o.get("path") or ""))
    elif title and not dept:   # đồ không phải quần áo, tool không ghép loại hàng (vd phụ kiện): so theo tên món
        opts = match_by_name(opts, title)
    d = (dept or "").lower()
    want = {"women": "women", "men": "men", "girls": "girl", "boys": "boy",
            "baby girls": "baby", "baby boys": "baby"}.get(d)
    if want:
        for o in opts:
            path = (o.get("path") or "").lower()
            if want == "men" and ("men's" in path or " men " in f" {path} ") and "women" not in path:
                return o
            if want != "men" and want in path:
                return o
    return opts[0]


def category_for(prod, cfg, override="", api=None):
    """(category id, item specifics thêm, mô tả nguồn) cho sản phẩm.

    Thứ tự: gõ tay trong bảng Kết quả -> quy tắc từ khoá -> eBay gợi ý (api). Không có thì để trống (gõ tay)."""
    r = pick_rule(prod, cfg)
    if override:
        same = r and r.get("category") == override   # specifics của quy tắc chỉ hợp với đúng Category đó
        return override, parse_specifics(r.get("specifics", "")) if same else {}, "gõ tay"
    if r and r.get("category"):
        return r["category"], parse_specifics(r.get("specifics", "")), f"quy tắc '{r['keywords']}'"
    if api and api.get("id"):
        return api["id"], {}, f"eBay gợi ý: {api.get('path') or api.get('name')}"
    return "", {}, ""


# ---------------------------------------------------------------- item specifics theo Category (eBay Taxonomy API)

# Tên thông số trên site -> tên item specific eBay
DETAIL_ALIASES = {
    "material": ("material", "fabric", "fabric content", "composition", "materials"),
    "outer shell material": ("outer shell material", "shell", "material", "fabric", "fabric content", "composition"),
    "lining material": ("lining", "lining material"),
    "upper material": ("upper material", "upper", "material"),
    "fabric type": ("fabric type", "construction"),
    "country/region of manufacture": ("origin", "country of origin", "made in"),
    "country of origin": ("origin", "country of origin", "made in"),
    "care instructions": ("care", "care instructions"),
    "fit": ("fit",),
    "pattern": ("pattern",),
    "closure": ("closure",),
    "neckline": ("neckline",),
    "sleeve length": ("sleeve length", "sleeve"),
    "dress length": ("length", "dress length"),
    "finish": ("finish",),
}
BASIC_COLORS = ["black", "white", "grey", "gray", "navy", "blue", "red", "green", "pink", "purple", "yellow",
                "orange", "brown", "beige", "ivory", "cream", "gold", "silver", "multicolor", "tan", "khaki"]


def _match_value(text, values):
    """Giá trị eBay cho phép xuất hiện nguyên từ trong text: lấy cái xuất hiện sớm nhất
    (vd "59% Cotton/39% Polyester" -> Cotton), cùng chỗ thì lấy cái dài hơn."""
    t = " " + text.lower() + " "
    best, best_key = "", None
    for v in values:
        lv = v.lower().strip()
        if len(lv) < 3:
            continue
        m = re.search(rf"(?<![a-z]){re.escape(lv)}(?![a-z])", t)
        if m and (best_key is None or (m.start(), -len(lv)) < best_key):
            best, best_key = v, (m.start(), -len(lv))
    return best



def axis_names(aspects):
    """Tên 2 trục biến thể. Category nào không cho biến thể theo Color/Size thì trả None cho trục đó."""
    if not aspects:
        return "Color", "Size"
    var = {a["name"].lower(): a["name"] for a in aspects if a.get("variation")}

    def pick(word):
        # Ưu tiên đúng tên ("Size"), rồi tên có chứa từ đó, bắt buộc trước (vd giày: "US Shoe Size")
        if word in var:
            return var[word]
        cands = [a for a in aspects if a.get("variation") and word in a["name"].lower()]
        cands.sort(key=lambda a: (not a.get("required"), len(a["name"])))
        return cands[0]["name"] if cands else None

    return pick("color"), pick("size")


# Nước hoa / cologne: "cỡ" của site là dung tích ("1.7 FL OZ") -> eBay có trục Volume ("1.7 fl oz"), không phải Size Type
VOLUME_RE = re.compile(r"(?i)^\s*(\d+(?:\.\d+)?)\s*(fl\.?\s*oz|oz|ml)\.?\s*$")


def norm_volume(label):
    """'1.7 FL OZ' -> '1.7 fl oz', '50 ML' -> '50 ml'; không phải dung tích thì None."""
    m = VOLUME_RE.match(label or "")
    if not m:
        return None
    unit = "ml" if m.group(2).lower() == "ml" else "fl oz"
    return f"{float(m.group(1)):g} {unit}"


def volume_axis(prod, aspects):
    """Tên trục Volume khi mọi cỡ của sản phẩm là dung tích và Category có Volume làm biến thể (nước hoa); không thì None."""
    ax = next((a["name"] for a in aspects or [] if a["name"].lower() == "volume" and a.get("variation")), None)
    labels = [v["size"] for v in prod["variants"] if v["size"]]
    return ax if ax and labels and all(norm_volume(x) for x in labels) else None


# Size quần nam kiểu "32W x 30L": eBay bắt Size = vòng eo ("32"), chiều dài để ở trục riêng Inseam ("30 in")
# bắt buộc có chữ W và L (không nhầm kích thước đồ nhà "16 x 72" của Touch of Class)
WAIST_INSEAM = re.compile(r"(\d{2,3})\s*W\s*[xX×]\s*(\d{2})\s*L(?:\s+(?:Tall|Regular))?", re.I)


def waist_inseam(label):
    m = WAIST_INSEAM.fullmatch((label or "").strip())
    return (m.group(1), m.group(2)) if m else None


# Quần "W x L": chỉ bán 1 chiều dài (đỡ tốn limit eBay) — cố định 32L; 32L hết sạch size mới xét 30L, rồi 34L
LENGTH_ORDER = ("32", "30", "34")


def pick_length(prod, keys):
    """Giữ lại các biến thể (vkey) của đúng 1 Length: cố định 32L, chỉ khi 32L hết sạch size mới lấy 30L, rồi 34L
    (LENGTH_ORDER).
    Không phải quần W x L, hoặc không còn Length nào trong danh sách (vd Tall chỉ có 36L) -> giữ nguyên."""
    keys = list(keys)
    want = set(keys)
    wl = {vkey(v): waist_inseam(v["size"]) for v in prod["variants"] if vkey(v) in want}
    if not wl or not all(wl.values()):
        return keys
    stock = collections.Counter(wl[vkey(v)][1] for v in prod["variants"] if vkey(v) in want and v.get("available"))
    have = [x for x in LENGTH_ORDER if stock[x]]
    if not have:
        return keys
    pick = have[0]
    return [k for k in keys if wl.get(k) and wl[k][1] == pick]


# Quần nữ size số (000, 00, 0, 2…20): mặc định chỉ bán size 4–18 (size nhỏ / lớn hơn vẫn tích thêm được ở cửa sổ Xem).
# Chỉ áp cho site có tên ở đây; size theo vòng eo (23, 27…) hoặc "32W x 30L" không bị đụng tới.
SIZE_RANGE = {"hollister": (4, 18)}
PANTS_RE = re.compile(r"(?i)\b(jeans?|pants?|trousers?|shorts?|joggers?|sweatpants?|culottes?|chinos?|cargos?)\b")


def pick_size_range(prod, keys):
    """Quần của site có SIZE_RANGE, mọi size đang chọn đều là số nhỏ (≤ 20): chỉ giữ size trong khoảng đó.
    Không phải quần / size không phải số / không còn size nào trong khoảng -> giữ nguyên."""
    keys = list(keys)
    rng = SIZE_RANGE.get(prod.get("site"))
    if not rng or not keys:
        return keys
    text = " ".join([prod.get("title", ""), *(prod.get("breadcrumbs") or [])])
    if not PANTS_RE.search(text):
        return keys
    want = set(keys)
    num = {}
    for v in prod["variants"]:
        if vkey(v) in want:
            s = (v["size"] or "").strip()
            if not s.isdigit() or int(s) > 20:
                return keys
            num[vkey(v)] = int(s)
    kept = [k for k in keys if rng[0] <= num.get(k, -1) <= rng[1]]
    return kept or keys


def inseam_axis(prod, aspects):
    """Tên trục Inseam khi phải tách size "32W x 30L" thành Size + Inseam: Category có Inseam làm biến thể,
    mọi size của sản phẩm đều dạng W x L và mọi vòng eo có trong danh sách Size của Category; không thì None."""
    ax = next((a["name"] for a in aspects or [] if a["name"].lower() == "inseam" and a.get("variation")), None)
    labels = [v["size"] for v in prod["variants"] if v["size"]]
    if not (ax and labels and all(waist_inseam(x) for x in labels)):
        return None
    size_vals = next((a.get("values") for a in aspects if a["name"].lower() == "size" and a.get("values")), None)
    if size_vals and not all(waist_inseam(x)[0] in size_vals for x in labels):
        return None   # vd Category Suits chỉ có size eo chẵn -> giữ nguyên "31W x 30L" như trước
    return ax


MATERIAL_VERB = re.compile(r"(?i)\b(?:is|are|made (?:of|from|with|in)|crafted (?:of|from|in|with)|"
                           r"constructed (?:of|from)|fashioned (?:of|from)|cast in|features?)\s+(.*)")
# Số + đơn vị inch (" / in / inch) + trục: W D H L hoặc dia/diam (chữ hoa hay thường; ngay sau trục được là "x")
_DIM = (r"(\d+(?:\.\d+)?)\s*(?:\"|''|”|″|in\.?|inch(?:es)?)?\s*"
        r"((?i:diam(?:eter)?\.?|dia\.?)|[WDHLwdhl](?![A-WYZa-wyz]))")
DIM_RE = re.compile(_DIM)
DIM_CHAIN = re.compile(rf"{_DIM}(?:\s*[x×X]\s*{_DIM})+")   # 17"Wx21.5"Dx35.5"H, 8"dia.x15"H
DIM_INCH = re.compile(r"\"|”|″|\bin\b\.?|inch")                # chuỗi kích thước phải ghi rõ inch
DIM_METRIC_AFTER = re.compile(r"(?i)^\s*\(?\s*(in\s+)?(cm|mm|centimet|millimet)")
NO_TITLE_MATCH = re.compile(r"country|region|character|franchise|brand|model|team|league|artist|author|"
                            r"signed|vintage|handmade|personaliz|california|unit|mpn|upc|ean")
MAIN_MATERIAL = {"material", "outer shell material", "main material", "frame material", "tabletop material",
                 "base material", "fabric material"}
COMMON_MATERIALS = ["polyresin", "resin", "metal", "iron", "wrought iron", "cast iron", "steel", "stainless steel",
                    "aluminum", "brass", "copper", "zinc", "tin", "wood", "solid wood", "bamboo", "rattan", "wicker",
                    "glass", "ceramic", "stoneware", "porcelain", "earthenware", "terracotta", "melamine", "acrylic",
                    "plastic", "marble", "stone", "slate", "concrete", "cotton", "linen", "polyester", "wool", "jute",
                    "velvet", "leather", "faux leather", "silk", "canvas", "paper", "mdf"]
DIM_ASPECT = {"W": "item width", "D": "item depth", "H": "item height", "L": "item length"}


def _material_text(prod):
    """Phần câu nói chất liệu: '... Holder is resin with a wooden dowel rod.' -> 'resin with a wooden dowel rod.'
    (bỏ phần tên sản phẩm đứng trước để 'Paper Towel Holder' không bị hiểu là chất liệu Paper)."""
    out = []
    for line in prod.get("bullets", []):
        for sent in re.split(r"(?<=[.!?])\s+", line):
            m = MATERIAL_VERB.search(sent)
            if m:
                rest = m.group(1)
                # "is/are" chỉ tính vài từ ngay sau ("is resin with...") — "is perfect on a wood table" không phải chất liệu
                if re.match(r"(?i)is|are", m.group(0)):
                    rest = " ".join(rest.split()[:3])
                out.append(rest)
    return " | ".join(out)


def dimensions(prod):
    """Kích thước ghi trong mô tả dạng 16.25"W x 7.5"D x 10.5"H -> {"item width": "16.25 in", ...}.
    Lấy chuỗi kích thước đầy đủ nhất (vd "Each folds to 8"D... Overall 17"Wx21.5"Dx35.5"H" -> bộ Overall)."""
    texts = [str(v) for k, v in (prod.get("details") or {}).items() if re.search(r"(?i)dimension|measure|size", k)]
    texts += list(prod.get("bullets", []))
    best = []
    for t in texts:
        t = t.replace("&quot;", '"')
        for m in DIM_CHAIN.finditer(t):
            # không ghi inch (vd "40 W x 30 D x 20 H cm") -> có thể là cm, không đoán
            if not DIM_INCH.search(m.group(0)) or DIM_METRIC_AFTER.search(t[m.end():]):
                continue
            hits = DIM_RE.findall(m.group(0))
            if len(hits) > len(best):
                best = hits
    if not best:   # chỉ ghi đường kính: 'Each plate measures 9" dia.'
        for t in texts:
            m = re.search(r"(\d+(?:\.\d+)?)\s*(?:\"|”|″|in\.?|inch(?:es)?)\s*(?i:diam(?:eter)?|dia)\b", t.replace("&quot;", '"'))
            if m:
                best = [(m.group(1), "dia")]
                break
    out = {}
    for num, axis in best:
        val = f"{float(num):g} in"
        if axis.lower().startswith("dia"):
            out.setdefault("item diameter", val)
            out.setdefault("item width", val)
        else:
            out.setdefault(DIM_ASPECT[axis.upper()], val)
    if not out:
        # Dạng ghi từng dòng: "Height: 7.19 in (182.5 mm); Width: 4.72 in; Depth: 5.14 in" (vd Sonos)
        for t in texts:
            for name, num in re.findall(r"(?i)\b(height|width|depth|length|diameter)\s*:\s*(\d+(?:\.\d+)?)\s*(?:in\b|inch|\")", t):
                out.setdefault(f"item {name.lower()}", f"{float(num):g} in")
    return out


def fill_aspects(prod, aspects, axes, have):
    """Tự điền item specifics theo danh sách aspects của Category.

    axes: tên trục biến thể đang dùng (không điền lại); have: specifics đã có (Brand, Department, quy tắc...).
    Trả về (dict tên -> giá trị, list tên specific BẮT BUỘC còn thiếu)."""
    # Nhóm hàng ghép ("Toys and Books") không dùng: chữ "Books" trong đó làm ô Type thành Books cho cả cầu trượt
    crumbs_txt = " ".join(single_crumbs(prod))
    text = " ".join([prod.get("title", ""), crumbs_txt])
    details = {strip_links(k).lower().strip(): strip_links(v) for k, v in (prod.get("details") or {}).items()
               if strip_links(v).strip()}
    # Dòng mô tả dạng "Tên: giá trị" (vd "Outer Shell Material: 100% Cotton") cũng tính là thông số của site
    for line in prod.get("bullets", []):
        m = re.match(r"\s*([A-Za-z][A-Za-z /&'-]{1,40}?)\s*:\s*(.{1,120})$", line)
        if m:
            details.setdefault(m.group(1).lower().strip(), m.group(2).strip())
    out, missing = {}, []
    have_l = {k.lower() for k in have}
    dims = dimensions(prod)
    if dims and not any(a["name"].lower() == "item depth" for a in aspects):
        dims.setdefault("item length", dims.get("item depth", ""))   # Category không có Depth -> D là chiều dài
    mat_text = None
    for a in aspects:
        name, low = a["name"], a["name"].lower()
        if low in have_l or name in axes:
            continue
        vals, sel = a.get("values") or [], a.get("mode") == "SELECTION_ONLY"
        val = ""
        # 0. kích thước đọc từ mô tả (Item Width / Height / Depth / Length)
        if dims.get(low) and not sel:
            out[name] = dims[low]
            continue
        # 1. thông số trên site cùng tên (Material, Fit, Pattern...)
        for key in DETAIL_ALIASES.get(low, (low,)):
            if key in details:
                dv = details[key]
                if low.startswith("country") and dv.lower().startswith("import"):
                    continue
                val = _match_value(dv, vals) if vals else ("" if sel else dv)
                # ô gõ tự do: chỉ lấy nguyên chữ của site khi ngắn gọn (không đổ cả đoạn
                # "Misses: 17 1/2"; Petite: 16 1/2"; Plus: 19"…" vào ô Fit)
                if not val and not sel and dv and len(dv) <= 50 and dv.count(":") < 2:
                    val = dv
                if val:
                    break
        # 2. chất liệu: chỉ đọc câu mô tả nói "... is/made of <chất liệu>" — không lấy từ tên sản phẩm
        #    (tên "Paper Towel Holder", "Wine Glass Rack" không phải chất liệu)
        if "material" in low:
            # Chất liệu CHÍNH mới đoán từ câu mô tả; chất liệu phụ (Lining, Upper, Handle, Sole...) chỉ lấy khi site
            # ghi đúng tên (bước 1) — "Wool Blazer" không có nghĩa lớp lót là Wool
            if not val and vals and low in MAIN_MATERIAL:
                if mat_text is None:
                    mat_text = _material_text(prod)
                # ô gõ tự do: thêm chất liệu phổ biến eBay chưa có trong gợi ý (vd Resin); lấy cái nhắc tới trước
                val = _match_value(mat_text, list(vals) + ([] if sel else [m.title() for m in COMMON_MATERIALS]))
                if not val and prod.get("department"):   # quần áo: tên kiểu "Cashmere Sweater" thì đúng là chất liệu
                    val = _match_value(text, vals)
        # 2b. giá trị eBay cho phép xuất hiện trong tên / danh mục sản phẩm — trừ các ô mà chữ trong tên dễ trùng
        #     nhầm ("Jersey Knit" -> Country of Origin = Jersey, "Cable Stitch" -> Character = Stitch)
        elif not val and vals and not NO_TITLE_MATCH.search(low):
            # tên sản phẩm trước, không có mới xét nhóm hàng (nhóm chỉ 1 loại)
            val = _match_value(prod.get("title", ""), vals) or (_match_value(crumbs_txt, vals) if crumbs_txt else "")
        # 3. bắt buộc mà vẫn trống: chỉ lấy thông tin có thật trên site (không đoán)
        if not val and a.get("required"):
            if low == "color":
                c = details.get("color") or details.get("colour") or details.get("finish") or ""
                val = _match_value(c + " " + text, vals or BASIC_COLORS) or ("" if sel else c)
            elif low == "department" and prod.get("department"):
                val = prod["department"]
        # 4. bắt buộc mà site không có -> "Does Not Apply" (ô chỉ-chọn mà eBay không cho giá trị này thì để người dùng chọn)
        if not val and a.get("required") and (not sel or any(x.lower() == "does not apply" for x in vals)):
            val = "Does Not Apply"
        if val:
            out[name] = clean_value(val, int(a.get("max_len") or 65))
        elif a.get("required"):
            missing.append(name)
    return out, missing


def parse_specifics(text):
    out = {}
    for part in (text or "").split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


# ---- FCC ID: eBay Mỹ bắt thiết bị phát sóng (Wi-Fi, Bluetooth, di động...) ghi FCC ID khớp đúng model (từ 13/10/2026);
# thiếu -> listing báo "Electronic equipment policy violation" và bị ẩn với người mua Mỹ. Tool không tra được FCC ID chắc chắn
# theo model, nên chỉ đánh dấu dòng cần; người dùng điền ở cửa sổ Xem -> item specific "FCC ID" (cột C:FCC ID).
FCC_NAME = "FCC ID"
# "Wi-Fi Enabled: No", "App Compatible: No"... (bảng thông số Best Buy) -> bỏ trước khi tìm dấu hiệu
_FCC_OFF = re.compile(r"(?i)\b(?:wireless connectivity|app compatible|bluetooth(?: enabled)?|wi-?fi(?: enabled)?|"
                      r"voice assistant built-in|works with)\s*:\s*(?:no|none|not applicable)\b")
# Hồng ngoại (remote máy sưởi, quạt) không cần FCC ID -> không tính "remote"; "cordless" là không dây điện, không phát sóng.
# Chữ lấy từ trang hay bị dính ("smart controlthrough the VeSync app", "third partyvoice assistants") -> không đòi \b hai đầu
_FCC_ON = re.compile(r"(?i)\bwi-?fi\b|\bbluetooth\b|\bcellular\b|\blte\b|\bzigbee\b|\bz-wave\b|\bmatter-enabled\b|"
                     r"app compatible\s*:\s*yes|\bworks with\s*:\s*(?:amazon alexa|google|apple homekit)|"
                     r"voice ?(?:control|assistant)|smart ?(?:home|control|app)|"
                     r"\b(?:via|through|with|using|in|from) (?:the |our |its )?[\w-]+ ?app\b|\bapp[- ](?:enabled|controlled)\b|"
                     r"\bwireless (?:mouse|keyboard|headphones?|earbuds?|speakers?|charg\w*|camera|doorbell|router|controller)")


def fcc_needed(prod):
    """Sản phẩm có dấu hiệu phát sóng (Wi-Fi, Bluetooth, điều khiển qua app / trợ lý giọng nói...) -> eBay Mỹ đòi FCC ID."""
    text = " ".join([prod.get("title") or "", *(prod.get("bullets") or []),
                     *(str(v) for v in (prod.get("details") or {}).values())])
    return bool(_FCC_ON.search(_FCC_OFF.sub(" ", text)))


def fcc_id(item):
    return next((v for k, v in parse_specifics(item.get("specifics", "")).items() if k.lower() == FCC_NAME.lower()), "")


def fcc_missing(item):
    return bool(item.get("product")) and fcc_needed(item["product"]) and not fcc_id(item)


def export_note(msg):
    """Ghi chú của export_run chỉ để biết (listing vẫn xuất), khác lỗi làm listing bị bỏ."""
    return msg.startswith(("Danh sách", "FCC ID")) or "ĐÃ ĐANG BÁN" in msg or "đổi SKU" in msg


DEPT_WORD = {"women": "Women's", "men": "Men's", "girls": "Girls'", "boys": "Boys'",
             "baby girls": "Baby Girls'", "baby boys": "Baby Boys'"}


def title_brand(prod, cfg):
    """Tên hãng đặt ở đầu tiêu đề (Cài đặt "Tên hãng ở đầu tiêu đề"); ô Brand của listing vẫn là Unbranded."""
    if not cfg.get("brand_in_title", True):
        return ""
    # Fanatics ghi hãng hàng in theo đơn là "Fanatics MTO Label" -> tiêu đề chỉ "Fanatics" (cả dòng đã chạy từ trước)
    return re.sub(r"(?i)\s+MTO Label$", "", (prod.get("brand") or "").strip())


def make_title(prod, cfg, max_len=80):
    """Tiêu đề theo site: "[Hãng] + Women's/Men's/Girls'... + tên sản phẩm" (dài quá thì app nhờ AI viết lại)."""
    dept = DEPT_WORD.get((prod.get("department") or "").lower(), "")
    brand = title_brand(prod, cfg)
    name = prod["title"]
    if prod.get("site") == "fanatics":   # Fanatics: tên site đã ghi sẵn Women's / Youth / Unisex khi cần -> không tự thêm Men's / Women's
        dept = ""
    if brand:   # bộ cào sót tên hãng trong tên -> bỏ để không lặp "Hãng Women's Hãng Coat" (kể cả hãng "'47")
        name = re.sub(rf"(?i)(?<!\w){re.escape(brand)}(?!\w)[’']?s?\s*", "", name).strip(" :-–")   # "Easy Playhouse: Tent"
    if dept:    # tên site đã có "Womens" / "Mens" (không dấu nháy) -> coi như đã có Department
        name = re.sub(rf"(?i)^{re.escape(dept.replace(chr(39), ''))}\b\s*", "", name)
    t = f"{dept} {name}"
    t = re.sub(r"\s{2,}", " ", t).strip()
    # Tên sản phẩm đã có sẵn chữ Women's/Men's thì không thêm lần nữa
    if dept and t.lower().count(dept.lower()) > 1:
        t = t.replace(dept + " ", "", 1)
    if brand and not t.lower().startswith(brand.lower()):
        t = f"{brand} {t}"
    return shorten_title(t, max_len)


# Chữ loại hàng tool tự ghép vào tiêu đề (vd Sonos "Era 100" + "Wireless Speaker") -> mục eBay hợp lệ phải có 1 trong
# các từ này. Không khớp = có thể ghép sai loại hàng (hoặc eBay chọn sai mục) -> báo ⚠ để người dùng xem lại.
TITLE_ADD_MATCH = {
    "speaker": ("speaker", "audio"), "soundbar": ("speaker", "soundbar", "home theater", "audio"),
    "subwoofer": ("subwoofer", "speaker"), "headphones": ("headphone", "earphone"),
    "system": ("home theater", "speaker", "audio"), "bundle": ("speaker", "audio"),
    "component": ("home audio", "receiver", "amplifier", "stream", "network", "audio"),
    "amplifier": ("amplifier", "receiver", "audio"),
}


def title_warn(item):
    """Cảnh báo khi chữ loại hàng tool ghép vào tiêu đề không khớp mục eBay đang dùng. Trả về "" nếu ổn / không kiểm được."""
    add = (item.get("product") or {}).get("title_add") or ""
    if not add or item.get("category"):   # không ghép, hoặc người dùng đã tự gõ Category -> không kiểm
        return ""
    api = item.get("category_api") or {}
    path = api.get("path") or "" if api.get("id") == item.get("category_auto") else ""
    if not path or title_add_fits(add, path):
        return ""
    return f"Tool ghép \"{add}\" vào tiêu đề nhưng eBay chọn mục \"{path.split(' › ')[-1]}\" — xem lại tiêu đề / Category"


def title_add_rank(add, path):
    """Thứ tự từ khoá đầu tiên khớp (nhỏ = khớp từ quan trọng hơn)."""
    tail = " › ".join(path.split(" › ")[-2:]).lower()
    want = TITLE_ADD_MATCH.get(add.split()[-1].lower(), (add.split()[-1].lower().rstrip("s"),))
    return next((i for i, w in enumerate(want) if w in tail), len(want))


def _stems(text):
    """Gốc từ để so tên món với tên mục eBay ("Charging Base" ~ "Chargers & Cradles")."""
    # bỏ "s"/"es" số nhiều trước khi cắt 5 chữ: "Lamp" ~ "Lamps", "Vase" ~ "Vases", "Hook" ~ "Hooks"
    return {re.sub(r"(es|s)$", "", w)[:5] for w in re.findall(r"[a-zà-ÿ]{4,}", text.lower())}


def match_by_name(opts, title):
    """Mục eBay đầu tiên không dính gì tới tên món mà mục khác có (vd "Play Charging Base": Media Streamers -> Chargers)
    -> chọn mục có nhiều từ khớp nhất. Mục đầu đã khớp thì giữ (không đổi kết quả của eBay khi không cần)."""
    tail = lambda o: _stems(" ".join((o.get("path") or "").split(" › ")[-2:]))   # noqa: E731
    # Mục đầu của eBay có dính tới BẤT KỲ từ nào trong tên -> giữ (vd "Florentina Table Runner" -> Table Runners)
    if not opts or _stems(title) & tail(opts[0]):
        return opts
    # Chọn lại chỉ theo 2 từ cuối (thường là loại món: "Charging Base", "Wall Hook");
    # từ chủ đề / màu như "Wine Picnic", "Antique Gold" không dùng để chọn
    words = _stems(" ".join(re.findall(r"[A-Za-z]{4,}", title)[-2:]))
    score = lambda o: len(words & tail(o))   # noqa: E731
    best = max(opts, key=score)
    return [best] + [o for o in opts if o is not best] if score(best) else opts


def title_add_fits(add, path):
    """Mục eBay (chỉ xét 2 cấp cuối — cấp trên kiểu "TV, Video & Home Audio" quá chung) có hợp với loại hàng không."""
    if not add:
        return True
    tail = " › ".join(path.split(" › ")[-2:]).lower()
    want = TITLE_ADD_MATCH.get(add.split()[-1].lower(), (add.split()[-1].lower().rstrip("s"),))
    return any(w in tail for w in want)


def fit_title(item, max_len):
    """Tiêu đề xuất CSV, tối đa max_len: dài quá thì cắt phần tên gốc, GIỮ ", Màu, Nhóm size" ở cuối
    (phần phân biệt các listing cùng sản phẩm)."""
    title, extra = item["title"], item.get("title_extra") or ""
    if len(title) > max_len and extra and title.endswith(", " + extra) and len(extra) + 12 < max_len:
        base = shorten_title(title[: -(len(extra) + 2)], max_len - len(extra) - 2)
        return f"{base}, {extra}"
    return shorten_title(title, max_len)


def shorten_title(title, max_len):
    max_len = max(int(max_len), 0)
    if len(title) <= max_len:
        return title
    cut = title[:max_len + 1].rsplit(" ", 1)[0][:max_len]   # không có dấu cách nào -> cắt cứng, không quá max_len
    return cut.rstrip(" -,&|")


def size_type(sizes, department=""):
    """Item specific Size Type từ nhãn size: mọi size đều Petite/Plus/Tall thì lấy loại đó, lẫn lộn thì Regular.
    department "Men": "30W" là vòng eo (không phải size Plus của đồ nữ)."""
    kinds = set()
    men = (department or "").lower() in ("men", "boys")
    for s in sizes:
        s = s.lower()
        # quần nam "30W x 32L" / "30W" (vòng eo) không phải size Plus "14W" của đồ nữ
        s = re.sub(r"\b\d+\s*w\s*x\s*\d+\s*l\b|\b\d+\s*w\b(?=\s*x)", "", s)
        if men:
            s = re.sub(r"\b\d+\s*w\b", "", s)
        if "petite" in s:
            kinds.add("Petite")
        elif re.search(r"\bplus\b|\b\d+w\b|\b[0-4]x\b|\bwoman\b", s):
            kinds.add("Plus")
        elif "tall" in s:
            kinds.add("Tall")
        else:
            kinds.add("Regular")
    return kinds.pop() if len(kinds) == 1 else "Regular"


# ---------------------------------------------------------------- CSV

ACTION = "*Action(SiteID=US|Country={country}|Currency=USD|Version=1193|CC=UTF-8)"
BASE_COLS = ["CustomLabel", "*Category", "*Title", "Relationship", "RelationshipDetails", "*ConditionID",
             "ConditionDescription", "*C:Brand", "*C:Size Type", "*C:Size", "*C:Color", "*C:Department"]
TAIL_COLS = ["C:Personalize", "C:Personalization Instructions", "PicURL", "*Description", "*Format", "*Duration",
             "*StartPrice", "*Quantity", "*Location", "ShippingProfileName", "ReturnProfileName", "PaymentProfileName"]
PERSONALIZE_MAX = 65   # eBay: mỗi item specific tối đa 65 ký tự
CONDITION_NAMES = {"1000": "New", "1500": "Open box", "2000": "Certified refurbished", "2500": "Seller refurbished",
                   "3000": "Used", "7000": "For parts or not working"}
# Hàng hãng tân trang (Sonos "Certified Refurbished"): eBay chỉ cho "Refurbished" với người bán trong chương trình
# eBay Refurbished -> dùng Seller refurbished nếu Category cho, không thì Used + ghi chú tình trạng
REFURB_PREFER = ("2500", "3000")
REFURB_NOTE = ("Manufacturer refurbished: inspected and tested by the manufacturer. "
               "Includes all accessories, manuals and documentation.")


def is_refurb(prod):
    return (prod.get("condition") or "").lower() == "refurbished"


def condition_for(item, cfg):
    """(ConditionID, ghi chú tình trạng) của listing. Hàng mới: Condition trong Cài đặt (mặc định 1000 New)."""
    prod = item["product"]
    if not is_refurb(prod):
        return cfg.get("condition_id", "1000"), ""
    cat = category_for(prod, cfg, item.get("category", ""), item.get("category_api"))[0]
    allowed = item.get("conditions") if item.get("conditions_cat") == cat else None
    if allowed:   # biết danh sách của Category: chỉ dùng mã eBay cho phép
        cid = next((c for c in REFURB_PREFER if c in allowed), None)
        if cid is None:
            raise RuntimeError(f"Category {cat} không cho đăng hàng tân trang (chỉ cho: "
                               f"{', '.join(allowed.values())}) — gõ Category khác ở bảng Kết quả")
    else:
        cid = "3000"   # chưa hỏi được eBay: Used là tình trạng mục nào cũng có
    return cid, prod.get("condition_note") or REFURB_NOTE
SKU_MAX = 50           # eBay: Custom label tối đa 50 ký tự


def clean_value(v, max_len=65):
    """Giá trị biến thể/specific: bỏ ký tự phân cách của File Exchange (; | =), giới hạn 65 ký tự."""
    v = re.sub(r"\s+", " ", re.sub(r"[;|=]", "/", str(v or ""))).strip()
    if len(v) <= max_len:
        return v
    cut = v[:max_len + 1].rsplit(" ", 1)[0]   # cắt ở ranh giới từ, không cắt giữa chữ
    return (cut if len(cut) >= max_len // 2 else v[:max_len]).rstrip(" ,.;/-")


def sku_profile(profile):
    """Tên Profile dùng trong SKU: bỏ dấu cách và ký tự lạ ("US 1" -> "US1")."""
    return re.sub(r"[^A-Za-z0-9_-]", "", profile or "")


def make_sku(cfg, site_key, code, profile=""):
    """SKU = {Profile}-{tiền tố site}{mã sản phẩm gốc}, vd EBSR73-TH-Q0801208 — nhìn SKU là tra ngược được."""
    prefix = site_cfg(cfg, site_key).get("prefix", "")
    base = re.sub(r"[^A-Za-z0-9_.-]", "", f"{prefix}{code}")
    p = sku_profile(profile)
    sku = f"{p}-{base}" if p else base
    if len(sku) > SKU_MAX:
        # dài quá: cắt bớt + 5 ký tự mã riêng, để "…-ALL" / "…-BLACK" của cùng sản phẩm không bị cắt thành 1 SKU
        sku = f"{sku[:SKU_MAX - 6]}-{hashlib.sha1(sku.encode()).hexdigest()[:5].upper()}"
    return sku


def variant_sku(parent, color, size, n):
    """SKU biến thể: SKU cha + mã màu + size; dài quá 50 ký tự thì dùng số thứ tự."""
    part = "-".join(x for x in (re.sub(r"[^A-Za-z0-9]", "", color)[:6].upper(),
                                 re.sub(r"[^A-Za-z0-9.]", "", size)[:8].upper()) if x)
    sku = f"{parent}-{part}" if part else f"{parent}-{n}"
    return sku if len(sku) <= SKU_MAX else f"{parent[:SKU_MAX - 5]}-V{n:03d}"


# Không đưa vào mô tả eBay: thông tin đổi theo thời gian (tình trạng hàng, ngày giao) và mã sản phẩm của site
# (người mua tra ra nguồn hàng). Vẫn giữ trong dữ liệu sản phẩm để xem trong tool.
DROP_DETAIL_KEY = re.compile(
    r"(?i)availab|stock|lead time|processing|production|deliver|shipping|ship time|made to order|estimated"
    r"|item (number|no\.?|#)|\bsku\b|style (number|no\.?|#)|product (number|no\.?|#|id)|model (number|no\.?)"
    r"|\bupc\b|\bmpn\b|\bgtin\b")
DROP_LINE = re.compile(
    r"(?i)\b\d{1,2}/\d{1,2}/\d{2,4}\b|order now|ships? (in|within|by|from our)|ready to ship|in stock|out of stock"
    r"|back-?order|pre-?order|only \d+ left|estimated (production|delivery|ship)|production time"
    r"|(item|product|style|model)\s*(#|no\.|number)\s*:?\s*\w|\bsku\s*[:#]"
    r"|^\s*(please\s+)?specify\b"    # "Specify Black or Brown": dặn chọn lựa chọn trên form đặt hàng của site
    r"|\bsearch\s+[A-Z0-9]{2,6}-\d{3}\b"   # "Search EL01-001 for Espresso": mã hàng khác của site
    r"|search box|you might also consider|^\s*enter [A-Z0-9]{3,5}\b")


def desc_lines(prod):
    """(đoạn mô tả, thông số) sẽ ghi vào listing, đã bỏ thông tin dễ sai theo thời gian / lộ nguồn."""
    # lọc theo từng câu: đoạn dài có 1 câu "Order now for shipment on 10/15" / "Specify finish..." chỉ bỏ câu đó
    # bỏ link / địa chỉ web của site (eBay cấm link ra ngoài) — cả với dữ liệu đã cào từ trước
    bullets = []
    for b in prod.get("bullets", []):
        b = strip_links(b)
        keep = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", b.strip()) if s.strip() and not DROP_LINE.search(s)]
        if keep:
            bullets.append(" ".join(keep))
    details = {}
    for k, v in (prod.get("details") or {}).items():
        k, v = strip_links(k), strip_links(v)
        if k and v.strip() and not DROP_DETAIL_KEY.search(k) and not DROP_LINE.search(v):
            details[k] = v
    return bullets, details


def build_description(item, cfg):
    prod = item["product"]
    bullets, details = desc_lines(prod)
    parts = [f"<p>{html.escape(b)}</p>" for b in bullets]
    if is_refurb(prod):   # hàng tân trang: nói rõ ngay đầu mô tả
        parts.insert(0, f"<p><b>Condition:</b> {html.escape(prod.get('condition_note') or REFURB_NOTE)}</p>")
    if cfg.get("show_details", True) and details:
        lis = "".join(f"<li><b>{html.escape(k)}:</b> {html.escape(str(v))}</li>" for k, v in details.items())
        if lis:
            parts.append(f"<ul>{lis}</ul>")
    footer = [b for b in cfg.get("footer_bullets", []) if b.strip()]
    if is_refurb(prod):   # không ghi "Brand new / never worn" cho hàng tân trang
        footer = [b for b in footer if not re.search(r"(?i)brand new|never (worn|used)|new with tags|\bunused\b", b)]
    if footer:
        parts.append("<ul>" + "".join(f"<li>{html.escape(b)}</li>" for b in footer) + "</ul>")
    if prod.get("personalized") and prod.get("personalize_fields"):
        parts.append("<p><b>Personalization:</b> " + html.escape("; ".join(prod["personalize_fields"])) + "</p>")
    return "".join(parts)


FIXED_COLS = {"brand": "*C:Brand", "size type": "*C:Size Type", "size": "*C:Size", "color": "*C:Color",
              "department": "*C:Department"}


def spec_col(name):
    return FIXED_COLS.get(name.lower(), f"C:{name}")


def build_specifics(item, cfg, category, extra, aspects, has_color, has_size):
    """Item specifics của listing + specifics bắt buộc còn thiếu + tên 2 trục biến thể.

    aspects = None khi chưa có danh sách của eBay (không có key API) -> điền như cũ, không kiểm tra thiếu."""
    prod = item["product"]
    known = aspects is not None
    names = {a["name"].lower(): a["name"] for a in aspects or []}
    color_axis, size_axis = axis_names(aspects)
    color_axis = color_axis or "Color"
    size_axis = volume_axis(prod, aspects) or size_axis
    if not size_axis:
        # Category không cho biến thể theo "Size" -> dùng tên trục của site (vd Design, Style)
        sn = clean_value(prod.get("size_name") or "", 40)
        size_axis = sn if sn and sn.lower() != "size" else ("Size" if not known else "Option")

    def name(n):
        return names.get(n.lower(), n) if (not known or n.lower() in names) else None

    def allowed(n, value):
        """Giá trị hợp lệ cho aspect chỉ-chọn (vd Department "Baby Girls" -> "Girls" nếu Category không có)."""
        a = next((x for x in aspects or [] if x["name"].lower() == n.lower()), None)
        if a and a.get("values") and a.get("mode") != "SELECTION_ONLY":
            # ô gõ tự do nhưng eBay có giá trị gợi ý hơi khác tên ("Regular" -> "Regular Size" ở nước hoa): dùng giá trị của eBay
            hit = {v.lower(): v for v in a["values"]}.get(f"{value} size".lower())
            if hit and value.lower() not in {v.lower() for v in a["values"]}:
                return hit
        if not a or a.get("mode") != "SELECTION_ONLY" or not a.get("values"):
            return value
        low = {v.lower(): v for v in a["values"]}
        # eBay có khi dùng số nhiều (vd Size Type "Petites")
        for cand in (value, value + "s", value.rstrip("s"), value.replace("Baby ", ""),
                     "Unisex Kids" if "baby" in value.lower() else ""):
            if cand and cand.lower() in low:
                return low[cand.lower()]
        return ""

    spec = {"Brand": cfg.get("brand") or "Unbranded"}
    sizes = [v["size"] for v in prod["variants"] if v["size"]]
    if prod.get("department") and name("Department") and allowed("Department", prod["department"]):
        spec[name("Department")] = allowed("Department", prod["department"])
    group = item.get("size_group") or ""
    st = GROUP_SIZE_TYPE.get(group.lower(), "Regular") if group else size_type(sizes, prod.get("department"))
    if prod.get("department") and has_size and name("Size Type") and allowed("Size Type", st):
        spec[name("Size Type")] = allowed("Size Type", st)
    spec.update({k: clean_value(v) for k, v in extra.items()})
    # specifics gõ tay: không được trùng trục biến thể (Color / Size / Inseam đang làm biến thể) và không đổi Brand
    ins_axis = inseam_axis(prod, aspects) if has_size else None
    busy = {x.lower() for x, on in ((color_axis, has_color), (size_axis, has_size), (ins_axis, bool(ins_axis)))
            if on} | {"brand"}
    spec.update({k: clean_value(v) for k, v in parse_specifics(item.get("specifics", "")).items()
                 if k.lower() not in busy})
    missing = []
    if known:
        axes = {x for x, on in ((color_axis, has_color), (size_axis, has_size), (ins_axis, bool(ins_axis))) if on}
        auto, missing = fill_aspects(prod, aspects, axes, spec)
        for k, v in auto.items():
            spec.setdefault(k, v)
    return spec, missing, color_axis, size_axis


def listing_rows(item, cfg):
    """Các dòng CSV của 1 listing (dict cột -> giá trị): 1 dòng cha + mỗi biến thể 1 dòng."""
    prod = item["product"]
    percent = site_percent(cfg, prod["site"])
    rnd = cfg.get("round_99", True)
    qty = str(int(cfg.get("quantity", 1)))
    variants = selling_variants(prod, cfg, item)
    if not variants:
        raise RuntimeError("Chưa chọn biến thể nào — bấm Xem để tích biến thể muốn bán")
    if not any(v.get("available") for v in variants):
        raise RuntimeError("Các biến thể đã chọn đều hết hàng trên site — bấm Xem để chọn lại")
    # Chọn đúng 1 nhóm size (vd Misses) -> nhóm đã nằm trong tiêu đề, size ghi gọn "1X" thay vì "1X Plus Petite"
    one_group = item.get("size_group") or ""
    # Danh sách Size chuẩn của Category (eBay bắt chọn trong danh sách, vd 2XL chứ không phải XXL)
    cat0, _, _ = category_for(prod, cfg, item.get("category", ""), item.get("category_api"))
    asp0 = item.get("aspects") if item.get("aspects_cat") == cat0 else None
    size_vals = next((a.get("values") for a in asp0 or [] if a["name"].lower() == "size" and a.get("values")), None)
    # Size ghép "S/M", "L/XL" mà danh sách Size của Category không có (vd tất nam: eBay chỉ nhận S, M, L, XL)
    # -> tách thành 2 biến thể S + M / L + XL, cùng giá, cùng tồn kho của size site; khách đặt S hay M thì
    # người bán tự quy về S/M của site. _site_size giữ size gốc để Theo dõi sản phẩm kiểm tra đúng biến thể site.
    if size_vals:
        allowed = {x.lower() for x in size_vals}
        split = []
        for v in variants:
            parts = [p.strip() for p in re.split(r"\s*/\s*", v["size"] or "")]
            if len(parts) == 2 and v["size"].lower() not in allowed and all(p.lower() in allowed for p in parts):
                split += [dict(v, size=p, _site_size=v["size"]) for p in parts]
            else:
                split.append(v)
        variants = split
    # Quần "32W x 30L": Size = "32", trục Inseam = "30 in" (eBay không nhận "32W x 30L" ở ô Size)
    ins_axis = inseam_axis(prod, asp0)

    def vinseam(label):
        return f"{waist_inseam(label)[1]} in" if ins_axis and label else ""

    def vsize_raw(label):
        if norm_volume(label):   # nước hoa: giá trị trục Volume "1.7 fl oz"
            return norm_volume(label)
        if ins_axis and label:
            label = waist_inseam(label)[0]
        std = ebay_size(label, prod["site"], size_vals)
        if std:
            return std
        return split_size(label, prod["site"])[0] if one_group else label

    # 2 nhãn size khác nhau ra cùng 1 size eBay (vd "M" và "M Tall" khi Category không có size Tall)
    # -> giữ nguyên nhãn site cho các size đó, KHÔNG bỏ mất biến thể
    mapped = {}
    for lab in dict.fromkeys(v["size"] for v in variants if v["size"]):
        mapped.setdefault((clean_value(vsize_raw(lab)), vinseam(lab)), []).append(lab)
    clash = {lab for labs in mapped.values() if len(labs) > 1 for lab in labs}

    def vsize(v):
        if v["size"] in clash:
            return split_size(v["size"], prod["site"])[0] if one_group else v["size"]
        return vsize_raw(v["size"])

    colors = [c for c in dict.fromkeys(clean_value(v["color"]) for v in variants) if c]
    sizes = [s for s in dict.fromkeys(clean_value(vsize(v)) for v in variants) if s]
    # thứ tự size theo trang site (gom mọi màu), không theo thứ tự gặp trong các biến thể đang chọn
    # (vd Talbots "2;4;6 Petite;8 Petite;6;8" -> "2;4;6;8;...;6 Petite;8 Petite")
    site_order = {s: i for i, s in enumerate(dict.fromkeys(clean_value(vsize(v)) for v in prod["variants"]))}
    sizes.sort(key=lambda s: site_order.get(s, len(site_order)))
    inseams = sorted({vinseam(v["size"]) for v in variants} - {""}, key=lambda x: int(x.split()[0]))
    # Chỉ 1 chiều dài (tool chốt 32L / 30L / 34L) -> Inseam là item specific cố định, không làm trục biến thể
    one_inseam = inseams[0] if len(inseams) == 1 else ""
    if one_inseam:
        inseams = []

        def vinseam(label):   # noqa: F811 — từ đây biến thể không còn trục Inseam
            return ""
    # Chỉ 1 màu -> màu là item specific cố định (không làm biến thể), listing chỉ còn biến thể Size
    single_color = colors[0] if len(colors) == 1 else ""
    has_color = len(colors) > 1
    has_size = bool(sizes)
    category, extra, _ = category_for(prod, cfg, item.get("category", ""), item.get("category_api"))
    if single_color:
        extra = dict(extra, Color=single_color)
    if not category:
        raise RuntimeError("Chưa có Category ID — gõ Category vào bảng Kết quả, hoặc điền key eBay API / quy tắc trong Cài đặt")
    aspects = item.get("aspects") if item.get("aspects_cat") == category else None
    specifics, missing, color_axis, size_axis = build_specifics(item, cfg, category, extra, aspects, has_color, has_size)
    if missing:
        raise RuntimeError(f"Thiếu item specifics bắt buộc của Category {category}: {', '.join(missing)} "
                           "— bấm Xem ở dòng này để điền")
    main_pics, color_pics = listing_pics(item, cfg)
    cond_id, cond_note = condition_for(item, cfg)
    sc = site_cfg(cfg, prod["site"])
    parent = {
        "CustomLabel": item["sku"],
        "*Category": category,
        "*Title": fit_title(item, cfg.get("title_max_len", 80)),
        "*ConditionID": cond_id,
        "ConditionDescription": cond_note,
        "PicURL": "|".join(main_pics[:24]),
        "*Description": build_description(item, cfg),
        "*Format": "FixedPrice",
        "*Duration": "GTC",
        "*Location": cfg.get("location", ""),
        "ShippingProfileName": sc.get("shipping_profile") or cfg.get("shipping_profile", ""),
        "ReturnProfileName": cfg.get("return_profile", ""),
        "PaymentProfileName": cfg.get("payment_profile", ""),
    }
    for k, v in specifics.items():
        parent[spec_col(k)] = v
    if one_inseam:
        parent[spec_col(ins_axis)] = one_inseam
    if prod.get("personalized"):
        hint = "; ".join(prod.get("personalize_fields") or []) or cfg.get("personalize_text", "")
        parent["C:Personalize"] = "Yes"
        parent["C:Personalization Instructions"] = hint[:PERSONALIZE_MAX]

    # Không có biến thể (1 màu 1 size hoặc không có trục nào) -> 1 dòng duy nhất
    if len(variants) == 1:
        v = variants[0]
        parent["*StartPrice"] = f"{ebay_price(base_price(v, prod['site']), percent, rnd):.2f}"
        parent["*Quantity"] = qty
        parent["_src"] = (v["price"], v.get("list_price"))   # giá site (cửa sổ Xem đọc; không ghi ra CSV)
        parent["_vkey"] = vkey(v)   # biến thể site của dòng này (Theo dõi sản phẩm đọc; không ghi ra CSV)
        if v["color"]:
            parent[spec_col(color_axis)] = clean_value(v["color"])
        # chỉ 1 cỡ "One Size" mà Category không có ô Size (tool đặt tên "Option") -> không ghi ô size thừa
        if vsize(v) and not (size_axis == "Option" and re.fullmatch(r"(?i)one\s*size(\s+fits\s+(all|most))?|os|o/s",
                                                                   vsize(v).strip())):
            parent[spec_col(size_axis)] = clean_value(vsize(v))
        if vinseam(v["size"]):
            parent[spec_col(ins_axis)] = vinseam(v["size"])
        return [parent]

    # Màu đứng trước trong RelationshipDetails: ảnh biến thể gắn theo màu
    detail = []
    if has_color:
        detail.append(f"{color_axis}=" + ";".join(colors))
    if has_size:
        detail.append(f"{size_axis}=" + ";".join(sizes))
    if inseams:
        detail.append(f"{ins_axis}=" + ";".join(inseams))
    parent["RelationshipDetails"] = "|".join(detail)
    rows = [parent]
    pic_done, combos, skus = set(), set(), set()
    for n, v in enumerate(variants, 1):
        c, s, i = clean_value(v["color"]), clean_value(vsize(v)), vinseam(v["size"])
        if (c, s, i) in combos:   # 2 biến thể trùng tên sau khi làm sạch -> eBay báo lỗi, giữ cái đầu
            continue
        combos.add((c, s, i))
        rd = "|".join(x for x in ((f"{color_axis}={c}" if has_color else ""),
                                  (f"{size_axis}={s}" if has_size else ""),
                                  (f"{ins_axis}={i}" if i else "")) if x)
        # SKU biến thể phải khác nhau: 2 màu chung 6 chữ đầu ("Bluff Blue" / "Bluff Blue Stripe") -> dùng số thứ tự
        sku = variant_sku(item["sku"], v["color"], v["size"], n)
        if sku in skus:
            sku = f"{item['sku'][:SKU_MAX - 5]}-V{n:03d}"
        skus.add(sku)
        row = {
            "_src": (v["price"], v.get("list_price")),
            "_vkey": vkey(dict(v, size=v.get("_site_size", v["size"]))),   # size tách từ "S/M" -> vẫn là biến thể S/M của site
            "CustomLabel": sku,
            "Relationship": "Variation",
            "RelationshipDetails": rd,
            "*StartPrice": f"{ebay_price(base_price(v, prod['site']), percent, rnd):.2f}",
            "*Quantity": qty if v.get("available") else "0",
        }
        # Ảnh biến thể: ghi ở dòng đầu tiên của mỗi màu
        if has_color and v["color"] in color_pics and c not in pic_done:
            row["PicURL"] = "|".join(color_pics[v["color"]][:12])
            pic_done.add(c)
        rows.append(row)
    return rows


def write_csv(path, items, cfg, country):
    rows = []
    for item in items:
        rows.extend(listing_rows(item, cfg))
    extra_cols = sorted({k for r in rows for k in r if k.startswith("C:") and k not in TAIL_COLS})
    header = BASE_COLS + extra_cols + TAIL_COLS
    info = ["Info", "Version=1.0.0", "Template=fx_category_template_EBAY_US"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(info + [""] * (len(header) + 1 - len(info)))
        w.writerow([ACTION.format(country=country or "US")] + header)
        for r in rows:
            first = "Add" if r.get("Relationship") != "Variation" else ""
            w.writerow([first] + [r.get(col, "") for col in header])
    return path


def group_by_profile(items):
    groups = {}
    for it in items:
        groups.setdefault(it.get("profile") or "", []).append(it)
    return groups


def clean_country(code):
    code = (code or "").strip().upper()
    return code if re.fullmatch(r"[A-Z]{2}", code) else ""


def country_conflicts(items):
    """{profile: [các country khác nhau]} cho Profile có nhiều hơn 1 quốc gia (không xuất được 1 file)."""
    seen = {}
    for it in items:
        seen.setdefault(it.get("profile") or "", set()).add(it.get("country") or "US")
    return {p: sorted(c) for p, c in seen.items() if len(c) > 1}


def out_dir(site_key):
    """output/<site>/goc: CSV gốc tự lưu lúc chạy xong (chưa sửa gì)."""
    return OUTPUT_DIR / site_key / "goc"


def unique_path(path):
    """Không ghi đè file đã có: thêm _2, _3... vào tên."""
    n, candidate = 2, path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        n += 1
    return candidate


def live_listings():
    """SKU -> Item ID của các listing đang bán trên eBay theo Theo dõi sản phẩm (đã nạp kết quả upload / báo cáo
    Active listings). SKU có tên acc ở đầu nên cùng link đăng sang acc khác vẫn xuất được bình thường."""
    path = Path(os.environ.get("RETAIL_TRACKING") or ROOT / "tracking.json")
    items = (load_json(path, {}) or {}).get("items") or {}
    return {k: v["item_id"] for k, v in items.items() if v.get("item_id") and v.get("status") == "live"}


def _vtail(color, size):
    """Phần màu + size của SKU biến thể (giống variant_sku): "Sun Drift", "XS" -> "SUNDRI-XS"."""
    return "-".join(x for x in (re.sub(r"[^A-Za-z0-9]", "", color or "")[:6].upper(),
                                 re.sub(r"[^A-Za-z0-9.]", "", size or "")[:8].upper()) if x)


def check_live_dupes(items, cfg, site_key):
    """Trước khi xuất: so với listing ĐANG BÁN trên đúng acc (Theo dõi sản phẩm), theo MÀU + SIZE chứ không chỉ SKU.
    KHÔNG bỏ listing nào (26/10): trùng màu + size, hoặc SKU đang dùng cho listing khác -> vẫn xuất, SKU thêm số
    -1, -2, -3... cho khỏi trùng SKU; listing trùng màu + size ghi it["_dupinfo"] để xuất file danh sách trùng.
    Trả về list thông báo (ghi log)."""
    path = Path(os.environ.get("RETAIL_TRACKING") or ROOT / "tracking.json")
    tracked = {k: v for k, v in ((load_json(path, {}) or {}).get("items") or {}).items()
               if v.get("item_id") and v.get("status") == "live"}
    notes, used = [], set()
    for it in items:
        it.pop("_dup", None)   # dấu của lần xuất trước không giữ lại
        it.pop("_dupinfo", None)
        prod = it.get("product") or {}
        base = make_sku(cfg, site_key, it.get("code") or prod.get("code", ""), it.get("profile", ""))
        mine = {_vtail(v["color"], v["size"]) for v in selling_variants(prod, cfg, it)} if prod else set()
        hit = None
        for k, v in tracked.items():
            if k != base and not k.startswith(base + "-"):
                continue
            theirs = {_vtail(x.get("color"), x.get("size")) if (x.get("color") or x.get("size"))
                      else "-".join((x.get("vsku") or "").split("-")[-2:]) for x in v.get("variants") or []}
            if mine & theirs:
                hit = (k, v, sorted(mine & theirs))
                break
        if hit:   # 26/10: không lọc nữa — vẫn xuất, ghi vào file danh sách trùng
            k, v, same = hit
            it["_dupinfo"] = {"old_sku": k, "item_id": v["item_id"], "old_title": v.get("title", ""),
                              "old_url": v.get("url", ""), "same": ", ".join(same[:12]) + (" ..." if len(same) > 12 else "")}
        if hit or it.get("sku") in tracked or it.get("sku") in used:   # SKU thêm số 1, 2, 3... cho khỏi trùng
            n = 1
            while f"{it['sku']}-{n}" in tracked or f"{it['sku']}-{n}" in used:
                n += 1
            new = f"{it['sku']}-{n}"
            why = f"trùng màu + size với {hit[0]} (Item ID {hit[1]['item_id']}) đang bán" if hit else "SKU đang dùng cho listing khác"
            notes.append(f"{it['sku']}: {why} -> vẫn xuất, đổi SKU thành {new}")
            it["sku"] = new
        used.add(it.get("sku"))
    return notes


def export_run(items, cfg, run_id, site_key, folder=None, log=None, step=None):
    """Xuất mỗi Profile 1 file CSV. Trả về (list đường dẫn file, list lỗi từng listing bị bỏ).

    Gốc (lúc chạy xong): output/<site>/goc/<lần chạy>/.
    folder: thư mục người dùng chọn khi bấm "Xuất CSV" (không ghi đè file cùng tên đã có).
    log: hàm ghi log tiến độ (bước kiểm ảnh có thể lâu); step: hàm nhận chữ "đang làm gì" cho giao diện."""
    conflicts = country_conflicts(items)
    if conflicts:
        detail = "; ".join(f"{p or '(trống)'}: {', '.join(c)}" for p, c in conflicts.items())
        raise RuntimeError(f"Mỗi Profile chỉ được 1 Action Country — đang lệch ở {detail}")
    good, errors = [], []
    if step:
        step("Đối chiếu với listing đang bán")
    errors += check_live_dupes(items, cfg, site_key)
    try:   # ảnh nào host đang trả lỗi (vd ImgBB 503) thì up lại sang nơi khác trước khi ghi link vào file
        errors += fix_bad_pics(items, cfg, log=log, step=step)
    except Exception as e:
        errors.append(f"Danh sách ảnh lỗi: chưa kiểm tra được ảnh ({str(e)[:100]})")
    if step:
        step("Ghi file CSV")
    for it in items:
        if it.get("_dup"):
            continue
        try:
            listing_rows(it, cfg)
            good.append(it)
        except Exception as e:
            errors.append(f"{it['sku']}: {e}")
    if not good:
        raise RuntimeError("Không xuất được listing nào:\n" + "\n".join(errors))
    no_fcc = [it.get("sku", "") for it in good if fcc_missing(it)]
    if no_fcc:   # vẫn xuất (eBay nhận file), chỉ nhắc: lên eBay sẽ bị báo vi phạm + ẩn cho tới khi điền
        errors.append(f"FCC ID: {len(no_fcc)} listing có Wi-Fi / Bluetooth chưa điền FCC ID — eBay Mỹ sẽ báo vi phạm và ẩn "
                      "listing (vẫn xuất; bấm Xem ở dòng đó, điền ô FCC ID rồi Xuất CSV lại): "
                      + ", ".join(no_fcc[:8]) + (" ..." if len(no_fcc) > 8 else ""))
    chosen = folder is not None
    folder = Path(folder) if chosen else out_dir(site_key) / run_id
    paths = []
    for profile, group in group_by_profile(good).items():
        name = f"{sku_profile(profile) or 'khong-profile'}_{len(group)}sp"
        if chosen:
            name = f"{site_key}_{name}_{run_id}"
        country = group[0].get("country") or "US"
        target = folder / f"{name}.csv"
        # Profile "US 1" và "US1" ra cùng tên file -> file sau không được ghi đè file trước
        if chosen or target in paths:
            target = unique_path(target)
        paths.append(write_csv(target, group, cfg, country))
    dups = [it for it in good if it.get("_dupinfo")]
    if dups:   # file danh sách trùng (mở bằng Excel): listing vừa xuất (SKU đã thêm số) + listing cũ đang bán
        name = f"TRUNG_{len(dups)}sp" + (f"_{site_key}_{run_id}" if chosen else "")
        target = unique_path(folder / f"{name}.csv") if chosen else folder / f"{name}.csv"
        folder.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Acc", "SKU cũ (đang bán)", "Item ID cũ", "Tên listing cũ", "Link cũ",
                        "Màu-size trùng", "SKU mới (vẫn xuất)", "Tên mới", "Link mới"])
            for it in dups:
                d = it["_dupinfo"]
                w.writerow([sku_profile(it.get("profile", "")), d["old_sku"], d["item_id"], d["old_title"], d["old_url"],
                            d["same"], it.get("sku", ""), it.get("title", ""), it.get("url", "")])
        errors.append(f"Danh sách {len(dups)} listing trùng với listing đang bán (vẫn xuất, SKU đã thêm số): {target}")
    return paths, errors


def new_run_id(site_key):
    """Tên thư mục cho 1 lần chạy, vd 1012-1015 (thêm -2, -3 nếu trùng phút)."""
    base = datetime.now().strftime("%m%d-%H%M")
    run_id, n = base, 2
    while (out_dir(site_key) / run_id).exists():
        run_id, n = f"{base}-{n}", n + 1
    return run_id


# ---------------------------------------------------------------- import danh sách link (CSV / Excel)

IMPORT_COLUMNS = {
    "profile": ("profile", "acc", "account"),
    "country": ("actioncountry", "country", "quocgia"),
    "url": ("linksanpham", "link", "url", "producturl", "productlink", "sourceurl"),
    "site": ("site", "trang", "website"),
}
TEMPLATE_NAME = "FILE MAU NHAP LINK.xlsx"


def template_path(mod=None):
    """1 file mẫu chung cho mọi site (cột Site chọn trong danh sách)."""
    return TEMPLATE_DIR / TEMPLATE_NAME


def make_template(mod=None, path=None):
    """File Excel mẫu để Import: 4 cột Profile · Action Country · Link sản phẩm · Site.
    Cột Site là thanh xổ xuống liệt kê ĐỦ các site đang có trong tool (thêm site mới -> bấm tạo lại file mẫu là có)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.datavalidation import DataValidation

    import sites as registry

    mods = list(registry.load_all().values())
    names = [m.NAME for m in mods]
    path = path or template_path()
    wb = Workbook()
    ws = wb.active
    ws.title = "Links"
    ws.append(["Profile", "Action Country", "Link sản phẩm", "Site"])
    first = mod or (mods[0] if mods else None)
    examples = [(m, ex) for m in ([first] if first else []) + [m for m in mods if m is not first]
                for ex in getattr(m, "EXAMPLES", [])[:1]][:3]
    for m, ex in examples:
        ws.append(["EBSR73", "US", ex, m.NAME])
    head_fill = PatternFill("solid", fgColor="4F46E5")
    thin = Side(style="thin", color="D5DAE3")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = Border(bottom=thin)
    ws.row_dimensions[1].height = 24
    for col, width in zip("ABCD", (18, 16, 95, 24)):
        ws.column_dimensions[col].width = width
    for r in range(2, 2001):
        ws.cell(r, 2).alignment = Alignment(horizontal="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:D2000"

    # Danh sách site để chọn. Ghi thẳng vào ô kiểm tra dữ liệu thì Excel, Google Sheets, LibreOffice đều hiện xổ xuống
    # (Google Sheets bỏ qua danh sách trỏ sang sheet khác khi tải file .xlsx lên). Excel chỉ cho tối đa 255 ký tự,
    # nhiều site hơn mức đó thì mới trỏ sang sheet "Sites" (ẩn).
    lst = wb.create_sheet("Sites")
    for i, n in enumerate(names, 1):
        lst.cell(i, 1, n)
    lst.sheet_state = "hidden"
    inline = ",".join(names)
    formula = f'"{inline}"' if len(inline) <= 250 and not any("," in n for n in names) \
        else f"=Sites!$A$1:$A${max(len(names), 1)}"
    dv_site = DataValidation(type="list", formula1=formula, allow_blank=True,
                             showErrorMessage=True, errorTitle="Site", error="Chọn site trong danh sách xổ xuống")
    dv_site.promptTitle, dv_site.prompt = "Site", "Bấm mũi tên để chọn site"
    dv_site.showInputMessage = True
    ws.add_data_validation(dv_site)
    dv_site.add("D2:D2000")
    dv_cc = DataValidation(type="textLength", operator="equal", formula1="2", allow_blank=True, showErrorMessage=True,
                           errorTitle="Action Country", error="Nhập mã quốc gia 2 chữ cái, vd US, JP, MD")
    ws.add_data_validation(dv_cc)
    dv_cc.add("B2:B2000")

    guide = wb.create_sheet("Huong dan")
    lines = [
        ("CÁCH DÙNG FILE MẪU", True),
        ("1. Mỗi dòng 1 link sản phẩm, điền ở sheet \"Links\". Xoá các dòng ví dụ rồi dán link của bạn vào.", False),
        ("2. Profile: tên acc eBay (vd TR, JP4, MOL114). Mỗi Profile ra 1 file CSV riêng, SKU có tên Profile ở đầu.", False),
        ("3. Action Country: mã quốc gia 2 chữ cái của chủ acc (US, JP, MD...). Một Profile chỉ được 1 mã quốc gia.", False),
        ("4. Link sản phẩm: copy nguyên link trên thanh địa chỉ trình duyệt.", False),
        ("5. Site: bấm mũi tên ở ô để chọn site trong danh sách. Để trống thì tool tự nhận theo link.", False),
        ("6. Một file có thể gồm nhiều site: Import ở tab nào thì tool lấy các dòng của site đó "
         "(dòng site khác được báo lại để Import ở tab của site đó).", False),
        ("7. Để trống Profile / Action Country thì listing đó chưa có acc (Country = US) — điền sau trong bảng Kết quả.", False),
        (f"8. Giữ nguyên dòng tên cột đầu tiên. Site hiện có ({len(names)}): {', '.join(names)}.", False),
    ]
    for text, bold in lines:
        guide.append([text])
        guide.cell(guide.max_row, 1).font = Font(bold=bold, size=12 if bold else 11)
    guide.column_dimensions["A"].width = 130
    wb.active = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _norm_header(h):
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def read_table(name, raw):
    """Đọc file .xlsx hoặc .csv thành list các hàng (list chuỗi)."""
    import io

    if name.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        ws = load_workbook(io.BytesIO(raw), read_only=True, data_only=True).active
        return [["" if v is None else str(v).strip() for v in row] for row in ws.iter_rows(values_only=True)]
    text = raw.decode("latin-1")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]


def parse_import(name, raw, mod, sites):
    """Trả về (items, warnings). items: [{url, key, profile, country, row}]."""
    rows = [r for r in read_table(name, raw) if any(c for c in r)]
    if not rows:
        raise RuntimeError("File trống")
    header = [_norm_header(h) for h in rows[0]]
    col = {}
    for key, names in IMPORT_COLUMNS.items():
        # tên cột khớp đúng trước, rồi mới tới "chứa chữ" (cột "Profile URL" không bị nhận là cột link)
        col[key] = next((i for i, h in enumerate(header) if h in names), None)
        if col[key] is None and key == "url":
            col[key] = next((i for i, h in enumerate(header) if ("url" in h or "link" in h) and "profile" not in h), None)
    if col["url"] is None:
        raise RuntimeError("Không thấy cột link (tên cột: Link sản phẩm). Hàng đầu tiên của file phải là tên cột.")
    by_name = {re.sub(r"[^a-z0-9]", "", m.NAME.lower()): m for m in sites.values()}

    def cell(r, key):
        i = col[key]
        return r[i].strip() if i is not None and i < len(r) else ""

    items, warnings, seen, other = [], [], {}, {}
    for n, r in enumerate(rows[1:], start=2):
        url = cell(r, "url")
        if not url:
            continue
        # Cột Site: dòng của site khác -> để Import ở tab site đó (1 file nhiều site vẫn dùng được)
        site_cell = cell(r, "site")
        site_mod = by_name.get(re.sub(r"[^a-z0-9]", "", site_cell.lower())) if site_cell else None
        if site_cell and site_mod is None:
            warnings.append(f"Dòng {n}: Site '{site_cell}' không có trong tool — tool tự nhận theo link")
        if site_mod is not None and site_mod is not mod:
            other[site_mod.NAME] = other.get(site_mod.NAME, 0) + 1
            continue
        key, why = check_link(url, mod, sites)
        if why:
            warnings.append(f"Dòng {n}: {why} — bỏ qua")
            continue
        profile = cell(r, "profile").strip()
        raw_country = cell(r, "country")
        country = clean_country(raw_country) or "US"
        if raw_country and not clean_country(raw_country):
            warnings.append(f"Dòng {n}: Action Country '{raw_country}' không hợp lệ (cần 2 chữ cái) — dùng {country}")
        # trùng = cùng link trong CÙNG 1 acc (cùng link cho nhiều acc là bình thường: mỗi acc 1 listing)
        if (profile, key) in seen:
            warnings.append(f"Dòng {n}: link trùng dòng {seen[(profile, key)]['row']} cùng Profile "
                            f"{profile or '(trống)'} — bỏ qua")
            continue
        item = {"url": url, "key": key, "profile": profile, "country": country, "row": n}
        seen[(profile, key)] = item
        items.append(item)
    for name, cnt in other.items():
        warnings.append(f"{cnt} dòng của {name} — không lấy ở tab này, Import file ở tab {name}")
    for p, c in country_conflicts(items).items():
        warnings.append(f"Profile {p or '(trống)'} có nhiều Action Country: {', '.join(c)} — sửa lại trước khi xuất")
    return items, warnings
