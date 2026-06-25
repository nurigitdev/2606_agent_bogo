"""
공식 Nous Hermes Agent(`hermes chat`) 두뇌 호출 모듈.

hermes_runtime.decide() 의 '처리 두뇌'를 커스텀 urllib OpenRouter 직접호출에서
공식 hermes CLI 로 통일하기 위한 subprocess 어댑터다. Mattermost 입출력·채널 라우팅·
방 격리는 hermes_runtime/mm_client 가 그대로 담당하고, 이 모듈은 오직 '한 메시지에 대한
행동 결정 JSON' 한 덩어리를 공식 두뇌로부터 받아오는 일만 한다.

설계(재귀학습 활성화판):
  - 비대화식(-q -Q): 1 메시지 = 1 hermes 프로세스. 폭주 방지 위해 --max-turns 상한 강제.
  - 역할별 영속 세션(`--continue hermes-<role>`): 같은 역할의 메시지는 같은 이름의 세션에
    누적된다. 공식 hermes 의 영속 메모리(memory_enabled)·자동 스킬생성(creation_nudge_interval/
    flush_min_turns/nudge_interval)은 '세션이 누적되어야' 임계치에 도달하므로, 단발 무상태
    호출을 역할별 누적 세션으로 바꿔 '진짜 재귀학습'을 켠다.
      · `--continue NAME` 은 그 이름의 세션이 '이미 있을 때만' 재개한다(없으면 즉시 에러).
        그래서 첫 호출은 이름 없이 새 세션을 만들고 stdout 의 `session_id:` 를 캡처해
        `hermes sessions rename <id> hermes-<role>` 로 명명한다. 이후 호출부터 --continue 성공.
  - 역할별 HERMES_HOME 격리(`~/.hermes/profiles/hermes<role>`): 공식 빌트인 memory 는
    `memories/USER.md`·`memories/MEMORY.md` 라는 '전역 단일 파일'에 누적되어 세션을 넘어
    모든 역할에 누출된다(라이브 실증됨). 역할 간 메모리 누수를 0 으로 만들려면 세션 분리만으론
    부족하고 HERMES_HOME 자체를 역할별로 가른다 → memories/sessions(state.db)/자동스킬이
    전부 역할 경계 안에 갇힌다. config.yaml/.env(자격증명·모델·memory·compression 설정)는
    공통 원본에서 1회 복제(clone)해 상속하므로 OpenRouter 키 재사용·신규 키 금지를 지킨다.
  - `--ignore-rules` 는 쓰지 않는다: 이 플래그는 SOUL/AGENTS 뿐 아니라 'memory' 자동주입까지
    함께 꺼서(=재귀학습 무력화) 본 작업의 목표와 정면충돌한다(--help 실증). 대신 (a)봇 cwd 에
    SOUL/AGENTS 가 없고 (b)역할 프로필 홈의 SOUL.md 를 비워(_ensure_role_home) 페르소나 오염을
    막는다. 페르소나·공통규칙·라우팅·교정·학습·방메모·대화이력·출력계약은 query 로 주입한다.
  - `--source tool`: 사용자 세션 목록 오염 방지(서드파티 통합용 태그). --continue 와 양립(실증).
  - timeout: 무한 대기 방지. 타임아웃/실패/JSON 파싱 실패 시 None 반환 → 호출부가
    기존 커스텀 ReAct 두뇌로 graceful fallback.
  - 세션 무한 성장: 공식 compression(config.yaml `compression.enabled: true`)에 위임.
  - 모델/프로바이더: config.yaml 기본(deepseek/deepseek-v4-flash) 사용(비용 통제 유지).
부작용 없는 순수 함수 + 호출 래퍼만 둔다(import 가능, sys.argv 파싱 없음).
"""
import os
import re
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
# 역할별 영속 세션 + 메모리 격리 토글(1=on 기본, 0=off). off 면 세션명/홈 분리를 끄고
# (구) 단발 무상태 호출에 준하게 동작 → 재귀학습은 꺼지지만 안전. 진단·롤백용.
RECURSIVE_LEARNING = _env_int("HERMES_RECURSIVE_LEARNING", 1, 0, 1) == 1
# 역할별 HERMES_HOME(프로필) 루트. 각 역할은 <root>/profiles/hermes<role> 을 자기 홈으로 쓴다.
# 미설정 시 공식 기본(~/.hermes)을 루트로 삼는다.
HERMES_ROOT = os.environ.get("HERMES_ROOT", os.path.expanduser("~/.hermes"))

# 역할명 → 파일/세션 안전 슬러그(소문자 영숫자만). 프로필명 규칙(lowercase alphanumeric)과
# 세션명 규칙을 동시에 만족시킨다.
_SLUG_RE = re.compile(r"[^a-z0-9]")
# 공식 CLI stdout 의 세션 ID 메타 라인(`session_id: 20260625_...`)에서 ID 만 캡처.
_SID_RE = re.compile(r"session_id:\s*([0-9a-zA-Z_]+)")
# `--continue NAME` 이 세션 부재 시 내는 신호(이 문자열이면 '세션 생성 후 rename' 분기).
_NO_SESSION_MARK = "No session found"
# 역할 홈 1회 준비를 마쳤는지 캐시(프로세스 생애 1회 setup). role → True.
_home_ready = {}
# 역할 세션이 명명(rename)되어 --continue 로 재개 가능한지 캐시. role → True.
_session_named = {}


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


