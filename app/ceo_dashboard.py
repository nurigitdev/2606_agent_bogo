"""
CEO 로컬 대시보드 — 부서 현황 모니터링 + 지시 송신 + 에이전트 현황 (단일 페이지).

CEO 가 Mattermost 의 여러 채널(인사총무팀/개발팀/보고라인/CEO브리핑)을 직접 오가지
않고, 브라우저 한 곳(http://127.0.0.1:<port>)에서:
  (a) 부서별 현황 카드  — 각 채널 최근 N건을 mm_client.history 로 폴링 표시
  (b) 지시 입력창       — 선택 채널(기본 CEO브리핑 → 박민철)에 메시지 게시
  (c) 에이전트 현황     — agent_schema.load_roles 의 role 목록 + 봇 활성 여부
  (d) 에이전트 관리 연계 — 기존 ceo_admin_runtime(CEO-에이전트관리) 파이프라인 안내

설계 원칙(기존 Hermes 인프라 그대로 재사용, 신규 의존성·신규 API 키 0):
  - 통신:   mm_client.MM(REST) — 토큰은 박민철(nk_config.json)을 재사용. 박민철은
            CEO브리핑·양 보고라인 멤버라 읽기/쓰기 권한이 이미 있다.
  - 데이터: channels.json / teams.json / agents/*.md 를 agent_schema 로 로드.
            채널·팀·토큰을 코드에 하드코딩하지 않는다(전부 데이터 파일에서).
  - 보안:   웹서버는 반드시 127.0.0.1(루프백)에서만 listen. 외부 노출 금지.
            게시 가능한 채널은 화이트리스트(채널/보고라인/브리핑)로 제한.
  - 의존성: 표준 라이브러리(http.server)만 사용 — node/npm·FastAPI 등 추가 0.

실행:   <venv>/python ceo_dashboard.py        (포트 기본 8787, HERMES_DASHBOARD_PORT 로 변경)
인증:   기존 봇 토큰(nk_config.json)만. ANTHROPIC_API_KEY 등 신규 키 요구 없음.

배포 모델: 읽기 전용 조회·게시만 하므로 git/파일쓰기 없음 → 미러/원본 구분 불요.
          데이터 파일은 HERE(실행 디렉터리) 기준으로 읽는다(미러든 원본이든 동일 사본).
"""
import json
import os
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import agent_schema as A
import mm_client as C

HERE = os.path.dirname(os.path.abspath(__file__))

# 루프백 전용. 외부(0.0.0.0)로 절대 바꾸지 말 것 — 인증 게이트 없는 로컬 대시보드다.
HOST = "127.0.0.1"
PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "8787"))

# 게시에 쓸 봇: 박민철(nk). CEO브리핑·양 보고라인의 멤버이므로 읽기/쓰기가 가능하다.
# 별도 봇 계정·신규 토큰 생성 없이 기존 토큰을 재사용한다.
DASHBOARD_CONFIG = "nk"

# ── 데이터 로드 (전부 기존 데이터 파일에서; 하드코딩 0) ─────────────────────────
CHANNELS = A.load_channels()                 # {채널명: channel_id}
TEAMS = A.load_teams()                        # {orchestrator, teams[]}
ROLES = A.load_roles()                        # {role: {name, username, config, ...}}
ID2NAME = {v: k for k, v in CHANNELS.items()}


def _build_whitelist():
    """대시보드가 다룰 채널 화이트리스트를 teams.json·orchestrator 정의에서 동적 구성.

    팀 채널/보고라인 + CEO 브리핑만 허용한다(CEO-에이전트관리는 전용 파이프라인이라 제외).
    채널명이 channels.json 에 실재하는 것만 통과시켜, 정의 누락 시 조용히 빠진다.
    반환: [{name, id, kind, team_label}] 순서 보존 리스트.
    """
    out = []
    seen = set()

    def add(name, kind, label=""):
        if not name or name in seen or name not in CHANNELS:
            return
        seen.add(name)
        out.append({"name": name, "id": CHANNELS[name], "kind": kind, "team_label": label})

    brief = TEAMS.get("orchestrator", {}).get("briefing_channel")
    add(brief, "briefing", "CEO")
    for t in TEAMS.get("teams", []):
        add(t.get("team_channel"), "team", t.get("label", ""))
        add(t.get("report_channel"), "report", t.get("label", ""))
    return out


