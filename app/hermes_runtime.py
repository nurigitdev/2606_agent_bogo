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
import hermes_brain as B

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


def _env_int(name, default, lo, hi):
    """환경변수에서 정수 설정을 읽되 [lo, hi] 범위로 강제 클램프(비용 폭주·오설정 방지)."""
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


# ── 에이전트 루프 비용·지연 통제 파라미터(전부 env 조정 가능, 안전 범위로 클램프) ─────
# REACT_MAX_STEPS: ReAct 루프 1회 decide 당 LLM 호출 상한(thought→action 반복 수).
#   기본 4. 1~8 로 클램프 — 단발(1) 대비 호출 증가를 구조적으로 상한 안에 가둔다.
REACT_MAX_STEPS = _env_int("HERMES_REACT_MAX_STEPS", 4, 1, 8)
# 도구 결과를 observation 으로 환류할 때 토큰 폭주를 막는 문자 절단 상한.
TOOL_RESULT_MAX_CHARS = _env_int("HERMES_TOOL_RESULT_MAX_CHARS", 1200, 200, 6000)
# get_channel_history 도구가 한 번에 가져올 수 있는 최근 메시지 수 상한.
TOOL_HISTORY_MAX = _env_int("HERMES_TOOL_HISTORY_MAX", 24, 4, 60)
# Reflexion 최종 점검 패스 활성화 여부(1=on, 0=off). on 이어도 패스는 정확히 1회만.
REFLEXION_ON = _env_int("HERMES_REFLEXION", 1, 0, 1) == 1
# 도구 호출 결과 토큰 절단과 별개로, decide 1회의 전체 LLM 호출 절대 상한(무한루프 백스톱).
#   = ReAct 단계(REACT_MAX_STEPS) + Reflexion(최대 1) + 형식오류 재시도 여유(1).
LLM_CALL_HARD_CAP = REACT_MAX_STEPS + 2

# 이 봇이 가장 최근에 실제 송신한 (원지시 맥락, 응답 본문). 학습방에서 교정이 들어왔을 때
# '원지시'와 '잘못된출력'을 자동으로 채우는 데 쓴다. 프로세스 메모리(휘발) — 교정의 보조 맥락일 뿐,
# 핵심인 교정 내용 자체는 학습 노트에 영구 저장된다.
_last_response = {"context": "", "message": ""}

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


