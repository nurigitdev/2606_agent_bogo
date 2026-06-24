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
    """단일 정적 HTML(외부 빌드체인 0, vanilla JS). 채널·기본값은 런타임 API 로 주입."""
    return """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes CEO 대시보드</title>
<style>
  :root { --bg:#0f1419; --card:#1a212b; --line:#28323e; --txt:#e6edf3;
          --mut:#8b97a5; --accent:#3b82f6; --ok:#22c55e; --bad:#ef4444; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",
         "Malgun Gothic",sans-serif; background:var(--bg); color:var(--txt); font-size:14px; }
  header { padding:14px 20px; border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:17px; margin:0; }
  .pill { font-size:12px; color:var(--mut); }
  .wrap { max-width:1200px; margin:0 auto; padding:18px 20px 60px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:14px 16px; min-height:160px; }
  .card h2 { font-size:14px; margin:0 0 4px; display:flex; justify-content:space-between; align-items:center; }
  .badge { font-size:11px; padding:2px 8px; border-radius:999px; background:#243140; color:var(--mut); }
  .badge.team { background:#1e3a5f; color:#93c5fd; }
  .badge.report { background:#3b2f1e; color:#fcd34d; }
  .badge.briefing { background:#2d1e3b; color:#d8b4fe; }
  .msgs { margin-top:8px; max-height:260px; overflow-y:auto; }
  .msg { padding:6px 0; border-top:1px solid var(--line); }
  .msg:first-child { border-top:none; }
  .msg .who { color:var(--accent); font-weight:600; font-size:12px; }
  .msg .when { color:var(--mut); font-size:11px; margin-left:6px; }
  .msg .body { white-space:pre-wrap; word-break:break-word; margin-top:2px; line-height:1.45; }
  .empty { color:var(--mut); font-size:12px; padding:10px 0; }
  .section-title { font-size:13px; color:var(--mut); margin:26px 0 10px; text-transform:uppercase; letter-spacing:.04em; }
  .composer { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .composer .row { display:flex; gap:10px; margin-bottom:10px; flex-wrap:wrap; align-items:center; }
  select, textarea, button { font-family:inherit; font-size:14px; }
  select { background:#0d1117; color:var(--txt); border:1px solid var(--line);
           border-radius:7px; padding:8px 10px; }
  textarea { width:100%; min-height:78px; background:#0d1117; color:var(--txt);
             border:1px solid var(--line); border-radius:7px; padding:10px; resize:vertical; }
  button { background:var(--accent); color:#fff; border:none; border-radius:7px;
           padding:9px 18px; cursor:pointer; font-weight:600; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .roster { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:12px; }
  .agent { background:var(--card); border:1px solid var(--line); border-radius:9px; padding:12px 14px; }
  .agent .nm { font-weight:600; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }
  .dot.on { background:var(--ok); } .dot.off { background:var(--bad); }
  .agent .meta { color:var(--mut); font-size:12px; margin-top:4px; line-height:1.5; }
  .note { color:var(--mut); font-size:12px; margin-top:8px; }
  .toast { position:fixed; bottom:18px; left:50%; transform:translateX(-50%);
           background:#243140; border:1px solid var(--line); color:var(--txt);
           padding:10px 18px; border-radius:8px; opacity:0; transition:opacity .25s; pointer-events:none; }
  .toast.show { opacity:1; }
  a { color:var(--accent); }
</style>
</head>
<body>
<header>
  <h1>Hermes CEO 대시보드</h1>
  <span class="pill" id="clock"></span>
  <span class="pill">자동 새로고침 <span id="poll">12</span>초 · 루프백 전용(127.0.0.1)</span>
</header>
<div class="wrap">
  <div class="section-title">부서별 현황</div>
  <div class="grid" id="cards"></div>

  <div class="section-title">CEO 지시</div>
  <div class="composer">
    <div class="row">
      <label for="ch">대상 채널</label>
      <select id="ch"></select>
      <span class="pill">기본 CEO브리핑 → 박민철(비서실장)에게 전달</span>
    </div>
    <textarea id="msg" placeholder="지시 내용을 입력하세요. 예) 개발팀 이번 주 출시 일정 정리해서 올려주세요."></textarea>
    <div class="row" style="margin-top:10px; justify-content:flex-end;">
      <button id="send">지시 전송</button>
    </div>
  </div>

  <div class="section-title">에이전트 현황</div>
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

async function api(path, opts){ const r=await fetch(path,opts);
  const d=await r.json().catch(()=>({error:'응답 파싱 실패'}));
  if(!r.ok) throw new Error(d.error||('HTTP '+r.status)); return d; }

async function loadChannels(){
  const d = await api('/api/channels');
  channels = d.channels; defaultPost = d.default_post_channel;
  // 현황 카드 골격
  const cards = document.getElementById('cards'); cards.innerHTML='';
  channels.forEach(c=>{
    const el=document.createElement('div'); el.className='card';
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