WHITELIST = _build_whitelist()
WHITELIST_NAMES = {c["name"] for c in WHITELIST}
# 게시 대상 기본값: CEO브리핑(→ 박민철). 없으면 화이트리스트 첫 채널.
DEFAULT_POST_CHANNEL = (
    TEAMS.get("orchestrator", {}).get("briefing_channel")
    if TEAMS.get("orchestrator", {}).get("briefing_channel") in WHITELIST_NAMES
    else (WHITELIST[0]["name"] if WHITELIST else None)
)

# 봇 토큰 로드 (기존 config json 재사용). 부재 시 즉시 중단(거짓 양성 금지).
_cfg_path = os.path.join(HERE, f"{DASHBOARD_CONFIG}_config.json")
if not os.path.isfile(_cfg_path):
    raise SystemExit(f"치명: 봇 설정 파일이 없습니다: {_cfg_path}")
with open(_cfg_path, encoding="utf-8") as _f:
    CFG = json.load(_f)
if not CFG.get("bot_token") or CFG["bot_token"].endswith("HERE"):
    raise SystemExit(
        f"치명: {DASHBOARD_CONFIG}_config.json 의 bot_token 이 비어/플레이스홀더입니다. "
        "실제 박민철 봇 토큰을 채우세요.")
mm = C.MM(CFG["bot_token"])

# config(=봇 계정) -> 그 봇을 쓰는 role 들. 봇 활성 점검에 사용.
CONFIG_TO_ROLES = {}
for _r, _m in ROLES.items():
    CONFIG_TO_ROLES.setdefault(_m.get("config", ""), []).append(_r)


def _bot_id_for(cfg):
    """{cfg}_config.json 에서 bot_id 를 안전하게 읽는다(파일 핸들 누수 방지)."""
    if not cfg:
        return None
    try:
        with open(os.path.join(HERE, f"{cfg}_config.json"), encoding="utf-8") as f:
            return json.load(f).get("bot_id")
    except (OSError, json.JSONDecodeError):
        return None

# 봇 활성 판정 결과 캐시 (config -> {ok, name}). 매 요청 user API 호출을 줄인다.
_bot_status_cache = {}


# ── 도메인 헬퍼 (전부 mm_client·agent_schema 경유) ───────────────────────────
def fetch_history(channel_name, n=8):
    """화이트리스트 채널의 최근 n건을 시간순 [{author, text, ts}] 로 반환.

    mm_client.MM.history(원시 {order, posts}) 를 시간 오름차순으로 평탄화한다.
    시스템 메시지(type 존재)는 제외해 사람/봇 대화만 보여준다.
    """
    if channel_name not in WHITELIST_NAMES:
        raise ValueError(f"허용되지 않은 채널: {channel_name}")
    cid = CHANNELS[channel_name]
    raw = mm.history(cid, n=n)
    order = raw.get("order", [])
    posts = raw.get("posts", {})
    items = []
    for pid in reversed(order):  # API 는 최신→과거. 화면은 과거→최신.
        p = posts.get(pid, {})
        if p.get("type"):  # join/leave 등 시스템 메시지 제외
            continue
        items.append({
            "author": _author_name(p.get("user_id", "")),
            "text": (p.get("message", "") or "")[:600],
            "ts": p.get("create_at", 0),
        })
    return items[-n:]


_author_cache = {}


def _author_name(uid):
    """user_id -> 표시 이름. 봇 id 는 role 이름으로, 그 외는 user API(닉네임)로 해석."""
    if not uid:
        return "?"
    if uid in _author_cache:
        return _author_cache[uid]
    # 등록된 봇이면 role 이름으로 바로 매핑(API 호출 절감).
    for cfg, roles in CONFIG_TO_ROLES.items():
        bid = _bot_id_for(cfg)
        if bid and bid == uid and roles:
            name = ROLES[roles[0]]["name"]
            _author_cache[uid] = name
            return name
    try:
        u = mm.user(uid)
        name = u.get("nickname") or u.get("username") or "사람"
    except Exception:
        name = "사람"
    _author_cache[uid] = name
    return name


