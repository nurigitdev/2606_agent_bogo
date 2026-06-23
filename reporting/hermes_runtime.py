"""
사내 업무 에이전트 — Hermes Agent(Nous Research) 런타임 위에서 구동.

두뇌: Hermes `AIAgent`(run_agent.AIAgent) + 모델 DeepSeek V4 Flash(OpenRouter 경유).
통로: Mattermost(WebSocket 수신 → REST 게시) = 메시징 게이트웨이(커스텀 어댑터).

기존 동작을 1:1 이상 이전:
  - 매 메시지마다 decide(LLM JSON)로 '지금 내 차례인가 + 무엇을 할까'를 스스로 판단
  - 봇 메시지는 '내가 멘션됐을 때만' 판단(메아리 차단)
  - 핸드오프는 멘션으로 다음 행위자 지명
  - 할 일이 끝나면 act=false로 침묵 / task_status=closed

차이점(핵심): LLM 호출이 raw urllib(OpenRouter)에서 Hermes AIAgent.chat()으로 교체됨.
  AIAgent가 provider 레이어·재시도·대화 루프를 내부 처리하고 최종 텍스트만 돌려준다.
  도구는 전부 비활성(이 에이전트는 JSON 판단만 생성).

실행: <venv>/python hermes_runtime.py <orchestrator|hr|dev>

주의: 파일명을 agent.py로 두면 hermes-agent 패키지의 `agent` 모듈과 충돌하므로
      반드시 hermes_runtime.py 등 다른 이름을 사용한다(cwd 셰도잉 방지).
"""
import asyncio
import json
import os
import sys
import urllib.request

import websockets

# Hermes Agent 런타임 — 두뇌. import 실패 시 즉시 중단(우회 금지).
from run_agent import AIAgent  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _die_usage(msg: str) -> "None":
    """잘못된 실행 인자에 대해 원시 트레이스백 대신 명확한 사용법을 출력하고 종료."""
    avail = ", ".join(sorted(
        f[:-3] for f in os.listdir(os.path.join(HERE, "agents")) if f.endswith(".md")
    ))
    sys.stderr.write(f"{msg}\n사용법: python hermes_runtime.py <{avail}>\n")
    raise SystemExit(2)


if len(sys.argv) < 2:
    _die_usage("역할 인자가 없습니다.")
ROLE = sys.argv[1]


def parse_md(path):
    """에이전트 .md(프론트매터 + 본문 프롬프트)를 파싱한다."""
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
CFG = json.load(open(os.path.join(HERE, f"{SPEC['config']}_config.json"), encoding="utf-8"))
LLM = json.load(open(os.path.join(HERE, "llm_config.json"), encoding="utf-8"))
TOKEN = CFG["bot_token"]
BOT_ID = CFG["bot_id"]
KEY = LLM["api_key"]
MODEL = LLM["model"]                       # deepseek/deepseek-v4-flash
BASE_URL = LLM.get("base_url", "https://openrouter.ai/api/v1")
MM = "http://localhost:8065/api/v4"

NAME = SPEC["name"]
USERNAME = SPEC["username"]
ALIASES = SPEC["aliases"]
PRIMARY = SPEC["primary"]
SUBS = set(SPEC["channels"])
NAME2USER = {r["name"]: r["username"] for r in ROLES.values()}
MEM_PATH = os.path.join(HERE, f"memory_{ROLE}.json")

# Hermes/OpenRouter provider 레이어가 환경변수에서 키를 읽을 수 있도록 보강.
# (AIAgent에 api_key를 직접 넘기지만, provider 내부 경로가 env를 읽는 경우 대비.)
os.environ.setdefault("OPENROUTER_API_KEY", KEY)

BOT_ID_NAMES = {}
for _r in ROLES.values():
    try:
        BOT_ID_NAMES[json.load(open(os.path.join(HERE, f"{_r['config']}_config.json")))["bot_id"]] = _r["name"]
    except Exception:
        pass

