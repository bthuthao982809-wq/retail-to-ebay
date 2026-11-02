"""Kiểm tra bản build: đủ module site, có font, config mặc định sạch (không key / proxy), không lẫn file bí mật.
python check_exe.py build\\dist\\RetailToEbay   (thư mục bản onedir) hoặc python check_exe.py RetailToEbay.exe"""
import json
import sys
from pathlib import Path

import sites

if sys.argv[1:] == ["--hidden-imports"]:   # build_exe.bat lấy dòng này để gom đủ module site vào exe
    print(" ".join(f"--hidden-import sites.{n}" for n in sites.ORDER))
    sys.exit(0)

from PyInstaller.archive.readers import CArchiveReader  # noqa: E402

target = Path(sys.argv[1] if len(sys.argv) > 1 else "build/dist/RetailToEbay")
exe = target / "RetailToEbay.exe" if target.is_dir() else target
internal = exe.parent / "_internal"
a = CArchiveReader(str(exe))


def read(name):
    """File dữ liệu: bản onedir nằm trong _internal, bản 1 file nằm trong exe."""
    if target.is_dir():
        p = internal / name
        return p.read_bytes() if p.is_file() else None
    return a.extract(name.replace("/", "\\")) if name.replace("/", "\\") in a.toc else None


pyz = a.open_embedded_archive("PYZ.pyz") if "PYZ.pyz" in a.toc else \
    CArchiveReader(str(exe)).open_embedded_archive("PYZ.pyz")
problems = []
missing = [n for n in sites.ORDER if f"sites.{n}" not in pyz.toc]
if missing:
    problems.append(f"thieu site {missing}")
cfg = json.loads(read("config.default.json") or b"{}")
leaked = [k for k in ("ebay_client_id", "ebay_client_secret", "gemini_api_key", "proxies_enc", "proxy_file",
                      "r2_access_key_id", "r2_secret_access_key", "imgbb_api_key") if cfg.get(k)]
if not cfg:
    problems.append("khong co config.default.json")
if leaked:
    problems.append(f"config con key {leaked}")
if not read("web/index.html") or not read("web/fonts/inter-latin.woff2"):
    problems.append("thieu giao dien / font")
if not read("web/bestbuy_capture.js") or "camoufox.sync_api" not in pyz.toc:
    problems.append("thieu Camoufox / script doc Best Buy")
if target.is_dir():
    secret = [p.name for p in internal.rglob("*") if p.name in ("private.key", "keygen.py", "config.json", "da_cap.csv")]
    if secret:
        problems.append(f"lan file bi mat {secret}")
if problems:
    print("[!] LOI:", " | ".join(problems))
    sys.exit(1)
print(f"OK: du {len(sites.ORDER)} site, co giao dien + font, config mac dinh khong co key / proxy")
