"""
메모리 2층 구조 단위 테스트 (네트워크/Mattermost 불필요).

검증:
  1) 방 격리 — 채널 A에 저장한 메모가 채널 B의 load_room_mem에는 안 나오고,
     채널 A의 load_room_mem에는 나온다. (급여 등 방-specific 정보 누출 차단 1순위)
  2) 역할 개인 메모 회귀 — 기존 load_mem/save_mem 롤링 저장이 그대로 동작.
  3) system_prompt 2층 주입 — 방 공유 기억/개인 메모 섹션이 분리 주입되고,
     room_memo 미전달 시(하위 호환) 기존 동작 유지.

실행: <venv>/python test_memory_isolation.py
"""
import os
import sys
import tempfile

# hermes_runtime 은 import 시점에 argv[1](역할)과 config 파일을 읽는다.
# 유효 역할을 주어 모듈 로딩을 통과시킨다.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = ["hermes_runtime.py", "orchestrator"]

import hermes_runtime as H  # noqa: E402  (argv 설정 후 import 의도)
import agent_schema as A  # noqa: E402


def _isolate_to_tmp(tmp):
    """모듈의 저장 루트(HERE)와 역할 메모 경로(MEM_PATH)를 임시 디렉토리로 격리.
    실제 운영 memory_*.json 파일을 오염시키지 않기 위함."""
    H.HERE = tmp
    H.MEM_PATH = os.path.join(tmp, "memory_test_role.json")


def test_room_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_to_tmp(tmp)
        ch_a = "channelAAA1111"   # 인사방 같은 채널 ID
        ch_b = "channelBBB2222"   # 다른 방 채널 ID

        # 채널 A에 방-specific 민감 메모(급여) 저장
        H.save_room_mem(ch_a, "김대리 급여 인상 3% 확정")
        # 채널 B에는 무관한 메모 저장
        H.save_room_mem(ch_b, "신규 배포 일정 공유")

        a = H.load_room_mem(ch_a)
        b = H.load_room_mem(ch_b)

        # 핵심 격리 단언: A의 급여 메모는 B에서 절대 보이면 안 된다.
        assert "급여" in a, f"채널 A 자기 메모가 보여야 함: {a!r}"
        assert "급여" not in b, f"누출! 채널 B에 A의 급여 메모가 새어들어옴: {b!r}"
        assert "배포" in b, f"채널 B 자기 메모가 보여야 함: {b!r}"
        assert "배포" not in a, f"누출! 채널 A에 B의 메모가 새어들어옴: {a!r}"

        # 파일이 채널별로 물리적으로 분리되었는지 확인
        assert os.path.basename(H.room_mem_path(ch_a)) != os.path.basename(H.room_mem_path(ch_b))
        print("[PASS] 방 격리 — A↔B 비격리 단언 통과")


def test_room_rolling():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_to_tmp(tmp)
        ch = "rollroom"
        for i in range(1, 10):  # 9줄 저장 → 최근 6줄만 유지
            H.save_room_mem(ch, f"라인{i}")
        mem = H.load_room_mem(ch)
        lines = [x for x in mem.split("\n") if x.strip()]
        assert len(lines) == 6, f"방 메모 롤링 6줄 유지여야 함: {len(lines)}줄"
        assert lines[0] == "라인4" and lines[-1] == "라인9", f"롤링 순서 오류: {lines}"
        print("[PASS] 방 메모 롤링(최근 6줄) 통과")


def test_role_mem_regression():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_to_tmp(tmp)
        # 빈 상태에서 시작
        assert H.load_mem() == "", "초기 역할 메모는 빈 문자열이어야 함"
        for i in range(1, 9):  # 8줄 → 최근 6줄만
            H.save_mem(f"개인진행{i}")
        mem = H.load_mem()
        lines = [x for x in mem.split("\n") if x.strip()]
        assert len(lines) == 6, f"역할 메모 롤링 6줄 유지여야 함: {len(lines)}줄"
        assert lines[-1] == "개인진행8", f"역할 메모 최신 라인 오류: {lines}"
        # 방 메모와 물리적으로 다른 파일인지 확인
        assert H.MEM_PATH != H.room_mem_path("개인진행")
        print("[PASS] 역할 개인 메모 회귀(load_mem/save_mem) 통과")


