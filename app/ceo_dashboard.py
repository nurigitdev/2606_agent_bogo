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

    시각: MiniMax 디자인 시스템(모노크롬 + 제품색 인코딩, DM Sans, pill 버튼,
    그라데이션 제품 카드 32px / 조용한 흰 문서 카드 16px 라디우스 대비). CSS 토큰은
    :root 변수로 선언. 서버/API/JS 동작 계약(element id·fetch 형식)은 전부 보존한다.
    """
    return """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes CEO 대시보드</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&display=swap" rel="stylesheet">
<style>
  :root {
    /* ── 색 토큰: 모노크롬 베이스 ── */
    --color-ink:#0a0a0a;          /* 헤드라인 / CTA / primary 버튼 배경 */
    --color-charcoal:#1f1f1f;     /* 본문 */
    --color-slate:#52525b;        /* 보조 텍스트 */
    --color-steel:#71717a;        /* 비활성·메타 */
    --color-stone:#a1a1aa;        /* 가장 흐린 텍스트 */
    --color-canvas:#ffffff;       /* 배경 */
    --color-surface:#f4f4f5;      /* 섹션·비활성 배경 */
    --color-surface-2:#fafafa;    /* 입력 배경 */
    --color-hairline:#e4e4e7;     /* 1px 보더 */
    --color-hairline-strong:#d4d4d8;
    /* ── 브랜드/제품색: '제품 정체성 카드'에만 ── */
    --brand-coral:#ff4d3d;        /* CEO 시그니처 */
    --brand-coral-2:#ff7a45;
    --brand-magenta:#d61f69;      /* 인사총무 */
    --brand-magenta-2:#ec4899;
    --brand-blue:#2563eb;         /* 개발(Hailuo) */
    --brand-blue-2:#3b82f6;
    --brand-purple:#7c3aed;       /* 관리 */
    --brand-purple-2:#a855f7;
    /* ── 상태색 ── */
    --ok:#15803d; --ok-bg:#dcfce7;
    --bad:#b91c1c; --bad-bg:#fee2e2;
    /* ── 스페이싱(4px 베이스, 8px 증분) ── */
    --space-1:4px; --space-2:8px; --space-3:12px; --space-4:16px;
    --space-5:20px; --space-6:24px; --space-8:32px; --space-10:40px;
    --space-12:48px; --space-16:64px; --space-20:80px;
    /* ── 라디우스: 라디우스 대비가 시그니처 ── */
    --radius-product:32px;        /* 그라데이션 제품 카드 */
    --radius-doc:16px;            /* 조용한 흰 문서 카드 */
    --radius-sm:12px;             /* 입력·내부 요소 */
    --radius-pill:9999px;         /* 모든 버튼·뱃지 */
    --shadow-float:0 8px 28px rgba(10,10,10,.10);
  }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body {
    margin:0;
    font-family:"DM Sans","Inter","Helvetica Neue",Arial,
      "Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;
    background:var(--color-canvas); color:var(--color-charcoal);
    font-size:16px; line-height:1.5; word-break:keep-all;
    -webkit-font-smoothing:antialiased;
  }

  /* ── 상단 검정 promo-banner(한 줄, radius 0) ── */
  .promo-banner {
    background:var(--color-ink); color:#fff;
    font-size:13px; font-weight:500; letter-spacing:-.01em;
    text-align:center; padding:9px 20px;
  }
  .promo-banner b { font-weight:600; }

  /* ── 상단 흰 네비 바 ── */
  header {
    position:sticky; top:0; z-index:20;
    background:rgba(255,255,255,.88); backdrop-filter:saturate(180%) blur(12px);
    border-bottom:1px solid var(--color-hairline);
    padding:var(--space-4) var(--space-6);
    display:flex; align-items:center; gap:var(--space-4); flex-wrap:wrap;
  }
  .brand { display:flex; align-items:center; gap:var(--space-3); }
  .logo-mark {
    width:30px; height:30px; border-radius:9px; flex:0 0 auto;
    background:var(--color-ink); color:#fff;
    display:flex; align-items:center; justify-content:center;
    font-weight:700; font-size:15px; letter-spacing:-.02em;
  }
  header h1 {
    font-size:18px; font-weight:600; letter-spacing:-.02em;
    margin:0; color:var(--color-ink);
  }
  .nav-meta { margin-left:auto; display:flex; align-items:center; gap:var(--space-3); flex-wrap:wrap; }
  .pill {
    font-size:12px; font-weight:500; color:var(--color-steel);
    letter-spacing:-.01em;
  }
  .chip {
    display:inline-flex; align-items:center; gap:6px;
    font-size:12px; font-weight:500; color:var(--color-slate);
    background:var(--color-surface); border:1px solid var(--color-hairline);
    border-radius:var(--radius-pill); padding:6px 12px; white-space:nowrap;
  }
  .chip .live-dot {
    width:7px; height:7px; border-radius:50%;
    background:var(--ok); box-shadow:0 0 0 3px var(--ok-bg);
  }

  .wrap { max-width:1240px; margin:0 auto; padding:var(--space-10) var(--space-6) var(--space-20); }

  /* ── 섹션 헤더 ── */
  .section-head { margin:var(--space-12) 0 var(--space-5); }
  .section-head:first-child { margin-top:0; }
  .section-title {
    font-size:32px; font-weight:600; letter-spacing:-1px;
    color:var(--color-ink); margin:0; line-height:1.15;
  }
  .section-sub {
    font-size:14px; font-weight:400; color:var(--color-steel);
    margin:var(--space-2) 0 0; letter-spacing:-.01em;
  }

  /* ── 부서 카드 그리드: 그라데이션 제품 카드 ── */
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:var(--space-5); }
  .card {
    border-radius:var(--radius-product);
    padding:var(--space-8);
    min-height:200px; color:#fff;
    display:flex; flex-direction:column;
    background:linear-gradient(150deg,#3f3f46 0%,#27272a 100%);
    overflow:hidden;
  }
  /* 제품색 인코딩 — 채널/팀을 제품처럼 색 부여 */
  .card.k-blue    { background:linear-gradient(150deg,var(--brand-blue-2) 0%,var(--brand-blue) 100%); }
  .card.k-magenta { background:linear-gradient(150deg,var(--brand-magenta-2) 0%,var(--brand-magenta) 100%); }
  .card.k-coral   { background:linear-gradient(150deg,var(--brand-coral-2) 0%,var(--brand-coral) 100%); }
  .card.k-purple  { background:linear-gradient(150deg,var(--brand-purple-2) 0%,var(--brand-purple) 100%); }
  .card h2 {
    font-size:20px; font-weight:600; letter-spacing:-.02em; margin:0 0 var(--space-4);
    display:flex; justify-content:space-between; align-items:flex-start; gap:var(--space-3);
    line-height:1.2;
  }
  /* 뱃지: pill. 제품 카드 위에서는 반투명 흰 글래스 */
  .badge {
    flex:0 0 auto;
    font-size:12px; font-weight:600; letter-spacing:-.01em;
    padding:5px 12px; border-radius:var(--radius-pill);
    background:rgba(255,255,255,.20); color:#fff;
    backdrop-filter:blur(4px); white-space:nowrap;
  }
  .badge.team {}
  .badge.report { background:rgba(255,255,255,.14); }
  .badge.briefing { background:rgba(255,255,255,.26); }
  /* 카드 내부 메시지 영역: 제품색 위 반투명 흰 패널 */
  .msgs {
    margin-top:auto; max-height:280px; overflow-y:auto;
    background:rgba(255,255,255,.92); border-radius:var(--radius-sm);
    padding:var(--space-2) var(--space-4); color:var(--color-charcoal);
  }
  .msg { padding:var(--space-3) 0; border-top:1px solid var(--color-hairline); }
  .msg:first-child { border-top:none; }
  .msg .who { color:var(--color-ink); font-weight:600; font-size:13px; letter-spacing:-.01em; }
  .msg .when { color:var(--color-steel); font-size:12px; margin-left:6px; font-weight:400; }
  .msg .body {
    white-space:pre-wrap; word-break:break-word; margin-top:var(--space-1);
    line-height:1.5; font-size:14px; color:var(--color-charcoal);
  }
  .empty { color:var(--color-steel); font-size:13px; padding:var(--space-3) 0; font-weight:400; }
  .msgs .empty { color:var(--color-steel); }
  /* 제품 카드 골격(메시지 로드 전) 상태 텍스트 */
  .card > .msgs:only-of-type { }

  /* ── CEO 지시: 떠있는 흰 패널(약한 그림자 허용) ── */
  .composer {
    background:var(--color-canvas);
    border:1px solid var(--color-hairline);
    border-radius:var(--radius-doc);
    padding:var(--space-6);
    box-shadow:var(--shadow-float);
  }
  .composer .row { display:flex; gap:var(--space-3); margin-bottom:var(--space-3); flex-wrap:wrap; align-items:center; }
  .composer label { font-size:13px; font-weight:600; color:var(--color-slate); letter-spacing:-.01em; }
  select, textarea, button { font-family:inherit; }
  select {
    background:var(--color-surface-2); color:var(--color-charcoal);
    border:1px solid var(--color-hairline-strong);
    border-radius:var(--radius-pill); padding:9px 16px;
    font-size:14px; font-weight:500; cursor:pointer; min-height:40px;
  }
  select:focus { outline:none; border-color:var(--color-ink); }
  textarea {
    width:100%; min-height:96px;
    background:var(--color-surface-2); color:var(--color-charcoal);
    border:1px solid var(--color-hairline-strong);
    border-radius:var(--radius-sm); padding:var(--space-4); resize:vertical;
    font-size:16px; line-height:1.5; letter-spacing:-.01em;
  }
  textarea::placeholder { color:var(--color-stone); }
  textarea:focus { outline:none; border-color:var(--color-ink); }
  /* 모든 버튼 pill. primary = 검정 pill */
  button {
    background:var(--color-ink); color:#fff; border:none;
    border-radius:var(--radius-pill); padding:11px 24px;
    cursor:pointer; font-weight:600; font-size:14px; letter-spacing:-.01em;
    min-height:44px; transition:transform .12s ease, opacity .12s ease;
  }
  button:hover { opacity:.88; }
  button:active { transform:translateY(1px); }
  button:disabled { opacity:.4; cursor:not-allowed; }

  /* ── 에이전트 현황: 조용한 흰 문서 카드(flat, hairline 보더) ── */
  .roster { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:var(--space-4); }
  .agent {
    background:var(--color-canvas);
    border:1px solid var(--color-hairline);
    border-radius:var(--radius-doc);
    padding:var(--space-5);
  }
  .agent .nm {
    font-weight:600; font-size:16px; letter-spacing:-.02em; color:var(--color-ink);
    display:flex; align-items:center; gap:6px;
  }
  .agent .nm .pill { color:var(--color-stone); font-weight:500; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; flex:0 0 auto; }
  .dot.on { background:var(--ok); box-shadow:0 0 0 3px var(--ok-bg); }
  .dot.off { background:var(--color-stone); box-shadow:0 0 0 3px var(--color-surface); }
  .agent .meta {
    color:var(--color-steel); font-size:13px; margin-top:var(--space-3); line-height:1.6;
    letter-spacing:-.01em;
  }
  .note {
    color:var(--color-slate); font-size:13px; margin-top:var(--space-5); line-height:1.6;
    background:var(--color-surface); border:1px solid var(--color-hairline);
    border-radius:var(--radius-doc); padding:var(--space-5); letter-spacing:-.01em;
  }
  .note b { color:var(--color-ink); font-weight:600; }

  /* ── 토스트: 떠있는 패널 ── */
  .toast {
    position:fixed; bottom:var(--space-6); left:50%; transform:translateX(-50%) translateY(8px);
    background:var(--color-ink); color:#fff;
    padding:12px 22px; border-radius:var(--radius-pill);
    font-size:14px; font-weight:500; letter-spacing:-.01em;
    opacity:0; transition:opacity .25s ease, transform .25s ease;
    pointer-events:none; box-shadow:var(--shadow-float);
  }
  .toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
  a { color:var(--color-ink); font-weight:500; }

  /* ── 반응형 ── */
  @media (max-width:1024px){
    .grid { grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); }
    .roster { grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); }
    .section-title { font-size:28px; }
    .wrap { padding:var(--space-8) var(--space-5) var(--space-16); }
  }
  @media (max-width:768px){
    body { font-size:15px; }
    .grid { grid-template-columns:1fr; }
    .roster { grid-template-columns:1fr; }
    .card { padding:var(--space-6); border-radius:24px; min-height:0; }
    .section-title { font-size:24px; letter-spacing:-.5px; }
    .nav-meta { width:100%; margin-left:0; }
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
  <div class="section-head">
    <h2 class="section-title">부서별 현황</h2>
    <p class="section-sub">각 채널의 최근 활동을 12초마다 폴링해 보여줍니다.</p>
  </div>
  <div class="grid" id="cards"></div>

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

  <div class="section-head">
    <h2 class="section-title">에이전트 현황</h2>
    <p class="section-sub">role 목록과 봇 활성 여부 · 담당 채널.</p>
  </div>
  <div class="roster" id="roster"></div>
  <div class="note" id="adminNote"></div>
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
