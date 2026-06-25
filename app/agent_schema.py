"""
에이전트 정의 로딩·검증 공용 모듈.

hermes_runtime.py(런타임 기동)와 lint_agents.py(머지 차단 린트)가 공유한다.
- agents/*.md frontmatter 파싱 + 필수필드/중복/채널참조 검증
- teams.json 로딩 + ROUTING 텍스트 동적 생성 (하드코딩 제거)
- agents/_shared/common_rules.md 상속 주입 헬퍼
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS_DIR = os.path.join(HERE, "agents")
SHARED_DIR = os.path.join(AGENTS_DIR, "_shared")
COMMON_RULES_PATH = os.path.join(SHARED_DIR, "common_rules.md")

# frontmatter 필수 필드 (린트가 누락 시 머지 차단).
REQUIRED_FIELDS = ("name", "username", "config", "primary", "channels")
LIST_FIELDS = ("aliases", "channels")

# 학습방 채널 ID 가 아직 실제 Mattermost ID 로 채워지지 않았음을 나타내는 placeholder 접두.
# channels.json 의 학습방 값이 이 접두로 시작하면 "미배선"으로 간주(린트 WARN, 런타임은 무해 동작).
LEARN_PLACEHOLDER_PREFIX = "TODO_"

# 학습 노트 안에서 '반드시 지킬 교정' 항목을 나타내는 라인 접두(단일 출처).
# hermes_runtime 의 저장 로직과 여기 system_prompt 의 강조 렌더가 같은 값을 공유한다.
CORRECTION_PREFIX = "[교정]"

# ── ReAct / tool use 단일 출처 ────────────────────────────────────────────────
# decide()가 단발 LLM 호출 → 다단계 에이전트 루프로 전환되면서, 매 반복의 출력 스키마와
# 안전 도구 화이트리스트를 여기(데이터/스키마 단일 출처)에 둔다. hermes_runtime 은 이 정의를
# 그대로 읽어 (a) 시스템 프롬프트에 ReAct 지침을 주입하고 (b) OpenRouter tools 파라미터를
# 구성하며 (c) 도구 이름 화이트리스트를 강제한다(저장 정의 ↔ 실행 강제 불일치 방지).

# 읽기형 안전 도구 이름(임의 셸·파일쓰기·외부망 없음). finalize 는 '최종 응답 확정' 특수 도구.
TOOL_GET_ROOM_MEMORY = "get_room_memory"     # 현재 방 공유 기억 조회(읽기)
TOOL_GET_LEARN_NOTE = "get_learn_note"       # 내 학습 노트(교정·노하우) 조회(읽기)
TOOL_GET_CHANNEL_HISTORY = "get_channel_history"  # 채널 최근 대화/현황 조회(읽기)
TOOL_FINALIZE = "finalize"                   # 최종 응답 확정(루프 즉시 탈출)

# 안전 도구 화이트리스트(이 집합 밖의 tool_call 은 런타임이 거부한다 — fail-closed).
SAFE_TOOL_NAMES = (
    TOOL_GET_ROOM_MEMORY,
    TOOL_GET_LEARN_NOTE,
    TOOL_GET_CHANNEL_HISTORY,
    TOOL_FINALIZE,
)

# finalize 가 확정해야 하는 최종 행동 결정 필드(기존 단발 decide() 출력 스키마와 동일 계약).
# ReAct 루프는 이 필드들을 finalize 도구의 arguments 로 받아 그대로 행동 결정 dict 로 쓴다.
FINALIZE_FIELDS = (
    "act", "target_channel", "message", "mentions", "ack", "ack_channel",
    "importance", "task_status", "memo", "reason", "learn_applied", "learn_basis",
)


def react_tool_specs(include_finalize=True):
    """OpenRouter(OpenAI 호환) function calling 의 tools 파라미터 스펙을 반환한다.
    읽기형 도구 + finalize(최종 응답 확정). 모든 도구는 부작용 없는 안전 화이트리스트.
    include_finalize=False 면 reflexion 등 finalize 강제 단계에서 읽기 도구를 숨길 수 있다."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": TOOL_GET_ROOM_MEMORY,
                "description": "현재 처리 중인 방(채널)의 공유 기억을 읽는다. 같은 방 다른 에이전트가 남긴 맥락 확인용. 부작용 없음(읽기 전용).",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": TOOL_GET_LEARN_NOTE,
                "description": "내 전용 학습 노트(과거 교정·노하우·정책)를 다시 읽는다. 같은 실수 반복 방지·정책 확인용. 부작용 없음(읽기 전용).",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": TOOL_GET_CHANNEL_HISTORY,
                "description": "현재 방의 최근 대화/현황을 더 많이 다시 읽는다(기본보다 깊게). 맥락이 부족할 때만 사용. 부작용 없음(읽기 전용).",
                "parameters": {
                    "type": "object",
                    "properties": {"n": {"type": "integer", "description": "가져올 최근 메시지 수(상한 적용됨)"}},
                    "additionalProperties": False,
                },
            },
        },
    ]
    if include_finalize:
        tools.append({
            "type": "function",
            "function": {
                "name": TOOL_FINALIZE,
                "description": "추론·도구 조회가 끝나 최종 행동을 확정할 때 호출한다. 이 도구를 호출하면 에이전트 루프가 즉시 끝난다. 송신하지 않을 거면 act=false 로 finalize 하라.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "act": {"type": "boolean", "description": "메시지를 송신할지(true) 침묵할지(false)"},
                        "target_channel": {"type": "string", "description": "송신 대상 채널명(채널 목록 안에서만)"},
                        "message": {"type": "string", "description": "송신 본문"},
                        "mentions": {"type": "array", "items": {"type": "string"}, "description": "멘션할 사람 이름 목록"},
                        "ack": {"type": "string", "description": "수신 확인/중간 보고 본문(선택)"},
                        "ack_channel": {"type": "string", "description": "ack 송신 채널명(선택)"},
                        "importance": {"type": "string", "enum": ["routine", "decision_needed", ""]},
                        "task_status": {"type": "string", "enum": ["open", "closed", ""]},
                        "memo": {"type": "string", "description": "이 방/개인 메모에 남길 한 줄(선택)"},
                        "reason": {"type": "string", "description": "이 결정의 한 줄 근거"},
                        "learn_applied": {"type": "boolean", "description": "[반드시 지킬 교정]을 반영했으면 true"},
                        "learn_basis": {"type": "string", "description": "어느 교정을 어떻게 지켰는지 한 줄"},
                    },
                    "required": ["act"],
                    "additionalProperties": False,
                },
            },
        })
    return tools