def test_system_prompt_two_layer():
    spec = {"prompt": "너는 인사 담당.", "name": "테스터", "primary": "인사방"}
    # 2층 주입: 방 공유 기억 + 개인 메모가 분리 섹션으로 들어가야 한다.
    sp = A.system_prompt(spec, "공통규칙", "라우팅텍스트",
                         memo="개인진행메모", room_memo="이방의급여이슈")
    assert "[이 방의 공유 기억]" in sp and "이방의급여이슈" in sp, "방 공유 기억 섹션 누락"
    assert "[내 개인 메모]" in sp and "개인진행메모" in sp, "개인 메모 섹션 누락"

    # 하위 호환: room_memo 미전달 시 기존 호출 형태가 그대로 동작.
    sp2 = A.system_prompt(spec, "공통규칙", "라우팅텍스트", memo="개인진행메모")
    assert "[이 방의 공유 기억]" not in sp2, "room_memo 없으면 방 섹션이 없어야 함"
    assert "[내 개인 메모]" in sp2, "기존 memo 인자 동작 유지되어야 함"
    # 하위 호환: learn_note 미전달 시 학습 노트 섹션이 없어야 함.
    assert "[팀 학습 노트]" not in sp2, "learn_note 없으면 학습 노트 섹션이 없어야 함"
    print("[PASS] system_prompt 2층 분리 주입 + 하위 호환 통과")


# ── 학습 노트(persistent 3층) 신규 테스트 ────────────────────────────────────

def _isolate_learn_to_tmp(tmp):
    """학습 노트 저장 루트도 임시 디렉토리로 격리(운영 memory_learn_*.json 오염 방지)."""
    H.HERE = tmp


def test_learn_note_persistent_no_rolling():
    """학습 노트는 롤링이 없어야 한다 — 6줄 초과로 넣어도 전부 보존(persistent)."""
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_learn_to_tmp(tmp)
        role = "hr"
        for i in range(1, 13):  # 12줄 저장 — 롤링이면 6줄로 잘릴 것
            H.save_learn_note(f"교훈{i}", role=role)
        note = H.load_learn_note(role=role)
        lines = [x for x in note.split("\n") if x.strip()]
        assert len(lines) == 12, f"학습 노트는 휘발 없이 12줄 전부 보존돼야 함(롤링 금지): {len(lines)}줄"
        assert lines[0] == "교훈1", f"가장 오래된 학습이 사라지면 안 됨: {lines[:2]}"
        assert lines[-1] == "교훈12", f"최신 학습 누락: {lines[-2:]}"
        print("[PASS] 학습 노트 persistent(롤링 없음, 12줄 전수 보존) 통과")


def test_learn_note_dedup():
    """같은 교훈을 반복 저장해도 중복 누적되지 않아야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_learn_to_tmp(tmp)
        role = "dev"
        H.save_learn_note("배포 전 롤백 플랜 필수", role=role)
        H.save_learn_note("배포 전 롤백 플랜 필수", role=role)  # 중복
        H.save_learn_note("배포 전 롤백 플랜 필수", role=role)  # 중복
        lines = [x for x in H.load_learn_note(role=role).split("\n") if x.strip()]
        assert len(lines) == 1, f"동일 교훈은 1줄만 유지돼야 함: {len(lines)}줄"
        print("[PASS] 학습 노트 중복 제거 통과")


def test_learn_note_role_isolation():
    """역할별 학습 노트는 격리 — hr 학습이 dev 노트에 절대 새지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_learn_to_tmp(tmp)
        H.save_learn_note("인사: 연차사용촉진 기한 엄수", role="hr")
        H.save_learn_note("개발: 결제 API는 SEV1 가정", role="dev")
        hr_note = H.load_learn_note(role="hr")
        dev_note = H.load_learn_note(role="dev")
        assert "연차" in hr_note and "연차" not in dev_note, f"누출! hr 학습이 dev에 새어듦: {dev_note!r}"
        assert "결제" in dev_note and "결제" not in hr_note, f"누출! dev 학습이 hr에 새어듦: {hr_note!r}"
        # 물리적으로 다른 파일인지 확인
        assert H.learn_path("hr") != H.learn_path("dev")
        print("[PASS] 학습 노트 역할 격리(hr↔dev 비격리 단언) 통과")


