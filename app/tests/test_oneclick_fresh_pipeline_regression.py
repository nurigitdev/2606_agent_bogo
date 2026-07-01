"""BOGO 원클릭 파이프라인 — 신선(fresh) 설치 첫-실행 회귀 테스트.

배경: 신선 리눅스(systemd) 환경에서 ./BOGO_start.sh 를 처음부터 끝까지 완주시키는
과정에서, 그동안 아무도 전 파이프라인을 신선 환경에서 실증하지 않아 숨어 있던 4개의
'첫-실행 전용' 결함이 각 단계에서 순차로 드러났다. 이 파일은 그 4개 버그를 각각
'정확히 재현'해 재발을 막는 회귀 테스트다. 각 테스트는 도커/네트워크 없이 순수 로직·
파일 계약만 검증하므로 CI 에서 항상 실행 가능하다.

재현 대상 버그(모두 신선 실측으로 확인·수정됨):
  BUG#4 [1/5 bootstrap]   pip check 가 선택적 GPU transitive(nvidia-cusparselt) 의
                          "not supported on this platform" 안내를 치명 실패로 오인 →
                          부트스트랩 전체 중단. → pip_check_ok 가 플랫폼 안내는 통과,
                          진짜 충돌만 실패.
  BUG#5 [3/5 backbone]    wait_mm_ready 가 sh/curl 없는 MM 이미지에서 healthcheck
                          'unhealthy' 오판정에 갇혀 MM 이 실서빙 중인데도 180s 소진 후
                          거짓-중단. → mmctl --local readiness 폴백(이미지 도구 무관).
  BUG#6 [5/5 systemd]     systemd unit 템플릿의 WorkingDirectory="__WORKDIR__" 가
                          선행 따옴표 때문에 "path is not absolute" bad-setting →
                          전 봇/대시보드 유닛 기동 실패. → WorkingDirectory 따옴표 제거.
  BUG#8 [5/5 dashboard]   ceo_auth.load_accounts 가 accounts_config.json 을 무조건
                          open → 신선 설치(파일 부재)에서 대시보드 크래시루프. →
                          파일 부재 시 빈 dict 로 graceful degrade(MM 인증 경로 유지).
"""
import json
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]


# ════════════════════════════════════════════════════════════════════════
# BUG#6 — systemd unit 템플릿: WorkingDirectory 는 따옴표 없이 절대경로여야 한다.
# ════════════════════════════════════════════════════════════════════════
def _workdir_lines(template: Path):
    return [
        ln.strip()
        for ln in template.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("WorkingDirectory=")
    ]


def test_systemd_workingdirectory_is_unquoted():
    """WorkingDirectory= 값이 큰따옴표로 감싸이면 systemd 가 'path is not absolute' 로
    유닛을 bad-setting 처리해 기동이 통째로 실패한다(신선 systemd 실측). 두 서비스
    템플릿 모두 따옴표 없는 __WORKDIR__ 여야 한다."""
    for name in ("bogo@.service.template", "bogo-backup.service.template"):
        tpl = APP_DIR / "service" / "templates" / name
        lines = _workdir_lines(tpl)
        assert lines, f"{name} 에 WorkingDirectory= 지시자가 없다."
        for ln in lines:
            val = ln.split("=", 1)[1]
            assert '"' not in val, (
                f"{name}: WorkingDirectory 값에 따옴표가 있으면 systemd 가 "
                f"non-absolute 로 거부한다: {ln!r}"
            )
            assert val == "__WORKDIR__" or val.startswith("/"), (
                f"{name}: WorkingDirectory 는 치환 토큰(__WORKDIR__) 또는 절대경로여야 한다: {ln!r}"
            )


def test_systemd_execstart_keeps_quotes():
    """ExecStart= 는 셸형 파싱이라 공백/한글 경로 인자를 위해 따옴표가 '올바른' 문법이다.
    WorkingDirectory 수정이 ExecStart 의 정당한 따옴표까지 없애지 않았는지 확인."""
    tpl = APP_DIR / "service" / "templates" / "bogo@.service.template"
    exec_lines = [
        ln for ln in tpl.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("ExecStart=")
    ]
    assert exec_lines, "ExecStart= 지시자가 없다."
    assert any('"__WORKDIR__/run_role.sh"' in ln for ln in exec_lines), (
        "ExecStart 의 run_role.sh 경로 인자는 공백/한글 안전을 위해 따옴표를 유지해야 한다."
    )


# ════════════════════════════════════════════════════════════════════════
# BUG#8 — ceo_auth.load_accounts: 파일 부재/손상 시 하드 크래시 금지(빈 dict).
# ════════════════════════════════════════════════════════════════════════
def test_load_accounts_missing_file_returns_empty(tmp_path):
    """accounts_config.json 이 없어도(신선 설치) load_accounts 는 예외 없이 빈 dict.
    대시보드는 이 결과로 정상 기동하고 Mattermost 인증 경로로 동작한다."""
    import ceo_auth as AUTH

    missing = tmp_path / "does_not_exist_accounts.json"
    assert not missing.exists()
    result = AUTH.load_accounts(path=str(missing))
    assert result == {}, "파일 부재 시 빈 dict 여야 한다(하드 크래시 금지)."


def test_load_accounts_corrupt_json_returns_empty(tmp_path):
    """손상된 JSON 도 크래시 대신 빈 dict 로 degrade."""
    import ceo_auth as AUTH

    bad = tmp_path / "corrupt.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    assert AUTH.load_accounts(path=str(bad)) == {}


