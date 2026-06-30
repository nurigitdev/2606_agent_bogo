"""net_autodetect 단위 테스트 — NIC 파싱·RFC1918 판정·모드 결정·.env 멱등 upsert.

네트워크 0(순수 함수/파싱만): ip 명령을 실제로 부르지 않고, 다양한 `ip -o -4`
출력 샘플(멀티홈/단일/공인혼재/도커가상포함)을 입력으로 분류·판정을 검증한다.
.env upsert 는 임시 파일로 멱등성과 비밀 비파괴를 검증한다.
"""
import os
import tempfile
import unittest

import net_autodetect as N

# ── 실제 `ip -o -4 addr show` 출력 샘플 ───────────────────────────────────
# 멀티홈: 사설 NIC 3장(A/B/C) + lo + docker0(가상).
SAMPLE_MULTIHOME = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 10.0.0.10/24 brd 10.0.0.255 scope global eth0\\       valid_lft forever preferred_lft forever
3: eth1    inet 172.16.0.10/24 brd 172.16.0.255 scope global eth1\\       valid_lft forever preferred_lft forever
4: eth2    inet 192.168.50.10/24 brd 192.168.50.255 scope global eth2\\       valid_lft forever preferred_lft forever
5: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever preferred_lft forever
"""

# 단일망: 사설 NIC 1장 + lo + veth(가상).
SAMPLE_SINGLE = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: enp3s0    inet 192.168.0.50/24 brd 192.168.0.255 scope global enp3s0\\       valid_lft forever preferred_lft forever
3: vethabc123    inet 169.254.1.1/16 scope link vethabc123\\       valid_lft forever preferred_lft forever
"""

# 공인 혼재: 공인 NIC 1장(인터넷 향) + 사설 NIC 2장. 멀티홈 자동활성 중단 대상.
# 공인 IP 는 '글로벌 라우팅' 대역이어야 한다(203.0.113/24 는 TEST-NET-3 문서용 예약
# 대역이라 is_global=False → 공인으로 안 잡힘). 실제 글로벌 라우팅 IP 로 둔다.
SAMPLE_PUBLIC_MIXED = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 198.51.45.5/24 brd 198.51.45.255 scope global eth0\\       valid_lft forever preferred_lft forever
3: eth1    inet 10.0.0.10/24 brd 10.0.0.255 scope global eth1\\       valid_lft forever preferred_lft forever
4: eth2    inet 172.16.0.10/24 brd 172.16.0.255 scope global eth2\\       valid_lft forever preferred_lft forever
"""

# Tailscale 혼재: CGNAT(100.x) 오버레이는 물리 사설 NIC 로 오분류되면 안 됨.
SAMPLE_TAILSCALE = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 192.168.0.50/24 brd 192.168.0.255 scope global eth0\\       valid_lft forever preferred_lft forever
3: tailscale0    inet 100.101.102.103/32 scope global tailscale0\\       valid_lft forever preferred_lft forever
"""