def react_system_addendum(max_steps):
    """system_prompt 에 덧붙일 ReAct(thought→self_critique→action) 작동 지침.
    매 반복마다 (1) thought 로 추론, (2) self_critique 로 자기비평, (3) 필요한 읽기 도구를
    호출하거나 충분하면 finalize 한다. 무거운 모델 교체 없이 같은 모델의 다단계 추론으로
    에이전트급 실행력을 흉내낸다. 비용 통제를 위해 반복 상한을 명시 주입한다."""
    return (
        "\n\n===== 에이전트 작동 방식 (ReAct 다단계 루프) =====\n"
        f"너는 단발 응답이 아니라 최대 {max_steps}단계까지 스스로 추론·도구조회·자기비평을 "
        "반복한 뒤 최종 행동을 확정하는 에이전트다. 각 단계에서 다음을 지켜라:\n"
        "1) thought: 지금 무엇을 판단해야 하는지 1~2문장으로 추론한다.\n"
        "2) self_critique: 직전 추론/맥락의 빈틈·위험·교정 위반 가능성을 스스로 비평한다.\n"
        "3) action: 맥락이 부족하면 읽기 도구(get_room_memory/get_learn_note/get_channel_history)를 "
        "호출해 사실을 더 모으고, 충분하면 finalize 도구로 최종 행동을 확정한다.\n"
        "규칙:\n"
        "- 같은 읽기 도구를 의미 없이 반복 호출하지 마라(이미 본 정보 재요청 금지).\n"
        "- 송신할 게 없으면 finalize(act=false)로 즉시 끝내라(침묵도 유효한 결정).\n"
        f"- 반드시 {max_steps}단계 안에 finalize 하라. 마지막 단계에서는 무조건 finalize 한다.\n"
        "- finalize 의 인자(act·target_channel·message·mentions·importance·task_status·memo·reason·"
        "learn_applied·learn_basis)가 곧 너의 최종 행동 결정이다. 채널·멘션은 라우팅 규칙을 따른다.\n"
        "- 도구 호출 없이 일반 텍스트만 길게 늘어놓지 마라. 행동은 finalize 로만 확정된다."
    )


