#!/usr/bin/env python3
"""
Mattermost 무인 프로비저닝 — 새 PC 더블클릭 한 번으로 토큰류 전부 자동 발급·기록.

WHY (근본 원인):
  기존엔 폴더만 옮기면 봇이 떠 있는 통신 백본(Colima→bogo-mm)까지는 자동이었지만,
  (a)관리자/팀/봇 계정 (b)각 봇 Access Token (c)필요 채널 ID 는 사람이 손으로
  만들어 *_config.json / channels.json 에 적어야만 동작했다(시크릿이라 git 제외).
  이 스크립트가 그 공백을 메운다 — bogo-mm 컨테이너의 mmctl --local(로컬 소켓,
  인증 불필요. docker-compose 의 MM_SERVICESETTINGS_ENABLELOCALMODE=true 로 활성)을
  통해 전부 생성하고 토큰을 발급해 설정 파일에 기록한다.

무엇을 (전부 멱등 — 이미 있으면 skip, 재실행 안전):
  1) 관리자 계정 생성(없으면)               — admin / .env BOGO_ADMIN_*
  2) 팀 생성(없으면)                          — .env BOGO_TEAM_NAME/-DISPLAY
  3) 봇 계정 3개 생성(없으면)                 — minchul/daeun/jihyun (agents/*.md 에서 도출)
  4) 각 봇 Access Token 발급 후 *_config.json 기록 (bot_id 동시 기록)
  5) 봇을 팀·필요 채널에 가입 + 시스템관리자 권한 부여(운영봇 nk)
  6) 필요 채널 9종 생성(없으면) 후 channels.json 에 ID 기록
  7) 봇을 각 채널 멤버로 추가(구독·송신 성립)

멱등 판정의 진실원은 'Mattermost 의 실재 상태'다. 설정 파일 값이 placeholder/빈값이거나
실재 토큰이 더 이상 유효하지 않으면 다시 발급한다(파일만 보고 skip 하지 않는다).

회귀 없음: 기존에 토큰이 이미 채워져 동작 중인 PC 에서 실행해도, 실재 계정/채널을 재사용하고
유효 토큰은 보존한다(불필요한 재발급 없음). 시크릿은 파일에만 쓰고 표준출력에 절대 노출하지 않는다.

사용:
  python provision_mm.py                 # 컨테이너 mmctl --local 경유(기본)
종료코드 0=성공, 1=프로비저닝 실패(상위 런처가 봇을 띄우지 않게 함).
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import agent_schema as A
import mm_client as C

HERE = os.path.dirname(os.path.abspath(__file__))
# 토큰 유효성 확인(_token_valid)용 REST 베이스. 호스트/포트는 mm_client 단일 진실원
# (MM_HOST/MM_PORT)에서 — 기본 127.0.0.1:8065. mmctl 자체는 컨테이너 내부 --local
# 소켓으로 돌아 이 주소와 무관하다(아래 mmctl 래퍼 참조).
MM_BASE = C.mm_http_base()
MM_CONTAINER = os.environ.get("BOGO_MM_CONTAINER", "bogo-mm")

# 운영(시스템관리자) 봇 = 오케스트레이터 config. 대시보드도 이 봇 토큰을 쓴다.
ADMIN_BOT_CONFIG = "nk"

# ── .env 에서 읽는 프로비저닝 자격증명(전부 합리적 기본값 → 묻지 않고 자동 진행) ──
ADMIN_USER = os.environ.get("BOGO_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("BOGO_ADMIN_PASS", "Bogo-Admin-1111!")
ADMIN_EMAIL = os.environ.get("BOGO_ADMIN_EMAIL", "admin@bogo.local")
TEAM_NAME = os.environ.get("BOGO_TEAM_NAME", "bogo")           # 슬러그(영문)
TEAM_DISPLAY = os.environ.get("BOGO_TEAM_DISPLAY", "BOGO")
BOT_EMAIL_DOMAIN = os.environ.get("BOGO_BOT_EMAIL_DOMAIN", "bogo.local")

# 한글 채널명 → Mattermost 슬러그(name) 매핑. sync_channels.py 의 학습방 슬러그와 일치.
CHANNEL_SLUG = {
    "인사총무팀": "hr-team",
    "개발팀": "dev-team",
    "인사총무-보고라인": "hr-report",
    "개발-보고라인": "dev-report",
    "CEO브리핑": "ceo-briefing",
    "정책기획실": "policy-planning",
    "인사총무-학습방": "hr-learn",
    "개발-학습방": "dev-learn",
    "비서실-학습방": "orchestrator-learn",
}


def say(msg):
    print(f"[provision] {msg}", flush=True)


# ════════════════════════════════════════════════════════════════════════
# mmctl 래퍼 — 두 경로
#   (A) --local 소켓: 인증 불필요. user/team/channel 생성·조회는 여기서.
#   (B) 인증 세션(auth): bot create / token generate 는 mmctl 이 --local 에서
#       명시적으로 막아 둔다("This command cannot be run in local mode",
#       mattermost/mattermost#36353). 그래서 admin(=--local 로 생성) 자격으로
#       서버 로그인한 인증 컨텍스트를 1회 만들어 그 경로로 실행한다.
#   두 경로 모두 컨테이너 내부에서 돈다(127.0.0.1:8065 — 외부 노출 없음).
# ════════════════════════════════════════════════════════════════════════
_AUTH_READY = False
AUTH_CTX = "bogo-provision"   # mmctl 인증 컨텍스트 이름(컨테이너 내부 임시)


def mmctl(*args, check=True):
    """--local 소켓 경로. 반환: (rc, stdout)."""
    cmd = ["docker", "exec", MM_CONTAINER, "mmctl", "--local", *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise RuntimeError(f"mmctl --local {' '.join(args)} 실패(rc={p.returncode}): {out.strip()[:300]}")
    return p.returncode, (p.stdout or "")


def mmctl_json(*args):
    """mmctl --local ... --json 실행 후 JSON 파싱. 실패/비-JSON 이면 None."""
    rc, out = mmctl(*args, "--json", check=False)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _ensure_auth_session():
    """admin 자격으로 컨테이너 내부 mmctl 인증 세션 1회 확립(bot/token 용).
    이미 만들어졌으면 재사용. 비밀번호는 인자로만 전달하고 출력하지 않는다."""
    global _AUTH_READY
    if _AUTH_READY:
        return
    # 컨테이너 내부에서 MM 자신(127.0.0.1:8065)으로 로그인. localhost 대신 127.0.0.1
    # 강제(프로젝트 IPv4 가드와 일치 — ::1 우선해석 회귀 차단).
    # NOTE: --name 은 `auth login` 에서만 유효한 플래그다(자격증명 이름 부여).
    # login 은 --no-activate 가 없으면 방금 만든 컨텍스트를 활성 컨텍스트로 설정한다.
    subprocess.run(
        ["docker", "exec", MM_CONTAINER, "mmctl", "auth", "login",
         "http://127.0.0.1:8065", "--name", AUTH_CTX,
         "--username", ADMIN_USER, "--password", ADMIN_PASS],
        capture_output=True, text=True)
    # 다른 활성 컨텍스트가 있더라도 우리 컨텍스트를 명시적으로 활성화(결정성 보장).
    # `auth set <name>` 이 컨텍스트를 전환하는 올바른 명령이다(per-command --name 은 없음).
    subprocess.run(
        ["docker", "exec", MM_CONTAINER, "mmctl", "auth", "set", AUTH_CTX],
        capture_output=True, text=True)
    # login 실패해도 여기서 죽지 않는다 — 실제 bot/token 호출에서 명확히 드러난다.
    _AUTH_READY = True


def mmctl_auth(*args, check=True):
    """인증 세션 경로(bot create / token generate). 세션을 보장한 뒤 실행.

    활성 컨텍스트(=_ensure_auth_session 에서 auth set 으로 고정한 AUTH_CTX)로
    서버 모드 실행한다. mmctl 에는 커맨드별 컨텍스트 선택용 --name 플래그가 없으므로
    (auth login 전용) 여기서 --name 을 붙이면 'unknown flag: --name' 로 실패한다."""
    _ensure_auth_session()
    cmd = ["docker", "exec", MM_CONTAINER, "mmctl", *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise RuntimeError(f"mmctl(auth) {' '.join(a for a in args if a != ADMIN_PASS)} "
                           f"실패(rc={p.returncode}): {out.strip()[:300]}")
    return p.returncode, (p.stdout or "")


def mmctl_auth_json(*args):
    """인증 세션 + --json 파싱. 실패/비-JSON 이면 None."""
    rc, out = mmctl_auth(*args, "--json", check=False)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _exists(items, key, value):
    """mmctl --json 리스트에서 key==value 항목 존재 여부."""
    if not isinstance(items, list):
        return False
    return any(isinstance(it, dict) and it.get(key) == value for it in items)


# ════════════════════════════════════════════════════════════════════════
# 단계별 멱등 보장
# ════════════════════════════════════════════════════════════════════════
def ensure_mmctl_available():
    """컨테이너에 mmctl 과 로컬모드가 살아있는지 확인(가장 신뢰도 높은 readiness)."""
    rc, _ = mmctl("system", "version", check=False)
    if rc != 0:
        raise RuntimeError(
            "mmctl --local 사용 불가. bogo-mm 컨테이너가 떴는지, docker-compose 의 "
            "MM_SERVICESETTINGS_ENABLELOCALMODE=true 인지 확인하세요.")


def ensure_admin():
    """시스템관리자 계정 생성(멱등). 이미 있으면 skip."""
    users = mmctl_json("user", "list") or []
    if _exists(users, "username", ADMIN_USER):
        say(f"관리자 '{ADMIN_USER}' 이미 존재 — skip.")
        return
    mmctl("user", "create", "--email", ADMIN_EMAIL, "--username", ADMIN_USER,
          "--password", ADMIN_PASS, "--system-admin", check=True)
    say(f"관리자 '{ADMIN_USER}' 생성 완료(system-admin).")


def ensure_team():
    """팀 생성(멱등). 슬러그 TEAM_NAME 기준."""
    teams = mmctl_json("team", "list") or []
    if _exists(teams, "name", TEAM_NAME):
        say(f"팀 '{TEAM_NAME}' 이미 존재 — skip.")
        return
    mmctl("team", "create", "--name", TEAM_NAME, "--display-name", TEAM_DISPLAY,
          "--email", ADMIN_EMAIL, check=True)
    say(f"팀 '{TEAM_NAME}'({TEAM_DISPLAY}) 생성 완료.")


def _bot_specs():
    """agents/*.md 에서 (config_stem, username, display_name) 도출. 단일 진실원."""
    specs = []
    for role, meta in A.load_roles().items():
        cfg = meta.get("config")
        user = meta.get("username")
        name = meta.get("name", user)
        if cfg and user:
            specs.append((cfg, user, name))
    # config 중복 제거(같은 봇 계정을 여러 role 이 공유할 수 있음).
    seen, uniq = set(), []
    for cfg, user, name in specs:
        if cfg in seen:
            continue
        seen.add(cfg)
        uniq.append((cfg, user, name))
    return uniq


def _user_id_by_name(username):
    """username 의 user_id. 없으면 None."""
    u = mmctl_json("user", "search", username)
    if isinstance(u, list) and u:
        return u[0].get("id")
    if isinstance(u, dict):
        return u.get("id")
    return None


def _extract_token(text):
    """mmctl 출력(텍스트/JSON 혼재)에서 26자 이상 영숫자 토큰 1개를 뽑는다."""
    try:
        j = json.loads(text)
        if isinstance(j, dict):
            t = j.get("token") or (j.get("data") or {}).get("token")
            if t:
                return t
    except (json.JSONDecodeError, TypeError):
        pass
    for line in text.splitlines():
        # "Token: xxxx" 또는 토큰만 있는 라인 모두 처리.
        cand = line.strip().split(":")[-1].strip()
        if len(cand) >= 26 and cand.isalnum():
            return cand
    return None


def ensure_bot(config_stem, username, display_name):
    """봇 계정 생성(멱등) + Access Token 발급 + nk 는 system-admin 부여.
    bot create / token generate 는 --local 에서 막혀 있어 인증 세션 경로(mmctl_auth)로 실행한다.
    반환: (bot_user_id, token). 기존 유효 토큰이 파일에 있고 실재 검증되면 보존(불필요 재발급 회피)."""
    # 봇 생성은 인증 세션 경로. --with-token 으로 생성과 동시에 PAT 를 발급받는다(평문 1회 출력).
    bots = mmctl_auth_json("bot", "list") or []
    created_token = None
    if not _exists(bots, "username", username):
        _, out = mmctl_auth("bot", "create", username,
                            "--display-name", display_name, "--with-token", check=True)
        created_token = _extract_token(out)
        say(f"봇 '{username}'({display_name}) 생성 완료(+token).")
    else:
        say(f"봇 '{username}' 이미 존재 — skip 생성.")

    bot_user_id = _user_id_by_name(username)
    if not bot_user_id:
        raise RuntimeError(f"봇 '{username}' user_id 조회 실패(생성 직후).")

    # 운영봇(nk)은 학습방 자기추가·게시 등에 system-admin 권한이 필요하다(멱등 부여).
    if config_stem == ADMIN_BOT_CONFIG:
        mmctl("user", "roles", "system_admin", username, check=False)

    cfg_path = os.path.join(HERE, f"{config_stem}_config.json")
    existing = _read_json(cfg_path)
    tok = existing.get("bot_token", "")
    # 1) 기존 토큰이 실재 검증되면 보존.
    if tok and not tok.endswith("HERE") and _token_valid(tok):
        say(f"봇 '{username}' 기존 토큰 유효 — 보존.")
        _write_json(cfg_path, {"bot_token": tok, "bot_id": bot_user_id})
        return bot_user_id, tok
    # 2) 방금 생성하며 받은 토큰이 있으면 사용.
    new_tok = created_token
    # 3) 없으면(이미 존재하던 봇 등) 토큰을 새로 발급(인증 세션 경로).
    if not new_tok:
        _, out = mmctl_auth("token", "generate", username, "bogo-runtime", check=True)
        new_tok = _extract_token(out)
    if not new_tok:
        raise RuntimeError(f"봇 '{username}' Access Token 발급 실패.")
    _write_json(cfg_path, {"bot_token": new_tok, "bot_id": bot_user_id})
    say(f"봇 '{username}' Access Token 발급·기록 완료(config={config_stem}).")
    return bot_user_id, new_tok


def add_bots_to_team(bot_ids):
    """봇들을 팀 멤버로 추가(멱등). mmctl 은 이미 멤버여도 무해."""
    for username in bot_ids:
        mmctl("team", "users", "add", TEAM_NAME, username, check=False)


def ensure_channels(bot_usernames):
    """필요 채널 9종 생성(멱등) 후 channels.json 에 ID 기록, 봇 멤버 추가."""
    channels = _read_json(os.path.join(HERE, "channels.json"))
    existing_chs = mmctl_json("channel", "list", TEAM_NAME) or []
    by_name = {c.get("name"): c for c in existing_chs if isinstance(c, dict)}

    for display, slug in CHANNEL_SLUG.items():
        ch = by_name.get(slug)
        if not ch:
            # NOTE: channel create 는 --display_name(언더스코어) 플래그를 쓴다(mmctl 규약).
            mmctl("channel", "create", "--team", TEAM_NAME, "--name", slug,
                  "--display_name", display, check=False)
            # 생성 직후 재조회로 ID 확보.
            ch = _channel_by_slug(slug)
            say(f"채널 '{display}'(slug={slug}) 생성.")
        else:
            say(f"채널 '{display}'(slug={slug}) 이미 존재 — skip.")
        cid = (ch or {}).get("id")
        if cid:
            channels[display] = cid
            for username in bot_usernames:
                mmctl("channel", "users", "add",
                      f"{TEAM_NAME}:{slug}", username, check=False)

    _write_json(os.path.join(HERE, "channels.json"), channels)
    missing = [d for d in CHANNEL_SLUG if not channels.get(d)]
    if missing:
        raise RuntimeError(f"채널 ID 미확보: {missing}")
    say(f"채널 {len(CHANNEL_SLUG)}종 ID 기록 완료(channels.json).")


def _channel_by_slug(slug):
    chs = mmctl_json("channel", "list", TEAM_NAME) or []
    for c in chs:
        if isinstance(c, dict) and c.get("name") == slug:
            return c
    return None


# ════════════════════════════════════════════════════════════════════════
# 일반 직원 계정 프로비저닝(층간 모드 — 직원이 자기 팀 채널에 보고를 올리는 흐름)
#   employees.json 명단을 읽어 mmctl --local 로 멱등 생성하고, 각 직원을 소속 팀
#   채널 멤버로 배치한다. 파일이 없으면(단일 PC 데모) 조용히 skip 한다.
#   비밀번호는 인자로만 전달하고 표준출력에 절대 노출하지 않는다.
# ════════════════════════════════════════════════════════════════════════
def ensure_employees():
    """employees.json 의 직원 계정 생성(멱등) + 소속 팀 채널 멤버 배치.

    멱등 규칙(provision 전반과 동일): Mattermost 실재 상태가 진실원. 이미 있으면 생성
    skip, 팀/채널 가입은 mmctl 이 이미 멤버여도 무해하므로 매 실행 보장한다.
    team_channels 는 channels.json 의 한글 채널명 → CHANNEL_SLUG 로 슬러그 변환한다.
    channels.json/CHANNEL_SLUG 에 없는 채널은 경고 후 skip(라우팅 정합 보호).
    """
    emp_path = os.path.join(HERE, "employees.json")
    data = _read_json(emp_path)
    employees = data.get("employees", []) if isinstance(data, dict) else []
    if not employees:
        say("employees.json 없음/비어있음 — 직원 계정 프로비저닝 skip(단일 PC 데모는 정상).")
        return

    existing = mmctl_json("user", "list") or []

    for emp in employees:
        if not isinstance(emp, dict):
            continue
        username = (emp.get("username") or "").strip()
        if not username:
            say("직원 항목에 username 누락 — skip.")
            continue
        email = (emp.get("email") or f"{username}@{BOT_EMAIL_DOMAIN}").strip()
        display = (emp.get("display_name") or username).strip()
        password = emp.get("password") or ""

        # 1) 계정 생성(멱등). 이미 있으면 skip(비밀번호 재설정하지 않음 — 운영자 변경 보존).
        if _exists(existing, "username", username):
            say(f"직원 '{username}' 이미 존재 — 생성 skip.")
        else:
            if not password:
                say(f"직원 '{username}' 신규 생성 실패 — password 누락(employees.json 확인).")
                continue
            # --nickname 으로 표시 이름 지정(대시보드/멘션에서 사람 이름으로 보이게).
            mmctl("user", "create", "--email", email, "--username", username,
                  "--password", password, "--nickname", display, check=True)
            say(f"직원 '{username}'({display}) 생성 완료.")

        # 2) 팀 가입(멱등). 채널에 들어가려면 먼저 팀 멤버여야 한다.
        mmctl("team", "users", "add", TEAM_NAME, username, check=False)

        # 3) 소속 팀 채널 멤버 배치(멱등). 한글 채널명 → 슬러그.
        for ch_display in emp.get("team_channels", []):
            slug = CHANNEL_SLUG.get(ch_display)
            if not slug:
                say(f"직원 '{username}': 미정의 채널 '{ch_display}' — skip(CHANNEL_SLUG 정합 확인).")
                continue
            mmctl("channel", "users", "add",
                  f"{TEAM_NAME}:{slug}", username, check=False)
        say(f"직원 '{username}' 팀·채널 멤버십 보장 완료.")

    say(f"직원 계정 {len(employees)}명 프로비저닝 완료(멱등).")


# ════════════════════════════════════════════════════════════════════════
# 토큰 유효성(REST 로 1회 확인) + JSON 파일 IO
# ════════════════════════════════════════════════════════════════════════
def _token_valid(token):
    """봇 토큰이 MM 에서 아직 유효한지 /users/me 로 확인."""
    try:
        req = urllib.request.Request(
            MM_BASE + "/users/me",
            headers={"Authorization": f"Bearer {token}"}, method="GET")
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ════════════════════════════════════════════════════════════════════════
def main():
    say("Mattermost 무인 프로비저닝 시작(mmctl --local, 전부 멱등).")
    try:
        ensure_mmctl_available()
        ensure_admin()
        ensure_team()
        specs = _bot_specs()
        bot_usernames = []
        for config_stem, username, display_name in specs:
            ensure_bot(config_stem, username, display_name)
            bot_usernames.append(username)
        add_bots_to_team(bot_usernames)
        ensure_channels(bot_usernames)
        # 층간 모드: 일반 직원 계정 + 소속 팀 채널 배치(employees.json 있을 때만).
        ensure_employees()
    except RuntimeError as e:
        say(f"실패: {e}")
        return 1
    say("프로비저닝 완료 — 토큰·채널 자동 발급·기록됨. 봇 기동 가능.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
