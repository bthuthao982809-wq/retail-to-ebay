"""Viết tiêu đề eBay chuẩn SEO bằng Google Gemini (API key miễn phí ở aistudio.google.com).

Gom nhiều sản phẩm vào 1 lần gọi để đỡ tốn lượt của gói miễn phí. Tiêu đề trả về được kiểm tra lại:
tối đa 80 ký tự, không tên hãng, không ký tự lạ.
"""
import json
import re
import time

import core

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BATCH = 8   # số sản phẩm mỗi lần gọi

# Tên hãng / site không được xuất hiện trong tiêu đề (listing để Unbranded)
SITE_BRANDS = ["Theory", "Banana Republic", "Touch of Class", "Janie and Jack", "Janie & Jack", "Eileen Fisher",
               "Azazie", "Soho Home", "Soho House", "Talbots", "Personal Creations", "Gap", "Tommy Bahama", "prAna",
               "Sonos", "Belkin", "Sanus", "Sonance", "Hernest"]
# Tên hãng cũng là chữ thường gặp ("Thigh Gap", "Color Theory"): chỉ bỏ khi sản phẩm thuộc đúng site đó
GENERIC_BRANDS = {"gap": "bananarepublic", "theory": "theory"}
BANNED = ["free shipping", "l@@k", "wow", "authentic", "genuine", "nwt", "new with tags", "brand new"]

# Phần quy tắc viết tiêu đề — người dùng xem / sửa được trong Cài đặt (ô trống = dùng bản này)
DEFAULT_RULES = """You write eBay listing titles that rank well in eBay search (Cassini).

Rules for every title:
- At most 80 characters (aim for 65-80). Title Case. Plain ASCII words, no emojis, no ALL CAPS words, no symbols like ! * ~ @.
- Start with the most-searched words: department (Women's / Men's / Girls' / Boys' / Baby Girls' ...) when given, then the product type buyers search for (e.g. Wool Pants, Midi Dress, Comforter Set, Plush Toy).
- Then the strongest attributes from the data: material, style/cut, length, sleeve, pattern, occasion, theme.
- If there is room, end with the size range written exactly as the "size_range" field (e.g. "Size 0 to 12", "Size 6-12M to 12"); never merge or invent sizes.
- Never include any brand or store name (the listing is Unbranded).
- Never include trademarked product-line names (anything marked ™ or ® in product_name, or listed in "trademarks"); describe the item generically instead.
- Never invent facts that are not in the data.
- If the listing has several colors, do not name a single color; if it has exactly one color you may include it.
- No repeated words.
- If "personalized" is true, the title must contain the word "Personalized" (or "Custom").
- Write each product's title independently from the others.
- Each product has "max_len": the title must be at most that many characters. If "appended_after" is not empty,
  the tool will add it to the end of your title itself, so do NOT include those words (colors / size group) yourself.
  If "prepended_before" is not empty, the tool will put it at the start of your title itself, so do NOT include it."""

# Phần cố định tool luôn thêm vào sau quy tắc (danh sách cấm + định dạng trả về + dữ liệu sản phẩm)
FIXED_TAIL = """
- Forbidden brand / store names (never use): {brands}.
- Forbidden words (never use): {banned}.
- If a product has "must_keep", its title MUST contain every phrase in "must_keep" word for word (team names, player
  names, event names such as World Series / Champions). These are NOT brand names: never remove or shorten them;
  shorten other words (material, sleeve, size range) instead.
{extra}
Return ONLY a JSON array, one object per product, in the same order: [{{"id": "...", "title": "..."}}]

Products:
{products}"""


def rules(cfg):
    return (cfg.get("ai_prompt") or "").strip() or DEFAULT_RULES


def configured(cfg):
    return bool((cfg.get("gemini_api_key") or "").strip())


