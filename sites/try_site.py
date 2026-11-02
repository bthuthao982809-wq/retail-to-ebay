"""Chạy thử 1 bộ cào: python -m sites.try_site <site> <url> [--proxy] [--browser]

In tóm tắt + lưu JSON đầy đủ vào _research/out/<site>_<code>.json
--proxy  : đi qua proxy trong C:\\Users\\toibihoviai\\Desktop\\Proxy.txt (xoay vòng)
--browser: cấp Chrome thật (Playwright) cho bộ cào cần trình duyệt
--start=N: bắt đầu từ proxy thứ N trong file (0-based)
"""
import importlib
import json
import sys
from pathlib import Path

from sites.base import Fetcher, ProxyPool, load_proxies, validate

ROOT = Path(__file__).resolve().parent.parent
PROXY_FILE = r"C:\Users\toibihoviai\Desktop\Proxy.txt"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    site, url = args[0], args[1]
    mod = importlib.import_module(f"sites.{site}")
    use_proxy = "--proxy" in flags or getattr(mod, "NEEDS_PROXY", False)
    pool = ProxyPool(load_proxies(PROXY_FILE))
    for a in flags:
        if a.startswith("--start="):   # bắt đầu từ proxy thứ N (để nhiều người thử cùng lúc không dùng chung proxy)
            pool.i = int(a.split("=", 1)[1])

    pw_holder = {}

    def page_factory(proxy):
        from playwright.sync_api import sync_playwright

        if "ctx" not in pw_holder:
            pw = sync_playwright().start()
            kw = dict(headless=False, locale="en-US", viewport={"width": 1280, "height": 900})
            if proxy:
                kw["proxy"] = proxy
            prof = ROOT / "_research" / f"prof_try_{site}"
            pw_holder["pw"] = pw
            pw_holder["ctx"] = pw.chromium.launch_persistent_context(str(prof), channel="chrome", **kw)
        ctx = pw_holder["ctx"]
        return ctx.pages[0] if ctx.pages else ctx.new_page()

    f = Fetcher(use_proxy=use_proxy, pool=pool, page_factory=page_factory if "--browser" in flags else None,
                log=lambda t, tag="": print("LOG", tag, t))
    prod = validate(mod.scrape(f, url))
    out = ROOT / "_research" / "out"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{site}_{prod['code']}.json"
    path.write_text(json.dumps(prod, indent=1, ensure_ascii=False), encoding="utf-8")
    avail = [v for v in prod["variants"] if v["available"]]
    prices = sorted({v["price"] for v in avail})
    print(f"OK {prod['site']} code={prod['code']} title={prod['title']!r}")
    print(f"   department={prod['department']!r} breadcrumbs={prod['breadcrumbs']}")
    print(f"   colors={[c['name'] for c in prod['colors']]} sizes({prod['size_name']})="
          f"{sorted({v['size'] for v in prod['variants']}, key=str)}")
    print(f"   variants={len(prod['variants'])} available={len(avail)} prices={prices[:5]}{'…' if len(prices) > 5 else ''}")
    print(f"   images: chung={len(prod['images'])} theo màu={ {c['name']: len(c['images']) for c in prod['colors']} }")
    first = (prod["colors"][0]["images"][:1] if prod["colors"] and prod["colors"][0]["images"] else prod["images"][:1])
    print(f"   ảnh đầu: {first}")
    print(f"   bullets={len(prod['bullets'])} details={list(prod['details'])}")
    print(f"   -> {path}")
    if "ctx" in pw_holder:
        pw_holder["ctx"].close()
        pw_holder["pw"].stop()


if __name__ == "__main__":
    main()