def post_message(channel_name, text):
    """선택 채널에 게시. 화이트리스트·빈문자 검증 후 mm_client.MM.post 경유."""
    if channel_name not in WHITELIST_NAMES:
        raise ValueError(f"허용되지 않은 채널: {channel_name}")
    text = (text or "").strip()
    if not text:
        raise ValueError("빈 메시지는 보낼 수 없습니다.")
    if len(text) > 4000:
        raise ValueError("메시지가 너무 깁니다(4000자 제한).")
    return mm.post(CHANNELS[channel_name], text)


def bot_status(cfg):
    """봇(config) 활성 여부를 Mattermost user API 로 확인. 캐시 사용."""
    if cfg in _bot_status_cache:
        return _bot_status_cache[cfg]
    res = {"ok": False, "name": ""}
    try:
        bid = _bot_id_for(cfg)
        if bid:
            u = mm.user(bid)
            # delete_at==0 이면 활성 계정. 조회 성공 자체가 토큰·계정 유효 신호.
            res = {"ok": (u.get("delete_at", 0) == 0), "name": u.get("username", "")}
    except Exception:
        res = {"ok": False, "name": ""}
    _bot_status_cache[cfg] = res
    return res


def roster():
    """에이전트 현황: role 목록 + 봇 활성 여부 + 담당 채널."""
    out = []
    for role, m in ROLES.items():
        st = bot_status(m.get("config", ""))
        out.append({
            "role": role,
            "name": m.get("name", role),
            "username": m.get("username", ""),
            "primary": m.get("primary", ""),
            "channels": m.get("channels", []),
            "bot_active": st["ok"],
        })
    return out


# ── HTTP 핸들러 ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    # 액세스 로그 소음 억제(필요시 주석 해제).
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path in ("/", "/index.html"):
            return self._html(INDEX_HTML)
        if path == "/api/channels":
            return self._json({
                "channels": WHITELIST,
                "default_post_channel": DEFAULT_POST_CHANNEL,
            })
        if path == "/api/roles":
            return self._json({
                "roles": roster(),
                "admin_channel": "CEO-에이전트관리" if "CEO-에이전트관리" in CHANNELS else None,
            })
        if path == "/api/history":
            q = parse_qs(u.query)
            ch = (q.get("channel") or [""])[0]
            try:
                n = max(1, min(30, int((q.get("n") or ["8"])[0])))
            except ValueError:
                n = 8
            if ch not in WHITELIST_NAMES:
                return self._json({"error": "허용되지 않은 채널"}, 400)
            try:
                return self._json({"channel": ch, "items": fetch_history(ch, n)})
            except urllib.error.URLError as e:
                return self._json({"error": f"Mattermost 연결 실패: {e}"}, 502)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)[:200]}, 500)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/post":
            return self._json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 100_000:
            return self._json({"error": "잘못된 요청 본문"}, 400)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json({"error": "JSON 파싱 실패"}, 400)
        ch = (data.get("channel") or "").strip()
        text = (data.get("text") or "").strip()
        if ch not in WHITELIST_NAMES:
            return self._json({"error": "허용되지 않은 채널"}, 400)
        try:
            res = post_message(ch, text)
            return self._json({"ok": True, "post_id": res.get("id", ""), "channel": ch})
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except urllib.error.URLError as e:
            return self._json({"error": f"Mattermost 연결 실패: {e}"}, 502)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)[:200]}, 500)


