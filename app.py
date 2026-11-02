"""Retail -> eBay CSV: server chạy trên máy, giao diện mở trong trình duyệt (hoặc cửa sổ app khi chạy .exe).

Mỗi site là 1 tab. Phần lớn site đọc bằng HTTP (curl_cffi) nên không cần mở Chrome;
site nào cần Chrome (vd Touch of Class) thì tool mở Chrome riêng, có proxy nếu bật.
"""
import base64
import copy
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import ai_title
from browser_support import OwnedBrowserProcesses, driver_pid, profile_is_free
import core
import ebay_api
import sites as site_registry
import tracking
from sites.base import BrowserLaunchError, Fetcher, ProxyPool, SkipProduct, proxy_url, validate

WEB_DIR = core.ASSETS / "web"
SESSION_DIR = core.ROOT / "sessions"   # bảng Kết quả của từng tab, lưu để mở lại tool không mất
SITES = site_registry.load_all()
_BROWSER_LOG_LOCK = threading.Lock()


class Session:
    """Trạng thái riêng của 1 tab (1 site): kết quả, log, lần chạy, file đã xuất."""

    def __init__(self, mod):
        self.mod = mod
        self.kind = mod.KEY
        self.logs = []
        self.results = []
        self.running = False
        self.progress = [0, 0]
        self.run_id = None          # tên thư mục của lần chạy, vd 1012-1015
        self.orig_files = []        # file CSV gốc (lúc chạy xong)
        self.export_folder = ""     # thư mục lần bấm "Xuất CSV" gần nhất
        self.export_files = []      # các file đã xuất lần đó
        self.dirty = False          # đã đổi Profile/Category... sau lần xuất gần nhất
        self.ai_running = False     # đang nhờ AI viết tiêu đề
        self.phase = ""             # việc tab đang làm sau khi chạy xong link (kiểm ảnh 40/182, AI, ghi CSV) -> hiện ở thanh Kiểm tra & sửa
        self.stop = threading.Event()   # nút Dừng của RIÊNG tab này (không dừng nhầm tab khác)
        self.pool = ProxyPool([])       # proxy của lần chạy đang chạy (mỗi site 1 bộ: chạy song song không giẫm nhau)
        self.browser = None             # Chrome riêng của lần chạy (chỉ mở khi site cần, vd Touch of Class)

    @property
    def label(self):
        return self.mod.NAME


# Cài đặt không ảnh hưởng bảng Kết quả (đổi không cần tính lại) / chỉ ảnh hưởng giá
NO_REFRESH = {"upload_images", "export_dir", "image_host", "image_size", "square_image", "delay_seconds",
              "gemini_model", "gemini_api_key", "ai_titles", "ai_title_notes", "ai_prompt", "proxies_enc", "proxy_file",
              "freeimage_api_key", "imgbb_api_key", "max_parallel_sites", "r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket",
              "r2_public_url", "r2_folder", "footer_bullets", "personalize_text", "brand_in_title", "quantity"}
PRICE_KEYS = {"price_percent", "round_99"}
# Các ô refresh_all tính lại (Category, item specifics, cảnh báo, giá) — chỉ ghi lại đúng các ô này
REFRESH_KEYS = ("category_api", "category_auto", "category_src", "aspects", "aspects_cat", "conditions",
                "conditions_cat", "title_warn", "problem", "price_text", "n_colors", "n_sizes", "n_variants", "n_all")
SLIM_SKIP = ("product", "pics", "pics_failed", "aspects", "variant_sel", "filters", "title_base", "title_extra", "conditions",
             "conditions_cat", "category_api", "aspects_cat", "img_info", "uploaded", "size_group")


def is_pending(r):
    """Dòng lỗi do chưa chạy tới (bấm Dừng / site chặn / tắt tool giữa chừng), không phải lỗi của chính link đó."""
    return r.get("status") == "error" and str(r.get("error") or "").startswith(("Chưa chạy", "Đã dừng"))


def slim(r):
    """Dòng kết quả gửi ra bảng mỗi giây (chỉ các ô bảng dùng, không kèm dữ liệu sản phẩm)."""
    out = {k: v for k, v in list(r.items()) if k not in SLIM_SKIP}   # list(): chụp nhanh, luồng khác thêm khoá cũng không lỗi
    prod = r.get("product") or {}
    out["title_add"] = prod.get("title_add") or ""   # chữ loại hàng tool tự ghép vào tiêu đề
    out["pcode"] = prod.get("code", "")             # mã gốc trên site (SKU có thể thêm đuôi màu -BLACK / -ALL)
    if r.get("status") == "ok" and prod:              # thiết bị Wi-Fi / Bluetooth chưa có FCC ID -> eBay Mỹ báo vi phạm, ẩn listing
        out["fcc"] = "missing" if core.fcc_missing(r) else ""
    items = (r.get("product") or {}).get("page_items") or []
    if items:   # trang bộ sưu tập: mã các món khác cùng trang (bảng hiện "Trang có N món")
        out["page_codes"] = [p["code"] for p in items]
    return out