def call_llm_msg(messages, model, tools=None, tool_choice=None, max_tokens=1500):
    """OpenRouter chat completion — tools(function calling) 지원판.
    단발 call_llm 과 달리 메시지 객체 전체(content + tool_calls)를 반환해 ReAct 루프가
    도구 호출을 환류할 수 있게 한다. tools 가 없으면 일반 호출과 동일하게 동작한다.
    JSON response_format 은 tools 와 함께 쓰지 않는다(도구 호출 응답과 충돌하므로)."""
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "temperature": 0.4}
    if tools:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    req = urllib.request.Request(
        BASE_URL + "/chat/completions", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST")
    r = json.loads(urllib.request.urlopen(req, timeout=60).read())
    return r["choices"][0]["message"]


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

# 교정 항목 라인 접두. 학습 노트의 한 줄이 이 접두로 시작하면 '반드시 지킬 교정'으로
# 취급한다. 일반 학습(노하우·정책 메모)과 구분해 (a) 프롬프트에서 강조 섹션으로 별도 렌더,
# (b) 상한 정리 시 우선 보존, (c) 같은 실수 반복 억제의 1순위 근거로 쓴다.
# agent_schema 를 단일 출처로 두고 그 값을 그대로 쓴다(저장 접두 ↔ 렌더 접두 불일치 방지).
CORRECTION_PREFIX = A.CORRECTION_PREFIX
# 학습 노트 무한 증식 방지 상한(전체 줄 수). 교정 항목은 이 상한에서 우선 보존되고,
# 일반 항목이 오래된 것부터 정리된다. persistent 원칙은 유지하되 폭주만 막는 수준.
LEARN_MAX_LINES = 40
# 교정 항목 자체 상한(교정만 너무 많아져도 정리). 가장 오래된 교정부터 정리.
LEARN_MAX_CORRECTIONS = 25


def learn_path(role):
    return os.path.join(HERE, f"memory_learn_{role}.json")


def load_learn_note(role=None):
    """이 역할(기본 ROLE)의 학습 노트 전체 문자열. 파일 없으면 빈 문자열."""
    role = role or ROLE
    try:
        return json.load(open(learn_path(role), encoding="utf-8")).get("notes", "")
    except Exception:
        return ""


def _learn_lines(role):
    return [x for x in load_learn_note(role).split("\n") if x.strip()]


def _is_correction_line(line):
    return line.strip().startswith(CORRECTION_PREFIX)


def _write_learn_lines(lines, role):
    json.dump({"notes": "\n".join(lines)},
              open(learn_path(role), "w", encoding="utf-8"), ensure_ascii=False)


def _prune_learn_lines(lines):
    """무한 증식 방지: 전체 상한 초과 시 오래된 '일반' 항목부터 정리(교정은 우선 보존).
    교정만으로도 상한을 넘으면 가장 오래된 교정부터 정리한다. persistent 유지·폭주 차단.
    입력 순서(오래된→최신)를 보존한 채 정리된 리스트를 반환한다."""
    corrections = [x for x in lines if _is_correction_line(x)]
    # 교정 항목 자체 상한 — 가장 오래된 교정부터 버린다(비교정은 전부 보존).
    if len(corrections) > LEARN_MAX_CORRECTIONS:
        drop = set(corrections[: len(corrections) - LEARN_MAX_CORRECTIONS])
        lines = [x for x in lines if not (_is_correction_line(x) and x in drop)]
    # 전체 상한 — 일반 항목을 오래된 것부터 버린다(교정 보존).
    while len([x for x in lines if x.strip()]) > LEARN_MAX_LINES:
        idx = next((i for i, x in enumerate(lines) if not _is_correction_line(x)), None)
        if idx is None:
            # 남은 게 전부 교정이면 상한을 넘겨도 교정을 보호(우선 보존 원칙).
            break
        lines.pop(idx)
    return lines


def save_learn_note(line, role=None):
    """학습 노트에 한 줄을 영구 누적(롤링 없음). 동일 줄은 중복 저장 안 함.
    저장 후 상한 정리를 적용해 무한 증식을 막되, 교정 항목은 우선 보존한다."""
    role = role or ROLE
    line = (line or "").strip().replace("\n", " ")
    if not line:
        return
    existing = _learn_lines(role)
    if line in existing:
        return
    existing.append(line)
    existing = _prune_learn_lines(existing)
    _write_learn_lines(existing, role)


def save_correction(original, wrong, fix, role=None):
    """교정 피드백을 구조화해 학습 노트에 '교정' 항목으로 영구 저장.
    원지시(original)·잘못된출력(wrong)·교정내용(fix)을 한 줄로 직렬화한다.
    다음 회차부터 [반드시 지킬 교정] 섹션으로 우선 주입되어 같은 실수 반복을 억제한다.
    교정은 상한 정리에서 일반 항목보다 우선 보존된다."""
    def _clean(s):
        return (s or "").strip().replace("\n", " ").replace("|||", "/")
    fix = _clean(fix)
    if not fix:
        return False
    parts = [f"교정={fix}"]
    orig = _clean(original)
    wrong = _clean(wrong)
    # 원지시·잘못된출력은 있으면 함께 기록(없어도 교정 내용만으로 유효).
    line = CORRECTION_PREFIX + " " + " ||| ".join(
        ([f"원지시={orig}"] if orig else []) + ([f"잘못된출력={wrong}"] if wrong else []) + parts)
    save_learn_note(line, role=role)
    return True


def load_corrections(role=None):
    """학습 노트 중 교정 항목만 추린 리스트(프롬프트 강조 렌더·self-check용)."""
    role = role or ROLE
    return [x for x in _learn_lines(role) if _is_correction_line(x)]


# 교정 피드백 트리거 키워드. 학습방에서 사람이 이 표현을 쓰면 단순 노하우가 아니라
# '직전 응답이 틀렸다'는 교정으로 간주해 교정 항목으로 구조화 저장한다.
CORRECTION_TRIGGERS = ("틀렸", "틀린", "아니라", "아니고", "잘못", "정정", "고쳐",
                       "그게 아니", "맞는 건", "맞는건", "오류", "수정해", "다시 해", "다시해")


def is_correction_feedback(text):
    """학습방 메시지가 '교정' 성격인지 판별. 트리거 키워드 포함 여부로 경량 판정."""
    t = (text or "")
    return any(k in t for k in CORRECTION_TRIGGERS)


def parse_correction(text):
    """사람 교정 메시지에서 (잘못된출력, 교정내용)을 경량 추출.
    'X가 아니라 Y' / 'X 아니고 Y' 패턴이면 X=잘못, Y=교정. 못 가르면 전체를 교정으로.
    무거운 LLM 호출 없이 규칙 기반 1패스로 처리한다."""
    t = (text or "").strip()
    for sep in ("가 아니라", "이 아니라", " 아니라", "가 아니고", "이 아니고", " 아니고"):
        if sep in t:
            wrong, fix = t.split(sep, 1)
            return wrong.strip(" ,.'\"" ), fix.strip(" ,.'\"")
    return "", t


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
    # self-check 필드(learn_applied)는 있으면 bool 이어야 한다. 없으면(하위 호환) 통과.
    la = d.get("learn_applied")
    if la is not None and la not in (True, False):
        return "learn_applied"
    return ""


def self_check(d, corrections):
    """경량 1패스 self-check: 주입된 교정과 현재 응답이 모순되는지 점검한다.
    무거운 다중 LLM 호출 없이, 모델이 스스로 채운 learn_applied/learn_basis 필드와
    교정 키워드의 응답 내 충돌 여부만 본다. 모순 의심 시 사유 문자열, 없으면 빈 문자열."""
    if not corrections:
        return ""
    # (1) 교정이 주입됐는데 모델이 learn_applied=false 로 무시했다면 모순 신호.
    if d.get("learn_applied") is False and d.get("act") is True:
        return "learn_applied=false(교정 무시한 채 송신 시도)"
    # (2) 교정의 '잘못된출력' 토큰이 이번 송신 본문에 그대로 다시 등장하면 재발 의심.
    body = " ".join(str(d.get(k) or "") for k in ("message", "ack", "reason"))
    if not body.strip():
        return ""
    for c in corrections:
        wrong = ""
        for seg in c.split("|||"):
            # 첫 세그먼트에는 '[교정]' 접두가 붙으므로 startswith 대신 키 위치를 찾는다.
            key = "잘못된출력="
            if key in seg:
                wrong = seg.split(key, 1)[1].strip()
        # 잘못된출력이 5자 이상 의미 토큰일 때만(짧은 토큰 오탐 방지) 재등장 검사.
        if len(wrong) >= 5 and wrong in body:
            return f"교정된 잘못된출력 재등장: {wrong[:30]}"
    return ""


def _truncate(s):
    """도구 결과를 observation 으로 환류할 때 토큰 폭주를 막기 위한 문자 절단."""
    s = s or ""
    if len(s) <= TOOL_RESULT_MAX_CHARS:
        return s
    return s[:TOOL_RESULT_MAX_CHARS] + " …(절단됨)"


def run_tool(name, args, channel_id):
    """안전 도구 화이트리스트 디스패처(읽기 전용). 화이트리스트 밖 이름은 fail-closed 로 거부.
    임의 셸·파일쓰기·외부망 도구는 존재하지 않는다 — 여기서 다루는 도구는 전부 부작용 없는 조회."""
    if name not in A.SAFE_TOOL_NAMES:
        return f"[거부] 허용되지 않은 도구: {name}"
    if name == A.TOOL_GET_ROOM_MEMORY:
        return _truncate(load_room_mem(channel_id) or "(이 방 공유 기억 없음)")
    if name == A.TOOL_GET_LEARN_NOTE:
        return _truncate(load_learn_note() or "(학습 노트 없음)")
    if name == A.TOOL_GET_CHANNEL_HISTORY:
        n = TOOL_HISTORY_MAX
        try:
            req_n = int((args or {}).get("n", TOOL_HISTORY_MAX))
            n = max(1, min(TOOL_HISTORY_MAX, req_n))  # 상한 강제(토큰·지연 통제)
        except (TypeError, ValueError):
            n = TOOL_HISTORY_MAX
        return _truncate("\n".join(history(channel_id, n=n)) or "(대화 없음)")
    # finalize 는 루프에서 직접 처리되므로 여기 도달하지 않는다(안전망).
    return "[거부] 알 수 없는 도구"


def _finalize_args_to_decision(args):
    """finalize 도구의 arguments(dict) → 기존 행동 결정 dict. 누락 필드는 기존 단발 스키마
    기본값으로 채워 _validate/송신부 계약을 그대로 유지한다(스키마 호환)."""
    args = args if isinstance(args, dict) else {}
    d = {}
    for k in A.FINALIZE_FIELDS:
        if k in args:
            d[k] = args[k]
    if "act" not in d:
        d["act"] = False  # act 누락 = 침묵(보수적 기본값)
    return d


def _extract_tool_calls(msg):
    """LLM 메시지 객체에서 tool_calls 리스트를 정규화해 (name, args, raw) 리스트로 반환.
    args 는 JSON 문자열일 수 있으므로 dict 로 파싱(실패 시 빈 dict)."""
    out = []
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except Exception:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        out.append((name, args, tc))
    return out


def _react_loop(sysmsg, user, corrections, channel_id, calls):
    """ReAct 다단계 루프: 매 반복 모델이 thought/critique 후 읽기 도구를 호출하거나 finalize.
    calls = [현재까지 LLM 호출 수] (가변 리스트로 호출자와 카운터 공유 — 하드캡 백스톱용).
    반환: (행동 결정 dict, 경고/사유). finalize 도구 호출 시 즉시 탈출(조기 종료)."""
    tools = A.react_tool_specs(include_finalize=True)
    messages = [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}]
    last = ""
    for step in range(REACT_MAX_STEPS):
        if len(calls) >= LLM_CALL_HARD_CAP:
            last = "hardcap"
            break
        # 마지막 단계에서는 finalize 를 강제(무한 도구호출·미확정 방지).
        force_final = (step == REACT_MAX_STEPS - 1)
        tool_choice = {"type": "function", "function": {"name": A.TOOL_FINALIZE}} if force_final else "auto"
        calls[0] += 1
        try:
            msg = call_llm_msg(messages, MODEL, tools=tools, tool_choice=tool_choice)
        except Exception as e:
            last = f"llm:{e}"
            break
        tcs = _extract_tool_calls(msg)
        if not tcs:
            # 도구 호출 없이 텍스트만 → finalize 하도록 한 번 더 유도(다음 단계에서 강제됨).
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            messages.append({"role": "user",
                             "content": "행동은 finalize 도구로만 확정된다. finalize 를 호출하라."})
            last = "no-tool-call"
            continue
        # assistant 의 tool_calls 메시지를 대화에 추가(프로토콜상 tool 응답 전 필수).
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": msg.get("tool_calls")})
        finalized = None
        for name, args, tc in tcs:
            if name == A.TOOL_FINALIZE:
                finalized = _finalize_args_to_decision(args)
                break
            obs = run_tool(name, args, channel_id)
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "name": name, "content": obs})
        if finalized is not None:
            return finalized, last  # 조기 종료: finalize 즉시 탈출
        # finalize 가 아니면 다음 반복으로(도구 observation 이 messages 에 환류된 상태).
    return None, last or "no-finalize"