def build_index_html():
    """단일 정적 HTML(외부 빌드체인 0, vanilla JS). 채널·기본값은 런타임 API 로 주입.

    시각: Apple 웹 디자인 시스템(SF Pro 타이포, 단일 Action Blue #0066cc 액센트,
    교차 명암 풀블리드 타일이 섹션 divider, 시스템 그림자 단 1개, pill/lg/sm/md
    라디우스 문법 분리, weight 300/400/600/700 래더). CSS 토큰은 :root 변수로 선언.
    서버/API/JS 동작 계약(element id·class·fetch 형식)은 전부 보존한다.
    """
    return """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes CEO 대시보드</title>
<style>
  :root {
    /* ── 텍스트 잉크: 순검정 금지, Apple #1d1d1f ── */
    --ink:#1d1d1f;            /* 헤드라인 / 본문 잉크 */
    --ink-soft:#424245;       /* 보조 본문 */
    --ink-muted:#6e6e73;      /* 메타·캡션 */
    --ink-faint:#86868b;      /* 가장 흐린 텍스트 */
    /* ── 단일 액센트: Action Blue. 제2 액센트 없음 ── */
    --blue:#0066cc;           /* 모든 인터랙티브: 링크·CTA·포커스 */
    --blue-sky:#2997ff;       /* 다크 배경 위 인라인 링크 전용 */
    --focus:#0071e3;          /* 포커스 링 */
    /* ── 표면 ── */
    --nav-black:#000000;      /* 글로벌 nav 바 전용(진짜 검정) */
    --canvas:#ffffff;         /* 주 캔버스(라이트 타일) */
    --parchment:#f5f5f7;      /* 교차 라이트 타일·푸터 */
    --tile-dark:#1d1d1f;      /* 풀블리드 near-black 타일 */
    --tile-dark-2:#2a2a2c;    /* near-black 위 내부 패널 */
    --tile-dark-3:#252527;    /* near-black 위 입력 */
    --hairline:#e0e0e0;       /* 유틸리티 카드 1px hairline */
    --hairline-soft:#d2d2d7;  /* 입력 보더 */
    /* 다크 타일 위 텍스트 */
    --on-dark:#ffffff;
    --on-dark-muted:#cccccc;
    /* ── 상태 도트(기능 신호, 액센트 아님) ── */
    --ok:#1d8a4e;
    --off:#86868b;
    /* ── 스페이싱(8px 베이스) ── */
    --space-1:4px; --space-2:8px; --space-3:12px; --space-4:16px;
    --space-5:20px; --space-6:24px; --space-8:32px; --space-10:40px;
    --space-12:48px; --space-16:64px; --space-20:80px;
    /* ── 라디우스 문법: 섞지 말 것 ── */
    --r-pill:9999px;          /* 블루 CTA·검색입력·옵션칩 */
    --r-lg:18px;              /* 유틸리티 카드 */
    --r-md:11px;              /* Pearl 버튼 */
    --r-sm:8px;               /* 다크 유틸 버튼 */
    /* ── 시스템 그림자: 단 하나. 제품/주요 비주얼에만 ── */
    --shadow:rgba(0,0,0,0.22) 3px 5px 30px;
  }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body {
    margin:0;
    font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text",
      "Apple SD Gothic Neo",system-ui,Inter,"Malgun Gothic",sans-serif;
    background:var(--canvas); color:var(--ink);
    font-size:17px; font-weight:400; line-height:1.47148; letter-spacing:-0.374px;
    word-break:keep-all; -webkit-font-smoothing:antialiased;
  }
  a { color:var(--blue); font-weight:400; text-decoration:none; }
  a:hover { text-decoration:underline; }

  /* ── 글로벌 nav: 진짜 검정 #000, 높이 44px ── */
  .promo-banner {
    background:var(--nav-black); color:var(--on-dark);
    height:44px; display:flex; align-items:center; justify-content:center;
    font-size:12px; font-weight:400; letter-spacing:-0.12px;
    padding:0 22px; text-align:center;
  }
  .promo-banner b { font-weight:600; }

  /* ── 상단 흰 네비 바(콘텐츠 헤더) ── */
  header {
    position:sticky; top:0; z-index:20;
    background:rgba(255,255,255,.82);
    -webkit-backdrop-filter:saturate(180%) blur(20px);
    backdrop-filter:saturate(180%) blur(20px);
    border-bottom:1px solid var(--hairline);
    padding:var(--space-4) var(--space-8);
    display:flex; align-items:center; gap:var(--space-4); flex-wrap:wrap;
  }
  .brand { display:flex; align-items:center; gap:var(--space-3); }
  .logo-mark {
    width:30px; height:30px; border-radius:var(--r-sm); flex:0 0 auto;
    background:var(--ink); color:var(--on-dark);
    display:flex; align-items:center; justify-content:center;
    font-weight:600; font-size:15px; letter-spacing:-0.3px;
  }
  header h1 {
    font-size:19px; font-weight:600; letter-spacing:-0.32px;
    margin:0; color:var(--ink);
  }
  .nav-meta { margin-left:auto; display:flex; align-items:center; gap:var(--space-3); flex-wrap:wrap; }
  .pill {
    font-size:13px; font-weight:400; color:var(--ink-muted);
    letter-spacing:-0.2px;
  }
  /* status chip: 다크 유틸 버튼 문법(sm 라운드, ink bg) */
  .chip {
    display:inline-flex; align-items:center; gap:7px;
    font-size:13px; font-weight:400; color:var(--on-dark);
    background:var(--ink); border:none;
    border-radius:var(--r-sm); padding:7px 13px; white-space:nowrap;
    letter-spacing:-0.2px;
  }
  .chip .live-dot {
    width:6px; height:6px; border-radius:50%; background:var(--ok);
  }

  /* ── 콘텐츠 락 1440px ── */
  .wrap { max-width:1440px; margin:0 auto; padding:0; }
  /* 섹션을 풀블리드 타일로 구성: 색 전환 자체가 divider ── */
  .tile { padding:var(--space-20) var(--space-8); }
  .tile-light { background:var(--canvas); }
  .tile-parchment { background:var(--parchment); }
  .tile-dark { background:var(--tile-dark); }
  .tile-inner { max-width:1280px; margin:0 auto; }

  /* ── 섹션 헤더: 헤드라인 위 64px+ 공기 ── */
  .section-head { margin:0 0 var(--space-12); }
  .section-title {
    font-size:40px; font-weight:600; letter-spacing:-0.374px;
    color:var(--ink); margin:0; line-height:1.1;
  }
  .tile-dark .section-title { color:var(--on-dark); }
  .section-sub {
    font-size:19px; font-weight:400; color:var(--ink-muted);
    margin:var(--space-3) 0 0; letter-spacing:-0.32px; line-height:1.4;
  }
  .tile-dark .section-sub { color:var(--on-dark-muted); }

  /* ── 부서 카드 그리드: near-black 타일 위 내부 패널 ── */
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:var(--space-6); }
  .card {
    border-radius:var(--r-lg);
    padding:var(--space-6);
    min-height:220px; color:var(--on-dark);
    display:flex; flex-direction:column;
    background:var(--tile-dark-2);
    overflow:hidden;
  }
  /* 채널 종류 표식: 좌측 4px 액센트 레일(단일 블루) + 무채 변주.
     색을 늘리지 않고 위계만 — k-coral(브리핑)만 블루로 신호. */
  .card.k-coral   { box-shadow:inset 4px 0 0 var(--blue); }
  .card.k-blue    { box-shadow:inset 4px 0 0 rgba(255,255,255,.55); }
  .card.k-magenta { box-shadow:inset 4px 0 0 rgba(255,255,255,.38); }
  .card.k-purple  { box-shadow:inset 4px 0 0 rgba(255,255,255,.24); }
  .card h2 {
    font-size:21px; font-weight:600; letter-spacing:-0.34px; margin:0 0 var(--space-4);
    display:flex; justify-content:space-between; align-items:flex-start; gap:var(--space-3);
    line-height:1.2; color:var(--on-dark);
  }
  /* 뱃지: pill. 다크 타일 위 외곽선 ghost */
  .badge {
    flex:0 0 auto;
    font-size:12px; font-weight:400; letter-spacing:-0.1px;
    padding:4px 11px; border-radius:var(--r-pill);
    background:transparent; color:var(--on-dark-muted);
    border:1px solid rgba(255,255,255,.28); white-space:nowrap;
  }
  .badge.briefing { color:var(--blue-sky); border-color:rgba(41,151,255,.5); }
  /* 카드 내부 메시지 영역: 다크 위 한 단계 밝은 패널 */
  .msgs {
    margin-top:auto; max-height:280px; overflow-y:auto;
    background:var(--canvas); border-radius:var(--r-md);
    padding:var(--space-2) var(--space-4); color:var(--ink);
  }
  .msg { padding:var(--space-3) 0; border-top:1px solid var(--hairline); }
  .msg:first-child { border-top:none; }
  .msg .who { color:var(--ink); font-weight:600; font-size:14px; letter-spacing:-0.2px; }
  .msg .when { color:var(--ink-faint); font-size:12px; margin-left:7px; font-weight:400; }
  .msg .body {
    white-space:pre-wrap; word-break:break-word; margin-top:var(--space-1);
    line-height:1.47; font-size:15px; color:var(--ink-soft);
  }
  .empty { color:var(--ink-muted); font-size:14px; padding:var(--space-3) 0; font-weight:400; }
  .msgs .empty { color:var(--ink-muted); }

  /* ── CEO 지시: parchment 타일 위 흰 유틸리티 카드(그림자 없음) ── */
  .composer {
    background:var(--canvas);
    border:1px solid var(--hairline);
    border-radius:var(--r-lg);
    padding:var(--space-6);
  }
  .composer .row { display:flex; gap:var(--space-3); margin-bottom:var(--space-4); flex-wrap:wrap; align-items:center; }
  .composer label { font-size:15px; font-weight:600; color:var(--ink); letter-spacing:-0.2px; }
  select, textarea, button { font-family:inherit; }
  /* select: 옵션칩 → pill 문법 */
  select {
    background:var(--canvas); color:var(--ink);
    border:1px solid var(--hairline-soft);
    border-radius:var(--r-pill); padding:9px 18px;
    font-size:15px; font-weight:400; cursor:pointer; min-height:44px;
    letter-spacing:-0.2px;
  }
  select:focus { outline:none; border-color:var(--blue); box-shadow:0 0 0 2px var(--focus); }
  /* textarea: 검색입력류 → 부드러운 사각(lg) + 블루 포커스 */
  textarea {
    width:100%; min-height:104px;
    background:var(--canvas); color:var(--ink);
    border:1px solid var(--hairline-soft);
    border-radius:var(--r-lg); padding:var(--space-4); resize:vertical;
    font-size:17px; line-height:1.47; letter-spacing:-0.374px;
  }
  textarea::placeholder { color:var(--ink-faint); }
  textarea:focus { outline:none; border-color:var(--blue); box-shadow:0 0 0 2px var(--focus); }
  /* button-primary: Action Blue pill, 흰 17px, padding 11x22 ── */
  button {
    background:var(--blue); color:var(--on-dark); border:none;
    border-radius:var(--r-pill); padding:11px 22px;
    cursor:pointer; font-weight:400; font-size:17px; letter-spacing:-0.32px;
    min-height:44px; transition:transform .14s ease;
  }
  button:active { transform:scale(0.95); }
  button:disabled { opacity:.36; cursor:not-allowed; }

  /* ── 에이전트 현황: 라이트 타일 위 흰 유틸리티 카드 ── */
  .roster { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:var(--space-5); }
  .agent {
    background:var(--canvas);
    border:1px solid var(--hairline);
    border-radius:var(--r-lg);
    padding:var(--space-6);
  }
  .agent .nm {
    font-weight:600; font-size:17px; letter-spacing:-0.32px; color:var(--ink);
    display:flex; align-items:center; gap:8px;
  }
  .agent .nm .pill { color:var(--ink-faint); font-weight:400; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; flex:0 0 auto; }
  .dot.on { background:var(--ok); }
  .dot.off { background:var(--off); }
  .agent .meta {
    color:var(--ink-muted); font-size:14px; margin-top:var(--space-3); line-height:1.6;
    letter-spacing:-0.2px;
  }
  /* note: parchment 타일 위 한 단계 더 들어간 안내 카드 */
  .note {
    color:var(--ink-soft); font-size:15px; margin-top:var(--space-6); line-height:1.6;
    background:var(--parchment); border:1px solid var(--hairline);
    border-radius:var(--r-lg); padding:var(--space-6); letter-spacing:-0.2px;
  }
  .note b { color:var(--ink); font-weight:600; }

  /* ── 토스트: 떠있는 요소 → 시스템 그림자 1개 적용 ── */
  .toast {
    position:fixed; bottom:var(--space-8); left:50%; transform:translateX(-50%) translateY(8px);
    background:var(--ink); color:var(--on-dark);
    padding:13px 24px; border-radius:var(--r-pill);
    font-size:15px; font-weight:400; letter-spacing:-0.2px;
    opacity:0; transition:opacity .25s ease, transform .25s ease;
    pointer-events:none; box-shadow:var(--shadow);
  }
  .toast.show { opacity:1; transform:translateX(-50%) translateY(0); }

  /* ── 반응형: 1440 락 → 태블릿 2열 → 모바일 1열 ── */
  @media (max-width:1024px){
    .grid { grid-template-columns:repeat(2,1fr); }
    .roster { grid-template-columns:repeat(2,1fr); }
    .section-title { font-size:32px; }
    .tile { padding:var(--space-16) var(--space-6); }
  }
  @media (max-width:768px){
    body { font-size:16px; }
    .grid { grid-template-columns:1fr; }
    .roster { grid-template-columns:1fr; }
    .card { min-height:0; }
    .section-title { font-size:28px; letter-spacing:-0.5px; }
    .section-sub { font-size:17px; }
    .nav-meta { width:100%; margin-left:0; }
    header { padding:var(--space-4) var(--space-5); }
    .tile { padding:var(--space-12) var(--space-5); }
    .composer .row { gap:var(--space-2); }
    select { width:100%; }
  }
</style>
</head>
<body>
<div class="promo-banner">루프백 전용(127.0.0.1) · CEO 단독 콘솔 · <b>외부에 노출되지 않습니다</b></div>
<header>
  <div class="brand">
    <span class="logo-mark">H</span>
    <h1>Hermes CEO 대시보드</h1>
  </div>
  <div class="nav-meta">
    <span class="pill" id="clock"></span>
    <span class="chip"><span class="live-dot"></span>자동 새로고침 <span id="poll">12</span>초</span>
  </div>
</header>
<div class="wrap">
  <!-- 부서별 현황 — near-black 풀블리드 타일 -->
  <section class="tile tile-dark">
    <div class="tile-inner">
      <div class="section-head">
        <h2 class="section-title">부서별 현황</h2>
        <p class="section-sub">각 채널의 최근 활동을 12초마다 폴링해 보여줍니다.</p>
      </div>
      <div class="grid" id="cards"></div>
    </div>
  </section>

  <!-- CEO 지시 — parchment 라이트 타일(색 전환이 divider) -->
  <section class="tile tile-parchment">
    <div class="tile-inner">
      <div class="section-head">
        <h2 class="section-title">CEO 지시</h2>
        <p class="section-sub">기본 CEO브리핑 → 박민철(비서실장)에게 전달됩니다.</p>
      </div>
      <div class="composer">
        <div class="row">
          <label for="ch">대상 채널</label>
          <select id="ch"></select>
        </div>
        <textarea id="msg" placeholder="지시 내용을 입력하세요. 예) 개발팀 이번 주 출시 일정 정리해서 올려주세요."></textarea>
        <div class="row" style="margin-top:var(--space-4); margin-bottom:0; justify-content:flex-end;">
          <button id="send">지시 전송</button>
        </div>
      </div>
    </div>
  </section>

  <!-- 에이전트 현황 — 흰 라이트 타일 -->
  <section class="tile tile-light">
    <div class="tile-inner">
      <div class="section-head">
        <h2 class="section-title">에이전트 현황</h2>
        <p class="section-sub">role 목록과 봇 활성 여부 · 담당 채널.</p>
      </div>
      <div class="roster" id="roster"></div>
      <div class="note" id="adminNote"></div>
    </div>
  </section>
</div>
<div class="toast" id="toast"></div>

<script>
const POLL_MS = 12000;
let channels = [];
let defaultPost = null;

function esc(s){ return (s||"").replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function fmtTime(ms){ if(!ms) return ""; const d=new Date(ms);
  return d.toLocaleString('ko-KR',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); }
function toast(t){ const el=document.getElementById('toast'); el.textContent=t;
  el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2600); }

// 제품색 인코딩: 채널의 kind + team_label 을 제품처럼 색 클래스로 매핑.
//   브리핑=Coral(시그니처) · 개발=Blue · 인사총무=Magenta · 관리=Purple.
function colorClass(c){
  if(c.kind==='briefing') return 'k-coral';
  const lab = (c.team_label||'');
  if(lab.indexOf('개발')>=0) return 'k-blue';
  if(lab.indexOf('인사')>=0||lab.indexOf('총무')>=0) return 'k-magenta';
  if(c.kind==='report') return 'k-purple';
  return 'k-blue';
}

async function api(path, opts){ const r=await fetch(path,opts);
  const d=await r.json().catch(()=>({error:'응답 파싱 실패'}));
  if(!r.ok) throw new Error(d.error||('HTTP '+r.status)); return d; }

async function loadChannels(){
  const d = await api('/api/channels');
  channels = d.channels; defaultPost = d.default_post_channel;
  // 현황 카드 골격(제품색 카드)
  const cards = document.getElementById('cards'); cards.innerHTML='';
  channels.forEach(c=>{
    const el=document.createElement('div'); el.className='card '+colorClass(c);
    el.innerHTML = `<h2>${esc(c.name)} <span class="badge ${c.kind}">`+
      `${c.kind==='team'?'팀':c.kind==='report'?'보고라인':'브리핑'}</span></h2>`+
      `<div class="msgs" id="m_${c.id}"><div class="empty">불러오는 중…</div></div>`;
    cards.appendChild(el);
  });
  // 채널 셀렉트
  const sel=document.getElementById('ch'); sel.innerHTML='';
  channels.forEach(c=>{ const o=document.createElement('option');
    o.value=c.name; o.textContent=c.name; if(c.name===defaultPost) o.selected=true;
    sel.appendChild(o); });
}

async function refreshOne(c){
  const box=document.getElementById('m_'+c.id); if(!box) return;
  try{
    const d=await api('/api/history?channel='+encodeURIComponent(c.name)+'&n=8');
    if(!d.items.length){ box.innerHTML='<div class="empty">메시지 없음</div>'; return; }
    box.innerHTML = d.items.map(m=>
      `<div class="msg"><span class="who">${esc(m.author)}</span>`+
      `<span class="when">${fmtTime(m.ts)}</span>`+
      `<div class="body">${esc(m.text)}</div></div>`).join('');
  }catch(e){ box.innerHTML='<div class="empty">로드 실패: '+esc(e.message)+'</div>'; }
}

async function refreshAll(){ for(const c of channels){ await refreshOne(c); } }

async function loadRoster(){
  const d=await api('/api/roles');
  const box=document.getElementById('roster'); box.innerHTML='';
  d.roles.forEach(r=>{
    const el=document.createElement('div'); el.className='agent';
    el.innerHTML=`<div class="nm"><span class="dot ${r.bot_active?'on':'off'}"></span>`+
      `${esc(r.name)} <span class="pill">(${esc(r.role)})</span></div>`+
      `<div class="meta">@${esc(r.username)} · 주담당: ${esc(r.primary)}<br>`+
      `채널: ${esc((r.channels||[]).join(', '))}<br>`+
      `봇 상태: ${r.bot_active?'활성':'비활성/미확인'}</div>`;
    box.appendChild(el);
  });
  const note=document.getElementById('adminNote');
  if(d.admin_channel){
    note.innerHTML='에이전트 정의 변경은 <b>'+esc(d.admin_channel)+'</b> 채널에서 '+
      '자연어로 지시 → diff 미리보기 → <b>적용</b> (ceo_admin_runtime 파이프라인). '+
      '이 대시보드는 모니터링·지시 전용이며 정의 파일은 변경하지 않습니다.';
  }
}

document.getElementById('send').addEventListener('click', async ()=>{
  const btn=document.getElementById('send');
  const ch=document.getElementById('ch').value;
  const txt=document.getElementById('msg').value.trim();
  if(!txt){ toast('내용을 입력하세요'); return; }
  btn.disabled=true;
  try{
    await api('/api/post',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({channel:ch,text:txt})});
    document.getElementById('msg').value='';
    toast('전송됨 → '+ch);
    const c=channels.find(x=>x.name===ch); if(c) refreshOne(c);
  }catch(e){ toast('전송 실패: '+e.message); }
  finally{ btn.disabled=false; }
});

function tickClock(){ document.getElementById('clock').textContent=
  new Date().toLocaleString('ko-KR'); }

(async function init(){
  document.getElementById('poll').textContent=Math.round(POLL_MS/1000);
  tickClock(); setInterval(tickClock,1000);
  try{ await loadChannels(); await refreshAll(); await loadRoster(); }
  catch(e){ toast('초기화 실패: '+e.message); }
  setInterval(refreshAll, POLL_MS);
  setInterval(loadRoster, POLL_MS*3);
})();
</script>
</body>
</html>"""


INDEX_HTML = build_index_html()


def main():
    if not WHITELIST:
        raise SystemExit("치명: 화이트리스트 채널이 비었습니다. channels.json/teams.json 을 확인하세요.")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"CEO 대시보드 가동 — http://{HOST}:{PORT}  (봇:{DASHBOARD_CONFIG} / 채널 {len(WHITELIST)}개)")
    print(f"  모니터링 채널: {', '.join(c['name'] for c in WHITELIST)}")
    print(f"  기본 지시 대상: {DEFAULT_POST_CHANNEL}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
        srv.shutdown()


if __name__ == "__main__":
    main()