# ── 공식 Nous Hermes Agent(`hermes chat`) 두뇌 통일 — 단일 출처 ─────────────────
# decide()의 처리 두뇌를 커스텀 urllib OpenRouter 직접호출에서 공식 hermes CLI 로 통일한다.
# 공식 hermes 는 system-prompt 주입용 별도 플래그가 없고(rules/SOUL/AGENTS 자동주입만 있음),
# 봇 머신의 무관한 SOUL.md/AGENTS.md 가 섞이면 페르소나가 오염되므로 `--ignore-rules` 로
# 기본 주입을 끄고, 우리의 페르소나·공통규칙·라우팅·교정·학습·방메모·대화이력·출력계약을
# 전부 하나의 query(-q) 로 합성해 주입한다(주입 단일 경로 → 계약 일관).
# 공식 두뇌는 자유서술 대신 '행동 결정 JSON' 한 덩어리만 출력하도록 강제한다(기존 finalize
# 스키마와 동일 계약 → 송신부·검증부를 그대로 재사용, 두뇌만 교체).

def finalize_schema_instruction():
    """공식 hermes 두뇌가 반드시 따라야 할 출력 계약. 자유서술·도구·설명 없이
    행동 결정 JSON 객체 하나만 출력하게 강제한다(기존 FINALIZE_FIELDS 스키마와 동일).
    이 JSON 이 곧 송신/검증부가 소비하는 행동 결정이다(두뇌 교체 후에도 계약 불변)."""
    return (
        "\n\n===== 출력 계약 (반드시 지킬 것) =====\n"
        "너는 위 맥락을 바탕으로 '이 메시지에 대해 무엇을 할지'를 결정해, "
        "아래 필드를 가진 JSON 객체 **하나만** 출력한다. 설명·인사·코드펜스·여는 말 금지. "
        "오직 JSON 한 덩어리만 출력하라.\n"
        "{\n"
        '  "act": true|false,            // 메시지를 송신할지(true)/침묵할지(false)\n'
        '  "target_channel": "채널명",   // 송신 대상 채널(아래 채널 목록 안에서만). 침묵이면 빈 문자열\n'
        '  "message": "송신 본문",       // act=true 일 때 보낼 내용\n'
        '  "mentions": ["사람이름"],     // 멘션할 사람 이름 목록(없으면 빈 배열)\n'
        '  "ack": "수신확인 본문",        // 중간보고/수신확인(선택, 없으면 빈 문자열)\n'
        '  "ack_channel": "채널명",      // ack 송신 채널(선택)\n'
        '  "importance": "routine"|"decision_needed"|"",\n'
        '  "task_status": "open"|"closed"|"",\n'
        '  "memo": "이 방/개인 메모 한 줄", // 남길 메모(선택)\n'
        '  "reason": "이 결정의 한 줄 근거",\n'
        '  "learn_applied": true|false,  // [반드시 지킬 교정]을 반영했으면 true\n'
        '  "learn_basis": "어느 교정을 어떻게 지켰는지 한 줄"\n'
        "}\n"
        "규칙:\n"
        "- 송신할 게 없으면 act=false 로 침묵하라(침묵도 유효한 결정).\n"
        "- 채널·멘션은 위 라우팅 규칙을 반드시 따른다(권한 밖 채널 송신 금지).\n"
        "- [반드시 지킬 교정]이 있으면 절대 위반하지 말고, learn_applied=true 로 표기하라.\n"
        "- 반드시 JSON 객체 하나만. 그 앞뒤로 어떤 글자도 출력하지 마라."
    )