class Engine:
    """Một thread riêng chạy lần lượt các link (Playwright sync chỉ chạy được trên thread đã tạo nó).
    Mọi tab dùng chung thread này nên mỗi lúc chỉ 1 tab được chạy."""

    def __init__(self):
        self.lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.refresh_lock = threading.Lock()   # tính lại bảng (đổi cài đặt): mỗi lúc 1 lượt
        self.jobs = queue.Queue()
        self.sessions = {k: Session(m) for k, m in SITES.items()}
        self.slot_cv = threading.Condition()   # chạy song song: tối đa max_parallel_sites site cùng lúc
        self.active = 0
        self.photo_jobs = {}        # (tab, id dòng) -> tiến trình tải ảnh màu ở cửa sổ Xem
        # Theo dõi sản phẩm: lần kiểm tra giá / tồn kho đang chạy (dùng chung thread chạy link)
        self.track = {"running": False, "progress": [0, 0], "logs": [], "stop": threading.Event(), "current": ""}
        self.load_sessions()
        threading.Thread(target=self._loop, daemon=True).start()

    def session(self, tab):
        return self.sessions.get(tab) or next(iter(self.sessions.values()))

    # ---- lưu bảng Kết quả xuống đĩa: tắt / mở lại tool vẫn còn

    SAVE_KEYS = ("results", "run_id", "orig_files", "export_folder", "export_files", "dirty")

    def save_session(self, s):
        with self.lock:
            data = {k: getattr(s, k) for k in self.SAVE_KEYS}
            # dòng đang chạy dở (tắt tool giữa chừng) -> lưu thành dòng lỗi, không mất dòng / không lệch số thứ tự
            data["results"] = [r if r.get("status") != "running"
                               else {k: r.get(k) for k in ("id", "url", "source", "profile", "country")}
                               | {"status": "error", "error": "Chưa chạy xong — bấm \"Chạy lại dòng lỗi\"", "category": ""}
                               for r in data["results"]]
            data["log_tail"] = s.logs[-1500:]   # để đọc lại lỗi sau khi chạy (không hiện lại trên giao diện)
            text = json.dumps(data, ensure_ascii=False)
        with self.save_lock:   # nhiều luồng cùng lưu 1 tab: lần lượt, file tạm riêng -> không ghi đè / hỏng file
            try:
                SESSION_DIR.mkdir(exist_ok=True)
                tmp = SESSION_DIR / f"{s.kind}.json.{threading.get_ident()}.tmp"
                tmp.write_text(text, encoding="utf-8")
                for attempt in range(5):
                    try:
                        tmp.replace(SESSION_DIR / f"{s.kind}.json")
                        break
                    except PermissionError:
                        time.sleep(0.2 * (attempt + 1))
            except OSError as e:
                print("Không lưu được bảng", s.kind, e)

    def new_id(self, s):
        """Id dòng mới = số lớn nhất đang có + 1 (không trùng kể cả khi có dòng bị bỏ)."""
        nums = [int(r["id"][1:]) for r in s.results if re.fullmatch(r"r\d+", str(r.get("id", "")))]
        return f"r{max(nums, default=0) + 1}"

    def load_sessions(self):
        for s in self.sessions.values():
            data = core.load_json(SESSION_DIR / f"{s.kind}.json", None)
            if not data:
                continue
            for k in self.SAVE_KEYS:
                if k in data:
                    setattr(s, k, data[k])
            if s.results:
                s.progress = [len(s.results), len(s.results)]
                s.logs.append({"t": time.strftime("%H:%M:%S"), "text": f"Đã mở lại kết quả lần chạy trước ({len(s.results)} dòng).", "tag": ""})

    # ---- trạng thái cho giao diện

    def log(self, s, text, tag=""):
        with self.lock:
            s.logs.append({"t": time.strftime("%H:%M:%S"), "text": text, "tag": tag})

    def snapshot(self, tab, since):
        s = self.session(tab)
        with self.lock:
            other = next((o for o in self.sessions.values() if o is not s and o.running), None)
            return {
                "running": s.running,
                "busy_other": "",   # các site chạy song song: tab khác đang chạy không chặn tab này
                "running_tabs": [o.kind for o in self.sessions.values() if o.running],
                "progress": s.progress,
                "logs": s.logs[since:],
                "log_total": len(s.logs),
                "results": [slim(r) for r in s.results],
                "run_id": s.run_id,
                "orig_files": s.orig_files,
                "export_folder": s.export_folder,
                "export_files": s.export_files,
                "dirty": s.dirty,
                "ai_running": s.ai_running,
                "phase": s.phase,
            }

    def retry_errors(self, tab, upload, rid=None, only=None):
        """Nút "↻ Chạy lại dòng lỗi": chạy lại các dòng lỗi ngay tại chỗ, giữ nguyên các dòng đã xong.
        only="pending": nút "▶ Chạy tiếp" — chỉ các dòng chưa chạy tới (đã bấm Dừng / site chặn) và dòng bị dừng giữa chừng;
        only="failed": chỉ các dòng lỗi thật."""
        s = self.session(tab)
        with self.lock:
            if s.running or s.ai_running:
                raise RuntimeError(f"Tab {s.label} đang chạy, chờ xong hoặc bấm Dừng")
            rows = [r for r in s.results if r["status"] == "error" and (rid is None or r["id"] == rid)
                    and (only is None or is_pending(r) == (only == "pending"))]
            if not rows:
                raise RuntimeError("Không có dòng chưa chạy nào" if only == "pending" else "Không có dòng lỗi nào")
            items = [{"url": r["url"], "key": None, "profile": r.get("profile", ""), "country": r.get("country") or "US",
                      "retry_id": r["id"]} for r in rows]
            s.running = True
            s.progress = [len(s.results) - len(items), len(s.results)]
        s.stop.clear()
        self.jobs.put((s, items, upload))
        return len(items)

    def append_links(self, tab, rid, urls, upload):
        """Cửa sổ Xem -> "Thêm vào bảng": chạy thêm các món khác cùng trang thành dòng mới, giữ nguyên các dòng đã có.
        Profile / Country lấy theo dòng đang xem."""
        s = self.session(tab)
        src = self._find(s, rid)
        urls = [u for u in dict.fromkeys(urls) if s.mod.URL_RE.search(u or "")]
        if not urls:
            raise RuntimeError("Chưa chọn món nào")
        with self.lock:
            if s.running or s.ai_running:
                raise RuntimeError(f"Tab {s.label} đang chạy, chờ xong hoặc bấm Dừng")
            items = [{"url": u, "key": None, "profile": src.get("profile", ""), "country": src.get("country") or "US",
                      "append": True} for u in urls]
            s.running = True
            s.progress = [0, len(items)]
        s.stop.clear()
        self.jobs.put((s, items, upload))
        return len(items)

    def submit(self, tab, items, upload, skipped=()):
        """items: [{url, key, profile, country}] (từ ô dán link hoặc file import)."""
        s = self.session(tab)
        with self.lock:
            if s.running:
                raise RuntimeError(f"Tab {s.label} đang chạy, chờ xong hoặc bấm Dừng")
            if s.ai_running:   # AI đang viết tiêu đề cho các dòng cũ: chạy mới sẽ đè id dòng -> tiêu đề lệch
                raise RuntimeError("AI đang viết tiêu đề ở tab này, chờ xong rồi chạy link mới")
            s.running = True
            s.logs.extend({"t": time.strftime("%H:%M:%S"), "text": f"⚠ Bỏ qua: {x}", "tag": "warn"} for x in skipped)
            s.results = []
            s.run_id = None
            s.orig_files, s.export_folder, s.export_files = [], "", []
            s.dirty = False
            s.progress = [0, len(items)]
        s.stop.clear()
        self.jobs.put((s, items, upload))

    def _find(self, s, rid):
        with self.lock:
            res = next((r for r in s.results if r.get("id") == rid and r["status"] == "ok"), None)
        if not res:
            raise RuntimeError("Không tìm thấy listing")
        return res

    def _add_color_pics(self, s, res, cfg):
        """Người dùng tích thêm màu chưa có ảnh -> up bổ sung ảnh của màu đó (hoặc dùng link gốc nếu lúc chạy không up)."""
        prod = res["product"]
        pics = res.setdefault("pics", {"main": [], "colors": {}, "general": []})
        chosen = {v["color"] for v in core.selling_variants(prod, cfg, res) if v["color"]}
        missing = [c for c in chosen if c not in pics.get("colors", {})]
        if not missing:
            return
        plan = core.plan_images(prod, cfg, only_colors=missing)
        plan["main"], plan["general"] = [], []
        if not plan["colors"]:
            return
        if not res.get("uploaded"):
            pics.setdefault("colors", {}).update(plan["colors"])
            return
        proxies = self._img_proxies(s, cfg)
        self.log(s, f"{res['sku']}: up ảnh cho {len(plan['colors'])} màu vừa chọn thêm")
        cache = core.load_json(core.CACHE_PATH, {})
        try:
            new, _, _ = self._upload_pics(s, prod, cfg, cache, plan=plan, proxies=proxies)
            pics.setdefault("colors", {}).update(new["colors"])
        except Exception as e:
            self.log(s, f"  ✗ up ảnh màu mới lỗi: {e}", "err")

    @staticmethod
    def _img_proxies(s, cfg):
        if core.site_cfg(cfg, s.kind).get("use_proxy"):
            px = core.proxies_for(cfg, s.kind)
            if px:
                u = proxy_url(px[0])
                return {"http": u, "https": u}
        return None

    @staticmethod
    def _sync_pic(res, cfg):
        """Ảnh đại diện ở bảng + số ảnh theo các màu đang chọn (giống ảnh sẽ ghi vào CSV)."""
        main, _ = core.listing_pics(res, cfg)
        if main:
            res["pic"], res["n_images"] = main[0], len(main)

    # ---- ảnh theo màu ở cửa sổ Xem: chọn màu chưa có ảnh -> tải + up ảnh màu đó, báo tiến trình từng ảnh

    def _photo_view(self, job, res):
        pics = res.get("pics") or {}
        return {"state": job["state"], "error": job.get("error", ""), "colors": job["colors"],
                "items": [{k: it[k] for k in ("color", "src", "state", "err")} for it in job["items"]],
                "pics": {"colors": pics.get("colors", {}), "failed": res.get("pics_failed", {})}}

    @staticmethod
    def _photo_merge(res, job):
        """Đưa ảnh đã up xong vào listing: ảnh nào lỗi thì ghi nhớ để bấm Thử lại đúng các ảnh đó."""
        pics = res.setdefault("pics", {"main": [], "colors": {}, "general": []})
        failed = res.setdefault("pics_failed", {})
        for c in job["colors"]:
            its = [it for it in job["items"] if it["color"] == c]
            ok = [it["url"] for it in its if it["state"] == "ok"]
            bad = [it["src"] for it in its if it["state"] != "ok"]
            if ok:
                pics.setdefault("colors", {})[c] = ok
            if bad:
                failed[c] = bad
            else:
                failed.pop(c, None)

    def photos_start(self, tab, rid, colors):
        s = self.session(tab)
        res = self._find(s, rid)
        cfg = core.load_config()
        key = (s.kind, rid)
        with self.lock:
            job = self.photo_jobs.get(key)
            if job and job["state"] == "running":
                return self._photo_view(job, res)
        prod = res["product"]
        known = {c["name"] for c in prod.get("colors", [])}
        plan = core.plan_images(prod, cfg, only_colors=[c for c in dict.fromkeys(colors) if c in known])
        with self.lock:   # "Thử lại": ảnh đã up xong ở lượt trước giữ nguyên, chỉ tải lại ảnh lỗi / chưa tải
            prev = self.photo_jobs.get(key)
        got = {it["src"]: it["url"] for it in (prev["items"] if prev else []) if it["state"] == "ok"}
        job = {"state": "running", "colors": list(plan["colors"]), "cancel": threading.Event(),
               "items": [{"color": c, "src": u, "state": "ok" if u in got else "wait", "url": got.get(u, ""), "err": ""}
                         for c, imgs in plan["colors"].items() for u in imgs]}
        with self.lock:
            self.photo_jobs[key] = job
        if not job["items"]:
            job["state"] = "done"
        elif not res.get("uploaded"):   # lúc chạy không up ảnh: dùng thẳng link ảnh gốc, không có gì để tải
            for it in job["items"]:
                it.update(state="ok", url=it["src"])
            with self.lock:
                self._photo_merge(res, job)
            job["state"] = "done"
        else:
            threading.Thread(target=self._photo_worker, args=(s, res, cfg, job), daemon=True).start()
        return self._photo_view(job, res)

    def _photo_worker(self, s, res, cfg, job):
        referer = res["product"]["url"]
        todo = queue.Queue()
        for it in job["items"]:
            if it["state"] != "ok":
                todo.put(it)
        try:
            proxies = self._img_proxies(s, cfg)
            base = core.load_json(core.CACHE_PATH, {})

            def work():
                cache = dict(base)   # mỗi luồng 1 bản riêng (rehost_image tự ghi thêm vào file chung)
                while True:
                    try:
                        it = todo.get_nowait()
                    except queue.Empty:
                        return
                    if job["cancel"].is_set():
                        it["state"] = "skip"
                        continue
                    it["state"] = "run"
                    try:
                        it["url"] = core.rehost_image(it["src"], cfg, cache, proxies=proxies, referer=referer)[0]
                        it["state"] = "ok"
                    except Exception as e:
                        it.update(state="fail", err=str(e)[:160])

            workers = [threading.Thread(target=work, daemon=True) for _ in range(2)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()
        except Exception as e:
            job["error"] = str(e)[:200]
            for it in job["items"]:
                if it["state"] in ("wait", "run"):
                    it["state"] = "fail"
                    it["err"] = job["error"]
        finally:
            with self.lock:
                self._photo_merge(res, job)
                s.dirty = True
            job["state"] = "cancelled" if job["cancel"].is_set() else "done"
            n_fail = sum(it["state"] == "fail" for it in job["items"])
            if n_fail:
                self.log(s, f"{res['sku']}: tải ảnh màu xong, {n_fail} ảnh lỗi", "warn")
            self.save_session(s)

    def photos_status(self, tab, rid):
        s = self.session(tab)
        res = self._find(s, rid)
        job = self.photo_jobs.get((s.kind, rid))
        return self._photo_view(job, res) if job else {"state": "none", "items": [], "colors": [],
                                                       "pics": {"colors": (res.get("pics") or {}).get("colors", {}),
                                                                "failed": res.get("pics_failed", {})}}

    def photos_cancel(self, tab, rid):
        job = self.photo_jobs.get((self.session(tab).kind, rid))
        if job and job["state"] == "running":
            job["cancel"].set()
        return self.photos_status(tab, rid)

    @staticmethod
    def _apply_filters(res, flt):
        """Bộ lọc giống trang gốc (nhóm size + màu). Chọn đúng 1 màu / 1 nhóm size -> thêm vào cuối tiêu đề,
        size ghi gọn (bỏ chữ nhóm). Chọn nhiều hoặc tất cả -> tiêu đề giữ nguyên."""
        prod = res["product"]
        all_colors = [c for c in dict.fromkeys(v["color"] for v in prod["variants"]) if c]
        all_groups = core.size_groups(prod)
        colors = [c for c in flt.get("colors", all_colors) if c in all_colors]
        groups = [g for g in flt.get("groups", all_groups) if g in all_groups]
        one_color = colors[0] if len(colors) == 1 else ""   # đúng 1 màu (kể cả sản phẩm chỉ có 1 màu) -> vào tiêu đề
        one_group = groups[0] if len(all_groups) > 1 and len(groups) == 1 else ""
        res["filters"] = {"colors": colors, "groups": groups}
        res["size_group"] = one_group
        base = res.get("title_base") or res["title"]
        res["title_base"] = base
        color_txt = core.nice_color(one_color)
        # tên sản phẩm đã có tên màu (nguyên từ: "Red" không tính trong "Tailored") -> không thêm lần nữa
        if color_txt and re.search(rf"(?i)\b{re.escape(color_txt)}\b", base):
            color_txt = ""
        if one_group and re.search(rf"(?i)\b{re.escape(one_group)}\b", base):   # tiêu đề gõ tay đã có nhóm size
            one_group_txt = ""
        elif one_group.lower() == "regular":   # size thường: không cần ghi "Regular" vào tiêu đề
            one_group_txt = ""
        else:
            one_group_txt = one_group
        extra = ", ".join(x for x in (color_txt, one_group_txt) if x)
        res["title_extra"] = extra
        res["title"] = f"{base}, {extra}" if extra else base   # vd "... Crop Pants, Black, Misses"

    @staticmethod
    def _set_manual_title(res, text):
        """Tiêu đề gõ tay thành phần gốc. Nếu vẫn còn đuôi ", Màu, Nhóm size" tool ghép thì tách ra, để lần sau đổi
        màu / nhóm size đuôi đổi theo (không thành "…, Black, Misses, Indigo")."""
        text = str(text).strip()
        extra = res.get("title_extra") or ""
        if extra and text.endswith(", " + extra):
            res["title_base"] = text[: -(len(extra) + 2)]
        else:
            res["title_base"], res["title_extra"] = text, ""
        res["title"] = text

    def _preselect_from_url(self, res, cfg, log=None):
        """Link gốc đã chọn sẵn màu / nhóm size -> mặc định chọn đúng như vậy (size = các size còn hàng)."""
        prod = res["product"]
        flt = core.url_filters(res["url"], prod)
        if not flt:
            Engine._one_length(res, log)
            return False
        colors = [c for c in dict.fromkeys(v["color"] for v in prod["variants"]) if c]
        groups = core.size_groups(prod)
        want_c = set(flt.get("colors", colors))
        want_g = set(flt.get("groups", groups))
        want_s = set(flt.get("sizes") or [])
        res["variant_sel"] = [core.vkey(v) for v in prod["variants"] if v.get("available")
                              and (not want_s or v["size"] in want_s)
                              and (not colors or v["color"] in want_c)
                              and (not groups or core.split_size(v["size"], prod["site"])[1] in want_g)]
        self._apply_filters(res, {"colors": list(want_c) if colors else [], "groups": list(want_g) if groups else []})
        if log:
            log(f"  theo link gốc / mặc định của site: chọn sẵn "
                f"{', '.join(flt.get('colors', []) + flt.get('groups', []) + flt.get('sizes', []))} "
                f"→ {len(res['variant_sel'])} size còn hàng")
        Engine._one_length(res, log)
        return True

    @staticmethod
    def _one_length(res, log=None):
        """Quần W x L: chỉ giữ 1 chiều dài (32L, hết thì 30L, rồi 34L) — đỡ tốn limit eBay."""
        prod = res["product"]
        sel = res.get("variant_sel")
        if sel is None:
            sel = core.default_selection(prod, {})
        kept = core.pick_length(prod, sel)
        if len(kept) != len(sel):
            res["variant_sel"] = kept
            if log:
                length = core.waist_inseam(next(v["size"] for v in prod["variants"] if core.vkey(v) == kept[0]))[1]
                log(f"  quần: chỉ lấy chiều dài {length}L → {len(kept)} size (bỏ {len(sel) - len(kept)} size dài khác)")
            sel = kept
        ranged = core.pick_size_range(prod, sel)   # quần size số: mặc định chỉ size 4–18
        if len(ranged) != len(sel):
            res["variant_sel"] = ranged
            if log:
                lo, hi = core.SIZE_RANGE[prod["site"]]
                log(f"  quần: chỉ lấy size {lo}–{hi} → {len(ranged)} size (bỏ {len(sel) - len(ranged)} size nhỏ / lớn hơn, "
                    f"vẫn tích thêm được ở cửa sổ Xem)")

    def update_rows(self, tab, ids, changes):
        """Sửa tay 1 hoặc nhiều listing: Profile (SKU đổi theo), Action Country, Category."""
        s = self.session(tab)
        rows = [self._find(s, rid) for rid in ids]
        if "country" in changes:
            country = core.clean_country(changes["country"])
            if not country:
                raise RuntimeError("Action Country phải là 2 chữ cái, vd US, JP, LK")
        if "category" in changes and changes["category"].strip() and not changes["category"].strip().isdigit():
            raise RuntimeError("Category ID chỉ gồm chữ số, vd 63861")
        cfg = core.load_config()
        with self.lock:
            for res in rows:
                if "profile" in changes:
                    res["profile"] = str(changes["profile"]).strip()
                    res["sku"] = core.make_sku(cfg, s.kind, res["code"], res["profile"])
                if "country" in changes:
                    res["country"] = country
                if "category" in changes:
                    res["category"] = str(changes["category"]).strip()
                if "specifics" in changes:
                    res["specifics"] = str(changes["specifics"]).strip()
                if "variant_sel" in changes:
                    valid = {core.vkey(v) for v in res["product"]["variants"]}
                    res["variant_sel"] = [k for k in changes["variant_sel"] if k in valid]
                if (changes.get("title") or "").strip():
                    # tiêu đề gõ ở bảng (chưa xuất): thành phần gốc, đổi màu / nhóm size thì ghép thêm vào sau
                    self._set_manual_title(res, changes["title"])
                if "filters" in changes:
                    self._apply_filters(res, changes["filters"] or {})
            s.dirty = True
        for res in rows:   # có thể gọi eBay API / up ảnh -> làm ngoài lock
            if "variant_sel" in changes:
                self._add_color_pics(s, res, cfg)
                res.update(price_info(res["product"], cfg, res))
                self._sync_pic(res, cfg)
            self._refresh_category(res, cfg)
        self.save_session(s)
        return [slim(r) for r in rows]

    def _refresh_category(self, res, cfg, log=None):
        """Tính lại Category đang dùng + lấy item specifics của Category đó (eBay API, có cache) + kiểm tra thiếu."""
        if res.get("category_options"):
            res["category_api"] = core.best_suggestion(res["category_options"], res["product"].get("department"),
                                                       res["product"].get("title_add", ""), res["product"].get("title", ""),
                                                       toy=core.is_toy(res["product"]))
        cat, _, src = core.category_for(res["product"], cfg, res.get("category", ""), res.get("category_api"))
        res["category_auto"] = cat
        res["category_src"] = src
        if cat and ebay_api.configured(cfg) and res.get("aspects_cat") != cat:
            try:
                res["aspects"] = ebay_api.aspects(cfg, cat)
                res["aspects_cat"] = cat
            except Exception as e:
                res["aspects"], res["aspects_cat"] = None, ""
                if log:
                    log(f"  ⚠ không lấy được item specifics của Category {cat}: {e}", "warn")
        # Hàng tân trang: hỏi eBay Category này cho những tình trạng nào (Seller refurbished hay chỉ Used)
        if cat and core.is_refurb(res["product"]) and ebay_api.configured(cfg) and res.get("conditions_cat") != cat:
            try:
                res["conditions"], res["conditions_cat"] = ebay_api.conditions(cfg, cat), cat
            except Exception as e:
                res["conditions"], res["conditions_cat"] = {}, ""
                if log:
                    log(f"  ⚠ không lấy được danh sách Condition của Category {cat}: {e}", "warn")
        res["title_warn"] = core.title_warn(res)
        try:
            core.listing_rows(res, cfg)
            res["problem"] = ""
        except Exception as e:
            res["problem"] = str(e)

    def _suggest_category(self, res, cfg, log):
        """Hỏi eBay Category hợp với sản phẩm (theo tên + Department + nhóm hàng trên site)."""
        if not (cfg.get("auto_category", True) and ebay_api.configured(cfg)):
            return ""
        q = ebay_api.query_for(res["product"])
        try:
            opts = ebay_api.suggest(cfg, q)
        except Exception as e:
            log(f"  ⚠ eBay gợi ý Category lỗi: {e} — dòng này để trống Category, bấm \"Hỏi lại\" ở bảng Kết quả sau", "warn")
            return str(e)
        res["category_options"] = opts[:10]   # _refresh_category chọn trong danh sách này (giữ đủ để tính lại sau)
        if not opts:
            log(f"  ⚠ eBay không gợi ý được Category cho \"{q}\"", "warn")
        return ""

    def resuggest(self, tab, ids=None):
        """Hỏi lại eBay Category cho các dòng thiếu (ids=None) hoặc các dòng được chọn — dùng khi lần chạy bị eBay
        giới hạn lượt gọi. Gặp lỗi (hết hạn mức, mất mạng) thì dừng ngay, khỏi tốn thêm lượt."""
        s = self.session(tab)
        if s.running:
            raise RuntimeError("Tab đang chạy, chờ chạy xong rồi hỏi lại Category")
        cfg = core.load_config()
        if not ebay_api.configured(cfg):
            raise RuntimeError("Chưa có key eBay API — điền Client ID / Client secret trong Cài đặt")
        ebay_api.reset_limit()
        want = set(ids) if ids else None
        with self.lock:
            rows = [r for r in s.results if r["status"] == "ok"
                    and ((r["id"] in want) if want else not (r.get("category") or r.get("category_auto")))]
        fixed, err = 0, ""
        for r in rows:
            err = self._suggest_category(r, cfg, lambda *a, **k: None)
            if err:
                break
            self._refresh_category(r, cfg)
            fixed += bool(r.get("category") or r.get("category_auto"))
        with self.lock:
            s.dirty = True
            left = sum(1 for r in s.results if r["status"] == "ok" and not (r.get("category") or r.get("category_auto")))
        self.save_session(s)
        return {"asked": len(rows), "fixed": fixed, "left": left, "error": err}

    def refresh_all(self, tab, prices_only=False):
        """Cài đặt (quy tắc Category, % giá...) vừa đổi -> tính lại cột Category / giá cho bảng Kết quả.
        prices_only: chỉ đổi % giá -> chỉ tính lại giá (nhanh, không tính lại Category / item specifics)."""
        s = self.session(tab)
        with self.refresh_lock:
            cfg = core.load_config()
            with self.lock:
                rows = [r for r in s.results if r["status"] == "ok"]
            for r in rows:
                tmp = dict(r)   # tính trên bản sao, chỉ ghi lại đúng các ô vừa tính
                if not prices_only:
                    self._refresh_category(tmp, cfg)
                tmp.update(price_info(tmp["product"], cfg, tmp))
                with self.lock:
                    # không ghi đè cả dòng: tiêu đề AI / sửa tay, Profile, biến thể... đổi trong lúc tính vẫn giữ
                    r.update({k: tmp[k] for k in REFRESH_KEYS if k in tmp})
        if rows:
            with self.lock:
                s.dirty = True   # giá / Category có thể đã đổi so với file đã xuất
            self.save_session(s)

    def _ai_titles(self, s, rows, cfg):
        """Viết lại tiêu đề các dòng bằng Gemini; giữ tiêu đề gốc để khôi phục được."""
        def log(text, tag=""):
            self.log(s, text, tag)

        log(f"AI (Gemini) đang viết tiêu đề cho {len(rows)} listing…")
        new = ai_title.write_titles(rows, cfg, log, s.stop.is_set)
        with self.lock:
            for r in s.results:
                if r.get("id") in new:
                    r.setdefault("title_orig", r["title"])
                    r["title"] = new[r["id"]]
                    r["title_src"] = "ai"
                    # Phần AI viết = phần gốc; ", Màu, Nhóm size" giữ nguyên ở cuối (đổi lựa chọn sau vẫn ghép đúng)
                    extra = r.get("title_extra", "")
                    r["title_base"] = r["title"][: -(len(extra) + 2)] if extra and r["title"].endswith(", " + extra) else r["title"]
            s.dirty = s.dirty or bool(new)
        for r in rows:
            if r["id"] in new:
                log(f"  {r['sku']}: {new[r['id']]}", "ok")
        return len(new)

    def ai_rewrite(self, tab, ids, restore=False):
        """Sửa tiêu đề dòng đã chọn; không chọn thì sửa các tiêu đề đang cảnh báo."""
        s = self.session(tab)
        with self.lock:
            if s.running or s.ai_running:
                raise RuntimeError(f"Tab {s.label} đang bận ({s.phase or ('AI viết tiêu đề' if s.ai_running else 'chạy link')})"
                                   " — chờ xong rồi bấm lại")
            rows = [r for r in s.results if r["status"] == "ok" and (not ids or r["id"] in ids)]
            if not ids and not restore:
                lim = int(core.load_config().get("title_max_len", 80) or 80)
                rows = [r for r in rows if len(r.get("title") or "") > lim or r.get("title_warn")]
                if not rows:
                    raise RuntimeError("Không có tiêu đề đang cảnh báo cần sửa — muốn AI viết lại dòng khác thì tích ô "
                                       "đầu dòng rồi bấm lại")
            if not rows:
                raise RuntimeError("Chưa có listing nào")
            if restore:
                cfg0 = core.load_config()
                for r in rows:
                    if r.get("title_orig") or r.get("title_src") == "ai":
                        r.pop("title_orig", None)
                        r["title_src"] = ""
                        # Dựng lại từ tên trên site + ", Màu, Nhóm size" đang chọn
                        r["title_base"] = core.make_title(r["product"], cfg0, 10_000)
                        r["title"] = r["title_base"]
                        if r.get("filters"):
                            self._apply_filters(r, r["filters"])
                s.dirty = True
            else:
                s.ai_running = True
        if restore:
            self.save_session(s)
            return len(rows)
        cfg = core.load_config()
        if not ai_title.configured(cfg):
            s.ai_running = False
            raise RuntimeError("Chưa có Gemini API key — lấy miễn phí ở aistudio.google.com rồi dán vào Cài đặt")

        def work():
            s.phase = f"AI đang viết tiêu đề {len(rows)} listing"
            try:
                self._ai_titles(s, rows, cfg)
            except Exception as e:
                self.log(s, f"✗ AI viết tiêu đề lỗi: {e}", "err")
            finally:
                s.ai_running = False
                s.phase = ""
                self.save_session(s)

        s.stop.clear()   # nút Dừng riêng từng tab: xoá lệnh Dừng cũ của tab này
        threading.Thread(target=work, daemon=True).start()
        return len(rows)

    def _check_ready(self, s):
        with self.lock:
            if s.running:
                raise RuntimeError("Đang chạy link, chờ xong rồi bấm lại")
            if not s.run_id:
                raise RuntimeError("Chưa có lần chạy nào")
            items = [dict(r) for r in s.results if r["status"] == "ok"]
        if not items:
            raise RuntimeError("Chưa có sản phẩm nào")
        return items

    def export(self, tab, titles, folder, ids=None):
        """Nút "Xuất CSV": xuất listing của lần chạy vào thư mục người dùng chọn. Mỗi Profile 1 file.
        ids: chỉ xuất các dòng đã tích (vd xuất lại riêng 1 listing lỗi ảnh); trống = mọi listing."""
        s = self.session(tab)
        items = self._check_ready(s)
        part = bool(ids)
        if part:
            items = [it for it in items if it["id"] in set(ids)]
            if not items:
                raise RuntimeError("Các dòng đã tích không còn listing nào xuất được")
            titles = {k: v for k, v in titles.items() if k in set(ids)}   # tiêu đề đang sửa của dòng khác: chưa xuất, giữ nguyên
        self.log(s, f"Đang xuất CSV cho {len(items)} dòng" + (" đã tích" if part else " (tất cả)") + "…")
        cfg = core.load_config()
        for it in items:
            if (titles.get(it["id"]) or "").strip():
                it["title"] = titles[it["id"]].strip()
        try:
            paths, errors = core.export_run(items, cfg, s.run_id, s.kind, folder=folder, log=lambda t: self.log(s, t),
                                            step=lambda t: setattr(s, "phase", f"Xuất CSV {len(items)} dòng: {t}"))
        finally:
            s.phase = ""
        if paths:   # listing bị bỏ vì trùng không ghi đè thông tin theo dõi của listing cũ; SKU đổi đuôi màu thì theo SKU mới
            self._track_exported([it for it in items if not it.get("_dup")], cfg, s.kind, s.run_id)
        with self.lock:
            for r in s.results:
                if (titles.get(r.get("id")) or "").strip():
                    # tiêu đề gõ tay thành phần gốc -> lần sau đổi màu / nhóm size không bị dựng lại từ tên site
                    self._set_manual_title(r, titles[r["id"]])
            if not part:   # xuất riêng vài dòng: không phải bộ đầy đủ -> giữ thông tin lần xuất đủ + nhắc "chưa xuất"
                s.export_folder = str(folder)
                s.export_files = [p.name for p in paths]
                s.dirty = False
        self.save_session(s)
        self.log(s, f"Đã xuất CSV {len(items)} dòng" + (" đã tích" if part else "") + f" ({len(paths)} file) vào {folder}: "
                 + ", ".join(p.name for p in paths), "ok")
        for e in errors:
            self.log(s, f"  ⚠ {e}" if core.export_note(e) else f"  ✗ Không xuất: {e}",
                     "warn" if core.export_note(e) else "err")
        return [p.name for p in paths], errors

    def preview(self, tab, rid, title=None):
        """Dữ liệu xem trước 1 listing, tính từ đúng các dòng CSV sẽ xuất."""
        s = self.session(tab)
        res = dict(self._find(s, rid))
        if title:
            res["title"] = title
        cfg = core.load_config()
        prod = res["product"]
        out = {"sku": res["sku"], "title": res["title"], "url": res["url"], "profile": res["profile"],
               "country": res["country"], "code": res["code"], "pics": res.get("pics") or {},
               "department": prod.get("department", ""), "breadcrumbs": prod.get("breadcrumbs", []),
               "personalized": prod.get("personalized", False),
               "personalize_fields": prod.get("personalize_fields", []),
               "percent": core.site_percent(cfg, s.kind), "error": "",
               "manual_specifics": res.get("specifics", "")}
        # Item specifics: tính cả khi listing còn thiếu (để hiện ra cho người dùng điền)
        cat, extra, _ = core.category_for(prod, cfg, res.get("category", ""), res.get("category_api"))
        vs = core.selling_variants(prod, cfg, res)
        sel = res.get("variant_sel")
        picked = set(sel) if sel is not None else set(core.default_selection(prod, cfg))
        pct, rnd = core.site_percent(cfg, s.kind), cfg.get("round_99", True)
        out["all_variants"] = [{"key": core.vkey(v), "color": v["color"], "size": v["size"],
                                "src_price": v["price"], "src_list": v.get("list_price"),
                                "price": f"{core.ebay_price(core.base_price(v, prod['site']), pct, rnd):.2f}",
                                "available": bool(v.get("available")), "selected": core.vkey(v) in picked}
                               for v in prod["variants"]]
        out["qty"] = int(cfg.get("quantity", 1))
        # Bộ lọc giống trang gốc: nhóm size + màu (mặc định chọn hết)
        groups = core.size_groups(prod)
        colors = [c for c in dict.fromkeys(v["color"] for v in prod["variants"]) if c]
        for v in out["all_variants"]:
            v["base"], v["group"] = core.split_size(v["size"], prod["site"])
        flt = res.get("filters") or {}
        out["groups"], out["colors"] = groups, colors
        out["filters"] = {"colors": flt.get("colors", colors), "groups": flt.get("groups", groups)}
        out["title_base"] = res.get("title_base") or res["title"]
        # Ảnh: ảnh chính dựng lại theo các màu ĐANG chọn (giống CSV), không phải ảnh của màu trong link gốc
        pics = dict(res.get("pics") or {})
        try:
            pics["main"] = core.listing_pics(res, cfg)[0] or pics.get("main") or []
        except Exception:
            pass
        out["pics"] = pics
        out["pics_failed"] = res.get("pics_failed", {})
        out["color_srcs"] = core.plan_images(prod, cfg, only_colors=colors)["colors"]   # ảnh gốc từng màu (hiện lúc đang tải)
        out["uploaded"] = bool(res.get("uploaded"))
        job = self.photo_jobs.get((s.kind, rid))
        out["photo_job"] = self._photo_view(job, res) if job and job["state"] == "running" else None
        # Trang bộ sưu tập: các món khác cùng trang (mỗi món 1 listing riêng) + món nào đã có trong bảng.
        # So theo mã gốc của site (SKU có thể có đuôi -BLACK / -ALL); cùng quy tắc với cột Biến thể ở bảng
        with self.lock:
            have = {(r.get("product") or {}).get("code") for r in s.results if r["status"] == "ok"}
        out["page_items"] = [dict(p, in_table=p["code"] in have) for p in prod.get("page_items") or []]
        for p in out["page_items"]:
            p["ebay_price"] = core.ebay_price(p["price"], pct, rnd) if p.get("price") else None
        aspects = res.get("aspects") if res.get("aspects_cat") == cat else None
        # giống listing_rows: chỉ 1 màu thì Color là item specific, không phải trục biến thể
        one_colors = [c for c in dict.fromkeys(core.clean_value(v["color"]) for v in vs) if c]
        n_colors = len(one_colors)
        if n_colors == 1:   # giống listing_rows: 1 màu -> item specific Color = màu đó
            extra = dict(extra, Color=one_colors[0])
        spec, missing, cax, sax = core.build_specifics(res, cfg, cat, extra, aspects,
                                                       n_colors > 1, any(v["size"] for v in vs))
        out["specifics"], out["missing"], out["category"] = spec, missing, cat
        out["axes"] = [cax, sax]
        out["aspects"] = [{"name": a["name"], "required": a["required"], "mode": a["mode"],
                           "values": a["values"][:80], "variation": a["variation"]}
                          for a in (aspects or []) if a["required"] or a.get("usage") == "RECOMMENDED"][:40]
        # Ô FCC ID: hiện khi sản phẩm có dấu hiệu phát sóng (bắt buộc với eBay Mỹ) hoặc Category có sẵn ô này (điền nếu muốn)
        out["fcc_need"] = core.fcc_needed(prod)
        out["fcc_aspect"] = any(a["name"].lower() == core.FCC_NAME.lower() for a in aspects or [])
        try:
            rows = core.listing_rows(res, cfg)
        except Exception as e:
            out["error"] = str(e)
            rows = []
        out["description"] = core.build_description(res, cfg)
        try:
            cond_id, cond_note = core.condition_for(res, cfg)
            out["condition"] = core.CONDITION_NAMES.get(str(cond_id), str(cond_id))
            out["condition_note"] = cond_note
        except RuntimeError as e:   # Category không cho hàng tân trang
            out["condition"], out["condition_note"] = "—", str(e)
        if rows:
            parent = rows[0]
            # thông số listing_rows tự ghi thêm (vd quần chốt 1 chiều dài -> Inseam 32 in) cũng hiện ở cửa sổ Xem
            for col, val in parent.items():
                name = col.lstrip("*")[2:] if col.lstrip("*").startswith("C:") else ""
                if name and val and name not in out["specifics"] and name not in (cax, sax):
                    out["specifics"][name] = val
            out["variants"] = []
            for r in rows[1:] or rows:
                d = dict(x.split("=", 1) for x in (r.get("RelationshipDetails") or "").split("|") if "=" in x)
                src_price, src_list = r.get("_src") or (None, None)   # giá site của đúng biến thể (listing_rows gắn)
                size = d.get(sax, parent.get(core.spec_col(sax), ""))
                more = [f"{k} {v}" for k, v in d.items() if k not in (cax, sax)]   # trục thêm, vd Inseam 30 in
                out["variants"].append({"color": d.get(cax, parent.get(core.spec_col(cax), "")),
                                        "size": " · ".join([size] + more) if more else size,
                                        "price": r.get("*StartPrice"), "qty": r.get("*Quantity"),
                                        "sku": r.get("CustomLabel"), "src_price": src_price, "src_list": src_list})
        else:
            # listing còn lỗi (thiếu item specifics...): vẫn hiện giá / màu / size theo các biến thể đang chọn
            out["variants"] = [{"color": v["color"], "size": v["size"], "price": f"{core.ebay_price(core.base_price(v, prod['site']), pct, rnd):.2f}",
                                "qty": out["qty"] if v.get("available") else 0, "sku": "",
                                "src_price": v["price"], "src_list": v.get("list_price")} for v in vs]
        return out

    # ---- worker

    def _loop(self):
        """Nhận lần chạy -> mỗi lần 1 luồng riêng (các site chạy song song, tối đa max_parallel_sites)."""
        while True:
            job = self.jobs.get()
            if job is None:   # tắt tool
                return
            threading.Thread(target=self._job, args=job, daemon=True).start()

    def _max_parallel(self):
        try:
            return max(1, min(6, int(core.load_config().get("max_parallel_sites", 3))))
        except (TypeError, ValueError):
            return 3

    def _job(self, s, items, upload):
        with self.slot_cv:
            told = False
            while self.active >= self._max_parallel() and not s.stop.is_set():
                if not told:
                    self.log(s, f"Đang có {self.active} site chạy cùng lúc (tối đa {self._max_parallel()}) — chờ tới lượt…", "warn")
                    told = True
                self.slot_cv.wait(2)
            self.active += 1
        prof = core.PROFILE_DIR if s.kind == "touchofclass" else core.PROFILE_DIR.with_name(f"{core.PROFILE_DIR.name}-{s.kind}")
        s.browser = _Browser(lambda text, tag="": self.log(s, text, tag), prof, camoufox=(s.kind == "bestbuy"))
        try:
            self._run(s, items, upload)
        except Exception as e:
            self.log(s, f"Lỗi nghiêm trọng: {e!r}", "err")
            traceback.print_exc()
        finally:
            s.browser.close()
            s.browser = None
            s.phase = ""
            with self.lock:
                s.running = False
            s.stop.clear()   # lệnh Dừng chỉ cho lần chạy này (không làm hỏng lần up ảnh màu mới sau đó)
            self.save_session(s)
            with self.slot_cv:
                self.active -= 1
                self.slot_cv.notify_all()

    # ---- Theo dõi sản phẩm

    def _tlog(self, text, tag=""):
        with self.lock:
            self.track["logs"].append({"t": time.strftime("%H:%M:%S"), "text": text, "tag": tag})
            del self.track["logs"][:-800]

    def track_start(self, skus=None, resume=False):
        """skus: đúng thứ tự trên bảng (chạy từ trên xuống). resume: chạy tiếp lượt dở (kể cả sau khi tắt / bật tool)."""
        data = tracking.load()
        if resume:
            todo = tracking.run_left()
            info = tracking.run_info() or {"total": len(todo), "done": 0}
            if not todo:
                tracking.run_clear()
                raise RuntimeError("Lượt kiểm tra trước đã chạy xong")
        else:
            todo = [k for k in (skus or list(data["items"])) if k in data["items"]]
            info = {"total": len(todo), "done": 0}
        if not todo:
            raise RuntimeError("Chưa có sản phẩm nào trong danh sách theo dõi")
        with self.lock:
            if self.track["running"]:
                raise RuntimeError("Đang kiểm tra rồi")
            self.track.update(running=True, progress=[info["done"], info["total"]], logs=[], current="")
        if not resume:
            tracking.run_start(todo)
        self.track["stop"].clear()
        # Luồng riêng (chạy song song với việc chạy link ở các tab), Chrome riêng nếu site cần trình duyệt
        threading.Thread(target=self._track_thread, args=(todo,), daemon=True).start()
        return len(todo)

    def _track_thread(self, skus):
        browser = _Browser(self._tlog, core.PROFILE_DIR.with_name(core.PROFILE_DIR.name + "-theo-doi"))
        try:
            self._track_run(skus, browser)
        except Exception as e:
            self._tlog(f"Lỗi nghiêm trọng: {e!r}", "err")
            traceback.print_exc()
        finally:
            browser.close()
            self.track["running"] = False
            self.track["current"] = ""

    def track_state(self, since=0):
        with self.lock:
            st = {"running": self.track["running"], "progress": self.track["progress"], "current": self.track["current"],
                  "logs": self.track["logs"][since:], "log_total": len(self.track["logs"])}
        st["resume"] = None if st["running"] else tracking.run_info()   # lượt dở -> nút "Chạy tiếp"
        return st

    def _track_run(self, skus, browser):
        """Đọc lại link của các listing đang theo dõi (cùng link nhiều acc thì đọc 1 lần), so với lúc list."""
        cfg = core.load_config()
        pool = ProxyPool(core.proxies_for(cfg))   # proxy riêng của lượt kiểm tra (không đụng lần chạy link đang chạy)
        data = tracking.load()
        items = [data["items"][k] for k in skus if k in data["items"]]
        groups = {}
        for it in items:   # đúng thứ tự trên bảng; dòng chưa có link để cuối (có thể được dòng list cũ bổ sung link trước đó)
            groups.setdefault((it["site"], it["url"]), []).append(it["sku"])
        groups = dict(sorted(groups.items(), key=lambda kv: not kv[0][1]))
        self._tlog(f"=== Kiểm tra {len(items)} listing ({len(groups)} link khác nhau) ===")
        fetchers = {}
        done, total = self.track["progress"]   # chạy tiếp lượt dở: đếm tiếp từ số đã chạy
        idx = [None]   # dữ liệu lúc list trên máy (chỉ đọc khi có dòng list cũ cần tìm)
        for gi, ((site, url), group) in enumerate(groups.items(), 1):
            stop = self.track["stop"]
            if stop.is_set():
                self._tlog("Đã dừng theo yêu cầu.", "warn")
                break
            s = self.sessions.get(site)
            self.track["current"] = url
            self._tlog(f"[{gi}/{len(groups)}] {url}")
            prod, err = None, ""
            if not url:
                err = "chưa có link gốc — nạp file list có link của listing này"
            elif s is None:
                err = f"site {site} không còn trong tool"
            else:
                if site not in fetchers:
                    sc = core.site_cfg(cfg, site)
                    # site có Proxy-<site>.txt riêng -> bộ proxy riêng cho site đó; không có thì dùng bộ chung của lượt kiểm tra
                    site_pool = ProxyPool(core.proxies_for(cfg, site)) if core.site_proxy_file(cfg, site) else pool
                    fetchers[site] = Fetcher(use_proxy=bool(sc.get("use_proxy")), pool=site_pool,
                                             page_factory=lambda proxy, site=site: browser.page(proxy, camoufox=(site == "bestbuy")),
                                             log=self._tlog, should_stop=stop.is_set)
                try:
                    prod = validate(s.mod.scrape(fetchers[site], url))
                except core.Stopped:
                    self._tlog("Đã dừng theo yêu cầu.", "warn")
                    break
                except Exception as e:
                    err = str(e) or repr(e)
            when = time.strftime("%Y-%m-%d %H:%M")

            def fn(d, group=group, prod=prod, err=err, when=when):
                counts = {}
                for k in group:
                    it = d["items"].get(k)
                    if not it:
                        continue
                    if prod is None:
                        it["check"] = {"time": when, "error": err, "vars": {}}
                        continue
                    if it.get("resolve"):   # list cũ: đọc link xong mới biết mã / màu thật -> tìm dữ liệu lúc list
                        if idx[0] is None:
                            idx[0] = tracking.DataIndex()
                        it = tracking.resolve(it, prod, cfg, idx[0], s.mod)
                        d["items"].pop(k, None)
                        ex = d["items"].get(it["sku"])
                        if ex is None:
                            d["items"][it["sku"]] = it
                        elif not ex.get("url"):   # dòng cùng SKU nạp từ file CSV (chưa có link): bổ sung link vào đó
                            it = d["items"][it["sku"]] = tracking._merge(ex, dict(it, profile=ex.get("profile") or it["profile"]))
                        else:   # SKU đã theo dõi từ dòng khác -> dùng dòng đó
                            it = ex
                    res = tracking.check_listing(it, prod, cfg)
                    it["check"] = {"time": when, "error": "", "vars": res}
                    for r in res.values():
                        counts[r["state"]] = counts.get(r["state"], 0) + 1
                if d.get("run"):   # lưu tiến độ: tắt tool rồi bật lại "Chạy tiếp" từ đây
                    d["run"]["done"].extend(group)
                return counts
            counts = tracking.update(fn)
            if err:
                self._tlog(f"  ✗ không đọc được: {err}", "err")
            else:
                names = {"up": "tăng giá", "down": "giảm giá", "oos": "hết hàng", "back": "có hàng lại"}
                msg = ", ".join(f"{n} size {names[k]}" for k, n in counts.items() if k in names)
                self._tlog(f"  {len(group)} listing · " + (msg or "không đổi"), "warn" if counts.get("up") or counts.get("oos") else "ok")
            done += len(group)
            with self.lock:
                self.track["progress"] = [done, total]
            if gi < len(groups) and stop.wait(core.site_delay(cfg, site)):
                continue
        if tracking.run_left():
            self._tlog(f"Tạm dừng ở {done}/{total} listing — bấm \"Chạy tiếp\" để chạy nốt (tắt / bật tool vẫn chạy tiếp được).", "warn")
        else:
            tracking.run_clear()
            self._tlog(f"Xong: đã kiểm tra {done}/{total} listing.", "ok")

    def _track_exported(self, items, cfg, site, run_id):
        """Listing vừa ghi ra CSV -> vào danh sách theo dõi."""
        try:
            tracking.add_exported(items, cfg, site, run_id)
        except Exception as e:
            print("Không thêm được vào Theo dõi sản phẩm:", e)
            traceback.print_exc()

    def _mark_rest(self, s, rest, why):
        """Dừng giữa chừng (bấm Dừng / site chặn): các link chưa chạy thành dòng lỗi -> "Chạy lại dòng lỗi" chạy tiếp."""
        with self.lock:
            for it in rest:
                if it.get("retry_id"):
                    old = next((r for r in s.results if r["id"] == it["retry_id"]), None)
                    if old is not None:
                        old.update(status="error", error=why)
                    continue
                s.results.append({"id": self.new_id(s), "url": it["url"], "status": "error", "error": why,
                                  "source": s.kind, "profile": it.get("profile", ""),
                                  "country": it.get("country") or "US", "category": ""})
            s.progress = [s.progress[1], s.progress[1]]

    def _fetcher(self, s, cfg):
        sc = core.site_cfg(cfg, s.kind)

        def log(text, tag=""):
            self.log(s, text, tag)

        return Fetcher(use_proxy=bool(sc.get("use_proxy")), pool=s.pool,
                       page_factory=s.browser.page if s.browser else None, log=log, should_stop=s.stop.is_set)

    def _upload_pics(self, s, prod, cfg, cache, fetcher=None, plan=None, proxies=None):
        """Up ảnh theo kế hoạch (ảnh chính + ảnh từng màu + ảnh chung). Trả về (pics, info ảnh đầu, ghi chú lỗi)."""
        plan = plan or core.plan_images(prod, cfg)
        need = list(dict.fromkeys(plan["main"] + [u for imgs in plan["colors"].values() for u in imgs]
                                  + plan.get("general", [])))
        if fetcher is not None:
            proxies = fetcher._proxies()
        mapping, first_info, failed = {}, None, 0
        started = time.monotonic()
        before = core.host_usage()
        def upload_one(src):
            if s.stop.is_set():
                raise core.Stopped()
            return core.rehost_image(src, cfg, cache, proxies=proxies, referer=prod["url"])

        # Chỉ Best Buy: tối đa 3 ảnh đang tải/up, thứ tự ảnh đầu ra vẫn theo plan.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=3 if s.kind == "bestbuy" else 1) as executor:
            futures = {executor.submit(upload_one, src): (n, src) for n, src in enumerate(need, 1)}
            for future in as_completed(futures):
                if s.stop.is_set():
                    for pending in futures:
                        pending.cancel()
                    raise core.Stopped()
                n, src = futures[future]
                try:
                    url, info, _cached = future.result()
                    mapping[src] = url
                    if src == need[0]:
                        first_info = info
                    if s.kind == "bestbuy" and not _cached:
                        self.log(s, f"  ảnh: đã xử lý {len(mapping) + failed}/{len(need)} (tối đa 3 ảnh cùng lúc)")
                except core.Stopped:
                    raise
                except Exception as e:
                    failed += 1
                    self.log(s, f"  ✗ ảnh {n}/{len(need)}: {e}", "err")
        used = {h: n - before.get(h, 0) for h, n in core.host_usage().items() if n > before.get(h, 0)}
        where = ", ".join(f"{core.HOST_NAMES[h]} {n}" for h, n in used.items()) or core.host_label(cfg)
        # Ảnh nằm ở nơi không phải nơi đầu danh sách = nơi đầu đang hỏng, tool đã tự chuyển
        moved = any(h != core.upload_hosts(cfg)[0] for h in used)
        down = core.host_down_note()
        self.log(s, f"  ảnh: up {len(mapping)}/{len(need)} ảnh ({where})" + (" — đã tự chuyển nơi up" if moved else "")
                    + (f", {failed} lỗi" if failed else "") + f" · {time.monotonic() - started:.1f}s"
                    + (f" · {down}" if down else ""), "warn" if failed or moved else "")
        pics = {"main": [mapping[u] for u in plan["main"] if u in mapping],
                "colors": {c: [mapping[u] for u in imgs if u in mapping] for c, imgs in plan["colors"].items()},
                "general": [mapping[u] for u in plan.get("general", []) if u in mapping]}
        pics["colors"] = {c: v for c, v in pics["colors"].items() if v}
        if not pics["main"] and not pics["colors"]:
            raise SkipProduct("Không up được ảnh nào")
        note = f"{failed} ảnh up lỗi" if failed else ""
        return pics, first_info, note

    def _read_product(self, s, fetcher, url, product_cache):
        key = s.mod.product_code(url) if s.kind == "bestbuy" else None
        if key and key in product_cache:
            self.log(s, f"  Dùng lại dữ liệu {key} đã đọc trong lượt này; ảnh đã up được dùng lại")
            return copy.deepcopy(product_cache[key])
        prod = validate(s.mod.scrape(fetcher, url))
        if key and getattr(fetcher, "bestbuy_data_live", False):
            product_cache[key] = copy.deepcopy(prod)
        return prod

    def _run(self, s, items, upload):
        cfg = core.load_config()
        cache = core.load_json(core.CACHE_PATH, {})
        s.pool = ProxyPool(core.proxies_for(cfg, s.kind))   # proxy riêng của site (Proxy-<site>.txt) đứng trước proxy chung
        site_pf = core.site_proxy_file(cfg, s.kind)
        sc = core.site_cfg(cfg, s.kind)
        if sc.get("use_proxy") and not s.pool.proxies:
            self.log(s, "Site này đang bật proxy nhưng chưa có proxy nào — dán proxy trong Cài đặt → Proxy", "err")
            return
        conflicts = core.country_conflicts(items)
        if conflicts:
            for p, c in conflicts.items():
                self.log(s, f"Profile {p or '(trống)'} có nhiều Action Country ({', '.join(c)}) — sửa file rồi chạy lại.", "err")
            return
        fetcher = self._fetcher(s, cfg)
        seen_codes = {}   # mã sản phẩm gốc -> số thứ tự dòng đầu tiên, để bắt mẫu trùng
        old_ids = set()   # chạy lại / thêm món: id các dòng có từ trước
        listed = {}       # (Profile, mã sản phẩm) -> {màu đã có listing} (1 acc không đăng 1 màu 2 lần)
        blocked_streak = 0   # số link liền nhau bị site chặn (2 -> tự dừng)
        retry = any(it.get("retry_id") for it in items)
        append = any(it.get("append") for it in items)
        product_cache = {}
        # Cache chỉ dùng trong cùng lượt chạy. Khi tiếp tục, dùng dữ liệu của các dòng
        # đã xong trong chính danh sách này (không lấy file trang cũ từ lượt khác).
        def product_key(url):
            return s.mod.product_code(url) if s.kind == "bestbuy" else None
        if s.kind == "bestbuy" and retry:
            for row in s.results:
                if row.get("status") == "ok" and row.get("product"):
                    product_cache[product_key(row["url"])] = copy.deepcopy(row["product"])
        if retry or append:   # chạy lại dòng lỗi / thêm món: các dòng đã xong vẫn tính để bắt trùng
            with self.lock:
                for k, r in enumerate(s.results, 1):
                    if r["status"] == "ok":
                        # cùng khoá như link mới: màu đọc từ link gốc (không lấy màu người dùng chọn lại sau đó)
                        flt_r = core.url_filters(r["url"], r["product"]) or {}
                        seen_codes.setdefault((r.get("profile", ""), r["product"]["code"],
                                               tuple(flt_r.get("colors") or [])), k)
                        listed.setdefault((r.get("profile", ""), r["product"]["code"]), set()).update(
                            v["color"] for v in core.selling_variants(r["product"], cfg, r) if v["color"])
                old_ids = {r["id"] for r in s.results}
        n = len(items)
        progress_base = s.progress[0] if retry else 0
        progress_total = s.progress[1] if retry else n
        profiles = core.group_by_profile(items)
        head = (f"Chạy lại {n} dòng lỗi" if retry else f"Thêm {n} món cùng trang vào bảng" if append
                else f"Bắt đầu {n} link")
        self.log(s, f"=== {head} {s.label} ("
                 + ", ".join(f"{p or 'không Profile'}: {len(g)}" for p, g in profiles.items())
                 + f"), giá {core.site_percent(cfg, s.kind):g}% giá gốc"
                 + (f", luôn đi proxy {s.pool.label()}" if sc.get("use_proxy")
                    else ", IP gốc (bị chặn sẽ tự chuyển proxy)" if s.pool.proxies else ", IP gốc (chưa có proxy)")
                 + (f" [{len(s.pool.proxies)} proxy: {site_pf.name} dùng trước, rồi proxy chung]" if site_pf else "")
                 + (f", up ảnh lên {core.host_label(cfg)}" if upload else ", không up ảnh") + " ===")
        for i, item in enumerate(items, 1):
            if s.stop.is_set():
                self.log(s, "Đã dừng theo yêu cầu.", "warn")
                self._mark_rest(s, items[i - 1:], "Chưa chạy — đã bấm Dừng")
                break
            url = item["url"]
            with self.lock:
                old = next((r for r in s.results if r["id"] == item.get("retry_id")), None) if item.get("retry_id") else None
                if old is not None:   # chạy lại dòng lỗi: dùng lại đúng dòng đó trong bảng
                    old.clear()
                    old.update(id=item["retry_id"], url=url, status="running", source=s.kind,
                               profile=item.get("profile", ""), country=item.get("country") or "US", category="")
                    res = old
                else:
                    res = {"id": self.new_id(s), "url": url, "status": "running", "source": s.kind,
                           "profile": item.get("profile", ""), "country": item.get("country") or "US", "category": ""}
                    s.results.append(res)
            self.log(s, f"[{progress_base + i}/{progress_total}] {res['profile'] + ' · ' if res['profile'] else ''}{url}")
            try:
                prod = self._read_product(s, fetcher, url, product_cache)
                code = prod["code"]
                # Trong CÙNG 1 Profile: cùng sản phẩm nhưng link chọn màu khác -> listing riêng (SKU thêm mã màu,
                # link không chọn màu -> thêm "ALL"); cùng màu -> trùng. Profile khác (acc khác) -> không tính trùng.
                flt0 = core.url_filters(url, prod) or {}
                ckey = (res["profile"], code, tuple(flt0.get("colors") or []))
                if ckey in seen_codes:
                    raise SkipProduct(f"Trùng sản phẩm với dòng {seen_codes[ckey]} (mã {code})")
                if any(k[0] == res["profile"] and k[1] == code for k in seen_codes):
                    tag = flt0["colors"][0] if flt0.get("colors") else "ALL"
                    code = f"{code}-{re.sub(r'[^A-Za-z0-9]', '', tag)[:8].upper()}"
                # Giữ đủ tên (không cắt): dài quá 80 thì ô tiêu đề tô đỏ + nút "Sửa bằng AI" trong bảng
                title = core.make_title(prod, cfg, 10_000)
                vs = core.selling_variants(prod, cfg)
                self.log(s, f"  {title}" + (f"  (dài {len(title)} ký tự — bấm ✨ Sửa bằng AI ở ô tiêu đề)"
                                            if len(title) > 80 else ""))
                self.log(s, f"  {len({v['color'] for v in vs if v['color']})} màu, "
                            f"{len({v['size'] for v in vs if v['size']})} size, {len(vs)} biến thể đưa vào listing"
                            f" (site có {len(prod['variants'])}, còn hàng {sum(v['available'] for v in prod['variants'])})")
                if len([v for v in prod["variants"] if v["available"]]) > 250:
                    self.log(s, f"  ⚠ hơn 250 biến thể còn hàng — eBay chỉ cho 250, giữ {len({v['color'] for v in vs})} "
                                "màu đầu (đủ size), các màu sau bỏ", "warn")
                def log(text, tag=""):
                    self.log(s, text, tag)

                # Link gốc đã chọn sẵn màu / nhóm size -> chọn theo đó trước, chỉ up ảnh của màu được chọn
                sku = core.make_sku(cfg, s.kind, code, res["profile"])
                tmp = dict(res, sku=sku, code=code, title=title, product=prod, uploaded=bool(upload),
                           variant_sel=core.default_selection(prod, cfg))
                self._preselect_from_url(tmp, cfg, log)
                # Cùng acc đã có listing chứa màu đó (vd link "-black" rồi link "tất cả màu") -> bỏ các màu đã đăng,
                # tránh 2 listing trùng biến thể trong 1 tài khoản (eBay cấm đăng trùng)
                used = listed.get((res["profile"], prod["code"]), set())
                if used:
                    vs0 = core.selling_variants(prod, cfg, tmp)
                    keep = [core.vkey(v) for v in vs0 if v["color"] not in used]
                    if not keep:
                        raise SkipProduct(f"Các màu của link này đã có listing ở acc {res['profile'] or '(trống)'} — bỏ qua")
                    if len(keep) < len(vs0):
                        tmp["variant_sel"] = keep
                        left = list(dict.fromkeys(v["color"] for v in vs0 if v["color"] not in used))
                        self._apply_filters(tmp, {"colors": left, "groups": (tmp.get("filters") or {}).get("groups",
                                                                                                    core.size_groups(prod))})
                        log(f"  bỏ {', '.join(sorted(used & {v['color'] for v in vs0}))} — đã có listing ở dòng khác cùng acc", "warn")
                plan = core.plan_images(prod, cfg, tmp)
                note = ""
                if upload:
                    pics, info, note = self._upload_pics(s, prod, cfg, cache, fetcher, plan=plan)
                else:
                    pics, info = plan, None
                    note = "Chưa up ảnh — đang dùng link ảnh gốc của site"
                problems = core.check_pic_url(pics["main"][0]) if pics["main"] else ["không có ảnh"]
                if problems:
                    note = "; ".join(filter(None, [note, *problems]))
                tmp["pics"] = pics
                self._suggest_category(tmp, cfg, log)
                self._refresh_category(tmp, cfg, log)
                tmp.update(price_info(prod, cfg, tmp))
                with self.lock:
                    res.update(tmp, status="ok", pic=pics["main"][0] if pics["main"] else "", img_info=info,
                               note=note, n_images=len(pics["main"]))
                    # chỉ tính "đã có" khi dòng chạy xong (dòng lỗi không làm link sau bị báo trùng / thêm đuôi SKU)
                    seen_codes[ckey] = s.results.index(res) + 1   # số dòng trong bảng (không phải thứ tự link)
                    listed.setdefault((res["profile"], prod["code"]), set()).update(
                        v["color"] for v in core.selling_variants(prod, cfg, res) if v["color"])
                self.log(s, f"  ✓ {sku} · {res['price_text']} · Category {res['category_auto'] or '(chưa có)'}"
                            + (f" ({res['category_src']})" if res["category_src"] else ""), "ok")
                if res.get("problem"):
                    self.log(s, f"  ⚠ {res['problem']}", "warn")
                if core.fcc_missing(res):
                    self.log(s, "  ⚠ Có Wi-Fi / Bluetooth — eBay Mỹ bắt điền FCC ID đúng model: bấm Xem, điền ô FCC ID", "warn")
                blocked_streak = 0
            except core.Stopped:
                with self.lock:
                    res.update(status="error", error="Đã dừng")
                self.log(s, "Đã dừng theo yêu cầu.", "warn")
                self._mark_rest(s, items[i:], "Chưa chạy — đã bấm Dừng")
                break
            except SkipProduct as e:
                with self.lock:
                    res.update(status="error", error=str(e))
                self.log(s, f"  ✗ Bỏ qua: {e}", "err")
                if "chặn" in str(e):
                    blocked_streak += 1
                    if blocked_streak >= 2:
                        # Site đang chặn: dừng luôn, đánh dấu các link còn lại để bấm "Chạy lại dòng lỗi" sau
                        rest = items[i:]
                        self._mark_rest(s, rest, "Chưa chạy — site đang chặn")
                        self.log(s, f"⛔ {s.label} đang chặn truy cập (2 link liền nhau) — tool tự dừng để không bị chặn lâu hơn. "
                                    f"Chờ 30–60 phút rồi bấm \"↻ Chạy lại dòng lỗi\" ({len(rest) + blocked_streak} dòng).", "err")
                        break
                else:
                    blocked_streak = 0
            except Exception as e:
                with self.lock:
                    res.update(status="error", error=repr(e))
                self.log(s, f"  ✗ Lỗi: {e!r}", "err")
                traceback.print_exc()
            with self.lock:
                s.progress = [progress_base + i, progress_total]
            if i < n and s.stop.wait(core.site_delay(cfg, s.kind)):
                continue  # bấm Dừng trong lúc nghỉ -> vòng sau sẽ thoát

        if s.browser:   # trình duyệt chỉ để đọc trang: đóng ngay, không chờ hết bước kiểm ảnh / ghi CSV (có thể vài phút)
            s.browser.close()
        ok = [r for r in s.results if r["status"] == "ok"]
        # Người dùng có thể đổi % giá / quy tắc Category trong lúc đang chạy -> tính lại theo cài đặt mới nhất trước khi
        # xuất (bảng và file CSV khớp nhau)
        s.phase = "Tính lại Category / giá theo cài đặt mới nhất"
        cfg = core.load_config()
        for r in ok:
            tmp = dict(r)
            self._refresh_category(tmp, cfg)
            tmp.update(price_info(tmp["product"], cfg, tmp))
            with self.lock:
                r.update({k: tmp[k] for k in REFRESH_KEYS if k in tmp})
        todo = [r for r in ok if r.get("title_src") != "ai"]   # dòng đã có tiêu đề AI thì không viết lại
        if retry or append:   # chỉ viết tiêu đề cho dòng vừa chạy, không đụng tiêu đề (có thể đã sửa tay) của dòng cũ
            redo = {it.get("retry_id") for it in items}
            todo = [r for r in todo if r["id"] not in old_ids or r["id"] in redo]
            with self.lock:
                s.dirty = True   # file đã xuất trước đó chưa có các dòng mới
        n_long = sum(1 for r in ok if len(r.get("title", "")) > 80)
        if n_long:
            self.log(s, f"⚠ {n_long} tiêu đề dài quá 80 ký tự (ô tô đỏ) — bấm ✨ Sửa bằng AI ở từng ô, "
                        "không sửa thì khi xuất sẽ bị cắt bớt", "warn")
        if todo and cfg.get("ai_titles") and ai_title.configured(cfg) and not s.stop.is_set():
            s.phase = f"AI đang viết tiêu đề {len(todo)} listing"
            try:
                self._ai_titles(s, todo, cfg)
            except Exception as e:
                self.log(s, f"✗ AI viết tiêu đề lỗi: {e} — giữ tiêu đề gốc", "err")
        self.log(s, "")
        by_ip = fetcher.ip_summary() if fetcher is not None else ""
        if by_ip:   # Best Buy: mỗi IP đọc được bao nhiêu link / bị chặn mấy lần -> biết IP / proxy nào còn dùng tốt
            self.log(s, f"Theo IP: {by_ip}")
        self.log(s, f"Xong: {len(ok)} listing, {len(s.results) - len(ok)} lỗi/bỏ qua.",
                 "ok" if len(ok) == len(s.results) else "warn")
        if ok:
            run_id = core.new_run_id(s.kind)
            exp = [dict(r) for r in ok]
            self.log(s, "Đang kiểm tra lại ảnh đã up và ghi file CSV gốc — xong mới bấm Chạy lại / Chạy tiếp được…")
            try:
                paths, errors = core.export_run(exp, cfg, run_id, s.kind, log=lambda t: self.log(s, t),
                                                step=lambda t: setattr(s, "phase", f"CSV gốc: {t}"))
            except Exception as e:
                self.log(s, f"Chưa xuất được CSV gốc: {e}", "warn")
                paths, errors = [], []
            with self.lock:
                s.run_id = run_id
                s.orig_files = [p.name for p in paths]
            if paths:
                self._track_exported([r for r in exp if not r.get("_dup")], cfg, s.kind, run_id)
                self.log(s, f"File CSV gốc ({len(paths)} file, mỗi Profile 1 file) trong output\\{s.kind}\\goc\\{run_id}: "
                         + ", ".join(p.name for p in paths), "ok")
            for e in errors:
                self.log(s, f"  ⚠ {e}" if core.export_note(e)
                         else f"  ✗ Không xuất: {e} — sửa trong bảng Kết quả rồi bấm Xuất CSV", "warn")


def price_info(prod, cfg, item=None):
    """Khoảng giá site / giá eBay cho cột Biến thể & giá (theo các biến thể đang chọn)."""
    vs = core.selling_variants(prod, cfg, item)
    pct = core.site_percent(cfg, prod["site"])
    rnd = cfg.get("round_99", True)
    src = sorted({core.base_price(v, prod["site"]) for v in vs}) or [0]   # Hollister / Eileen Fisher: giá gốc
    out = sorted({core.ebay_price(p, pct, rnd) for p in src})
    fmt = lambda xs: f"${xs[0]:.2f}" if len(xs) == 1 else f"${xs[0]:.2f}–${xs[-1]:.2f}"   # noqa: E731
    return {"price_text": f"{fmt(src)} → {fmt(out)}", "n_colors": len({v['color'] for v in vs if v['color']}),
            "n_sizes": len({v['size'] for v in vs if v['size']}), "n_variants": len(vs),
            "n_all": len(prod["variants"])}


def _proxy_route(r):
    """Chặn tài nguyên không cần thiết khi Chrome đi qua proxy (proxy rớt kết nối nếu mở quá nhiều host)."""
    req = r.request
    if req.resource_type in ("image", "font", "media"):
        return r.abort()
    host = re.sub(r"^https?://([^/:]+).*", r"\1", req.url)
    try:
        page_host = re.sub(r"^https?://([^/:]+).*", r"\1", req.frame.page.url or "")
    except Exception:
        page_host = ""
    root = ".".join(page_host.split(".")[-2:]) if "." in page_host else ""
    allow = ("cloudflare.com", "awswaf.com", "akamai", "akstat", "perimeterx", "px-cdn", "px-cloud")
    if req.resource_type == "document" or not root or host.endswith(root) or any(a in host for a in allow):
        return r.continue_()
    return r.abort()


class _Browser:
    """Chrome riêng của 1 luồng (lần chạy 1 site / lượt Theo dõi sản phẩm). Playwright chỉ dùng được trên thread đã tạo
    nó; mỗi luồng 1 thư mục profile riêng để chạy song song không giành nhau. Chỉ mở khi site cần trình duyệt."""

    def __init__(self, log, profile, camoufox=False):
        self.log = log
        self.profile = profile
        self.pw = None
        self.ctx = None
        self.proxy = "none"
        self.camoufox = camoufox
        self.owned = OwnedBrowserProcesses()

    def page(self, proxy, camoufox=None):
        if camoufox is not None and camoufox != self.camoufox:
            if self.ctx is not None:
                self._close_ctx()
            self.camoufox = camoufox
        want = proxy["server"] if proxy else ""
        if self.ctx is not None and self.proxy != want:
            self._close_ctx()
        if self.ctx is not None:
            try:
                pages = [p for p in self.ctx.pages if not p.is_closed()]
                return pages[0] if pages else self.ctx.new_page()
            except Exception:
                self._close_ctx()
        self.log(f"Mở {'Camoufox' if self.camoufox else 'Chrome'}{' qua proxy ' + want.split('://')[-1] if want else ''}...")
        kw = dict(headless=False, locale="en-US", viewport={"width": 1280, "height": 900})
        if proxy:
            kw["proxy"] = proxy
        self.ctx = self._launch(kw)
        self.proxy = want
        if proxy:
            self.ctx.route("**/*", _proxy_route)
        return self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    LAUNCH_TRIES = 3
    LAUNCH_TIMEOUT = 90000   # lần mở đầu sau khi bật tool có thể quá 30 giây (nạp tiện ích, tạo dấu vân tay)

    def _launch(self, kw):
        """Retry this product with a fresh driver and verified profile release, preserving proxy/identity data."""
        self._launch_proxy = dict(kw.get("proxy") or {})
        for attempt in range(1, self.LAUNCH_TRIES + 1):
            try:
                self._wait_profile_free()
                if self.pw is None:
                    self.pw = self._start_driver()
                context = self._launch_once(kw)
                self.ctx = context
                self._capture_owned()
                if not context.pages:
                    context.new_page()
                return context
            except Exception as e:
                self._capture_owned()
                why = self._record_error("launch", e, kw, attempt)
                # A launch timeout can leave a browser behind without assigning self.ctx.
                # Capture its ownership BEFORE stopping the old Playwright driver.
                try:
                    self._reset_driver()
                except Exception as cleanup_error:
                    self._record_error("launch_cleanup", cleanup_error, kw, attempt)
                    raise BrowserLaunchError("Chưa dọn được phiên browser lỗi; chi tiết: browser_diagnostics.log") from cleanup_error
                if attempt == self.LAUNCH_TRIES:
                    raise BrowserLaunchError(f"{why}; chi tiết: browser_diagnostics.log") from e
                self.log(f"  Mở trình duyệt lỗi ({why}) — đã khởi tạo lại bộ điều khiển, thử lại cùng sản phẩm "
                         f"({attempt + 1}/{self.LAUNCH_TRIES}) sau 5 giây", "warn")
                time.sleep(5)

    @staticmethod
    def _start_driver():
        from playwright.sync_api import sync_playwright
        return sync_playwright().start()

    def _launch_once(self, kw):
        if self.camoufox:
            from camoufox.sync_api import NewBrowser
            from camoufox.addons import DefaultAddons
            return NewBrowser(self.pw, persistent_context=True, user_data_dir=str(self.profile),
                              exclude_addons=[DefaultAddons.UBO], timeout=self.LAUNCH_TIMEOUT,
                              **{k: v for k, v in kw.items() if k != "viewport"})
        try:
            return self.pw.chromium.launch_persistent_context(str(self.profile), channel="chrome", **kw)
        except Exception:
            return self.pw.chromium.launch_persistent_context(str(self.profile), **kw)

    def _record_error(self, stage, exc, kw=None, attempt=None):
        """Full traceback and browser stderr/call-log, with proxy credentials removed."""
        proxy = (kw or {}).get("proxy") or getattr(self, "_launch_proxy", {})
        def redact(value):
            value = re.sub(r'(https?|socks5?)://[^\s/@]+@', r'\1://***@', value)
            for field in ("username", "password"):
                if proxy.get(field):
                    value = value.replace(str(proxy[field]), "***")
            return value
        detail = redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        first = (str(exc).splitlines() or [""])[0].lstrip(": ").strip() or type(exc).__name__
        record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": stage, "attempt": attempt,
                  "browser": "Camoufox" if self.camoufox else "Chrome", "profile": str(self.profile),
                  "driver_pid": driver_pid(self.pw), "owned_pids": list(self.owned.handles),
                  "proxy": redact(str(proxy.get("server") or "direct")), "detail": detail}
        try:
            with _BROWSER_LOG_LOCK:
                with (core.ROOT / "browser_diagnostics.log").open("a", encoding="utf-8") as file:
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            self.log("  Không ghi được browser_diagnostics.log", "warn")
        return redact(first)[:220]

    def _capture_owned(self):
        try:
            self.owned.capture(self.pw)
        except Exception as exc:
            self._record_error("capture_owned", exc)
            self.log("  Không xác định được tiến trình browser; sẽ không dừng tiến trình không rõ chủ", "warn")

    def _finish_owned(self):
        killed, remaining = self.owned.finish()
        if killed:
            self.log(f"  Đã dọn {len(killed)} tiến trình browser còn sót của phiên này", "warn")
        if remaining:
            raise BrowserLaunchError(f"Browser phiên này chưa thoát (PID {remaining}); chưa mở đè hồ sơ")

    def _reset_driver(self):
        self._capture_owned()
        old, self.pw = self.pw, None
        if old is not None:
            try:
                old.stop()
            except Exception as exc:
                self._record_error("stop_driver", exc)
        self.ctx, self.proxy = None, "none"
        self._finish_owned()

    def _wait_profile_free(self, limit=15):
        """Chờ tiến trình vừa đóng nhả thư mục hồ sơ (Firefox giữ parent.lock, Chrome giữ lockfile) rồi mới mở lại,
        không thì mở ngay sẽ "Failed to launch the browser process"."""
        end = time.monotonic() + limit
        while True:
            if profile_is_free(self.profile, self.camoufox):
                return
            if time.monotonic() >= end:
                raise BrowserLaunchError("Hồ sơ browser vẫn đang được sử dụng; chưa mở phiên mới")
            time.sleep(0.5)

    def _close_ctx(self):
        self._capture_owned()
        try:
            self.ctx.close()
        except Exception as exc:
            self._record_error("close_context", exc)
            self._reset_driver()
        self.ctx, self.proxy = None, "none"
        self._finish_owned()

    def close(self):
        if self.ctx is not None:
            self._close_ctx()
        self._reset_driver()


