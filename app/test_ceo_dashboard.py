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
import re
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

    def test_mattermost_base_uses_ipv4_literal_not_localhost(self):
        """회귀: Mattermost REST 베이스는 127.0.0.1(IPv4 리터럴)이어야 한다.

        버그: 'localhost' 를 쓰면 macOS getaddrinfo 가 IPv6 ::1 을 먼저 반환하는데,
        colima ssh 포트포워드는 IPv4(*:8065)만 바인딩하므로 ::1:8065 연결이
        [Errno 61] Connection refused 로 실패한다. 그 결과 대시보드의 모든
        Mattermost 호출(history/post/bot_status)이 끊겨 "연결 안 됨"이 된다.
        ASCII 미러가 stale 해 이 수정이 누락되면 이 단언이 바로 잡아낸다.
        """
        import mm_client as C
        self.assertIn("127.0.0.1", C.MM_BASE)
        self.assertNotIn("localhost", C.MM_BASE)
        self.assertTrue(C.MM_BASE.endswith("/api/v4"))


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


class BotStatusCacheTest(unittest.TestCase):
    """봇 활성 판정 캐시 정책 회귀 테스트.

    버그: _bot_status_cache 가 TTL 없는 영구 캐시라, 서버가 Mattermost 보다 먼저
          기동하거나 기동 시점 502/404 였을 때 첫 bot_status 가 예외→{ok:False}로
          캐시되면 백엔드가 회복돼도 영구 비활성으로 고착됐다.
    근본수정: 실패(ok=False)는 캐시하지 않고, 성공(ok=True)만 짧은 TTL 로 캐시.
    """

    def setUp(self):
        self._orig_mm = D.mm
        self._orig_ttl = D._BOT_STATUS_TTL
        D._bot_status_cache.clear()
        # _bot_id_for 가 실제 *_config.json 을 읽어 유효 bot_id 를 돌려주는
        # config 하나를 자동 선택(데이터 파일 의존을 최소화).
        self._cfg = next(
            (c for c in D.CONFIG_TO_ROLES if c and D._bot_id_for(c)), None
        )

    def tearDown(self):
        D.mm = self._orig_mm
        D._BOT_STATUS_TTL = self._orig_ttl
        D._bot_status_cache.clear()

    def _require_cfg(self):
        if not self._cfg:
            self.skipTest("유효 bot_id 를 가진 *_config.json 이 없어 캐시 경로 검증 불가")
        return self._cfg

    def test_failure_is_not_cached_and_recovers(self):
        """일시 장애로 첫 조회가 실패해도 영구 캐시되지 않고, 백엔드 회복 후 활성 복구."""
        cfg = self._require_cfg()

        class FlakyMM:
            """첫 user() 호출은 장애(예외), 이후 호출은 정상 계정 반환."""

            def __init__(self):
                self.calls = 0

            def user(self, uid):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("backend 502 (Mattermost 기동 전)")
                return {"username": "bot", "delete_at": 0}

        flaky = FlakyMM()
        D.mm = flaky

        # 1) 장애 시점: 비활성으로 보이지만 캐시에 저장되면 안 된다.
        first = D.bot_status(cfg)
        self.assertFalse(first["ok"])
        self.assertNotIn(cfg, D._bot_status_cache)  # 실패는 비캐시

        # 2) 다음 폴링: 백엔드 회복 → 재시도되어 활성으로 자동 복구.
        second = D.bot_status(cfg)
        self.assertTrue(second["ok"])
        self.assertIn(cfg, D._bot_status_cache)  # 성공만 캐시
        self.assertEqual(flaky.calls, 2)  # 실패가 캐시됐다면 재시도가 없었을 것

    def test_success_is_cached_then_refetched_after_ttl(self):
        """성공은 TTL 동안 캐시(중복 호출 절감)되고, TTL 만료 후 재조회된다."""
        cfg = self._require_cfg()

        class CountingMM:
            def __init__(self):
                self.calls = 0

            def user(self, uid):
                self.calls += 1
                return {"username": "bot", "delete_at": 0}

        counting = CountingMM()
        D.mm = counting
        D._BOT_STATUS_TTL = 60.0

        # 1) 첫 조회 성공 → 캐시.
        self.assertTrue(D.bot_status(cfg)["ok"])
        self.assertEqual(counting.calls, 1)

        # 2) TTL 내 재조회: 캐시 히트로 user API 추가 호출 없음.
        self.assertTrue(D.bot_status(cfg)["ok"])
        self.assertEqual(counting.calls, 1)

        # 3) TTL 만료 강제: 다음 조회는 다시 user API 를 호출(계정 상태 변화 반영).
        D._bot_status_cache[cfg]["exp"] = 0.0
        self.assertTrue(D.bot_status(cfg)["ok"])
        self.assertEqual(counting.calls, 2)

    def test_deactivated_account_after_ttl_reflects_inactive(self):
        """TTL 만료 후 계정이 비활성(delete_at!=0)으로 바뀌면 그 변화가 반영된다."""
        cfg = self._require_cfg()

        state = {"delete_at": 0}

        class StatefulMM:
            def user(self, uid):
                return {"username": "bot", "delete_at": state["delete_at"]}

        D.mm = StatefulMM()

        self.assertTrue(D.bot_status(cfg)["ok"])  # 활성 → 캐시
        # 계정 비활성화 + TTL 만료 강제.
        state["delete_at"] = 123456789
        D._bot_status_cache[cfg]["exp"] = 0.0
        # 비활성으로 재판정되고, 실패이므로 캐시에서 제거된다.
        self.assertFalse(D.bot_status(cfg)["ok"])
        self.assertNotIn(cfg, D._bot_status_cache)


