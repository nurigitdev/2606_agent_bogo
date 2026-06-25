"""
ceo_dashboard 핵심 경로 테스트 (표준 unittest — 신규 의존성 0, 네트워크 0).

검증 대상:
  - 채널 화이트리스트가 channels.json/teams.json 에서 동적 구성되는가(하드코딩 아님)
  - 루프백(127.0.0.1) 바인드 상수가 보존되는가
  - fetch_history 가 mm.history 원시({order,posts})를 시간순으로 평탄화하는가
  - post_message 가 화이트리스트·빈문자·길이 검증을 하는가
  - 화이트리스트 밖 채널은 read/post 모두 거부되는가

네트워크 호출은 mm 클라이언트 메서드를 스텁으로 교체해 차단한다.
"""
import unittest

import ceo_dashboard as D


class FakeMM:
    """mm_client.MM 대체 스텁 — 네트워크 없이 history/post/user 흉내."""

    def __init__(self):
        self.posted = []

    def history(self, cid, n=8):
        # API 와 동일하게 최신→과거 순서. 시스템 메시지(type) 1건 포함.
        return {
            "order": ["p3", "p2", "p1", "sys"],
            "posts": {
                "p1": {"user_id": "u1", "message": "첫 메시지", "create_at": 1000},
                "p2": {"user_id": "u2", "message": "둘째", "create_at": 2000},
                "p3": {"user_id": "u1", "message": "셋째", "create_at": 3000},
                "sys": {"user_id": "u1", "message": "joined", "create_at": 500, "type": "system_join"},
            },
        }

    def post(self, cid, message):
        self.posted.append((cid, message))
        return {"id": "newpost123"}

    def user(self, uid):
        return {"username": "tester", "nickname": "테스터", "delete_at": 0}


class WhitelistTest(unittest.TestCase):
    def test_whitelist_from_data_files(self):
        # teams.json 의 team/report 채널 + orchestrator 브리핑이 화이트리스트에 들어가야 한다.
        names = {c["name"] for c in D.WHITELIST}
        self.assertIn("CEO브리핑", names)
        self.assertIn("개발팀", names)
        self.assertIn("인사총무팀", names)
        self.assertIn("개발-보고라인", names)
        # 개조 전용 학습방은 모니터링 화이트리스트에서 제외(역할별 ceo_admin 파이프라인 전용).
        self.assertNotIn("비서실-학습방", names)
        self.assertNotIn("개발-학습방", names)

    def test_whitelist_ids_match_channels_json(self):
        for c in D.WHITELIST:
            self.assertEqual(c["id"], D.CHANNELS[c["name"]])

    def test_default_post_channel_is_briefing(self):
        self.assertEqual(D.DEFAULT_POST_CHANNEL, "CEO브리핑")


class BindTest(unittest.TestCase):
    def test_loopback_only(self):
        # 외부 노출 회귀 방지: 반드시 루프백.
        self.assertEqual(D.HOST, "127.0.0.1")
        self.assertNotEqual(D.HOST, "0.0.0.0")


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMM()
        self._orig = D.mm
        D.mm = self.fake
        D._author_cache.clear()

    def tearDown(self):
        D.mm = self._orig

    def test_history_time_ascending_and_no_system(self):
        items = D.fetch_history("개발팀", n=8)
        # 시스템 메시지 제외 → 3건.
        self.assertEqual(len(items), 3)
        # 시간 오름차순(과거→최신).
        ts = [m["ts"] for m in items]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(items[0]["text"], "첫 메시지")
        self.assertEqual(items[-1]["text"], "셋째")

    def test_history_rejects_unlisted_channel(self):
        with self.assertRaises(ValueError):
            D.fetch_history("존재하지않는채널", n=5)


class PostTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMM()
        self._orig = D.mm
        D.mm = self.fake

    def tearDown(self):
        D.mm = self._orig

    def test_post_ok(self):
        res = D.post_message("CEO브리핑", "테스트 지시")
        self.assertEqual(res["id"], "newpost123")
        self.assertEqual(len(self.fake.posted), 1)
        self.assertEqual(self.fake.posted[0][0], D.CHANNELS["CEO브리핑"])

    def test_post_rejects_empty(self):
        with self.assertRaises(ValueError):
            D.post_message("CEO브리핑", "   ")

    def test_post_rejects_too_long(self):
        with self.assertRaises(ValueError):
            D.post_message("CEO브리핑", "x" * 4001)

    def test_post_rejects_unlisted_channel(self):
        # 화이트리스트(팀/보고라인/브리핑) 밖 채널은 post_message 가 막는다.
        # 학습방(개조 전용)도 모니터링 화이트리스트에 없어 차단된다.
        with self.assertRaises(ValueError):
            D.post_message("비서실-학습방", "막혀야 함")
        self.assertEqual(len(self.fake.posted), 0)


class RosterTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMM()
        self._orig = D.mm
        D.mm = self.fake
        D._bot_status_cache.clear()

    def tearDown(self):
        D.mm = self._orig

    def test_roster_lists_registered_roles(self):
        r = D.roster()
        roles = {x["role"] for x in r}
        # agents/*.md 에 정의된 role 들이 모두 노출되어야 한다.
        self.assertTrue({"dev", "hr", "orchestrator"}.issubset(roles))
        for x in r:
            self.assertIn("bot_active", x)
            self.assertIn("name", x)


# ── Vault(누적 기억) 통합 테스트 ────────────────────────────────────────────────
class _FakeHandler:
    """Handler 의 Vault 라우트 메서드만 떼어 단위 테스트하기 위한 경량 믹스인 호스트.

    BaseHTTPRequestHandler 를 실제 소켓으로 띄우지 않고, _json 응답을 (obj, code) 로
    가로채 라우트 로직(권한 게이트·필터·traversal·degrade)을 직접 검증한다.
    """

    def __init__(self):
        self.last = None  # (obj, code)

    def _json(self, obj, code=200):
        self.last = (obj, code)
        return self.last

    def _html(self, html, code=200, extra_headers=None):
        self.last = (html, code)
        return self.last

    # 실제 Handler 의 미바인드 메서드를 그대로 빌려 self 에 묶어 호출한다.
    _vault_allowed = D.Handler._vault_allowed
    _get_vault_list = D.Handler._get_vault_list
    _get_vault_search = D.Handler._get_vault_search
    _get_vault_note = D.Handler._get_vault_note


def _u(query):
    """urlparse 결과 흉내(.query 만 쓰므로 충분)."""
    from urllib.parse import urlparse
    return urlparse("/x?" + query)


CEO = {"role": "ceo", "login_id": "ceo@x", "label": "CEO"}
STAFF = {"role": "staff", "login_id": "sw9@x", "staff_channels": []}
ADMIN = {"role": "admin", "login_id": "admin", "label": "관리자"}


class VaultAuthGateTest(unittest.TestCase):
    """신규 Vault 엔드포인트가 ceo/admin 인증 뒤에서만 동작하는지(직원 차단)."""

    def setUp(self):
        self.h = _FakeHandler()

    def test_staff_blocked_on_list(self):
        self.h._get_vault_list(_u(""), STAFF)
        obj, code = self.h.last
        self.assertEqual(code, 403)

    def test_staff_blocked_on_search(self):
        self.h._get_vault_search(_u("q=test"), STAFF)
        self.assertEqual(self.h.last[1], 403)

    def test_staff_blocked_on_note(self):
        self.h._get_vault_note(_u("path=foo.md"), STAFF)
        self.assertEqual(self.h.last[1], 403)

    def test_ceo_allowed(self):
        self.assertTrue(self.h._vault_allowed(CEO))
        self.assertTrue(self.h._vault_allowed(ADMIN))
        self.assertFalse(self.h._vault_allowed(STAFF))