def official_brain_query(spec, common_rules, routing, cname, convo, speaker_name,
                         text, memo="", room_memo="", learn_note=""):
    """공식 hermes 두뇌(`hermes chat -q`)에 통째로 줄 단일 query 를 합성한다.
    기존 system_prompt(페르소나+공통규칙+라우팅+메모리 3층) 조립을 그대로 재사용하되,
    공식 CLI 에는 system 슬롯이 없으므로 시스템 지침 + 현재 방/대화/메시지 + 출력계약을
    하나의 user query 문자열로 이어 붙인다. react_steps=0 으로 ReAct 지침은 넣지 않는다
    (공식 두뇌는 자체 ReAct 루프를 가지므로 우리 루프 지침은 불필요·충돌)."""
    sysmsg = system_prompt(spec, common_rules, routing, memo=memo,
                           room_memo=room_memo, learn_note=learn_note, react_steps=0)
    user_block = (
        "\n\n===== 지금 처리할 상황 =====\n"
        f"[현재 방: {cname}]\n[최근 대화]\n{convo}\n\n"
        f"[방금 들어온 메시지] {speaker_name}: {text}"
    )
    return sysmsg + user_block + finalize_schema_instruction()


def _strip_fence(s):
    """LLM 출력에서 코드펜스를 벗기고 첫 { ~ 마지막 } 사이만 추출(JSON 강건 파싱용).
    hermes_runtime._strip_fence / mm_client.strip_fence 와 동일 로직(여기 자체 보유 →
    공식 두뇌 출력 파싱이 외부 모듈 의존 없이 닫혀 동작)."""
    s = (s or "").strip()
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


def parse_official_brain_output(raw):
    """공식 hermes CLI(-Q) stdout 에서 행동 결정 JSON 을 추출해 dict 로 반환.
    -Q 모드 출력은 'session_id: ...' 메타 라인 + 응답 본문이 섞여 나오므로,
    session_id/usage 류 메타 라인을 걷어내고 strip_fence 로 첫 { ~ 마지막 } 만 파싱한다.
    파싱 실패 시 None(→ 호출부가 fallback 으로 전환)."""
    if not raw:
        return None
    lines = []
    for ln in raw.splitlines():
        s = ln.strip()
        # CLI 메타 라인 제거(응답 본문이 아님).
        if s.startswith("session_id:") or s.startswith("usage:") or s.startswith("cost:"):
            continue
        lines.append(ln)
    body = _strip_fence("\n".join(lines))
    if not body:
        return None
    try:
        d = json.loads(body)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def parse_md(path):
    """agents/*.md 한 파일을 {필드..., prompt} dict로 파싱."""
    txt = open(path, encoding="utf-8").read()
    parts = txt.split("---", 2)
    fm, body = (parts[1], parts[2]) if len(parts) >= 3 else ("", txt)
    meta = {}
    for line in fm.strip().splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        meta[k] = [x.strip() for x in v.split(",") if x.strip()] if k in LIST_FIELDS else v
    meta["prompt"] = body.strip()
    return meta


def _agent_files():
    return sorted(
        f for f in os.listdir(AGENTS_DIR)
        if f.endswith(".md") and not f.startswith("_")
    )


def load_roles():
    """role(파일명 stem) -> 파싱 dict. _로 시작하는 파일(_shared 등)은 제외."""
    return {f[:-3]: parse_md(os.path.join(AGENTS_DIR, f)) for f in _agent_files()}


