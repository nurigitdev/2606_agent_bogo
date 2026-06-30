"""
ReAct 다단계 에이전트 루프 + tool use(function calling) + Reflexion 자기검증 단위 테스트.
(네트워크/Mattermost/OpenRouter 불필요 — LLM 실호출은 전부 모킹)

검증:
  1) 반복 상한 — REACT_MAX_STEPS 안에서만 LLM 이 호출되고, finalize 없이도 하드캡을 넘지 않는다.
  2) 도구 화이트리스트 — 화이트리스트 밖 도구는 fail-closed 로 거부(임의 셸/파일쓰기/외부망 없음).
  3) finalize 조기탈출 — 중간에 finalize 하면 남은 단계를 돌지 않고 즉시 결정 반환.
  4) 도구 결과 환류 — 읽기 도구 호출 결과가 observation 으로 다음 호출 messages 에 들어간다.
  5) Reflexion 1회 제한 — 자기검증 패스는 정확히 1회만 추가 호출, 결정 재확정.
  6) 교정위반 감지 → Reflexion 재확정 — 모순 결정이 들어오면 Reflexion 으로 교정된다.
  7) system_prompt ReAct 주입 + tool spec + history 상한.

실행: python3 test_agent_loop.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = ["bogo_runtime.py", "orchestrator"]

import bogo_runtime as H  # noqa: E402
import bogo_brain as B  # noqa: E402
import agent_schema as A  # noqa: E402

# 이 파일은 두뇌 교체 후 '보존된 커스텀 ReAct 두뇌(fallback 경로)'를 검증한다.
# decide()는 공식 외부 CLI 두뇌를 먼저 시도하므로, 여기서는 공식 두뇌를 끄고(USE_OFFICIAL_BRAIN
# =False) fallback(_decide_fallback = ReAct+Reflexion)만 타도록 강제한다. 이로써 기존
# ReAct/도구/Reflexion 안전망이 fallback 으로서 여전히 정상 동작함을 회귀 검증한다.
B.USE_OFFICIAL_BRAIN = False


def _msg_with_tool_calls(calls):
    """OpenRouter 형식의 assistant 메시지(tool_calls 포함) 모킹 헬퍼.
    calls = [(name, args_dict), ...]. arguments 는 실제 API 처럼 JSON 문자열로 직렬화."""
    return {
        "content": "",
        "tool_calls": [
            {"id": f"call_{i}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
            for i, (name, args) in enumerate(calls)
        ],
    }


class _FakeLLM:
    """call_llm_msg 를 대체하는 스크립트형 모킹. 호출될 때마다 미리 정한 응답을 순서대로 반환.
    call_count 로 실제 LLM 호출 횟수를 검증한다(반복 상한·Reflexion 1회 검증의 근거)."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.call_count = 0
        self.last_messages = None

    def __call__(self, messages, model, tools=None, tool_choice=None, max_tokens=1500):
        self.call_count += 1
        self.last_messages = messages
        if self.scripted:
            return self.scripted.pop(0)
        # 스크립트 소진 시 안전하게 finalize(테스트 무한루프 방지)
        return _msg_with_tool_calls([(A.TOOL_FINALIZE, {"act": False, "reason": "스크립트소진"})])


def _patch_history(monkey_lines="사람: 안녕"):
    """history() 가 네트워크를 타지 않도록 모킹."""
    H.history = lambda channel_id, n=12: [monkey_lines]


def _base_decide(fake, channel_id="ch1"):
    """공통 셋업: history·LLM 모킹 후 decide 실행. (corrections 없는 기본 상태)"""
    _patch_history()
    H.call_llm_msg = fake
    # 학습노트/방메모 파일이 없어도 안전(load_* 는 빈 문자열 반환)
    return H.decide("테스트방", channel_id, "사람", "보고 처리해줘")


