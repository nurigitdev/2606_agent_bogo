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

HERE = os.path.dirname(os.path.abspath(__file__))


def _die_usage(msg: str):
    avail = ", ".join(sorted(
        f[:-3] for f in os.listdir(os.path.join(HERE, "agents")) if f.endswith(".md")))
    sys.stderr.write(f"{msg}\n사용법: python hermes_runtime.py <{avail}>\n")
    raise SystemExit(2)


if len(sys.argv) < 2:
    _die_usage("역할 인자가 없습니다.")
ROLE = sys.argv[1]


def parse_md(path):
    txt = open(path, encoding="utf-8").read()
    parts = txt.split("---", 2)
    fm, body = (parts[1], parts[2]) if len(parts) >= 3 else ("", txt)
    meta = {}
    for line in fm.strip().splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        meta[k] = [x.strip() for x in v.split(",")] if k in ("aliases", "channels") else v
    meta["prompt"] = body.strip()
    return meta


AGENTS_DIR = os.path.join(HERE, "agents")
ROLES = {f[:-3]: parse_md(os.path.join(AGENTS_DIR, f))
         for f in os.listdir(AGENTS_DIR) if f.endswith(".md")}
if ROLE not in ROLES:
    _die_usage(f"알 수 없는 역할: {ROLE!r}")
SPEC = ROLES[ROLE]
CH = json.load(open(os.path.join(HERE, "channels.json"), encoding="utf-8"))
ID2NAME = {v: k for k, v in CH.items()}
VALID_CHANNELS = set(CH)
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

ROUTING = """
[채널과 업무 흐름]
- 인사총무팀 / 개발팀: 팀원(사람)이 보고를 올리는 방. 팀 에이전트는 정리해 자기 팀 보고라인(인사총무→'인사총무-보고라인', 개발→'개발-보고라인')에 박민철을 멘션해 상신한다. 상위 지시의 1차 수행 결과도 이 방에서 팀원에게 전달.
- 인사총무-보고라인 / 개발-보고라인: 팀 에이전트와 박민철이 보고·피드백·지시를 주고받는 방.
- CEO브리핑: 박민철과 CEO(사람)만의 방. 박민철이 상신하고 CEO 지시를 받는다.

[역할별 행동]
- 이다은(인사총무): 인사총무팀 사람 보고 → '인사총무-보고라인'에 박민철 멘션 상신. 박민철 피드백 → 보완 회신. 박민철 지시(CEO발) → 1차 수행 후 인사총무팀에서 팀원에게 전달, closed.
- 최지현(개발): 개발팀 사람 보고 → '개발-보고라인'에 박민철 멘션 상신. 박민철 피드백 → 보완 회신. 박민철 지시(CEO발) → 1차 수행 후 개발팀에서 팀원에게 전달, closed.
- 박민철: 양 보고라인의 팀 보고를 받아 → 보완 필요하면 팀 에이전트 멘션해 반려, 충분하면 importance 판단(routine=간단 정리, decision_needed='CEO브리핑' 상신). CEO브리핑의 CEO 지시 → 인사총무 건은 '인사총무-보고라인' 이다은, 개발 건은 '개발-보고라인' 최지현 멘션해 분배. ★팀 상신을 단어만 바꿔 복붙하지 마라. 반드시 (1)우선순위·시급도 판단 (2)팀이 놓친 리스크·연관 사안 추가 (3)팀 권고에 동조/수정 의견을 더해 '가공'한 뒤 올린다.

[보고/메시지 작성 포맷 — 사람이 읽는다]
대괄호 라벨([보고유형][핵심][배경])·영문 enum(decision_needed)·"~에 대해 보고드립니다" 군더더기 절대 금지. 대신:
- 첫 줄: 이모지 1개 + 결론 한 줄 굵게(**). 결정 요청 📌 / 리스크 ⚠️ / 단순 공유 📣.
- 빈 줄 뒤 근거 1~2줄(또는 "- " 불릿).
- 마지막 줄: 상대가 할 행동·요청·예상 시점.
줄바꿈으로 위계, 굵게는 결론·요청에만, 이모지 1~2개.

[의사결정 보고는 '그것만 보고 바로 결정'할 수 있어야 한다]
결정 요청(📌)에는 반드시: ① 정량 현황(수치·사실, "줄었다/저렴하다" 모호어 금지) ② 제안 구체(무엇·얼마·언제·범위) ③ 핵심 옵션 2개 + 각 득실 ④ 명확한 권고 + 근거 ⑤ 예상 효과·리스크. 인용한 핵심 수치는 출처(누가/어떻게 측정)도 함께. 못 채우면 올리지 말고, "필요시 요청" 같은 미룸 없이 그 자리에서 사람 방에 message로 부족 항목을 콕 집어 되묻는다. 박민철도 미완성은 CEO에 올리지 말고 팀에 콕 집어 반려.

[진행상황 공유]
사람이 보고를 올리면 그 사람 방(인사총무팀/개발팀)에 진행상황을 짧게 알린다(접수→상신→결정, 다음 액션·시점 포함). ack에 한 줄, ack_channel에 그 방 이름(없으면 빈 문자열).

[행동 결정 — 반드시 JSON 객체 하나만, 설명·코드펜스 금지]
{"act": true/false, "target_channel": "인사총무팀|개발팀|인사총무-보고라인|개발-보고라인|CEO브리핑|", "mentions": ["박민철|이다은|최지현"], "message": "target_channel 본문(없으면 빈)", "ack": "사람에게 알릴 진행상황(없으면 빈)", "ack_channel": "인사총무팀|개발팀|", "memo": "이 건에서 다음에 기억할 핵심 1줄(없으면 빈)", "importance": "routine|decision_needed|", "task_status": "open|closed|", "reason": "한줄"}
'지금 네 차례의 네 업무'일 때만 act=true. 아니면 act=false. 사이클 끝나면 task_status=closed.
"""


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
    sysmsg = (SPEC["prompt"] + f"\n\n[진행 중 업무 메모]\n{load_mem()}\n" + ROUTING
              + f"\n[너] 이름:{NAME} / 주 담당 방:{PRIMARY}")
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
            memo = (d.get("memo") or "").strip()
            if memo:
                save_mem(memo)
            elif d.get("task_status") == "closed":
                save_mem(f"{cname} 처리완료: {(d.get('reason') or '')[:60]}")


if __name__ == "__main__":
    asyncio.run(run())