def load_common_rules():
    """공유 규칙 본문. 없으면 빈 문자열(런타임이 죽지 않도록 graceful)."""
    try:
        return open(COMMON_RULES_PATH, encoding="utf-8").read().strip()
    except FileNotFoundError:
        return ""


def load_teams():
    return json.load(open(os.path.join(HERE, "teams.json"), encoding="utf-8"))


def load_channels():
    return json.load(open(os.path.join(HERE, "channels.json"), encoding="utf-8"))


def load_learning_rooms(teams):
    """teams.json 의 learning_rooms 목록(없으면 빈 리스트)."""
    return teams.get("learning_rooms", [])


def learning_room_for_role(teams, role):
    """주어진 role(파일명 stem)의 전용 학습방 정의를 반환. 없으면 None.
    런타임이 '이 역할의 학습방 채널은 무엇인가'를 알아내는 단일 출처."""
    for room in load_learning_rooms(teams):
        if room.get("owner") == role:
            return room
    return None


def validate_roles(roles, valid_channels):
    """필수필드 누락·username 중복·미정의 채널 참조를 검사. 오류 문자열 리스트 반환(빈=정상)."""
    errors = []
    seen_users = {}
    for role, meta in sorted(roles.items()):
        for field in REQUIRED_FIELDS:
            if not meta.get(field):
                errors.append(f"[{role}] 필수 필드 누락: {field}")
        if not meta.get("prompt"):
            errors.append(f"[{role}] 페르소나 본문(prompt)이 비어 있음")
        user = meta.get("username")
        if user:
            if user in seen_users:
                errors.append(f"[{role}] username '{user}' 중복 (이미 {seen_users[user]})")
            else:
                seen_users[user] = role
        for ch in meta.get("channels", []):
            if ch not in valid_channels:
                errors.append(f"[{role}] 미정의 채널 참조: '{ch}' (channels.json에 없음)")
        primary = meta.get("primary")
        if primary and primary not in valid_channels:
            errors.append(f"[{role}] primary 채널 미정의: '{primary}'")
    return errors


def validate_teams(teams, valid_channels):
    """teams.json의 채널 참조 정합성 검사. 오류 리스트 반환."""
    errors = []
    orch = teams.get("orchestrator", {})
    bc = orch.get("briefing_channel")
    if bc and bc not in valid_channels:
        errors.append(f"[orchestrator] briefing_channel 미정의: '{bc}'")
    ids = set()
    for t in teams.get("teams", []):
        tid = t.get("id", "?")
        if tid in ids:
            errors.append(f"[teams] 팀 id 중복: '{tid}'")
        ids.add(tid)
        for key in ("team_channel", "report_channel"):
            ch = t.get(key)
            if not ch:
                errors.append(f"[teams:{tid}] 필수 필드 누락: {key}")
            elif ch not in valid_channels:
                errors.append(f"[teams:{tid}] {key} 미정의 채널: '{ch}'")
        if not t.get("agent"):
            errors.append(f"[teams:{tid}] 필수 필드 누락: agent")
    # collab_rooms(여러 에이전트 공동 참여 작업방) 정합성 검사
    room_ids = set()
    for room in teams.get("collab_rooms", []):
        rid = room.get("id", "?")
        if rid in room_ids:
            errors.append(f"[collab_rooms] room id 중복: '{rid}'")
        room_ids.add(rid)
        ch = room.get("channel")
        if not ch:
            errors.append(f"[collab_rooms:{rid}] 필수 필드 누락: channel")
        elif ch not in valid_channels:
            errors.append(f"[collab_rooms:{rid}] channel 미정의: '{ch}'")
        parts = room.get("participants") or []
        if not parts:
            errors.append(f"[collab_rooms:{rid}] 필수 필드 누락: participants")
        lead = room.get("lead")
        if not lead:
            errors.append(f"[collab_rooms:{rid}] 필수 필드 누락: lead")
        elif lead not in parts:
            errors.append(f"[collab_rooms:{rid}] lead '{lead}'가 participants에 없음")
        deliver = room.get("deliver_to")
        if deliver and deliver not in valid_channels:
            errors.append(f"[collab_rooms:{rid}] deliver_to 미정의 채널: '{deliver}'")
    # learning_rooms(역할별 전용 학습방) 정합성 검사
    learn_ids = set()
    learn_owners = set()
    learn_channels = set()
    for room in teams.get("learning_rooms", []):
        rid = room.get("id", "?")
        if rid in learn_ids:
            errors.append(f"[learning_rooms] room id 중복: '{rid}'")
        learn_ids.add(rid)
        owner = room.get("owner")
        if not owner:
            errors.append(f"[learning_rooms:{rid}] 필수 필드 누락: owner")
        elif owner in learn_owners:
            errors.append(f"[learning_rooms:{rid}] owner '{owner}' 중복 (역할당 학습방 1개)")
        else:
            learn_owners.add(owner)
        ch = room.get("channel")
        if not ch:
            errors.append(f"[learning_rooms:{rid}] 필수 필드 누락: channel")
        elif ch not in valid_channels:
            errors.append(f"[learning_rooms:{rid}] channel 미정의: '{ch}' (channels.json에 없음)")
        elif ch in learn_channels:
            errors.append(f"[learning_rooms:{rid}] channel '{ch}' 중복 (학습방끼리 채널 공유 금지)")
        else:
            learn_channels.add(ch)
    return errors


