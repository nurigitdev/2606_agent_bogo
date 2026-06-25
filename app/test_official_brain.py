"""
공식 Nous Hermes Agent(`hermes chat`) 두뇌 통일 단위 테스트.
(네트워크/실제 hermes 프로세스 불필요 — subprocess 호출은 전부 모킹)

검증:
  1) 출력 파싱 — -Q 모드 stdout(session_id 메타 라인 + JSON 본문)에서 행동 결정 dict 추출.
  2) 정상 경로 — 공식 두뇌가 유효 JSON 을 주면 decide() 가 그 결정을 그대로 반환(fallback 없음).
  3) graceful fallback — 공식 호출 실패(None)/파싱 실패/스키마 위반 시 커스텀 ReAct 두뇌로 전환.
  4) 페르소나·맥락 주입 — query 에 페르소나·공통규칙·라우팅·교정·학습·방메모·대화이력·출력계약이 합성된다.
  5) 방 격리 보존 — 다른 방의 공유 기억은 현재 방 query 에 섞이지 않는다.
  6) 비용 통제 — call_official_brain 이 --max-turns / -Q / --source tool / timeout 을 건다(1메시지=1호출).
  7) 토글 OFF — USE_OFFICIAL_BRAIN=False 면 공식 두뇌를 건너뛰고 곧장 fallback.
  8) 역할 격리(세션명·홈) — 역할별 session_name/role_home 이 역할마다 다르고 슬러그가 안전하다.
  9) 영속 세션 구성 — 세션 명명 후에는 --continue hermes-<role> 로, 미명명이면 새 세션 생성 후
     stdout 의 session_id 를 rename 한다(--continue 부재 시 에러 분기 포함).
 10) role 전달 배선 — decide() 가 ROLE 을 decide_via_official→call_official_brain 까지 넘긴다.
 11) --ignore-rules 부재 — memory 자동주입을 끄지 않도록 chat 인자에 --ignore-rules 가 없다.

실행: python3 test_official_brain.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = ["hermes_runtime.py", "orchestrator"]

import hermes_runtime as H  # noqa: E402
import hermes_brain as B  # noqa: E402
import agent_schema as A  # noqa: E402


def _valid_decision(message="처리 완료", channel="CEO브리핑"):
    return {
        "act": True, "target_channel": channel, "message": message,
        "mentions": [], "ack": "", "ack_channel": "",
        "importance": "routine", "task_status": "closed",
        "memo": "", "reason": "테스트", "learn_applied": True, "learn_basis": "n/a",
    }


def _qmode_stdout(decision):
    """공식 hermes -Q 모드 stdout 모사: session_id 메타 라인 + JSON 본문."""
    return "session_id: 20260625_test_abc\n" + json.dumps(decision, ensure_ascii=False)


def test_parse_official_output():
    """session_id 메타 라인 + 코드펜스/잡설이 섞여도 행동 결정 JSON 을 추출한다."""
    d = _valid_decision()
    parsed = A.parse_official_brain_output(_qmode_stdout(d))
    assert parsed is not None and parsed["message"] == "처리 완료", f"파싱 실패: {parsed}"
    # 코드펜스 + 앞뒤 잡설이 있어도 추출.
    noisy = "session_id: x\n여기 결정입니다:\n```json\n" + json.dumps(d, ensure_ascii=False) + "\n```\n끝."
    parsed2 = A.parse_official_brain_output(noisy)
    assert parsed2 is not None and parsed2["act"] is True, f"펜스/잡설 파싱 실패: {parsed2}"
    # 깨진 출력은 None(→ fallback 트리거).
    assert A.parse_official_brain_output("session_id: x\n그냥 텍스트 응답") is None
    assert A.parse_official_brain_output("") is None
    print("[PASS] 공식 두뇌 출력 파싱(메타 제거·펜스·실패→None) 통과")


def test_official_happy_path():
    """공식 두뇌가 유효 JSON 을 주면 decide() 가 그 결정을 그대로 반환(fallback 안 탐)."""
    H.history = lambda channel_id, n=12: ["사람: 보고 처리해줘"]
    fallback_called = {"v": False}

    def _no_fallback(*a, **k):
        fallback_called["v"] = True
        raise AssertionError("fallback 이 호출되면 안 된다(공식 두뇌 성공 경로)")

    d = _valid_decision(message="공식두뇌 응답")
    orig_call = B.call_official_brain
    orig_fb = H._decide_fallback
    orig_resolve = B.resolve_hermes_bin
    orig_toggle = B.USE_OFFICIAL_BRAIN
    try:
        B.resolve_hermes_bin = lambda: "/fake/hermes"
        B.USE_OFFICIAL_BRAIN = True
        B.call_official_brain = lambda query, timeout=None, role=None: _qmode_stdout(d)
        H._decide_fallback = _no_fallback
        out = H.decide("CEO브리핑", "ch1", "사람", "보고 처리해줘")
    finally:
        B.call_official_brain = orig_call
        H._decide_fallback = orig_fb
        B.resolve_hermes_bin = orig_resolve
        B.USE_OFFICIAL_BRAIN = orig_toggle
    assert out["message"] == "공식두뇌 응답", f"공식 두뇌 결정 미반영: {out}"
    assert fallback_called["v"] is False, "공식 성공인데 fallback 이 호출됨"
    print("[PASS] 공식 두뇌 정상 경로(결정 반환·fallback 미호출) 통과")


def test_official_failure_falls_back():
    """공식 호출이 실패(None)/파싱실패/스키마위반이면 커스텀 ReAct 두뇌로 graceful fallback."""
    H.history = lambda channel_id, n=12: ["사람: 보고 처리해줘"]
    fb = {"v": 0}

    def _fb(cname, channel_id, sp, text, corrections):
        fb["v"] += 1
        return _valid_decision(message="fallback 결정")

    orig_call = B.call_official_brain
    orig_fb = H._decide_fallback
    orig_resolve = B.resolve_hermes_bin
    orig_toggle = B.USE_OFFICIAL_BRAIN
    try:
        B.resolve_hermes_bin = lambda: "/fake/hermes"
        B.USE_OFFICIAL_BRAIN = True
        H._decide_fallback = _fb
        # (a) 공식 호출이 None(실패/타임아웃)
        B.call_official_brain = lambda query, timeout=None, role=None: None
        out_a = H.decide("CEO브리핑", "ch1", "사람", "x")
        # (b) 공식 호출이 비-JSON(파싱 실패)
        B.call_official_brain = lambda query, timeout=None, role=None: "session_id: x\n그냥 텍스트"
        out_b = H.decide("CEO브리핑", "ch1", "사람", "x")
        # (c) 공식 호출이 스키마 위반 JSON(act 누락→잘못된 타입)
        B.call_official_brain = lambda query, timeout=None, role=None: json.dumps({"act": "yes"})
        out_c = H.decide("CEO브리핑", "ch1", "사람", "x")
    finally:
        B.call_official_brain = orig_call
        H._decide_fallback = orig_fb
        B.resolve_hermes_bin = orig_resolve
        B.USE_OFFICIAL_BRAIN = orig_toggle
    assert out_a["message"] == out_b["message"] == out_c["message"] == "fallback 결정"
    assert fb["v"] == 3, f"세 실패 케이스 모두 fallback 해야 함: {fb['v']}회"
    print("[PASS] 공식 실패/파싱실패/스키마위반 → graceful fallback 통과")


def test_toggle_off_skips_official():
    """USE_OFFICIAL_BRAIN=False 면 공식 두뇌를 시도조차 않고 곧장 fallback."""
    H.history = lambda channel_id, n=12: ["사람: x"]
    called = {"official": 0, "fb": 0}

    orig_call = B.call_official_brain
    orig_fb = H._decide_fallback
    orig_toggle = B.USE_OFFICIAL_BRAIN
    try:
        B.USE_OFFICIAL_BRAIN = False

        def _spy_call(query, timeout=None, role=None):
            called["official"] += 1
            return _qmode_stdout(_valid_decision())
        B.call_official_brain = _spy_call

        def _fb(*a, **k):
            called["fb"] += 1
            return _valid_decision(message="fb")
        H._decide_fallback = _fb
        H.decide("CEO브리핑", "ch1", "사람", "x")
    finally:
        B.call_official_brain = orig_call
        H._decide_fallback = orig_fb
        B.USE_OFFICIAL_BRAIN = orig_toggle
    assert called["official"] == 0, "토글 OFF 인데 공식 두뇌가 호출됨"
    assert called["fb"] == 1, "토글 OFF 면 fallback 으로 가야 함"
    print("[PASS] USE_OFFICIAL_BRAIN=False → 공식 두뇌 미시도·곧장 fallback 통과")


def test_query_injects_persona_and_context():
    """합성 query 에 페르소나·공통규칙·라우팅·교정·학습·방메모·대화이력·출력계약이 들어간다."""
    spec = H.SPEC
    q = A.official_brain_query(
        spec, "공통규칙본문", "라우팅본문",
        cname="CEO브리핑", convo="사람: 지난 대화내용토큰",
        speaker_name="사람", text="방금메시지토큰",
        memo="개인메모토큰", room_memo="방기억토큰",
        learn_note=A.CORRECTION_PREFIX + " 교정=교정토큰\n일반학습토큰")
    # 페르소나 본문(박민철)
    assert spec["prompt"][:10] in q, "페르소나 본문 미주입"
    assert "공통규칙본문" in q and "라우팅본문" in q, "공통규칙/라우팅 미주입"
    assert "교정토큰" in q and "반드시 지킬 교정" in q, "교정 강조 섹션 미주입"
    assert "일반학습토큰" in q, "일반 학습 노트 미주입"
    assert "방기억토큰" in q and "개인메모토큰" in q, "방메모/개인메모 미주입"
    assert "지난 대화내용토큰" in q and "방금메시지토큰" in q, "대화이력/현재메시지 미주입"
    assert "출력 계약" in q and '"act"' in q, "출력 계약(JSON 스키마) 미주입"
    # 공식 두뇌는 우리 ReAct 루프 지침을 넣지 않는다(자체 루프 사용).
    assert "ReAct 다단계 루프" not in q, "공식 두뇌 query 에 커스텀 ReAct 지침이 섞임"
    print("[PASS] query 페르소나·맥락·교정·출력계약 주입 통과")


def test_room_isolation_in_query():
    """방 격리: 현재 방 query 에는 그 방의 room_memo 만 들어가고 타 방 기억은 섞이지 않는다."""
    spec = H.SPEC
    q_salary = A.official_brain_query(
        spec, "", "", cname="급여방", convo="c", speaker_name="사람", text="t",
        room_memo="급여기밀_월500")
    q_other = A.official_brain_query(
        spec, "", "", cname="개발방", convo="c", speaker_name="사람", text="t",
        room_memo="배포일정")
    assert "급여기밀_월500" in q_salary, "현재 방 기억 누락"
    assert "급여기밀_월500" not in q_other, "타 방 기억이 다른 방 query 로 누출(방 격리 위반)"
    assert "배포일정" in q_other, "현재 방 기억 누락"
    print("[PASS] query 방 격리(타 방 기억 비누출) 통과")


def _capture_runs(role=None, timeout=42, named=None, stdout=None,
                  recursive=True, returncode=0, no_session_first=False):
    """call_official_brain 을 호출하며 subprocess.run 에 전달된 모든 cmd 를 캡처한다.
    named: 미리 _session_named[role]=True 로 둘지. no_session_first: 첫 --continue 가
    'No session found' 를 내고 생성 경로로 폴백하는 시나리오 모사."""
    import subprocess as _sp
    runs = []

    class _FakeProc:
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    default_out = stdout if stdout is not None else _qmode_stdout(_valid_decision())

    def _fake_run(cmd, **kw):
        runs.append({"cmd": cmd, "timeout": kw.get("timeout"),
                     "home": (kw.get("env") or {}).get("HERMES_HOME")})
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "profile":      # profile create
            return _FakeProc(0, "created")
        if sub == "sessions":     # sessions rename
            return _FakeProc(0, "renamed")
        # chat 호출. --continue 가 있고 no_session_first 면 첫 chat 은 세션 부재 신호.
        is_continue = "--continue" in cmd
        if is_continue and no_session_first and not getattr(_fake_run, "_continued", False):
            _fake_run._continued = True
            return _FakeProc(0, "No session found matching 'x'.")
        return _FakeProc(returncode, default_out)

    orig = {
        "run": _sp.run, "resolve": B.resolve_hermes_bin, "toggle": B.USE_OFFICIAL_BRAIN,
        "rec": B.RECURSIVE_LEARNING, "ready": dict(B._home_ready), "named": dict(B._session_named),
        "isdir": os.path.isdir,
    }
    try:
        _sp.run = _fake_run
        B.resolve_hermes_bin = lambda: "/fake/hermes"
        B.USE_OFFICIAL_BRAIN = True
        B.RECURSIVE_LEARNING = recursive
        # 홈 생성 분기를 결정적으로: 이미 준비됐다고 보아 profile create 부작용을 건너뛴다.
        B._home_ready = {role: True} if role else {}
        B._session_named = {role: bool(named)} if role else {}
        os.path.isdir = lambda p: True  # 역할 홈 존재로 간주(생성 분기 스킵)
        out = B.call_official_brain("질의", timeout=timeout, role=role)
    finally:
        _sp.run = orig["run"]
        B.resolve_hermes_bin = orig["resolve"]
        B.USE_OFFICIAL_BRAIN = orig["toggle"]
        B.RECURSIVE_LEARNING = orig["rec"]
        B._home_ready = orig["ready"]
        B._session_named = orig["named"]
        os.path.isdir = orig["isdir"]
        if hasattr(_fake_run, "_continued"):
            del _fake_run._continued
    return runs, out


def test_cost_controls_in_command():
    """call_official_brain 이 비용·폭주 통제 플래그(--max-turns/-Q/--source/timeout)를 건다.
    재귀학습 OFF(단발 무상태) 경로로 본다 — role 없이 1프로세스 1호출."""
    runs, out = _capture_runs(role=None, timeout=42, recursive=False)
    assert len(runs) == 1, f"단발 경로는 정확히 1 프로세스여야: {len(runs)}회"
    cmd = runs[0]["cmd"]
    assert cmd[0] == "/fake/hermes" and cmd[1] == "chat", f"hermes chat 호출 아님: {cmd[:2]}"
    assert "--max-turns" in cmd, "내부 도구루프 상한(--max-turns) 미적용(비용 통제 누락)"
    assert "-Q" in cmd, "비대화식(-Q) 미적용"
    assert "--source" in cmd and "tool" in cmd, "--source tool 미적용(세션 오염 차단)"
    assert "-q" in cmd and "질의" in cmd, "query(-q) 미전달"
    assert runs[0]["timeout"] == 42, "timeout(무한대기 방지) 미적용"
    assert out is not None, "정상 stdout 인데 None 반환"
    print("[PASS] 비용·폭주 통제 플래그(--max-turns/-Q/--source/timeout, 1메시지=1호출) 통과")


def test_no_ignore_rules_flag():
    """--ignore-rules 부재: 이 플래그는 memory 자동주입까지 끄므로(재귀학습 무력화) 절대 넣지 않는다."""
    for rec, role in ((False, None), (True, "orchestrator")):
        runs, _ = _capture_runs(role=role, recursive=rec, named=True)
        for r in runs:
            if r["cmd"][1] == "chat":
                assert "--ignore-rules" not in r["cmd"], \
                    f"chat 인자에 --ignore-rules 가 있음(memory 자동주입이 꺼져 재귀학습 무력화): {r['cmd']}"
    print("[PASS] --ignore-rules 부재(공식 memory 자동주입 보존 → 재귀학습 가능) 통과")


def test_role_isolation_session_and_home():
    """역할 격리: session_name/role_home 이 역할마다 다르고, 슬러그가 파일·세션 안전하다."""
    assert B.session_name("orchestrator") == "hermes-orchestrator"
    assert B.session_name("hr") == "hermes-hr"
    assert B.session_name("dev") == "hermes-dev"
    # 역할 간 세션명/홈이 절대 같지 않다(누수 경계).
    names = {B.session_name(r) for r in ("orchestrator", "hr", "dev")}
    homes = {B.role_home(r) for r in ("orchestrator", "hr", "dev")}
    assert len(names) == 3 and len(homes) == 3, "역할별 세션명/홈이 충돌(격리 경계 붕괴)"
    # 홈은 공식 profile 규약(<root>/profiles/<name>)을 따른다.
    assert os.path.join("profiles", "hermesorchestrator") in B.role_home("orchestrator")
    # 슬러그는 영숫자만(이상 입력도 안전).
    assert B.role_slug("HR-팀!") == "hr", f"슬러그 비안전: {B.role_slug('HR-팀!')}"
    assert B.session_name("") == "hermes-role", "빈 역할도 안전 기본값"
    print("[PASS] 역할 격리(세션명·홈 분리·슬러그 안전) 통과")


def test_persistent_session_first_then_continue():
    """영속 세션 구성:
      - 세션 미명명(첫 진입): --continue 없이 새 세션 생성 → stdout 의 session_id 를 rename.
      - 세션 명명됨: --continue hermes-<role> 로 재개(누적). 역할 홈(HERMES_HOME)이 주입된다."""
    role = "orchestrator"
    sid_out = "session_id: 20260625_aaa_bbb\n" + json.dumps(_valid_decision(), ensure_ascii=False)
    # (1) 첫 진입: 미명명 → 새 세션 생성 후 rename 이 일어나야 한다.
    runs, out = _capture_runs(role=role, named=False, stdout=sid_out)
    chat_runs = [r for r in runs if r["cmd"][1] == "chat"]
    rename_runs = [r for r in runs if r["cmd"][1] == "sessions" and "rename" in r["cmd"]]
    assert chat_runs, "chat 호출이 없음"
    assert "--continue" not in chat_runs[0]["cmd"], "첫 진입인데 --continue 사용(세션 부재 에러 위험)"
    assert rename_runs, "새 세션 생성 후 rename(세션 명명) 이 없음 → 다음 호출이 영속 안 됨"
    assert "hermes-orchestrator" in rename_runs[0]["cmd"], "rename 대상이 역할 세션명이 아님"
    assert runs[0]["home"] and "hermesorchestrator" in runs[0]["home"], \
        "역할 홈(HERMES_HOME) 미주입(메모리 격리 안 됨)"
    assert out is not None
    # (2) 명명됨: --continue 로 재개.
    runs2, out2 = _capture_runs(role=role, named=True)
    chat2 = [r for r in runs2 if r["cmd"][1] == "chat"]
    assert chat2 and "--continue" in chat2[0]["cmd"] and "hermes-orchestrator" in chat2[0]["cmd"], \
        "명명된 역할인데 --continue hermes-<role> 로 재개하지 않음(영속 누적 실패)"
    assert chat2[0]["home"] and "hermesorchestrator" in chat2[0]["home"], "재개 시 역할 홈 미주입"
    assert out2 is not None
    print("[PASS] 영속 세션(첫 진입→생성+rename, 이후 --continue 재개·역할 홈 주입) 통과")


def test_continue_missing_session_falls_to_create():
    """--continue 가 'No session found' 를 내면(세션 reset/prune) 즉시 생성 경로로 폴백해 자가복구한다."""
    role = "dev"
    sid_out = "session_id: 20260625_ccc_ddd\n" + json.dumps(_valid_decision(), ensure_ascii=False)
    runs, out = _capture_runs(role=role, named=True, stdout=sid_out, no_session_first=True)
    chat_runs = [r for r in runs if r["cmd"][1] == "chat"]
    rename_runs = [r for r in runs if r["cmd"][1] == "sessions" and "rename" in r["cmd"]]
    assert len(chat_runs) >= 2, "세션 부재 시 생성 경로 chat 으로 폴백하지 않음(자가복구 실패)"
    assert "--continue" in chat_runs[0]["cmd"], "첫 시도는 --continue(명명됐다고 알려졌으므로)"
    assert "--continue" not in chat_runs[1]["cmd"], "폴백 생성 호출은 --continue 없이여야 함"
    assert rename_runs, "폴백 생성 후 재명명(다음 호출 영속 복구) 이 없음"
    assert out is not None
    print("[PASS] --continue 세션 부재 → 생성 경로 자가복구(rename 재명명) 통과")


def test_decide_passes_role_through():
    """role 전달 배선: decide() → decide_via_official → call_official_brain 까지 ROLE 이 흐른다."""
    H.history = lambda channel_id, n=12: ["사람: x"]
    seen = {"role": None}
    orig_call = B.call_official_brain
    orig_resolve = B.resolve_hermes_bin
    orig_toggle = B.USE_OFFICIAL_BRAIN
    orig_fb = H._decide_fallback
    try:
        B.resolve_hermes_bin = lambda: "/fake/hermes"
        B.USE_OFFICIAL_BRAIN = True

        def _spy(query, timeout=None, role=None):
            seen["role"] = role
            return _qmode_stdout(_valid_decision())
        B.call_official_brain = _spy
        H._decide_fallback = lambda *a, **k: _valid_decision()
        H.decide("CEO브리핑", "ch1", "사람", "x")
    finally:
        B.call_official_brain = orig_call
        B.resolve_hermes_bin = orig_resolve
        B.USE_OFFICIAL_BRAIN = orig_toggle
        H._decide_fallback = orig_fb
    assert seen["role"] == H.ROLE, f"ROLE({H.ROLE!r}) 이 call_official_brain 까지 전달 안 됨: {seen['role']!r}"
    print(f"[PASS] role 전달 배선(decide→official→call, role={seen['role']!r}) 통과")


if __name__ == "__main__":
    test_parse_official_output()
    test_official_happy_path()
    test_official_failure_falls_back()
    test_toggle_off_skips_official()
    test_query_injects_persona_and_context()
    test_room_isolation_in_query()
    test_cost_controls_in_command()
    test_no_ignore_rules_flag()
    test_role_isolation_session_and_home()
    test_persistent_session_first_then_continue()
    test_continue_missing_session_falls_to_create()
    test_decide_passes_role_through()
    print("\n전체 통과 ✓ — 공식 hermes 두뇌(재귀학습판): 출력 파싱·정상 경로·graceful fallback·"
          "토글 OFF·페르소나/맥락 주입·방 격리·비용 통제·--ignore-rules 부재·역할 격리(세션/홈)·"
          "영속 세션(생성+rename→--continue)·세션부재 자가복구·role 전달 배선")
