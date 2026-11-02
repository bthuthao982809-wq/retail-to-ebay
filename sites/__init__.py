"""Danh sách site: mỗi module trong thư mục này là 1 tab của tool."""
import importlib

ORDER = ["theory", "bananarepublic", "touchofclass", "janieandjack", "eileenfisher",
         "azazie", "sohohome", "talbots", "personalcreations", "tommybahama", "prana", "sonos", "hollister",
         "fanatics", "bestbuy"]


def load_all():
    """{key: module} theo thứ tự tab. Module chưa viết xong thì bỏ qua."""
    out = {}
    for name in ORDER:
        try:
            mod = importlib.import_module(f"sites.{name}")
        except ModuleNotFoundError:
            continue
        out[mod.KEY] = mod
    return out
