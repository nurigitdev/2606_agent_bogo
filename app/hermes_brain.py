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
    단, 라이브 실증 결과 `--source tool` 은 세션 DB 의 source 컬럼에 반영되지 않고 cli 로 기록된다
    (`sessions list --source tool` → "No sessions found", `--source cli` → 봇 세션 노출). 그래서
    세션 식별은 source 에 의존하지 않고 '타이틀 네이밍(hermes-<role>)' 단독으로 일원화한다.
    CLI 풀 오염 회피는 source 가 아니라 '역할별 HERMES_HOME 격리'가 담당한다(이미 적용됨).
  - 세션 영속성 진실원천: '프로세스 메모리 캐시'가 아니라 '세션 DB 의 타이틀'이다. 봇이 재기동되면
    프로세스 dict 는 비지만 state.db 의 hermes-<role> 타이틀은 남는다. 그래서 매 호출 전
    `hermes sessions list` 로 그 역할 홈에서 hermes-<role> 타이틀 존재를 확인해 있으면 --continue,
    없으면 새 세션 생성→rename 한다. `--continue hermes-<role>` 가 타이틀로 정확히 그 세션을
    재개함을 라이브 실증(누적 message_count 증가·직전 맥락 기억).
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
# 주의: 홈 setup(profile create/SOUL 비우기)은 1회면 충분하고 봇 생애 동안 유지되므로 캐시 OK.
# 반면 '세션 명명 여부'는 캐시하지 않는다(아래 _session_named 제거 참조).
_home_ready = {}
# [P0 근본수정] '세션 명명 여부'를 프로세스 메모리에 캐시하지 않는다.
# (구) _session_named dict 는 봇 재기동 시 비어, 재기동마다 --continue 를 건너뛰고 새 세션을
# 반복 생성 → 학습 누적이 끊겼다. 영속성의 진실원천은 프로세스 메모리가 아니라 '세션 DB 의
# 타이틀'이다. 매 호출 전 session_exists() 로 그 역할 홈에서 hermes-<role> 타이틀 존재를
# 직접 확인한다 → 봇 재기동 후에도 같은 세션이 끊김없이 재개된다.
# `sessions list` 출력에서 타이틀 컬럼을 안전 비교하기 위한 정규식(타이틀이 줄 선두에 옴).
_SESSIONS_HEADER_TOK = ("Title", "Preview", "Last Active", "ID")


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
    # [P0 리셋 차단] clone 으로 끌려온 config 의 session_reset.mode(=both)는 매일 4시 daily
    # 리셋으로 봇 영속 세션을 끊는다. 봇 프로필은 자동 리셋을 받으면 안 되므로 none 으로 고정한다
    # (공식 setup.py 권장 기본값). 메인 ~/.hermes/config.yaml 은 건드리지 않아 사용자 CLI 세션의
    # 일일 리셋은 그대로 유지된다 → 봇 영속성과 사용자 정책이 분리된다.
    _force_session_reset_none(os.path.join(home, "config.yaml"))
    _home_ready[role] = True
    return True