def test_react_max_steps_cap():
    """반복 상한: 모델이 계속 읽기 도구만 호출해도 REACT_MAX_STEPS 안에서 finalize 가 강제되고
    LLM 호출이 하드캡을 넘지 않는다(비용 폭주 방지의 핵심 단언)."""
    # 매번 읽기 도구만 호출 → 마지막 단계에서 finalize 강제됨.
    # 마지막(force_final) 단계에서는 어차피 finalize tool_choice 가 강제되지만,
    # 모킹은 그 단계에 finalize 를 반환하도록 둔다.
    script = []
    for _ in range(H.REACT_MAX_STEPS - 1):
        script.append(_msg_with_tool_calls([(A.TOOL_GET_ROOM_MEMORY, {})]))
    script.append(_msg_with_tool_calls([(A.TOOL_FINALIZE, {"act": False, "reason": "끝"})]))
    fake = _FakeLLM(script)
    # Reflexion 이 켜져 있으면 +1 호출. 검증을 명확히 하기 위해 act=false 라도 Reflexion 은 돈다.
    old_reflex = H.REFLEXION_ON
    H.REFLEXION_ON = False  # 이 테스트는 순수 ReAct 상한만 검증
    try:
        d = _base_decide(fake)
    finally:
        H.REFLEXION_ON = old_reflex
    assert d["act"] is False, f"finalize 결정이 반영돼야 함: {d}"
    # ReAct 호출 수는 정확히 REACT_MAX_STEPS 이하여야 한다.
    assert fake.call_count <= H.REACT_MAX_STEPS, \
        f"ReAct 호출이 상한 초과: {fake.call_count} > {H.REACT_MAX_STEPS}"
    # 하드캡도 절대 넘지 않는다.
    assert fake.call_count <= H.LLM_CALL_HARD_CAP, "하드캡 초과"
    print(f"[PASS] ReAct 반복 상한 준수(호출 {fake.call_count} ≤ {H.REACT_MAX_STEPS}) 통과")


def test_finalize_early_exit():
    """finalize 조기탈출: 첫 단계에서 finalize 하면 남은 단계를 돌지 않고 1회 호출로 끝난다."""
    script = [_msg_with_tool_calls([(A.TOOL_FINALIZE,
                                     {"act": True, "target_channel": "CEO브리핑",
                                      "message": "처리 완료", "reason": "즉시확정"})])]
    fake = _FakeLLM(script)
    old_reflex = H.REFLEXION_ON
    H.REFLEXION_ON = False
    try:
        d = _base_decide(fake)
    finally:
        H.REFLEXION_ON = old_reflex
    assert d["act"] is True and d["message"] == "처리 완료", f"finalize 결정 반영 실패: {d}"
    assert fake.call_count == 1, f"조기탈출 실패 — 1회만 호출돼야 함: {fake.call_count}회"
    print("[PASS] finalize 조기탈출(1회 호출로 즉시 종료) 통과")


def test_tool_whitelist_fail_closed():
    """도구 화이트리스트: 화이트리스트 밖 도구는 거부되고, 임의 셸/파일쓰기/외부망 도구가 없다."""
    # 직접 디스패처 검증 — 허용되지 않은 이름은 [거부] 반환.
    assert H.run_tool("rm_rf", {}, "ch1").startswith("[거부]"), "임의 도구가 거부되지 않음"
    assert H.run_tool("exec_shell", {"cmd": "ls"}, "ch1").startswith("[거부]"), "셸 도구가 거부되지 않음"
    # 화이트리스트 안 읽기 도구는 정상 동작(읽기 전용, 부작용 없음).
    _patch_history()
    assert "거부" not in H.run_tool(A.TOOL_GET_CHANNEL_HISTORY, {"n": 3}, "ch1"), "정상 읽기 도구가 거부됨"
    # tool spec 에는 finalize + 읽기 도구만 있고, 위험 도구 이름은 전혀 없다.
    names = {t["function"]["name"] for t in A.react_tool_specs()}
    assert names == set(A.SAFE_TOOL_NAMES), f"tool spec 이 화이트리스트와 불일치: {names}"
    assert not any("shell" in n or "exec" in n or "write" in n or "file" in n for n in names), \
        "위험 도구 이름이 화이트리스트에 존재"
    print("[PASS] 도구 화이트리스트 fail-closed(셸/쓰기/외부망 없음) 통과")


