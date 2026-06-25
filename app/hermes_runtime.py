"""
사내 업무 에이전트 — Hermes AIAgent 런타임. (BENAN 결함 수정판)

BENAN 적대적 심사 반영:
  1) 메모리 쓰기(save_mem) — memo/closed 시 롤링 저장
  2) 송신 채널/멘션 화이트리스트(fail-closed) — 게이트키핑 우회 차단
  3) 출력 스키마 검증 + 1회 재시도 + 실패 로그/사람 안내(silent drop 제거)
  4) 백스톱 복구(60초 송신 상한) — 폭주 차단
  5) 멘션 엄격화(@username/풀네임/안전 별칭만)
  6) FALLBACK 모델 실제 배선
  7) API 키 환경변수(.env) 우선
실행: <venv>/python hermes_runtime.py <orchestrator|hr|dev>
"""
import asyncio
import json
import os
import sys
import time
import urllib.request

import websockets

import agent_schema as A

HERE = os.path.dirname(os.path.abspath(__file__))


def _die_usage(msg: str):
    avail = ", ".join(sorted(A.load_roles()))
    sys.stderr.write(f"{msg}\n사용법: python hermes_runtime.py <{avail}>\n")
    raise SystemExit(2)


if len(sys.argv) < 2:
    _die_usage("역할 인자가 없습니다.")
ROLE = sys.argv[1]

# 데이터(에이전트 정의·팀·채널·공유규칙) 로드 — 전부 데이터 분리, 파이썬 하드코딩 없음.
ROLES = A.load_roles()
if ROLE not in ROLES:
    _die_usage(f"알 수 없는 역할: {ROLE!r}")
SPEC = ROLES[ROLE]
CH = A.load_channels()
ID2NAME = {v: k for k, v in CH.items()}
VALID_CHANNELS = set(CH)
TEAMS = A.load_teams()
COMMON_RULES = A.load_common_rules()
ROUTING = A.build_routing(TEAMS)
# 이 역할 전용 학습방 정의(없으면 None). owner == ROLE 매칭.
LEARN_ROOM = A.learning_room_for_role(TEAMS, ROLE)

# 기동 시 가벼운 스키마 검증 — 정의가 깨졌으면 폭주 전에 멈춘다(fail-fast).
_errs = A.validate_roles(ROLES, VALID_CHANNELS) + A.validate_teams(TEAMS, VALID_CHANNELS)
if _errs:
    sys.stderr.write("에이전트/팀 정의 검증 실패:\n  " + "\n  ".join(_errs) + "\n")
    raise SystemExit(3)

CFG = json.load(open(os.path.join(HERE, f"{SPEC['config']}_config.json"), encoding="utf-8"))
LLM = json.load(open(os.path.join(HERE, "llm_config.json"), encoding="utf-8"))
TOKEN = CFG["bot_token"]
BOT_ID = CFG["bot_id"]
KEY = os.environ.get("OPENROUTER_API_KEY") or LLM.get("api_key", "")
MODEL = LLM["model"]
FALLBACK = LLM.get("fallback", "openai/gpt-4o-mini")
BASE_URL = LLM.get("base_url", "https://openrouter.ai/api/v1")
MM = "http://localhost:8065/api/v4"

NAME = SPEC["name"]
USERNAME = SPEC["username"]
ALIASES = SPEC["aliases"]
PRIMARY = SPEC["primary"]
SUBS = set(SPEC["channels"])
NAME2USER = {r["name"]: r["username"] for r in ROLES.values()}
MEM_PATH = os.path.join(HERE, f"memory_{ROLE}.json")
# 학습방 채널명(이 역할에 학습방이 선언돼 있을 때만). 매 이벤트에서 현재 방이
# 학습방인지 판별하는 데 쓴다.
LEARN_CHANNEL = (LEARN_ROOM or {}).get("channel", "")

if KEY:
    os.environ.setdefault("OPENROUTER_API_KEY", KEY)

BOT_ID_NAMES = {}
for _r in ROLES.values():
    try:
        BOT_ID_NAMES[json.load(open(os.path.join(HERE, f"{_r['config']}_config.json")))["bot_id"]] = _r["name"]
    except Exception:
        pass

_sent = []
BACKSTOP_WINDOW = 60
BACKSTOP_MAX = 10

