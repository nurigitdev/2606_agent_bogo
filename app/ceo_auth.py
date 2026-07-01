"""
인증·세션 모듈 — CEO 대시보드 로그인/역할 분기 기반.

설계 (이중 인증 경로, 신규 시크릿·API 키 0):
  1차) Mattermost /api/v4/users/login (body {login_id, password}) 로 실제 서버 인증을
       시도. 성공 시 응답 헤더 Token 과 user(role/email/username) 를 세션에 보관해
       그 사용자 컨텍스트로 history/post 호출이 가능하다(봇 토큰 폴백 유지).
  2차) accounts_config.json 의 3계정 자격증명으로 로컬 검증(견고한 폴백). Mattermost 에
       해당 계정이 없거나 서버 미가동이어도 데모 3계정은 항상 로그인된다.
  둘 중 하나라도 통과하면 로그인 허용. role 은 config 매핑을 우선한다(권한의 단일 진실).

보안:
  - 비밀번호 평문 저장·로깅 금지. PBKDF2-HMAC-SHA256(200k iters, per-account salt) 해시만 저장.
  - 비교는 hmac.compare_digest 로 상수시간(타이밍 공격 완화).
  - 세션 토큰은 secrets.token_urlsafe(추측 불가). 서버 메모리(딕셔너리) 저장.
  - 쿠키는 호출측(HTTP 핸들러)에서 HttpOnly·SameSite=Strict 로 설정.

데모 기본값: 세 계정 모두 비밀번호 1111 (accounts_config.json 의 pw_hash 로 검증).
"""
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# NOTE: localhost(=::1 우선 해석) 대신 127.0.0.1 강제.
# colima ssh 포트포워드가 IPv4(*:8065)만 바인딩해 ::1 로는 Errno 61 refused 가 난다.
MM_BASE = "http://127.0.0.1:8065/api/v4"

# 세션 수명(초). 만료 세션은 검증 시 폐기한다.
SESSION_TTL = 12 * 3600
# PBKDF2 파라미터(accounts_config.json 생성 시와 동일해야 함).
_PBKDF2_ITERS = 200_000


def _hash_pw(password, salt_hex):
    """PBKDF2-HMAC-SHA256 해시(hex). salt 는 hex 문자열."""
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), _PBKDF2_ITERS)
    return dk.hex()


