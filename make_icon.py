"""Vẽ app.ico (icon file .exe) giống logo trên giao diện: túi mua hàng + mũi tên, nền xanh ngọc."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024           # vẽ to rồi thu nhỏ cho mịn
U = S / 32         # 1 đơn vị của viewBox 32x32 (giống favicon trong web/index.html)
STOPS = [(0, (52, 211, 153)), (.45, (16, 185, 129)), (1, (8, 145, 178))]


def grad(t):
    for (a, ca), (b, cb) in zip(STOPS, STOPS[1:]):
        if t <= b:
            k = (t - a) / (b - a)
            return tuple(round(x + (y - x) * k) for x, y in zip(ca, cb))
    return STOPS[-1][1]


def p(x, y):        # toạ độ trong khung 24x24 của nét vẽ (dịch 4,4 như favicon)
    return ((x + 4) * U, (y + 4) * U)


def main():
    bg = Image.new("RGBA", (S, S))
    px = bg.load()
    for y in range(S):
        for x in range(S):
            px[x, y] = grad((x + y) / (2 * S)) + (255,)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=8 * U, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(bg, mask=mask)

    d = ImageDraw.Draw(img)
    w = round(2.1 * U)
    white = (255, 255, 255, 255)
    # thân túi
    body = [p(4.5, 8.5), p(19.5, 8.5), p(18.3, 19.8), p(17.6, 21), p(6.4, 21), p(5.7, 19.8), p(4.5, 8.5)]
    d.line(body, fill=white, width=w, joint="curve")
    # quai túi
    d.arc((*p(8.8, 3.8), *p(15.2, 10.2)), 180, 360, fill=white, width=w)
    d.line([p(8.8, 7), p(8.8, 8.5)], fill=white, width=w)
    d.line([p(15.2, 7), p(15.2, 8.5)], fill=white, width=w)
    # mũi tên đi lên
    d.line([p(9.6, 17.2), p(14.2, 12.6)], fill=white, width=w)
    d.line([p(10.9, 12.6), p(14.2, 12.6), p(14.2, 15.9)], fill=white, width=w, joint="curve")
    r = w / 2   # bo tròn đầu nét
    for pt in (p(9.6, 17.2), p(10.9, 12.6), p(14.2, 15.9), p(4.5, 8.5), p(19.5, 8.5)):
        d.ellipse((pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r), fill=white)

    out = Path(__file__).resolve().parent / "app.ico"
    img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.resize((256, 256), Image.LANCZOS).save(out.with_suffix(".png"))
    print(out)


if __name__ == "__main__":
    main()
