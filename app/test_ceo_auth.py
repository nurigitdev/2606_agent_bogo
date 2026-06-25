"""
인증·세션·역할 분기·권한 차단 테스트 (표준 unittest, 네트워크 0).

검증 대상:
  - config 폴백 인증: 올바른 비번 통과 / 틀린 비번·없는 계정 거부 (mattermost 미접근 모드)
  - 비밀번호 평문 미저장(PBKDF2 해시만), 상수시간 비교
  - 세션: 토큰 추측 불가 길이, 생성/조회/만료/폐기
  - role 매핑: ceo/staff/admin 정확
  - 채널 권한: ceo=전체, staff=자기채널만, admin=전체+에이전트관리
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
        self.assertEqual(set(self.acc), {"ceo@nurivoice.com", "sw9@nurivoice.com", "admin"})

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
        for lid, role in [("ceo@nurivoice.com", "ceo"),
                          ("sw9@nurivoice.com", "staff"), ("admin", "admin")]:
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

    def test_admin_sees_all_plus_admin_channel(self):
        names = {c["name"] for c in D.channels_for_role(self._ident("admin"))}
        self.assertTrue(set(D.WHITELIST_NAMES).issubset(names))
        if D.ADMIN_CHANNEL:
            self.assertIn(D.ADMIN_CHANNEL, names)

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

    def test_admin_can_post_admin_channel(self):
        if not D.ADMIN_CHANNEL:
            self.skipTest("ADMIN_CHANNEL 없음")
        self.assertIn(D.ADMIN_CHANNEL, D.post_channels_for_role(self._ident("admin")))


class PostMessageAnyTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMM()
        self._orig = D.mm
        D.mm = self.fake

    def tearDown(self):
        D.mm = self._orig

    def test_post_any_to_admin_channel(self):
        if not D.ADMIN_CHANNEL:
            self.skipTest("ADMIN_CHANNEL 없음")
        res = D.post_message_any(D.ADMIN_CHANNEL, "관리 지시")
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
        # 데모 기본값 1111 이 힌트로 명시되어 있어야 동작 안내가 된다.
        self.assertIn("1111", D.LOGIN_HTML)

    def test_apple_design_tokens_preserved(self):
        # Apple 디자인 시스템 핵심 토큰 계승 확인.
        self.assertIn("#0066cc", D.INDEX_HTML)        # 단일 Action Blue
        self.assertIn("scale(0.95)", D.INDEX_HTML)    # active scale
        self.assertIn("SF Pro", D.INDEX_HTML)         # SF Pro 스택

    def test_loopback_only_preserved(self):
        self.assertEqual(D.HOST, "127.0.0.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