def test_tool_observation_feedback():
    """도구 결과 환류: 읽기 도구 호출 결과가 observation(role=tool)으로 다음 messages 에 들어간다."""
    captured = {}

    class _Capture(_FakeLLM):
        def __call__(self, messages, model, tools=None, tool_choice=None, max_tokens=1500):
            # 2번째 호출 시점의 messages 를 캡처(첫 도구 결과가 환류됐는지 확인).
            if self.call_count == 1:
                captured["messages"] = list(messages)
            return super().__call__(messages, model, tools, tool_choice, max_tokens)

    # 방 메모리에 식별 가능한 토큰을 심어 둔다 → 도구가 이걸 읽어 observation 으로 환류해야 함.
    orig_room_mem = H.load_room_mem
    H.load_room_mem = lambda channel_id: "방기억토큰_ABC"
    script = [
        _msg_with_tool_calls([(A.TOOL_GET_ROOM_MEMORY, {})]),
        _msg_with_tool_calls([(A.TOOL_FINALIZE, {"act": False, "reason": "확인끝"})]),
    ]
    fake = _Capture(script)
    old_reflex = H.REFLEXION_ON
    H.REFLEXION_ON = False
    try:
        _base_decide(fake)
    finally:
        H.REFLEXION_ON = old_reflex
        H.load_room_mem = orig_room_mem
    msgs = captured.get("messages", [])
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert tool_msgs, "도구 observation(role=tool)이 환류되지 않음"
    assert any("방기억토큰_ABC" in (m.get("content") or "") for m in tool_msgs), \
        f"도구 결과가 observation 으로 환류되지 않음: {tool_msgs}"
    print("[PASS] 도구 결과 observation 환류 통과")


def test_reflexion_single_pass():
    """Reflexion 1회 제한: reflexion on 일 때 자기검증 패스는 정확히 1회만 추가 호출된다."""
    # ReAct 1회(finalize) + Reflexion 1회 = 총 2회.
    script = [
        _msg_with_tool_calls([(A.TOOL_FINALIZE,
                               {"act": True, "target_channel": "CEO브리핑",
                                "message": "초안", "reason": "1차"})]),
        _msg_with_tool_calls([(A.TOOL_FINALIZE,
                               {"act": True, "target_channel": "CEO브리핑",
                                "message": "최종 점검본", "reason": "reflexion"})]),
    ]
    fake = _FakeLLM(script)
    old_reflex = H.REFLEXION_ON
    H.REFLEXION_ON = True
    try:
        d = _base_decide(fake)
    finally:
        H.REFLEXION_ON = old_reflex
    assert fake.call_count == 2, f"Reflexion 은 정확히 1회 추가 호출(총 2회)여야 함: {fake.call_count}회"
    assert d["message"] == "최종 점검본", f"Reflexion 재확정 결과가 반영돼야 함: {d}"
    print("[PASS] Reflexion 정확히 1회 패스 + 재확정 통과")


def test_reflexion_corrects_violation():
    """교정위반 감지: 교정된 '잘못된출력'이 그대로 들어오면 self_check 가 잡고 Reflexion 으로 교정된다."""
    # 교정: '12월31일' 이 잘못된출력 → 응답에 다시 등장하면 모순.
    orig_load_corrections = H.load_corrections  # [회귀수정] 원본 보존 후 finally 에서 복원
    H.load_corrections = lambda role=None: [
        H.CORRECTION_PREFIX + " 잘못된출력=12월31일 ||| 교정=종료 6개월 전"]
    script = [
        # ReAct: 모순된 결정(잘못된출력 재등장)을 finalize.
        _msg_with_tool_calls([(A.TOOL_FINALIZE,
                               {"act": True, "target_channel": "CEO브리핑",
                                "message": "연차 기한은 12월31일입니다", "reason": "오답"})]),
        # Reflexion: 교정된 올바른 답으로 재확정.
        _msg_with_tool_calls([(A.TOOL_FINALIZE,
                               {"act": True, "target_channel": "CEO브리핑",
                                "message": "연차 기한은 종료 6개월 전입니다",
                                "learn_applied": True, "reason": "교정반영"})]),
    ]
    fake = _FakeLLM(script)
    old_reflex = H.REFLEXION_ON
    H.REFLEXION_ON = True
    try:
        d = _base_decide(fake)
    finally:
        H.REFLEXION_ON = old_reflex
        # [회귀수정] 빈 lambda 로 덮으면 후속 테스트의 load_corrections 가 영구 오염된다.
        # 원본 함수를 복원해 전역 상태 누출을 막는다(test 간 격리).
        H.load_corrections = orig_load_corrections
    assert "12월31일" not in d["message"], f"교정 위반이 Reflexion 으로 제거돼야 함: {d}"
    assert "6개월" in d["message"], f"교정된 올바른 답으로 재확정돼야 함: {d}"
    assert fake.call_count == 2, f"Reflexion 1회만(총 2회) 호출돼야 함: {fake.call_count}회"
    print("[PASS] 교정위반 감지 → Reflexion 재확정 통과")