def _reflexion_pass(sysmsg, user, decision, corrections, channel_id, calls):
    """Reflexion 자기검증 1패스(정확히 1회). 확정된 결정을 같은 모델에 다시 보여 주고
    (교정위반·지시충족·근거충분) 점검 후 finalize 로 재확정하게 한다. 실패하면 원결정 유지."""
    if len(calls) >= LLM_CALL_HARD_CAP:
        return decision
    tools = A.react_tool_specs(include_finalize=True)
    review = (user + "\n\n[직전에 확정한 최종 행동]\n" + json.dumps(decision, ensure_ascii=False)
              + A.reflexion_prompt(corrections))
    messages = [{"role": "system", "content": sysmsg}, {"role": "user", "content": review}]
    calls[0] += 1
    try:
        msg = call_llm_msg(messages, MODEL, tools=tools,
                           tool_choice={"type": "function", "function": {"name": A.TOOL_FINALIZE}})
    except Exception as e:
        print(f"REFLEXION-SKIP [{NAME}] 점검 호출 실패, 원결정 유지: {e}")
        return decision
    for name, args, _tc in _extract_tool_calls(msg):
        if name == A.TOOL_FINALIZE:
            revised = _finalize_args_to_decision(args)
            if _validate(revised) == "":
                return revised
    return decision  # 재확정 실패 시 원결정 유지(보수적)


