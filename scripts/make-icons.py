#!/usr/bin/env python3
"""ミュージックの記号アイコン (16x16、塗りだけ) を作る。

GTK 4.22 の symbolic は塗りを強制するので、線は太さを持った塗りの図形に
展開する。1 アイコン = 1 つの <path> (nonzero)。図形は画面座標で時計回りに
描き、穴は反時計回りにする。穴どうしは重ねない (重なると塗りに戻る)。
"""
import math
import sys
from pathlib import Path

OUT = Path(sys.argv[1])
W = 1.5  # 16px での線の太さ (SF Symbols の regular 相当)


def f(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def pt(p):
    return f"{f(p[0])} {f(p[1])}"


def shoelace(poly):
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1])) / 2


def oriented(poly, hole=False):
    poly = list(poly)
    a = shoelace(poly)
    if (a < 0) != hole:
        poly.reverse()
    return poly


def polygon(poly, hole=False):
    poly = oriented(poly, hole)
    return "M" + " L".join(pt(p) for p in poly) + " Z"


def rounded(poly, radii, hole=False):
    """角を丸めた多角形。radii は数か頂点ごとの列。"""
    poly = list(poly)
    if isinstance(radii, (int, float)):
        radii = [radii] * len(poly)
    a = shoelace(poly)
    if (a < 0) != hole:
        poly.reverse()
        radii = list(reversed(radii))
    n = len(poly)
    out = []
    for i in range(n):
        A, P, B = poly[i - 1], poly[i], poly[(i + 1) % n]
        r = radii[i]
        ux, uy = A[0] - P[0], A[1] - P[1]
        vx, vy = B[0] - P[0], B[1] - P[1]
        lu, lv = math.hypot(ux, uy), math.hypot(vx, vy)
        ux, uy, vx, vy = ux / lu, uy / lu, vx / lv, vy / lv
        cos = max(-1.0, min(1.0, ux * vx + uy * vy))
        theta = math.acos(cos)
        if r <= 0 or theta < 1e-3 or abs(theta - math.pi) < 1e-3:
            out.append(("L", P))
            continue
        t = r / math.tan(theta / 2)
        t = min(t, lu / 2, lv / 2)
        r_eff = t * math.tan(theta / 2)
        p1 = (P[0] + ux * t, P[1] + uy * t)
        p2 = (P[0] + vx * t, P[1] + vy * t)
        cross = (P[0] - A[0]) * (B[1] - P[1]) - (P[1] - A[1]) * (B[0] - P[0])
        sweep = 1 if cross > 0 else 0
        out.append(("L", p1))
        out.append(("A", r_eff, sweep, p2))
    d = ""
    first = True
    for seg in out:
        if seg[0] == "L":
            d += ("M" if first else " L") + pt(seg[1])
        else:
            d += f" A{f(seg[1])} {f(seg[1])} 0 0 {seg[2]} {pt(seg[3])}"
        first = False
    return d + " Z"


def circle(cx, cy, r, hole=False):
    s = 0 if hole else 1
    return (f"M{f(cx + r)} {f(cy)} A{f(r)} {f(r)} 0 1 {s} {f(cx - r)} {f(cy)} "
            f"A{f(r)} {f(r)} 0 1 {s} {f(cx + r)} {f(cy)} Z")


def ring(cx, cy, r, w=W):
    return circle(cx, cy, r + w / 2) + " " + circle(cx, cy, r - w / 2, hole=True)


def capsule(p0, p1, w=W):
    """両端の丸い線分 (時計回り)。"""
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    h = w / 2
    if length < 1e-6:
        return circle(x0, y0, h)
    nx, ny = dy / length * h, -dx / length * h  # 進行方向の左 (画面の上側)
    a = (x0 + nx, y0 + ny)
    b = (x1 + nx, y1 + ny)
    c = (x1 - nx, y1 - ny)
    e = (x0 - nx, y0 - ny)
    return (f"M{pt(a)} L{pt(b)} A{f(h)} {f(h)} 0 0 1 {pt(c)} "
            f"L{pt(e)} A{f(h)} {f(h)} 0 0 1 {pt(a)} Z")


def polyline(points, w=W):
    return " ".join(capsule(a, b, w) for a, b in zip(points, points[1:]))