def test_learn_note_vs_room_mem_separation():
    """학습 노트(persistent)와 방 메모리(롤링)는 물리적으로 분리된 파일이어야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_learn_to_tmp(tmp)
        # 방 메모리는 롤링(6줄), 학습 노트는 누적(무제한) — 같은 키워드라도 파일이 달라야 함
        H.save_room_mem("somechannel", "방 롤링 항목")
        H.save_learn_note("학습 누적 항목", role="hr")
        assert H.learn_path("hr") != H.room_mem_path("somechannel"), "학습 노트와 방 메모리 파일이 같으면 안 됨"
        assert "학습" not in H.load_room_mem("somechannel"), "학습 노트가 방 메모리로 새면 안 됨"
        assert "방 롤링" not in H.load_learn_note(role="hr"), "방 메모리가 학습 노트로 새면 안 됨"
        print("[PASS] 학습 노트 ↔ 방 메모리 파일 분리 통과")


def test_system_prompt_three_layer():
    """3층 주입: 학습 노트 + 방 공유 기억 + 개인 메모가 각각 분리 섹션으로 들어가야 한다."""
    spec = {"prompt": "너는 인사 담당.", "name": "테스터", "primary": "인사방"}
    sp = A.system_prompt(spec, "공통규칙", "라우팅텍스트",
                         memo="개인진행메모", room_memo="이방의급여이슈",
                         learn_note="연차촉진 기한 엄수 교훈")
    assert "[팀 학습 노트]" in sp and "연차촉진" in sp, "학습 노트 섹션 누락"
    assert "[이 방의 공유 기억]" in sp and "이방의급여이슈" in sp, "방 공유 기억 섹션 누락"
    assert "[내 개인 메모]" in sp and "개인진행메모" in sp, "개인 메모 섹션 누락"
    # 학습 노트가 방 공유 기억보다 앞에 와야 한다(영구 정책을 먼저 각인).
    assert sp.index("[팀 학습 노트]") < sp.index("[이 방의 공유 기억]"), "학습 노트는 방 기억보다 앞에 주입돼야 함"
    print("[PASS] system_prompt 3층 분리 주입 통과")


# ── 재귀학습 교정 루프 신규 테스트 ────────────────────────────────────────────

def test_correction_save_structured():
    """교정 저장은 원지시·잘못된출력·교정내용을 구조화해 [교정] 항목으로 누적한다."""
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_learn_to_tmp(tmp)
        ok = H.save_correction(
            original="연차 촉진 기한 안내",
            wrong="기한은 12월 31일이라고 답함",
            fix="연차 사용 촉진 기한은 회계연도 종료 6개월 전이다",
            role="hr")
        assert ok is True, "유효 교정은 저장 성공해야 함"
        corrections = H.load_corrections(role="hr")
        assert len(corrections) == 1, f"교정 1건 누적돼야 함: {corrections}"
        c = corrections[0]
        assert c.startswith(H.CORRECTION_PREFIX), "교정 라인은 교정 접두로 시작해야 함"
        assert "원지시=" in c and "잘못된출력=" in c and "교정=" in c, f"3요소 구조화 누락: {c}"
        assert "6개월" in c, "교정 핵심 내용이 보존돼야 함"
        # 교정 내용이 비면 저장 안 함
        assert H.save_correction("o", "w", "", role="hr") is False, "빈 교정은 저장 안 함"
        print("[PASS] 교정 구조화 저장(원지시·잘못된출력·교정) 통과")


def test_correction_priority_injection():
    """교정 항목은 [반드시 지킬 교정] 강조 섹션으로 일반 학습 노트와 분리·우선 렌더된다."""
    note = (H.CORRECTION_PREFIX + " 원지시=X ||| 잘못된출력=틀린답 ||| 교정=올바른답\n"
            "이다은: 일반 노하우 한 줄")
    sp = A.system_prompt({"prompt": "p", "name": "n", "primary": "방"},
                         "공통", "라우팅", learn_note=note)
    assert "[반드시 지킬 교정]" in sp, "교정 강조 섹션 누락"
    assert "올바른답" in sp, "교정 내용 주입 누락"
    assert "[팀 학습 노트]" in sp and "일반 노하우" in sp, "일반 학습 노트 섹션 누락"
    # 교정 섹션이 일반 학습 노트보다 앞에 와야 한다(최우선 각인).
    assert sp.index("[반드시 지킬 교정]") < sp.index("[팀 학습 노트]"), "교정은 일반 노트보다 앞이어야 함"
    # self-check 안내 필드가 함께 주입돼야 한다.
    assert "learn_applied" in sp and "[자기점검]" in sp, "self-check 안내 누락"
    # 교정이 없으면 강조 섹션·self-check 안내도 없어야 한다(하위 호환).
    sp2 = A.system_prompt({"prompt": "p", "name": "n", "primary": "방"},
                          "공통", "라우팅", learn_note="이다은: 일반만")
    assert "[반드시 지킬 교정]" not in sp2 and "learn_applied" not in sp2, "교정 없으면 강조/자기점검 미주입"
    assert "[팀 학습 노트]" in sp2, "일반 노트는 주입돼야 함"
    print("[PASS] 교정 우선 주입 + self-check 안내 + 하위 호환 통과")


def test_self_check_field_and_conflict():
    """_validate는 learn_applied bool 을 허용하고, self_check는 교정 모순을 잡는다."""
    corrections = [H.CORRECTION_PREFIX + " 잘못된출력=12월31일 ||| 교정=6개월전"]
    # (1) learn_applied 비-bool 은 스키마 거부
    assert H._validate({"act": True, "learn_applied": "yes"}) == "learn_applied", "비-bool 거부해야 함"
    assert H._validate({"act": True, "learn_applied": True}) == "", "bool 은 통과해야 함"
    # (2) 교정 무시(learn_applied=false + act=true) 모순 감지
    assert H.self_check({"act": True, "learn_applied": False}, corrections), "교정 무시 모순 감지 실패"
    # (3) 교정된 잘못된출력이 본문에 재등장하면 모순 감지
    bad = {"act": True, "message": "연차 기한은 12월31일입니다"}
    assert H.self_check(bad, corrections), "잘못된출력 재등장 모순 감지 실패"
    # (4) 교정을 잘 지킨 응답은 모순 없음
    good = {"act": True, "learn_applied": True, "message": "연차 기한은 종료 6개월전입니다"}
    assert H.self_check(good, corrections) == "", f"정상 응답인데 모순 오탐: {H.self_check(good, corrections)}"
    # (5) 교정이 없으면 self-check는 항상 통과
    assert H.self_check(bad, []) == "", "교정 없으면 self-check 통과여야 함"
    print("[PASS] self-check 필드 검증 + 교정 모순 감지 통과")


def test_correction_feedback_parse():
    """교정 트리거 감지 + 'X가 아니라 Y' 파싱."""
    assert H.is_correction_feedback("그거 틀렸어, A가 아니라 B야"), "교정 트리거 감지 실패"
    assert not H.is_correction_feedback("수고했어 잘했네"), "일반 칭찬을 교정으로 오탐"
    wrong, fix = H.parse_correction("연차 기한은 12월31일이 아니라 6개월 전이야")
    assert "12월31일" in wrong and "6개월" in fix, f"파싱 오류: wrong={wrong!r} fix={fix!r}"
    # 가를 수 없으면 전체를 교정으로
    w2, f2 = H.parse_correction("이건 그냥 정정해야 해")
    assert w2 == "" and "정정" in f2, f"폴백 파싱 오류: {w2!r} {f2!r}"
    print("[PASS] 교정 피드백 트리거 감지 + 파싱 통과")


def test_learn_note_cap_pruning():
    """무한 증식 방지: 상한 초과 시 일반 항목부터 정리되고 교정은 우선 보존된다."""
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_learn_to_tmp(tmp)
        role = "dev"
        # 교정 3건 먼저 저장(보존 대상)
        for i in range(3):
            H.save_correction(f"orig{i}", f"wrong{i}", f"교정내용{i}", role=role)
        # 일반 항목을 상한 훨씬 초과로 저장
        for i in range(H.LEARN_MAX_LINES + 30):
            H.save_learn_note(f"최지현: 일반항목{i}", role=role)
        lines = [x for x in H.load_learn_note(role=role).split("\n") if x.strip()]
        assert len(lines) <= H.LEARN_MAX_LINES, f"전체 상한 초과 — 정리 실패: {len(lines)}줄"
        # 교정 3건은 모두 살아있어야 한다(우선 보존)
        corrections = [x for x in lines if x.startswith(H.CORRECTION_PREFIX)]
        assert len(corrections) == 3, f"교정이 정리돼 사라짐(우선 보존 실패): {len(corrections)}건"
        for i in range(3):
            assert any(f"교정내용{i}" in c for c in corrections), f"교정내용{i} 유실"
        # 가장 오래된 일반 항목은 정리됐어야 한다
        assert "일반항목0" not in H.load_learn_note(role=role), "오래된 일반 항목이 정리되지 않음"
        print("[PASS] 학습 노트 상한 정리(교정 우선 보존, 오래된 일반 정리) 통과")


def test_correction_role_isolation():
    """교정도 역할 격리 — hr 교정이 dev 노트에 절대 새지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_learn_to_tmp(tmp)
        H.save_correction("o", "w", "인사연차교정", role="hr")
        H.save_correction("o", "w", "개발배포교정", role="dev")
        hr_c = "\n".join(H.load_corrections(role="hr"))
        dev_c = "\n".join(H.load_corrections(role="dev"))
        assert "인사연차교정" in hr_c and "인사연차교정" not in dev_c, f"누출! hr 교정이 dev에: {dev_c!r}"
        assert "개발배포교정" in dev_c and "개발배포교정" not in hr_c, f"누출! dev 교정이 hr에: {hr_c!r}"
        print("[PASS] 교정 역할 격리(hr↔dev) 통과")