def _decide_fallback(cname, channel_id, sp, text, corrections):
    """커스텀 두뇌(보존된 fallback): ReAct 다단계 루프 + (선택)Reflexion 자기검증.
    공식 hermes 두뇌 호출이 실패/타임아웃/파싱실패일 때만 쓰인다. 비용 통제:
    ReAct 단계 상한·도구 결과 절단·finalize 조기탈출·하드캡·Reflexion 최대 1회."""
    sysmsg = A.system_prompt(SPEC, COMMON_RULES, ROUTING,
                             memo=load_mem(), room_memo=load_room_mem(channel_id),
                             learn_note=load_learn_note(), react_steps=REACT_MAX_STEPS)
    convo = "\n".join(history(channel_id))
    user = f"[현재 방: {cname}]\n[최근 대화]\n{convo}\n\n[방금 들어온 메시지] {sp}: {text}"
    calls = [0]  # 이 decide 1회의 누적 LLM 호출 수(하드캡 백스톱용 — 가변 공유).
    d, why = _react_loop(sysmsg, user, corrections, channel_id, calls)
    if d is None:
        raise ValueError(f"decide: react 루프가 finalize 없이 종료됨({why})")
    bad = _validate(d)
    if bad:
        raise ValueError(f"decide: finalize 결정 스키마 위반({bad})")
    # 경량 self-check: 주입된 교정과 모순 신호.
    conflict = self_check(d, corrections)
    # Reflexion 자기검증 1패스(교정 모순이거나 reflexion on 일 때). 정확히 1회만.
    if conflict or REFLEXION_ON:
        if conflict:
            print(f"SELF-CHECK [{NAME}] 교정 모순 감지 → Reflexion 재확정: {conflict}")
        d2 = _reflexion_pass(sysmsg, user, d, corrections, channel_id, calls)
        if d2 is not d:
            d = d2
            conflict = self_check(d, corrections)
    if conflict:
        # Reflexion 후에도 모순이면 차단하지 않되 로그로 남긴다(silent 통과 방지).
        print(f"SELF-CHECK-WARN [{NAME}] Reflexion 후에도 교정 모순 잔존: {conflict}")
    return d