def build_routing(teams):
    """teams.json 데이터로 ROUTING 텍스트를 동적 생성. (이전 하드코딩 ROUTING의 라우팅 흐름 부분을 대체)"""
    orch = teams.get("orchestrator", {})
    tlist = teams.get("teams", [])
    rooms = teams.get("collab_rooms", [])
    learn_rooms = teams.get("learning_rooms", [])
    orch_name = orch.get("role", "박민철")
    brief = orch.get("briefing_channel", "")
    principal = orch.get("principal", "CEO")

    lines = ["[채널과 업무 흐름]"]
    team_chs = " / ".join(t["team_channel"] for t in tlist)
    report_chs = " / ".join(t["report_channel"] for t in tlist)
    lines.append(
        f"- {team_chs}: 팀원(사람)이 보고를 올리는 방. 팀 에이전트는 정리해 자기 팀 보고라인에 "
        f"{orch_name}을 멘션해 상신한다. 상위 지시의 1차 수행 결과도 이 방에서 팀원에게 전달."
    )
    lines.append(
        f"- {report_chs}: 팀 에이전트와 {orch_name}이 보고·피드백·지시를 주고받는 방."
    )
    if brief:
        lines.append(f"- {brief}: {orch_name}과 {principal}(사람)만의 방. {orch_name}이 상신하고 {principal} 지시를 받는다.")
    for room in rooms:
        names = "·".join(room.get("participants", []))
        lines.append(
            f"- {room['channel']}: {names}이 함께 참여하는 협업 작업방. "
            f"{principal}이 정책·기획 같은 범부서 과제를 게시하면 각자 전문성으로 기여하고, "
            f"리드 {room.get('lead', orch_name)}이 취합·정리해 '{room.get('deliver_to', brief)}'에 제출한다."
        )
    # 역할별 전용 학습방: 채널 흐름 안내(owner role → 사람 이름 매핑)
    name_by_role = {"orchestrator": orch_name}
    for t in tlist:
        if t.get("id"):
            name_by_role[t["id"]] = t.get("agent", t["id"])
    for lr in learn_rooms:
        owner = lr.get("owner", "")
        owner_name = name_by_role.get(owner, owner)
        lines.append(
            f"- {lr['channel']}: {owner_name} 전용 학습방. 여기 올라온 교정·노하우·정책은 "
            f"휘발 없이 '학습 노트'에 영구 누적되어 {owner_name}이 매 응답에 자동 참고한다. "
            f"(다른 역할의 학습 노트는 절대 주입되지 않는다 — 방 격리)"
        )

    lines.append("")
    lines.append("[역할별 행동]")
    for t in tlist:
        lines.append(
            f"- {t['agent']}({t['label']}): {t['team_channel']} 사람 보고 → '{t['report_channel']}'에 "
            f"{t['escalate_to']} 멘션 상신. {t['escalate_to']} 피드백 → 보완 회신. "
            f"{t['escalate_to']} 지시({principal}발) → 1차 수행 후 {t['team_channel']}에서 팀원에게 전달, closed."
        )
    # orchestrator(상향·하향) 행동을 팀 데이터로부터 동적 구성
    up_map = ", ".join(f"{t['label']} 건은 '{t['report_channel']}' {t['agent']}" for t in tlist)
    lines.append(
        f"- {orch_name}: 각 보고라인의 팀 보고를 받아 → 보완 필요하면 팀 에이전트 멘션해 반려, "
        f"충분하면 importance 판단(routine=간단 정리, decision_needed='{brief}' 상신). "
        f"{brief}의 {principal} 지시 → {up_map} 멘션해 분배. "
        "★팀 상신을 단어만 바꿔 복붙하지 마라(공통규칙 4 가공 원칙 준수)."
    )
    # 협업 작업방 행동 프로토콜(데이터 주도) — 참여 에이전트별 기여 + 리드 취합·제출
    for room in rooms:
        parts = room.get("participants", [])
        lead = room.get("lead", orch_name)
        deliver = room.get("deliver_to", brief)
        members = "·".join(parts)
        non_lead = [p for p in parts if p != lead]
        contrib = ", ".join(non_lead) if non_lead else members
        lines.append("")
        lines.append(f"[협업 작업방: {room['channel']}]")
        lines.append(
            f"- {principal}이 '{room['channel']}'에 정책·기획 과제를 게시하면 {members} 전원이 같은 방에서 협업한다. "
            f"각자 자기 전문 영역의 분석·근거·초안을 같은 방에 게시(target_channel='{room['channel']}')하고, "
            "다른 참여자 기여를 읽고 자기 몫을 보탠다(중복·복붙 금지, 공통규칙 4)."
        )
        lines.append(
            f"- 기여자({contrib}): 자기 차례(자기 전문성이 필요한 부분)일 때만 act=true로 '{room['channel']}'에 기여를 올린다. "
            f"리드 {lead}이 추가 입력을 멘션 요청하면 보완 회신한다."
        )
        lines.append(
            f"- 리드 {lead}: 참여자 기여가 모이면 하나의 결과물로 취합·구조화해 '{deliver}'에 {principal}에게 제출하고 task_status=closed. "
            f"기여가 부족하면 해당 참여자를 '{room['channel']}'에서 멘션해 콕 집어 보완 요청한다. "
            "단순 취합이 아니라 우선순위·리스크·권고를 더해 의사결정 가능한 형태로 가공한다."
        )
    return "\n".join(lines)