def _force_session_reset_none(config_path):
    """역할 프로필 config.yaml 의 session_reset.mode 를 none 으로 강제(idempotent).
    YAML 라이브러리 의존 없이 라인 단위로 보수적 패치(주석으로 롤백용 원값 보존).
    - `session_reset:` 블록 안의 `mode:` 줄을 `mode: none` 으로 교체.
    - 블록 자체가 없으면 파일 끝에 블록을 추가한다(gateway 기본 both 회피).
    파일 부재/오류 시 조용히 통과(최악의 경우 메인 정책을 따르되, 식별은 title 로 자가복구)."""
    try:
        if not os.path.exists(config_path):
            return False
        with open(config_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        out, in_block, patched, block_seen = [], False, False, False
        for ln in lines:
            stripped = ln.strip()
            # 최상위 키 진입/이탈 판정(들여쓰기 0 + `key:` 형태).
            is_top_key = bool(re.match(r"^[A-Za-z0-9_]+:", ln))
            if is_top_key:
                in_block = stripped.startswith("session_reset:")
                if in_block:
                    block_seen = True
            if in_block and re.match(r"^\s+mode:\s*", ln) and not patched:
                indent = ln[:len(ln) - len(ln.lstrip())]
                if "none" not in ln:
                    out.append(f"{indent}# [P0 봇 영속세션 보존] 자동 리셋 차단(원값 보존): {stripped}")
                out.append(f"{indent}mode: none")
                patched = True
                continue
            out.append(ln)
        if not block_seen:
            # session_reset 블록이 아예 없으면 추가(gateway 가 absent 를 both 로 보는 것 방지).
            out += ["session_reset:",
                    "  # [P0 봇 영속세션 보존] 자동 리셋 차단(블록 부재 → gateway 기본 both 회피).",
                    "  mode: none"]
            patched = True
        if patched:
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
        return patched
    except OSError:
        return False


def _name_session(role, session_id):
    """방금 만든 세션(session_id)을 역할 고정 세션명으로 rename → 이후 --continue 로 재개 가능.
    명명 성공 여부는 프로세스 캐시에 남기지 않는다(진실원천=세션 DB 타이틀). 다음 호출은
    session_exists() 로 DB 를 직접 보고 판단하므로, 봇 재기동에도 영속이 유지된다."""
    if not session_id:
        return False
    r = _run_hermes(["sessions", "rename", session_id, session_name(role)],
                    role=role, timeout=OFFICIAL_TIMEOUT)
    return r is not None and r.returncode == 0


def _parse_session_titles(stdout):
    """`hermes sessions list` stdout(컬럼 정렬 텍스트)에서 타이틀 집합을 뽑는다.
    출력 포맷: 헤더(Title Preview Last Active ID) + 구분선 + 각 행(타이틀이 줄 선두 컬럼).
    타이틀은 줄 맨 앞의 첫 공백 구획 토큰(공백 2칸 이상으로 컬럼이 갈린다). '—'(무명)은 제외.
    파싱은 보수적으로: 헤더/구분선/빈 줄을 건너뛰고, 각 행의 선두 토큰만 본다."""
    titles = set()
    for line in (stdout or "").splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        # 헤더 줄(Title/Preview/... 동시 포함)·구분선(─ 다수)은 건너뛴다.
        if all(tok in s for tok in _SESSIONS_HEADER_TOK):
            continue
        if set(s.strip()) <= set("─-"):
            continue
        # 컬럼은 공백 2칸 이상으로 구분된다. 선두 토큰 = 타이틀 컬럼.
        first = re.split(r"\s{2,}", s.strip(), maxsplit=1)[0].strip()
        if first and first != "—":
            titles.add(first)
    return titles


def session_exists(role):
    """역할 홈(HERMES_HOME)의 세션 DB 에 hermes-<role> 타이틀 세션이 실재하는지 직접 확인.
    [P0 진실원천] 프로세스 메모리 캐시가 아니라 DB 를 본다 → 봇 재기동 후에도 정확히 판단한다.
    list 실패(실행파일 없음/오류)면 보수적으로 False(→ 호출부가 --continue 시도 후 자가복구하거나
    새 세션 생성 경로로 안전하게 떨어진다)."""
    r = _run_hermes(["sessions", "list", "--limit", "200"], role=role, timeout=OFFICIAL_TIMEOUT)
    if r is None or r.returncode != 0:
        return False
    return session_name(role) in _parse_session_titles(r.stdout or "")


# 세션 행의 ID(마지막 컬럼) 형식: 20260625_163510_9d70d8 (날짜_시간_해시). 줄의 마지막 토큰.
_SESSION_ID_RE = re.compile(r"\b(\d{8}_\d{6}_[0-9a-fA-F]+)\b")


def _latest_session_id(role):
    """역할 홈의 `sessions list` 최상단(=가장 최근) 행에서 세션 ID 를 가져온다.
    [근본수정] 새 세션 생성 후 stdout 의 `session_id:` 메타는 --max-turns 상한 도달·경고 출력 등으로
    누락될 수 있다(라이브 실증). 그래서 session_id 캡처를 stdout 이 아니라 DB(list 최상단 ID)에서
    한다 → max-turns 에 걸린 호출도 직후 rename 이 안정적으로 성공해 영속이 보장된다.
    list 의 행은 최신순 정렬이며 ID 는 줄의 마지막 토큰(Title 컬럼 유무와 무관)이다."""
    r = _run_hermes(["sessions", "list", "--limit", "5"], role=role, timeout=OFFICIAL_TIMEOUT)
    if r is None or r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        s = line.strip()
        if not s or all(tok in s for tok in _SESSIONS_HEADER_TOK):
            continue
        if set(s) <= set("─-"):
            continue
        m = _SESSION_ID_RE.search(s)
        if m:
            return m.group(1)  # 첫 데이터 행 = 최신 세션
    return None


def _base_chat_args(query):
    """모든 chat 호출 공통 인자(모델/프로바이더/소스/비용상한/비대화식/query).
    --ignore-rules 는 의도적으로 제외(memory 자동주입을 끄지 않기 위함 — 재귀학습 보존)."""
    args = ["chat",
            "--source", "tool",                       # 의도 태그(보조). 단 DB 에 cli 로 기록되므로
                                                      # 세션 식별엔 쓰지 않는다 → title(hermes-<role>)
                                                      # 단독 식별로 일원화. CLI 풀 오염 회피는
                                                      # 역할별 HERMES_HOME 격리가 담당.
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
      - [P0 영속성] 매 호출 전 session_exists(role) 로 세션 DB 에서 hermes-<role> 타이틀 존재를
        '직접' 확인한다(프로세스 캐시 의존 제거). 있으면 `--continue hermes-<role>` 로 재개해
        같은 역할 메시지가 누적된다(진짜 재귀학습). 봇이 재기동돼도 DB 타이틀은 남으므로 끊김없이
        같은 세션으로 이어진다.
      - 없으면(첫 진입 또는 세션이 정리됨) --continue 없이 새 세션을 만들고 stdout 의 session_id 를
        캡처해 역할 세션명으로 rename → 다음 호출부터 영속.
      - --continue 시도가 'No session found'(reset/prune 직후 경합)면 즉시 생성 경로로 자가복구한다.
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

    # [P0 진실원천] 세션 DB 에 hermes-<role> 타이틀이 실재하면 곧장 --continue 로 재개.
    # 프로세스 메모리가 아니라 DB 를 보므로 봇 재기동 후에도 동일 세션이 끊김없이 이어진다.
    if session_exists(role):
        args = _base_chat_args(query)
        args[1:1] = ["--continue", sname]
        r = _run_hermes(args, role=role, timeout=timeout)
        if r is not None and r.returncode == 0 and _NO_SESSION_MARK not in (r.stdout or ""):
            return r.stdout
        # 세션이 방금 사라졌거나(reset/prune 경합) 재개 실패 → 아래 생성 경로로 자가복구.

    # 세션 생성 경로: --continue 없이 호출(새 세션) → 직후 DB 최상단 ID 로 rename.
    r = _run_hermes(_base_chat_args(query), role=role, timeout=timeout)
    if r is None or r.returncode != 0:
        return None
    out = r.stdout or ""
    # [근본수정] session_id 캡처를 stdout 메타가 아니라 DB(list 최상단)에서 한다.
    # stdout 의 `session_id:` 는 --max-turns 상한 도달·경고로 누락될 수 있어 rename 이 실패하고
    # 매 호출 새 세션이 양산됐다(라이브 실증). DB 최상단 = 방금 만든 세션이므로 안정적이다.
    # 보조: stdout 메타가 있으면 그것을 우선(동일 세션이며 1프로세스 절약).
    m = _SID_RE.search(out)
    sid = m.group(1) if m else _latest_session_id(role)
    if sid:
        _name_session(role, sid)  # 다음 호출부터 DB 타이틀로 인식 → --continue 누적
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
