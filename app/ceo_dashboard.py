"""
CEO 로컬 대시보드 — 부서 현황 모니터링 + 지시 송신 + 에이전트 현황 (단일 페이지).

CEO 가 Mattermost 의 여러 채널(인사총무팀/개발팀/보고라인/CEO브리핑)을 직접 오가지
않고, 브라우저 한 곳(http://127.0.0.1:<port>)에서:
  (a) 부서별 현황 카드  — 각 채널 최근 N건을 mm_client.history 로 폴링 표시
  (b) 지시 입력창       — 선택 채널(기본 CEO브리핑 → 박민철)에 메시지 게시
  (c) 에이전트 현황     — agent_schema.load_roles 의 role 목록 + 봇 활성 여부
  (d) 에이전트 개조 연계 — 역할별 학습방에서 자연어 지시 → ceo_admin_runtime 파이프라인 안내

설계 원칙(기존 BOGO 인프라 그대로 재사용, 신규 의존성·신규 API 키 0):
  - 통신:   mm_client.MM(REST) — 토큰은 박민철(nk_config.json)을 재사용. 박민철은
            CEO브리핑·양 보고라인 멤버라 읽기/쓰기 권한이 이미 있다.
  - 데이터: channels.json / teams.json / agents/*.md 를 agent_schema 로 로드.
            채널·팀·토큰을 코드에 하드코딩하지 않는다(전부 데이터 파일에서).
  - 보안:   웹서버는 반드시 127.0.0.1(루프백)에서만 listen. 외부 노출 금지.
            게시 가능한 채널은 화이트리스트(채널/보고라인/브리핑)로 제한.
  - 의존성: 표준 라이브러리(http.server)만 사용 — node/npm·FastAPI 등 추가 0.

실행:   <venv>/python ceo_dashboard.py        (포트 기본 8642, BOGO_DASHBOARD_PORT 로 변경)
        상시 가동은 launchd(com.bogo.dashboard) / systemd(bogo@dashboard) 가 소유 —
        service/install_service.sh 가 봇 4역할과 함께 KeepAlive 로 등록한다.
인증:   기존 봇 토큰(nk_config.json)만. ANTHROPIC_API_KEY 등 신규 키 요구 없음.

배포 모델: 읽기 전용 조회·게시만 하므로 git/파일쓰기 없음 → 미러/원본 구분 불요.
          데이터 파일은 HERE(실행 디렉터리) 기준으로 읽는다(미러든 원본이든 동일 사본).
"""
import http.cookies
import json
import os
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import agent_schema as A
import ceo_auth as AUTH
import mm_client as C
import vault_schema as VS

# Vault RAG 는 선택적 의존(sqlite 인덱스 + 선택적 로컬 임베딩). import/DB 가 없어도
# 대시보드가 죽지 않도록 graceful 하게 잡는다 — 검색 화면은 '인덱스 없음'으로 강등한다.
try:
    import vault_rag as VR
    _VAULT_RAG_ERR = None
except Exception as _e:  # noqa: BLE001 — 어떤 import 실패든 대시보드는 계속 떠야 한다
    VR = None
    _VAULT_RAG_ERR = f"{type(_e).__name__}: {_e}"

HERE = os.path.dirname(os.path.abspath(__file__))

# 루프백 전용. 외부(0.0.0.0)로 절대 바꾸지 말 것 — 인증 게이트 없는 로컬 대시보드다.
HOST = "127.0.0.1"
PORT = int(os.environ.get("BOGO_DASHBOARD_PORT", "8642"))

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

    팀 채널/보고라인 + CEO 브리핑만 허용한다(학습방은 역할별 개조 전용 파이프라인이라 제외).
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
# 에이전트 개조는 역할별 학습방(ceo_admin_runtime)에서 처리한다. 대시보드는 모니터링·지시
# 전용이므로 별도 관리 채널을 두지 않는다(admin 도 WHITELIST 만 보고 게시).

# ── 인증·세션 (ceo_auth) ─────────────────────────────────────────────────────
ACCOUNTS = AUTH.load_accounts()
SESSIONS = AUTH.SessionStore()
COOKIE_NAME = "bogo_sid"


def channels_for_role(identity):
    """로그인 신원(role)이 모니터링/조회할 수 있는 채널 화이트리스트를 반환.

    ceo/admin → 전체 화이트리스트(팀/보고라인/브리핑). 에이전트 개조는 학습방 전용 파이프라인.
    staff     → accounts_config 의 staff_channels 중 실재하는 것만.
    """
    role = identity["role"]
    if role in ("ceo", "admin"):
        return list(WHITELIST)
    # staff: 자기 부서 채널만.
    allowed = set(identity.get("staff_channels", []))
    return [c for c in WHITELIST if c["name"] in allowed]


def post_channels_for_role(identity):
    """role 이 메시지를 게시할 수 있는 채널명 집합(서버측 권한 강제의 단일 기준).

    ceo/admin → 조회 가능 채널 전부. staff → 자기 부서 채널만.
    """
    return {c["name"] for c in channels_for_role(identity)}


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

# 봇 활성 판정 캐시 (config -> {res, exp}). exp 는 time.monotonic() 기준 만료 시각.
# 성공(ok=True) 결과만 TTL 동안 캐시한다. 실패(ok=False)는 절대 캐시하지 않아
# 일시적 백엔드 장애(서버가 Mattermost 보다 먼저 기동·502/404 등) 직후 첫 폴링이
# 실패해도 다음 폴링에서 재시도되어 백엔드 회복 시 자동으로 활성 복구된다.
_bot_status_cache = {}
# 성공 결과 캐시 수명(초). 짧게 두어 user API 호출은 절감하되 계정 상태 변화도 반영.
_BOT_STATUS_TTL = 60.0


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


def fetch_history_any(channel_name, n=8):
    """채널 권한 검사 없이 history 조회(채널 권한은 호출측 role 기준이 강제).

    fetch_history 와 동일하지만 화이트리스트 제한이 없어, 모니터링 화이트리스트 밖이지만
    권한이 허용된 채널을 조회할 수 있다(범용 헬퍼).
    """
    if channel_name not in CHANNELS:
        raise ValueError(f"존재하지 않는 채널: {channel_name}")
    raw = mm.history(CHANNELS[channel_name], n=n)
    order = raw.get("order", [])
    posts = raw.get("posts", {})
    items = []
    for pid in reversed(order):
        p = posts.get(pid, {})
        if p.get("type"):
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
        # 같은 버그 클래스: user API 일시 장애 시 fallback 을 영구 캐시하면
        # 그 사용자가 이후에도 계속 "사람" 으로 고착된다. 실패는 캐시하지 않고
        # 다음 조회에서 재시도되게 한다(백엔드 회복 시 실제 닉네임 복구).
        return "사람"
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


def post_message_any(channel_name, text):
    """채널 게시(빈문자·길이 검증). 채널 권한은 호출측(role 기준)이 강제하므로,

    여기서는 channels.json 에 실재하는 채널이기만 하면 게시한다. 모니터링 화이트리스트
    밖이지만 권한이 허용된 채널을 위해 분리한다(범용 헬퍼).
    """
    if channel_name not in CHANNELS:
        raise ValueError(f"존재하지 않는 채널: {channel_name}")
    text = (text or "").strip()
    if not text:
        raise ValueError("빈 메시지는 보낼 수 없습니다.")
    if len(text) > 4000:
        raise ValueError("메시지가 너무 깁니다(4000자 제한).")
    return mm.post(CHANNELS[channel_name], text)


def bot_status(cfg):
    """봇(config) 활성 여부를 Mattermost user API 로 확인.

    캐시 정책(영구 비활성 고착 방지):
      - 성공(ok=True) 결과만 _BOT_STATUS_TTL 초 동안 캐시한다.
      - 실패(ok=False)·예외는 캐시하지 않아 다음 폴링에서 재시도되며,
        백엔드가 회복되면 그때 성공 결과로 자동 활성 복구된다.
    """
    now = time.monotonic()
    cached = _bot_status_cache.get(cfg)
    # 만료되지 않은 성공 캐시만 재사용(실패는 애초에 저장되지 않음).
    if cached is not None and cached["exp"] > now:
        return cached["res"]
    res = {"ok": False, "name": ""}
    try:
        bid = _bot_id_for(cfg)
        if bid:
            u = mm.user(bid)
            # delete_at==0 이면 활성 계정. 조회 성공 자체가 토큰·계정 유효 신호.
            res = {"ok": (u.get("delete_at", 0) == 0), "name": u.get("username", "")}
    except Exception:
        res = {"ok": False, "name": ""}
    # 성공만 캐시. 실패는 만료된 엔트리도 함께 제거해 stale 재사용을 원천 차단.
    if res["ok"]:
        _bot_status_cache[cfg] = {"res": res, "exp": now + _BOT_STATUS_TTL}
    else:
        _bot_status_cache.pop(cfg, None)
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


# ── Vault 조회 헬퍼 (전부 vault_schema 경유; 경로 traversal 원천 차단) ──────────
def vault_rag_status():
    """Vault RAG 사용 가능 여부 + 강등 사유. 화면 안내·degrade 분기의 단일 기준.

    반환 {ok, mode, reason}:
      - ok=False: 모듈 import 실패 또는 인덱스 DB 부재 → 검색은 '인덱스 없음' 안내.
      - mode: '의미+어휘'(임베딩 모델 로드됨) | '어휘(FTS5)단독' | '비활성'.
    """
    if VR is None:
        return {"ok": False, "mode": "비활성",
                "reason": _VAULT_RAG_ERR or "vault_rag 모듈 로드 실패"}
    if not os.path.isfile(VR.DB_PATH):
        return {"ok": False, "mode": "비활성",
                "reason": "RAG 인덱스 DB 없음(vault_rag.py index 로 색인 필요)"}
    try:
        mode = "의미+어휘" if VR.embed_available() else "어휘(FTS5)단독"
    except Exception:  # noqa: BLE001 — 임베딩 점검 실패도 검색 자체는 가능
        mode = "어휘(FTS5)단독"
    return {"ok": True, "mode": mode, "reason": ""}


def _obsidian_uri(rel_path):
    """노트 상대경로 → Obsidian URI(obsidian://open?vault=...&file=...).

    CEO 가 대시보드에서 클릭 한 번에 Obsidian 으로 같은 노트를 열 수 있게 한다.
    vault 이름은 VAULT_ROOT 폴더명, file 은 확장자(.md) 제외 경로(Obsidian 규약).
    """
    vault_name = os.path.basename(os.path.realpath(VS.VAULT_ROOT))
    file_no_ext = rel_path[:-3] if rel_path.endswith(".md") else rel_path
    return ("obsidian://open?vault=" + quote(vault_name, safe="")
            + "&file=" + quote(file_no_ext, safe=""))


def _note_abs_path(rel_path):
    """노트 상대경로를 Vault 루트 하위 절대경로로 안전 정규화(traversal 차단).

    vault_schema.vault_path 가 realpath 기준 commonpath 검사로 루트 이탈을 막는다.
    '../' 이나 절대경로 주입 시 ValueError 를 던지므로 호출측이 403/400 으로 거른다.
    """
    rel = (rel_path or "").strip().lstrip("/")
    if not rel or not rel.endswith(".md"):
        raise ValueError("유효하지 않은 노트 경로(.md 만 허용)")
    # vault_path 는 루트 밖으로 나가는 결합을 ValueError 로 차단한다(원천 봉쇄).
    return VS.vault_path(*rel.split("/"))