def arc_band(cx, cy, r, a0, a1, w=W):
    """太さ w の円弧 (両端は丸)。角度は画面座標で +x から時計回り。"""
    h = w / 2
    ro, ri = r + h, r - h
    t0, t1 = math.radians(a0), math.radians(a1)
    large = 1 if abs(a1 - a0) > 180 else 0

    def p(rr, t):
        return (cx + rr * math.cos(t), cy + rr * math.sin(t))

    return (f"M{pt(p(ro, t0))} A{f(ro)} {f(ro)} 0 {large} 1 {pt(p(ro, t1))} "
            f"A{f(h)} {f(h)} 0 0 1 {pt(p(ri, t1))} "
            f"A{f(ri)} {f(ri)} 0 {large} 0 {pt(p(ri, t0))} "
            f"A{f(h)} {f(h)} 0 0 1 {pt(p(ro, t0))} Z")


def rrect(x0, y0, x1, y1, r, hole=False):
    return rounded([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], r, hole)


def rrect_ring(x0, y0, x1, y1, r, w=W):
    h = w / 2
    return (rrect(x0 - h, y0 - h, x1 + h, y1 + h, r + h) + " "
            + rrect(x0 + h, y0 + h, x1 - h, y1 - h, max(0.01, r - h), hole=True))


def ellipse(cx, cy, rx, ry, rot_deg, n=40, hole=False):
    t = math.radians(rot_deg)
    c, s = math.cos(t), math.sin(t)
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        x, y = rx * math.cos(a), ry * math.sin(a)
        pts.append((cx + x * c - y * s, cy + x * s + y * c))
    return polygon(pts, hole)


def rotate(points, deg, cx=8.0, cy=8.0):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return [(cx + (x - cx) * c - (y - cy) * s, cy + (x - cx) * s + (y - cy) * c) for x, y in points]


def cross(cx, cy, arm, w, deg=45, hole=False):
    """丸い端の × (1 つの多角形。穴に使えるように)。"""
    h = w / 2
    base = [(-h, -arm), (h, -arm), (h, -h), (arm, -h), (arm, h), (h, h),
            (h, arm), (-h, arm), (-h, h), (-arm, h), (-arm, -h), (-h, -h)]
    pts = rotate([(cx + x, cy + y) for x, y in base], deg, cx, cy)
    radii = [h * 0.98, h * 0.98, 0.25] * 4
    return rounded(pts, radii, hole)