def test_learning_room_for_role():
    """teams.json learning_rooms 에서 역할별 학습방을 정확히 조회한다."""
    teams = A.load_teams()
    hr_room = A.learning_room_for_role(teams, "hr")
    dev_room = A.learning_room_for_role(teams, "dev")
    orch_room = A.learning_room_for_role(teams, "orchestrator")
    assert hr_room and hr_room["channel"] == "인사총무-학습방", f"hr 학습방 매핑 오류: {hr_room}"
    assert dev_room and dev_room["channel"] == "개발-학습방", f"dev 학습방 매핑 오류: {dev_room}"
    assert orch_room and orch_room["channel"] == "비서실-학습방", f"orchestrator 학습방 매핑 오류: {orch_room}"
    assert A.learning_room_for_role(teams, "없는역할") is None, "미정의 역할은 None이어야 함"
    print("[PASS] learning_room_for_role 역할별 학습방 조회 통과")


if __name__ == "__main__":
    test_room_isolation()
    test_room_rolling()
    test_role_mem_regression()
    test_system_prompt_two_layer()
    test_learn_note_persistent_no_rolling()
    test_learn_note_dedup()
    test_learn_note_role_isolation()
    test_learn_note_vs_room_mem_separation()
    test_system_prompt_three_layer()
    test_learning_room_for_role()
    # 재귀학습 교정 루프 신규
    test_correction_save_structured()
    test_correction_priority_injection()
    test_self_check_field_and_conflict()
    test_correction_feedback_parse()
    test_learn_note_cap_pruning()
    test_correction_role_isolation()
    print("\n전체 테스트 통과 ✓ — 메모리 3층 격리/회귀/persistent + 프롬프트 3층 주입 "
          "+ 교정 구조화 저장·우선주입·self-check·상한정리·역할격리(재귀학습 닫힌 루프)")