ENGINE = None
PICK_FOLDER = None   # desktop.py gán hộp chọn thư mục của cửa sổ app; chạy bằng trình duyệt thì dùng tkinter


def pick_folder(initial):
    """Hỏi người dùng chọn thư mục lưu CSV. Trả về đường dẫn, hoặc None nếu bấm Huỷ."""
    if os.environ.get("RETAIL_EXPORT_DIR"):   # chạy thử tự động, không hiện hộp thoại
        return os.environ["RETAIL_EXPORT_DIR"]
    if PICK_FOLDER:
        return PICK_FOLDER(initial)
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)   # hộp thoại nổi lên trên trình duyệt
    try:
        path = filedialog.askdirectory(initialdir=initial or None, title="Chọn thư mục lưu file CSV", parent=root)
    finally:
        root.destroy()
    return path or None


def has_xlsx_app():
    """Máy có chương trình mở .xlsx (Excel, WPS...) không."""
    try:
        import winreg
        return bool(winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, ".xlsx"))
    except OSError:
        return False


def license_info():
    """Dòng bản quyền ở đầu giao diện; chỉ hiện ở bản .exe có khoá key."""
    import license as lic

    out = {"text": "", "level": "", "telegram": "@toibihoviai"}
    if not getattr(sys, "frozen", False) or not lic.has_public_key():
        return out
    ok, _, expiry = lic.check_saved(core.ROOT)
    if not ok:
        return out
    if expiry is None:
        out.update(text="Bản quyền: vĩnh viễn")
        return out
    left = lic.days_left(expiry)
    out["text"] = f"Bản quyền đến {expiry:%d/%m/%Y} · còn {left} ngày"
    if left <= 7:
        out.update(text=out["text"] + " — sắp hết hạn, xin key mới", level="err")
    elif left <= 30:
        out["level"] = "warn"
    return out


