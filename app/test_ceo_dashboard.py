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


if __name__ == "__main__":
    unittest.main(verbosity=2)
