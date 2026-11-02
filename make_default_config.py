"""Tạo build/config.default.json để đóng gói vào exe: cài đặt hiện tại nhưng bỏ key bí mật và đường dẫn riêng."""
import json
from pathlib import Path

import core

root = Path(__file__).resolve().parent
cfg = core.load_config()
cfg.update(r2_secret_access_key="", r2_access_key_id="", r2_account_id="", r2_bucket="", r2_public_url="",
           imgbb_api_key="",
           image_host="auto",   # máy mới chưa có key ImgBB / R2 -> up ảnh lên freeimage.host
           export_dir="", ebay_client_id="", ebay_client_secret="", gemini_api_key="", proxies_enc="")
cfg.pop("proxy_file", None)   # máy mới: tool tự tìm Proxy.txt cạnh tool / trên Desktop (để "" = đã xoá hết proxy)
(root / "build").mkdir(exist_ok=True)
(root / "build" / "config.default.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
print("build/config.default.json ok")