def test_load_accounts_valid_file_still_parses(tmp_path):
    """정상 파일은 그대로 파싱되어야 한다(graceful degrade 가 정상 경로를 깨지 않음)."""
    import ceo_auth as AUTH

    good = tmp_path / "accounts.json"
    good.write_text(
        json.dumps({"accounts": [{"login_id": "ceo", "role": "ceo",
                                   "pw_hash": "x", "salt": "00"}]}),
        encoding="utf-8")
    accts = AUTH.load_accounts(path=str(good))
    assert set(accts) == {"ceo"}
    assert accts["ceo"]["role"] == "ceo"


# ════════════════════════════════════════════════════════════════════════
# BUG#4 — bootstrap.sh pip_check_ok: 플랫폼 미지원 안내는 통과, 진짜 충돌은 실패.
# ════════════════════════════════════════════════════════════════════════
def _run_pip_check_ok(fake_pip_output: str, fake_pip_rc: int, tmp_path: Path):
    """bootstrap.sh 의 pip_check_ok 함수만 떼어내 가짜 pip 로 구동한다.
    VENV_PY 를 가짜 python 스텁으로 치환해 `-m pip check` 출력/rc 를 주입한다."""
    fake_py = tmp_path / "fakepy"
    fake_py.write_text(
        "#!/usr/bin/env bash\n"
        # 인자가 (-m pip check) 일 때만 주입 출력을 낸다.
        f'cat <<"EOF"\n{fake_pip_output}\nEOF\n'
        f"exit {fake_pip_rc}\n",
        encoding="utf-8")
    fake_py.chmod(0o755)

    # bootstrap.sh 에서 pip_check_ok + say 정의만 추출해 소싱하기보다,
    # 함수 본문을 직접 호출하는 최소 하니스를 만든다(공백/색상 say 는 무해 스텁).
    harness = tmp_path / "harness.sh"
    boot = (APP_DIR / "bootstrap.sh").read_text(encoding="utf-8")
    # pip_check_ok 함수 블록 추출(정의 시작 ~ 첫 단독 '}' 라인).
    lines = boot.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("pip_check_ok()"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    func_src = "\n".join(lines[start:end + 1])
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        'say(){ :; }\n'
        f'VENV_PY="{fake_py}"\n'
        f"{func_src}\n"
        "pip_check_ok\n",
        encoding="utf-8")
    harness.chmod(0o755)
    p = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    return p.returncode


def test_pip_check_ok_passes_on_platform_note(tmp_path):
    """nvidia-cusparselt 류 '플랫폼 미지원' 안내만 있으면 pip_check_ok 는 통과(rc=0)."""
    rc = _run_pip_check_ok(
        "nvidia-cusparselt-cu13 0.8.1 is not supported on this platform",
        fake_pip_rc=1, tmp_path=tmp_path)
    assert rc == 0, "플랫폼 미지원 안내만 있으면 통과해야 한다(부트스트랩 중단 금지)."


def test_pip_check_ok_fails_on_real_conflict(tmp_path):
    """진짜 의존성 충돌(requires ... not installed)은 여전히 실패(rc!=0)해야 한다."""
    rc = _run_pip_check_ok(
        "somepkg 1.0 requires missinglib, which is not installed.",
        fake_pip_rc=1, tmp_path=tmp_path)
    assert rc != 0, "진짜 충돌은 실패로 게이트해야 한다."


def test_pip_check_ok_passes_when_clean(tmp_path):
    """pip check 가 rc=0(깨끗)이면 당연히 통과."""
    rc = _run_pip_check_ok("", fake_pip_rc=0, tmp_path=tmp_path)
    assert rc == 0


# ════════════════════════════════════════════════════════════════════════
# BUG#5 — infra_up.sh wait_mm_ready: 'unhealthy' 포함 비-healthy 상태에서
#         mmctl --local readiness 폴백이 존재해야 한다(이미지 도구 무관 신호).
# ════════════════════════════════════════════════════════════════════════
def test_wait_mm_ready_has_mmctl_fallback():
    """wait_mm_ready 가 healthy 가 아닌 모든 상태에서 mmctl --local 로 readiness 를
    확인하는 폴백을 갖추었는지 소스 계약으로 검증한다(sh/curl 부재 이미지 대응)."""
    src = (APP_DIR / "infra_up.sh").read_text(encoding="utf-8")
    assert "mmctl --local system version" in src, (
        "wait_mm_ready 에 mmctl --local readiness 폴백이 있어야 한다."
    )
    # none/starting 만이 아니라 'unhealthy' 도 폴백 경로에 들어가야 한다
    # (healthcheck 도구 부재로 unhealthy 오판정 시 거짓-중단 방지).
    assert '"$health" != "healthy"' in src, (
        "healthy 가 아닌 모든 상태(unhealthy 포함)에서 직접 readiness 를 확인해야 한다."
    )


def test_bootstrap_uses_pip_check_ok_gate():
    """install_requirements_current_venv 가 raw 'pip check' 대신 pip_check_ok 게이트를
    쓰는지 확인(플랫폼-안내 관용 게이트 배선)."""
    src = (APP_DIR / "bootstrap.sh").read_text(encoding="utf-8")
    assert "pip_check_ok" in src
    # 과거의 무조건 실패 배선("pip check"; then return 1)이 남아 있지 않은지
    assert 'if ! "$VENV_PY" -m pip check; then' not in src, (
        "raw 'pip check' 무조건 실패 게이트가 남아 있으면 안 된다(pip_check_ok 로 대체)."
    )
