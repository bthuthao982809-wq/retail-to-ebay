"""Bản cửa sổ riêng (dùng để đóng gói .exe): server chạy nền, giao diện hiện trong cửa sổ WebView2.

Bản .exe cần key: máy chưa kích hoạt thì hiện màn hình kích hoạt trước, kích hoạt xong
mới bật server và mở app (server chưa bật thì cũng không vào được qua trình duyệt).
"""
import html
import os
import re
import subprocess
import sys
import threading
import webbrowser

import webview

import app
import core
import license as lic

TELEGRAM = "@toibihoviai"
TITLE = "Retail → eBay CSV"


def activation_reason():
    """None = được mở app; còn lại là lý do cần kích hoạt. Chạy từ mã nguồn thì không khoá."""
    if os.environ.get("RETAIL_TEST_ACTIVATION"):   # thử màn hình kích hoạt khi chạy từ mã nguồn
        return os.environ["RETAIL_TEST_ACTIVATION"]
    if not getattr(sys, "frozen", False) or not lic.has_public_key():
        return None
    ok, reason, _ = lic.check_saved(core.ROOT)
    return None if ok else reason


ACTIVATION_HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<style>
  * { box-sizing: border-box; }
  body { margin: 0; background: #f5f6f8; color: #1f2328; font: 14px/1.5 "Segoe UI", system-ui, sans-serif; }
  .card { margin: 22px; background: #fff; border: 1px solid #e3e5e9; border-radius: 12px; padding: 20px 22px; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .muted { color: #6a717c; font-size: 13px; }
  .reason { margin-top: 10px; padding: 10px 12px; border-radius: 8px; background: #fff1f0; color: #cf222e;
            border: 1px solid #ffcecb; white-space: pre-line; font-size: 13px; }
  .step { margin-top: 18px; font-weight: 600; }
  .mid { display: flex; gap: 10px; margin-top: 8px; align-items: center; }
  .mid input { flex: 1; height: 44px; font: 18px Consolas, monospace; text-align: center; letter-spacing: 1px;
               border: 1px solid #d4d7dd; border-radius: 8px; background: #f6f8fa; }
  textarea { width: 100%; margin-top: 8px; min-height: 76px; border: 1px solid #d4d7dd; border-radius: 8px;
             padding: 10px 12px; font: 13px Consolas, monospace; resize: vertical; outline: none; }
  textarea:focus { border-color: #1f6feb; box-shadow: 0 0 0 3px rgba(31,111,235,.15); }
  button { height: 40px; padding: 0 16px; border-radius: 8px; border: 1px solid #d4d7dd; background: #fff;
           font: inherit; cursor: pointer; }
  button:hover { background: #f3f4f6; }
  button.primary { background: #1f6feb; border-color: #1f6feb; color: #fff; font-weight: 600; }
  button.primary:hover { background: #1a5fd0; }
  .row { display: flex; gap: 10px; align-items: center; margin-top: 18px; }
  .row .grow { flex: 1; }
  a { color: #1f6feb; cursor: pointer; text-decoration: none; }
  #msg { margin-top: 10px; font-size: 13px; min-height: 20px; white-space: pre-line; }
  .ok { color: #1a7f37; } .err { color: #cf222e; }
</style></head>
<body><div class="card">
  <h1>Kích hoạt Retail → eBay CSV</h1>
  <div class="muted">Tool này cần key để chạy trên máy của bạn.</div>
  __REASON__
  <div class="step">Bước 1 — Gửi mã máy dưới đây qua Telegram <a id="tg1">__TG__</a></div>
  <div class="mid"><input id="mid" readonly value="…"><button id="copy">Chép mã</button></div>
  <div class="step">Bước 2 — Dán key nhận được vào đây</div>
  <textarea id="key" placeholder="Dán key vào đây" spellcheck="false"></textarea>
  <div id="msg"></div>
  <div class="row">
    <span class="grow muted">Hỗ trợ: Telegram <a id="tg2">__TG__</a></span>
    <button id="quit">Thoát</button>
    <button id="go" class="primary">Kích hoạt</button>
  </div>
</div>
<script>
  const $ = (s) => document.querySelector(s);
  const msg = (t, cls) => { $("#msg").textContent = t; $("#msg").className = cls || ""; };
  // Cầu nối pywebview có thể sẵn sàng trước hoặc sau khi script này chạy -> đợi đến khi có hàm api
  const ready = () => new Promise((resolve) => {
    const tick = () => (window.pywebview && window.pywebview.api && window.pywebview.api.machine_id)
      ? resolve(window.pywebview.api) : setTimeout(tick, 100);
    tick();
  });
  ready().then(async (api) => {
    $("#mid").value = await api.machine_id();
    $("#copy").onclick = async () => { await api.copy($("#mid").value); msg("Đã chép mã máy.", "ok"); };
    $("#tg1").onclick = $("#tg2").onclick = () => api.telegram();
    $("#quit").onclick = () => api.quit();
    const go = async () => {
      const key = $("#key").value.trim();
      if (!key) return msg("Chưa dán key.", "err");
      $("#go").disabled = true;
      const r = await api.activate(key);
      if (!r.ok) { $("#go").disabled = false; return msg(r.msg, "err"); }
      msg("✓ Kích hoạt thành công — " + r.msg + "\\nĐang mở app…", "ok");
      setTimeout(() => api.open_app(), 900);
    };
    $("#go").onclick = go;
    $("#key").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    // Dán key thì thay hẳn nội dung cũ (tránh dính với lần dán trước)
    $("#key").addEventListener("paste", () => { $("#key").value = ""; msg(""); });
    $("#key").focus();
  });
</script></body></html>"""


class ActivationApi:
    """Các hàm màn hình kích hoạt gọi được từ JavaScript (window.pywebview.api)."""

    def __init__(self, start_app):
        # Thuộc tính bắt đầu bằng "_" để pywebview không đưa sang JavaScript
        # (nó dò mọi thuộc tính công khai, gặp đối tượng cửa sổ là bị kẹt)
        self._start_app = start_app
        self._window = None

    def machine_id(self):
        return lic.machine_id()

    def copy(self, text):
        # Chép qua lệnh clip của Windows (clipboard của trình duyệt bị chặn với trang nội bộ)
        subprocess.run("clip", input=text.encode("utf-8"), check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True

    def telegram(self):
        webbrowser.open(f"https://t.me/{TELEGRAM.lstrip('@')}")

    def activate(self, text):
        # Key = 15 ký tự + "." + 86 ký tự. Dò đúng mẫu này trong đoạn dán, nên dán kèm lời nhắn
        # hay dán dính 2 lần vẫn nhận được; không có mẫu nào thì kiểm tra nguyên văn để báo lỗi cụ thể.
        candidates = re.findall(r"[A-Za-z0-9_-]{15}\.[A-Za-z0-9_-]{86}", "".join(text.split())) or [text]
        first_reason = ""
        for key in candidates:
            ok, reason, expiry = lic.check_key(key)
            if ok:
                lic.save_key(core.ROOT, key)
                return {"ok": True, "msg": lic.describe(expiry)}
            first_reason = first_reason or reason
        return {"ok": False, "msg": first_reason}

    def open_app(self):
        self._start_app()

    def quit(self):
        self._window.destroy()


def main():
    webview.settings["ALLOW_DOWNLOADS"] = True                    # nút "Tải CSV"
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True     # bấm ảnh mở bằng trình duyệt
    state = {}

    def start_server():
        server, url = app.start_server()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        state["server"] = server
        return url

    def pick_folder(initial):
        # Hộp chọn thư mục của chính cửa sổ app (nút "Xuất CSV")
        picked = window.create_file_dialog(webview.FileDialog.FOLDER, directory=initial or "")
        return picked[0] if picked else None

    app.PICK_FOLDER = pick_folder

    reason = activation_reason()
    if reason is None:
        window = webview.create_window(TITLE, start_server(), width=1360, height=920, min_size=(960, 640))
    else:
        def after_activation():
            url = start_server()
            window.title = TITLE
            window.resize(1360, 920)
            window.load_url(url)

        api = ActivationApi(after_activation)
        note = "" if reason == "Chưa kích hoạt." else f'<div class="reason">{html.escape(reason)}</div>'
        page = ACTIVATION_HTML.replace("__REASON__", note).replace("__TG__", TELEGRAM)
        window = webview.create_window(f"Kích hoạt {TITLE}", html=page, js_api=api, width=640, height=560,
                                       resizable=True, min_size=(560, 480))
        api._window = window
    # Dùng lại hồ sơ WebView2 cạnh tool (mặc định pywebview tạo hồ sơ mới mỗi lần mở -> mở chậm hơn)
    webview.start(private_mode=False, storage_path=str(core.ROOT / "webview-data"))

    # Đóng cửa sổ -> dừng việc đang chạy, đóng Chrome, tắt server
    if "server" in state:
        app.shutdown(state["server"])


if __name__ == "__main__":
    main()