def reflexion_prompt(corrections):
    """Reflexion 자기검증 패스용 지침. 확정 직전 같은 모델이 (a)교정 위반 (b)지시 충족
    (c)근거 충분성을 점검해 finalize 를 재확정한다. 무거운 다중 모델 호출 없이 경량 1패스."""
    base = (
        "\n\n===== 최종 점검(Reflexion) =====\n"
        "방금 정한 최종 행동을 확정하기 전에 스스로 점검하라:\n"
        "(a) 교정 위반: [반드시 지킬 교정]을 어긴 부분이 있는가?\n"
        "(b) 지시 충족: 방금 들어온 메시지가 요구한 것을 실제로 충족했는가?\n"
        "(c) 근거 충분: 송신할 내용에 빠진 사실·맥락은 없는가?\n"
        "문제가 있으면 수정해서, 없으면 그대로, finalize 도구로 최종 행동을 다시 확정하라. "
        "반드시 finalize 도구만 호출한다(다른 도구·일반 텍스트 금지)."
    )
    if corrections:
        base += "\n특히 아래 교정을 다시 확인하라:\n" + "\n".join(corrections)
    return base


def system_prompt(spec, common_rules, routing, memo="", room_memo="", learn_note="",
                  react_steps=0):
    """에이전트 시스템 프롬프트 조립: 페르소나(고유) + 공유규칙(상속) + 라우팅(데이터) + 메모.

    메모리 3층 구조:
      - learn_note: 이 역할 전용 '학습 노트'(persistent, 영구 누적·휘발 안 됨).
                    학습방에 쌓인 교정·노하우·정책을 매 회차 [팀 학습 노트]로 주입.
                    역할별 1개 파일이라 타 역할 학습노트는 절대 주입되지 않는다(방 격리).
      - room_memo: 현재 방(채널)의 공유 기억(롤링 6줄, 휘발). 같은 방의 모든 에이전트가 공유.
                   다른 방 처리 시엔 주입되지 않아 방 경계로 정보 누출이 차단된다.
      - memo: 역할(에이전트) 개인의 진행 메모(롤링 6줄, 휘발). 방과 무관하게 유지.
    기존 호출과의 하위 호환을 위해 room_memo·learn_note는 기본값 빈 문자열.
    """
    parts = [spec["prompt"]]
    if common_rules:
        parts.append("\n\n===== 전 에이전트 공통 규칙 (상속) =====\n" + common_rules)
    if learn_note:
        # 학습 노트를 '교정'과 '일반'으로 분리 렌더한다. 교정은 같은 실수 반복을 막는
        # 최우선 지침이므로 [반드시 지킬 교정] 강조 섹션으로 먼저·따로 주입하고,
        # 나머지 노하우·정책은 [팀 학습 노트]로 일반 주입한다(우선순위 시각적 구분).
        lines = [x for x in learn_note.split("\n") if x.strip()]
        corrections = [x for x in lines if x.strip().startswith(CORRECTION_PREFIX)]
        general = [x for x in lines if not x.strip().startswith(CORRECTION_PREFIX)]
        if corrections:
            parts.append(
                "\n\n[반드시 지킬 교정] (과거 같은 실수로 교정받은 항목 — 절대 반복 금지, 최우선 준수)\n"
                + "\n".join(corrections)
            )
        if general:
            parts.append(
                "\n\n[팀 학습 노트] (내 학습방에 영구 누적된 노하우·정책 — 매 판단에 반영하라)\n"
                + "\n".join(general)
            )
        # self-check: 모델이 응답에 교정 반영 여부를 스스로 표기하게 한다(경량 1패스 검증 근거).
        if corrections:
            parts.append(
                "\n\n[자기점검] 출력 JSON에 \"learn_applied\"(bool: 위 [반드시 지킬 교정]을 "
                "이번 응답에 반영했으면 true)와 \"learn_basis\"(어느 교정을 어떻게 지켰는지 한 줄) "
                "필드를 추가하라. 교정과 모순되는 응답은 금지다."
            )
    if room_memo:
        parts.append("\n\n[이 방의 공유 기억]\n" + room_memo)
    if memo:
        parts.append("\n\n[내 개인 메모]\n" + memo)
    parts.append("\n\n===== 라우팅 (teams.json 기반) =====\n" + routing)
    parts.append(f"\n[너] 이름:{spec['name']} / 주 담당 방:{spec['primary']}")
    # ReAct 다단계 작동 지침은 라우팅·역할 정보 뒤에 마지막으로 주입한다(작동 방식이
    # 가장 최근 맥락으로 모델 머리에 남도록). react_steps<=0 이면 단발 모드(하위 호환).
    if react_steps and react_steps > 0:
        parts.append(react_system_addendum(react_steps))
    return "".join(parts)