ROUTING = """
[채널과 업무 흐름]
- 인사총무팀 / 개발팀: 팀원(사람)이 보고를 올리는 방. 팀 에이전트는 여기 보고를 받으면 정리해 자기 팀 전용 보고라인(인사총무→'인사총무-보고라인', 개발→'개발-보고라인')에 박민철을 멘션해 상신한다. 상위 지시의 1차 수행 결과도 이 방에서 팀원에게 전달한다.
- 인사총무-보고라인: 이다은과 박민철이 인사총무 보고·피드백·지시를 주고받는 방.
- 개발-보고라인: 최지현과 박민철이 개발 보고·피드백·지시를 주고받는 방.
- CEO브리핑: 박민철과 CEO(사람)만의 방. 박민철이 CEO에게 상신하고, CEO의 지시를 받는다.

[역할별 행동]
- 이다은(인사총무): 인사총무팀의 사람 보고 → '인사총무-보고라인'에 박민철 멘션해 정리·상신. 인사총무-보고라인에서 박민철 피드백 → 보완해 회신. 박민철의 지시(CEO발) → 1차 수행 후 인사총무팀에서 팀원에게 전달하고 task_status=closed.
- 최지현(개발): 개발팀의 사람 보고 → '개발-보고라인'에 박민철 멘션해 정리·상신. 개발-보고라인에서 박민철 피드백 → 보완해 회신. 박민철의 지시(CEO발) → 1차 수행 후 개발팀에서 팀원에게 전달하고 task_status=closed.
- 박민철: 양 보고라인('인사총무-보고라인','개발-보고라인')의 팀 보고를 받아 → 보완 필요하면 해당 팀 에이전트 멘션해 피드백, 충분하면 importance 판단(routine=간단 정리, decision_needed='CEO브리핑'에 보고 스페셜리스트 포맷으로 상신). CEO브리핑의 CEO 지시 → 인사총무 건은 '인사총무-보고라인'에 이다은 멘션, 개발 건은 '개발-보고라인'에 최지현 멘션해 분배.

[보고/메시지 작성 포맷 — 사람이 읽는다]
대괄호 라벨([보고유형][핵심][배경])·영문 enum(decision_needed)·"~에 대해 보고드립니다" 같은 군더더기를 절대 쓰지 마라. 대신:
- 첫 줄: 이모지 1개 + 결론 한 줄을 굵게(**). 결정 요청은 📌, 리스크는 ⚠️, 단순 공유는 📣.
- 빈 줄 뒤 근거 1~2줄(또는 "- " 불릿).
- 마지막 줄: 상대가 할 행동·요청·다음 단계와 예상 시점.
줄바꿈(\\n)으로 위계를 만들고, 굵게는 결론·요청에만, 이모지는 1~2개. 한 메시지에 깔끔히 정리한다.

[의사결정 보고는 '그것만 보고 바로 결정'할 수 있어야 한다 — 가장 중요]
결정 요청(📌) 보고에는 반드시 담아라:
① 정량 현황: 수치·사실로. "격차가 줄었다/저렴하다/괜찮다" 같은 모호한 말 금지. 비용·규모·기간을 숫자로(예: 월 토큰비용 800만원→150만원, 81% 절감).
② 제안의 구체: 무엇을, 얼마, 언제, 어느 범위로.
③ 핵심 옵션 2개 내외 + 각 득실(추진/보류 시 각각 얻고 잃는 것).
④ 명확한 권고 + 그 근거.
⑤ 결정 시 예상 효과·리스크.
정보가 부족해 위를 못 채우면 절대 그대로 올리지 마라. 단 "필요시 요청드리겠습니다"처럼 미루는 것도 금지다. 팀 에이전트는 그 자리에서 사람 방에 message로 부족한 항목을 콕 집어 되묻는다. 사람이 답하면 그때 채워서 상신한다. 박민철도 보완 안 된 보고는 CEO에 올리지 말고, 해당 팀 에이전트에게 무엇이 빠졌는지 콕 집어 돌려보낸다. CEO에는 결정 가능한 완성 보고만 간다.

[진행상황 공유 — 사람 답답하지 않게]
사람이 보고를 올리면 상신·처리하는 동안 그 사람 방(인사총무팀/개발팀)에 진행상황을 짧게 알린다. 접수 시 "확인했습니다. 검토 후 박실장께 상신하겠습니다"처럼 다음 액션·예상 시점을 함께. 상신·결정이 나면 결과를 다시 그 방에 전달. ack에 그 한 줄을, ack_channel에 그 사람 방 이름을 넣는다(알릴 게 없으면 ack는 빈 문자열).

[행동 결정 — 반드시 JSON만 출력]
{"act": true/false, "target_channel": "인사총무팀|개발팀|인사총무-보고라인|개발-보고라인|CEO브리핑", "mentions": ["다음 행위자 0~2명: 박민철|이다은|최지현"], "message": "target_channel에 올릴 본문(위 포맷 준수, 없으면 빈 문자열)", "ack": "보고 올린 사람에게 알릴 진행상황 한 줄(없으면 빈 문자열)", "ack_channel": "ack 올릴 사람 방(인사총무팀|개발팀, 없으면 빈 문자열)", "importance": "routine|decision_needed", "task_status": "open|closed", "reason": "한줄"}
다른 어떤 설명·코드펜스(```)도 붙이지 말고 JSON 객체 하나만 출력하라.
원칙: '지금 네 차례의 네 업무'일 때만 act=true(상신과 함께 사람에게 ack로 진행상황도 챙겨라). 네 일이 아니거나 이미 처리됐으면 act=false로 침묵. 한 사이클이 끝나면 task_status=closed.
"""