# ROUTING 은 teams.json 으로부터 agent_schema.build_routing()이 동적 생성한다(상단에서 ROUTING 변수로 로드).
# 보고/메시지 포맷·작성원칙·진행공유·행동결정 JSON 스키마는 agents/_shared/common_rules.md(COMMON_RULES)로 분리되어
# system_prompt 조립 시 상속 주입된다. (이전의 통짜 하드코딩 ROUTING 문자열 제거됨.)


def call_llm(messages, model, max_tokens=1500):
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "temperature": 0.4, "response_format": {"type": "json_object"}}
    req = urllib.request.Request(
        BASE_URL + "/chat/completions", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST")
    r = json.loads(urllib.request.urlopen(req, timeout=60).read())
    return r["choices"][0]["message"]["content"]


def _strip_fence(s):
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    return s.strip()


def load_mem():
    try:
        return json.load(open(MEM_PATH, encoding="utf-8")).get("summary", "")
    except Exception:
        return ""


def save_mem(line):
    line = (line or "").strip().replace("\n", " ")
    if not line:
        return
    lines = [x for x in load_mem().split("\n") if x.strip()]
    lines.append(line)
    json.dump({"summary": "\n".join(lines[-6:])},
              open(MEM_PATH, "w", encoding="utf-8"), ensure_ascii=False)


# ── 메모리 3층: 역할별 '학습 노트'(persistent, 영구 누적 — 휘발 안 됨) ─────────
# 일반 방 메모리(memory_ch_*.json)와 역할 개인 메모(memory_{ROLE}.json)는 최근 6줄만
# 남기고 롤링 폐기된다. 학습방에 쌓이는 교정·노하우·정책은 그렇게 사라지면 안 되므로,
# 역할당 1개 전용 파일(memory_learn_{ROLE}.json)에 줄을 잘라내지 않고 누적 저장한다.
#  - 역할별 1개 파일이라 ROLE 경계가 곧 격리 경계 → 타 역할 학습 노트는 절대 섞이지 않는다.
#  - 롤링이 없으므로 학습방에 N줄을 넣으면 N줄이 그대로 보존된다(persistent 단언의 근거).
# 같은 줄(공백 정규화 후)이 이미 있으면 중복 저장하지 않는다(같은 교훈 반복 누적 방지).

def learn_path(role):
    return os.path.join(HERE, f"memory_learn_{role}.json")


def load_learn_note(role=None):
    """이 역할(기본 ROLE)의 학습 노트 전체 문자열. 파일 없으면 빈 문자열."""
    role = role or ROLE
    try:
        return json.load(open(learn_path(role), encoding="utf-8")).get("notes", "")
    except Exception:
        return ""


def save_learn_note(line, role=None):
    """학습 노트에 한 줄을 영구 누적(롤링 없음). 동일 줄은 중복 저장 안 함."""
    role = role or ROLE
    line = (line or "").strip().replace("\n", " ")
    if not line:
        return
    existing = [x for x in load_learn_note(role).split("\n") if x.strip()]
    if line in existing:
        return
    existing.append(line)
    json.dump({"notes": "\n".join(existing)},
              open(learn_path(role), "w", encoding="utf-8"), ensure_ascii=False)


# ── 메모리 2층 구조: 방(채널)별 공유 메모리 ───────────────────────────────
# 역할별 memory_{ROLE}.json(개인 진행 메모)과 별개로, 채널마다 memory_ch_{slug}.json을
# 둔다. 같은 방에 들어온 모든 에이전트(역할 무관)가 같은 파일을 공유해 읽고 쓴다.
# 다른 방을 처리할 때는 그 방 파일을 읽지 않으므로, 한 방의 내용(예: 급여)이 프롬프트를
# 통해 다른 방으로 새지 않는다(방 격리).

def _room_slug(channel_id):
    """채널 ID를 파일명 안전 슬러그로 정규화. 영숫자/-/_만 남기고 나머지는 _로."""
    cid = str(channel_id or "")
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in cid)


def room_mem_path(channel_id):
    return os.path.join(HERE, f"memory_ch_{_room_slug(channel_id)}.json")


def load_room_mem(channel_id):
    """해당 채널의 공유 기억 문자열. 파일이 없으면 빈 문자열로 시작."""
    if not channel_id:
        return ""
    try:
        return json.load(open(room_mem_path(channel_id), encoding="utf-8")).get("summary", "")
    except Exception:
        return ""