def _read_note_frontmatter(abs_path):
    """노트 파일을 frontmatter dict + 본문으로 파싱. 파일 없으면 None."""
    try:
        with open(abs_path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    fm, body = VS.parse_note(raw)
    return {"frontmatter": fm, "body": body}


def list_vault_notes(role=None, team=None, ntype=None, limit=200):
    """Vault 노트를 frontmatter 메타와 함께 최신순으로 나열(메타필터 지원).

    경로/팀/역할/type 은 전부 frontmatter 에서 읽는다(코드 하드코딩 0). date desc 정렬로
    최신 보고가 위로 온다. 템플릿 폴더는 제외(빈 양식이 목록을 오염시키지 않게).
    반환 [{path, id, type, role, team, date, title, snippet}].
    """
    root = os.path.realpath(VS.VAULT_ROOT)
    out = []
    for dirpath, _dirs, files in os.walk(root):
        base = os.path.basename(dirpath)
        if base == VS.DIR_TEMPLATES:  # 빈 양식 제외
            continue
        for fn in files:
            if not fn.endswith(".md"):
                continue
            abs_p = os.path.join(dirpath, fn)
            parsed = _read_note_frontmatter(abs_p)
            if parsed is None:
                continue
            fm = parsed["frontmatter"]
            body = parsed["body"] or ""
            r = fm.get("role", "") or ""
            t = fm.get("team", "") or ""
            ty = fm.get("type", "") or ""
            if role and r != role:
                continue
            if team and t != team:
                continue
            if ntype and ty != ntype:
                continue
            rel = os.path.relpath(abs_p, root)
            title = body.strip().splitlines()[0][:120] if body.strip() else fn
            out.append({
                "path": rel,
                "id": fm.get("id", ""),
                "type": ty,
                "role": r,
                "team": t,
                "date": fm.get("date", ""),
                "title": title,
                "snippet": body.strip().replace("\n", " ")[:160],
            })
    out.sort(key=lambda x: x.get("date", ""), reverse=True)
    return out[:limit]


def vault_facets():
    """노트 목록에서 role/team/type 선택지(파셋)를 동적 수집 → 브라우징 필터 UI 구성용."""
    notes = list_vault_notes(limit=10000)
    roles, teams, types = set(), set(), set()
    for n in notes:
        if n["role"]:
            roles.add(n["role"])
        if n["team"]:
            teams.add(n["team"])
        if n["type"]:
            types.add(n["type"])
    return {
        "roles": sorted(roles),
        "teams": sorted(teams),
        "types": sorted(types),
        "total": len(notes),
    }


def vault_search(query, role=None, team=None, ntype=None, top_k=8):
    """RAG 하이브리드 검색 위임(graceful). 인덱스/모듈 없으면 빈 결과 + 사유.

    반환 {ok, mode, reason, results[]}. results 는 vault_rag.search 의 형식을 그대로 전달
    (path/title/snippet/score/type/role/team/date) — 화면이 노트 링크로 연결한다.
    """
    st = vault_rag_status()
    if not st["ok"]:
        return {"ok": False, "mode": st["mode"], "reason": st["reason"], "results": []}
    q = (query or "").strip()
    if not q:
        return {"ok": True, "mode": st["mode"], "reason": "", "results": []}
    types = [ntype] if ntype else None
    try:
        # 대시보드는 CEO/admin 전체 조망 -> 가시성 필터 우회(role/team 은 순수 메타필터로만).
        hits = VR.search(q, role=role or None, team=team or None,
                         types=types, top_k=top_k, apply_visibility=False)
    except Exception as e:  # noqa: BLE001 — 검색 실패도 대시보드는 500 금지
        return {"ok": False, "mode": st["mode"],
                "reason": f"검색 실패: {type(e).__name__}", "results": []}
    _log_vault_search(q, hits, role, team)
    return {"ok": True, "mode": st["mode"], "reason": "", "results": hits}


def _log_vault_search(query, hits, role, team):
    """대시보드 RAG 검색 텔레메트리(실패 무해)."""
    try:
        import vault_telemetry as VT
        VT.log_retrieval(query, hits, viewer_role=role or "admin",
                         viewer_team=team or "", source="dashboard")
    except Exception:  # noqa: BLE001
        pass


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

    def _html(self, html, code=200, extra_headers=None):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel):
        """static/ 정적 파일 서빙(로고·파비콘). 경로 탈출 차단·MIME 매핑."""
        # 경로 정규화 후 static 디렉터리 밖이면 거부(directory traversal 방지)
        base = os.path.join(HERE, "static")
        target = os.path.normpath(os.path.join(base, rel.lstrip("/")))
        if not target.startswith(base + os.sep) or not os.path.isfile(target):
            return self._json({"error": "not found"}, 404)
        ext = os.path.splitext(target)[1].lower()
        ctype = {
            ".svg": "image/svg+xml", ".png": "image/png",
            ".ico": "image/x-icon", ".webp": "image/webp",
        }.get(ext, "application/octet-stream")
        with open(target, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    # ── 세션/쿠키 헬퍼 ──────────────────────────────────────────────────────
    def _identity(self):
        """요청 쿠키의 세션 토큰 -> 유효 신원. 없으면 None."""
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        try:
            jar = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return None
        m = jar.get(COOKIE_NAME)
        return SESSIONS.get(m.value) if m else None

    def _session_cookie(self, token):
        """HttpOnly·SameSite=Strict 세션 쿠키(루프백이라 Secure 생략)."""
        return (f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={AUTH.SESSION_TTL}")

    def _clear_cookie(self):
        return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"

    def _read_json_body(self):
        """POST 본문을 안전하게 JSON 파싱. 실패 시 None."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > 100_000:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _send_json_cookie(self, obj, cookie):
        """JSON 응답 + Set-Cookie 동시 전송."""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    # ── GET ─────────────────────────────────────────────────────────────────
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        # 정적 자산(로고·파비콘)은 인증 이전에 공개 서빙 — 로그인 화면에서도 필요.
        if path == "/favicon.ico":
            return self._send_static("favicon.ico")
        if path.startswith("/static/"):
            return self._send_static(path[len("/static/"):])
        ident = self._identity()
        if path == "/login":
            if ident:
                return self._html("", 302, [("Location", "/")])
            return self._html(LOGIN_HTML)
        # 그 외 모든 라우트는 인증 필수.
        if not ident:
            if path.startswith("/api/"):
                return self._json({"error": "인증 필요"}, 401)
            return self._html("", 302, [("Location", "/login")])
        if path in ("/", "/index.html"):
            return self._html(INDEX_HTML)
        if path == "/api/me":
            return self._json({"login_id": ident["login_id"], "role": ident["role"],
                               "label": ident.get("label", "")})
        if path == "/api/channels":
            chs = channels_for_role(ident)
            names = {c["name"] for c in chs}
            default = DEFAULT_POST_CHANNEL if DEFAULT_POST_CHANNEL in names else (
                chs[0]["name"] if chs else None)
            return self._json({"channels": chs, "default_post_channel": default})
        if path == "/api/roles":
            if ident["role"] == "staff":  # 직원은 에이전트 현황 접근 불가.
                return self._json({"error": "권한 없음"}, 403)
            return self._json({"roles": roster()})
        if path == "/api/history":
            return self._get_history(u, ident)
        # ── Vault(누적 기억) 라우트: ceo/admin 전용. 직원은 채널만. ──
        if path in ("/vault", "/vault/", "/vault.html"):
            if not self._vault_allowed(ident):
                return self._html("", 302, [("Location", "/")])
            return self._html(VAULT_HTML)
        if path == "/api/vault/list":
            return self._get_vault_list(u, ident)
        if path == "/api/vault/search":
            return self._get_vault_search(u, ident)
        if path == "/api/vault/note":
            return self._get_vault_note(u, ident)
        return self._json({"error": "not found"}, 404)

    def _vault_allowed(self, ident):
        """Vault 누적 기억 열람 권한: ceo/admin 만(staff 는 채널 모니터링까지)."""
        return ident.get("role") in ("ceo", "admin")

    def _get_vault_list(self, u, ident):
        """Vault 노트 브라우징: role/team/type 메타필터 + 파셋(선택지) 동시 반환."""
        if not self._vault_allowed(ident):
            return self._json({"error": "권한 없음"}, 403)
        q = parse_qs(u.query)
        role = (q.get("role") or [""])[0] or None
        team = (q.get("team") or [""])[0] or None
        ntype = (q.get("type") or [""])[0] or None
        try:
            notes = list_vault_notes(role=role, team=team, ntype=ntype)
            return self._json({"notes": notes, "facets": vault_facets(),
                               "rag": vault_rag_status()})
        except Exception as e:  # noqa: BLE001 — 조회 실패도 500 노출 최소화
            return self._json({"error": str(e)[:200]}, 500)

    def _get_vault_search(self, u, ident):
        """RAG 검색: q + 선택적 role/team/type 필터. 인덱스 없으면 graceful 안내."""
        if not self._vault_allowed(ident):
            return self._json({"error": "권한 없음"}, 403)
        q = parse_qs(u.query)
        query = (q.get("q") or [""])[0]
        role = (q.get("role") or [""])[0] or None
        team = (q.get("team") or [""])[0] or None
        ntype = (q.get("type") or [""])[0] or None
        try:
            k = max(1, min(20, int((q.get("k") or ["8"])[0])))
        except ValueError:
            k = 8
        try:
            res = vault_search(query, role=role, team=team, ntype=ntype, top_k=k)
            return self._json(res)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)[:200]}, 500)

    def _get_vault_note(self, u, ident):
        """단일 노트 열람: frontmatter + 본문. 경로는 Vault 루트 하위로 강제(이탈 차단)."""
        if not self._vault_allowed(ident):
            return self._json({"error": "권한 없음"}, 403)
        q = parse_qs(u.query)
        rel = (q.get("path") or [""])[0]
        try:
            abs_p = _note_abs_path(rel)  # traversal 시 ValueError
        except ValueError as e:
            return self._json({"error": f"허용되지 않은 경로: {e}"}, 400)
        parsed = _read_note_frontmatter(abs_p)
        if parsed is None:
            return self._json({"error": "노트를 찾을 수 없습니다."}, 404)
        norm_rel = os.path.relpath(abs_p, os.path.realpath(VS.VAULT_ROOT))
        return self._json({
            "path": norm_rel,
            "frontmatter": parsed["frontmatter"],
            "body": parsed["body"],
            "vault_file": abs_p,
            "obsidian_uri": _obsidian_uri(norm_rel),
        })

    def _get_history(self, u, ident):
        """채널 메시지 조회. role 별 접근 채널을 서버측에서 강제."""
        q = parse_qs(u.query)
        ch = (q.get("channel") or [""])[0]
        try:
            n = max(1, min(30, int((q.get("n") or ["8"])[0])))
        except ValueError:
            n = 8
        if ch not in {c["name"] for c in channels_for_role(ident)}:
            return self._json({"error": "허용되지 않은 채널"}, 403)
        try:
            return self._json({"channel": ch, "items": fetch_history_any(ch, n)})
        except urllib.error.URLError as e:
            return self._json({"error": f"Mattermost 연결 실패: {e}"}, 502)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)[:200]}, 500)

    # ── POST ────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            return self._do_login()
        if path == "/api/logout":
            return self._do_logout()
        if path == "/api/post":
            return self._do_post()
        return self._json({"error": "not found"}, 404)

    def _do_login(self):
        data = self._read_json_body()
        if data is None:
            return self._json({"error": "잘못된 요청 본문"}, 400)
        ident = AUTH.authenticate(data.get("login_id", ""), data.get("password", ""), ACCOUNTS)
        if not ident:
            return self._json({"error": "아이디 또는 비밀번호가 올바르지 않습니다."}, 401)
        token = SESSIONS.create(ident)
        return self._send_json_cookie({"ok": True, "role": ident["role"]},
                                      self._session_cookie(token))

    def _do_logout(self):
        raw = self.headers.get("Cookie", "")
        if raw:
            try:
                m = http.cookies.SimpleCookie(raw).get(COOKIE_NAME)
                if m:
                    SESSIONS.destroy(m.value)
            except http.cookies.CookieError:
                pass
        return self._send_json_cookie({"ok": True}, self._clear_cookie())

    def _do_post(self):
        """채널 게시. 인증 + role 별 게시 가능 채널을 서버측에서 강제."""
        ident = self._identity()
        if not ident:
            return self._json({"error": "인증 필요"}, 401)
        data = self._read_json_body()
        if data is None:
            return self._json({"error": "잘못된 요청 본문"}, 400)
        ch = (data.get("channel") or "").strip()
        text = (data.get("text") or "").strip()
        # 프론트 숨김이 아니라 서버가 권한을 강제한다.
        if ch not in post_channels_for_role(ident):
            return self._json({"error": "이 채널에 게시할 권한이 없습니다."}, 403)
        try:
            res = post_message_any(ch, text)
            return self._json({"ok": True, "post_id": res.get("id", ""), "channel": ch})
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except urllib.error.URLError as e:
            return self._json({"error": f"Mattermost 연결 실패: {e}"}, 502)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)[:200]}, 500)


# ── 공유 파비콘 링크 (전 페이지 <head> 공통) ─────────────────────────────────
_FAVICON_LINKS = """
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16.png">
<link rel="shortcut icon" href="/favicon.ico">
<link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192.png">
<meta name="theme-color" content="#1C5FAE">"""


# ── 공유 CSS (Apple 디자인 시스템; 로그인·메인 공통) ──────────────────────────
_CSS = """
  :root {
    --ink:#1d1d1f; --ink-soft:#424245; --ink-muted:#6e6e73;
    --ink-faint:#86868b;
    --blue:#0066cc; --blue-sky:#2997ff;
    --nav-black:#000000; --canvas:#ffffff; --parchment:#f5f5f7;
    --tile-dark:#1d1d1f; --tile-dark-2:#2a2a2c; --tile-dark-3:#252527;
    --hairline:#e0e0e0; --hairline-soft:#d2d2d7;
    --on-dark:#ffffff; --on-dark-muted:#cccccc;
    --ok:#1d8a4e; --off:#86868b;
    --space-1:4px; --space-2:8px; --space-3:12px; --space-4:16px;
    --space-5:20px; --space-6:24px; --space-8:32px; --space-10:40px;
    --space-12:48px; --space-16:64px; --space-20:80px;
    --r-pill:9999px; --r-lg:18px; --r-md:11px; --r-sm:8px;
    --shadow:rgba(0,0,0,0.22) 3px 5px 30px;
    --sidebar-w:288px;
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
  /* 로고 마크: 투명 배경 SVG 이미지(에이전트 BOGO M/나비 마크). 배경·테두리 없음. */
  .logo-mark {
    width:30px; height:30px; flex:0 0 auto;
    display:block; object-fit:contain;
    background:transparent; border:none;
  }
  header h1 { font-size:19px; font-weight:600; letter-spacing:-0.32px; margin:0; color:var(--ink); }
  .nav-meta { margin-left:auto; display:flex; align-items:center; gap:var(--space-3); flex-wrap:wrap; }
  .pill { font-size:13px; font-weight:400; color:var(--ink-muted); letter-spacing:-0.2px; }
  .chip {
    display:inline-flex; align-items:center; gap:7px;
    font-size:13px; font-weight:400; color:var(--on-dark);
    background:var(--ink); border:none;
    border-radius:var(--r-sm); padding:7px 13px; white-space:nowrap; letter-spacing:-0.2px;
  }
  .chip .live-dot { width:6px; height:6px; border-radius:50%; background:var(--ok); }
  .chip.logout { background:transparent; color:var(--blue); cursor:pointer;
    border:1px solid var(--hairline-soft); transition:transform .14s ease; }
  .chip.logout:active { transform:scale(0.95); }
  a.chip.nav-link { background:var(--blue); color:var(--on-dark); cursor:pointer;
    text-decoration:none; transition:transform .14s ease; }
  a.chip.nav-link:hover { text-decoration:none; }
  a.chip.nav-link:active { transform:scale(0.95); }
  /* ── 보고 뷰: CEO브리핑 우선 고정 카드 ── */
  .row-card.pinned { border:1px solid var(--accent-line); background:var(--accent-soft); }
  .rc-badge { flex:0 0 auto; font-size:11px; font-weight:700; color:var(--on-dark);
    background:var(--accent); border-radius:999px; padding:1px 8px; margin-left:6px; }
  /* ── Vault(기억 보관소) ── */
  .vault-toolbar { display:flex; gap:var(--space-3); flex-wrap:wrap; align-items:center;
    margin-bottom:var(--space-6); }
  .vault-search { display:flex; gap:var(--space-3); flex:1 1 320px; min-width:280px; }
  .vault-search input { flex:1; min-height:44px; background:var(--canvas); color:var(--ink);
    border:1px solid var(--hairline-soft); border-radius:var(--r-pill);
    padding:0 var(--space-5); font-size:15px; letter-spacing:-0.2px; }
  .vault-search input:focus { outline:none; border-color:var(--blue); box-shadow:0 0 0 3px var(--accent-soft); }
  .rag-badge { font-size:12px; color:var(--ink-muted); letter-spacing:-0.2px;
    padding:5px 12px; border:1px solid var(--hairline-soft); border-radius:var(--r-pill); }
  .rag-badge.off { color:#b3261e; border-color:rgba(179,38,30,.4); }
  .note-list { display:grid; grid-template-columns:repeat(auto-fill,minmax(380px,1fr));
    gap:var(--space-5); }
  .note-item { background:var(--canvas); border:1px solid var(--hairline);
    border-radius:var(--r-lg); padding:var(--space-5); cursor:pointer;
    transition:transform .12s ease, box-shadow .12s ease; }
  .note-item:hover { box-shadow:var(--shadow); transform:translateY(-1px); }
  .note-item .nt-title { font-weight:600; font-size:16px; color:var(--ink);
    letter-spacing:-0.3px; line-height:1.35; margin-bottom:var(--space-2); }
  .note-item .nt-meta { font-size:12px; color:var(--ink-muted); letter-spacing:-0.2px;
    display:flex; gap:8px; flex-wrap:wrap; margin-bottom:var(--space-2); }
  .note-item .nt-tag { background:var(--parchment); border:1px solid var(--hairline);
    border-radius:var(--r-pill); padding:2px 10px; }
  .note-item .nt-snip { font-size:14px; color:var(--ink-soft); line-height:1.5;
    letter-spacing:-0.2px; }
  .note-item .nt-score { color:var(--blue); font-weight:600; }
  .vault-filters { display:flex; gap:var(--space-3); flex-wrap:wrap; align-items:center; }
  .vault-filters select { min-height:40px; padding:7px 16px; font-size:14px; }
  .modal-back { position:fixed; inset:0; background:rgba(0,0,0,.45); display:none;
    z-index:50; align-items:flex-start; justify-content:center; padding:var(--space-10) var(--space-4);
    overflow-y:auto; }
  .modal-back.show { display:flex; }
  .modal { background:var(--canvas); border-radius:var(--r-lg); max-width:820px; width:100%;
    box-shadow:var(--shadow); padding:var(--space-8); }
  .modal h2 { font-size:24px; font-weight:600; letter-spacing:-0.34px; margin:0 0 var(--space-4);
    color:var(--ink); line-height:1.25; }
  .modal .fm { font-size:13px; color:var(--ink-muted); margin-bottom:var(--space-5);
    line-height:1.7; letter-spacing:-0.2px; word-break:break-all; }
  .modal .fm b { color:var(--ink-soft); }
  .modal .links { display:flex; gap:var(--space-3); flex-wrap:wrap; margin-bottom:var(--space-5); }
  .modal .links a { font-size:13px; padding:7px 14px; border:1px solid var(--hairline-soft);
    border-radius:var(--r-pill); color:var(--blue); }
  .modal .nbody { white-space:pre-wrap; word-break:break-word; line-height:1.6;
    font-size:15px; color:var(--ink-soft); border-top:1px solid var(--hairline);
    padding-top:var(--space-5); }
  .modal .mclose { float:right; cursor:pointer; color:var(--ink-muted); font-size:22px;
    line-height:1; border:none; background:none; padding:0; min-height:auto; }
  .role-tag { font-size:12px; font-weight:600; letter-spacing:-0.1px;
    padding:5px 12px; border-radius:var(--r-pill);
    background:var(--blue); color:var(--on-dark); white-space:nowrap; }
  .wrap { max-width:1440px; margin:0 auto; padding:0; }
  .tile { padding:var(--space-20) var(--space-8); }
  .tile-light { background:var(--canvas); }
  .tile-parchment { background:var(--parchment); }
  .tile-dark { background:var(--tile-dark); }
  .tile-inner { max-width:1280px; margin:0 auto; }
  .section-head { margin:0 0 var(--space-12); }
  .section-title { font-size:40px; font-weight:600; letter-spacing:-0.374px;
    color:var(--ink); margin:0; line-height:1.1; }
  .tile-dark .section-title { color:var(--on-dark); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:var(--space-6); }
  .card {
    border-radius:var(--r-lg); padding:var(--space-6);
    min-height:220px; color:var(--on-dark);
    display:flex; flex-direction:column; background:var(--tile-dark-2); overflow:hidden;
  }
  .card.k-coral   { box-shadow:inset 4px 0 0 var(--blue); }
  .card.k-blue    { box-shadow:inset 4px 0 0 rgba(255,255,255,.55); }
  .card.k-magenta { box-shadow:inset 4px 0 0 rgba(255,255,255,.38); }
  .card.k-purple  { box-shadow:inset 4px 0 0 rgba(255,255,255,.24); }
  .card.k-admin   { box-shadow:inset 4px 0 0 var(--blue-sky); }
  .card h2 {
    font-size:21px; font-weight:600; letter-spacing:-0.34px; margin:0 0 var(--space-4);
    display:flex; justify-content:space-between; align-items:flex-start; gap:var(--space-3);
    line-height:1.2; color:var(--on-dark);
  }
  .badge {
    flex:0 0 auto; font-size:12px; font-weight:400; letter-spacing:-0.1px;
    padding:4px 11px; border-radius:var(--r-pill);
    background:transparent; color:var(--on-dark-muted);
    border:1px solid rgba(255,255,255,.28); white-space:nowrap;
  }
  .badge.briefing { color:var(--blue-sky); border-color:rgba(41,151,255,.5); }
  .badge.admin { color:var(--blue-sky); border-color:rgba(41,151,255,.5); }
  .msgs {
    margin-top:auto; max-height:280px; overflow-y:auto;
    background:var(--canvas); border-radius:var(--r-md);
    padding:var(--space-2) var(--space-4); color:var(--ink);
  }
  .msg { padding:var(--space-3) 0; border-top:1px solid var(--hairline); }
  .msg:first-child { border-top:none; }
  .msg .who { color:var(--ink); font-weight:600; font-size:14px; letter-spacing:-0.2px; }
  .msg .when { color:var(--ink-faint); font-size:12px; margin-left:7px; font-weight:400; }
  .msg .body { white-space:pre-wrap; word-break:break-word; margin-top:var(--space-1);
    line-height:1.47; font-size:15px; color:var(--ink-soft); }
  .empty { color:var(--ink-muted); font-size:14px; padding:var(--space-3) 0; font-weight:400; }
  .msgs .empty { color:var(--ink-muted); }
  .composer { background:var(--canvas); border:1px solid var(--hairline);
    border-radius:var(--r-lg); padding:var(--space-6); }
  .composer .row { display:flex; gap:var(--space-3); margin-bottom:var(--space-4);
    flex-wrap:wrap; align-items:center; }
  .composer label { font-size:15px; font-weight:600; color:var(--ink); letter-spacing:-0.2px; }
  select, textarea, button, input { font-family:inherit; }
  select {
    background:var(--canvas); color:var(--ink);
    border:1px solid var(--hairline-soft);
    border-radius:var(--r-pill); padding:9px 18px;
    font-size:15px; font-weight:400; cursor:pointer; min-height:44px; letter-spacing:-0.2px;
  }
  select:focus { outline:none; border-color:var(--blue); box-shadow:0 0 0 3px var(--accent-soft); }
  textarea {
    width:100%; min-height:104px;
    background:var(--canvas); color:var(--ink);
    border:1px solid var(--hairline-soft);
    border-radius:var(--r-lg); padding:var(--space-4); resize:vertical;
    font-size:17px; line-height:1.47; letter-spacing:-0.374px;
  }
  textarea::placeholder { color:var(--ink-faint); }
  textarea:focus { outline:none; border-color:var(--blue); box-shadow:0 0 0 3px var(--accent-soft); }
  button {
    background:var(--blue); color:var(--on-dark); border:none;
    border-radius:var(--r-pill); padding:11px 22px;
    cursor:pointer; font-weight:400; font-size:17px; letter-spacing:-0.32px;
    min-height:44px; transition:transform .14s ease;
  }
  button:active { transform:scale(0.95); }
  button:disabled { opacity:.36; cursor:not-allowed; }
  .roster { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:var(--space-5); }
  .agent { background:var(--canvas); border:1px solid var(--hairline);
    border-radius:var(--r-lg); padding:var(--space-6); }
  .agent .nm { font-weight:600; font-size:17px; letter-spacing:-0.32px; color:var(--ink);
    display:flex; align-items:center; gap:8px; }
  .agent .nm .pill { color:var(--ink-faint); font-weight:400; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; flex:0 0 auto; }
  .dot.on { background:var(--ok); }
  .dot.off { background:var(--off); }
  .agent .meta { color:var(--ink-muted); font-size:14px; margin-top:var(--space-3);
    line-height:1.6; letter-spacing:-0.2px; }
  .note { color:var(--ink-soft); font-size:15px; margin-top:var(--space-6); line-height:1.6;
    background:var(--parchment); border:1px solid var(--hairline);
    border-radius:var(--r-lg); padding:var(--space-6); letter-spacing:-0.2px; }
  .note b { color:var(--ink); font-weight:600; }
  .toast {
    position:fixed; bottom:var(--space-8); left:50%; transform:translateX(-50%) translateY(8px);
    background:var(--ink); color:var(--on-dark);
    padding:13px 24px; border-radius:var(--r-pill);
    font-size:15px; font-weight:400; letter-spacing:-0.2px;
    opacity:0; transition:opacity .25s ease, transform .25s ease;
    pointer-events:none; box-shadow:var(--shadow);
  }
  .toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
  /* ── 로그인 ── */
  .login-wrap { min-height:calc(100vh - 44px); display:flex; align-items:center;
    justify-content:center; background:var(--parchment); padding:var(--space-8); }
  .login-card { background:var(--canvas); border:1px solid var(--hairline);
    border-radius:var(--r-lg); padding:var(--space-12) var(--space-10);
    width:100%; max-width:420px; box-shadow:var(--shadow); }
  .login-card .logo-mark { width:44px; height:44px; margin:0 auto var(--space-6); }
  .login-card h1 { font-size:28px; font-weight:600; letter-spacing:-0.4px; text-align:center;
    margin:0 0 var(--space-8); color:var(--ink); }
  .login-card .field { margin-bottom:var(--space-4); }
  .login-card label { display:block; font-size:14px; font-weight:600; color:var(--ink-soft);
    margin-bottom:var(--space-2); letter-spacing:-0.2px; }
  .login-card input { width:100%; min-height:48px; background:var(--canvas); color:var(--ink);
    border:1px solid var(--hairline-soft); border-radius:var(--r-md);
    padding:0 var(--space-4); font-size:17px; letter-spacing:-0.32px; }
  .login-card input:focus { outline:none; border-color:var(--blue); box-shadow:0 0 0 3px var(--accent-soft); }
  .login-card button { width:100%; margin-top:var(--space-4); }
  .login-err { color:#b3261e; font-size:14px; margin-top:var(--space-4); min-height:18px;
    text-align:center; letter-spacing:-0.2px; }
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
    .nav-meta { width:100%; margin-left:0; }
    header { padding:var(--space-4) var(--space-5); }
    .tile { padding:var(--space-12) var(--space-5); }
    .composer .row { gap:var(--space-2); }
    select { width:100%; }
    .login-card { padding:var(--space-10) var(--space-6); }
  }

  /* ══ SaaS 워크스페이스 레이아웃(메인 대시보드 전용) ══════════════════════════
     원칙: 한 화면 1차 작업(메인=입력창+응답만) · progressive disclosure · 뷰 분리(SPA) ·
     ⌘K 팔레트 · 우측 슬라이드 패널 · 타입 스케일(13/14/16/20/24) · 4·8px 간격 ·
     위계는 색이 아닌 크기·굵기 · 단일 액센트 + 약한 그림자 · 150~200ms subtle 모션. */
  :root {
    --fz-13:13px; --fz-14:14px; --fz-16:16px; --fz-20:20px; --fz-24:24px;
    --gap-1:4px; --gap-2:8px; --gap-3:12px; --gap-4:16px; --gap-5:20px; --gap-6:24px; --gap-8:32px;
    --side-w:264px; --col-w:744px;
    --accent:#0066cc; --accent-soft:rgba(0,102,204,.10); --accent-line:rgba(0,102,204,.30);
    --shadow-1:0 1px 2px rgba(0,0,0,.05); --shadow-2:0 4px 16px rgba(0,0,0,.08);
    --shadow-panel:-8px 0 32px rgba(0,0,0,.14);
    --motion:170ms cubic-bezier(.4,0,.2,1);
  }
  body.app-shell { height:100vh; overflow:hidden; }
  .ws { display:flex; height:calc(100vh - 44px); background:var(--canvas); }

  /* ── 사이드바(얇게 240~280px, 그룹화, 접힘, active 표시) ── */
  .nav { width:var(--side-w); flex:0 0 var(--side-w); background:var(--parchment);
    border-right:1px solid var(--hairline); display:flex; flex-direction:column; height:100%;
    transition:margin-left var(--motion); }
  .nav.collapsed { margin-left:calc(-1 * var(--side-w)); }
  .nav-head { display:flex; align-items:center; gap:var(--gap-3);
    padding:var(--gap-4) var(--gap-5); flex:0 0 auto; }
  .nav-head .logo-mark { width:26px; height:26px; }
  .nav-head .brandname { font-weight:600; font-size:var(--fz-16); letter-spacing:-0.3px; color:var(--ink); }
  /* 사이드바 접기 버튼(nav-head 우측 끝) */
  .nav-collapse { margin-left:auto; flex:0 0 auto; width:28px; height:28px; min-height:28px; padding:0;
    display:flex; align-items:center; justify-content:center; background:transparent; border:none;
    border-radius:var(--r-sm); color:var(--ink-muted); cursor:pointer; transition:background var(--motion); }
  .nav-collapse:hover { background:rgba(0,0,0,.05); color:var(--ink-soft); }
  /* 펴기 버튼(stage 좌상단, 접힘 시에만 노출)
     주의: .icon-btn{display:flex}가 소스 뒤에 정의돼 동일 특이도(0,1,0)에서
     소스 순서로 .nav-open{display:none}을 이긴다(navOpen이 펼친 상태에도 노출되는 버그).
     따라서 .stage 후손 셀렉터로 특이도를 (0,2,0)/(0,3,0)으로 올려 .icon-btn을 확실히 이기게 한다. */
  .nav-open { position:absolute; top:var(--gap-3); left:var(--gap-3); z-index:4; }
  .stage .nav-open { display:none; }
  .stage.nav-collapsed .nav-open { display:flex; }
  .nav-scroll { flex:1 1 auto; overflow-y:auto; padding:var(--gap-2) var(--gap-3) var(--gap-4); }
  .nav-foot { flex:0 0 auto; border-top:1px solid var(--hairline); padding:var(--gap-3) var(--gap-4); }
  /* ⌘K 트리거 */
  .cmdk-trigger { width:100%; display:flex; align-items:center; gap:var(--gap-2);
    background:var(--canvas); color:var(--ink-muted); border:1px solid var(--hairline-soft);
    border-radius:var(--r-md); padding:9px 12px; font-size:var(--fz-14); min-height:40px;
    letter-spacing:-0.2px; margin-bottom:var(--gap-4); cursor:pointer; transition:border-color var(--motion); }
  .cmdk-trigger:hover { border-color:var(--hairline); }
  .cmdk-trigger .kbd { margin-left:auto; font-size:11px; color:var(--ink-faint);
    border:1px solid var(--hairline-soft); border-radius:5px; padding:1px 6px; font-weight:600; }
  /* 네비 그룹 */
  .nav-group { margin-bottom:var(--gap-5); }
  .nav-group-label { font-size:11px; font-weight:600; letter-spacing:0.5px; text-transform:uppercase;
    color:var(--ink-faint); padding:0 var(--gap-3); margin-bottom:var(--gap-1); }
  .nav-item { display:flex; align-items:center; gap:var(--gap-3); width:100%; background:transparent;
    color:var(--ink-soft); border:none; border-radius:var(--r-sm); padding:8px var(--gap-3);
    font-size:var(--fz-14); font-weight:500; letter-spacing:-0.2px; cursor:pointer; min-height:38px;
    text-align:left; transition:background var(--motion), color var(--motion); position:relative; }
  .nav-item:hover { background:rgba(0,0,0,.045); }
  .nav-item.active { background:var(--accent-soft); color:var(--ink); font-weight:600; }
  .nav-item.active::before { content:""; position:absolute; left:0; top:7px; bottom:7px; width:3px;
    border-radius:0 3px 3px 0; background:var(--accent); }
  .nav-item .ni-ico { width:18px; height:18px; flex:0 0 auto; opacity:.85;
    display:inline-flex; align-items:center; justify-content:center; }
  .nav-item .ni-txt { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .nav-item .ni-count { flex:0 0 auto; font-size:11px; font-weight:600; color:var(--ink-muted);
    background:var(--canvas); border:1px solid var(--hairline-soft); border-radius:var(--r-pill);
    padding:1px 8px; min-width:20px; text-align:center; display:none; }
  .nav-item .ni-count.show { display:block; }
  .nav-item .ni-count.alert { color:var(--accent); border-color:var(--accent-line);
    background:var(--accent-soft); }
  /* 새 작업 = 단독 primary 버튼 */
  .nav-primary { display:flex; align-items:center; gap:var(--gap-3); width:100%; min-height:42px;
    background:var(--accent-soft); color:var(--accent); border:none; border-radius:var(--r-md);
    padding:9px var(--gap-3); font-size:var(--fz-14); font-weight:600; letter-spacing:-0.2px;
    cursor:pointer; text-align:left; margin-bottom:var(--gap-5);
    transition:background var(--motion); }
  .nav-primary:hover { background:var(--accent-line); }
  .nav-primary.active::before { content:none; }
  .nav-primary .ni-ico { width:18px; height:18px; flex:0 0 auto; display:inline-flex;
    align-items:center; justify-content:center; }
  .nav-primary .ni-txt { flex:1; }
  /* 프로필(하단) */
  .profile { display:flex; align-items:center; gap:var(--gap-3); }
  .profile .avatar { width:32px; height:32px; border-radius:50%; flex:0 0 auto; background:var(--ink);
    color:var(--on-dark); display:flex; align-items:center; justify-content:center;
    font-weight:600; font-size:var(--fz-13); }
  .profile .pf-meta { flex:1; min-width:0; }
  .profile .pf-name { font-weight:600; font-size:var(--fz-14); color:var(--ink); letter-spacing:-0.2px;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .profile .pf-role { font-size:var(--fz-13); color:var(--ink-muted); letter-spacing:-0.1px; }
  .profile .pf-logout { flex:0 0 auto; background:transparent; color:var(--ink-muted);
    border:1px solid var(--hairline-soft); border-radius:var(--r-sm); padding:6px 10px;
    font-size:var(--fz-13); font-weight:500; min-height:auto; transition:color var(--motion); }
  .profile .pf-logout:hover { color:var(--ink); }

  /* ── 메인 영역 ── */
  .stage { flex:1 1 auto; display:flex; flex-direction:column; min-width:0; height:100%; position:relative; }
  .icon-btn { background:transparent; border:1px solid var(--hairline-soft); border-radius:var(--r-sm);
    width:36px; height:36px; min-height:36px; padding:0; display:flex; align-items:center;
    justify-content:center; cursor:pointer; color:var(--ink-soft); font-size:16px;
    transition:background var(--motion); }
  .icon-btn:hover { background:rgba(0,0,0,.04); }

  .stage-scroll { flex:1 1 auto; overflow-y:auto; }
  .col { max-width:var(--col-w); margin:0 auto; padding:var(--gap-8) var(--gap-6) var(--gap-6); }
  .view-head { margin-bottom:var(--gap-6); }
  .view-head h2 { font-size:var(--fz-24); font-weight:700; letter-spacing:-0.4px; color:var(--ink);
    margin:0 0 var(--gap-1); line-height:1.2; }
  .view-head p { font-size:var(--fz-14); color:var(--ink-muted); margin:0; letter-spacing:-0.2px; }

  /* ── 인라인 아이콘(Lucide 스타일, currentColor 상속) ── */
  .icn { width:16px; height:16px; flex:0 0 auto; display:inline-block; vertical-align:middle;
    stroke:currentColor; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; fill:none; }
  .icn-18 { width:18px; height:18px; }
  /* ── 채팅 빈 상태: 입력창만(히어로·제안카드 전면 제거) ── */
  .chat-empty { height:100%; }
  /* ── 채팅 2모드 ──
     채팅 상태: .dock 은 normal flex flow 로 .stage-scroll 아래(화면 하단)에 고정.
       → 메시지가 누적돼도 입력창이 대화 위에 떠서 가리지 않는다(absolute 센터링 회귀 제거).
     빈 상태(is-empty): 메시지 0 → 입력창만 화면 정중앙.
       빈 stage-scroll(콘텐츠 없음)을 flex:0 으로 접고, .dock 을 상하 auto margin 으로
       세로 중앙에 띄운다(absolute/transform 없이 normal flow 안에서 센터링). */
  .stage.is-empty .stage-scroll { flex:0 0 auto; }
  .stage.is-empty .dock { margin-top:auto; margin-bottom:auto; }
  /* 첫 전송 FLIP 후 첫 버블 페이드인 */
  @keyframes bubbleIn { from{ opacity:0; transform:translateY(8px); } to{ opacity:1; transform:none; } }
  .bubble-row.fresh { animation:bubbleIn 180ms cubic-bezier(.4,0,.2,1); }
  @media (prefers-reduced-motion:reduce){
    .bubble-row.fresh { animation:none; }
    .dock { transition:none !important; }
  }

  /* 대화 버블 */
  .bubble-row { display:flex; margin-bottom:var(--gap-5); }
  .bubble-row.me { justify-content:flex-end; }
  .bubble-row.sys { justify-content:flex-start; }
  .bubble { max-width:86%; border-radius:var(--r-lg); padding:var(--gap-4) var(--gap-5);
    font-size:var(--fz-16); line-height:1.55; letter-spacing:-0.2px; white-space:pre-wrap; word-break:break-word; }
  .bubble-row.me .bubble { background:var(--accent); color:var(--on-dark); border-bottom-right-radius:var(--r-sm); }
  .bubble-row.sys .bubble { background:var(--parchment); color:var(--ink-soft);
    border:1px solid var(--hairline); border-bottom-left-radius:var(--r-sm); }
  .bubble .b-meta { font-size:var(--fz-13); opacity:.7; margin-top:6px; letter-spacing:-0.1px; }

  /* ── progressive disclosure: 한 줄 카드(클릭 시 우측 패널) ── */
  .row-card { display:flex; align-items:center; gap:var(--gap-3); width:100%; text-align:left;
    background:var(--canvas); border:1px solid var(--hairline); border-radius:var(--r-md);
    padding:13px var(--gap-4); margin-bottom:var(--gap-2); cursor:pointer; min-height:auto;
    transition:border-color var(--motion), box-shadow var(--motion); }
  .row-card:hover { border-color:var(--accent-line); box-shadow:var(--shadow-1); }
  .row-card .rc-dot { width:8px; height:8px; border-radius:50%; flex:0 0 auto; background:var(--off); }
  .row-card .rc-dot.on { background:var(--ok); }
  .row-card .rc-title { flex:1; font-size:var(--fz-14); font-weight:600; color:var(--ink);
    letter-spacing:-0.2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .row-card .rc-sub { font-size:var(--fz-13); color:var(--ink-muted); letter-spacing:-0.1px;
    flex:0 0 auto; max-width:46%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .row-card .rc-chev { color:var(--ink-faint); font-size:15px; flex:0 0 auto; }
  .list-empty { color:var(--ink-muted); font-size:var(--fz-14); padding:var(--gap-6) 0; text-align:center; }

  /* ── 통합 입력창: normal flex flow 로 .stage 하단에 고정(채팅 누적 시에도 대화를 가리지 않음).
     빈 상태에서만 .stage.is-empty .dock 규칙이 상하 auto margin 으로 세로 중앙 배치한다. ── */
  .dock { flex:0 0 auto; padding:0 var(--gap-6) var(--gap-5); background:var(--canvas); }
  .dock-shell { max-width:var(--col-w); margin:0 auto; }
  .chat-box { display:flex; align-items:flex-end; gap:var(--gap-2); background:var(--canvas);
    border:1px solid var(--hairline-soft); border-radius:26px; padding:8px 8px 8px var(--gap-5);
    box-shadow:var(--shadow-1); transition:border-color var(--motion), box-shadow var(--motion); }
  .chat-box:focus-within { border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  .chat-box textarea { flex:1 1 auto; border:none; background:transparent; box-shadow:none;
    min-height:26px; max-height:200px; padding:9px 0; margin:0; resize:none;
    font-size:var(--fz-16); line-height:1.5; letter-spacing:-0.2px; }
  .chat-box textarea:focus { outline:none; border:none; box-shadow:none; }
  .send-btn { flex:0 0 auto; width:38px; height:38px; min-height:38px; padding:0; border-radius:50%;
    background:var(--ink); color:var(--on-dark); display:flex; align-items:center; justify-content:center;
    font-size:17px; line-height:1; transition:background var(--motion); }
  .send-btn:disabled { background:var(--hairline-soft); color:var(--ink-faint); opacity:1; }

  /* ── 우측 슬라이드 패널(상세) ── */
  .panel-scrim { position:fixed; inset:0; background:rgba(0,0,0,.32); opacity:0; pointer-events:none;
    transition:opacity var(--motion); z-index:60; }
  .panel-scrim.show { opacity:1; pointer-events:auto; }
  .panel { position:fixed; top:0; right:0; height:100vh; width:min(520px,92vw); background:var(--canvas);
    box-shadow:var(--shadow-panel); transform:translateX(100%); transition:transform var(--motion);
    z-index:61; display:flex; flex-direction:column; }
  .panel.show { transform:translateX(0); }
  .panel-head { flex:0 0 auto; display:flex; align-items:flex-start; gap:var(--gap-3);
    padding:var(--gap-5) var(--gap-6); border-bottom:1px solid var(--hairline); }
  .panel-head .ph-title { flex:1; font-size:var(--fz-20); font-weight:700; letter-spacing:-0.3px;
    color:var(--ink); line-height:1.3; margin:0; }
  .panel-head .ph-close { flex:0 0 auto; background:transparent; border:none; color:var(--ink-muted);
    font-size:22px; line-height:1; cursor:pointer; padding:2px 6px; min-height:auto; }
  .panel-head .ph-close:hover { color:var(--ink); }
  .panel-body { flex:1 1 auto; overflow-y:auto; padding:var(--gap-5) var(--gap-6); }
  .panel-meta { display:flex; gap:var(--gap-2); flex-wrap:wrap; margin-bottom:var(--gap-4); }
  .panel-meta .pm-tag { font-size:var(--fz-13); color:var(--ink-muted); background:var(--parchment);
    border:1px solid var(--hairline); border-radius:var(--r-pill); padding:3px 11px; letter-spacing:-0.1px; }
  .panel-msg { border-top:1px solid var(--hairline); padding:var(--gap-3) 0; }
  .panel-msg:first-child { border-top:none; }
  .panel-msg .who { font-size:var(--fz-14); font-weight:600; color:var(--ink); letter-spacing:-0.2px; }
  .panel-msg .when { font-size:var(--fz-13); color:var(--ink-faint); margin-left:7px; }
  .panel-msg .body { white-space:pre-wrap; word-break:break-word; margin-top:5px; line-height:1.55;
    font-size:var(--fz-14); color:var(--ink-soft); }
  .panel-section-t { font-size:var(--fz-13); font-weight:600; letter-spacing:0.4px; text-transform:uppercase;
    color:var(--ink-faint); margin:var(--gap-5) 0 var(--gap-2); }
  .panel-kv { font-size:var(--fz-14); color:var(--ink-soft); line-height:1.7; letter-spacing:-0.2px; }
  .panel-kv b { color:var(--ink); font-weight:600; }
  .panel .note { margin-top:var(--gap-5); }

  /* ── ⌘K 커맨드 팔레트 ── */
  .cmdk-back { position:fixed; inset:0; background:rgba(0,0,0,.35); display:none; z-index:80;
    align-items:flex-start; justify-content:center; padding:12vh var(--gap-4) var(--gap-4); }
  .cmdk-back.show { display:flex; }
  .cmdk { width:100%; max-width:600px; background:var(--canvas); border:1px solid var(--hairline);
    border-radius:var(--r-lg); box-shadow:var(--shadow-2); overflow:hidden;
    animation:cmdkIn var(--motion); }
  @keyframes cmdkIn { from{ opacity:0; transform:translateY(-8px) scale(.99); } to{ opacity:1; transform:none; } }
  .cmdk input { width:100%; border:none; border-bottom:1px solid var(--hairline); background:var(--canvas);
    color:var(--ink); padding:var(--gap-5) var(--gap-6); font-size:var(--fz-16); letter-spacing:-0.2px; }
  .cmdk input:focus { outline:none; }
  .cmdk-list { max-height:54vh; overflow-y:auto; padding:var(--gap-2); }
  .cmdk-cat { font-size:11px; font-weight:600; letter-spacing:0.5px; text-transform:uppercase;
    color:var(--ink-faint); padding:var(--gap-2) var(--gap-3) var(--gap-1); }
  .cmdk-item { display:flex; align-items:center; gap:var(--gap-3); width:100%; background:transparent;
    border:none; border-radius:var(--r-sm); padding:10px var(--gap-3); font-size:var(--fz-14);
    color:var(--ink-soft); letter-spacing:-0.2px; cursor:pointer; text-align:left; min-height:auto; }
  .cmdk-item:hover, .cmdk-item.cur { background:var(--accent-soft); color:var(--ink); }
  .cmdk-item .ci-ico { width:18px; height:18px; flex:0 0 auto; opacity:.8;
    display:inline-flex; align-items:center; justify-content:center; }
  .cmdk-item .ci-txt { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .cmdk-item .ci-kind { font-size:var(--fz-13); color:var(--ink-faint); flex:0 0 auto; }
  .cmdk-empty { color:var(--ink-muted); font-size:var(--fz-14); padding:var(--gap-5); text-align:center; }

  .scrim { display:none; }

  /* ══ 보고 워크플로(reports view) — 평탄 report 리스트 + 컨트롤바 + 처리 풋바 ══
     ChatGPT 웹 적용: 행에 상태점·제목·요약·팀배지·작성자·상대시간·NEW 노출,
     호버 액션바(승인·핀·후속·복사), 안읽음은 색 아닌 굵기로도 구분(WCAG AA). */
  .rep-stat { display:flex; gap:var(--gap-4); flex-wrap:wrap; font-size:var(--fz-14);
    color:var(--ink-muted); letter-spacing:-0.2px; margin-top:var(--gap-1); }
  .rep-stat b { color:var(--ink); font-weight:600; }
  .rep-bar { display:flex; gap:var(--gap-2); flex-wrap:wrap; align-items:center;
    margin-bottom:var(--gap-5); }
  .rep-search { flex:1 1 220px; min-width:180px; display:flex; align-items:center; gap:var(--gap-2);
    background:var(--canvas); border:1px solid var(--hairline-soft); border-radius:var(--r-md);
    padding:0 var(--gap-3); min-height:40px; transition:border-color var(--motion); }
  .rep-search:focus-within { border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  .rep-search input { flex:1; border:none; background:transparent; font-size:var(--fz-14);
    letter-spacing:-0.2px; padding:9px 0; color:var(--ink); }
  .rep-search input:focus { outline:none; }
  .rep-search .icn { color:var(--ink-faint); }
  .rep-seg { display:flex; gap:0; background:var(--parchment); border:1px solid var(--hairline);
    border-radius:var(--r-md); padding:2px; }
  .rep-seg button { background:transparent; border:none; border-radius:var(--r-sm);
    font-size:var(--fz-13); font-weight:500; color:var(--ink-muted); letter-spacing:-0.2px;
    padding:6px 12px; min-height:32px; cursor:pointer; transition:background var(--motion), color var(--motion); }
  .rep-seg button.on { background:var(--canvas); color:var(--ink); font-weight:600; box-shadow:var(--shadow-1); }
  .rep-chip { background:var(--canvas); border:1px solid var(--hairline-soft); border-radius:var(--r-pill);
    font-size:var(--fz-13); font-weight:500; color:var(--ink-soft); letter-spacing:-0.2px;
    padding:6px 13px; min-height:34px; cursor:pointer; transition:all var(--motion); }
  .rep-chip.on { background:var(--accent-soft); border-color:var(--accent-line); color:var(--accent); font-weight:600; }
  .rep-sort { background:var(--canvas); border:1px solid var(--hairline-soft); border-radius:var(--r-md);
    font-size:var(--fz-13); color:var(--ink-soft); padding:7px var(--gap-3); min-height:38px; cursor:pointer; }
  .rep-sort:focus { outline:none; border-color:var(--accent); }

  .rep-sec { font-size:11px; font-weight:600; letter-spacing:.5px; text-transform:uppercase;
    color:var(--ink-faint); margin:var(--gap-5) 0 var(--gap-2); }
  .rep-sec:first-of-type { margin-top:0; }
  .rep-row { display:flex; align-items:flex-start; gap:var(--gap-3); width:100%; position:relative;
    background:var(--canvas); border:1px solid var(--hairline); border-radius:var(--r-md);
    padding:12px var(--gap-4); margin-bottom:var(--gap-2); cursor:pointer; text-align:left;
    transition:border-color var(--motion), box-shadow var(--motion); }
  .rep-row:hover { border-color:var(--accent-line); box-shadow:var(--shadow-1); }
  .rep-row:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  .rep-row.unread .rep-title { font-weight:700; }
  .rep-dot { width:8px; height:8px; border-radius:50%; margin-top:6px; flex:0 0 auto; }
  .rep-dot.st-new { background:var(--accent); }
  .rep-dot.st-prog { background:#e0a008; }
  .rep-dot.st-done { background:var(--ink-faint); }
  .rep-main { flex:1 1 auto; min-width:0; }
  .rep-title { font-size:var(--fz-14); font-weight:600; color:var(--ink); line-height:1.4;
    -webkit-line-clamp:1; display:-webkit-box; -webkit-box-orient:vertical; overflow:hidden; }
  .rep-snip { font-size:var(--fz-13); color:var(--ink-muted); line-height:1.5; margin-top:2px;
    -webkit-line-clamp:1; display:-webkit-box; -webkit-box-orient:vertical; overflow:hidden; }
  .rep-meta { display:flex; align-items:center; gap:7px; flex-wrap:wrap; margin-top:6px; }
  .rep-teambadge { font-size:11px; font-weight:600; padding:2px 8px; border-radius:var(--r-pill);
    background:var(--parchment); border:1px solid var(--hairline); color:var(--ink-soft); }
  .rep-who { font-size:var(--fz-13); color:var(--ink-muted); letter-spacing:-0.1px; }
  .rep-new { font-size:10px; font-weight:700; color:var(--on-dark); background:var(--accent);
    border-radius:var(--r-pill); padding:1px 7px; letter-spacing:.3px; }
  .rep-pinmark { color:var(--accent); font-size:12px; }
  .rep-actions { display:flex; gap:2px; margin-left:auto; flex:0 0 auto; opacity:0;
    transition:opacity var(--motion); }
  .rep-row:hover .rep-actions, .rep-row:focus-within .rep-actions { opacity:1; }
  .rep-iconbtn { width:32px; height:32px; min-height:32px; padding:0; display:flex; align-items:center;
    justify-content:center; background:transparent; border:1px solid transparent; border-radius:var(--r-sm);
    color:var(--ink-muted); cursor:pointer; transition:background var(--motion), color var(--motion); }
  .rep-iconbtn:hover { background:var(--accent-soft); color:var(--accent); }
  .rep-iconbtn.on { color:var(--accent); }

  /* 패널: 보고 전문 문서형(680px 가독폭) + CEO 코멘트 우측 버블 */
  .rep-doc { max-width:680px; }
  .rep-doc .rep-docmeta { display:flex; gap:var(--gap-2); flex-wrap:wrap; align-items:center;
    margin-bottom:var(--gap-4); }
  .rep-doc .body { font-size:var(--fz-16); line-height:1.6; color:var(--ink-soft);
    white-space:pre-wrap; word-break:break-word; }
  .rep-statusline { display:flex; align-items:center; gap:var(--gap-2); margin-bottom:var(--gap-4);
    font-size:var(--fz-13); color:var(--ink-muted); }
  .rep-statuspill { font-size:11px; font-weight:600; padding:3px 10px; border-radius:var(--r-pill);
    border:1px solid var(--hairline); }
  .rep-statuspill.st-new { color:var(--accent); border-color:var(--accent-line); background:var(--accent-soft); }
  .rep-statuspill.st-prog { color:#946700; border-color:rgba(224,160,8,.4); background:rgba(224,160,8,.1); }
  .rep-statuspill.st-done { color:var(--ink-muted); background:var(--parchment); }
  .rep-cmt { display:flex; justify-content:flex-end; margin:var(--gap-3) 0; }
  .rep-cmt .bubble { max-width:86%; background:var(--accent); color:var(--on-dark);
    border-radius:var(--r-lg); border-bottom-right-radius:var(--r-sm); padding:var(--gap-3) var(--gap-4);
    font-size:var(--fz-14); line-height:1.5; white-space:pre-wrap; word-break:break-word; }
  .rep-cmt .bubble .b-meta { font-size:11px; opacity:.75; margin-top:4px; }
  .rep-panel-foot { flex:0 0 auto; border-top:1px solid var(--hairline);
    padding:var(--gap-4) var(--gap-6); display:flex; gap:var(--gap-2); flex-wrap:wrap; }
  .rep-btn { font-size:var(--fz-14); font-weight:600; letter-spacing:-0.2px; border-radius:var(--r-md);
    padding:9px var(--gap-4); min-height:40px; cursor:pointer; border:1px solid var(--hairline-soft);
    background:var(--canvas); color:var(--ink-soft); transition:all var(--motion); }
  .rep-btn:hover { border-color:var(--accent-line); color:var(--ink); }
  .rep-btn.approve { background:var(--accent); color:var(--on-dark); border-color:var(--accent); }
  .rep-btn.approve:hover { filter:brightness(1.06); }
  .rep-btn.reject { background:transparent; color:#b3261e; border:1px solid rgba(179,38,30,.4); }
  .rep-btn.reject:hover { background:rgba(179,38,30,.06); }

  /* 빈 상태 3종 가이드 */
  .rep-empty { text-align:center; padding:var(--gap-8) var(--gap-4); color:var(--ink-muted); }
  .rep-empty .re-ico { width:44px; height:44px; margin:0 auto var(--gap-4); color:var(--ink-faint);
    display:flex; align-items:center; justify-content:center; background:var(--parchment);
    border-radius:50%; }
  .rep-empty .re-ico .icn { width:22px; height:22px; }
  .rep-empty .re-t { font-size:var(--fz-16); font-weight:600; color:var(--ink); margin-bottom:6px; }
  .rep-empty .re-d { font-size:var(--fz-14); line-height:1.5; margin-bottom:var(--gap-4); }
  .rep-empty .rep-btn { display:inline-flex; align-items:center; }

  /* 반려 사유 모달 */
  .rep-modal-back { position:fixed; inset:0; background:rgba(0,0,0,.4); display:none; z-index:90;
    align-items:center; justify-content:center; padding:var(--gap-4); }
  .rep-modal-back.show { display:flex; }
  .rep-modal { background:var(--canvas); border-radius:var(--r-lg); box-shadow:var(--shadow-2);
    width:100%; max-width:440px; padding:var(--gap-6); }
  .rep-modal h3 { font-size:var(--fz-16); font-weight:700; margin:0 0 var(--gap-3); color:var(--ink); }
  .rep-modal textarea { width:100%; min-height:90px; border:1px solid var(--hairline-soft);
    border-radius:var(--r-md); padding:var(--gap-3); font-size:var(--fz-14); resize:vertical;
    font-family:inherit; letter-spacing:-0.2px; }
  .rep-modal textarea:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  .rep-modal .rep-modal-err { color:#b3261e; font-size:var(--fz-13); min-height:18px; margin-top:6px; }
  .rep-modal .rep-modal-foot { display:flex; gap:var(--gap-2); justify-content:flex-end; margin-top:var(--gap-3); }
  @media (prefers-reduced-motion:reduce){ .rep-actions { transition:none; } }
  @media (hover:none){ .rep-actions { opacity:1; } }

  @media (max-width:768px){
    .ws { position:relative; }
    .nav { position:absolute; z-index:50; top:0; left:0; box-shadow:var(--shadow-2); }
    .nav.collapsed { margin-left:calc(-1 * var(--side-w)); box-shadow:none; }
    .scrim { display:none; position:absolute; inset:0; z-index:45; background:rgba(0,0,0,.35); }
    .scrim.show { display:block; }
    .col, .dock-shell { max-width:100%; }
  }
"""


def build_login_html():
    """로그인 페이지(부제 없음, 헤드라인만). 데모 자격증명은 힌트로 명시."""
    return """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>에이전트 BOGO 로그인</title>""" + _FAVICON_LINKS + """
<style>""" + _CSS + """</style>
</head>
<body>
<div class="login-wrap">
  <form class="login-card" id="loginForm" autocomplete="off">
    <img class="logo-mark" src="/static/logo.svg" alt="에이전트 BOGO" width="44" height="44">
    <h1>에이전트 BOGO 로그인</h1>
    <div class="field">
      <label for="lid">아이디</label>
      <input id="lid" type="text" autocomplete="username" placeholder="아이디" required>
    </div>
    <div class="field">
      <label for="pw">비밀번호</label>
      <input id="pw" type="password" autocomplete="current-password" placeholder="비밀번호" required>
    </div>
    <button id="loginBtn" type="submit">로그인</button>
    <div class="login-err" id="err"></div>
  </form>
</div>
<script>
document.getElementById('loginForm').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const btn=document.getElementById('loginBtn'), err=document.getElementById('err');
  err.textContent=''; btn.disabled=true;
  try{
    const r=await fetch('/api/login',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({login_id:document.getElementById('lid').value,
        password:document.getElementById('pw').value})});
    const d=await r.json().catch(()=>({error:'응답 파싱 실패'}));
    if(!r.ok){ err.textContent=d.error||('HTTP '+r.status); btn.disabled=false; return; }
    location.href='/';
  }catch(ex){ err.textContent='로그인 실패: '+ex.message; btn.disabled=false; }
});
</script>
</body>
</html>"""


def build_index_html():
    """역할 인지형 SaaS 워크스페이스. /api/me 의 role 에 따라 화면·권한을 분기한다.

    Apple 디자인 토큰(SF Pro·단일 Action Blue·active scale·시스템 그림자)을 _CSS 로 계승.
    - ceo/admin: 전체 보고·에이전트 현황·기억 보관소 + 전체 채널 지시
    - staff:     자기 부서 보고·자기 채널 지시(에이전트 현황·기억 보관소 비노출)
    SaaS 워크스페이스 패턴(2026): 메인은 입력창+응답 스트림 하나만. 보고·과거질문·에이전트
    현황은 SPA 뷰 전환으로 분리(한 화면 동시 노출 금지). 상단 3-숫자 스트립 + ⌘K 팔레트 +
    우측 슬라이드 패널(상세). 서버 계약 무변경: /api/me·channels·history·post·roles·logout.
    """
    return """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>에이전트 BOGO 워크스페이스</title>""" + _FAVICON_LINKS + """
<style>""" + _CSS + """</style>
</head>
<body class="app-shell">
<div class="ws">
  <!-- 사이드바: 얇게, 그룹화, active 표시, 접힘 -->
  <nav class="nav" id="nav">
    <div class="nav-head">
      <img class="logo-mark" src="/static/logo.svg" alt="에이전트 BOGO" width="26" height="26">
      <span class="brandname">에이전트 BOGO</span>
      <button class="nav-collapse" id="navCollapse" title="사이드바 접기 (&#8984;\\)" aria-label="사이드바 접기 (Cmd+\\)"><svg class="icn icn-18" viewBox="0 0 24 24" aria-hidden="true"><rect width="18" height="18" x="3" y="3" rx="2"></rect><path d="M9 3v18"></path></svg></button>
    </div>
    <div class="nav-scroll">
      <button class="cmdk-trigger" id="cmdkTrigger">
        <svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8"></circle><path d="m21 21-4.3-4.3"></path></svg>
        <span>검색·이동</span><span class="kbd" id="cmdkKbd">&#8984;K</span>
      </button>
      <button class="nav-primary" data-view="chat" id="navChat">
        <span class="ni-ico"><svg class="icn icn-18" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"></path></svg></span><span class="ni-txt">새 작업</span>
      </button>
      <div class="nav-group">
        <div class="nav-group-label">워크스페이스</div>
        <button class="nav-item" data-view="history" id="navHistory">
          <span class="ni-ico"><svg class="icn icn-18" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"></path><path d="M3 3v5h5"></path><path d="M12 7v5l4 2"></path></svg></span><span class="ni-txt">과거 질문</span>
          <span class="ni-count" id="cntHistory">0</span>
        </button>
        <button class="nav-item" data-view="reports" id="navReports">
          <span class="ni-ico"><svg class="icn icn-18" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"></path><path d="M14 2v4a2 2 0 0 0 2 2h4"></path><path d="M10 9H8"></path><path d="M16 13H8"></path><path d="M16 17H8"></path></svg></span><span class="ni-txt">보고</span>
          <span class="ni-count" id="cntReports">0</span>
        </button>
        <button class="nav-item" data-view="roster" id="navRoster" style="display:none">
          <span class="ni-ico"><svg class="icn icn-18" viewBox="0 0 24 24" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M22 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg></span><span class="ni-txt">에이전트 현황</span>
          <span class="ni-count" id="cntRoster">0</span>
        </button>
      </div>
      <div class="nav-group" id="navSettings" style="display:none; border-top:1px solid var(--hairline); padding-top:var(--gap-4)">
        <a class="nav-item" id="navVault" href="/vault" style="display:none">
          <span class="ni-ico"><svg class="icn icn-18" viewBox="0 0 24 24" aria-hidden="true"><rect width="20" height="5" x="2" y="3" rx="1"></rect><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"></path><path d="M10 12h4"></path></svg></span><span class="ni-txt">기억 보관소</span>
        </a>
      </div>
    </div>
    <div class="nav-foot">
      <div class="profile">
        <span class="avatar" id="pfAvatar">H</span>
        <div class="pf-meta">
          <div class="pf-name" id="pfName">…</div>
          <div class="pf-role" id="pfRole"></div>
        </div>
        <button class="pf-logout" id="logout">로그아웃</button>
      </div>
    </div>
  </nav>
  <div class="scrim" id="scrim"></div>

  <!-- 메인 무대(한 번에 한 뷰) -->
  <main class="stage">
    <button class="icon-btn nav-open" id="navOpen" title="사이드바 펴기 (&#8984;\\)" aria-label="사이드바 펴기 (Cmd+\\)"><svg class="icn icn-18" viewBox="0 0 24 24" aria-hidden="true"><rect width="18" height="18" x="3" y="3" rx="2"></rect><path d="M9 3v18"></path></svg></button>
    <div class="stage-scroll" id="stageScroll">
      <div id="stageBody"></div>
    </div>
    <!-- 통합 입력창(채팅 뷰에서만 노출) -->
    <div class="dock" id="dock">
      <div class="dock-shell">
        <div class="chat-box">
          <textarea id="msg" rows="1" placeholder="기억·지시·질문을 입력하세요."></textarea>
          <button class="send-btn" id="send" title="전송" aria-label="전송" disabled><svg class="icn icn-18" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 7-7 7 7"></path><path d="M12 19V5"></path></svg></button>
        </div>
      </div>
    </div>
  </main>
</div>

<!-- 우측 슬라이드 패널(상세) -->
<div class="panel-scrim" id="panelScrim"></div>
<aside class="panel" id="panel" aria-hidden="true">
  <div class="panel-head">
    <h2 class="ph-title" id="panelTitle"></h2>
    <button class="ph-close" id="panelClose" title="닫기" aria-label="닫기"><svg class="icn icn-18" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"></path><path d="m6 6 12 12"></path></svg></button>
  </div>
  <div class="panel-body" id="panelBody"></div>
  <div class="rep-panel-foot" id="panelFoot" style="display:none"></div>
</aside>

<!-- ⌘K 커맨드 팔레트 -->
<div class="cmdk-back" id="cmdkBack">
  <div class="cmdk" role="dialog" aria-modal="true">
    <input id="cmdkInput" type="text" placeholder="질문 검색 · 에이전트 이동 · 보고 열기…" autocomplete="off">
    <div class="cmdk-list" id="cmdkList"></div>
  </div>
</div>

<!-- 반려 사유 모달 -->
<div class="rep-modal-back" id="repModalBack">
  <div class="rep-modal" role="dialog" aria-modal="true" aria-labelledby="repRejectTitle">
    <h3 id="repRejectTitle">반려 사유</h3>
    <textarea id="repRejectReason" placeholder="반려 사유를 입력하세요(필수)"></textarea>
    <div class="rep-modal-err" id="repRejectErr"></div>
    <div class="rep-modal-foot">
      <button class="rep-btn" id="repRejectCancel">취소</button>
      <button class="rep-btn reject" id="repRejectConfirm">반려 확정</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>
<script>
let channels = [], defaultPost = null, me = null;
let teamGroups = [];        // [{label, channels:[...]}]
let sessions = [];          // localStorage 보존 대화 세션
let activeSession = null;
let roster = [];            // 에이전트 현황 캐시
let view = 'chat';          // 'chat' | 'history' | 'reports' | 'roster'
let cmdkIdx = 0, cmdkRows = [];
const POLL_MS = 15000;
let panelTimer = null;

function esc(s){ return (s||"").replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtTime(ms){ if(!ms) return ""; const d=new Date(ms);
  return d.toLocaleString('ko-KR',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); }
function toast(t){ const el=document.getElementById('toast'); el.textContent=t;
  el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2600); }
function kindLabel(k){ return k==='team'?'팀':k==='report'?'보고라인':'브리핑'; }

async function api(path, opts){
  const r=await fetch(path,opts);
  if(r.status===401){ location.href='/login'; throw new Error('세션 만료'); }
  const d=await r.json().catch(()=>({error:'응답 파싱 실패'}));
  if(!r.ok) throw new Error(d.error||('HTTP '+r.status)); return d;
}

// ── 대화 세션(localStorage; 서버 스키마 무변경) ──────────────────────────────
function sessKey(){ return 'bogo_sessions_'+((me&&me.login_id)||'anon'); }
function loadSessions(){
  try{ sessions = JSON.parse(localStorage.getItem(sessKey())||'[]'); }catch(e){ sessions=[]; }
  if(!Array.isArray(sessions)) sessions=[];
}
function saveSessions(){
  try{ localStorage.setItem(sessKey(), JSON.stringify(sessions.slice(0,100))); }catch(e){}
}
function newSession(){
  activeSession = { id:'s'+Date.now()+Math.random().toString(36).slice(2,6),
    title:'새 작업', channel:defaultPost, ts:Date.now(), msgs:[], pending:false };
  return activeSession;
}
function pendingCount(){ return sessions.filter(s=>s.pending).length; }

// ── 팀↔채널 그룹(teams.json 기반: team_label) ───────────────────────────────
function buildTeamGroups(){
  const order=[], map={};
  channels.forEach(c=>{
    const label=c.team_label||(c.kind==='briefing'?'CEO':'기타');
    if(!(label in map)){ map[label]={label,channels:[]}; order.push(label); }
    map[label].channels.push(c);
  });
  teamGroups=order.map(l=>map[l]);
}
// CEO브리핑(orchestrator briefing) 채널 식별: kind==='briefing' 우선, 없으면 이름에 '브리핑' 포함
function briefingChannel(){
  return channels.find(c=>c.kind==='briefing') || channels.find(c=>/브리핑/.test(c.name)) || null;
}
// 게시 가능한 첫 채널(staff 기본 전송 채널 보정용)
function firstPostChannel(){ return (channels[0] && channels[0].name) || defaultPost || null; }

// ══ 보고 워크플로 도메인 모델 ════════════════════════════════════════════════
// 서버 {author,text,ts}를 report 단위로 승격(채널×메시지 평탄화). report.id=channel#ts.
// 워크플로 상태(read·pinned·status·decision·comment)는 localStorage bogoFlow:<login_id>.
let reports=[];             // 평탄화된 report 객체 캐시(메모리)
let repFilter={ q:'', status:'all', teams:new Set(), sort:'unhandled' };
let repLoading=false;

function reportId(channel, ts){ return channel+'#'+ts; }
function repTitle(text){
  const first=(text||'').trim().split(/\\n/)[0].trim();
  return first.slice(0,80)||'(제목 없음)';
}
function repSnippet(text){
  const t=(text||'').replace(/\\s+/g,' ').trim();
  return t.slice(0,140);
}
function relTime(ms){
  if(!ms) return '';
  const diff=Date.now()-ms, m=Math.floor(diff/60000);
  if(m<1) return '방금 전';
  if(m<60) return m+'분 전';
  const h=Math.floor(m/60); if(h<24) return h+'시간 전';
  const d=Math.floor(h/24); if(d<7) return d+'일 전';
  return fmtTime(ms);
}

// ── bogoFlow store(영속): { [reportId]: {read,pinned,status,decision,comment} } ──
function flowKey(){ return 'bogoFlow:'+((me&&me.login_id)||'anon'); }
function loadFlow(){ try{ return JSON.parse(localStorage.getItem(flowKey())||'{}')||{}; }catch(e){ return {}; } }
function saveFlow(o){ try{ localStorage.setItem(flowKey(), JSON.stringify(o)); }catch(e){} }
function flowOf(id){
  const f=loadFlow(); const v=f[id]||{};
  return { read:!!v.read, pinned:!!v.pinned,
    status:v.status||(v.read?'in_progress':'new'),
    decision:v.decision||null, comment:v.comment||null };
}
function setFlow(id, patch){
  const f=loadFlow(); f[id]=Object.assign({}, f[id]||{}, patch); saveFlow(f);
}
function markReportRead(id){
  const cur=flowOf(id);
  if(!cur.read){ setFlow(id, {read:true, status:cur.status==='new'?'in_progress':cur.status}); }
}

// staff 권한 경계(C3 근본 차단): staff는 자기 team_label 소속 채널의 report만,
// briefing(CEO 기밀) kind는 무조건 제외. channels[0] 가정 금지.
function staffTeamLabels(){
  // 서버가 staff에게 내려준 channels의 team_label 집합(= 자기 부서)
  const s=new Set();
  channels.forEach(c=>{ if(c.kind!=='briefing' && c.team_label) s.add(c.team_label); });
  return s;
}
function visibleChannels(){
  if(me&&me.role==='staff'){
    const mine=staffTeamLabels();
    return channels.filter(c=>c.kind!=='briefing' && mine.has(c.team_label));
  }
  return channels.slice();
}

// 모든 가시 채널에서 메시지를 가져와 report 단위로 평탄화한다.
async function loadReports(){
  const chs=visibleChannels();
  const out=[];
  await Promise.all(chs.map(async c=>{
    try{
      const d=await api('/api/history?channel='+encodeURIComponent(c.name)+'&n=20');
      (d.items||[]).forEach(m=>{
        if(!m || !m.text) return;
        out.push({ id:reportId(c.name, m.ts), channel:c.name, kind:c.kind,
          team_label:c.team_label||(c.kind==='briefing'?'CEO':'기타'),
          author:m.author||'', text:m.text, ts:m.ts||0,
          title:repTitle(m.text), snippet:repSnippet(m.text) });
      });
    }catch(e){ /* 403/네트워크 등은 조용히 건너뛴다(권한 밖 채널 보호) */ }
  }));
  reports=out;
  return out;
}

// 미처리(new+in_progress) report 수 — 정렬·통계용
function unhandledCount(){
  return reports.filter(r=>{ const s=flowOf(r.id).status; return s==='new'||s==='in_progress'; }).length;
}
function newCount(){ return reports.filter(r=>flowOf(r.id).status==='new').length; }
// 안읽음 report 수 — nav 배지용(M1: report.id 단위, 패널 1개 열람=배지 1감소)
function unreadCount(){ return reports.filter(r=>!flowOf(r.id).read).length; }

// 보고 배지 갱신: 안읽음 report 수(report.id 단위 — 채널 일괄 읽음 금지)
async function refreshReportBadge(){
  if(repLoading) return;
  repLoading=true;
  try{ await loadReports(); }finally{ repLoading=false; }
  setBadge('cntReports', unreadCount(), true);
  if(view==='reports') renderReportsView();
}

// ── 상단 숫자 스트립 ─────────────────────────────────────────────────────────
// 뱃지 헬퍼: 값>0일 때만 .show(+선택적 .alert), 0이면 숨김
function setBadge(id, val, alert){
  const el=document.getElementById(id); if(!el) return;
  if(val>0){ el.textContent=val; el.classList.add('show'); el.classList.toggle('alert', !!alert); }
  else { el.classList.remove('show','alert'); }
}
function refreshStats(){
  const act = roster.filter(r=>r.bot_active).length;
  const inactive = roster.length - act;
  // stat-strip(svAgents/svPending/svReports)은 topbar 제거 커밋(4dbffbf)에서 DOM이 사라졌다.
  // 죽은 참조를 남기지 않는다 — 뱃지(nav 카운트)만 갱신한다.
  // 과거질문 = 대기 세션수(주의), 보고 = 신규 없으면 미표시, 현황 = 비활성 에이전트수(0이면 숨김)
  setBadge('cntHistory', pendingCount(), true);
  // cntReports 는 refreshReportBadge()가 실데이터로 갱신(여기선 건드리지 않음)
  setBadge('cntRoster', inactive, true);
}

// ── 뷰 라우팅(한 번에 하나; 메인 동시 노출 금지) ─────────────────────────────
// topbar(stageTitle) 제거 커밋(4dbffbf)으로 setStageTitle/VIEW_TITLE/ellip 은 모두
// 죽은 코드가 되어 제거했다. 제거된 DOM(stageTitle)을 가리키는 잔존 참조를 남기지 않는다.
function setView(v){
  view=v;
  document.querySelectorAll('[data-view]').forEach(e=>{
    if(e.classList.contains('stat')) return;
    e.classList.toggle('active', e.dataset.view===v);
  });
  // 빈 채팅(메시지 0)일 때만 is-empty: dock은 항상 DOM 유지, 위치만 클래스로
  const stage=document.querySelector('.stage');
  const emptyChat = (v==='chat') && !(activeSession&&activeSession.msgs&&activeSession.msgs.length);
  stage.classList.toggle('is-empty', emptyChat);
  // M3: 보고 뷰는 입력창(.dock) 비노출 — 채팅 입력 dock이 보고 목록을 가리지 않게 한다.
  const dock=document.getElementById('dock'); if(dock) dock.style.display=(v==='chat')?'':'none';
  document.getElementById('stageScroll').scrollTop=0;
  if(v==='chat') renderChat();
  else if(v==='history') renderHistoryView();
  else if(v==='reports') renderReportsView();
  else if(v==='roster') renderRosterView();
}

// ── Lucide 스타일 인라인 아이콘(외부 CDN/패키지 없이 path 직접) ───────────────
const ICN={
  chevronRight:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"></path></svg>',
  fileText:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"></path><path d="M14 2v4a2 2 0 0 0 2 2h4"></path><path d="M10 9H8"></path><path d="M16 13H8"></path><path d="M16 17H8"></path></svg>',
  users:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M22 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg>',
  history:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"></path><path d="M3 3v5h5"></path><path d="M12 7v5l4 2"></path></svg>',
  squarePen:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"></path></svg>',
  check:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5"></path></svg>',
  pin:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 17v5"></path><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"></path></svg>',
  reply:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><polyline points="9 17 4 12 9 7"></polyline><path d="M20 18v-2a4 4 0 0 0-4-4H4"></path></svg>',
  copy:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"></rect><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"></path></svg>',
  inbox:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"></polyline><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"></path></svg>',
  filter:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"></polygon></svg>',
  search:'<svg class="icn" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8"></circle><path d="m21 21-4.3-4.3"></path></svg>',
};
function renderChat(){
  const body=document.getElementById('stageBody');
  const stage=document.querySelector('.stage');
  const msgs=(activeSession&&activeSession.msgs)||[];
  if(!msgs.length){
    // 빈 상태 = 입력창이 메인 중앙. 안내문구·칩 없이 입력창만 노출.
    stage.classList.add('is-empty');
    body.innerHTML='';
    return;
  }
  stage.classList.remove('is-empty');
  body.innerHTML='<div class="col" id="convCol">'+msgs.map(m=>{
    const cls=m.role==='me'?'me':'sys';
    const to=m.to?'→ '+esc(m.to):'';
    const lead=esc(m.who||'')+((m.who&&(to||m.ts))?' · ':'')+(to?to+(m.ts?' · ':''):'');
    const meta=(m.ts||to||m.who)?'<div class="b-meta">'+lead+(m.ts?fmtTime(m.ts):'')+'</div>':'';
    return '<div class="bubble-row '+cls+'"><div class="bubble">'+esc(m.text)+meta+'</div></div>';
  }).join('')+'</div>';
  const sc=document.getElementById('stageScroll'); sc.scrollTop=sc.scrollHeight;
}

// ── 과거 질문 뷰: 한 줄 카드(클릭=대화 복귀) ─────────────────────────────────
function renderHistoryView(){
  const body=document.getElementById('stageBody');
  let h='<div class="col"><div class="view-head"><h2>과거 질문</h2>'
    +'<p>이전 작업을 클릭하면 그 대화로 돌아갑니다.</p></div>';
  if(!sessions.length){ h+='<div class="list-empty">아직 기록이 없습니다.</div></div>'; body.innerHTML=h; return; }
  h+=sessions.map(s=>'<button class="row-card" data-sid="'+esc(s.id)+'">'
    +'<span class="rc-dot'+(s.pending?'':' on')+'"></span>'
    +'<span class="rc-title">'+esc(s.title||'대화')+'</span>'
    +'<span class="rc-sub">'+fmtTime(s.ts)+(s.pending?' · 대기':'')+'</span>'
    +'<span class="rc-chev">'+ICN.chevronRight+'</span></button>').join('')+'</div>';
  body.innerHTML=h;
  body.querySelectorAll('.row-card').forEach(el=>
    el.addEventListener('click',()=>openSession(el.dataset.sid)));
}

// ══ 보고 뷰(단일 화면): 평탄 report 리스트 + 컨트롤바 + 시간 섹션 + 처리 풋바 ══
// staff·ceo 동일 화면(staff는 visibleChannels()가 자기 부서로 한정 — C3 근본 차단).
const STATUS_KO={new:'신규', in_progress:'진행', done:'완료'};
const STATUS_CLASS={new:'st-new', in_progress:'st-prog', done:'st-done'};

// 시간 섹션 그룹(오늘/어제/지난 7일/이전)
function timeSection(ts){
  const now=new Date(); const d=new Date(ts);
  const day=x=>new Date(x.getFullYear(),x.getMonth(),x.getDate()).getTime();
  const diff=Math.round((day(now)-day(d))/86400000);
  if(diff<=0) return {key:'today', label:'오늘'};
  if(diff===1) return {key:'yest', label:'어제'};
  if(diff<=7) return {key:'week', label:'지난 7일'};
  return {key:'old', label:'이전'};
}

// 필터·정렬 적용된 report 목록
function filteredReports(){
  const q=(repFilter.q||'').trim().toLowerCase();
  return reports.filter(r=>{
    const fl=flowOf(r.id);
    if(repFilter.status!=='all' && fl.status!==repFilter.status) return false;
    if(repFilter.teams.size && !repFilter.teams.has(r.team_label)) return false;
    if(q){
      const hay=(r.title+' '+r.text+' '+r.author).toLowerCase();
      if(!hay.includes(q)) return false;
    }
    return true;
  }).sort((a,b)=>{
    if(repFilter.sort==='unhandled'){
      const rank=r=>{ const s=flowOf(r.id).status; return (s==='new'||s==='in_progress')?0:1; };
      const ra=rank(a), rb=rank(b); if(ra!==rb) return ra-rb;
    }
    return b.ts-a.ts;  // ts desc
  });
}

function teamLabelsAll(){
  const s=[]; const seen=new Set();
  reports.forEach(r=>{ if(!seen.has(r.team_label)){ seen.add(r.team_label); s.push(r.team_label); } });
  return s;
}

function repRowHtml(r){
  const fl=flowOf(r.id);
  const unread=!fl.read;
  const dotCls=STATUS_CLASS[fl.status]||'st-new';
  const newBadge=(fl.status==='new')?'<span class="rep-new">NEW</span>':'';
  const pinMark=fl.pinned?'<span class="rep-pinmark" title="고정됨">'+ICN.pin+'</span>':'';
  return '<button class="rep-row'+(unread?' unread':'')+'" data-rid="'+esc(r.id)+'">'
    +'<span class="rep-dot '+dotCls+'" aria-hidden="true"></span>'
    +'<span class="rep-main">'
      +'<span class="rep-title">'+esc(r.title)+'</span>'
      +'<span class="rep-snip">'+esc(r.snippet)+'</span>'
      +'<span class="rep-meta">'
        +'<span class="rep-teambadge">'+esc(r.team_label)+'</span>'
        +'<span class="rep-who">'+esc(r.author||'-')+' · '+esc(relTime(r.ts))+'</span>'
        +newBadge+pinMark
      +'</span>'
    +'</span>'
    +'<span class="rep-actions">'
      // staff는 보고 제출자(read-only): 승인·고정·후속 액션 미노출, 복사만 제공
      +(me&&me.role==='staff'?'':
         '<span class="rep-iconbtn'+(fl.status==='done'?' on':'')+'" role="button" tabindex="0" data-act="approve" title="승인" aria-label="승인">'+ICN.check+'</span>'
        +'<span class="rep-iconbtn'+(fl.pinned?' on':'')+'" role="button" tabindex="0" data-act="pin" title="고정" aria-label="고정">'+ICN.pin+'</span>'
        +'<span class="rep-iconbtn" role="button" tabindex="0" data-act="followup" title="후속 지시" aria-label="후속 지시">'+ICN.reply+'</span>')
      +'<span class="rep-iconbtn" role="button" tabindex="0" data-act="copy" title="복사" aria-label="복사">'+ICN.copy+'</span>'
    +'</span></button>';
}

function emptyStateHtml(kind){
  if(kind==='filter'){
    return '<div class="rep-empty"><div class="re-ico">'+ICN.filter+'</div>'
      +'<div class="re-t">조건에 맞는 보고가 없습니다</div>'
      +'<div class="re-d">검색어·상태·팀 필터를 조정해 보세요.</div>'
      +'<button class="rep-btn rep-clearfilter">필터 초기화</button></div>';
  }
  if(kind==='done'){
    return '<div class="rep-empty"><div class="re-ico">'+ICN.check+'</div>'
      +'<div class="re-t">오늘 보고를 전부 확인했습니다 ✓</div>'
      +'<div class="re-d">미처리 보고가 없습니다. 새 지시를 보내면 여기로 모입니다.</div>'
      +(me&&me.role!=='staff'?'<button class="rep-btn rep-newinstr">새 지시 보내기</button>':'')+'</div>';
  }
  // 0건
  return '<div class="rep-empty"><div class="re-ico">'+ICN.inbox+'</div>'
    +'<div class="re-t">아직 들어온 보고가 없습니다</div>'
    +'<div class="re-d">'+(me&&me.role==='staff'?'담당 채널에 보고가 올라오면 여기로 모입니다.':'팀에 작업을 지시하면 여기로 모입니다.')+'</div>'
    +(me&&me.role!=='staff'?'<button class="rep-btn rep-newinstr">새 지시 보내기</button>':'')+'</div>';
}

function renderReportsView(){
  const body=document.getElementById('stageBody');
  // 미처리/신규/오늘 통계
  const todayN=reports.filter(r=>timeSection(r.ts).key==='today').length;
  let h='<div class="col"><div class="view-head"><h2>보고</h2>'
    +'<div class="rep-stat"><span>신규 <b>'+newCount()+'</b></span>'
    +'<span>진행 <b>'+reports.filter(r=>flowOf(r.id).status==='in_progress').length+'</b></span>'
    +'<span>오늘 <b>'+todayN+'</b>건</span></div></div>';

  // 컨트롤바: 검색 + 상태필터 + 팀칩(다중) + 정렬
  const labels=teamLabelsAll();
  h+='<div class="rep-bar">'
    +'<label class="rep-search"><span class="icn-wrap" aria-hidden="true">'+ICN.search+'</span>'
      +'<input id="repSearch" type="search" placeholder="제목·본문·작성자 검색" value="'+esc(repFilter.q)+'" aria-label="보고 검색"></label>'
    +'<div class="rep-seg" role="group" aria-label="상태 필터">'
      +['all','new','in_progress','done'].map(s=>'<button data-st="'+s+'"'+(repFilter.status===s?' class="on"':'')+'>'
        +(s==='all'?'전체':STATUS_KO[s])+'</button>').join('')
    +'</div>';
  labels.forEach(l=>{ h+='<button class="rep-chip'+(repFilter.teams.has(l)?' on':'')+'" data-team="'+esc(l)+'">'+esc(l)+'</button>'; });
  h+='<select class="rep-sort" id="repSort" aria-label="정렬">'
    +'<option value="unhandled"'+(repFilter.sort==='unhandled'?' selected':'')+'>미처리 우선</option>'
    +'<option value="latest"'+(repFilter.sort==='latest'?' selected':'')+'>최신순</option>'
    +'</select></div>';

  // 본문: 핀 섹션 → 시간 섹션
  const list=filteredReports();
  if(!reports.length){
    h+=emptyStateHtml('empty')+'</div>'; body.innerHTML=h; bindReportsView(); return;
  }
  if(!list.length){
    // 필터 결과 0. 미처리(new+in_progress)가 0건이면 '완료' 격려, 아니면 '필터0'.
    // (전체/신규/진행 필터에서 처리할 게 없으면 완료 화면, 완료 필터/검색·팀칩 0매칭은 필터0.)
    const handledAll=unhandledCount()===0 && repFilter.q==='' && !repFilter.teams.size
      && (repFilter.status==='all'||repFilter.status==='new'||repFilter.status==='in_progress');
    h+=emptyStateHtml(handledAll?'done':'filter')+'</div>'; body.innerHTML=h; bindReportsView(); return;
  }
  const pinned=list.filter(r=>flowOf(r.id).pinned);
  const rest=list.filter(r=>!flowOf(r.id).pinned);
  if(pinned.length){
    h+='<div class="rep-sec">📌 고정</div>'+pinned.map(repRowHtml).join('');
  }
  // 시간 섹션(rest) — list가 이미 정렬됨, 섹션 헤더만 삽입
  let curSec=null;
  rest.forEach(r=>{
    const sec=timeSection(r.ts);
    if(sec.key!==curSec){ h+='<div class="rep-sec">'+esc(sec.label)+'</div>'; curSec=sec.key; }
    h+=repRowHtml(r);
  });
  h+='</div>';
  body.innerHTML=h;
  bindReportsView();
}

function bindReportsView(){
  const body=document.getElementById('stageBody');
  // 컨트롤바
  const se=document.getElementById('repSearch');
  if(se) se.addEventListener('input',e=>{ repFilter.q=e.target.value;
    const at=document.activeElement; renderReportsView();
    const ns=document.getElementById('repSearch'); if(ns){ ns.focus(); ns.setSelectionRange(ns.value.length,ns.value.length); } });
  body.querySelectorAll('.rep-seg button').forEach(b=>b.addEventListener('click',()=>{ repFilter.status=b.dataset.st; renderReportsView(); }));
  body.querySelectorAll('.rep-chip').forEach(b=>b.addEventListener('click',()=>{
    const l=b.dataset.team; if(repFilter.teams.has(l)) repFilter.teams.delete(l); else repFilter.teams.add(l); renderReportsView(); }));
  const so=document.getElementById('repSort'); if(so) so.addEventListener('change',e=>{ repFilter.sort=e.target.value; renderReportsView(); });
  const cf=body.querySelector('.rep-clearfilter'); if(cf) cf.addEventListener('click',()=>{ repFilter={q:'',status:'all',teams:new Set(),sort:repFilter.sort}; renderReportsView(); });
  const ni=body.querySelector('.rep-newinstr'); if(ni) ni.addEventListener('click',()=>{ startNewChat(); });
  // 행: 클릭=패널 + read, 액션바
  body.querySelectorAll('.rep-row').forEach(el=>{
    const rid=el.dataset.rid;
    el.addEventListener('click',e=>{
      const act=e.target.closest('[data-act]');
      if(act){ e.stopPropagation(); handleRepAction(act.dataset.act, rid); return; }
      openReportPanel(rid);
    });
    el.querySelectorAll('[data-act]').forEach(a=>a.addEventListener('keydown',e=>{
      if(e.key==='Enter'||e.key===' '){ e.preventDefault(); e.stopPropagation(); handleRepAction(a.dataset.act, rid); } }));
  });
}

function repById(id){ return reports.find(r=>r.id===id)||null; }

// 행 호버 액션바 핸들러
function handleRepAction(act, rid){
  const r=repById(rid); if(!r) return;
  // staff는 read-only: 복사 외 상태변경 액션 일체 차단(표면 미노출과 별개의 근본 가드)
  if(me&&me.role==='staff'&&act!=='copy') return;
  if(act==='approve'){ approveReport(rid); }
  else if(act==='pin'){ togglePin(rid); }
  else if(act==='followup'){ followUp(rid); }
  else if(act==='copy'){
    navigator.clipboard.writeText(r.text).then(()=>toast('보고 본문을 복사했습니다')).catch(()=>toast('복사 실패')); }
}

// ── 워크플로 핸들러 ──────────────────────────────────────────────────────────
// staff(보고 제출자)는 모든 보고 상태변경 권한이 없다. 각 mutate 핸들러 진입부에서
// 근본 차단해 표면 숨김이 우회되거나 향후 서버 영속으로 승격돼도 권한이 새지 않게 한다.
function isReportMutator(){ return !(me&&me.role==='staff'); }
function approveReport(rid){
  if(!isReportMutator()) return;
  setFlow(rid, {decision:'approved', status:'done', read:true});
  toast('승인했습니다 ✓'); syncAfterFlow(rid);
}
function completeReport(rid){
  if(!isReportMutator()) return;
  setFlow(rid, {status:'done', read:true}); toast('완료 처리했습니다'); syncAfterFlow(rid);
}
function togglePin(rid){
  if(!isReportMutator()) return;
  const cur=flowOf(rid); setFlow(rid, {pinned:!cur.pinned});
  toast(!cur.pinned?'상단에 고정했습니다':'고정을 해제했습니다'); syncAfterFlow(rid);
}
function addComment(rid, text){
  if(!isReportMutator()) return;
  if(!text||!text.trim()) return;
  setFlow(rid, {comment:{text:text.trim(), ts:Date.now()}, read:true}); syncAfterFlow(rid);
}
function rejectReport(rid, reason){
  if(!isReportMutator()) return;
  setFlow(rid, {decision:'rejected', status:'done', read:true, comment:{text:'[반려] '+reason, ts:Date.now()}});
  toast('반려 처리했습니다'); syncAfterFlow(rid);
}
function followUp(rid){
  if(!isReportMutator()) return;
  const r=repById(rid); if(!r) return;
  // 후속 지시: 해당 팀 채널 프리필 + 인용 → 채팅 composer로 이동(/api/post 재사용)
  setFlow(rid, {status:'in_progress', read:true});
  startNewChat();
  if(activeSession) activeSession.channel=r.channel;
  const ta=document.getElementById('msg');
  if(ta){
    const quote=r.text.split(/\\n/).slice(0,3).map(l=>'> '+l).join('\\n');
    ta.value='['+r.team_label+' 보고 후속 지시]\\n'+quote+'\\n\\n';
    autoGrow(ta); refreshSendState(); ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }
  toast(r.channel+' 후속 지시를 작성하세요');
  syncAfterFlow(rid);
}
// flow 변경 후 화면·배지 낙관적 동기화(재fetch 없이)
function syncAfterFlow(rid){
  setBadge('cntReports', unreadCount(), true);
  if(view==='reports') renderReportsView();
  if(panelOpenRid===rid) renderReportPanelBody(rid);
}

// ── 에이전트 현황 뷰: 한 줄 카드 → 클릭 시 패널 상세 ─────────────────────────
function renderRosterView(){
  if(me.role==='staff'){ setView('chat'); return; }
  const body=document.getElementById('stageBody');
  body.innerHTML='<div class="col"><div class="view-head"><h2>에이전트 현황</h2>'
    +'<p>불러오는 중…</p></div></div>';
  loadRoster().then(()=>{
    let h='<div class="col"><div class="view-head"><h2>에이전트 현황</h2>'
      +'<p>에이전트를 선택하면 상세 정보를 봅니다.</p></div>';
    h+=roster.map((r,i)=>'<button class="row-card" data-ag="'+i+'">'
      +'<span class="rc-dot'+(r.bot_active?' on':'')+'"></span>'
      +'<span class="rc-title">'+esc(r.name)+'</span>'
      +'<span class="rc-sub">'+esc(r.role)+' · '+(r.bot_active?'활성':'비활성')+'</span>'
      +'<span class="rc-chev">'+ICN.chevronRight+'</span></button>').join('')+'</div>';
    body.innerHTML=h;
    body.querySelectorAll('.row-card').forEach(el=>
      el.addEventListener('click',()=>openAgentPanel(roster[+el.dataset.ag])));
  }).catch(e=>{
    body.innerHTML='<div class="col"><div class="list-empty">로드 실패: '+esc(e.message)+'</div></div>';
  });
}
async function loadRoster(){
  if(me.role==='staff') return;
  try{ const d=await api('/api/roles'); roster=d.roles||[]; refreshStats(); }catch(e){ throw e; }
}

// ── 우측 슬라이드 패널 ───────────────────────────────────────────────────────
function openPanel(title, bodyHtml){
  if(panelTimer){ clearInterval(panelTimer); panelTimer=null; }
  document.getElementById('panelTitle').textContent=title;
  document.getElementById('panelBody').innerHTML=bodyHtml;
  // 비보고 패널은 풋바 숨김(보고 패널이 별도로 표시)
  const foot=document.getElementById('panelFoot'); if(foot){ foot.style.display='none'; foot.innerHTML=''; }
  document.getElementById('panel').classList.add('show');
  document.getElementById('panel').setAttribute('aria-hidden','false');
  document.getElementById('panelScrim').classList.add('show');
}
function closePanel(){
  if(panelTimer){ clearInterval(panelTimer); panelTimer=null; }
  panelOpenRid=null;
  document.getElementById('panel').classList.remove('show');
  document.getElementById('panel').setAttribute('aria-hidden','true');
  document.getElementById('panelScrim').classList.remove('show');
  const foot=document.getElementById('panelFoot'); if(foot){ foot.style.display='none'; foot.innerHTML=''; }
}
let panelOpenRid=null;
// 단일 보고 문서형 패널(680px) + 처리 풋바(승인/반려/코멘트/후속/완료)
function openReportPanel(rid){
  const r=repById(rid); if(!r) return;
  panelOpenRid=rid;
  markReportRead(rid);                 // 자동 read + 그 행 NEW만 제거
  openPanel(r.team_label+' 보고', '');
  renderReportPanelBody(rid);
  // 목록·배지 즉시 동기화(읽음 1건 = 배지 1감소)
  setBadge('cntReports', unreadCount(), true);
  if(view==='reports') renderReportsView();
}
function renderReportPanelBody(rid){
  const r=repById(rid); if(!r) return;
  const fl=flowOf(rid);
  const stCls=STATUS_CLASS[fl.status]||'st-new';
  let html='<div class="rep-doc">'
    +'<div class="rep-statusline">'
      +'<span class="rep-statuspill '+stCls+'">'+(STATUS_KO[fl.status]||'신규')+'</span>'
      +(fl.decision==='approved'?'<span class="rep-statuspill st-done">승인됨 ✓</span>':'')
      +(fl.decision==='rejected'?'<span class="rep-statuspill" style="color:#b3261e">반려됨</span>':'')
      +(fl.pinned?'<span class="rep-statuspill st-new">📌 고정</span>':'')
    +'</div>'
    +'<div class="rep-docmeta">'
      +'<span class="pm-tag">'+esc(r.channel)+' · '+kindLabel(r.kind)+'</span>'
      +'<span class="pm-tag">'+esc(r.author||'-')+'</span>'
      +'<span class="pm-tag">'+esc(fmtTime(r.ts))+'</span>'
    +'</div>'
    +'<div class="body">'+esc(r.text)+'</div>';
  // CEO 코멘트(우측 버블)
  if(fl.comment){
    html+='<div class="rep-cmt"><div class="bubble">'+esc(fl.comment.text)
      +'<div class="b-meta">CEO · '+esc(fmtTime(fl.comment.ts))+'</div></div></div>';
  }
  html+='</div>';
  document.getElementById('panelBody').innerHTML=html;
  // 처리 풋바(staff는 읽기 전용 — 승인/반려/후속 비노출)
  const foot=document.getElementById('panelFoot');
  if(me&&me.role==='staff'){ foot.style.display='none'; foot.innerHTML=''; return; }
  foot.style.display='';
  foot.innerHTML='<button class="rep-btn approve" data-pact="approve">승인</button>'
    +'<button class="rep-btn reject" data-pact="reject">반려</button>'
    +'<button class="rep-btn" data-pact="comment">코멘트</button>'
    +'<button class="rep-btn" data-pact="followup">후속 지시</button>'
    +'<button class="rep-btn" data-pact="done">완료 처리</button>';
  foot.querySelectorAll('[data-pact]').forEach(b=>b.addEventListener('click',()=>{
    const a=b.dataset.pact;
    if(a==='approve') approveReport(rid);
    else if(a==='reject') openRejectModal(rid);
    else if(a==='comment') openCommentPrompt(rid);
    else if(a==='followup'){ closePanel(); followUp(rid); }
    else if(a==='done') completeReport(rid);
  }));
}
// 코멘트: 로컬 메모(패널에 CEO 우측 버블로 표시)
function openCommentPrompt(rid){
  const txt=prompt('CEO 코멘트(이 보고에 대한 메모):');
  if(txt!==null && txt.trim()){ addComment(rid, txt); }
}
// 반려 사유 모달(빈 사유 차단)
function openRejectModal(rid){
  const back=document.getElementById('repModalBack');
  document.getElementById('repRejectReason').value='';
  document.getElementById('repRejectErr').textContent='';
  back.classList.add('show');
  const ta=document.getElementById('repRejectReason'); ta.focus();
  const confirm=document.getElementById('repRejectConfirm');
  const cancel=document.getElementById('repRejectCancel');
  const close=()=>{ back.classList.remove('show'); confirm.onclick=null; cancel.onclick=null; };
  confirm.onclick=()=>{
    const reason=ta.value.trim();
    if(!reason){ document.getElementById('repRejectErr').textContent='반려 사유를 입력하세요.'; return; }
    rejectReport(rid, reason); close();
  };
  cancel.onclick=close;
}
function openAgentPanel(r){
  openPanel(r.name,
    '<div class="panel-meta"><span class="pm-tag">'+esc(r.role)+'</span>'
    +'<span class="pm-tag">'+(r.bot_active?'활성':'비활성/미확인')+'</span></div>'
    +'<div class="panel-section-t">정보</div>'
    +'<div class="panel-kv"><b>계정</b> @'+esc(r.username)+'<br>'
    +'<b>주담당</b> '+esc(r.primary||'-')+'<br>'
    +'<b>채널</b> '+esc((r.channels||[]).join(', ')||'-')+'</div>'
    +'<div class="note">에이전트 정의 변경은 각 에이전트의 <b>역할별 학습방</b>에서 '
    +'자연어 지시 → diff 미리보기 → <b>적용</b> (ceo_admin_runtime 파이프라인). '
    +'그 방에서는 그 방 담당 에이전트만 수정됩니다(방 격리).</div>');
}

// ── 새 작업 / 세션 열기 ──────────────────────────────────────────────────────
function startNewChat(){ newSession(); setView('chat'); document.getElementById('msg').focus(); }
function openSession(id){
  const s=sessions.find(x=>x.id===id); if(!s) return;
  activeSession=s; setView('chat');
}


// ── 전송: 통합 입력창 → 기본 대상 채널로 /api/post ───────────────────────────
async function doSend(){
  const ta=document.getElementById('msg'); const txt=ta.value.trim(); if(!txt) return;
  const btn=document.getElementById('send');
  if(!activeSession) newSession();
  const ch=activeSession.channel||defaultPost;
  if(!ch){ toast('게시 가능한 대상 채널이 없습니다'); return; }
  const wasEmpty = activeSession.msgs.length===0;
  const dock=document.getElementById('dock');
  const stage=document.querySelector('.stage');
  // 첫 전송: FLIP 전환을 위해 이동 전 dock 위치 측정
  const first = wasEmpty ? dock.getBoundingClientRect() : null;
  activeSession.msgs.push({role:'me',text:txt,ts:Date.now(),to:ch});
  if(activeSession.title==='새 작업') activeSession.title=txt.slice(0,40);
  activeSession.pending=true; activeSession.ts=Date.now();
  sessions=sessions.filter(s=>s.id!==activeSession.id); sessions.unshift(activeSession);
  saveSessions();
  ta.value=''; autoGrow(ta); btn.disabled=true; refreshStats();
  if(view==='chat'){
    if(wasEmpty){ flipFirstSend(first, dock, stage); }
    else { renderChat(); markFreshBubble(); }
  }
  try{
    await api('/api/post',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({channel:ch,text:txt})});
    activeSession.msgs.push({role:'sys',
      text:'전송 완료 → '+ch+'\\n담당 에이전트가 처리 후 해당 채널에 응답합니다. '
        +'진행 상황은 좌측 ‘보고’에서 확인하세요.', who:'시스템', ts:Date.now()});
    activeSession.pending=false;
  }catch(e){
    activeSession.msgs.push({role:'sys',text:'전송 실패: '+e.message,who:'시스템',ts:Date.now()});
    activeSession.pending=false;
  }finally{
    saveSessions(); refreshStats();
    if(view==='chat'){ renderChat(); markFreshBubble(); }
  }
}
// 마지막 버블에 fade-in 클래스 부여(2번째 메시지부터·서버응답)
function markFreshBubble(){
  const rows=document.querySelectorAll('#convCol .bubble-row');
  if(rows.length){ rows[rows.length-1].classList.add('fresh'); }
}
// 첫 전송 FLIP: dock을 빈상태→하단으로 부드럽게 이동, 첫 버블 페이드인
function flipFirstSend(first, dock, stage){
  const reduce = window.matchMedia('(prefers-reduced-motion:reduce)').matches;
  stage.classList.remove('is-empty');
  renderChat();           // 대화 레이아웃으로 전환(dock 하단 고정)
  markFreshBubble();      // 첫 버블 페이드인
  if(reduce) return;
  const last=dock.getBoundingClientRect();
  const dy=first.top-last.top;
  if(!dy){ return; }
  dock.style.transform='translateY('+dy+'px)';
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
    dock.style.transition='transform 260ms cubic-bezier(.4,0,.2,1)';
    dock.style.transform='';
  }));
  const cleanup=()=>{ dock.style.transition=''; dock.style.transform='';
    dock.removeEventListener('transitionend',cleanup); };
  dock.addEventListener('transitionend',cleanup);
}

// ── 입력창 동작 ──────────────────────────────────────────────────────────────
function autoGrow(ta){ ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,200)+'px'; }
function refreshSendState(){
  const ta=document.getElementById('msg'), btn=document.getElementById('send');
  btn.disabled=!ta.value.trim();
}

// ── 사이드바 토글 ────────────────────────────────────────────────────────────
function toggleNav(){
  const n=document.getElementById('nav'), scrim=document.getElementById('scrim');
  const stage=document.querySelector('.stage');
  const collapsed=n.classList.toggle('collapsed');
  stage.classList.toggle('nav-collapsed', collapsed);
  if(window.matchMedia('(max-width:768px)').matches)
    scrim.classList.toggle('show', !collapsed);
}

// ── ⌘K 커맨드 팔레트(과거질문 검색 · 에이전트 이동 · 보고 열기) ──────────────
function cmdkSources(){
  const items=[];
  items.push({cat:'이동',ico:ICN.squarePen,txt:'새 작업',kind:'',act:startNewChat});
  items.push({cat:'이동',ico:ICN.fileText,txt:'보고 열기',kind:'',act:()=>setView('reports')});
  // 미처리만 보기(신규 필터 단축)
  items.push({cat:'이동',ico:ICN.filter,txt:'미처리 보고만 보기',kind:'신규',
    act:()=>{ repFilter.status='new'; setView('reports'); }});
  if(me.role!=='staff') items.push({cat:'이동',ico:ICN.users,txt:'에이전트 현황',kind:'',act:()=>setView('roster')});
  // 개별 보고 검색(제목/작성자/팀)
  reports.slice().sort((a,b)=>b.ts-a.ts).slice(0,60).forEach(r=>items.push({
    cat:'보고',ico:ICN.fileText,txt:r.title,kind:r.team_label+' · '+(r.author||''),
    act:()=>{ setView('reports'); openReportPanel(r.id); }}));
  roster.forEach(r=>items.push({cat:'에이전트',ico:ICN.users,txt:r.name,kind:r.role,
    act:()=>{ if(me.role!=='staff'){ setView('roster'); openAgentPanel(r); } }}));
  sessions.forEach(s=>items.push({cat:'과거 질문',ico:ICN.history,txt:s.title||'대화',kind:fmtTime(s.ts),
    act:()=>openSession(s.id)}));
  return items;
}
function openCmdk(){
  document.getElementById('cmdkBack').classList.add('show');
  const inp=document.getElementById('cmdkInput'); inp.value=''; inp.focus();
  renderCmdk('');
}
function closeCmdk(){ document.getElementById('cmdkBack').classList.remove('show'); }
function renderCmdk(q){
  q=(q||'').trim().toLowerCase();
  let items=cmdkSources();
  if(q) items=items.filter(it=>(it.txt+' '+it.cat+' '+it.kind).toLowerCase().includes(q));
  items=items.slice(0,40); cmdkRows=items; cmdkIdx=0;
  const list=document.getElementById('cmdkList');
  if(!items.length){ list.innerHTML='<div class="cmdk-empty">결과 없음</div>'; return; }
  let html='', lastCat=null;
  items.forEach((it,i)=>{
    if(it.cat!==lastCat){ html+='<div class="cmdk-cat">'+esc(it.cat)+'</div>'; lastCat=it.cat; }
    html+='<button class="cmdk-item'+(i===0?' cur':'')+'" data-i="'+i+'">'
      +'<span class="ci-ico">'+it.ico+'</span><span class="ci-txt">'+esc(it.txt)+'</span>'
      +(it.kind?'<span class="ci-kind">'+esc(it.kind)+'</span>':'')+'</button>';
  });
  list.innerHTML=html;
  list.querySelectorAll('.cmdk-item').forEach(el=>
    el.addEventListener('click',()=>runCmdk(+el.dataset.i)));
}
function moveCmdk(d){
  if(!cmdkRows.length) return;
  cmdkIdx=(cmdkIdx+d+cmdkRows.length)%cmdkRows.length;
  const els=[...document.querySelectorAll('.cmdk-item')];
  els.forEach((e,i)=>e.classList.toggle('cur', +e.dataset.i===cmdkIdx));
  const cur=els.find(e=>+e.dataset.i===cmdkIdx); if(cur) cur.scrollIntoView({block:'nearest'});
}
function runCmdk(i){
  const it=cmdkRows[i]; if(!it) return;
  closeCmdk(); it.act();
}

const ROLE_KO={ceo:'CEO',staff:'직원',admin:'관리자'};

(async function init(){
  try{ me=await api('/api/me'); }catch(e){ location.href='/login'; return; }
  const label=me.label||me.login_id||'';
  document.getElementById('pfName').textContent=label||'사용자';
  document.getElementById('pfRole').textContent=(ROLE_KO[me.role]||me.role)
    +(me.login_id&&me.login_id!==label?' · '+me.login_id:'');
  document.getElementById('pfAvatar').textContent=(label||'H').trim().charAt(0).toUpperCase();
  document.title='에이전트 BOGO '+(ROLE_KO[me.role]||'')+' 워크스페이스';
  if(!/Mac|iPhone|iPad/.test(navigator.platform||'')){ const kb=document.getElementById('cmdkKbd'); if(kb) kb.textContent='Ctrl K'; }

  // role 분기: ceo/admin 만 에이전트현황·기억보관소
  // (statAgents 는 topbar 제거 커밋(4dbffbf)에서 사라졌으므로 참조하지 않는다)
  if(me.role==='ceo'||me.role==='admin'){
    document.getElementById('navRoster').style.display='';
    document.getElementById('navSettings').style.display='';
    const vn=document.getElementById('navVault'); if(vn) vn.style.display='';
  }

  // staff IA 축소: '새 작업'→'보고 작성' 라벨 치환, '보고'를 기본 진입 강조, '과거 질문'은 하위로 이동
  if(me.role==='staff'){
    const nc=document.querySelector('#navChat .ni-txt'); if(nc) nc.textContent='보고 작성';
    // '보고 작성'(제출)과 구분: 자기 팀에 올라온 보고 열람 뷰는 '받은 보고'로 차별화
    const nrt=document.querySelector('#navReports .ni-txt'); if(nrt) nrt.textContent='받은 보고';
    const grp=document.querySelector('#navHistory')&&document.querySelector('#navHistory').parentNode;
    const nh=document.getElementById('navHistory'), nr=document.getElementById('navReports');
    // 보고를 과거 질문보다 위로(부차화)
    if(grp&&nh&&nr&&nr.nextSibling!==nh){ grp.insertBefore(nr, nh); }
  }

  // 데이터 로드
  try{
    const d=await api('/api/channels');
    channels=d.channels; defaultPost=d.default_post_channel;
    buildTeamGroups();
    // targetName(dock-hint)은 제거 커밋(4dbffbf)에서 사라졌으므로 참조하지 않는다.
  }catch(e){ toast('초기화 실패: '+e.message); }

  loadSessions();
  if(sessions.length) activeSession=sessions[0]; else newSession();
  // staff 권한기반 기본 전송채널: CEO브리핑이면 403이므로 자기 부서(게시 가능 첫) 채널로 보정
  if(me.role==='staff'){
    const fp=firstPostChannel();
    if(fp){ defaultPost=fp; if(activeSession&&(!activeSession.channel||/브리핑/.test(activeSession.channel))) activeSession.channel=fp; }
  }
  refreshStats();
  await refreshReportBadge().catch(()=>{});  // 진입 전 report 평탄화 1회(빈 깜빡임 방지)
  setInterval(()=>{ refreshReportBadge().catch(()=>{}); }, POLL_MS);
  if(me.role!=='staff'){ loadRoster().catch(()=>{}); }

  // 이벤트 바인딩
  document.querySelectorAll('.nav-item[data-view], .nav-primary[data-view]').forEach(el=>
    el.addEventListener('click',()=>{ const v=el.dataset.view; if(v==='chat') startNewChat(); else setView(v); }));
  document.querySelectorAll('.stat[data-view]').forEach(el=>
    el.addEventListener('click',()=>{ const v=el.dataset.view; if(v==='roster'&&me.role==='staff') return; setView(v); }));
  document.getElementById('navCollapse').addEventListener('click',toggleNav);
  document.getElementById('navOpen').addEventListener('click',toggleNav);
  document.getElementById('scrim').addEventListener('click',toggleNav);
  document.getElementById('send').addEventListener('click',doSend);
  document.getElementById('panelClose').addEventListener('click',closePanel);
  document.getElementById('panelScrim').addEventListener('click',closePanel);
  document.getElementById('cmdkTrigger').addEventListener('click',openCmdk);
  document.getElementById('cmdkBack').addEventListener('click',e=>{ if(e.target.id==='cmdkBack') closeCmdk(); });
  document.getElementById('cmdkInput').addEventListener('input',e=>renderCmdk(e.target.value));
  document.getElementById('cmdkInput').addEventListener('keydown',e=>{
    if(e.key==='ArrowDown'){ e.preventDefault(); moveCmdk(1); }
    else if(e.key==='ArrowUp'){ e.preventDefault(); moveCmdk(-1); }
    else if(e.key==='Enter'){ e.preventDefault(); runCmdk(cmdkIdx); }
    else if(e.key==='Escape'){ closeCmdk(); }
  });
  document.getElementById('logout').addEventListener('click', async ()=>{
    try{ await fetch('/api/logout',{method:'POST'}); }catch(e){}
    location.href='/login';
  });
  const ta=document.getElementById('msg');
  ta.addEventListener('input',()=>{ autoGrow(ta); refreshSendState(); });
  ta.addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); doSend(); } });
  document.addEventListener('keydown',e=>{
    if((e.metaKey||e.ctrlKey)&&(e.key==='k'||e.key==='K')){ e.preventDefault(); openCmdk(); }
    else if((e.metaKey||e.ctrlKey)&&e.key==='\\\\'){ e.preventDefault(); toggleNav(); }
    else if(e.key==='Escape'){ closePanel(); }
  });

  // 역할별 진입화면: staff는 보고, ceo/admin은 채팅
  setView(me.role==='staff' ? 'reports' : 'chat');
})();
</script>
</body>
</html>"""


def build_vault_html():
    """Vault(누적 기억) 브라우징·검색·노트 열람 페이지(ceo/admin 전용).

    한 화면에서:
      (a) RAG 검색박스 → top-k 결과(노트 링크 + 스니펫 + score)
      (b) role/team/type 메타필터 + 최신순 노트 목록(파셋은 서버가 동적 수집)
      (c) 노트 클릭 → 모달로 frontmatter + 본문 열람(경로는 서버가 traversal 차단)
    Obsidian 파일 경로/URI 도 함께 표기해 CEO 가 옵시디언으로 직접 열 수 있게 한다.
    공통 _CSS(Apple 디자인 시스템) 계승. 모든 출력은 esc()로 XSS 이스케이프.
    대시보드(INDEX_HTML)와 동일한 SaaS 사이드바 셸(body.app-shell·.ws·.nav)을 사용해
    디자인 일관성을 유지하며, 사이드바 nav 로 양방향 이동(대시보드↔보관소)이 가능하다.
    """
    return """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>에이전트 BOGO 기억 보관소</title>""" + _FAVICON_LINKS + """
<style>""" + _CSS + """
/* vault 페이지 전용: stage-scroll 이 full-height 스크롤 영역이 됨 */
.vault-stage { flex:1 1 auto; overflow-y:auto; }
.vault-content { max-width:1080px; margin:0 auto; padding:var(--gap-6) var(--gap-6) var(--gap-8); }
</style>
</head>
<body class="app-shell">
<div class="ws">
  <!-- 사이드바: 대시보드와 동일한 구조, 기억 보관소 항목 active 표시 -->
  <nav class="nav" id="nav">
    <div class="nav-head">
      <img class="logo-mark" src="/static/logo.svg" alt="에이전트 BOGO" width="26" height="26">
      <span class="brandname">에이전트 BOGO</span>
    </div>
    <div class="nav-scroll">
      <div class="nav-group">
        <div class="nav-group-label">워크스페이스</div>
        <a class="nav-item" href="/">
          <span class="ni-ico"><svg class="icn icn-18" viewBox="0 0 24 24" aria-hidden="true"><rect width="18" height="18" x="3" y="3" rx="2"></rect><path d="M9 3v18"></path><path d="M3 9h18"></path></svg></span>
          <span class="ni-txt">대시보드</span>
        </a>
        <a class="nav-item active" href="/vault" aria-current="page">
          <span class="ni-ico"><svg class="icn icn-18" viewBox="0 0 24 24" aria-hidden="true"><rect width="20" height="5" x="2" y="3" rx="1"></rect><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"></path><path d="M10 12h4"></path></svg></span>
          <span class="ni-txt">기억 보관소</span>
        </a>
      </div>
    </div>
    <div class="nav-foot">
      <div class="profile">
        <span class="avatar" id="pfAvatar">H</span>
        <div class="pf-meta">
          <div class="pf-name" id="pfName">…</div>
          <div class="pf-role" id="pfRole"></div>
        </div>
        <button class="pf-logout" id="logout">로그아웃</button>
      </div>
    </div>
  </nav>

  <!-- 메인 영역 -->
  <main class="stage">
    <div class="vault-stage">
      <div class="vault-content">
        <!-- 페이지 헤더 -->
        <div class="view-head" style="display:flex;align-items:center;gap:var(--gap-4);margin-bottom:var(--gap-6);">
          <div style="flex:1;">
            <h2 style="font-size:var(--fz-24);font-weight:700;letter-spacing:-0.4px;color:var(--ink);margin:0 0 var(--gap-1);line-height:1.2;">기억 보관소</h2>
            <p style="font-size:var(--fz-14);color:var(--ink-muted);margin:0;letter-spacing:-0.2px;">누적 보고·피드백·결정을 검색하고 열람합니다.</p>
          </div>
          <span class="rag-badge" id="ragBadge">RAG 상태…</span>
        </div>

        <!-- 검색 영역 -->
        <div style="background:var(--parchment);border:1px solid var(--hairline);border-radius:var(--r-lg);padding:var(--gap-5) var(--gap-6);margin-bottom:var(--gap-6);">
          <div class="vault-toolbar">
            <div class="vault-search">
              <input id="q" type="text" placeholder="누적된 보고·피드백·결정에서 검색 (예: LLM 비용)">
              <button id="searchBtn">검색</button>
            </div>
          </div>
          <div class="vault-filters">
            <select id="fRole"><option value="">역할 전체</option></select>
            <select id="fTeam"><option value="">팀 전체</option></select>
            <select id="fType"><option value="">유형 전체</option></select>
            <span class="pill" id="resultMeta"></span>
          </div>
        </div>

        <!-- 노트 목록 -->
        <div>
          <h3 id="listTitle" style="font-size:var(--fz-16);font-weight:600;letter-spacing:-0.3px;color:var(--ink);margin:0 0 var(--gap-4);">최신 노트</h3>
          <div class="note-list" id="noteList"><div class="empty">불러오는 중…</div></div>
        </div>
      </div>
    </div>
  </main>
</div>

<!-- 노트 상세 모달 -->
<div class="modal-back" id="modalBack">
  <div class="modal" id="modal">
    <button class="mclose" id="mclose">&times;</button>
    <h2 id="mTitle"></h2>
    <div class="fm" id="mFm"></div>
    <div class="links" id="mLinks"></div>
    <div class="nbody" id="mBody"></div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
function esc(s){ return (s||"").replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function toast(t){ const el=document.getElementById('toast'); el.textContent=t;
  el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2600); }
async function api(path,opts){
  const r=await fetch(path,opts);
  if(r.status===401){ location.href='/login'; throw new Error('세션 만료'); }
  const d=await r.json().catch(()=>({error:'응답 파싱 실패'}));
  if(!r.ok) throw new Error(d.error||('HTTP '+r.status)); return d;
}
let searchMode=false;  // true=검색 결과 표시 중, false=브라우징 목록

function fillFacet(sel, values, label){
  sel.innerHTML='<option value="">'+label+'</option>';
  values.forEach(v=>{ const o=document.createElement('option');
    o.value=v; o.textContent=v; sel.appendChild(o); });
}
function renderRag(rag){
  const b=document.getElementById('ragBadge');
  if(rag && rag.ok){ b.textContent='RAG: '+rag.mode; b.classList.remove('off'); }
  else { b.textContent='RAG 인덱스 없음'; b.classList.add('off'); }
}
function noteCard(n, withScore){
  const score = (withScore && n.score!=null)
    ? '<span class="nt-score">score '+esc(String(n.score))+'</span> · ' : '';
  return '<div class="note-item" data-path="'+esc(n.path)+'">'
    +'<div class="nt-title">'+esc(n.title||n.path)+'</div>'
    +'<div class="nt-meta">'
    +(n.type?'<span class="nt-tag">'+esc(n.type)+'</span>':'')
    +(n.role?'<span class="nt-tag">역할 '+esc(n.role)+'</span>':'')
    +(n.team?'<span class="nt-tag">팀 '+esc(n.team)+'</span>':'')
    +(n.date?'<span class="nt-tag">'+esc(n.date)+'</span>':'')
    +'</div><div class="nt-snip">'+score+esc(n.snippet||'')+'</div></div>';
}
function bindCards(){
  document.querySelectorAll('.note-item').forEach(el=>{
    el.addEventListener('click', ()=>openNote(el.getAttribute('data-path')));
  });
}
function filters(){
  return {
    role:document.getElementById('fRole').value,
    team:document.getElementById('fTeam').value,
    type:document.getElementById('fType').value,
  };
}
async function loadList(){
  searchMode=false;
  document.getElementById('listTitle').textContent='최신 노트';
  const f=filters();
  const qs=new URLSearchParams();
  if(f.role) qs.set('role',f.role); if(f.team) qs.set('team',f.team);
  if(f.type) qs.set('type',f.type);
  const box=document.getElementById('noteList'); box.innerHTML='<div class="empty">불러오는 중…</div>';
  try{
    const d=await api('/api/vault/list?'+qs.toString());
    renderRag(d.rag);
    if(d.facets){
      const fr=document.getElementById('fRole'), ft=document.getElementById('fTeam'),
            fy=document.getElementById('fType');
      const rv=fr.value, tv=ft.value, yv=fy.value;
      fillFacet(fr,d.facets.roles,'역할 전체'); fr.value=rv;
      fillFacet(ft,d.facets.teams,'팀 전체'); ft.value=tv;
      fillFacet(fy,d.facets.types,'유형 전체'); fy.value=yv;
    }
    document.getElementById('resultMeta').textContent='노트 '+d.notes.length+'개';
    if(!d.notes.length){ box.innerHTML='<div class="empty">조건에 맞는 노트가 없습니다.</div>'; return; }
    box.innerHTML=d.notes.map(n=>noteCard(n,false)).join('');
    bindCards();
  }catch(e){ box.innerHTML='<div class="empty">로드 실패: '+esc(e.message)+'</div>'; }
}
async function doSearch(){
  const query=document.getElementById('q').value.trim();
  if(!query){ loadList(); return; }
  searchMode=true;
  document.getElementById('listTitle').textContent='검색 결과: '+query;
  const f=filters();
  const qs=new URLSearchParams({q:query});
  if(f.role) qs.set('role',f.role); if(f.team) qs.set('team',f.team);
  if(f.type) qs.set('type',f.type);
  const box=document.getElementById('noteList'); box.innerHTML='<div class="empty">검색 중…</div>';
  try{
    const d=await api('/api/vault/search?'+qs.toString());
    renderRag({ok:d.ok,mode:d.mode});
    if(!d.ok){
      box.innerHTML='<div class="empty">검색 불가: '+esc(d.reason||'인덱스 없음')+'</div>';
      document.getElementById('resultMeta').textContent=''; return;
    }
    document.getElementById('resultMeta').textContent='검색결과 '+d.results.length+'개 · '+esc(d.mode);
    if(!d.results.length){ box.innerHTML='<div class="empty">검색 결과가 없습니다.</div>'; return; }
    box.innerHTML=d.results.map(n=>noteCard(n,true)).join('');
    bindCards();
  }catch(e){ box.innerHTML='<div class="empty">검색 실패: '+esc(e.message)+'</div>'; }
}
async function openNote(path){
  try{
    const d=await api('/api/vault/note?path='+encodeURIComponent(path));
    const fm=d.frontmatter||{};
    document.getElementById('mTitle').textContent=(d.body||'').trim().split('\\n')[0].slice(0,120)||path;
    const rows=[];
    if(fm.type) rows.push('<b>유형</b> '+esc(fm.type));
    if(fm.role) rows.push('<b>역할</b> '+esc(fm.role));
    if(fm.team) rows.push('<b>팀</b> '+esc(fm.team));
    if(fm.date) rows.push('<b>일시</b> '+esc(fm.date));
    if(fm.id) rows.push('<b>id</b> '+esc(fm.id));
    rows.push('<b>경로</b> '+esc(d.path));
    if(d.vault_file) rows.push('<b>파일</b> '+esc(d.vault_file));
    document.getElementById('mFm').innerHTML=rows.join(' · ');
    const links=[];
    if(d.obsidian_uri) links.push('<a href="'+esc(d.obsidian_uri)+'">Obsidian 에서 열기</a>');
    document.getElementById('mLinks').innerHTML=links.join('');
    document.getElementById('mBody').textContent=d.body||'(본문 없음)';
    document.getElementById('modalBack').classList.add('show');
  }catch(e){ toast('노트 열람 실패: '+e.message); }
}
function closeModal(){ document.getElementById('modalBack').classList.remove('show'); }
document.getElementById('searchBtn').addEventListener('click', doSearch);
document.getElementById('q').addEventListener('keydown', e=>{ if(e.key==='Enter') doSearch(); });
['fRole','fTeam','fType'].forEach(id=>document.getElementById(id)
  .addEventListener('change', ()=>{ searchMode?doSearch():loadList(); }));
document.getElementById('mclose').addEventListener('click', closeModal);
document.getElementById('modalBack').addEventListener('click', e=>{
  if(e.target.id==='modalBack') closeModal(); });
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeModal(); });
document.getElementById('logout').addEventListener('click', async ()=>{
  try{ await fetch('/api/logout',{method:'POST'}); }catch(e){}
  location.href='/login';
});
// 프로필 정보 로드 (사이드바 하단 표시용)
(async function(){
  try{
    const me=await api('/api/me');
    const label=me.label||me.login_id||'';
    const ROLE_KO={ceo:'CEO',admin:'관리자',staff:'직원'};
    document.getElementById('pfName').textContent=label||'사용자';
    document.getElementById('pfRole').textContent=(ROLE_KO[me.role]||me.role)
      +(me.login_id&&me.login_id!==label?' · '+me.login_id:'');
    document.getElementById('pfAvatar').textContent=(label||'H').trim().charAt(0).toUpperCase();
  }catch(e){ /* 세션 만료는 api() 내부에서 /login 리디렉트 처리됨 */ }
  await loadList();
})();
</script>
</body>
</html>"""


LOGIN_HTML = build_login_html()
INDEX_HTML = build_index_html()
VAULT_HTML = build_vault_html()


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