def decide(cname, channel_id, sp, text):
    """이 봇의 '처리 두뇌' 단일 진입점. 두뇌는 공식 Nous Hermes Agent(`hermes chat`)로
    통일한다. Mattermost 입출력·채널 라우팅·방 격리·메모리 3층은 이 함수 밖(run/저장부)이
    그대로 담당하고, 여기서는 '한 메시지 → 행동 결정 dict' 변환만 한다.

    경로:
      1) 공식 두뇌(hermes_brain.decide_via_official): 페르소나·공통규칙·라우팅·교정/학습/
         방메모·대화이력·출력계약을 합성한 query 를 공식 hermes 에 비대화식으로 1회 던져
         행동 결정 JSON 을 받는다(1메시지=1호출, --max-turns/timeout 으로 폭주·무한대기 차단).
      2) 실패/타임아웃/JSON 파싱 실패/스키마 위반 → 보존된 커스텀 ReAct 두뇌로 graceful
         fallback(_decide_fallback). 두뇌만 교체됐을 뿐 기존 안전망은 그대로 살아 있다.

    메모리 3층 주입 계약(공식·fallback 동일):
      - learn_note: 이 역할 전용 학습 노트(persistent, 교정 포함). 방과 무관하게 항상 주입.
      - room_memo : 현재 방(채널)의 공유 기억(롤링). 다른 방엔 주입 안 됨(방 격리).
      - memo      : 역할 개인 진행 메모(롤링).
    """
    corrections = load_corrections()
    # ── 1차: 공식 hermes 두뇌 ───────────────────────────────────────────────
    if B.USE_OFFICIAL_BRAIN and B.resolve_hermes_bin():
        convo = "\n".join(history(channel_id))
        try:
            d = B.decide_via_official(
                SPEC, COMMON_RULES, ROUTING, cname, convo, sp, text,
                memo=load_mem(), room_memo=load_room_mem(channel_id),
                learn_note=load_learn_note(), role=ROLE)
        except Exception as e:
            print(f"OFFICIAL-BRAIN-ERR [{NAME}] {cname}: {type(e).__name__}: {e} → fallback")
            d = None
        if d is not None:
            bad = _validate(d)
            if bad:
                print(f"OFFICIAL-BRAIN-BADSCHEMA [{NAME}] {cname}: {bad} → fallback")
            else:
                conflict = self_check(d, corrections)
                if conflict:
                    # 공식 두뇌가 교정과 모순된 결정을 냈으면 silent 통과시키지 않고 로그.
                    print(f"SELF-CHECK-WARN [{NAME}] 공식 두뇌 교정 모순 잔존: {conflict}")
                return d
        else:
            print(f"OFFICIAL-BRAIN-MISS [{NAME}] {cname}: 공식 두뇌 응답 없음/파싱 실패 → fallback")
    # ── 2차: 보존된 커스텀 ReAct 두뇌(fallback) ─────────────────────────────
    return _decide_fallback(cname, channel_id, sp, text, corrections)


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
    # open_timeout: MM 부재 시 connect 가 무한 대기하지 않게 상한을 둔다.
    # ping_interval/ping_timeout: keepalive ping 으로 좀비 연결(반쯤 끊긴 소켓)을 감지해
    # 끊어준다 → run_forever 의 백오프 재접속 루프가 작동할 수 있게 한다.
    async with websockets.connect("ws://localhost:8065/api/v4/websocket",
                                  open_timeout=20, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"seq": 1, "action": "authentication_challenge",
                                  "data": {"token": TOKEN}}))
        brain = "공식hermes" if B.is_official_available() else "커스텀(공식 미가용)"
        if B.is_official_available() and B.RECURSIVE_LEARNING:
            sess = f" 영속세션:{B.session_name(ROLE)} 홈:{B.role_home(ROLE)}"
        else:
            sess = " (재귀학습 OFF — 단발 무상태)"
        print(f"{NAME}({ROLE}) 가동[두뇌:{brain}]{sess} 공식모델:{B.OFFICIAL_MODEL} "
              f"fallback모델:{MODEL} 구독:{SUBS}")
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
                body = text.strip()
                if is_correction_feedback(body):
                    # 교정 피드백: 직전 봇 응답을 '잘못된출력'으로, 그 응답의 맥락을 '원지시'로
                    # 연결해 구조화 저장한다. 다음 회차부터 [반드시 지킬 교정]으로 우선 주입된다.
                    wrong_from_text, fix = parse_correction(body)
                    wrong = wrong_from_text or _last_response.get("message", "")
                    orig = _last_response.get("context", "")
                    if save_correction(orig, wrong, fix):
                        print(f"[{NAME}] 교정 학습 ← {LEARN_CHANNEL}: 교정={fix[:40]}")
                    else:
                        save_learn_note(f"{sp_name}: {body[:300]}")
                else:
                    save_learn_note(f"{sp_name}: {body[:300]}")
                    print(f"[{NAME}] 학습 노트 누적 ← {LEARN_CHANNEL}: {body[:40]}")
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
                        # 교정 연결용으로 직전 응답을 추적: 무엇(원지시 맥락)에 어떻게(본문) 답했는지.
                        _last_response["context"] = f"{cname}에서 '{speaker(p['user_id'])}: {text[:80]}'에 대한 응답"
                        _last_response["message"] = msg[:200]
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