def product_brief(item, cfg):
    """Dữ liệu gửi cho AI về 1 listing (chỉ thông tin công khai của sản phẩm)."""
    prod = item["product"]
    vs = core.selling_variants(prod, cfg, item)
    colors = list(dict.fromkeys(v["color"] for v in vs if v["color"]))
    # Thứ tự size theo site (gom mọi màu), chỉ giữ size đang được chọn -> "XS to XXL" không bị ngược
    picked = {v["size"] for v in vs}
    sizes = [x for x in dict.fromkeys(v["size"] for v in prod["variants"] if v["size"]) if x in picked]
    cat, extra, _ = core.category_for(prod, cfg, item.get("category", ""), item.get("category_api"))
    aspects = item.get("aspects") if item.get("aspects_cat") == cat else None
    if len(colors) == 1:   # giống listing_rows: 1 màu là item specific Color, không phải trục biến thể
        extra = dict(extra, Color=core.clean_value(colors[0]))
    spec, _, _, _ = core.build_specifics(item, cfg, cat, extra, aspects, len(colors) > 1, bool(sizes))
    spec = {k: v for k, v in spec.items() if k.lower() != "brand"}
    api = item.get("category_api") or {}
    extra = item.get("title_extra", "")   # ", Màu, Nhóm size" người dùng đã chọn -> tool tự gắn vào cuối
    brand = core.title_brand(prod, cfg, item.get("title_brand_override"))
    return {
        "id": item["id"],
        "max_len": max_len(cfg) - (len(extra) + 2 if extra else 0) - (len(brand) + 1 if brand else 0),
        "appended_after": extra,
        "prepended_before": brand,
        "product_name": prod.get("title", ""),
        # Fanatics: không ghi Men's / Women's vào tiêu đề (tên site đã có khi cần) -> không đưa Department cho AI
        "department": "" if prod.get("site") == "fanatics" else prod.get("department", ""),
        "ebay_category": api.get("path") if api.get("id") == cat else "",
        "site_category": " > ".join(prod.get("breadcrumbs", [])),
        "item_specifics": spec,
        # Bỏ mã sản phẩm của site / tình trạng hàng / ngày giao (giống phần mô tả)
        "details": {k: str(v)[:120] for k, v in core.desc_lines(prod)[1].items()},
        # Đã chọn 1 màu / nhóm size (sẽ gắn vào cuối) -> không đưa cho AI, tránh lặp
        "colors": [] if extra else colors[:12] + (["..."] if len(colors) > 12 else []),
        "color_count": len(colors),
        "sizes": sizes[:20] + (["..."] if len(sizes) > 20 else []),
        # Chỉ khi trục đó đúng là size (Personal Creations có trục "Design"/"Style" -> không phải size)
        "size_range": "" if "size" not in (prod.get("size_name") or "size").lower() or not sizes
                      else (f"{sizes[0]} to {sizes[-1]}" if len(sizes) > 1 else sizes[0]),
        "option_name": prod.get("size_name") or "",
        "personalized": bool(prod.get("personalized")),
        "condition": "Refurbished (must say Refurbished in the title)" if core.is_refurb(prod) else "New",
        "trademarks": trademarks(prod.get("title", "")),
        "description": " ".join(core.desc_lines(prod)[0])[:600],
        "must_keep": keep_words(prod),
    }


# Đồ cổ động (Fanatics): tên đội / cầu thủ / giải đấu là từ khách tìm, KHÔNG phải tên hãng -> AI bắt buộc giữ
EVENT_RE = re.compile(r"\b(?:19|20)\d{2}\s+(?:[A-Z][\w.&'-]*\s+){0,5}?(?:World Series|Champions|Championship Series|"
                      r"Division Series|Champs)(?:\s+(?:Champions|Champs|MVP))?\b"   # "...Division Series Champions"
                      r"|\bCity Connect\b|\bCooperstown(?:\s+Collection)?\b")
# Chữ giới tính / lứa tuổi có sẵn trong tên Fanatics -> giữ (tool không tự thêm Men's / Women's cho Fanatics nữa)
GENDER_RE = re.compile(r"(?i)\b(?:Women's|Men's|Unisex|Youth|Kids'|Girls'|Boys'|Toddler|Infant|Preschool)(?!\w)")
SIZE_TOK = r"(?:Yth\s+)?(?:\d{0,2}X{0,3}[SML]|\d{1,2}XL|XXL|\d{1,2})"


