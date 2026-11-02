#!/usr/bin/env python3
"""
keygen.py — CÔNG CỤ CỦA CHỦ TOOL. Tạo key RetailToEbay cho từng máy.

ĐỪNG BAO GIỜ CHIA SẺ FILE NÀY hoặc thư mục keys/ — ai có khoá bí mật là tự tạo
được key không giới hạn.

DÙNG LẦN ĐẦU
    python keygen.py --init
        Tạo keys/private.key (giữ riêng) và license_pub.py (khoá công khai, được
        đóng gói vào app). Chỉ chạy MỘT LẦN. Chạy lại sẽ đổi khoá và MỌI KEY ĐÃ
        CẤP TRƯỚC ĐÓ SẼ HẾT HIỆU LỰC.

CẤP KEY (hoặc bấm đúp "Cap key.bat")
    python keygen.py ABCD-EF12-3456-7890
        Key vĩnh viễn cho máy đó.
    python keygen.py ABCD-EF12-3456-7890 --days 365 --ten "Nam - kho HN"
        Key hết hạn sau 365 ngày.
    python keygen.py --list
        Xem lại các key đã cấp (keys/da_cap.csv).

QUY TRÌNH
    1. Người dùng mở app -> app hiện MÃ MÁY -> gửi mã đó cho bạn
    2. Bạn cấp key -> gửi key lại cho họ
    3. Họ dán key vào app -> dùng được trên đúng máy đó
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import license as lic

HERE = Path(__file__).resolve().parent
KEYS_DIR = HERE / "keys"
PRIVATE_FILE = KEYS_DIR / "private.key"
PUB_MODULE = HERE / "license_pub.py"
ISSUED_CSV = KEYS_DIR / "da_cap.csv"


def do_init(force: bool) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if PRIVATE_FILE.exists() and not force:
        print(f"[!] {PRIVATE_FILE} DA TON TAI.")
        print("    Tao lai se lam MOI KEY DA CAP HET HIEU LUC. That su muon lam lai thi them --force.")
        return 1

    KEYS_DIR.mkdir(exist_ok=True)
    private = Ed25519PrivateKey.generate()
    raw_priv = private.private_bytes(encoding=serialization.Encoding.Raw,
                                     format=serialization.PrivateFormat.Raw,
                                     encryption_algorithm=serialization.NoEncryption())
    raw_pub = private.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                                format=serialization.PublicFormat.Raw)
    PRIVATE_FILE.write_text(raw_priv.hex(), encoding="utf-8")
    PUB_MODULE.write_text(
        '"""Khoá CÔNG KHAI của RetailToEbay — sinh bởi keygen.py --init.\n\n'
        'An toàn khi đóng gói vào app: chỉ dùng để KIỂM TRA key, không tạo được key mới.\n'
        'Khoá bí mật nằm ở keys/private.key, không bao giờ được đóng gói hay chia sẻ."""\n'
        f'PUBLIC_KEY = "{raw_pub.hex()}"\n', encoding="utf-8")
    print("Da tao xong.")
    print(f"  {PRIVATE_FILE}   <- GIU RIENG, dung chia se, nen sao luu")
    print(f"  {PUB_MODULE}  <- se duoc dong goi vao app")
    return 0


def load_private():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if not PRIVATE_FILE.exists():
        print(f"[!] Chua co {PRIVATE_FILE}. Chay 'python keygen.py --init' truoc.")
        sys.exit(1)
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(PRIVATE_FILE.read_text(encoding="utf-8").strip()))


def do_issue(machine_text: str, days: int | None, name: str, only_key: bool = False) -> int:
    try:
        machine = lic.parse_machine_id(machine_text)
    except ValueError as e:
        print(f"LOI: {e}" if only_key else f"[!] {e}")
        return 1

    expiry = None if not days else date.today() + timedelta(days=days)
    payload = lic.build_payload(machine, expiry)
    key = lic.format_key(payload, load_private().sign(payload))

    KEYS_DIR.mkdir(exist_ok=True)
    new = not ISSUED_CSV.exists()
    with open(ISSUED_CSV, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["Ngay cap", "Ma may", "Ten", "Han dung", "Key"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), machine_text.upper(), name,
                    expiry.isoformat() if expiry else "vinh vien", key])

    if only_key:  # cho "Cap key.bat" bắt lấy, không in gì khác
        print(key)
        return 0
    print("=" * 72)
    print(f"Ma may  : {machine_text.upper()}")
    print(f"Ten     : {name or '(khong ghi)'}")
    print(f"Han dung: {expiry.strftime('%d/%m/%Y') if expiry else 'vinh vien'}")
    print("=" * 72)
    print("KEY (gui nguyen chuoi nay cho nguoi dung):\n")
    print(key)
    print("\n" + "=" * 72)
    print(f"Da ghi vao {ISSUED_CSV}")
    return 0


def do_list() -> int:
    if not ISSUED_CSV.exists():
        print("Chua cap key nao.")
        return 0
    with open(ISSUED_CSV, encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        print("Chua cap key nao.")
        return 0
    print("%-17s %-22s %-18s %s" % ("NGAY CAP", "MA MAY", "TEN", "HAN DUNG"))
    print("-" * 78)
    for r in rows[1:]:
        if len(r) >= 4:
            print("%-17s %-22s %-18s %s" % (r[0], r[1], r[2][:18], r[3]))
    print(f"\nTong: {len(rows) - 1} key. Chi tiet (ca key) trong {ISSUED_CSV}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Tao key ban quyen cho RetailToEbay (cong cu cua chu tool)")
    ap.add_argument("machine", nargs="?", help="Ma may nguoi dung gui, dang ABCD-EF12-3456-7890")
    ap.add_argument("--init", action="store_true", help="Tao cap khoa lan dau")
    ap.add_argument("--force", action="store_true", help="Buoc tao lai cap khoa")
    ap.add_argument("--days", type=int, default=0, help="So ngay hieu luc (0 hoac bo trong = vinh vien)")
    ap.add_argument("--ten", default="", help="Ghi chu: cap cho ai")
    ap.add_argument("--list", action="store_true", help="Xem cac key da cap")
    ap.add_argument("--chi-key", action="store_true", help="Chi in ra key (cho 'Cap key.bat')")
    a = ap.parse_args()

    if a.init:
        sys.exit(do_init(a.force))
    if a.list:
        sys.exit(do_list())
    if not a.machine:
        ap.print_help()
        print('\nVi du:  python keygen.py ABCD-EF12-3456-7890 --days 365 --ten "Nam"')
        sys.exit(1)
    sys.exit(do_issue(a.machine, a.days, a.ten, a.chi_key))


if __name__ == "__main__":
    main()
