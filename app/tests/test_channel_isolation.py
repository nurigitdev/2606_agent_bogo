"""
채널 송수신 격리 회귀 테스트 (네트워크/Mattermost/LLM 불필요).

배경(적대적 감사):
  agents/<role>.md 의 `channels:` 화이트리스트는 LLM(헤르메스 두뇌) 프롬프트에도 안내되지만,
  프롬프트 지침만으로는 LLM 이 어기면 인사 보고가 개발 채널로(또는 그 반대로) 샐 수 있다.
  실제 격리를 보장하는 것은 프롬프트가 아니라 bogo_runtime 의 '코드 게이트'다:
    - 수신: run() 이 ID2NAME 으로 푼 채널명이 SUBS(이 역할 구독) 밖이면 즉시 continue.
    - 송신: post 직전 can_send(채널) == (채널 in SUBS) 가 False 면 SEND-BLOCK/ACK-BLOCK
            으로 차단(LLM 이 권한 밖 target_channel 을 지정해도 코드가 막는다).
  이 파일은 그 코드 게이트를 회귀로 가둔다 — '개발봇이 인사 채널로 송신 시도 → 차단'을
  메모리 격리 테스트와 같은 결로 단언한다. 게이트가 약화되면 이 테스트가 깨진다.

검증:
  1) 수신 격리 — SUBS 밖 채널명은 격리 경계에서 걸러진다(구독 채널만 처리).
  2) 송신 격리(핵심) — can_send 가 SUBS 화이트리스트로 fail-closed. 개발 역할은 인사
     채널로 송신 불가, 자기 구독 채널로는 송신 가능.
  3) 역할 간 비대칭 격리 — 인사↔개발 구독이 서로의 팀/보고라인을 포함하지 않는다.
  4) _validate 2차 방어선 — sendable 화이트리스트를 주면 권한 밖 target/ack 결정을
     검증 단계에서 조기 거부하되, 자기 구독 채널 결정은 통과(정상 라우팅 무회귀).

실행: python3 -m pytest tests/test_channel_isolation.py  또는 직접 실행.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def _load_role_module(role):
    """주어진 역할로 bogo_runtime 을 (재)로딩한다.

    bogo_runtime 은 import 시점에 sys.argv[1](역할)을 읽어 그 역할의 SPEC/SUBS/CFG 를
    모듈 전역으로 고정한다. 역할별 격리를 검증하려면 역할마다 새 모듈 인스턴스가 필요하므로
    sys.modules 캐시를 비우고 argv 를 바꿔 다시 import 한다.
    """
    sys.argv = ["bogo_runtime.py", role]
    for m in ("bogo_runtime",):
        sys.modules.pop(m, None)
    import importlib
    mod = importlib.import_module("bogo_runtime")
    return mod


# 데이터(agents/*.md, channels.json)에서 직접 읽은 기대값 — 테스트가 데이터 변경을 따라가게.
DEV = _load_role_module("dev")
HR = _load_role_module("hr")
ORCH = _load_role_module("orchestrator")


def test_dev_cannot_send_to_hr_channels():
    """핵심 단언: 개발봇은 인사총무 채널로 송신할 수 없다(LLM 이 지정해도 코드가 차단)."""
    # 개발봇이 LLM 출력으로 인사 채널을 target 으로 골라도 can_send 가 False → 차단.
    assert DEV.can_send("인사총무팀") is False, "누수! 개발봇이 인사총무팀으로 송신 가능"
    assert DEV.can_send("인사총무-보고라인") is False, "누수! 개발봇이 인사총무-보고라인으로 송신 가능"
    assert DEV.can_send("인사총무-학습방") is False, "누수! 개발봇이 인사 학습방으로 송신 가능"
    # 자기 구독 채널로는 정상 송신 가능(정상 라우팅 무회귀).
    assert DEV.can_send("개발팀") is True, "개발봇이 자기 팀 채널로 송신 불가 — 회귀"
    assert DEV.can_send("개발-보고라인") is True, "개발봇이 자기 보고라인으로 송신 불가 — 회귀"


def test_hr_cannot_send_to_dev_channels():
    """대칭 단언: 인사봇은 개발 채널로 송신할 수 없다."""
    assert HR.can_send("개발팀") is False, "누수! 인사봇이 개발팀으로 송신 가능"
    assert HR.can_send("개발-보고라인") is False, "누수! 인사봇이 개발-보고라인으로 송신 가능"
    assert HR.can_send("개발-학습방") is False, "누수! 인사봇이 개발 학습방으로 송신 가능"
    assert HR.can_send("인사총무팀") is True, "인사봇이 자기 팀 채널로 송신 불가 — 회귀"
    assert HR.can_send("인사총무-보고라인") is True, "인사봇이 자기 보고라인으로 송신 불가 — 회귀"


def test_unknown_channel_send_is_fail_closed():
    """존재하지 않는/빈 채널명은 송신 불가(fail-closed)."""
    assert DEV.can_send("존재하지않는채널") is False
    assert DEV.can_send("") is False
    assert HR.can_send("CEO브리핑") is False, "누수! 인사봇이 CEO브리핑으로 직접 송신 가능"
    assert DEV.can_send("CEO브리핑") is False, "누수! 개발봇이 CEO브리핑으로 직접 송신 가능"


def test_orchestrator_routing_intact():
    """정상 라우팅 무회귀: 박민철(오케스트레이터)은 양 보고라인·CEO브리핑으로 송신 가능,
    팀 실무 채널(인사총무팀/개발팀)로는 송신하지 않는다(구독에 없음)."""
    assert ORCH.can_send("개발-보고라인") is True, "오케스트레이터가 개발 보고라인으로 송신 불가 — 회귀"
    assert ORCH.can_send("인사총무-보고라인") is True, "오케스트레이터가 인사 보고라인으로 송신 불가 — 회귀"
    assert ORCH.can_send("CEO브리핑") is True, "오케스트레이터가 CEO브리핑으로 송신 불가 — 회귀"
    assert ORCH.can_send("정책기획실") is True, "오케스트레이터가 정책기획실로 송신 불가 — 회귀"
    # 팀 실무 방은 구독하지 않으므로 직접 송신 불가(보고라인 경유 원칙).
    assert ORCH.can_send("개발팀") is False, "오케스트레이터가 개발팀 실무방으로 직접 송신 가능 — 격리 위반"
    assert ORCH.can_send("인사총무팀") is False, "오케스트레이터가 인사총무팀 실무방으로 직접 송신 가능 — 격리 위반"


def test_receive_isolation_subscription_boundary():
    """수신 격리: 각 역할의 SUBS 가 곧 수신 경계다. run() 은 cname not in SUBS 면 continue
    하므로, 한 역할의 SUBS 에 타 도메인 실무/보고 채널이 들어 있지 않음을 단언한다."""
    # 개발 구독에는 인사 채널이 하나도 없어야 한다.
    assert not (DEV.SUBS & {"인사총무팀", "인사총무-보고라인", "인사총무-학습방"}), \
        f"누수! 개발 구독에 인사 채널 포함: {DEV.SUBS}"
    # 인사 구독에는 개발 채널이 하나도 없어야 한다.
    assert not (HR.SUBS & {"개발팀", "개발-보고라인", "개발-학습방"}), \
        f"누수! 인사 구독에 개발 채널 포함: {HR.SUBS}"
    # 팀봇은 CEO브리핑을 구독하지 않는다(CEO 직보 차단 — 박민철 경유).
    assert "CEO브리핑" not in DEV.SUBS and "CEO브리핑" not in HR.SUBS, "팀봇이 CEO브리핑 구독 — 격리 위반"


def test_validate_second_defense_blocks_forbidden_target():
    """2차 방어선: _validate 에 sendable(역할 송신 화이트리스트)을 주면, LLM 이 권한 밖
    채널을 target 으로 지정한 결정을 검증 단계에서 조기 거부한다. 자기 구독 채널은 통과."""
    sendable = DEV.SUBS
    # 권한 밖 target → forbidden 사유로 거부.
    bad = {"act": True, "target_channel": "인사총무팀", "message": "x"}
    assert DEV._validate(bad, sendable=sendable).startswith("target-forbidden"), \
        "2차 방어선이 권한 밖 target 을 거부하지 못함"
    # 권한 밖 ack_channel → forbidden 사유로 거부.
    bad_ack = {"act": True, "ack_channel": "인사총무-보고라인", "ack": "x"}
    assert DEV._validate(bad_ack, sendable=sendable).startswith("ackch-forbidden"), \
        "2차 방어선이 권한 밖 ack_channel 을 거부하지 못함"
    # 자기 구독 채널 target → 통과(정상 라우팅 무회귀).
    good = {"act": True, "target_channel": "개발-보고라인", "message": "정상 상신"}
    assert DEV._validate(good, sendable=sendable) == "", "정상 라우팅이 2차 방어선에 막힘 — 회귀"
    # sendable 미전달(하위 호환) → 전체 채널 존재만 검사하므로 통과(기존 호출부 동작 보존).
    assert DEV._validate(bad) == "", "sendable 없는 기존 호출 동작이 바뀜 — 하위 호환 깨짐"


if __name__ == "__main__":
    test_dev_cannot_send_to_hr_channels()
    test_hr_cannot_send_to_dev_channels()
    test_unknown_channel_send_is_fail_closed()
    test_orchestrator_routing_intact()
    test_receive_isolation_subscription_boundary()
    test_validate_second_defense_blocks_forbidden_target()
    print("\n전체 통과 ✓ — 채널 송수신 격리 코드 게이트(can_send/SUBS/_validate 2차 방어) 회귀")
