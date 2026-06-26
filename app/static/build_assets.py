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

# ── 정밀 좌표 설계 (100x100 viewBox 기준) ────────────────────────────────────
# 좌측 삼각형의 3개 논리 꼭짓점:
#   TC = 상단 중앙(살짝 솟음), BC = 하단 중앙(V골), TL = 좌상단(크게 둥근 라운드)
# 우측은 (100-x, 100-y) 점대칭으로 자동 생성.
# 좌측 삼각형: 윗변은 좌상단→상단중앙 수평, 외곽변은 좌상단→하단중앙 사선(\),
# 내부 공유변은 상단중앙→하단중앙(수직). 우측은 좌우 거울 → 가운데서 만나 M/나비.
TC = (50.0, 10.0)    # top-center (상단 중앙, 살짝 솟는 정점)
BC = (50.0, 90.0)    # bottom-center (하단 중앙 — 좌우가 만나 V골)
TL = (9.0, 10.0)     # top-left logical corner (크게 둥근 라운드 대상)
R_BIG = 30.0         # 좌상단 큰 라운드 반경
R_TIP = 5.0          # 뾰족 꼭짓점(TC, BC) 소량 라운드


def _norm(vx, vy):
    d = math.hypot(vx, vy)
    return (vx / d, vy / d) if d else (0.0, 0.0)


def _round_corner(p_prev, corner, p_next, radius):
    """corner 꼭짓점을 radius 로 둥글린 (진입점, 제어점, 진출점)을 반환."""
    cx, cy = corner
    d1 = _norm(p_prev[0] - cx, p_prev[1] - cy)
    d2 = _norm(p_next[0] - cx, p_next[1] - cy)
    # 두 변 길이로 라운드 반경 클램프(과도한 라운드 방지)
    len1 = math.hypot(p_prev[0] - cx, p_prev[1] - cy)
    len2 = math.hypot(p_next[0] - cx, p_next[1] - cy)
    r = min(radius, len1 * 0.5, len2 * 0.5)
    p_in = (cx + d1[0] * r, cy + d1[1] * r)
    p_out = (cx + d2[0] * r, cy + d2[1] * r)
    return p_in, corner, p_out


def left_triangle_path():
    """좌측(파랑) 삼각형의 SVG path d 문자열 + 폴리라인 점열 반환."""
    # 꼭짓점 순회: TC -> BC -> TL -> (TC)  (TL 만 크게 라운드)
    tc_in, _, tc_out = _round_corner(TL, TC, BC, R_TIP)       # TC 라운드
    bc_in, _, bc_out = _round_corner(TC, BC, TL, R_TIP)       # BC 라운드
    tl_in, tlc, tl_out = _round_corner(BC, TL, TC, R_BIG)     # TL 큰 라운드

    d = (
        f"M {tc_out[0]:.3f} {tc_out[1]:.3f} "
        f"L {bc_in[0]:.3f} {bc_in[1]:.3f} "
        f"Q {BC[0]:.3f} {BC[1]:.3f} {bc_out[0]:.3f} {bc_out[1]:.3f} "
        f"L {tl_in[0]:.3f} {tl_in[1]:.3f} "
        f"Q {tlc[0]:.3f} {tlc[1]:.3f} {tl_out[0]:.3f} {tl_out[1]:.3f} "
        f"L {tc_in[0]:.3f} {tc_in[1]:.3f} "
        f"Q {TC[0]:.3f} {TC[1]:.3f} {tc_out[0]:.3f} {tc_out[1]:.3f} "
        "Z"
    )
    return d


def _mirror(pt):
    """수직 중심축(x=50) 기준 좌우 반사 — M/나비 실루엣을 만든다."""
    return (100.0 - pt[0], pt[1])


def right_triangle_path():
    """우측(하늘색) 삼각형 = 좌측의 좌우 거울 대칭(M/나비 실루엣)."""
    g_tc, g_bc, g_tl = _mirror(TC), _mirror(BC), _mirror(TL)
    tc_in, _, tc_out = _round_corner(g_tl, g_tc, g_bc, R_TIP)
    bc_in, _, bc_out = _round_corner(g_tc, g_bc, g_tl, R_TIP)
    tl_in, tlc, tl_out = _round_corner(g_bc, g_tl, g_tc, R_BIG)
    d = (
        f"M {tc_out[0]:.3f} {tc_out[1]:.3f} "
        f"L {bc_in[0]:.3f} {bc_in[1]:.3f} "
        f"Q {g_bc[0]:.3f} {g_bc[1]:.3f} {bc_out[0]:.3f} {bc_out[1]:.3f} "
        f"L {tl_in[0]:.3f} {tl_in[1]:.3f} "
        f"Q {g_tl[0]:.3f} {g_tl[1]:.3f} {tl_out[0]:.3f} {tl_out[1]:.3f} "
        f"L {tc_in[0]:.3f} {tc_in[1]:.3f} "
        f"Q {g_tc[0]:.3f} {g_tc[1]:.3f} {tc_out[0]:.3f} {tc_out[1]:.3f} "
        "Z"
    )
    return d


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


# ── PIL 래스터: SVG 의 베지어를 직접 샘플링해 폴리곤으로 채운다 ───────────────
def _quad(p0, p1, p2, n=24):
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        x = mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0]
        y = mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _left_polygon():
    tc_in, _, tc_out = _round_corner(TL, TC, BC, R_TIP)
    bc_in, _, bc_out = _round_corner(TC, BC, TL, R_TIP)
    tl_in, tlc, tl_out = _round_corner(BC, TL, TC, R_BIG)
    poly = [tc_out]
    poly.append(bc_in)
    poly += _quad(bc_in, BC, bc_out)
    poly.append(tl_in)
    poly += _quad(tl_in, tlc, tl_out)
    poly.append(tc_in)
    poly += _quad(tc_in, TC, tc_out)
    return poly


def _right_polygon():
    g_tc, g_bc, g_tl = _mirror(TC), _mirror(BC), _mirror(TL)
    tc_in, _, tc_out = _round_corner(g_tl, g_tc, g_bc, R_TIP)
    bc_in, _, bc_out = _round_corner(g_tc, g_bc, g_tl, R_TIP)
    tl_in, tlc, tl_out = _round_corner(g_bc, g_tl, g_tc, R_BIG)
    poly = [tc_out, bc_in]
    poly += _quad(bc_in, g_bc, bc_out)
    poly.append(tl_in)
    poly += _quad(tl_in, g_tl, tl_out)
    poly.append(tc_in)
    poly += _quad(tc_in, g_tc, tc_out)
    return poly


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