def test_history_tool_cap():
    """get_channel_history 도구는 요청 n 이 커도 TOOL_HISTORY_MAX 로 상한이 강제된다."""
    captured = {}
    H.history = lambda channel_id, n=12: captured.update(n=n) or ["줄"]
    H.run_tool(A.TOOL_GET_CHANNEL_HISTORY, {"n": 99999}, "ch1")
    assert captured["n"] == H.TOOL_HISTORY_MAX, f"history n 상한 강제 실패: {captured['n']}"
    print(f"[PASS] history 도구 n 상한 강제(≤{H.TOOL_HISTORY_MAX}) 통과")


def test_system_prompt_react_injection():
    """system_prompt 에 react_steps>0 이면 ReAct 작동 지침이 주입되고, 0이면(단발) 미주입."""
    spec = {"prompt": "p", "name": "n", "primary": "방"}
    sp_on = A.system_prompt(spec, "공통", "라우팅", react_steps=4)
    assert "ReAct 다단계 루프" in sp_on and "finalize" in sp_on, "ReAct 지침 주입 누락"
    assert "최대 4단계" in sp_on, "반복 상한 수치 주입 누락"
    sp_off = A.system_prompt(spec, "공통", "라우팅")  # 기본 react_steps=0
    assert "ReAct 다단계 루프" not in sp_off, "단발 모드인데 ReAct 지침이 주입됨(하위 호환 위반)"
    print("[PASS] system_prompt ReAct 주입 + 단발 하위 호환 통과")


def test_autonomous_completion_in_finalize_contract():
    """자율 완결(공통규칙 §2-1) 지침이 공식 두뇌 출력계약에 주입되는지 회귀 검증.
    되묻기로 끝내지 말고 옵션·소요·리스크·추천·후속을 스스로 채우라는 핵심 의미론이
    finalize_schema_instruction 에 들어 있어야 한다(단발·떠먹임 근본수정의 단일 출처)."""
    instr = A.finalize_schema_instruction()
    assert "자율 완결" in instr, "자율 완결 지침이 출력계약에 없음"
    assert "되묻고 끝내지 마라" in instr, "되묻기 금지 지침 누락"
    assert "추정" in instr, "추정 채움 지침 누락"
    print("[PASS] 출력계약 자율 완결(되묻기 금지·추정 채움) 주입 통과")


def test_autonomous_completion_in_react_addendum():
    """자율 완결 지침이 fallback ReAct 작동 지침에도 주입되는지 회귀 검증.
    공식 두뇌·fallback 두 경로 모두 같은 '되묻지 말고 완결' 의미론을 갖게 한다."""
    add = A.react_system_addendum(5)
    assert "되묻기 전에" in add or "되묻고 끝내는 finalize 는 임무" in add, \
        "ReAct 지침에 자율 완결/되묻기 금지 누락"
    assert "(추정: 근거)" in add or "추정" in add, "ReAct 지침에 추정 채움 누락"
    print("[PASS] ReAct 작동지침 자율 완결 주입 통과")


def test_common_rules_autonomous_section():
    """공통규칙 §2-1(사장 질문 0 — 자율 완결)이 전 에이전트 system_prompt 에 상속 주입되는지
    검증. 박민철 전용이던 '사장 질문 0 즉결'을 공통으로 일반화한 것이 모든 봇에 닿아야 한다."""
    common = A.load_common_rules()
    assert "사장 질문 0" in common, "공통규칙에 자율 완결 §2-1 섹션이 없음(일반화 누락)"
    # 옛 §2 의 '되묻기 권장' 문구("부족 항목을 콕 집어 되묻는다")가 능동형으로 교체됐는지 확인.
    assert "부족 항목을 콕 집어 되묻는다" not in common, "옛 되묻기 권장 문구가 §2에 남아있음"
    assert "스스로 채워서 올린다" in common, "§2 자율 채움 문구로 교체 안 됨"
    # system_prompt 가 공통규칙을 실제로 상속 주입하는지(전 봇 공통 도달 경로) 확인.
    spec = {"prompt": "페르소나", "name": "n", "primary": "방"}
    sp = A.system_prompt(spec, common, "라우팅")
    assert "사장 질문 0" in sp, "system_prompt 가 공통규칙 자율 완결 섹션을 주입하지 않음"
    print("[PASS] 공통규칙 자율 완결 §2-1 일반화 + system_prompt 상속 통과")