class AuthorNameCacheTest(unittest.TestCase):
    """_author_name 의 동일 버그 클래스(실패 fallback 영구 캐시) 회귀 테스트.

    버그: user API 일시 장애 시 'fallback(사람)' 을 _author_cache 에 영구 저장해,
          해당 사용자가 백엔드 회복 뒤에도 계속 '사람' 으로 고착됐다.
    근본수정: 예외 경로는 캐시하지 않고 'fallback' 만 반환 → 다음 조회에서 재시도.
    """

    def setUp(self):
        self._orig_mm = D.mm
        D._author_cache.clear()

    def tearDown(self):
        D.mm = self._orig_mm
        D._author_cache.clear()

    def test_failed_lookup_not_cached_and_recovers(self):
        class FlakyMM:
            def __init__(self):
                self.calls = 0

            def user(self, uid):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("backend 502")
                return {"nickname": "진짜닉네임", "delete_at": 0}

        flaky = FlakyMM()
        D.mm = flaky
        uid = "non-bot-user-xyz"

        # 1) 장애: fallback 반환하되 캐시되면 안 된다.
        self.assertEqual(D._author_name(uid), "사람")
        self.assertNotIn(uid, D._author_cache)

        # 2) 회복: 재시도되어 실제 닉네임 해석 + 그때 캐시.
        self.assertEqual(D._author_name(uid), "진짜닉네임")
        self.assertIn(uid, D._author_cache)
        self.assertEqual(flaky.calls, 2)


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