def hull(points):
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cr(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cr(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cr(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def teardrop(cx, cy, r, tip, hole=False):
    pts = [(cx + r * math.cos(2 * math.pi * i / 36), cy + r * math.sin(2 * math.pi * i / 36)) for i in range(36)]
    return polygon(hull(pts + [tip]), hole)


def bezier(p0, p1, p2, p3, n=12):
    out = []
    for i in range(n + 1):
        t = i / n
        m = 1 - t
        out.append((m ** 3 * p0[0] + 3 * m * m * t * p1[0] + 3 * m * t * t * p2[0] + t ** 3 * p3[0],
                    m ** 3 * p0[1] + 3 * m * m * t * p1[1] + 3 * m * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def arc_pts(cx, cy, r, a0, a1, n=8):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def star_points(cx, cy, ro, ri):
    pts = []
    for i in range(10):
        r = ro if i % 2 == 0 else ri
        a = math.radians(-90 + i * 36)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def svg(name, *parts):
    d = " ".join(p for p in parts if p)
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            f'<path d="{d}" fill="#2e3436"/></svg>\n')


icons = {}

# 虫めがね (検索)
icons["search"] = svg("search", ring(6.8, 6.8, 4.9), capsule((10.7, 10.7), (14.3, 14.3), 1.9))

# 家 (ホーム)。屋根は線、戸は塗り
icons["home"] = svg(
    "home",
    polyline([(1.3, 7.7), (8.0, 1.8), (14.7, 7.7)], 1.55),
    polyline([(3.3, 6.2), (3.3, 13.6), (12.7, 13.6), (12.7, 6.2)], 1.5),
    rrect(6.5, 9.0, 9.5, 13.6, 0.7),
)

# 電波 (ラジオ)
icons["radio"] = svg(
    "radio",
    circle(8, 8, 1.75),
    arc_band(8, 8, 3.7, -48, 48), arc_band(8, 8, 3.7, 132, 228),
    arc_band(8, 8, 6.5, -50, 50), arc_band(8, 8, 6.5, 130, 230),
)

# 時計 (最近再生した項目)
icons["recent"] = svg(
    "recent",
    ring(8, 8, 6.25),
    capsule((8, 8.2), (8, 4.1), 1.5),
    capsule((8, 8.2), (10.9, 9.9), 1.5),
)

# 8 分音符 (曲)
icons["note"] = svg(
    "note",
    ellipse(5.7, 12.0, 2.75, 2.1, -22),
    capsule((7.75, 12.0), (7.75, 2.1), 1.5),
    rounded([(7.2, 1.3), (13.6, 3.2), (13.6, 6.2), (7.2, 4.4)], [0.6, 0.9, 0.9, 0.3]),
)

# 音符とリスト (プレイリスト)
icons["note-list"] = svg(
    "note-list",
    capsule((1.6, 3.2), (8.6, 3.2)),
    capsule((1.6, 6.8), (8.6, 6.8)),
    capsule((1.6, 10.4), (5.6, 10.4)),
    ellipse(9.9, 12.7, 2.25, 1.75, -22),
    capsule((11.6, 12.7), (11.6, 4.0), 1.45),
    rounded([(11.1, 3.2), (15.0, 4.3), (15.0, 6.9), (11.1, 5.8)], [0.5, 0.7, 0.7, 0.3]),
)

# 3x3 の格子 (すべてのプレイリスト)
cells = []
for yy in (1.3, 6.15, 11.0):
    for xx in (1.3, 6.15, 11.0):
        cells.append(rrect(xx, yy, xx + 3.7, yy + 3.7, 1.0))
icons["grid"] = svg("grid", *cells)

# 箇条書き (次に再生)
icons["queue"] = svg(
    "queue",
    *[circle(2.3, y, 1.3) for y in (3.4, 8.0, 12.6)],
    *[capsule((5.7, y), (14.5, y), 1.5) for y in (3.4, 8.0, 12.6)],
)

# 吹き出しと引用符 (歌詞)。吹き出しは塗り、引用符は穴
icons["lyrics"] = svg(
    "lyrics",
    rrect(0.9, 1.6, 15.1, 11.9, 3.7),
    rounded([(3.2, 10.8), (7.8, 11.4), (2.7, 14.9)], [0.3, 0.3, 0.7]),
    teardrop(5.75, 7.35, 1.4, (7.05, 3.85), hole=True),
    teardrop(10.05, 7.35, 1.4, (11.35, 3.85), hole=True),
)

# 交差する 2 本の矢印 (シャッフル)
top = [(1.2, 4.4), (3.9, 4.4)] + bezier((3.9, 4.4), (7.4, 4.4), (8.4, 11.6), (11.6, 11.6), 12)[1:] + [(13.8, 11.6)]
bottom = [(1.2, 11.6), (3.9, 11.6)] + bezier((3.9, 11.6), (7.4, 11.6), (8.4, 4.4), (11.6, 4.4), 12)[1:] + [(13.8, 4.4)]
icons["shuffle"] = svg(
    "shuffle",
    polyline(top, 1.5), polyline(bottom, 1.5),
    polyline([(12.0, 2.2), (14.3, 4.4), (12.0, 6.6)], 1.5),
    polyline([(12.0, 9.4), (14.3, 11.6), (12.0, 13.8)], 1.5),
)


def repeat_loop(narrow=False):
    r = 2.6
    top_path = [(2.3, 8.6)] + arc_pts(2.3 + r, 3.9 + r, r, 180, 270, 8) + [(13.0, 3.9)]
    bot_path = [(13.7, 7.4)] + arc_pts(13.7 - r, 12.1 - r, r, 0, 90, 8) + [(3.0, 12.1)]
    return [
        polyline(top_path, 1.5),
        polyline([(11.0, 1.7), (13.3, 3.9), (11.0, 6.1)], 1.5),
        polyline(bot_path, 1.5),
        polyline([(5.0, 9.9), (2.7, 12.1), (5.0, 14.3)], 1.5),
    ]


# ループする矢印 (リピート)
icons["repeat"] = svg("repeat", *repeat_loop())

# リピート (1 曲)。ループの中に「1」
icons["repeat-one"] = svg(
    "repeat-one",
    *repeat_loop(),
    capsule((8.35, 6.0), (8.35, 10.0), 1.35),
    capsule((8.35, 6.0), (7.1, 6.9), 1.2),
)

# 再生
icons["play"] = svg("play", rounded([(3.9, 1.9), (14.2, 8.0), (3.9, 14.1)], 1.3))

# 一時停止
icons["pause"] = svg("pause", rrect(3.3, 2.0, 6.7, 14.0, 1.2), rrect(9.3, 2.0, 12.7, 14.0, 1.2))

# 次へ / 前へ
fwd1 = [(0.9, 3.0), (8.1, 8.0), (0.9, 13.0)]
fwd2 = [(7.8, 3.0), (15.0, 8.0), (7.8, 13.0)]
icons["next"] = svg("next", rounded(fwd1, 0.9), rounded(fwd2, 0.9))
icons["previous"] = svg("previous",
                        rounded([(16 - x, y) for x, y in fwd1], 0.9),
                        rounded([(16 - x, y) for x, y in fwd2], 0.9))

# 停止
icons["stop"] = svg("stop", rrect(2.8, 2.8, 13.2, 13.2, 2.2))

# スピーカー
speaker = rounded([(0.9, 5.5), (4.2, 5.5), (8.1, 2.1), (8.1, 13.9), (4.2, 10.5), (0.9, 10.5)],
                  [0.9, 0.2, 0.7, 0.7, 0.2, 0.9])
icons["volume"] = svg(
    "volume", speaker,
    arc_band(8.2, 8, 3.0, -48, 48, 1.45),
    arc_band(8.2, 8, 5.8, -52, 52, 1.45),
)
icons["volume-mute"] = svg(
    "volume-mute", speaker,
    capsule((10.4, 5.8), (14.6, 10.2), 1.45),
    capsule((14.6, 5.8), (10.4, 10.2), 1.45),
)

# 出力先 (AirPlay の音声に似た、開いた同心円と三角)
icons["output"] = svg(
    "output",
    arc_band(8, 7.3, 6.3, 142, 398, 1.45),
    arc_band(8, 7.3, 3.4, 150, 390, 1.45),
    rounded([(4.3, 15.1), (8.0, 10.4), (11.7, 15.1)], 0.9),
)

# その他 (…)
icons["more"] = svg("more", *[circle(x, 8, 1.5) for x in (2.9, 8.0, 13.1)])

# 星
icons["star"] = svg("star", rounded(star_points(8, 8.55, 7.6, 3.25), [0.7, 0.2] * 5))

# 山かっこ
icons["back"] = svg("back", polyline([(10.6, 2.2), (4.8, 8.0), (10.6, 13.8)], 1.8))
icons["forward"] = svg("forward", polyline([(5.4, 2.2), (11.2, 8.0), (5.4, 13.8)], 1.8))
icons["chevron-right"] = svg("chevron-right", polyline([(5.9, 3.4), (10.5, 8.0), (5.9, 12.6)], 2.0))

# 閉じる
icons["close"] = svg("close", capsule((3.2, 3.2), (12.8, 12.8), 1.8), capsule((12.8, 3.2), (3.2, 12.8), 1.8))

# ミニプレーヤー (枠の右下に小さな画面)
icons["miniplayer"] = svg(
    "miniplayer",
    rrect_ring(1.4, 2.6, 14.6, 13.4, 2.4, 1.4),
    rrect(8.1, 7.9, 12.6, 11.4, 1.0),
)

# フルスクリーン (外向きの 2 本の矢印)
icons["fullscreen"] = svg(
    "fullscreen",
    polyline([(2.3, 7.4), (2.3, 2.3), (7.4, 2.3)], 1.6),
    capsule((2.6, 2.6), (6.6, 6.6), 1.6),
    polyline([(13.7, 8.6), (13.7, 13.7), (8.6, 13.7)], 1.6),
    capsule((13.4, 13.4), (9.4, 9.4), 1.6),
)

# 追加
icons["plus"] = svg("plus", capsule((8, 2.1), (8, 13.9), 1.8), capsule((2.1, 8), (13.9, 8), 1.8))

# イコライザ (縦のスライダー 3 本)
icons["equalizer"] = svg(
    "equalizer",
    *[capsule((x, 1.6), (x, 14.4), 1.3) for x in (3.2, 8.0, 12.8)],
    rrect(1.2, 8.6, 5.2, 11.6, 1.3),
    rrect(6.0, 3.6, 10.0, 6.6, 1.3),
    rrect(10.8, 7.2, 14.8, 10.2, 1.3),
)

# 局 (アンテナと電波)
icons["station"] = svg(
    "station",
    circle(8, 5.4, 1.7),
    rounded([(6.9, 6.2), (9.1, 6.2), (10.4, 14.6), (5.6, 14.6)], [0.3, 0.3, 0.7, 0.7]),
    arc_band(8, 5.4, 3.3, -50, 50, 1.45), arc_band(8, 5.4, 3.3, 130, 230, 1.45),
    arc_band(8, 5.4, 6.1, -46, 46, 1.45), arc_band(8, 5.4, 6.1, 134, 226, 1.45),
)

# 丸の中の × (消去)
icons["clear"] = svg("clear", circle(8, 8, 7.2), cross(8, 8, 3.0, 1.5, hole=True))

# 丸の中の ! (再生できなかった曲。exclamationmark.circle.fill)
icons["warning"] = svg("warning", circle(8, 8, 7.2), rrect(7.05, 3.5, 8.95, 9.3, 0.95, hole=True),
                       circle(8, 11.75, 1.1, hole=True))

OUT.mkdir(parents=True, exist_ok=True)
for name, data in icons.items():
    (OUT / f"music-{name}-symbolic.svg").write_text(data)
print(len(icons), "icons")