def load_accounts(path=None):
    """accounts_config.json -> {login_id: account dict}. 평문 비밀번호는 존재하지 않는다.

    파일이 없거나(신선 설치) 손상됐으면 빈 dict 를 돌려준다(하드 크래시 금지).

    WHY (근본 원인): accounts_config.json 은 운영자 로컬 시크릿이라 git 제외 대상이고,
      bootstrap 의 config copy 목록·프로비저닝 어디에도 이 파일을 생성하는 경로가 없다.
      과거엔 load_accounts 가 파일을 무조건 open 해, 신선 설치(파일 부재)에서 대시보드가
      기동 즉시 FileNotFoundError 로 죽어 systemd 가 무한 재시작하고 [5/5] dashboard
      health check 가 실패해 파이프라인 전체가 중단됐다(신선 systemd 실측). 그러나 인증은
      이중 경로(1차 Mattermost 로그인 + 2차 로컬 config 폴백)로 설계돼 있어, 로컬 config 가
      비어도 프로비저닝된 admin 계정으로 Mattermost 인증 경로가 정상 동작한다. 따라서 파일
      부재를 '폴백 계정 0개'로 해석해 graceful degrade 하는 것이 옳다 → 이 버그 클래스
      (신선 설치에서 미프로비저닝 시크릿 파일 부재로 핵심 서비스가 크래시루프)을 근절한다.
      운영자가 나중에 accounts_config.json 을 채우면 로컬 폴백 계정이 그대로 활성화된다.
    """
    path = path or os.path.join(HERE, "accounts_config.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {a["login_id"]: a for a in data.get("accounts", []) if isinstance(a, dict) and a.get("login_id")}


def verify_local(login_id, password, accounts):
    """config 폴백 검증. 성공 시 account dict, 실패 시 None. 상수시간 비교."""
    acct = accounts.get((login_id or "").strip())
    if not acct:
        return None
    expected = acct.get("pw_hash", "")
    got = _hash_pw(password or "", acct["salt"])
    if hmac.compare_digest(got, expected):
        return acct
    return None


def mm_login(login_id, password, timeout=5):
    """Mattermost 실제 인증(1차 경로).

    성공 시 {'token': <세션토큰>, 'user': <user json>} 반환, 실패/미가동 시 None.
    토큰은 응답 헤더 Token 에 담겨 온다.
    """
    body = json.dumps({"login_id": login_id, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        MM_BASE + "/users/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            token = resp.headers.get("Token")
            user = json.loads(resp.read())
        if not token:
            return None
        return {"token": token, "user": user}
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        # 계정 없음(401)·서버 미가동 모두 여기로. 폴백이 처리한다.
        return None


def authenticate(login_id, password, accounts=None, allow_mm=True):
    """이중 경로 인증. 성공 시 신원 dict, 실패 시 None.

    반환 신원: {login_id, role, label, staff_channels, source, mm_token, mm_user}
      - role 은 config 매핑 우선(권한 단일 진실). config 에 없고 mm 만 성공한 계정은
        mm 의 roles 문자열에서 admin 포함 여부로 ceo/staff 를 보수적으로 판정.
      - mm_token: Mattermost 로그인 성공 시 그 사용자 토큰(없으면 None → 봇 토큰 폴백).
    """
    accounts = load_accounts() if accounts is None else accounts
    login_id = (login_id or "").strip()
    mm_res = mm_login(login_id, password) if allow_mm else None
    local = verify_local(login_id, password, accounts)
    if not mm_res and not local:
        return None

    if local:
        # config 계정: role/label/채널은 config 가 단일 진실.
        return {
            "login_id": login_id,
            "role": local["role"],
            "label": local.get("label", ""),
            "staff_channels": local.get("staff_channels", []),
            "source": "mm+config" if mm_res else "config",
            "mm_token": mm_res["token"] if mm_res else None,
            "mm_user": mm_res["user"] if mm_res else None,
        }
    # config 에 없지만 mm 인증만 통과한 사용자: 보수적 role 판정.
    user = mm_res["user"]
    role = "ceo" if "system_admin" in (user.get("roles", "")) else "staff"
    return {
        "login_id": login_id,
        "role": role,
        "label": user.get("nickname") or user.get("username", ""),
        "staff_channels": [],
        "source": "mm",
        "mm_token": mm_res["token"],
        "mm_user": user,
    }


class SessionStore:
    """서버측 세션 저장소(메모리). 토큰은 추측 불가. 만료 시 폐기."""

    def __init__(self, ttl=SESSION_TTL):
        self._ttl = ttl
        self._sessions = {}  # token -> {identity, created}

    def create(self, identity):
        """신원으로 세션 생성 → 쿠키에 실을 토큰 반환."""
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {"identity": identity, "created": time.time()}
        return token

    def get(self, token):
        """유효 세션의 신원 반환. 없거나 만료면 None(만료는 폐기)."""
        if not token:
            return None
        s = self._sessions.get(token)
        if not s:
            return None
        # >= 비교: ttl=0(즉시 만료) 의도가 같은 시각 틱에서도 항상 성립하도록 한다.
        # (> 비교면 같은 부동소수 time() 값일 때 0.0 > 0 == False 로 만료가 새어 통과됨.)
        if time.time() - s["created"] >= self._ttl:
            self._sessions.pop(token, None)
            return None
        return s["identity"]

    def destroy(self, token):
        """로그아웃: 세션 폐기."""
        self._sessions.pop(token, None)