class VaultListTest(unittest.TestCase):
    """브라우징: 노트 목록 + 파셋 + RAG 상태가 인증된 CEO 에게 렌더되는가."""

    def setUp(self):
        self.h = _FakeHandler()

    def test_list_returns_notes_and_facets(self):
        self.h._get_vault_list(_u(""), CEO)
        obj, code = self.h.last
        self.assertEqual(code, 200)
        self.assertIn("notes", obj)
        self.assertIn("facets", obj)
        self.assertIn("rag", obj)
        # 실제 vault 에 노트가 존재하므로 1개 이상 회수되어야 한다(MVP 아님 실증).
        self.assertGreater(len(obj["notes"]), 0)
        for n in obj["notes"]:
            self.assertIn("path", n)
            self.assertIn("title", n)
            self.assertTrue(n["path"].endswith(".md"))

    def test_list_role_filter(self):
        self.h._get_vault_list(_u("role=dev"), CEO)
        obj, _ = self.h.last
        for n in obj["notes"]:
            self.assertEqual(n["role"], "dev")


class VaultSearchTest(unittest.TestCase):
    """RAG 검색: 인증된 CEO 가 검색 → 결과/모드/사유가 반환되는가."""

    def setUp(self):
        self.h = _FakeHandler()

    def test_search_renders_results_or_degrade(self):
        self.h._get_vault_search(_u("q=" + "LLM"), CEO)
        obj, code = self.h.last
        self.assertEqual(code, 200)
        self.assertIn("ok", obj)
        self.assertIn("mode", obj)
        self.assertIn("results", obj)
        # RAG 인덱스가 있으면 ok=True. 결과는 노트 링크 형식(path)을 가진다.
        if obj["ok"]:
            for r in obj["results"]:
                self.assertIn("path", r)

    def test_empty_query_returns_empty_not_error(self):
        self.h._get_vault_search(_u("q="), CEO)
        obj, code = self.h.last
        self.assertEqual(code, 200)
        self.assertEqual(obj["results"], [])


class VaultNoteTraversalTest(unittest.TestCase):
    """노트 열람: 정상 경로는 본문, traversal/이탈 경로는 400 으로 차단."""

    def setUp(self):
        self.h = _FakeHandler()

    def test_valid_note_readable(self):
        # 먼저 목록에서 실제 경로 하나를 얻어 그 노트를 연다.
        notes = D.list_vault_notes(limit=1)
        self.assertTrue(notes, "vault 에 노트가 있어야 테스트 가능")
        rel = notes[0]["path"]
        self.h._get_vault_note(_u("path=" + rel), CEO)
        obj, code = self.h.last
        self.assertEqual(code, 200)
        self.assertIn("body", obj)
        self.assertIn("frontmatter", obj)
        self.assertIn("obsidian_uri", obj)

    def test_traversal_rejected(self):
        for bad in ["../ceo_auth.py", "../../etc/passwd", "/etc/passwd",
                    "..%2F..%2Fsecret.md", "foo.txt"]:
            self.h._get_vault_note(_u("path=" + bad), CEO)
            obj, code = self.h.last
            self.assertIn(code, (400, 404),
                          f"traversal/이탈 경로가 차단되지 않음: {bad} -> {code}")

    def test_nonexistent_note_404(self):
        self.h._get_vault_note(_u("path=20_Reports/does_not_exist.md"), CEO)
        self.assertEqual(self.h.last[1], 404)


class VaultDegradeTest(unittest.TestCase):
    """RAG 모듈/인덱스 부재 시 대시보드가 500 대신 '인덱스 없음'으로 강등되는가."""

    def test_search_degrade_when_rag_module_missing(self):
        orig = D.VR
        try:
            D.VR = None  # 모듈 로드 실패 상황 시뮬레이션
            res = D.vault_search("아무거나")
            self.assertFalse(res["ok"])
            self.assertEqual(res["results"], [])
            self.assertTrue(res["reason"])  # 사유 안내 존재
        finally:
            D.VR = orig

    def test_rag_status_reports_mode(self):
        st = D.vault_rag_status()
        self.assertIn("ok", st)
        self.assertIn("mode", st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
