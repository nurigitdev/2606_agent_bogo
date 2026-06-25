"""멀티프로세스 동시 쓰기 테스트용 워커(spawn 호환 — 모듈 최상위에 함수 정의).
test_vault.py 가 이 모듈의 mp_write 를 Pool 로 호출한다. VAULT_ROOT 는 인자로 주입받아
프로세스마다 같은 디렉토리에 쓴다(진짜 다중 에이전트 동시 쓰기 모사)."""
import os


def mp_write(args):
    """프로세스 1개가 N개 보고 노트를 동시 쓰기. 반환: 실제 생성 성공 개수."""
    tid, vroot, n_each = args
    import vault_schema as S
    S.VAULT_ROOT = vroot
    import vault_writer as W
    made = 0
    for i in range(n_each):
        p = W.append_report(f"proc{tid}", "개발", f"mp t{tid} n{i}",
                            body="동시쓰기 본문 " * 10, tags=["mp"])
        if p and os.path.exists(p):
            made += 1
    return made