# 루프백 전용: 물리 사설 NIC 0(단일 PC 데모).
SAMPLE_LOOPBACK_ONLY = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever preferred_lft forever
"""


class RFC1918Test(unittest.TestCase):
    def test_private_ranges(self):
        for ip in ("10.0.0.1", "10.255.255.254", "172.16.0.1", "172.31.255.1",
                   "192.168.0.1", "192.168.255.254"):
            self.assertTrue(N.is_rfc1918(ip), f"{ip} 는 RFC1918 사설이어야 함")

    def test_non_private(self):
        # 172.32.x 는 172.16/12 밖(공인), 100.x 는 CGNAT, 203.x 는 공인.
        for ip in ("172.32.0.1", "100.64.0.1", "203.0.113.5", "8.8.8.8",
                   "169.254.1.1", "127.0.0.1"):
            self.assertFalse(N.is_rfc1918(ip), f"{ip} 는 RFC1918 사설이 아니어야 함")

    def test_public_detection(self):
        # 글로벌 라우팅 대역만 공인. 198.51.45.x 는 글로벌(문서용 198.51.100/24 아님).
        self.assertTrue(N.is_public_ipv4("198.51.45.5"))
        self.assertTrue(N.is_public_ipv4("8.8.8.8"))
        # 사설·CGNAT·링크로컬·루프백·문서용 예약(TEST-NET)은 공인 아님(보수적 판정).
        for ip in ("10.0.0.10", "192.168.0.1", "172.16.0.1", "100.64.0.1",
                   "169.254.1.1", "127.0.0.1", "203.0.113.5", "198.51.100.5"):
            self.assertFalse(N.is_public_ipv4(ip), f"{ip} 가 공인으로 오판정됨")


class ParseTest(unittest.TestCase):
    def test_parse_ip_o_multihome(self):
        pairs = N.parse_ip_o_output(SAMPLE_MULTIHOME)
        self.assertIn(("eth0", "10.0.0.10"), pairs)
        self.assertIn(("eth1", "172.16.0.10"), pairs)
        self.assertIn(("eth2", "192.168.50.10"), pairs)
        self.assertIn(("docker0", "172.17.0.1"), pairs)
        self.assertIn(("lo", "127.0.0.1"), pairs)

    def test_parse_ifconfig_fallback(self):
        sample = (
            "eth0: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500\n"
            "        inet 10.0.0.10  netmask 255.255.255.0  broadcast 10.0.0.255\n"
            "lo: flags=73<UP,LOOPBACK,RUNNING>  mtu 65536\n"
            "        inet 127.0.0.1  netmask 255.0.0.0\n"
        )
        pairs = N.parse_ifconfig_output(sample)
        self.assertIn(("eth0", "10.0.0.10"), pairs)
        self.assertIn(("lo", "127.0.0.1"), pairs)


class ClassifyTest(unittest.TestCase):
    def test_multihome_excludes_virtual(self):
        c = N.classify_interfaces(N.parse_ip_o_output(SAMPLE_MULTIHOME))
        # 물리 사설 NIC 3장만 private 에. docker0·lo 는 제외.
        self.assertEqual(
            c["private"],
            [("eth0", "10.0.0.10"), ("eth1", "172.16.0.10"),
             ("eth2", "192.168.50.10")],
        )
        self.assertEqual(c["public"], [])
        self.assertIn(("docker0", "172.17.0.1"), c["virtual"])

    def test_tailscale_not_counted_as_private(self):
        c = N.classify_interfaces(N.parse_ip_o_output(SAMPLE_TAILSCALE))
        # 물리 사설 NIC 1장만, tailscale0(CGNAT 오버레이)은 virtual.
        self.assertEqual(c["private"], [("eth0", "192.168.0.50")])
        self.assertIn(("tailscale0", "100.101.102.103"), c["virtual"])

    def test_public_mixed_classification(self):
        c = N.classify_interfaces(N.parse_ip_o_output(SAMPLE_PUBLIC_MIXED))
        self.assertEqual(c["public"], [("eth0", "198.51.45.5")])
        self.assertEqual(len(c["private"]), 2)


class DecideModeTest(unittest.TestCase):
    def test_multihome_mode_and_env(self):
        d = N.decide_mode(N.classify_interfaces(N.parse_ip_o_output(SAMPLE_MULTIHOME)))
        self.assertEqual(d["mode"], "multihome")
        env = d["env"]
        self.assertEqual(env["BOGO_MULTIHOME"], "1")
        self.assertEqual(env["MM_BIND_HOST"], "0.0.0.0")
        self.assertEqual(env["BOGO_DASHBOARD_HOST"], "0.0.0.0")
        # 대표(첫) NIC = SiteURL, 나머지 2개 = CORS 공백 구분.
        self.assertEqual(env["MM_SITE_URL"], "http://10.0.0.10:8065")
        self.assertEqual(
            env["MM_ALLOW_CORS_FROM"],
            "http://172.16.0.10:8065 http://192.168.50.10:8065",
        )

    def test_single_lan_mode_binds_specific_ip_not_wildcard(self):
        d = N.decide_mode(N.classify_interfaces(N.parse_ip_o_output(SAMPLE_SINGLE)))
        self.assertEqual(d["mode"], "lan")
        env = d["env"]
        # 단일망은 0.0.0.0 가 아니라 그 사설 IP 로만 바인딩(회귀 가드).
        self.assertEqual(env["MM_BIND_HOST"], "192.168.0.50")
        self.assertEqual(env["BOGO_DASHBOARD_HOST"], "192.168.0.50")
        self.assertEqual(env["BOGO_MULTIHOME"], "0")
        self.assertEqual(env["MM_ALLOW_CORS_FROM"], "")

    def test_public_mixed_triggers_guard_no_wildcard(self):
        # 공인 NIC 가 있으면 멀티홈 0.0.0.0 자동활성을 중단(인터넷 노출 방지).
        d = N.decide_mode(N.classify_interfaces(N.parse_ip_o_output(SAMPLE_PUBLIC_MIXED)))
        self.assertEqual(d["mode"], "guard")
        # 가드 모드는 네트워크 키를 건드리지 않는다(기존 .env = 루프백 유지).
        self.assertEqual(d["env"], {})

    def test_loopback_mode_when_no_physical_private(self):
        d = N.decide_mode(N.classify_interfaces(N.parse_ip_o_output(SAMPLE_LOOPBACK_ONLY)))
        self.assertEqual(d["mode"], "loopback")
        self.assertEqual(d["env"]["MM_BIND_HOST"], "127.0.0.1")
        self.assertEqual(d["env"]["BOGO_MULTIHOME"], "0")


class EnvUpsertTest(unittest.TestCase):
    """.env 멱등 upsert — 비밀 비파괴 + 재실행 결과 동일 + in-place 교체."""

    MULTIHOME_ENV = {
        "BOGO_MULTIHOME": "1",
        "MM_BIND_HOST": "0.0.0.0",
        "MM_SITE_URL": "http://10.0.0.10:8065",
        "MM_ALLOW_CORS_FROM": "http://172.16.0.10:8065 http://192.168.50.10:8065",
        "BOGO_DASHBOARD_HOST": "0.0.0.0",
    }

    def test_preserves_secrets_and_manual_values(self):
        original = (
            "LLM_BACKEND=openrouter\n"
            "OPENROUTER_API_KEY=sk-or-v1-SECRET\n"
            "BOGO_ADMIN_PASS=Manual-Pass-1111!\n"
        )
        new = N.upsert_env_text(original, self.MULTIHOME_ENV)
        # 비밀·수동값은 한 글자도 변하지 않아야 한다.
        self.assertIn("OPENROUTER_API_KEY=sk-or-v1-SECRET", new)
        self.assertIn("BOGO_ADMIN_PASS=Manual-Pass-1111!", new)
        self.assertIn("LLM_BACKEND=openrouter", new)
        # 네트워크 키는 자동 블록에 주입됨.
        self.assertIn("BOGO_MULTIHOME=1", new)
        self.assertIn("MM_BIND_HOST=0.0.0.0", new)
        # 공백 포함 CORS 값은 따옴표로 감싸 set -a 안전.
        self.assertIn(
            'MM_ALLOW_CORS_FROM="http://172.16.0.10:8065 http://192.168.50.10:8065"',
            new,
        )

    def test_idempotent_reapply_is_stable(self):
        original = "OPENROUTER_API_KEY=sk-or-v1-SECRET\n"
        once = N.upsert_env_text(original, self.MULTIHOME_ENV)
        twice = N.upsert_env_text(once, self.MULTIHOME_ENV)
        self.assertEqual(once, twice, "재적용 시 .env 가 달라짐(멱등 위반)")
        # 자동 블록이 한 번만 존재(중복 누적 없음).
        self.assertEqual(twice.count(N._BLOCK_BEGIN), 1)

    def test_inplace_replace_of_active_key(self):
        # 사용자가 본문에 활성으로 둔 키는 그 자리에서 값만 교체(블록으로 중복 추가 안 함).
        original = (
            "MM_BIND_HOST=192.168.0.99\n"
            "OPENROUTER_API_KEY=sk-or-v1-SECRET\n"
        )
        new = N.upsert_env_text(original, self.MULTIHOME_ENV)
        # 기존 활성 라인이 0.0.0.0 으로 교체되고, 중복 라인이 없어야 한다.
        self.assertEqual(new.count("MM_BIND_HOST="), 1)
        self.assertIn("MM_BIND_HOST=0.0.0.0", new)
        self.assertNotIn("MM_BIND_HOST=192.168.0.99", new)

    def test_commented_key_is_not_touched_but_block_added(self):
        # 주석(비활성) 키는 그대로 두고, 활성값은 자동 블록에 주입(.env.example 유래).
        original = "# MM_BIND_HOST=0.0.0.0\nOPENROUTER_API_KEY=sk-or-v1-SECRET\n"
        new = N.upsert_env_text(original, self.MULTIHOME_ENV)
        self.assertIn("# MM_BIND_HOST=0.0.0.0", new)  # 주석 보존
        # 활성 값은 블록에 들어가 실제 적용됨.
        self.assertIn(N._BLOCK_BEGIN, new)

    def test_crlf_line_endings_are_preserved(self):
        # 회귀 가드: Windows 에서 편집된 .env(CRLF)나 CRLF .env.example 을 시드해도
        # 비밀·수동값 라인의 \r 을 삼키지 않아야 한다('비밀값 한 글자도 안 건드림' 불변식).
        # 과거엔 splitlines()+"\n".join 으로 CRLF 가 LF 로 강제 변환돼 비밀 라인이 변형됐다.
        original = (
            "OPENROUTER_API_KEY=sk-or-v1-SECRET\r\n"
            "BOGO_ADMIN_PASS=Manual-Pass-1111!\r\n"
        )
        new = N.upsert_env_text(original, self.MULTIHOME_ENV)
        # 비밀 라인이 CRLF 그대로 보존(LF 로 강등되지 않음).
        self.assertIn("OPENROUTER_API_KEY=sk-or-v1-SECRET\r\n", new)
        self.assertIn("BOGO_ADMIN_PASS=Manual-Pass-1111!\r\n", new)
        # 외톨이 LF(\r 없는 줄바꿈)가 비밀 라인에 끼어들지 않음.
        self.assertNotIn("OPENROUTER_API_KEY=sk-or-v1-SECRET\n", new.replace("\r\n", "\r\r"))

    def test_lf_file_stays_lf(self):
        # LF 파일은 CR 이 새로 끼어들지 않아야 한다(스타일 보존, 회귀 0).
        original = "OPENROUTER_API_KEY=sk-or-v1-SECRET\n"
        new = N.upsert_env_text(original, self.MULTIHOME_ENV)
        self.assertNotIn("\r", new)

    def test_crlf_idempotent_reapply_is_stable(self):
        # CRLF 입력도 재적용 멱등이어야 한다(스타일 보존이 멱등을 깨지 않음).
        original = "OPENROUTER_API_KEY=sk-or-v1-SECRET\r\n"
        once = N.upsert_env_text(original, self.MULTIHOME_ENV)
        twice = N.upsert_env_text(once, self.MULTIHOME_ENV)
        self.assertEqual(once, twice, "CRLF 재적용 시 .env 가 달라짐(멱등 위반)")

    def test_apply_creates_env_from_example_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            example = os.path.join(d, ".env.example")
            env = os.path.join(d, ".env")
            with open(example, "w", encoding="utf-8") as f:
                f.write("OPENROUTER_API_KEY=sk-or-v1-TEMPLATE\n")
            # 실제 호스트 NIC 에 의존하지 않도록 detect 를 멀티홈 샘플로 대체.
            orig = N.detect
            N.detect = lambda: N.decide_mode(  # type: ignore
                N.classify_interfaces(N.parse_ip_o_output(SAMPLE_MULTIHOME)))
            try:
                N.apply_env(env, example)
            finally:
                N.detect = orig
            self.assertTrue(os.path.exists(env))
            txt = open(env, encoding="utf-8").read()
            self.assertIn("OPENROUTER_API_KEY=sk-or-v1-TEMPLATE", txt)  # 템플릿 시드 보존
            self.assertIn("MM_BIND_HOST=0.0.0.0", txt)  # 네트워크 키 주입

    def test_apply_guard_mode_does_not_alter_network_keys(self):
        # 공인 NIC 혼재 → 가드: 기존 .env 의 네트워크 키를 건드리지 않는다.
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, ".env")
            with open(env, "w", encoding="utf-8") as f:
                f.write("OPENROUTER_API_KEY=sk-or-v1-SECRET\nMM_BIND_HOST=127.0.0.1\n")
            orig = N.detect
            N.detect = lambda: N.decide_mode(  # type: ignore
                N.classify_interfaces(N.parse_ip_o_output(SAMPLE_PUBLIC_MIXED)))
            try:
                N.apply_env(env, None)
            finally:
                N.detect = orig
            txt = open(env, encoding="utf-8").read()
            self.assertIn("OPENROUTER_API_KEY=sk-or-v1-SECRET", txt)
            self.assertIn("MM_BIND_HOST=127.0.0.1", txt)  # 가드: 루프백 그대로
            self.assertNotIn("0.0.0.0", txt)  # 0.0.0.0 자동 주입 안 됨


class SummaryTest(unittest.TestCase):
    def test_multihome_summary_lists_each_net(self):
        d = N.decide_mode(N.classify_interfaces(N.parse_ip_o_output(SAMPLE_MULTIHOME)))
        s = N.format_summary(d)
        self.assertIn("멀티홈", s)
        self.assertIn("10.0.0.10", s)
        self.assertIn("172.16.0.10", s)
        self.assertIn("192.168.50.10", s)

    def test_guard_summary_warns_public(self):
        d = N.decide_mode(N.classify_interfaces(N.parse_ip_o_output(SAMPLE_PUBLIC_MIXED)))
        s = N.format_summary(d)
        self.assertIn("공인", s)
        self.assertIn("198.51.45.5", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