# ── 역할 격리: 슬러그 / 세션명 / 프로필(HERMES_HOME) ───────────────────────────
def role_slug(role):
    """역할명을 프로필·세션명에 안전한 슬러그로. 소문자 영숫자만 남긴다(빈 값이면 'role')."""
    s = _SLUG_RE.sub("", (role or "").lower())
    return s or "role"


def session_name(role):
    """역할별 고정 세션명. 같은 역할 메시지는 모두 이 이름의 세션에 누적(영속)된다."""
    return f"hermes-{role_slug(role)}"


def role_home(role):
    """역할별 HERMES_HOME(프로필 디렉토리). 공식 profile 규약(<root>/profiles/<name>)을 따른다.
    이 경계가 곧 memory/세션/자동스킬의 격리 경계 → 타 역할로 절대 누출되지 않는다."""
    return os.path.join(HERMES_ROOT, "profiles", f"hermes{role_slug(role)}")


def _run_hermes(args, role=None, timeout=None, capture=True):
    """공통 hermes 실행 래퍼: 역할 홈(HERMES_HOME) 주입 + cwd=HERE + 부모 env 상속.
    role 이 주어지고 재귀학습 ON 이면 HERMES_HOME 을 역할 프로필로 덮어쓴다(격리).
    OPENROUTER_API_KEY 등 자격증명은 역할 프로필의 .env(clone 으로 상속)와 부모 env 로 해결."""
    binp = resolve_hermes_bin()
    if not binp:
        return None
    env = os.environ.copy()
    if role and RECURSIVE_LEARNING:
        env["HERMES_HOME"] = role_home(role)
    try:
        return subprocess.run(
            [binp] + args, capture_output=capture, text=True,
            timeout=timeout or OFFICIAL_TIMEOUT,
            cwd=HERE, env=env, check=False)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def _ensure_role_home(role):
    """역할 프로필 홈을 프로세스 생애 1회 준비(idempotent).
    - 없으면 `profile create hermes<role> --clone` 로 공통 config.yaml/.env 를 상속 생성한다
      (OpenRouter 키 재사용 — 신규 키 금지). clone 은 전역 SOUL.md/memories 도 끌고 오므로,
      페르소나 오염원인 SOUL.md 와 전역 누적이 새는 memories/{USER,MEMORY}.md 를 비운다.
    - 이미 있으면 SOUL.md/memories 정합만 보장하고 통과.
    실패해도 예외를 던지지 않는다(호출부가 home 없이 진행 → 최악의 경우 fallback)."""
    if not RECURSIVE_LEARNING:
        return False
    if _home_ready.get(role):
        return True
    home = role_home(role)
    name = f"hermes{role_slug(role)}"
    if not os.path.isdir(home):
        # --clone: config.yaml/.env/SOUL.md 를 활성 프로필에서 복사(자격증명 상속).
        # --no-alias: wrapper 스크립트 불필요. --no-skills 는 쓰지 않는다(공식 번들 스킬은
        # 자동 스킬생성의 출발점으로 두되, 생성물은 역할 홈 안에만 쌓이므로 격리 유지).
        r = _run_hermes(["profile", "create", name, "--clone", "--no-alias",
                         "--description", f"Mattermost {role} brain (persistent recursive learning)"],
                        role=None, timeout=OFFICIAL_TIMEOUT)
        if r is None or r.returncode != 0 or not os.path.isdir(home):
            return False
    # 페르소나 오염 차단: clone 된 전역 SOUL.md 를 비운다(우리 페르소나는 query 로 주입).
    soul = os.path.join(home, "SOUL.md")
    try:
        if os.path.exists(soul) and os.path.getsize(soul) > 0:
            open(soul, "w", encoding="utf-8").close()
    except OSError:
        pass
    # 전역 누적이 새 온 memories/{USER,MEMORY}.md 를 비워 '이 역할만의' 누적으로 시작.
    # (이 디렉토리는 이제 역할 홈 안이라, 비운 뒤 쌓이는 건 전부 이 역할 전용이다.)
    mem_dir = os.path.join(home, "memories")
    try:
        os.makedirs(mem_dir, exist_ok=True)
        for fn in ("USER.md", "MEMORY.md"):
            fp = os.path.join(mem_dir, fn)
            if os.path.exists(fp) and os.path.getsize(fp) > 0:
                open(fp, "w", encoding="utf-8").close()
    except OSError:
        pass
    _home_ready[role] = True
    return True


