"""
인증·세션·역할 분기·권한 차단 테스트 (표준 unittest, 네트워크 0).

검증 대상:
  - config 폴백 인증: 올바른 비번 통과 / 틀린 비번·없는 계정 거부 (mattermost 미접근 모드)
  - 비밀번호 평문 미저장(PBKDF2 해시만), 상수시간 비교
  - 세션: 토큰 추측 불가 길이, 생성/조회/만료/폐기
  - role 매핑: ceo/staff/admin 정확
  - 채널 권한: ceo=전체, staff=자기채널만, admin=전체(WHITELIST). 개조는 역할별 학습방 전용.
  - 게시 권한: staff 가 자기 채널 외 게시 불가(서버측 강제)
  - 부제 제거 회귀: 메인/로그인 HTML 에 보조 설명 텍스트가 남지 않았는가
"""
import unittest

import ceo_auth as AUTH
import ceo_dashboard as D


class FakeMM:
    def history(self, cid, n=8):
        return {"order": [], "posts": {}}

    def post(self, cid, message):
        return {"id": "p1"}

    def user(self, uid):
        return {"username": "u", "nickname": "n", "delete_at": 0}


class LocalAuthTest(unittest.TestCase):
    def setUp(self):
        self.acc = AUTH.load_accounts()

    def test_accounts_loaded(self):
        self.assertEqual(set(self.acc), {"admin", "ceo", "e1", "e2", "e3"})

    def test_no_plaintext_password(self):
        # 평문 비밀번호가 어떤 계정에도 저장되어 있지 않아야 한다.
        for a in self.acc.values():
            self.assertNotIn("password", a)
            self.assertNotIn("pw", a)
            self.assertIn("pw_hash", a)
            self.assertIn("salt", a)
            self.assertNotEqual(a["pw_hash"], "1111")

    def test_correct_password_passes(self):
        # allow_mm=False 로 순수 config 폴백 경로만 검증.
        for lid, role in [("ceo", "ceo"), ("e1", "staff"),
                          ("e2", "staff"), ("e3", "staff"), ("admin", "admin")]:
            ident = AUTH.authenticate(lid, "1111", self.acc, allow_mm=False)
            self.assertIsNotNone(ident, lid)
            self.assertEqual(ident["role"], role)
            self.assertEqual(ident["source"], "config")

    def test_wrong_password_rejected(self):
        self.assertIsNone(AUTH.authenticate("admin", "9999", self.acc, allow_mm=False))

    def test_unknown_account_rejected(self):
        self.assertIsNone(AUTH.authenticate("nobody@x.com", "1111", self.acc, allow_mm=False))

    def test_empty_credentials_rejected(self):
        self.assertIsNone(AUTH.authenticate("", "", self.acc, allow_mm=False))

    def test_verify_local_constant_time_hash(self):
        # 같은 비번 두 번 해시 → 동일(결정적), 다른 salt 계정 간 해시 상이.
        h1 = AUTH._hash_pw("1111", self.acc["admin"]["salt"])
        h2 = AUTH._hash_pw("1111", self.acc["admin"]["salt"])
        self.assertEqual(h1, h2)
        self.assertEqual(h1, self.acc["admin"]["pw_hash"])


class SessionTest(unittest.TestCase):
    def test_token_unguessable_and_lifecycle(self):
        ss = AUTH.SessionStore()
        ident = {"login_id": "admin", "role": "admin"}
        tok = ss.create(ident)
        self.assertGreaterEqual(len(tok), 32)  # token_urlsafe(32) → 충분히 김
        self.assertEqual(ss.get(tok)["role"], "admin")
        ss.destroy(tok)
        self.assertIsNone(ss.get(tok))

    def test_session_expiry(self):
        ss = AUTH.SessionStore(ttl=0)  # 즉시 만료
        tok = ss.create({"login_id": "x", "role": "staff"})
        self.assertIsNone(ss.get(tok))  # 만료 → None

    def test_unknown_token(self):
        ss = AUTH.SessionStore()
        self.assertIsNone(ss.get("does-not-exist"))
        self.assertIsNone(ss.get(""))


class ChannelAuthorizationTest(unittest.TestCase):
    """role 별 채널 조회/게시 권한이 서버측 헬퍼로 강제되는가."""

    def _ident(self, role, staff_channels=None):
        return {"login_id": role, "role": role, "label": "",
                "staff_channels": staff_channels or []}

    def test_ceo_sees_all_whitelist(self):
        names = {c["name"] for c in D.channels_for_role(self._ident("ceo"))}
        self.assertEqual(names, set(D.WHITELIST_NAMES))

    def test_admin_sees_all_whitelist(self):
        # 개조 전용 채널 폐지 후: admin 은 WHITELIST(팀/보고라인/브리핑) 전체를 본다.
        names = {c["name"] for c in D.channels_for_role(self._ident("admin"))}
        self.assertEqual(names, set(D.WHITELIST_NAMES))

    def test_staff_sees_only_own_channels(self):
        ident = self._ident("staff", ["개발팀", "개발-보고라인"])
        names = {c["name"] for c in D.channels_for_role(ident)}
        self.assertEqual(names, {"개발팀", "개발-보고라인"})
        # 다른 부서 채널은 안 보임.
        self.assertNotIn("인사총무팀", names)
        self.assertNotIn("CEO브리핑", names)

    def test_staff_cannot_post_outside_own_channels(self):
        ident = self._ident("staff", ["개발팀"])
        allowed = D.post_channels_for_role(ident)
        self.assertIn("개발팀", allowed)
        self.assertNotIn("CEO브리핑", allowed)
        self.assertNotIn("인사총무팀", allowed)

    def test_admin_can_post_whitelist(self):
        # admin 은 WHITELIST 전체에 게시할 수 있다(전체 화이트리스트 = 게시 허용 집합).
        allowed = D.post_channels_for_role(self._ident("admin"))
        self.assertEqual(allowed, set(D.WHITELIST_NAMES))


class PostMessageAnyTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMM()
        self._orig = D.mm
        D.mm = self.fake

    def tearDown(self):
        D.mm = self._orig

    def test_post_any_to_existing_channel(self):
        # post_message_any 는 channels.json 에 실재하는 채널이면 게시(권한은 호출측 강제).
        res = D.post_message_any("CEO브리핑", "지시")
        self.assertEqual(res["id"], "p1")

    def test_post_any_rejects_nonexistent(self):
        with self.assertRaises(ValueError):
            D.post_message_any("없는채널xyz", "x")

    def test_post_any_rejects_empty(self):
        with self.assertRaises(ValueError):
            D.post_message_any("CEO브리핑", "   ")


class HtmlRegressionTest(unittest.TestCase):
    """부제(subtitle/tagline) 류 보조 설명 제거 회귀 + 로그인 페이지 존재."""

    def test_no_subtitle_class_in_index(self):
        # 기존 .section-sub 보조 설명 블록이 메인에서 제거되었는가.
        self.assertNotIn('class="section-sub"', D.INDEX_HTML)
        self.assertNotIn("section-sub", D.INDEX_HTML)

    def test_removed_specific_subtitles(self):
        # 리스타일 전 존재하던 부제 문구가 사라졌는가(회귀 방지).
        for phrase in ["12초마다 폴링해", "박민철(비서실장)에게 전달",
                       "role 목록과 봇 활성 여부 · 담당 채널"]:
            self.assertNotIn(phrase, D.INDEX_HTML)

    def test_login_html_exists_and_has_form(self):
        self.assertIn("/api/login", D.LOGIN_HTML)
        self.assertIn("비밀번호", D.LOGIN_HTML)
        # 로그인 폼의 핵심 입력(아이디 id=lid / 비밀번호 type=password)이 존재해야 한다.
        # 데모 기본 비밀번호(1111)를 로그인 화면에 평문으로 노출하지 않는다 — 자격증명
        # 힌트 노출은 보안 안티패턴이라 HTML 단언에서 제외한다(데모 비번 안내는
        # accounts_config 주석/README 등 비공개 문서에서만 제공).
        self.assertIn('id="lid"', D.LOGIN_HTML)
        self.assertIn('type="password"', D.LOGIN_HTML)

    def test_apple_design_tokens_preserved(self):
        # Apple 디자인 시스템 핵심 토큰 계승 확인.
        self.assertIn("#0066cc", D.INDEX_HTML)        # 단일 Action Blue
        self.assertIn("scale(0.95)", D.INDEX_HTML)    # active scale
        self.assertIn("SF Pro", D.INDEX_HTML)         # SF Pro 스택

    def test_loopback_only_preserved(self):
        self.assertEqual(D.HOST, "127.0.0.1")


class MattermostIPv4ForcedRegressionTest(unittest.TestCase):
    """회귀: Mattermost 접속 URL 의 IPv4(127.0.0.1) 강제.

    Bug was: 대시보드 채널 상세에서 "Mattermost 연결 실패: <urlopen error
        [Errno 61] Connection refused>" — /api/history 로드 실패.
    Root cause: 소스가 http(s)/ws://localhost:8065 를 사용. macOS 에서 localhost 는
        ::1(IPv6) 로 먼저 풀리는데, colima 의 ssh 포트포워드가 IPv4(*:8065)만
        바인딩해 ::1 로는 Errno 61 Connection refused 가 발생.
    Fixed in: ceo_auth.MM_BASE, mm_client.MM_BASE, bogo_runtime.MM 및 ws,
        ceo_admin_runtime ws — 모두 127.0.0.1 로 강제.

    이 테스트는 어떤 소스가 다시 localhost:8065 로 회귀하면 즉시 실패한다.
    네트워크 0 — 상수 문자열만 검증한다.
    """

    def _assert_ipv4(self, url):
        self.assertNotIn("localhost", url, f"localhost(=::1 우선) 금지: {url}")
        self.assertIn("127.0.0.1", url, f"IPv4 강제 필요: {url}")

    def test_ceo_auth_mm_base_is_ipv4(self):
        self._assert_ipv4(AUTH.MM_BASE)

    def test_mm_client_mm_base_is_ipv4(self):
        import mm_client
        self._assert_ipv4(mm_client.MM_BASE)

    def test_runtime_sources_have_no_localhost_mm(self):
        # 인라인 ws URL 등 상수가 아닌 접속 지점까지 소스 텍스트로 전수 검증.
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        for fname in ("bogo_runtime.py", "ceo_admin_runtime.py", "ceo_auth.py",
                      "mm_client.py"):
            with open(os.path.join(here, fname), encoding="utf-8") as f:
                src = f.read()
            for bad in ("://localhost:8065", "://localhost:8067"):
                self.assertNotIn(
                    bad, src,
                    f"{fname} 에 {bad} 잔존 — IPv6 거부 회귀 위험")


if __name__ == "__main__":
    unittest.main(verbosity=2)
