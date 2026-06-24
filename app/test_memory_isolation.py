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
    print("[PASS] system_prompt 2층 분리 주입 + 하위 호환 통과")


if __name__ == "__main__":
    test_room_isolation()
    test_room_rolling()
    test_role_mem_regression()
    test_system_prompt_two_layer()
    print("\n전체 테스트 통과 ✓ — 방 메모리 격리 + 역할 메모 회귀 + 프롬프트 2층 주입")