def _name_session(role, session_id):
    """방금 만든 세션(session_id)을 역할 고정 세션명으로 rename → 이후 --continue 로 재개 가능."""
    if not session_id:
        return False
    r = _run_hermes(["sessions", "rename", session_id, session_name(role)],
                    role=role, timeout=OFFICIAL_TIMEOUT)
    if r is not None and r.returncode == 0:
        _session_named[role] = True
        return True
    return False


def _base_chat_args(query):
    """모든 chat 호출 공통 인자(모델/프로바이더/소스/비용상한/비대화식/query).
    --ignore-rules 는 의도적으로 제외(memory 자동주입을 끄지 않기 위함 — 재귀학습 보존)."""
    args = ["chat",
            "--source", "tool",                       # 사용자 세션 목록 오염 방지
            "--max-turns", str(OFFICIAL_MAX_TURNS),   # 내부 도구루프 비용 상한
            "--pass-session-id",                      # stdout 에 session_id 노출(첫 세션 캡처용)
            "-Q",                                     # 비대화식(배너/스피너/미리보기 억제)
            "-q", query]
    if OFFICIAL_PROVIDER:
        args[1:1] = ["--provider", OFFICIAL_PROVIDER]
    if OFFICIAL_MODEL:
        args[1:1] = ["--model", OFFICIAL_MODEL]
    return args


def call_official_brain(query, timeout=None, role=None):
    """공식 hermes 두뇌를 비대화식으로 1회 호출해 stdout 전체를 반환.
    실패(실행파일 없음/비정상 종료/타임아웃)면 None. 출력 파싱은 호출부가 담당.

    재귀학습 ON + role 주어짐:
      - 역할 홈(HERMES_HOME=<root>/profiles/hermes<role>)을 1회 준비(_ensure_role_home).
      - 역할 고정 세션(`--continue hermes-<role>`)으로 호출 → 같은 역할 메시지가 누적되어
        공식 영속 메모리·자동 스킬생성 임계치에 도달한다(진짜 재귀학습).
      - 단, --continue 는 세션이 '없으면' 에러('No session found')다. 첫 호출은 --continue 없이
        새 세션을 만들고 stdout 의 session_id 를 캡처해 역할 세션명으로 rename → 다음부터 영속.
    재귀학습 OFF 또는 role 없음:
      - (구) 단발 무상태 호출(세션 누적 없음, 홈 분리 없음). 안전한 폴백 모드.

    OPENROUTER_API_KEY 등 자격증명은 역할 프로필 .env(상속)와 부모 env 로 해결(신규 키 금지)."""
    if not USE_OFFICIAL_BRAIN:
        return None
    if not resolve_hermes_bin():
        return None

    # ── 재귀학습 비활성 또는 role 미지정: (구) 단발 무상태 호출 ─────────────────
    if not (RECURSIVE_LEARNING and role):
        r = _run_hermes(_base_chat_args(query), role=role, timeout=timeout)
        if r is None or r.returncode != 0:
            return None
        return r.stdout

    # ── 재귀학습 활성 + role: 역할 홈 + 역할 영속 세션 ─────────────────────────
    _ensure_role_home(role)  # 실패해도 진행(홈 없이 호출 → 최소한 단발은 동작)
    sname = session_name(role)

    # 세션이 이미 명명돼 있다고 알려졌으면 곧장 --continue 시도.
    if _session_named.get(role):
        args = _base_chat_args(query)
        args[1:1] = ["--continue", sname]
        r = _run_hermes(args, role=role, timeout=timeout)
        if r is not None and r.returncode == 0 and _NO_SESSION_MARK not in (r.stdout or ""):
            return r.stdout
        # 세션이 사라졌거나(reset/prune) 첫 진입 → 아래 생성 경로로 폴백.
        _session_named[role] = False

    # 세션 생성 경로: --continue 없이 호출(새 세션) → session_id 캡처 → rename.
    r = _run_hermes(_base_chat_args(query), role=role, timeout=timeout)
    if r is None or r.returncode != 0:
        return None
    out = r.stdout or ""
    m = _SID_RE.search(out)
    if m:
        _name_session(role, m.group(1))  # 다음 호출부터 --continue 로 누적
    return out


def decide_via_official(spec, common_rules, routing, cname, convo, speaker_name, text,
                        memo="", room_memo="", learn_note="", timeout=None, role=None):
    """공식 두뇌로 '행동 결정 dict' 를 얻는다. 페르소나·공통규칙·라우팅·교정/학습/방메모·
    대화이력·출력계약을 합성해 query 로 주입하고, 응답 JSON 을 파싱해 반환.
    role 을 받아 call_official_brain 에 전달 → 역할별 영속 세션·메모리 격리에 쓰인다.
    공식 호출 실패·파싱 실패 시 None(→ 호출부가 커스텀 두뇌로 fallback)."""
    query = A.official_brain_query(
        spec, common_rules, routing, cname, convo, speaker_name, text,
        memo=memo, room_memo=room_memo, learn_note=learn_note)
    raw = call_official_brain(query, timeout=timeout, role=role)
    if raw is None:
        return None
    return A.parse_official_brain_output(raw)