def _build_agent(system_prompt: str) -> AIAgent:
    """역할 system prompt를 입은 Hermes AIAgent 인스턴스를 만든다.

    - model: DeepSeek V4 Flash (OpenRouter 포맷)
    - base_url/api_key: OpenRouter 직접 지정(기존 키 재사용, 신규 키 요구 없음)
    - 도구 전부 비활성(disabled_toolsets=전체) + skip_memory: 이 에이전트는 JSON 판단만 생성
    - quiet_mode: CLI 출력 억제
    - max_iterations는 여유를 둔다(1이면 Hermes가 'summarise' 강제 경로로 빠져 응답이 잘림).
      도구가 전부 비활성이라 실제 루프는 1회로 끝나지만, 내부 continuation 여지를 남긴다.
    - max_tokens는 박민철의 의사결정 보고(상세) + JSON을 담도록 넉넉히.
    스레드 안전성 원칙상 호출 시점마다 새 인스턴스를 만든다.
    """
    return AIAgent(
        model=MODEL,
        api_key=KEY,
        base_url=BASE_URL,
        ephemeral_system_prompt=system_prompt,
        quiet_mode=True,
        save_trajectories=False,
        skip_memory=True,
        skip_context_files=True,
        max_iterations=4,            # 도구 비활성이나 continuation 여지 확보(1=강제 summarise→잘림)
        disabled_toolsets=["all"],   # 모든 도구 비활성 → 순수 텍스트(JSON) 생성
        max_tokens=1500,             # 의사결정 보고 본문 + JSON 충분히
    )


def _strip_fence(s: str) -> str:
    """모델이 ```json ... ``` 코드펜스를 붙여도 JSON 본문만 추출한다."""
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        # 첫 줄이 'json' 라벨이면 제거
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    # 첫 '{' ~ 마지막 '}' 구간으로 보정
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    return s.strip()


def load_mem():
    try:
        return json.load(open(MEM_PATH, encoding="utf-8")).get("summary", "")
    except Exception:
        return ""


_pn = {}


def speaker(uid):
    if not uid:
        return "사람"                         # user_id 누락 시 불필요한 /users/None 조회 방지
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
    """채널 최근 대화를 화자:본문 줄 목록으로. 조회 실패/오류 본문이면 빈 맥락으로 강등(판단은 계속)."""
    try:
        req = urllib.request.Request(MM + f"/channels/{channel_id}/posts?per_page={n}",
                                     headers={"Authorization": f"Bearer {TOKEN}"})
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
        order, posts = d["order"], d["posts"]    # 오류 본문(message/status_code)이면 KeyError
    except Exception as e:
        print("history-err", type(e).__name__, e)
        return []
    lines = []
    for pid in reversed(order):
        p = posts.get(pid, {})
        if p.get("type"):
            continue
        lines.append(f"{speaker(p.get('user_id'))}: {p.get('message', '')[:300]}")
    return lines[-n:]


def decide(cname, channel_id, sp, text):
    """Hermes AIAgent(두뇌=DeepSeek)로 행동 JSON을 생성한다."""
    sysmsg = (SPEC["prompt"] + f"\n\n[진행 중 업무 메모]\n{load_mem()}\n" + ROUTING
              + f"\n[너] 이름:{NAME} / 주 담당 방:{PRIMARY}")
    convo = "\n".join(history(channel_id))
    user = f"[현재 방: {cname}]\n[최근 대화]\n{convo}\n\n[방금 들어온 메시지] {sp}: {text}"
    agent = _build_agent(sysmsg)
    out = agent.chat(user)
    return json.loads(_strip_fence(out), strict=False)  # 본문 내 개행 허용