async def run_forever():
    # MM 재접속 내성: MM 이 일시적으로 내려가거나(컨테이너 재시작/Colima 부팅 지연) WS 가
    # 끊겨도 프로세스를 죽이지 않고 지수 백오프로 재연결한다. launchd KeepAlive 와 충돌하지
    # 않는다 — 정상 운영 중에는 이 루프가 프로세스를 살려 두므로 KeepAlive 가 크래시-재시작
    # 루프를 돌 일이 없고, 진짜 프로세스 사망(OOM 등) 시에만 KeepAlive 가 받쳐 준다(이중 안전망).
    backoff = 2          # 첫 재시도 2s
    backoff_max = 60     # 상한 60s — MM 콜드부팅을 흡수하되 폭주하지 않는 간격
    while True:
        try:
            await run()
            # run() 이 예외 없이 반환 = 서버가 WS 를 정상 종료 → 재접속 시도(정상 흐름).
            print(f"[{NAME}] WS 종료됨 — 재접속 시도.")
            backoff = 2
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
            # 연결 실패/끊김류만 재시도 대상. (MM 부재·네트워크·핸드셰이크 실패 등)
            print(f"[{NAME}] MM 연결 실패/끊김: {type(e).__name__}: {e} — {backoff}s 후 재접속.")
        except Exception as e:
            # 예기치 못한 오류도 프로세스를 죽이지 않고 재시도(장님 운영 방지: 로그 남김).
            print(f"[{NAME}] 예기치 못한 오류: {type(e).__name__}: {e} — {backoff}s 후 재접속.")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, backoff_max)


if __name__ == "__main__":
    asyncio.run(run_forever())
