#!/usr/bin/env python3
"""에이전트 BOGO 로고 벡터 + 파비콘 세트 생성기.

원본 첨부 로고를 SVG 벡터로 정밀 재현한다(투명 배경 내장).
두 개의 라운드 모서리 삼각형이 캔버스 중심(50,50)에 대한 180° 점대칭으로
맞물려 M / 나비 형태를 이룬다.
  - 좌측 삼각형: 진한 파랑(#1C5FAE), 좌상단 모서리가 크게 둥글다.
  - 우측 삼각형: 밝은 하늘색(#18B4E6), 우하단 모서리가 크게 둥글다(점대칭).
상단 중앙은 살짝 솟고, 하단 중앙은 V자로 패인다.

PIL로 동일 좌표를 그대로 사용해 PNG/ICO를 래스터라이즈한다(SVG·PNG 형태 일치 보장).
"""
import math

from PIL import Image, ImageDraw

NAVY = (28, 95, 174)    # #1C5FAE
CYAN = (24, 180, 230)   # #18B4E6
HEX_NAVY = "#1C5FAE"
HEX_CYAN = "#18B4E6"

# ── 점대칭(180° 회전) 풍차/나비형 (100x100 viewBox) ──────────────────────────
# 좌측 navy 삼각형: 좌상단 코너가 크게 둥글다. 윗변 수평. 뾰족점은 아래(중앙 하단).
#   꼭짓점 순회(시계방향):  TL(좌상단·큰 라운드) → TR(윗변 우측 끝) → BP(아래 뾰족점)
# 우측 cyan 삼각형: 위 도형을 중심(50,50) 기준 180° 회전(x,y)->(100-x,100-y).
#   → 우하단 코너가 크게 둥글고 위로 뾰족. navy 아래뾰족 / cyan 위뾰족이 어긋나 맞물림.
# 결과: 좌상-우하 대각선 색경계의 M/나비/풍차. 좌우대칭 V 아님(점대칭).
# 각 꼭짓점 = (좌표, 라운드반경). 라운드는 해당 코너에만 적용.
L_TRI = [
    ((13.0, 14.0), 23.0),   # TL 좌상단 — 크게 둥근 라운드
    ((61.0, 14.0), 4.5),    # TR 윗변 우측 끝 — 소량 라운드
    ((52.0, 87.0), 4.0),    # BP 아래 뾰족점 — 소량 라운드
]


def _norm(vx, vy):
    d = math.hypot(vx, vy)
    return (vx / d, vy / d) if d else (0.0, 0.0)


def _point_sym(pt):
    """중심(50,50) 기준 180° 회전 점대칭."""
    return (100.0 - pt[0], 100.0 - pt[1])


def _rounded_quad_pts(corner, p_prev, p_next, radius):
    """corner 를 radius 로 둥글린 (진입점, 제어점=corner, 진출점)."""
    cx, cy = corner
    d1 = _norm(p_prev[0] - cx, p_prev[1] - cy)
    d2 = _norm(p_next[0] - cx, p_next[1] - cy)
    len1 = math.hypot(p_prev[0] - cx, p_prev[1] - cy)
    len2 = math.hypot(p_next[0] - cx, p_next[1] - cy)
    r = min(radius, len1 * 0.5, len2 * 0.5)
    p_in = (cx + d1[0] * r, cy + d1[1] * r)
    p_out = (cx + d2[0] * r, cy + d2[1] * r)
    return p_in, (cx, cy), p_out


def _tri_path(verts):
    """[(점,반경)...] 삼각형(폐곡선)을 라운드 코너 SVG path d 로 직렬화."""
    n = len(verts)
    rounded = []
    for i in range(n):
        corner, radius = verts[i]
        prev_pt = verts[(i - 1) % n][0]
        next_pt = verts[(i + 1) % n][0]
        rounded.append(_rounded_quad_pts(corner, prev_pt, next_pt, radius))
    # 시작점 = 첫 코너의 진출점
    d = f"M {rounded[0][2][0]:.3f} {rounded[0][2][1]:.3f} "
    for i in range(1, n + 1):
        p_in, ctrl, p_out = rounded[i % n]
        d += f"L {p_in[0]:.3f} {p_in[1]:.3f} "
        d += f"Q {ctrl[0]:.3f} {ctrl[1]:.3f} {p_out[0]:.3f} {p_out[1]:.3f} "
    return d + "Z"