def test_proxy(cfg, n=3, site=None):
    """Thử n proxy đầu (danh sách chung, hoặc danh sách riêng của `site`): nước, nhà mạng, có phải IP hosting/VPS không
    (Best Buy chặn ngay IP hosting, IP dân cư / nhà mạng doanh nghiệp thì vào được)."""
    from curl_cffi import requests

    out = []
    proxies = core.parse_proxy_lines(core.site_proxy_lines(cfg, site)) if site else core.proxies_for(cfg)
    if not proxies:
        raise RuntimeError("Chưa có proxy nào — dán proxy vào ô bên trên (mỗi dòng ip:port:user:pass)")
    for p in proxies[:n]:
        u = proxy_url(p)
        label = p["server"].split("://")[-1]
        try:
            r = requests.get("http://ip-api.com/json/?fields=status,country,isp,hosting", proxies={"http": u, "https": u}, timeout=20)
            j = r.json()
            out.append(f"✓ {label}: {j.get('country')} · {j.get('isp')}" + (" · IP hosting/VPS (site dễ chặn)" if j.get("hosting") else ""))
        except Exception as e:
            out.append(f"✗ {label}: {type(e).__name__}")
    return {"count": len(proxies), "lines": out}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if re.fullmatch(r"/fonts/[\w.-]+\.woff2", u.path):   # font đóng gói sẵn (web/fonts)
            f = WEB_DIR / "fonts" / u.path.rsplit("/", 1)[1]
            if not f.is_file():
                return self._send(404, {"error": "not found"})
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "font/woff2")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=31536000")
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/api/config":
            return self._send(200, dict(core.public_config(core.load_config()), ai_prompt_default=ai_title.DEFAULT_RULES))
        if u.path == "/api/sites":
            return self._send(200, [{"key": m.KEY, "name": m.NAME, "examples": getattr(m, "EXAMPLES", [])[:2],
                                     "needs_proxy": getattr(m, "NEEDS_PROXY", False),
                                     "price_basis": core.price_basis(m.KEY)} for m in SITES.values()])
        if u.path == "/api/thumb":
            src = q.get("u", [""])[0]
            if not src.startswith("https://"):
                return self._send(400, {"error": "bad url"})
            try:
                data = core.thumbnail(src, core.load_config())
            except Exception:
                return self._send(404, {"error": "no image"})
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/api/license":
            return self._send(200, license_info())
        if u.path == "/api/status":
            return self._send(200, ENGINE.snapshot(q.get("tab", [""])[0], int(q.get("since", ["0"])[0])))
        if u.path == "/api/track/state":   # mtime đổi -> giao diện tải lại danh sách
            st = ENGINE.track_state(int(q.get("since", ["0"])[0]))
            st["mtime"] = tracking.PATH.stat().st_mtime if tracking.PATH.exists() else 0
            return self._send(200, st)
        if u.path == "/api/track/items":
            d = tracking.load()
            return self._send(200, {"items": list(d["items"].values()), "alert": tracking.ALERT, "aliases": d.get("aliases") or {}})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            tab = body.get("tab") or ""
            s = ENGINE.session(tab)
            if path == "/api/config":
                old = core.load_config()
                cfg = core.update_config(body)
                # Chỉ tính lại bảng khi GIÁ TRỊ thật sự đổi (giao diện lưu cả form mỗi lần Chạy / Xuất...)
                changed = {k for k in set(old) | set(cfg) if k != "sites" and old.get(k) != cfg.get(k)} - NO_REFRESH
                site_changed = {(t, k) for t, sc in cfg["sites"].items()
                                for k in set(sc) | set(old["sites"].get(t, {}))
                                if sc.get(k) != old["sites"].get(t, {}).get(k)}
                # brand_in_title: áp dụng cho lần chạy sau (không dựng lại tiêu đề đã có / đã sửa tay)
                site_changed = {(t, k) for t, k in site_changed if k not in ("use_proxy", "delay", "prefix", "brand_in_title")}
                if changed <= PRICE_KEYS and all(k == "price_percent" for _, k in site_changed):
                    # chỉ đổi giá: tính lại giá (nhanh) — đổi % chung thì mọi tab, đổi % 1 site thì site đó
                    tabs = list(ENGINE.sessions) if changed else {t for t, _ in site_changed}
                    for t in tabs:
                        if t in ENGINE.sessions:
                            ENGINE.refresh_all(t, prices_only=True)
                else:
                    # quy tắc Category, Condition...: tab đang mở tính ngay, các tab khác tính nền (lần lượt)
                    if tab in ENGINE.sessions:
                        ENGINE.refresh_all(tab)
                    others = [t for t in ENGINE.sessions if t != tab]
                    threading.Thread(target=lambda: [ENGINE.refresh_all(t) for t in others], daemon=True).start()
                return self._send(200, dict(core.public_config(cfg), ai_prompt_default=ai_title.DEFAULT_RULES))
            if path == "/api/test-host":
                cfg = core.load_config()
                results = core.test_image_host(cfg)   # [(tên nơi, link | None, lỗi)] — thử từng nơi trong danh sách
                return self._send(200, {"ok": any(url for _, url, _ in results), "host": core.host_label(cfg),
                                        "hosts": [{"name": n, "ok": bool(u), "error": e} for n, u, e in results]})
            if path == "/api/test-ebay":
                return self._send(200, {"ok": True, "sample": ebay_api.test(core.load_config())})
            if path == "/api/test-ai":
                return self._send(200, {"ok": True, "result": ai_title.test(core.load_config())})
            if path == "/api/ai-titles":
                n = ENGINE.ai_rewrite(s.kind, body.get("ids", []), bool(body.get("restore")))
                return self._send(200, {"count": n})
            if path == "/api/proxies":
                # Dán / thay / xoá proxy. Trả về config đã che (chỉ ip:port) + số dòng nhận / bỏ
                cfg, ok, bad = core.edit_proxies(core.load_config(), body.get("action", ""), body.get("text", ""),
                                                 body.get("index"))
                out = dict(core.public_config(cfg), ai_prompt_default=ai_title.DEFAULT_RULES)
                return self._send(200, dict(out, added=ok, bad=bad))
            if path == "/api/site-proxies":
                # Cài đặt -> Proxy riêng theo site: dán / xoá proxy riêng của 1 site. Trả về config đã che + số dòng nhận / bỏ
                cfg, ok, bad = core.edit_site_proxies(core.load_config(), body.get("site", ""), body.get("action", ""),
                                                      body.get("text", ""), body.get("index"))
                out = dict(core.public_config(cfg), ai_prompt_default=ai_title.DEFAULT_RULES)
                return self._send(200, dict(out, added=ok, bad=bad))
            if path == "/api/test-proxy":
                return self._send(200, test_proxy(core.load_config(), site=body.get("site") or None))
            if path == "/api/run":
                skipped = []
                if body.get("items"):  # danh sách đã import từ file CSV/Excel
                    items = body["items"]
                else:  # ô dán link: chưa có Profile, Country = US -> sửa từng dòng trong bảng Kết quả
                    pairs, skipped = core.parse_link_list(body.get("links", ""), s.mod, SITES)
                    items = [{"url": url, "key": key, "profile": "", "country": "US"} for key, url in pairs]
                if not items:
                    detail = ("\n" + "\n".join(skipped)) if skipped else ""
                    return self._send(400, {"error": f"Chưa có link sản phẩm {s.mod.NAME} nào{detail}"})
                ENGINE.submit(s.kind, items, bool(body.get("upload", True)), skipped)
                return self._send(200, {"count": len(items), "skipped": skipped})
            if path == "/api/resuggest":
                return self._send(200, ENGINE.resuggest(s.kind, body.get("ids")))
            if path == "/api/retry":
                return self._send(200, {"count": ENGINE.retry_errors(s.kind, bool(body.get("upload", True)), body.get("id"),
                                                                     body.get("only"))})
            if path == "/api/append":
                return self._send(200, {"count": ENGINE.append_links(s.kind, body.get("id", ""), body.get("urls") or [],
                                                                     bool(body.get("upload", True)))})
            if path == "/api/import":
                raw = base64.b64decode(body.get("data", "").split(",", 1)[-1])
                items, warnings = core.parse_import(body.get("name", ""), raw, s.mod, SITES)
                summary = {p or "(không Profile)": len(g) for p, g in core.group_by_profile(items).items()}
                return self._send(200, {"items": items, "warnings": warnings, "summary": summary})
            if path == "/api/import-all":
                # Nhập file tổng: chia từng dòng về đúng site (cột Site; ô trống thì nhận theo link)
                raw = base64.b64decode(body.get("data", "").split(",", 1)[-1])
                name = body.get("name", "")
                out, taken, why = {}, set(), {}
                noise = re.compile(r"dùng tab|không lấy ở tab này|Import file ở tab|không phải link sản phẩm")
                for key, mod in SITES.items():
                    items, warns = core.parse_import(name, raw, mod, SITES)
                    for w in warns:
                        m = re.match(r"Dòng (\d+): (.*)", w)
                        if m:
                            why.setdefault(int(m.group(1)), m.group(2))
                    if items:
                        taken.update(it["row"] for it in items)
                        out[key] = {"items": items, "warnings": [w for w in warns if not noise.search(w)],
                                    "summary": {p or "(không Profile)": len(g) for p, g in core.group_by_profile(items).items()}}
                skipped = [f"Dòng {n}: " + ("link không thuộc site nào trong tool" if "không phải link sản phẩm" in w else w)
                           for n, w in sorted(why.items()) if n not in taken and not re.search(r"link trùng dòng", w)]
                return self._send(200, {"sites": out, "skipped": skipped})
            if path == "/api/template":
                # Tạo file mẫu trong thư mục "file mau" rồi mở bằng Excel; file đang mở trong Excel thì chỉ mở lại
                target = core.template_path()   # 1 file mẫu chung, cột Site có đủ các site hiện có
                try:
                    core.make_template(s.mod, target)
                except PermissionError:
                    pass
                opened = has_xlsx_app()
                if opened:
                    os.startfile(target)
                else:  # máy không có Excel -> mở thư mục, chọn sẵn file mẫu
                    subprocess.Popen(["explorer", "/select,", str(target)])
                return self._send(200, {"path": str(target), "opened_in_app": opened})
            # ---- Theo dõi sản phẩm
            if path == "/api/track/check":
                return self._send(200, {"count": ENGINE.track_start(body.get("skus") or None, bool(body.get("resume")))})
            if path == "/api/track/stop":
                ENGINE.track["stop"].set()
                return self._send(200, {})
            if path in ("/api/track/import-list", "/api/track/import-result"):
                files = [(f.get("name", ""), base64.b64decode(f.get("data", "").split(",", 1)[-1])) for f in body.get("files", [])]
                if path.endswith("list"):
                    return self._send(200, tracking.import_lists(files, core.load_config()))
                rep = {"live": 0, "removed": 0, "unknown": 0, "qty_updated": 0}
                for name, raw in files:
                    for k, v in tracking.import_results(name, raw).items():
                        rep[k] += v
                return self._send(200, rep)
            if path == "/api/track/profile":
                return self._send(200, {"changed": tracking.set_profile(body.get("skus") or [], body.get("profile", ""),
                                                                        bool(body.get("remember", True)))})
            if path == "/api/track/alias-add":
                return self._send(200, {"changed": tracking.add_alias(body.get("name", ""), body.get("to", ""))})
            if path == "/api/track/alias-remove":
                return self._send(200, {"removed": tracking.drop_alias(body.get("name", ""))})
            if path == "/api/track/remove":
                return self._send(200, {"removed": tracking.remove(body.get("skus") or [])})
            if path == "/api/track/revise":
                folder = core.OUTPUT_DIR / "theo-doi" / "revise-SL0"
                files, no_id, n = tracking.write_revise(folder)
                if files:
                    os.startfile(folder)
                return self._send(200, {"files": [p.name for p in files], "folder": str(folder), "no_id": no_id, "variants": n})
            if path == "/api/rows":
                return self._send(200, ENGINE.update_rows(s.kind, body.get("ids", []), body.get("changes", {})))
            if path == "/api/preview":
                return self._send(200, ENGINE.preview(s.kind, body.get("id", ""), body.get("title")))
            if path == "/api/photos":
                return self._send(200, ENGINE.photos_start(s.kind, body.get("id", ""), body.get("colors") or []))
            if path == "/api/photos/status":
                return self._send(200, ENGINE.photos_status(s.kind, body.get("id", "")))
            if path == "/api/photos/cancel":
                return self._send(200, ENGINE.photos_cancel(s.kind, body.get("id", "")))
            if path == "/api/stop":
                s.stop.set()   # chỉ dừng tab đang mở (lần chạy link hoặc AI của tab đó)
                return self._send(200, {})
            if path == "/api/export":
                cfg = core.load_config()
                ENGINE._check_ready(s)   # báo lỗi trước khi mở hộp chọn thư mục
                folder = pick_folder(cfg.get("export_dir") or str(core.OUTPUT_DIR))
                if not folder:
                    return self._send(200, {"cancelled": True})
                fresh = core.load_config()
                fresh["export_dir"] = folder   # lần sau mở sẵn thư mục này
                core.save_json(core.CONFIG_PATH, fresh)
                files, errors = ENGINE.export(s.kind, body.get("titles", {}), folder, body.get("ids") or None)
                # notes: chỉ để biết, listing vẫn xuất (FCC ID, đổi SKU, file danh sách trùng); errors: listing bị bỏ thật
                return self._send(200, {"files": files, "folder": folder,
                                        "errors": [e for e in errors if not core.export_note(e)],
                                        "notes": [e for e in errors if core.export_note(e)]})
            if path == "/api/open-output":
                # Mở thư mục CSV gốc của lần chạy hiện tại; chưa có lần chạy thì mở thư mục gốc chung
                base = core.out_dir(s.kind)
                folder = base / s.run_id if s.run_id and (base / s.run_id).exists() else base
                folder.mkdir(parents=True, exist_ok=True)
                os.startfile(folder)
                return self._send(200, {})
            self._send(404, {"error": "not found"})
        except Exception as e:
            # Ghi chi tiết ra loi.log cạnh tool (bản .exe không có cửa sổ console để xem lỗi)
            try:
                with open(core.ROOT / "loi.log", "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} POST {path}\n{traceback.format_exc()}\n")
            except OSError:
                pass
            self._send(400, {"error": str(e) or type(e).__name__})


def free_port(start=8810):  # tránh cổng của MERCH TO CSV (8790+) và MERCH TO EBAY (8765)
    for port in range(start, start + 20):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("Không tìm được cổng trống")


def start_server():
    """Khởi động engine + server trên cổng trống. Trả về (server, url)."""
    global ENGINE
    ENGINE = Engine()
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server, f"http://127.0.0.1:{port}"


def shutdown(server, timeout=8):
    """Dừng việc đang chạy, đóng Chrome, tắt server."""
    for s in ENGINE.sessions.values():
        s.stop.set()
    ENGINE.track["stop"].set()
    ENGINE.jobs.put(None)
    deadline = time.time() + timeout
    while (any(s.running for s in ENGINE.sessions.values()) or ENGINE.track["running"]) and time.time() < deadline:
        time.sleep(0.2)
    server.shutdown()


def main():
    server, url = start_server()
    print(f"Retail -> eBay CSV đang chạy tại {url}  (tắt cửa sổ này để thoát tool)")
    if not os.environ.get("RETAIL_NO_BROWSER"):
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