def test_tool_result_truncation():
    """도구 결과는 TOOL_RESULT_MAX_CHARS 로 절단되어 토큰 폭주를 막는다."""
    big = "가" * (H.TOOL_RESULT_MAX_CHARS + 5000)
    orig = H.load_room_mem
    H.load_room_mem = lambda channel_id: big
    try:
        out = H.run_tool(A.TOOL_GET_ROOM_MEMORY, {}, "ch1")
    finally:
        H.load_room_mem = orig
    assert len(out) <= H.TOOL_RESULT_MAX_CHARS + 20, f"도구 결과 절단 실패: {len(out)}자"
    assert "절단됨" in out, "절단 표시 누락"
    print("[PASS] 도구 결과 토큰 절단 통과")


def test_token_health_401_detects_zombie():
    """회귀 테스트 — 봇 무응답(좀비) 근본 원인 재현.

    버그: MM 서버 재시작 후 Personal Access Token 의 세션 캐시가 DB 와 어긋나면
      토큰이 DB 상 유효해도 REST/WS 가 401(Invalid or expired session)을 돌려준다.
      WS 는 TCP 만 붙고 인증이 거부돼 posted 이벤트를 못 받아 봇이 무응답 좀비가 됐다.
    근본 원인: 봇이 401 인증 실패를 무한 재접속 백오프로 조용히 삼켜 가시화하지 못함.
    수정 위치: bogo_runtime.token_health() — /users/me 401 이면 False + 강한 경고 로그.

    여기서는 /users/me 가 401 을 주는 상황을 모킹해 token_health 가 좀비를 False 로
    판정하고 경고를 출력하는지 검증한다(이 함수가 좀비를 감지 못하면 회귀)."""
    import io
    import urllib.error
    from contextlib import redirect_stdout

    orig = H.urllib.request.urlopen

    def _fake_401(req, timeout=None):
        raise urllib.error.HTTPError(
            H.MM + "/users/me", 401, "Unauthorized", {},
            io.BytesIO(b'{"id":"api.context.session_expired.app_error"}'))

    H.urllib.request.urlopen = _fake_401
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            ok = H.token_health()
    finally:
        H.urllib.request.urlopen = orig
    assert ok is False, "401 인증 실패를 좀비(False)로 판정하지 못함 — 회귀"
    out = buf.getvalue()
    assert "토큰 인증 실패" in out and "좀비" in out, \
        f"좀비 경고 로그 누락(운영자 가시화 실패): {out!r}"
    print("[PASS] token_health 401 좀비 감지 + 경고 통과")


def test_token_health_ok_when_valid():
    """정상 토큰(자기 BOT_ID 반환) 시 token_health 가 True 를 반환하는지(오탐 방지)."""
    import io
    import json as _json
    from contextlib import redirect_stdout

    orig = H.urllib.request.urlopen

    class _Resp:
        def __init__(self, b):
            self._b = b
        def read(self):
            return self._b

    def _fake_ok(req, timeout=None):
        return _Resp(_json.dumps({"id": H.BOT_ID, "username": "bot"}).encode())

    H.urllib.request.urlopen = _fake_ok
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            ok = H.token_health()
    finally:
        H.urllib.request.urlopen = orig
    assert ok is True, "유효 토큰을 좀비로 오판(False) — 오탐 회귀"
    print("[PASS] token_health 유효 토큰 True 통과")


if __name__ == "__main__":
    test_token_health_401_detects_zombie()
    test_token_health_ok_when_valid()
    test_react_max_steps_cap()
    test_finalize_early_exit()
    test_tool_whitelist_fail_closed()
    test_tool_observation_feedback()
    test_reflexion_single_pass()
    test_reflexion_corrects_violation()
    test_history_tool_cap()
    test_system_prompt_react_injection()
    test_autonomous_completion_in_finalize_contract()
    test_autonomous_completion_in_react_addendum()
    test_common_rules_autonomous_section()
    test_tool_result_truncation()
    print("\n전체 통과 ✓ — ReAct 반복상한·finalize 조기탈출·도구 화이트리스트(fail-closed)·"
          "observation 환류·Reflexion 1회·교정위반 재확정·history 상한·ReAct 주입·"
          "자율완결(출력계약·ReAct·공통규칙 일반화)·토큰 절단")