def post(channel_id, message):
    req = urllib.request.Request(
        MM + "/posts",
        data=json.dumps({"channel_id": channel_id, "message": message}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        method="POST")
    urllib.request.urlopen(req, timeout=15)


def gate(text, is_bot, cname):
    # 사람 메시지는 구독한 모든 방에서 통과(하향 통신 보장).
    # 봇 메시지는 멘션됐을 때만 통과(메아리 차단).
    if not is_bot:
        return True
    mentioned = (NAME in text or f"@{USERNAME}" in text or USERNAME in text
                 or any(a in text for a in ALIASES))
    return mentioned


WS_URL = "ws://localhost:8065/api/v4/websocket"


async def _handle(raw):
    """단일 WS 프레임을 처리한다. 한 메시지의 어떤 실패도 루프를 죽이지 않게 격리한다."""
    try:
        ev = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        print("ws-frame-err", e)            # 깨진 프레임 → 무시(연결은 유지)
        return
    if ev.get("event") != "posted":
        return
    try:
        p = json.loads(ev["data"]["post"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print("post-parse-err", e)
        return
    if p.get("user_id") == BOT_ID:
        return
    cid = p.get("channel_id")
    cname = ID2NAME.get(cid)
    if cname not in SUBS:
        return
    is_bot = p.get("props", {}).get("from_bot") == "true"
    text = p.get("message", "")
    if not gate(text, is_bot, cname):
        return
    try:
        # decide는 동기(블로킹) Hermes 호출이므로 스레드로 오프로드.
        # 내부 Mattermost 조회(history/speaker) 또는 LLM/JSON 파싱 실패가
        # 여기로 전파되며, 메시지 단위로만 드롭한다(루프·연결은 유지).
        d = await asyncio.to_thread(decide, cname, cid, speaker(p["user_id"]), text)
    except Exception as e:
        print("decide-err", type(e).__name__, e)
        return
    if not d.get("act"):
        return
    # 진행상황 공유(사람 방)
    ackmsg = (d.get("ack") or "").strip()
    ackch = CH.get(d.get("ack_channel", ""))
    if ackmsg and ackch:
        try:
            post(ackch, ackmsg)
        except Exception as e:
            print("ack-err", e)
    # 본 메시지(상신/지시/전달)
    tgt = CH.get(d.get("target_channel", ""))
    msg = (d.get("message") or "").strip()
    if tgt and msg:
        ments = d.get("mentions") if isinstance(d.get("mentions"), list) else []
        pre = " ".join(f"@{NAME2USER[m]}" for m in ments if m in NAME2USER)
        body = (pre + "\n" if pre else "") + msg
        try:
            post(tgt, body)
            print(f"[{NAME}] → {d.get('target_channel')} ({d.get('reason', '')[:40]})")
        except Exception as e:
            print("post-err", e)


async def _session():
    """WS 1회 연결: 인증 → 인증 응답 확인 → 메시지 수신 루프."""
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"seq": 1, "action": "authentication_challenge",
                                  "data": {"token": TOKEN}}))
        # 인증 응답 확인: 첫 프레임이 'hello'면 성공, 'error'면 토큰 불량 → 재시도 무의미.
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if first.get("status") == "FAIL" or first.get("event") == "error":
                raise RuntimeError(f"WS 인증 실패(토큰 확인 필요): {first}")
            if first.get("event") not in ("hello", None) and first.get("seq_reply") != 1:
                # hello가 아니어도 정상 이벤트일 수 있으므로 처리만 하고 진행.
                await _handle(json.dumps(first))
        except asyncio.TimeoutError:
            print("auth-ack-timeout — 응답 없이 수신 모드 진입")
        print(f"{NAME}({ROLE}) 가동[Hermes Agent 런타임] — 모델:{MODEL} / 구독:{SUBS}")
        async for raw in ws:
            await _handle(raw)


async def run():
    """재연결 슈퍼바이저: 연결이 끊기면 백오프 후 재접속(데몬은 죽지 않는다).

    인증 실패(토큰 불량)는 재시도해도 동일하므로 즉시 중단해 운영자가 알게 한다.
    """
    backoff = 1
    while True:
        try:
            await _session()
            backoff = 1                      # 정상 종료(서버측 close) → 즉시 재접속
        except RuntimeError as e:            # 인증 실패 등 비복구 오류
            print("fatal", e)
            raise
        except (OSError, websockets.WebSocketException) as e:
            print(f"ws-disconnect ({type(e).__name__}: {e}) — {backoff}s 후 재접속")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)   # 지수 백오프(최대 30s)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print(f"\n{NAME}({ROLE}) 종료")