def drop_partial_size(t, prod):
    """Fanatics: đuôi size cụt / sai ("Pullover S", "Tee Size 2XL", "S to") khi sản phẩm có nhiều size -> bỏ.
    Giữ nếu đúng cả khoảng size của sản phẩm ("Size S to 3XL" / "S to 3XL")."""
    sizes = list(dict.fromkeys(v.get("size") for v in prod.get("variants") or [] if v.get("size")))
    if len(sizes) < 2:
        return t
    m = re.search(rf"(?:\s+Size)?\s+{SIZE_TOK}(?:\s*(?:to|-)\s*(?:{SIZE_TOK})?)?\s*$", t)
    if not m:
        return t
    frag = re.sub(r"(?i)^\s*size\s+", "", m.group(0)).strip()
    full = f"{sizes[0]} to {sizes[-1]}"
    return t if frag.lower() == full.lower() else t[:m.start()].strip(" ,-")


def _fold(s):
    """Bỏ dấu (Díaz -> Diaz) để so / ghi tiêu đề chữ thường ASCII."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def keep_words(prod):
    """Cụm bắt buộc có trong tiêu đề AI: tên đội, tên cầu thủ (phần trước tên đội), giải đấu / sự kiện. Chỉ đồ cổ động
    (sản phẩm có Team trong thông số). Thứ tự: cầu thủ, đội, sự kiện."""
    name = _fold(prod.get("title") or "")
    # Fanatics ghi "Unisex" trong tên = loại chính của món (không phải nam / nữ riêng) -> giữ, AI hay bỏ
    g = GENDER_RE.search(name) if prod.get("site") == "fanatics" else None
    out = [g.group(0)] if g else []   # "Women's" / "Unisex" / "Youth" có trong tên site -> AI phải giữ
    teams = prod.get("teams")
    if teams is None:   # dữ liệu cào từ bản cũ: đọc ở thông số Team
        teams = [t.strip() for t in ((prod.get("details") or {}).get("Team") or "").split(",") if t.strip()]
    # "MLB Merchandise" / tên giải không phải đội; hàng chung quá nhiều đội (bộ thẻ đủ 30 đội) thì không bắt giữ đội nào
    teams = [t for t in teams if not re.search(r"(?i)merchandise|multi[- ]?team|league gear|^(mlb|nfl|nba|nhl|ncaa|mls|wnba|nascar|f1)$", t)]
    if not teams or len(teams) > 3:
        return out
    first = min((name.find(_fold(t)) for t in teams if _fold(t) in name), default=-1)
    if first > 0:   # "Yandy Díaz Tampa Bay Rays ..." -> cầu thủ "Yandy Diaz"
        player = re.sub(r"(?i)\s+vs\.?$", "", name[:first].strip(" -&,"))   # "Tampa Bay Rays vs." -> "Tampa Bay Rays"
        player = re.sub(r"\b[A-Z]{3,}\b", " ", player).strip(" -&,")   # bỏ chữ viết tắt (MLBPA, NBA) — không phải tên người
        player = re.sub(r"\s{2,}", " ", player)
        if player and not re.search(r"\d", player) and len(player.split()) <= 6:
            out.append(player)
    out += [_fold(t) for t in teams]
    out += [m.group(0) for m in EVENT_RE.finditer(name)][:2]
    return list(dict.fromkeys(out))


TYPE_WORD = re.compile(r"(?i)^(t-?shirts?|tees?|hats?|caps?|hoodies?|sweatshirts?|pullovers?|jackets?|jerseys?|polos?|"
                       r"shorts|pants|joggers|beanies?|tanks?|tops?|dress(es)?|socks|quarter-zip|full-zip|half-zip|"
                       r"vests?|shirts?|visors?|scarf|scarves|mugs?|flags?|blankets?)$")
DEPT_TOKEN = re.compile(r"(?i)^(men's|women's|unisex|youth|kids'?|boys'|girls'|toddler|infant|baby)$")


SAME_WORD = {"t-shirt": ("tee", "tees", "tshirt", "t-shirts"), "hoodie": ("hooded",), "hat": ("cap",),
             "sweatshirt": ("pullover", "crewneck"), "jersey": ("jerseys",)}


def _has_word(t, w):
    """Tiêu đề t có chữ w (hoặc chữ cùng nghĩa: T-Shirt ~ Tee, Hat ~ Cap) không."""
    f = _fold(t)
    return any(re.search(rf"(?i)(?<!\w){re.escape(x)}(?!\w)", f) for x in (w,) + SAME_WORD.get(w.lower(), ()))


def head_words(prod, keep=()):
    """2 chữ cuối tên trên site = loại hàng ("Block Art", "Trucker Hat", "Pullover Hoodie") -> giữ khi cắt tiêu đề.
    Bỏ tên hãng / tên đội / cầu thủ trước khi lấy ("...76ers Nike T-Shirt" -> "T-Shirt", không phải "Nike T-Shirt")."""
    name = re.sub(r"\s+-\s+[^-]+$", "", _fold(prod.get("title") or ""))
    for k in [prod.get("brand") or ""] + list(keep):
        if k:
            name = re.sub(rf"(?i)(?<!\w){re.escape(_fold(k))}(?!\w)", " ", name)
    words = [w for w in name.split() if w.lower() not in ("vs.", "vs", "&", "-", "x")]
    return " ".join(words[-2:])


def fit_keep(t, room, keep, head=""):
    """Cắt tiêu đề còn <= room ký tự theo thứ tự ưu tiên (đồ cổ động):
      luôn giữ: chữ giới tính đầu câu, loại hàng (T-Shirt, Hat... hoặc 2 chữ cuối tên site), tên đội, tên cầu thủ
      giữ nếu còn chỗ: giải đấu / sự kiện (2026 World Series...)
      bỏ trước: đuôi "Size ..." (bỏ cả cụm, không để "Size S to"), rồi các chữ khác từ cuối lên."""
    if len(t) <= room or not keep:
        return core.shorten_title(t, room)
    hw = _fold(head).lower().split() if head else []
    soft_head = len(hw) > 1 and bool(TYPE_WORD.match(hw[-1]))   # "Quarter-Zip Pullover": chữ đầu bỏ được trước giải đấu
    keep = list(keep) + ([head] if head and not soft_head else [])
    no_size = re.sub(rf"\s+(?:Size\s+\S.*|{SIZE_TOK}\s+to\s+\S+)$", "", t).strip(" ,-")   # "Size S to 3XL" / "S to 3XL"
    if len(no_size) <= room:
        return no_size
    words = no_size.split()
    low = [_fold(w).lower() for w in words]
    level = [2] * len(words)   # 0 = luôn giữ, 1 = giữ nếu còn chỗ, 2 = bỏ trước
    for k in keep:
        kw = _fold(k).lower().split()
        lv = 1 if EVENT_RE.fullmatch(_fold(k)) else 0
        for i in range(len(low) - len(kw) + 1):
            if low[i:i + len(kw)] == kw:
                for j in range(i, i + len(kw)):
                    level[j] = min(level[j], lv)
                break
    # chữ loại hàng: chỉ giữ chắc chữ CUỐI ("Quarter-Zip Pullover" -> giữ "Pullover", "Quarter-Zip" bỏ được nếu chật)
    types = [i for i, w in enumerate(words) if TYPE_WORD.match(w.strip(","))]
    if types:
        level[types[-1]] = 0
    if words and DEPT_TOKEN.match(words[0]):
        level[0] = 0
    if soft_head:   # chữ mô tả của loại hàng: bỏ sau các chữ thường, trước giải đấu; chữ loại hàng cuối luôn giữ
        for i in range(len(low) - len(hw) + 1):
            if low[i:i + len(hw)] == hw:
                for j in range(i, i + len(hw) - 1):
                    if level[j] == 2:
                        level[j] = 1.5
                level[i + len(hw) - 1] = 0
                break
    while len(" ".join(words)) > room:
        drop = next((i for i in range(len(words) - 1, -1, -1) if level[i] == 2), None)
        if drop is None:
            break
        words.pop(drop)
        level.pop(drop)

    def abbr(x):   # viết tắt tên giải (AL / NL, khách vẫn tìm vậy); vẫn dài thì NLCS / ALCS / NLDS / ALDS
        x = re.sub(r"\bAmerican League\b", "AL", re.sub(r"\bNational League\b", "NL", x))
        if len(x) > room:
            x = re.sub(r"\b(AL|NL) Championship Series\b", r"\1CS", re.sub(r"\b(AL|NL) Division Series\b", r"\1DS", x))
        return x

    s = " ".join(words).strip(" ,-&")
    if len(s) > room:
        s = abbr(s)
    while len(s) > room:   # vẫn dài: bỏ chữ mô tả của loại hàng ("Quarter-Zip") trước khi bỏ giải đấu
        drop = next((i for i in range(len(words) - 1, -1, -1) if level[i] == 1.5), None)
        if drop is None:
            break
        words.pop(drop)
        level.pop(drop)
        s = abbr(" ".join(words).strip(" ,-&"))
    if len(s) > room:   # vẫn dài: bỏ CẢ cụm giải đấu
        s = re.sub(r"\s{2,}", " ", EVENT_RE.sub(" ", s)).strip(" ,-&")
    return core.shorten_title(s, room)


def max_len(cfg):
    return int(cfg.get("title_max_len", 80) or 80)


def trademarks(name):
    """Từ đứng ngay trước ™ / ® trong tên sản phẩm (vd "Personalized Mini Flopsies™" -> "Flopsies") — dễ bị VeRO,
    không đưa vào tiêu đề. Chỉ lấy đúng từ đó: không kéo theo "Personalized" phía trước."""
    return [m for m in re.findall(r"([A-Za-z][\w'&\-]*)\s*[™®]", name or "")]


def clean_title(title, item, limit=80, brand=""):
    """Kiểm tra tiêu đề AI trả về: bỏ tên hãng / tên thương hiệu ™® / từ cấm / ký tự lạ, tối đa 80 ký tự."""
    t = (title or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"[^\w\s'&/,.\-+%\"]", " ", t)
    prod = item.get("product") or {}
    own = prod.get("brand", "")   # tên hãng của sản phẩm: luôn bỏ khỏi chữ AI viết (brand ở tham số mới là chữ gắn đầu)
    t = re.sub(r"\s{2,}", " ", t)
    brands = [b for b in SITE_BRANDS if GENERIC_BRANDS.get(b.lower(), prod.get("site")) == prod.get("site")]
    for b in brands + ([own] if own else []) + trademarks(prod.get("title", "")):
        t = re.sub(rf"(?i)\b{re.escape(b)}\b['’]?s?", " ", t)
    for w in BANNED:
        t = re.sub(rf"(?i)\b{re.escape(w)}\b", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -,/&")
    # Model nhỏ hay quên dấu nháy: Womens -> Women's, Girls -> Girls' ...
    t = re.sub(r"\b(Wo)?mens\b", lambda m: "Women's" if m.group(1) else "Men's", t, flags=re.I)
    t = re.sub(r"^(Wo)?[Mm]en\b(?!')", lambda m: "Women's" if m.group(1) else "Men's", t)   # "Men T-Shirt" -> "Men's T-Shirt"
    if prod.get("site") == "fanatics":   # Fanatics: AI tự thêm Men's / Women's đầu câu mà tên site không có chữ đó -> bỏ
        m = re.match(r"(?i)^(men's|women's|mens|womens)\s+", t)
        if m:
            w = "women" if m.group(1).lower().startswith("w") else "men"
            if not re.search(rf"(?i)\b{w}'?s\b", prod.get("title") or ""):
                t = t[m.end():]
    t = re.sub(r"^(Girls|Boys)\b(?!')", r"\1'", t)
    # Đồ cổ động: AI lỡ bỏ tên đội / cầu thủ / giải đấu -> tool tự thêm lại ngay sau chữ Men's / Women's...;
    # bỏ chữ rác AI chép từ dữ liệu ("MLB Multi-Team"); loại hàng (2 chữ cuối tên site) bị bỏ -> thêm lại ở cuối
    keep = keep_words(prod)
    if keep:
        t = re.sub(r"(?i)\b(?:MLB|NFL|NBA|NHL)?\s*Multi[- ]?Team\b", " ", t)
        t = re.sub(r"\s{2,}", " ", t).strip(" -,/&")
    missing = []
    for k in keep:
        if _fold(k).lower() in _fold(t).lower():
            continue
        yr = re.match(r"(?:19|20)\d{2}\b", k) if EVENT_RE.fullmatch(_fold(k)) else None
        if yr:   # AI viết thiếu / viết tắt giải ("2026 MLB Division Series", "2026 NL ... Series") -> thay tại chỗ bằng cụm đủ
            m = re.search(rf"\b{yr.group(0)}\b(?:\s+[\w.&'-]+){{0,6}}?\s+(?:Series|Champions|Champs|MVP)\b"
                          r"(?:\s+(?:Champions|Champs|MVP)\b)?", t)
            if m:
                t = t[:m.start()] + k + t[m.end():]
                continue
        missing.append(k)
    if missing:
        head = re.match(r"(?i)^((?:Men's|Women's|Unisex|Youth|Kids'|Boys'|Girls'|Toddler|Infant)\s+)?", t).group(1) or ""
        t = f"{head}{' '.join(missing)} {t[len(head):]}".strip()
    # Loại hàng: AI thiếu CHỮ CUỐI ("Block" mà không có "Art") -> thêm các chữ còn thiếu của nó vào cuối;
    # đã có chữ cuối ("Everybody T-Shirt" mà AI có "T-Shirt" / "Tee") -> thôi
    hw = (head_words(prod, keep) if keep else "").split()
    if hw and not _has_word(t, hw[-1]):
        t = f"{t} {' '.join(w for w in hw if not _has_word(t, w))}"
    extra = item.get("title_extra", "")
    # AI lỡ ghi màu / nhóm size ở CUỐI tiêu đề -> bỏ (tool tự gắn lại); giữa câu thì giữ ("Black Watch Plaid Skirt")
    for _ in range(3):
        for part in (p.strip() for p in extra.split(",") if p.strip()):
            t = re.sub(rf"(?i)[,\s-]+{re.escape(part)}\s*$", "", t).strip(" -,/&")
    if core.is_refurb(prod):   # hàng tân trang: tiêu đề luôn phải nói rõ, đúng 1 chữ "Refurbished"
        t = re.sub(r"(?i)\b(certified\s+)?refurb(ished)?\b\s*", "", t).strip(" -,/&")
        t = f"Refurbished {t}"
    room = limit - (len(extra) + 2 if extra else 0) - (len(brand) + 1 if brand else 0)
    if room < 15:   # phần ghép thêm quá dài (nhiều màu / tên hãng dài): bỏ phần ghép, không để tiêu đề cụt
        extra, room = "", limit - (len(brand) + 1 if brand else 0)
    t = fit_keep(t, room, keep, head_words(prod, keep))   # không cắt vào tên đội / cầu thủ / giải đấu / loại hàng
    if prod.get("site") == "fanatics":
        t = drop_partial_size(t, prod)
    t = f"{brand} {t}" if brand else t   # tên hãng (nếu bật) do tool gắn ở đầu — AI không tự viết
    return f"{t}, {extra}" if extra else t


# Model dự phòng: model chính quá tải (503) hoặc hết lượt phút (429) thì chuyển ngay sang model kế tiếp
# (bản lite nhanh, gói miễn phí rộng; dòng 2.5 Google đã ngưng cho tài khoản mới)
FALLBACK_MODELS = ["gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-flash-latest", "gemini-3.5-flash"]
DEFAULT_MODEL = FALLBACK_MODELS[0]


def _models(cfg):
    first = (cfg.get("gemini_model") or DEFAULT_MODEL).strip()
    return [first] + [m for m in FALLBACK_MODELS if m != first]


def _call(cfg, prompt, on_wait=None, rounds=3):
    """Gọi Gemini. Lần lượt thử từng model; cả lượt đều bận thì chờ rồi thử lượt mới (tối đa rounds lượt)."""
    from curl_cffi import requests

    body_base = {"contents": [{"parts": [{"text": prompt}]}],
                 "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}}
    headers = {"x-goog-api-key": cfg["gemini_api_key"].strip(), "Content-Type": "application/json"}
    last, busy_wait = "", 0
    for rnd in range(rounds):
        if rnd:
            wait = max(busy_wait, 20 * rnd)
            if on_wait:
                on_wait(wait)
            time.sleep(wait)
        for model in _models(cfg):
            try:
                # Tối đa 30 giây mỗi lần: model quá tải đôi khi treo không trả lời -> chuyển model khác
                r = requests.post(API.format(model=model), json=body_base, timeout=30, headers=headers)
            except Exception as e:
                last = f"{model}: {e}"
                continue
            if r.status_code in (404, 429) or r.status_code >= 500:
                # 503 = model quá tải, 429 = hết lượt của model này, 404 = tài khoản không có model -> thử model khác
                last = r.text
                m = re.search(r'"retryDelay":\s*"(\d+)', r.text)
                if m:
                    busy_wait = max(busy_wait, int(m.group(1)) + 3)
                continue
            j = r.json() if r.text.strip().startswith("{") else {}
            if r.status_code != 200:
                msg = (j.get("error") or {}).get("message") or r.text[:200]
                raise RuntimeError(f"Gemini lỗi HTTP {r.status_code}: {msg}")
            try:
                return j["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError):
                reason = ((j.get("promptFeedback") or {}).get("blockReason")
                          or ((j.get("candidates") or [{}])[0] or {}).get("finishReason"))
                raise RuntimeError(f"Gemini không trả lời ({reason})")
    if "PerDay" in last or "per day" in last.lower():
        raise RuntimeError("Gemini đã hết lượt miễn phí của hôm nay — mai chạy tiếp")
    msg = (re.search(r'"message":\s*"([^"]+)', last) or [None, ""])[1]
    raise RuntimeError(f"Các model Gemini đều đang bận / hết lượt — thử lại sau ít phút. {msg[:150]}")


def write_titles(items, cfg, log=None, should_stop=None):
    """Viết tiêu đề cho list listing. Trả về {id: tiêu đề mới}. Lỗi 1 lô thì ghi log, lô khác chạy tiếp."""
    log = log or (lambda *a, **k: None)
    if not configured(cfg):
        raise RuntimeError("Chưa có Gemini API key — lấy miễn phí ở aistudio.google.com rồi dán vào Cài đặt")
    extra = (cfg.get("ai_title_notes") or "").strip()
    out = {}
    for i in range(0, len(items), BATCH):
        if should_stop and should_stop():
            break
        chunk = items[i:i + BATCH]
        try:   # dữ liệu 1 sản phẩm lỗi chỉ bỏ lô đó, các lô sau vẫn chạy
            prompt = rules(cfg) + FIXED_TAIL.format(
                brands=", ".join(SITE_BRANDS), banned=", ".join(BANNED),
                extra=f"- Extra instructions from the seller: {extra}" if extra else "",
                products=json.dumps([product_brief(it, cfg) for it in chunk], ensure_ascii=False))
            text = _call(cfg, prompt, lambda s: log(f"  Gemini bắt chờ {s} giây (giới hạn gói miễn phí)…", "warn"))
            data = json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M))
        except Exception as e:
            log(f"  ✗ AI viết tiêu đề lô {i // BATCH + 1}: {e}", "err")
            continue
        by_id = {it["id"]: it for it in chunk}
        for d in data if isinstance(data, list) else []:
            if not isinstance(d, dict):   # AI trả sai dạng (vd list chữ) -> bỏ phần tử đó, không bỏ cả lượt
                continue
            it = by_id.get(str(d.get("id")))
            if not it:
                continue
            t = clean_title(str(d.get("title", "")), it, max_len(cfg), core.title_brand(it["product"], cfg, it.get("title_brand_override")))
            if len(t) >= 20:
                out[it["id"]] = t
        log(f"  AI đã viết {sum(1 for it in chunk if it['id'] in out)}/{len(chunk)} tiêu đề (lô {i // BATCH + 1})")
    return out


def test(cfg):
    if not configured(cfg):
        raise RuntimeError("Chưa điền Gemini API key")
    # Nút "Kiểm tra key": chỉ thử 1 lượt các model, không chờ -> trả lời trong vài giây
    from curl_cffi import requests

    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1", timeout=20,
                     headers={"x-goog-api-key": cfg["gemini_api_key"].strip()})
    if r.status_code in (400, 401, 403):
        msg = (re.search(r'"message":\s*"([^"]+)', r.text) or [None, r.text[:120]])[1]
        raise RuntimeError(f"Key sai hoặc bị khoá: {msg}")
    try:
        _call(cfg, 'Return ONLY this JSON: {"ok": true}', rounds=1)
        return "OK"
    except RuntimeError as e:
        return f"Key đúng, nhưng lúc này {e}"
