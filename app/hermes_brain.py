"""
공식 Nous Hermes Agent(`hermes chat`) 두뇌 호출 모듈.

hermes_runtime.decide() 의 '처리 두뇌'를 커스텀 urllib OpenRouter 직접호출에서
공식 hermes CLI 로 통일하기 위한 subprocess 어댑터다. Mattermost 입출력·채널 라우팅·
방 격리는 hermes_runtime/mm_client 가 그대로 담당하고, 이 모듈은 오직 '한 메시지에 대한
행동 결정 JSON' 한 덩어리를 공식 두뇌로부터 받아오는 일만 한다.

설계:
  - 비대화식(-q -Q): 1 메시지 = 1 hermes 프로세스. 폭주 방지 위해 --max-turns 상한 강제.
  - `--ignore-rules`: 봇 머신의 SOUL.md/AGENTS.md 자동주입을 끄고(페르소나 오염 차단),
    우리 페르소나·공통규칙·라우팅·교정·학습·방메모·대화이력·출력계약만 query 로 주입.
  - `--source tool`: 사용자 세션 목록 오염 방지(서드파티 통합용 태그).
  - timeout: 무한 대기 방지. 타임아웃/실패/JSON 파싱 실패 시 None 반환 → 호출부가
    기존 커스텀 ReAct 두뇌로 graceful fallback.
  - 모델/프로바이더: config.yaml 기본(deepseek/deepseek-v4-flash) 사용(비용 통제 유지).
부작용 없는 순수 함수 + 호출 래퍼만 둔다(import 가능, sys.argv 파싱 없음).
"""
import os
import shutil
import subprocess

import agent_schema as A

HERE = os.path.dirname(os.path.abspath(__file__))


def _env_int(name, default, lo, hi):
    """환경변수에서 정수 설정을 읽되 [lo, hi] 로 클램프(오설정·폭주 방지)."""
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


# ── 공식 두뇌 호출 통제 파라미터(env 조정 가능, 안전 범위 클램프) ────────────────
# 공식 hermes 사용 여부 토글. 1=공식 두뇌 우선(기본), 0=완전 비활성(항상 fallback).
USE_OFFICIAL_BRAIN = _env_int("HERMES_USE_OFFICIAL_BRAIN", 1, 0, 1) == 1
# 1 메시지 처리당 hermes 프로세스 벽시계 상한(초). 무한 대기·비용 폭주 방지.
OFFICIAL_TIMEOUT = _env_int("HERMES_OFFICIAL_TIMEOUT", 90, 10, 600)
# 공식 두뇌의 내부 도구호출 반복 상한(--max-turns). 비용 통제(낮게 유지).
OFFICIAL_MAX_TURNS = _env_int("HERMES_OFFICIAL_MAX_TURNS", 6, 1, 30)
# 공식 hermes CLI 실행 파일 경로(미설정 시 PATH 및 알려진 위치 탐색).
OFFICIAL_BIN = os.environ.get("HERMES_BIN", "")
# 공식 두뇌가 쓸 프로바이더/모델(미설정 시 config.yaml 기본값 사용 = 빈 문자열).
OFFICIAL_PROVIDER = os.environ.get("HERMES_PROVIDER", "openrouter")
OFFICIAL_MODEL = os.environ.get("HERMES_MODEL", "deepseek/deepseek-v4-flash")


def resolve_hermes_bin():
    """공식 hermes 실행 파일 경로를 해석. 우선순위: HERMES_BIN env → PATH → 알려진 설치 위치.
    찾지 못하면 빈 문자열(→ 호출부가 fallback)."""
    if OFFICIAL_BIN and os.path.exists(OFFICIAL_BIN):
        return OFFICIAL_BIN
    found = shutil.which("hermes")
    if found:
        return found
    # 알려진 사용자 설치 위치(pipx/uv tool 기본).
    for cand in (
        os.path.expanduser("~/.local/bin/hermes"),
        os.path.expanduser("~/.hermes/hermes-agent/hermes"),
    ):
        if os.path.exists(cand):
            return cand
    return ""


def is_official_available():
    """공식 두뇌를 쓸 수 있는 상태인지(토글 ON + 실행파일 존재). 로그/진단용."""
    return USE_OFFICIAL_BRAIN and bool(resolve_hermes_bin())


def call_official_brain(query, timeout=None):
    """공식 hermes 두뇌를 비대화식으로 1회 호출해 stdout 전체를 반환.
    실패(실행파일 없음/비정상 종료/타임아웃)면 None. 출력 파싱은 호출부가 담당.
    OPENROUTER_API_KEY 등 자격증명은 부모 프로세스 환경(.env 로드됨)을 그대로 상속한다."""
    if not USE_OFFICIAL_BRAIN:
        return None
    binp = resolve_hermes_bin()
    if not binp:
        return None
    cmd = [binp, "chat",
           "--ignore-rules",       # 봇 머신 SOUL/AGENTS 자동주입 차단(페르소나 오염 방지)
           "--source", "tool",     # 사용자 세션 목록 오염 방지
           "--max-turns", str(OFFICIAL_MAX_TURNS),  # 내부 도구루프 비용 상한
           "-Q",                   # 비대화식 프로그램 모드(배너/스피너/미리보기 억제)
           "-q", query]
    if OFFICIAL_PROVIDER:
        cmd[2:2] = ["--provider", OFFICIAL_PROVIDER]
    if OFFICIAL_MODEL:
        cmd[2:2] = ["--model", OFFICIAL_MODEL]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout or OFFICIAL_TIMEOUT,
            cwd=HERE, env=os.environ.copy(), check=False)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def decide_via_official(spec, common_rules, routing, cname, convo, speaker_name, text,
                        memo="", room_memo="", learn_note="", timeout=None):
    """공식 두뇌로 '행동 결정 dict' 를 얻는다. 페르소나·공통규칙·라우팅·교정/학습/방메모·
    대화이력·출력계약을 합성해 query 로 주입하고, 응답 JSON 을 파싱해 반환.
    공식 호출 실패·파싱 실패 시 None(→ 호출부가 커스텀 두뇌로 fallback)."""
    query = A.official_brain_query(
        spec, common_rules, routing, cname, convo, speaker_name, text,
        memo=memo, room_memo=room_memo, learn_note=learn_note)
    raw = call_official_brain(query, timeout=timeout)
    if raw is None:
        return None
    return A.parse_official_brain_output(raw)
