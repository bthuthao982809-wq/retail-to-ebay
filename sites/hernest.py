"""Hernest: SSR __sandwich__, một SKU cụ thể theo -p-ID.html của link đầu vào.

SKU anh em có trang/giá/ảnh riêng và có thể có ba trục (Height/Shape/Style).
Không suy giá/tồn kho của SKU khác từ skuList chỉ chứa liên kết.
"""
import json
import re
from urllib.parse import urlsplit

from sites.base import SkipProduct, clean_text, dedup, strip_brand, to_price, validate

KEY = "hernest"
NAME = "Hernest"
NEEDS_PROXY = False
URL_RE = re.compile(r"https?://(?:www\.)?hernest\.com/products/[\w-]+-p-\d+\.html(?:\?[^\s<>\"']*)?", re.I)
EXAMPLES = ["https://www.hernest.com/products/elliot-16-inch-solid-oak-side-table-p-8432.html"]


def product_code(url):
    m = re.search(r"-p-(\d+)\.html(?:[?#]|$)", url, re.I)
    return m.group(1) if m else None


def _data(html, url):
    m = re.search(r"window\.__sandwich__\s*=\s*", html)
    if not m:
        raise SkipProduct("Hernest không trả dữ liệu sản phẩm (trang lỗi hoặc bị chặn)")
    try:
        state = json.JSONDecoder().raw_decode(html[m.end():])[0]
    except (ValueError, TypeError):
        raise SkipProduct("Dữ liệu sản phẩm Hernest không hợp lệ")
    code = product_code(url)
    for page in (state.get("page") or {}).values():
        data = page.get("data") or {}
        info = data.get("productInfo") or {}
        if str(info.get("productsSkuId") or "") == code:
            return data
    raise SkipProduct("Hernest trả trang khác mã sản phẩm trong link — bỏ qua để tránh lấy nhầm")


def _text(value):
    if isinstance(value, list):
        return "; ".join(filter(None, (_text(x) for x in value)))
    if not isinstance(value, (str, int, float)):
        return ""
    return clean_text(str(value))


def _image(url):
    if not isinstance(url, str):
        return ""
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or not (p.hostname == "yitashop.com" or p.hostname.endswith(".yitashop.com")):
        return ""
    if not re.search(r"\.(?:jpe?g|png|webp|avif)$", p.path, re.I):
        return ""
    return url


def scrape(f, url):
    data = _data(f.get(url), url)
    info = data["productInfo"]
    if info.get("currency") != "USD":
        raise SkipProduct("Giá Hernest không phải USD — không dùng giá này để xuất")
    price = to_price(info.get("salePrice"))
    if price is None or price <= 0:
        raise SkipProduct("Không đọc được giá bán Hernest")
    brand = _text(info.get("brandName")) or NAME
    title = strip_brand(_text(info.get("productsName")), brand)
    if not title:
        raise SkipProduct("Hernest không có tên sản phẩm")
    details, attrs = {}, {}
    for group in ("saleAttrs", "paramsAttrs", "overviewAttrs"):
        for a in info.get(group) or []:
            key, val = _text(a.get("productsOptionsName")), _text(a.get("productsOptionsValuesName"))
            if key and val:
                details[key] = val
                if group == "saleAttrs":
                    attrs[key.lower()] = val
    spec = info.get("specInfo") or {}
    for a in spec.get("specItemList") or []:
        key, val = _text(a.get("name")), _text(a.get("value"))
        if key and val:
            details[key] = val
    color = attrs.get("color") or attrs.get("finish") or ""
    # Style của cây thông thật sự là màu lá; giữ nguyên nhãn và không gán Style thành màu tùy tiện.
    if not color:
        color = details.get("Tree Color", "")
    size = attrs.get("height") or attrs.get("size") or ""
    size_name = "Height" if "height" in attrs else "Size" if size else ""
    gallery = []
    for im in info.get("sourceImageList") or info.get("imageList") or []:
        if str(im.get("type")) != "1":
            continue
        gallery.extend(filter(None, [_image(im.get("largeImage") or im.get("imagePath")), _image(im.get("overlapPath"))]))
    # Đưa ảnh kích thước vào bộ ảnh, giữ mô tả kỹ thuật cùng đúng SKU.
    gallery.extend(filter(None, (_image(x) for x in spec.get("specImageList") or [spec.get("specImagePath")])))
    gallery = dedup(gallery)
    if not gallery:
        raise SkipProduct("Hernest không trả ảnh sản phẩm")
    desc = [_text(info.get("collectionDesc")), _text(info.get("productsDescription"))]
    desc += [_text(x.get("title")) for x in info.get("sellingPoint") or []]
    bullets = dedup(x for x in desc if x)
    if not bullets and info.get("feedDesc"):
        bullets = [_text(info["feedDesc"])]
    # Không đoán còn hàng khi stock vắng. Cho phép đặt hàng hết kho chỉ khi site nói rõ.
    stock = to_price(info.get("stocks"))
    status = str(info.get("productsStatus"))
    can_order = str(info.get("isOutStockCanBuy")) == "1"
    # status=3 vẫn có Add to Cart: hàng cho đặt trước/giao chậm, không phải status=2 Sold Out.
    available = (status == "1" and ((stock is not None and stock > 0) or can_order)) or (status == "3" and can_order)
    if status == "3" and available:
        details["Availability"] = "Available to order; delayed delivery"
    old = to_price(info.get("marketPrice"))
    code = product_code(url)
    return validate({
        "site": KEY, "code": code, "url": url.split("?")[0].split("#")[0],
        "title": title, "brand": brand, "bullets": bullets, "details": details,
        "breadcrumbs": [_text(x.get("title")) for x in data.get("navBread") or []
                        if x.get("title") and _text(x["title"]).lower() != "home" and not str(x["title"]).upper().startswith("SKU")],
        "department": "", "size_name": size_name,
        "colors": [{"name": color, "images": gallery}] if color else [], "images": gallery,
        "variants": [{"color": color, "size": size, "price": price,
                      "list_price": old if old and old > price else None,
                      "available": available, "id": code}],
    })