class RenderedHtmlIntegrityTest(unittest.TestCase):
    """렌더된 대시보드 HTML 의 DOM 무결성 회귀 테스트.

    Bug was: 로그아웃 버튼 무동작 / ceo 로그인 후 화면 깨짐.
    Root cause: topbar/dock-hint 제거 커밋(4dbffbf)이 stageTitle·statAgents·
      svAgents·svPending·svReports·targetName DOM 을 삭제했으나, 이를 가리키는
      getElementById 죽은 참조가 INDEX_HTML JS 에 남았다. 가드 누락 상태에서
      init() IIFE 안 죽은 참조가 TypeError 를 던지면 부트스트랩이 중단되어
      이후 logout 핸들러 바인딩(getElementById('logout'))까지 도달하지 못해
      로그아웃 버튼이 무동작이 된다.
    Fixed in: ceo_dashboard.py build_index_html() — 죽은 참조 6개 전부 제거.
    """

    DOCS = None  # setUpClass 에서 채움

    @classmethod
    def setUpClass(cls):
        cls.DOCS = {"LOGIN": D.LOGIN_HTML, "INDEX": D.INDEX_HTML, "VAULT": D.VAULT_HTML}

    @staticmethod
    def _ids(html):
        return re.findall(r'\bid="([^"]+)"', html)

    @staticmethod
    def _refs(html):
        return set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", html))

    def test_no_duplicate_ids_in_any_document(self):
        for name, html in self.DOCS.items():
            ids = self._ids(html)
            dups = sorted({x for x in ids if ids.count(x) > 1})
            self.assertEqual(dups, [], f"{name} 문서에 중복 id 존재: {dups}")

    def test_no_dead_getElementById_references(self):
        for name, html in self.DOCS.items():
            missing = sorted(self._refs(html) - set(self._ids(html)))
            self.assertEqual(
                missing, [],
                f"{name} 가 정의되지 않은 id 를 getElementById 로 참조(죽은 참조): {missing}")

    def test_login_form_and_handler_present(self):
        self.assertIn('id="loginForm"', D.LOGIN_HTML)
        self.assertIn("fetch('/api/login'", D.LOGIN_HTML)
        self.assertIn("location.href='/'", D.LOGIN_HTML)

    def test_logout_trigger_and_handler_present(self):
        for name in ("INDEX", "VAULT"):
            html = self.DOCS[name]
            self.assertIn('id="logout"', html, f"{name}: 로그아웃 트리거 누락")
            self.assertIn("getElementById('logout').addEventListener", html,
                          f"{name}: 로그아웃 클릭 핸들러 누락")
            self.assertIn("fetch('/api/logout'", html, f"{name}: /api/logout 호출 누락")

    @staticmethod
    def _scripts(html):
        # 인라인 <script> 본문만 추출(외부 src 스크립트는 제외).
        return [
            m.group(1)
            for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                 html, re.DOTALL | re.IGNORECASE)
        ]

    def test_no_broken_backslash_string_literal(self):
        """회귀: 파이썬 triple-quote 안의 JS 백슬래시 이중 이스케이프 누락 방지.

        Bug was: ceo/admin/staff 로그인 후 메인 본문이 통째로 빈칸.
        Root cause: INDEX_HTML 내장 JS 의 키바인딩
          `e.key==='\\\\'` 가 파이썬 문자열에서 백슬래시 1개로 펼쳐져
          서버가 보낸 JS 가 `e.key==='\\'` (종료되지 않은 문자열 리터럴)이 되었다.
          이 한 줄이 <script> 전체를 SyntaxError 로 깨뜨려 init()·렌더 함수가
          정의조차 되지 않아 본문이 비었다.
        Fixed in: build_index_html() — 소스를 `'\\\\\\\\'` 로 바꿔 서버 JS 가
          유효한 `'\\\\'`(이스케이프된 백슬래시 1개) 가 되도록 보정.
        이 테스트는 렌더된 JS 에서 백슬래시 직후 따옴표가 바로 오는
        (= 따옴표를 이스케이프해 문자열이 닫히지 않는) 위험 패턴을 직접 금지한다.
        """
        # 위험 패턴: 홀수 개 백슬래시 뒤에 작은따옴표가 와서 문자열을 종료하지 못함.
        # 가장 단순·확실한 형태인  ='\'  (정확히 backslash 1개 + quote)를 금지.
        bad = re.compile(r"='\\'")
        for name, html in self.DOCS.items():
            for js in self._scripts(html):
                self.assertNotRegex(
                    js, bad,
                    f"{name}: 종료되지 않은 JS 문자열 리터럴 `='\\'` 패턴 발견 "
                    f"(파이썬 백슬래시 이중 이스케이프 누락 회귀)")

    def test_dock_not_absolute_centered_must_be_flex_flow(self):
        """회귀: 통합 입력창(.dock)이 채팅 누적 시 대화를 가리지 않아야 한다.

        Bug was: 빈 채팅뿐 아니라 메시지가 쌓인 채팅 상태에서도 입력창이
          화면 세로 정중앙에 고정돼, 누적된 메시지 위로 떠서 대화 9~10번째
          줄을 가려 '아예 못 쓸 수준'으로 깨졌다.
        Root cause: 입력창 중앙배치 커밋(0db54a6)이 .dock 의 기본 규칙을
          `position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
          z-index:5` 로 바꿔, .dock 을 normal flow 에서 떼어내 .stage 의 세로
          중앙에 영구 고정했다. 채팅 모드에서도 동일 적용 → 입력창이
          스크롤되는 메시지 영역 위에 겹쳐 떠버림.
        Fixed in: ceo_dashboard.py _CSS — .dock 을 normal flex flow
          (`flex:0 0 auto`)로 복구해 .stage-scroll 아래(하단)에 고정.
          빈 상태에서만 `.stage.is-empty .dock{margin-top:auto;margin-bottom:auto}`
          로 normal flow 안에서 세로 중앙 배치(absolute/transform 미사용).

        정적 가드: .dock 기본 규칙이 absolute 센터링으로 회귀하지 않도록 금지.
        """
        css = D._CSS
        # .dock 의 모든 규칙 블록을 수집(셀렉터가 정확히 `.dock` 단독인 것만).
        # @media 안의 `.dock{transition:none}` 같은 부분 오버라이드와 구분해,
        # background 를 지정하는 '기본 레이아웃 규칙' 블록을 식별한다.
        dock_blocks = [
            mm.group(1)
            for mm in re.finditer(r"(?<![\w.-])\.dock\s*\{([^}]*)\}", css)
        ]
        self.assertTrue(dock_blocks, "_CSS 에서 .dock 규칙을 찾지 못함")
        base = [b for b in dock_blocks if "background" in b]
        self.assertEqual(
            len(base), 1,
            f".dock 기본(background 포함) 규칙이 정확히 1개여야 함: {len(base)}개")
        dock_rule = base[0]
        self.assertIn("flex:0 0 auto", dock_rule,
                      ".dock 이 normal flex flow(flex:0 0 auto)가 아님 — "
                      "하단 고정 레이아웃이 깨졌다")
        self.assertNotIn("position:absolute", dock_rule,
                         ".dock 이 absolute 센터링으로 회귀 — 채팅 누적 시 "
                         "입력창이 대화를 가린다")
        self.assertNotIn("translate(-50%,-50%)", dock_rule,
                         ".dock 이 transform 센터링으로 회귀")
        # 빈 상태 센터링 규칙은 normal flow(auto margin)로만 존재해야 한다.
        self.assertRegex(
            css, r"\.stage\.is-empty\s+\.dock\s*\{[^}]*margin-top:auto[^}]*\}",
            "빈 상태 .dock 세로 중앙 규칙(margin-top:auto) 누락")
        # 전체 CSS 어디에도 dock 을 absolute 로 띄우는 규칙이 남지 않도록 가드
        self.assertNotRegex(
            css, r"\.dock\s*\{[^}]*position:absolute",
            ".dock 에 position:absolute 규칙이 남아있음(중앙 고정 회귀 잔재)")

    def test_no_removed_dock_chips_dead_references(self):
        """회귀: 제거된 dock-chips 기능의 죽은 참조가 남지 않아야 한다.

        커밋 54e711f 가 dock-chips 칩(renderDockChips/chipLabel/.dock-chips)을
        의도적으로 제거했다. 이 잔재(함수 호출·DOM id·CSS 클래스)가 남으면
        죽은 참조로 콘솔 에러·레이아웃 잔재를 일으킨다.
        """
        html = D.INDEX_HTML
        for token in ("renderDockChips", "chipLabel", 'id="dockChips"',
                      "getElementById('dockChips')", "class=\"dock-chips\"",
                      "team-chip"):
            self.assertNotIn(token, html,
                             f"INDEX: 제거된 dock-chips 잔재 발견: {token}")

    def test_inline_scripts_parse_as_valid_js(self):
        """회귀: 렌더된 인라인 <script> 가 실제 JS 파서로 파싱되는가.

        node 가 있으면 `node --check` 로 전체 스크립트를 파싱해
        어떤 SyntaxError 도 없음을 실증한다(빈 본문 버그의 근본 게이트).
        node 가 없으면 환경 제약으로 skip.
        """
        import shutil
        import subprocess
        import tempfile
        node = shutil.which("node")
        if not node:
            self.skipTest("node 미설치 — JS 파싱 검증 건너뜀")
        for name, html in self.DOCS.items():
            scripts = self._scripts(html)
            self.assertTrue(scripts, f"{name}: 인라인 <script> 가 없음")
            for idx, js in enumerate(scripts):
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".js", delete=False, encoding="utf-8") as f:
                    f.write(js)
                    path = f.name
                try:
                    proc = subprocess.run(
                        [node, "--check", path],
                        capture_output=True, text=True, timeout=20)
                finally:
                    import os
                    os.unlink(path)
                self.assertEqual(
                    proc.returncode, 0,
                    f"{name} script#{idx} JS 파싱 실패(SyntaxError):\n{proc.stderr}")

    def test_focus_ring_uses_soft_halo_not_opaque_double_border(self):
        """회귀: 입력 요소 focus 가 불투명 이중 테두리로 회귀하지 않아야 한다.

        Bug was: select/textarea/.vault-search input/.login-card input focus 시
          `border-color:var(--blue)` 위에 `box-shadow:0 0 0 2px var(--focus)`
          (불투명 #0071e3 2px 링)이 겹쳐, 파란 테두리 바깥에 또 진한 파란 링이
          이중으로 그려져 투박하게 보였다(.chat-box:focus-within 의 반투명
          후광 패턴과 디자인 언어 불일치).
        Fixed in: _CSS — 4개 focus 규칙 전부 반투명 후광
          `box-shadow:0 0 0 3px var(--accent-soft)` 로 통일. dead 변수 --focus 제거.

        정적 가드: 불투명 --focus 링과 그 변수 정의가 되살아나지 못하게 금지하고,
          focus 후광이 디자인 시스템 토큰(--accent-soft)로만 통일됐는지 확인한다.
        """
        css = D._CSS
        self.assertNotIn("var(--focus)", css,
                         "dead 변수 var(--focus)(불투명 파란 이중 테두리) 회귀")
        self.assertNotIn("--focus:", css,
                         "--focus 변수 정의가 되살아남(dead 토큰)")
        # focus 규칙의 box-shadow 는 반투명 후광(accent-soft)이거나 명시적 none
        # (.chat-box textarea 는 :focus-within 가 후광을 맡아 자체 box-shadow:none)
        # 둘 중 하나여야 한다 — 불투명 색상 링은 금지.
        focus_rules = re.findall(r":focus\s*\{([^}]*box-shadow[^}]*)\}", css)
        self.assertTrue(focus_rules, "box-shadow 를 쓰는 :focus 규칙을 찾지 못함")
        for rule in focus_rules:
            self.assertTrue(
                "var(--accent-soft)" in rule or "box-shadow:none" in rule,
                f"focus box-shadow 가 디자인 토큰(--accent-soft) 후광도 none 도 "
                f"아님(불투명 링 의심): {rule.strip()}")
        # 반투명 후광을 쓰는 :focus 규칙이 최소 1개는 존재해야 한다(통일 증명).
        self.assertTrue(
            any("var(--accent-soft)" in r for r in focus_rules),
            "반투명 후광(--accent-soft)을 쓰는 focus 규칙이 하나도 없음")

    def test_newbadge_pin_selector_is_stable_marker_not_channel_name(self):
        """회귀: CEO브리핑 핀 카드 NEW 배지 셀렉터가 채널명 인코딩에 의존하지 않는다.

        Bug class: 핀 카드 NEW 배지의 속성값은 HTML 이스케이프(esc), 조회 셀렉터는
          CSS.escape 로 서로 다른 인코딩을 써, 채널명에 따옴표 등 특수문자가 들어오면
          셀렉터 불일치로 NEW 배지가 표시되지 않는다. 핀 카드는 단 하나뿐이므로
          채널명 셀렉터 자체가 불필요했다.
        Fixed in: build_index_html() — 고정 마커 data-newbadge-pin="1" 로 교체.

        정적 가드: 깨지기 쉬운 CSS.escape(채널명) 셀렉터가 되살아나지 못하게 금지.
        """
        html = D.INDEX_HTML
        self.assertNotIn("CSS.escape", html,
                         "CSS.escape(채널명) 셀렉터(인코딩 불일치 취약) 회귀")
        self.assertNotIn('data-newbadge="', html,
                         "채널명 기반 data-newbadge 속성(취약 셀렉터) 회귀")
        self.assertIn('data-newbadge-pin="1"', html,
                      "핀 카드 NEW 배지의 고정 마커(data-newbadge-pin) 누락")
        self.assertIn("[data-newbadge-pin=\"1\"]", html,
                      "핀 카드 NEW 배지 조회가 고정 마커 셀렉터를 쓰지 않음")

    def test_logo_mark_has_no_dead_font_size(self):
        """회귀: <img> 로고(.logo-mark)에 무의미한 font-size 잔재가 남지 않아야 한다.

        .logo-mark 는 텍스트 로고에서 <img> SVG 로 교체됐는데, 과거 텍스트 시절의
        font-size 선언이 .nav-head/.login-card 규칙에 죽은 속성으로 남아 있었다.
        이미지에 font-size 는 효과가 없어 코드 위생 차원에서 제거한다.

        정적 가드: .logo-mark 규칙에 font-size 가 다시 들어오지 못하게 금지.
        """
        css = D._CSS
        for rule in re.findall(r"\.logo-mark\s*\{([^}]*)\}", css):
            self.assertNotIn(
                "font-size", rule,
                f"<img> .logo-mark 규칙에 죽은 font-size 잔재: {rule.strip()}")


