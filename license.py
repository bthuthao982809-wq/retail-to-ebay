"""license.py — kiểm tra key bản quyền của RetailToEbay (cùng cơ chế với tool WM CHECK GIÁ).

Cách hoạt động
    Chủ tool giữ KHOÁ BÍ MẬT (keys/private.key, không bao giờ chia sẻ) để ký key.
    App chỉ chứa KHOÁ CÔNG KHAI (license_pub.py) nên không ai tự tạo key được,
    kể cả khi mổ file .exe ra.

Giới hạn
    Key chạy offline trên máy người dùng thì về lý thuyết vẫn có thể bị vá bỏ đoạn
    kiểm tra. Key chặn việc chuyển tay tuỳ tiện: người khác nhận file về không mở
    được nếu không xin key.

Khoá theo máy
    Key gắn với MachineGuid của bản Windows đó. Cài lại Windows thì đổi mã máy,
    cần cấp key mới. Mã máy của tool này khác mã máy của các tool khác (MerchToCSV, WM CHECK GIÁ...)
    trên cùng một máy, nên key của tool này không dùng cho tool kia được và ngược lại.
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
from datetime import date
from pathlib import Path

KEY_VERSION = 1
EPOCH = date(2020, 1, 1)
LICENSE_FILE = "license.dat"
APP_SALT = b"RETAILTOEBAY|"


# ---------------------------------------------------------------- mã máy

def _raw_machine_id() -> bytes:
    """Định danh máy, ổn định qua các lần khởi động (ưu tiên MachineGuid của Windows)."""
    seed = b""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            seed = str(winreg.QueryValueEx(k, "MachineGuid")[0]).encode()
    except Exception:
        pass
    if not seed:
        import platform
        import uuid
        seed = f"{platform.node()}|{uuid.getnode()}".encode()
    return hashlib.sha256(APP_SALT + seed).digest()[:8]


def machine_id() -> str:
    """Mã máy dạng người dùng đọc/chép được: ABCD-EF12-3456-7890."""
    h = _raw_machine_id().hex().upper()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


def parse_machine_id(text: str) -> bytes:
    clean = "".join(c for c in (text or "").upper() if c in "0123456789ABCDEF")
    if len(clean) != 16:
        raise ValueError("Mã máy phải có 16 ký tự hex (dạng ABCD-EF12-3456-7890)")
    return bytes.fromhex(clean)


# ---------------------------------------------------------------- key

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def build_payload(machine: bytes, expiry: date | None) -> bytes:
    """11 byte: phiên bản(1) + mã máy(8) + hạn dùng(2, 0 = vĩnh viễn)."""
    if expiry is None:
        days = 0
    else:
        days = (expiry - EPOCH).days
        if not 0 < days < 65535:
            raise ValueError("Hạn dùng nằm ngoài khoảng cho phép")
    return struct.pack("<B8sH", KEY_VERSION, machine, days)


def read_payload(raw: bytes) -> tuple[int, bytes, date | None]:
    ver, machine, days = struct.unpack("<B8sH", raw)
    return ver, machine, (EPOCH.fromordinal(EPOCH.toordinal() + days) if days else None)


def format_key(payload: bytes, signature: bytes) -> str:
    return f"{_b64e(payload)}.{_b64e(signature)}"


def check_key(key: str, public_key_bytes: bytes | None = None,
              today: date | None = None) -> tuple[bool, str, date | None]:
    """Trả về (hợp lệ, lý do, hạn dùng). Kiểm tra: định dạng -> chữ ký -> đúng máy -> còn hạn."""
    if public_key_bytes is None:
        public_key_bytes = _public_key()
    if not public_key_bytes:
        return False, "App chưa được gắn khoá công khai (lỗi khi đóng gói).", None

    parts = "".join((key or "").split()).split(".")
    if len(parts) != 2:
        return False, "Key sai định dạng.", None
    try:
        payload, signature = _b64d(parts[0]), _b64d(parts[1])
        ver, machine, expiry = read_payload(payload)
    except Exception:
        return False, "Key sai định dạng hoặc bị cắt mất ký tự.", None
    if ver != KEY_VERSION:
        return False, f"Key thuộc phiên bản {ver}, app cần phiên bản {KEY_VERSION}.", None

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature, payload)
        except InvalidSignature:
            return False, "Chữ ký không hợp lệ — key giả, bị sửa, hoặc là key của tool khác.", None
    except ImportError:
        return False, "Thiếu thư viện mã hoá trong bản đóng gói.", None

    if machine != _raw_machine_id():
        return False, f"Key này cấp cho máy khác.\nMáy này: {machine_id()}", None
    if expiry is not None and (today or date.today()) > expiry:
        return False, f"Key đã hết hạn ngày {expiry:%d/%m/%Y}.", expiry
    return True, "", expiry


# ---------------------------------------------------------------- khoá công khai + lưu key

def _public_key() -> bytes | None:
    try:
        from license_pub import PUBLIC_KEY
        return bytes.fromhex(PUBLIC_KEY) if PUBLIC_KEY else None
    except Exception:
        return None


def has_public_key() -> bool:
    return bool(_public_key())


def _stable_dir() -> Path | None:
    """Chỗ lưu key không phụ thuộc vị trí file .exe: đổi thư mục / cập nhật bản mới không bị hỏi key lại."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return Path(base) / "RetailToEbay" if base else Path.home() / ".retailtoebay"


def _places(folder: Path) -> list[Path]:
    out = [folder / LICENSE_FILE]
    d = _stable_dir()
    if d:
        out.append(d / LICENSE_FILE)
    return out


def save_key(folder: Path, key: str) -> None:
    """Ghi key vào cả hai chỗ, một chỗ ghi hỏng vẫn còn chỗ kia."""
    for p in _places(folder):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(key.strip(), encoding="utf-8")
        except OSError:
            pass


def check_saved(folder: Path) -> tuple[bool, str, date | None]:
    """Thử từng chỗ lưu, lấy chỗ đầu tiên hợp lệ; gặp key hợp lệ thì chép bù vào chỗ còn thiếu."""
    last_reason = ""
    places = _places(folder)
    for p in places:
        try:
            saved = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not saved:
            continue
        ok, reason, expiry = check_key(saved)
        if ok:
            if any(not q.exists() for q in places):
                save_key(folder, saved)
            return True, "", expiry
        last_reason = reason
    return False, last_reason or "Chưa kích hoạt.", None


def days_left(expiry: date | None) -> int | None:
    return None if expiry is None else (expiry - date.today()).days


def describe(expiry: date | None) -> str:
    if expiry is None:
        return "Bản quyền: vĩnh viễn"
    return f"Bản quyền đến {expiry:%d/%m/%Y} (còn {days_left(expiry)} ngày)"