def save_room_mem(channel_id, line):
    """해당 채널의 공유 기억에 한 줄 롤링 추가(최근 6줄 유지). 다른 방 파일은 건드리지 않는다."""
    if not channel_id:
        return
    line = (line or "").strip().replace("\n", " ")
    if not line:
        return
    lines = [x for x in load_room_mem(channel_id).split("\n") if x.strip()]
    lines.append(line)
    json.dump({"summary": "\n".join(lines[-6:])},
              open(room_mem_path(channel_id), "w", encoding="utf-8"), ensure_ascii=False)


_pn = {}


def speaker(uid):
    if uid in BOT_ID_NAMES:
        return BOT_ID_NAMES[uid]
    if uid not in _pn:
        try:
            req = urllib.request.Request(MM + f"/users/{uid}", headers={"Authorization": f"Bearer {TOKEN}"})
            u = json.loads(urllib.request.urlopen(req, timeout=10).read())
            _pn[uid] = u.get("nickname") or u.get("username") or "사람"
        except Exception:
            _pn[uid] = "사람"
    return _pn[uid]


def history(channel_id, n=12):
    req = urllib.request.Request(MM + f"/channels/{channel_id}/posts?per_page={n}",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    lines = []
    for pid in reversed(d["order"]):
        p = d["posts"][pid]
        if p.get("type"):
            continue
        lines.append(f"{speaker(p['user_id'])}: {p.get('message', '')[:300]}")
    return lines[-n:]


def _validate(d):
    if not isinstance(d, dict):
        return "not-dict"
    if d.get("act") not in (True, False):
        return "act"
    tc = d.get("target_channel") or ""
    if tc and tc not in VALID_CHANNELS:
        return f"target:{tc}"
    ac = d.get("ack_channel") or ""
    if ac and ac not in VALID_CHANNELS:
        return f"ackch:{ac}"
    if (d.get("importance") or "") not in ("routine", "decision_needed", ""):
        return "importance"
    if (d.get("task_status") or "") not in ("open", "closed", ""):
        return "task_status"
    return ""


def decide(cname, channel_id, sp, text):
    # 메모리 3층 주입:
    #  - learn_note: 이 역할 전용 학습 노트(persistent). 방과 무관하게 항상 주입한다.
    #    역할별 1개 파일이라 타 역할 학습노트는 구조적으로 섞이지 않는다(방 격리 유지).
    #  - room_memo: 현재 방(채널)의 공유 기억(롤링).
    #  - memo: 역할 개인 진행 메모(롤링).
    sysmsg = A.system_prompt(SPEC, COMMON_RULES, ROUTING,
                             memo=load_mem(), room_memo=load_room_mem(channel_id),
                             learn_note=load_learn_note())
    convo = "\n".join(history(channel_id))
    user = f"[현재 방: {cname}]\n[최근 대화]\n{convo}\n\n[방금 들어온 메시지] {sp}: {text}"
    last = ""
    for attempt in range(2):
        model = MODEL if attempt == 0 else FALLBACK
        u = user if attempt == 0 else user + "\n\n[경고] 직전 출력이 형식 오류였다. 설명 없이 유효한 JSON 객체 하나만 출력하라."
        try:
            out = call_llm([{"role": "system", "content": sysmsg},
                            {"role": "user", "content": u}], model)
        except Exception as e:
            last = f"llm:{e}"
            continue
        try:
            d = json.loads(_strip_fence(out), strict=False)
        except Exception as e:
            last = f"parse:{e}"
            continue
        why = _validate(d)
        if not why:
            return d
        last = f"validate:{why}"
    raise ValueError(f"decide invalid after retries: {last}")


def post(channel_id, message):
    req = urllib.request.Request(
        MM + "/posts",
        data=json.dumps({"channel_id": channel_id, "message": message}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        method="POST")
    urllib.request.urlopen(req, timeout=15)


def is_mentioned(text):
    if f"@{USERNAME}" in text:
        return True
    for token in [NAME] + list(ALIASES):
        if token and token in text:
            return True
    return False


def gate(text, is_bot, cname):
    if not is_bot:
        return True
    return is_mentioned(text)


def backstop_ok():
    now = time.time()
    _sent[:] = [t for t in _sent if now - t < BACKSTOP_WINDOW]
    if len(_sent) >= BACKSTOP_MAX:
        return False
    _sent.append(now)
    return True


def can_send(cname):
    return cname in SUBS


async def run():
    async with websockets.connect("ws://localhost:8065/api/v4/websocket") as ws:
        await ws.send(json.dumps({"seq": 1, "action": "authentication_challenge",
                                  "data": {"token": TOKEN}}))
        print(f"{NAME}({ROLE}) 가동[Hermes] 모델:{MODEL} 폴백:{FALLBACK} 구독:{SUBS}")
        async for raw in ws:
            ev = json.loads(raw)
            if ev.get("event") != "posted":
                continue
            p = json.loads(ev["data"]["post"])
            if p.get("user_id") == BOT_ID:
                continue
            cid = p["channel_id"]
            cname = ID2NAME.get(cid)
            if cname not in SUBS:
                continue
            is_bot = p.get("props", {}).get("from_bot") == "true"
            text = p.get("message", "")
            # 학습방 트리거: 이 역할의 학습방에 사람이 올린 메시지(교정·노하우·정책)는
            # 휘발 없이 학습 노트에 영구 누적한다. 봇 메아리는 누적하지 않는다.
            # 누적은 게이트(멘션 여부)와 무관 — 학습방의 모든 사람 발화가 학습 대상.
            if LEARN_CHANNEL and cname == LEARN_CHANNEL and not is_bot and text.strip():
                sp_name = speaker(p["user_id"])
                save_learn_note(f"{sp_name}: {text.strip()[:300]}")
                print(f"[{NAME}] 학습 노트 누적 ← {LEARN_CHANNEL}: {text.strip()[:40]}")
            if not gate(text, is_bot, cname):
                continue
            try:
                d = await asyncio.to_thread(decide, cname, cid, speaker(p["user_id"]), text)
            except Exception as e:
                print(f"DECIDE-FAIL [{NAME}] {cname}: {e}")
                if not is_bot and can_send(cname):
                    try:
                        post(cid, "⚠️ 처리 중 일시 오류가 발생해 재시도가 필요합니다. 잠시 후 다시 봐주세요.")
                    except Exception:
                        pass
                continue
            if not d.get("act"):
                continue
            if not backstop_ok():
                print(f"BACKSTOP [{NAME}] 60초 송신 상한 — 차단")
                continue
            ackmsg = (d.get("ack") or "").strip()
            ackcn = d.get("ack_channel") or ""
            if ackmsg and ackcn:
                if can_send(ackcn):
                    try:
                        post(CH[ackcn], ackmsg)
                    except Exception as e:
                        print("ack-err", e)
                else:
                    print(f"ACK-BLOCK [{NAME}] 권한 밖 ack_channel:{ackcn}")
            tcn = d.get("target_channel") or ""
            msg = (d.get("message") or "").strip()
            if tcn and msg:
                if not can_send(tcn):
                    print(f"SEND-BLOCK [{NAME}] 권한 밖 target:{tcn} (게이트키핑 우회 차단)")
                else:
                    ments = d.get("mentions") or []
                    pre = " ".join(f"@{NAME2USER[m]}" for m in ments if m in NAME2USER)
                    body = (pre + "\n" if pre else "") + msg
                    try:
                        post(CH[tcn], body)
                        print(f"[{NAME}] → {tcn} ({d.get('reason', '')[:40]})")
                    except Exception as e:
                        print("post-err", e)
            # 메모리 2층 저장:
            #  - LLM이 남기는 메모(memo)와 closed 처리완료 메모는 그 방의 맥락이므로
            #    방별 공유 메모리에 저장한다 → 다른 방으로 절대 새지 않음(격리 1순위).
            #  - 개인 진행 추적용으로 역할별 메모리에도 보조 저장(방 이름을 접두로 표기해
            #    개인 메모가 방 맥락과 섞여도 출처를 알 수 있게 한다).
            memo = (d.get("memo") or "").strip()
            if memo:
                save_room_mem(cid, memo)
                save_mem(f"[{cname}] {memo}")
            elif d.get("task_status") == "closed":
                done = f"{cname} 처리완료: {(d.get('reason') or '')[:60]}"
                save_room_mem(cid, done)
                save_mem(done)


if __name__ == "__main__":
    asyncio.run(run())