class PermissionMatrixTest(unittest.TestCase):
    """회귀: 5계정(admin/ceo/e1/e2/e3) 권한 매트릭스가 의도대로 강제되는가.

    Bug class(가드): 권한 경계(channels_for_role·post_channels_for_role)가
      accounts_config.json 의 role/staff_channels 와 어긋나면 직원이 타 부서
      채널을 보거나 게시할 수 있다. E2E 전수 점검에서 실측한 기대 매트릭스를
      단위 테스트로 고정해, 화이트리스트·계정 설정 변경 시 회귀를 잡는다.
    """

    # E2E 실측으로 확정한 계정별 조회/게시 허용 채널.
    EXPECT = {
        "admin": {"CEO브리핑", "인사총무팀", "인사총무-보고라인", "개발팀", "개발-보고라인"},
        "ceo": {"CEO브리핑", "인사총무팀", "인사총무-보고라인", "개발팀", "개발-보고라인"},
        "e1": {"개발팀", "개발-보고라인"},
        "e2": {"인사총무팀", "인사총무-보고라인"},
        "e3": {"개발팀", "개발-보고라인", "인사총무팀", "인사총무-보고라인"},
    }

    @classmethod
    def setUpClass(cls):
        cls.accts = D.ACCOUNTS  # {login_id: account dict}

    def _ident(self, login_id):
        a = self.accts[login_id]
        return {"login_id": login_id, "role": a["role"],
                "label": a.get("label", ""),
                "staff_channels": a.get("staff_channels", [])}

    def test_all_five_accounts_exist(self):
        for uid in self.EXPECT:
            self.assertIn(uid, self.accts, f"계정 {uid} 가 accounts_config 에 없음")

    def test_channels_for_role_matches_matrix(self):
        for uid, exp in self.EXPECT.items():
            got = {c["name"] for c in D.channels_for_role(self._ident(uid))}
            self.assertEqual(got, exp, f"[{uid}] 조회 채널 매트릭스 불일치: {got} != {exp}")

    def test_post_channels_match_view_channels(self):
        # 게시 가능 채널 == 조회 가능 채널(역할별 단일 기준).
        for uid, exp in self.EXPECT.items():
            got = D.post_channels_for_role(self._ident(uid))
            self.assertEqual(got, exp, f"[{uid}] 게시 채널 매트릭스 불일치")

    def test_staff_cannot_reach_other_department_channels(self):
        # e1(개발)은 인사총무 채널을, e2(인사총무)는 개발 채널을 볼 수 없어야 한다.
        e1 = D.post_channels_for_role(self._ident("e1"))
        e2 = D.post_channels_for_role(self._ident("e2"))
        self.assertNotIn("인사총무팀", e1)
        self.assertNotIn("개발팀", e2)
        # 비화이트리스트 채널(정책기획실)은 어떤 계정에도 노출되지 않는다.
        for uid in self.EXPECT:
            self.assertNotIn("정책기획실",
                             D.post_channels_for_role(self._ident(uid)),
                             f"[{uid}] 비화이트리스트 채널 노출")