def _tri_polygon(verts, seg=24):
    """[(점,반경)...] 삼각형을 베지어 샘플링한 폴리라인 점열(PIL 채움용)."""
    n = len(verts)
    rounded = []
    for i in range(n):
        corner, radius = verts[i]
        prev_pt = verts[(i - 1) % n][0]
        next_pt = verts[(i + 1) % n][0]
        rounded.append(_rounded_quad_pts(corner, prev_pt, next_pt, radius))
    poly = [rounded[0][2]]
    for i in range(1, n + 1):
        p_in, ctrl, p_out = rounded[i % n]
        poly.append(p_in)
        # quad 베지어 샘플
        for k in range(1, seg + 1):
            t = k / seg
            mt = 1 - t
            x = mt * mt * p_in[0] + 2 * mt * t * ctrl[0] + t * t * p_out[0]
            y = mt * mt * p_in[1] + 2 * mt * t * ctrl[1] + t * t * p_out[1]
            poly.append((x, y))
    return poly


def _right_verts():
    return [(_point_sym(p), r) for (p, r) in L_TRI]


def left_triangle_path():
    return _tri_path(L_TRI)


def right_triangle_path():
    return _tri_path(_right_verts())


def build_svg():
    left = left_triangle_path()
    right = right_triangle_path()
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
        'width="100" height="100" role="img" aria-label="에이전트 BOGO">\n'
        '  <title>에이전트 BOGO</title>\n'
        f'  <path fill="{HEX_NAVY}" d="{left}"/>\n'
        f'  <path fill="{HEX_CYAN}" d="{right}"/>\n'
        '</svg>\n'
    )


# ── PIL 래스터: SVG 와 동일 좌표/베지어를 폴리곤으로 채운다 ───────────────────
def _left_polygon():
    return _tri_polygon(L_TRI)


def _right_polygon():
    return _tri_polygon(_right_verts())


def render_png(size, pad_ratio=0.0):
    """투명 배경 정사각 PNG. pad_ratio>0 이면 여백(애플 터치 아이콘용)."""
    ss = 4  # supersample
    canvas = size * ss
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    inner = canvas * (1 - 2 * pad_ratio)
    off = canvas * pad_ratio
    sc = inner / 100.0

    def tx(poly):
        return [(off + x * sc, off + y * sc) for x, y in poly]

    d.polygon(tx(_left_polygon()), fill=NAVY)
    d.polygon(tx(_right_polygon()), fill=CYAN)
    return img.resize((size, size), Image.LANCZOS)


def main():
    import os
    here = os.path.dirname(os.path.abspath(__file__))

    with open(os.path.join(here, "logo.svg"), "w", encoding="utf-8") as f:
        f.write(build_svg())
    # favicon.svg = 동일 벡터(브라우저 SVG 파비콘)
    with open(os.path.join(here, "favicon.svg"), "w", encoding="utf-8") as f:
        f.write(build_svg())

    # PNG 세트
    render_png(16).save(os.path.join(here, "favicon-16.png"))
    render_png(32).save(os.path.join(here, "favicon-32.png"))
    render_png(192).save(os.path.join(here, "icon-192.png"))
    render_png(512).save(os.path.join(here, "icon-512.png"))
    # 애플 터치 아이콘: 불투명 흰 배경 + 약간의 여백이 iOS 권장
    apple = Image.new("RGBA", (180, 180), (255, 255, 255, 255))
    glyph = render_png(180, pad_ratio=0.16)
    apple.alpha_composite(glyph)
    apple.convert("RGB").save(os.path.join(here, "apple-touch-icon.png"))

    # favicon.ico 멀티 사이즈(16·32·48)
    ico = render_png(48)
    ico.save(os.path.join(here, "favicon.ico"),
             sizes=[(16, 16), (32, 32), (48, 48)])

    # 미리보기(자체 QA용): 흰/회색 체커 위에 합성
    prev = Image.new("RGBA", (240, 240), (245, 245, 247, 255))
    g = render_png(200, pad_ratio=0.06)
    prev.alpha_composite(g, (20, 20))
    prev.convert("RGB").save(os.path.join(here, "_preview.png"))
    print("OK: logo.svg, favicon.svg, favicon-16/32.png, icon-192/512.png, "
          "apple-touch-icon.png, favicon.ico, _preview.png")


if __name__ == "__main__":
    main()