class _GateHandler:
    """do_GET 인증 게이트만 단위 검증하기 위한 경량 Handler 스텁.

    실제 소켓 없이 _identity 를 주입하고 _html/_json 응답을 (payload, code, headers)
    로 가로채, 미인증 라우팅(302 리다이렉트 vs 401 vs 200 본문)을 직접 검증한다.
    """

    def __init__(self, ident, path):
        self._ident_val = ident
        self.path = path
        self.last = None  # (payload, code, headers)

    def _identity(self):
        return self._ident_val

    def _html(self, html, code=200, extra_headers=None):
        self.last = (html, code, dict(extra_headers or []))
        return self.last

    def _json(self, obj, code=200):
        self.last = (obj, code, {})
        return self.last

    # Vault/me/channels 등 인증 후 분기는 본 테스트 범위 밖이므로
    # 게이트 통과 시 즉시 200 INDEX 로 끝나도록 _html(INDEX) 경로만 탄다.
    do_GET = D.Handler.do_GET
    _send_static = lambda self, *a, **k: ("static", 200, {})  # noqa: E731


class AuthGateRedirectTest(unittest.TestCase):
    """회귀: 미인증 요청의 인증 게이트.

    Bug class(가드): 미인증 GET / 가 200 으로 대시보드 본문을 흘리면 인증 우회.
      반드시 302 → /login 으로 리다이렉트되어야 하고, 미인증 /api/* 는 401.
      인증된 요청은 / 가 200 본문을 받는다. (launchd 미러가 구버전을 들고
      있을 때 게이트가 사라지는 운영 사고 클래스를 코드 레벨에서 고정.)
    """

    CEO = {"role": "ceo", "login_id": "ceo", "label": "CEO",
           "staff_channels": []}

    def _run(self, ident, path):
        h = _GateHandler(ident, path)
        h.do_GET()
        return h.last

    def test_unauth_root_redirects_to_login(self):
        payload, code, headers = self._run(None, "/")
        self.assertEqual(code, 302, "미인증 GET / 가 302 가 아님(본문 유출 위험)")
        self.assertEqual(headers.get("Location"), "/login")

    def test_unauth_api_returns_401(self):
        for p in ("/api/me", "/api/channels", "/api/roles", "/api/history"):
            payload, code, _ = self._run(None, p)
            self.assertEqual(code, 401, f"미인증 {p} 가 401 이 아님")

    def test_unauth_login_page_served(self):
        payload, code, _ = self._run(None, "/login")
        self.assertEqual(code, 200)
        self.assertEqual(payload, D.LOGIN_HTML)

    def test_auth_root_serves_index(self):
        payload, code, _ = self._run(self.CEO, "/")
        self.assertEqual(code, 200)
        self.assertEqual(payload, D.INDEX_HTML)

    def test_auth_login_redirects_home(self):
        # 이미 로그인한 사용자가 /login 가면 / 로 돌려보낸다.
        payload, code, headers = self._run(self.CEO, "/login")
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
