"""층간(중앙 서버) 네트워크 배선 테스트 — MM 접속 주소 외부화 + 직원 프로비저닝.

검증 대상(네트워크 0 — 순수 함수/파싱만):
  - mm_client.mm_host/mm_port/mm_http_base/mm_ws_url 가 .env(MM_HOST/MM_PORT)로
    외부화되되 안전 기본값(127.0.0.1:8065 = 루프백)을 유지하는가
  - 잘못된/빈 값은 안전 기본값으로 폴백하는가(오설정 내성)
  - 'localhost' 가 기본값으로 절대 새어 들어가지 않는가(::1 우선해석 회귀 차단)
  - provision_mm.ensure_employees 가 employees.json 명단을 멱등 생성하고
    한글 팀 채널명을 슬러그로 변환해 멤버 배치하는가(라우팅 정합), 파일 없으면 skip
  - employees.json 의 team_channels 가 channels.json 실재 채널과 정합하는가
"""
import json
import os
import unittest


def _with_env(**kv):
    """주어진 환경변수를 임시 설정하는 컨텍스트 헬퍼(테스트 후 원복)."""
    class _Ctx:
        def __enter__(self):
            self._saved = {k: os.environ.get(k) for k in kv}
            for k, v in kv.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return self

        def __exit__(self, *a):
            for k, v in self._saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return _Ctx()


class MMAddressResolutionTest(unittest.TestCase):
    """MM 접속 주소가 .env 로 외부화되되 안전 기본값을 유지하는가."""

    def test_default_host_is_loopback_not_localhost(self):
        import mm_client as C
        with _with_env(MM_HOST=None, MM_PORT=None):
            self.assertEqual(C.mm_host(), "127.0.0.1")
            self.assertEqual(C.mm_port(), "8065")
            # 'localhost' 가 기본값으로 새면 macOS 에서 ::1 우선 → colima refused.
            self.assertNotIn("localhost", C.mm_http_base())
            self.assertNotIn("localhost", C.mm_ws_url())

    def test_default_urls_shape(self):
        import mm_client as C
        with _with_env(MM_HOST=None, MM_PORT=None):
            self.assertEqual(C.mm_http_base(), "http://127.0.0.1:8065/api/v4")
            self.assertEqual(C.mm_ws_url(), "ws://127.0.0.1:8065/api/v4/websocket")

    def test_tailscale_ip_override(self):
        # 층간 모드: 중앙 서버 Tailscale IP 로 봇이 LAN 너머 붙는다.
        import mm_client as C
        with _with_env(MM_HOST="100.101.102.103", MM_PORT="8065"):
            self.assertEqual(C.mm_host(), "100.101.102.103")
            self.assertEqual(C.mm_http_base(),
                             "http://100.101.102.103:8065/api/v4")
            self.assertEqual(C.mm_ws_url(),
                             "ws://100.101.102.103:8065/api/v4/websocket")

    def test_lan_ip_and_custom_port_override(self):
        import mm_client as C
        with _with_env(MM_HOST="192.168.0.50", MM_PORT="9000"):
            self.assertEqual(C.mm_http_base(),
                             "http://192.168.0.50:9000/api/v4")

    def test_blank_and_invalid_fall_back_to_defaults(self):
        # 오설정 내성: 공백 host / 비숫자 port 는 안전 기본값으로 폴백.
        import mm_client as C
        with _with_env(MM_HOST="   ", MM_PORT="not-a-number"):
            self.assertEqual(C.mm_host(), "127.0.0.1")
            self.assertEqual(C.mm_port(), "8065")


class EmployeeProvisionTest(unittest.TestCase):
    """직원 프로비저닝: employees.json 멱등 생성 + 한글채널→슬러그 멤버 배치.

    mmctl(docker exec) 호출은 전부 스텁으로 가로채 네트워크/도커 의존을 차단하고,
    호출 인자(생성/팀가입/채널가입)만 검증한다.
    """

    def setUp(self):
        import provision_mm as P
        self.P = P
        # 실제 docker/mmctl 호출 차단 — 호출 로그만 수집.
        self.calls = []
        self._orig_mmctl = P.mmctl
        self._orig_mmctl_json = P.mmctl_json
        self._existing_users = []  # _exists 판정용 가짜 user list

        def fake_mmctl(*args, check=True):
            self.calls.append(tuple(args))
            return 0, ""

        def fake_mmctl_json(*args):
            # user list 조회만 가짜 반환(나머지는 None).
            if args[:2] == ("user", "list"):
                return list(self._existing_users)
            return None

        P.mmctl = fake_mmctl
        P.mmctl_json = fake_mmctl_json

    def tearDown(self):
        self.P.mmctl = self._orig_mmctl
        self.P.mmctl_json = self._orig_mmctl_json

    def _write_employees(self, employees):
        path = os.path.join(self.P.HERE, "employees.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"employees": employees}, f, ensure_ascii=False)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_skip_when_no_file(self):
        # employees.json 부재(단일 PC 데모) → 호출 0, 예외 없음.
        path = os.path.join(self.P.HERE, "employees.json")
        self.assertFalse(os.path.exists(path), "테스트 전제: employees.json 없어야 함")
        self.P.ensure_employees()
        # user/team/channel 생성·가입 호출이 전혀 없어야 한다.
        self.assertEqual(self.calls, [])

    def test_creates_new_employee_with_team_and_channels(self):
        self._write_employees([{
            "username": "kim7f", "display_name": "김민수",
            "email": "kim7f@bogo.local", "password": "Pw-1111!",
            "team_channels": ["개발팀", "개발-보고라인"],
        }])
        self._existing_users = []  # 신규
        self.P.ensure_employees()
        # 1) user create 호출에 username/nickname 포함.
        creates = [c for c in self.calls if c[:2] == ("user", "create")]
        self.assertEqual(len(creates), 1)
        self.assertIn("kim7f", creates[0])
        self.assertIn("김민수", creates[0])
        # 비밀번호는 인자로 전달되지만(평문 저장 아님) — 호출에 포함됨을 확인.
        self.assertIn("Pw-1111!", creates[0])
        # 2) 팀 가입.
        self.assertTrue(any(c[:3] == ("team", "users", "add") for c in self.calls))
        # 3) 한글 채널명이 슬러그(dev-team/dev-report)로 변환되어 채널 가입.
        ch_adds = [c for c in self.calls if c[:3] == ("channel", "users", "add")]
        targets = {c[3] for c in ch_adds}
        self.assertIn(f"{self.P.TEAM_NAME}:dev-team", targets)
        self.assertIn(f"{self.P.TEAM_NAME}:dev-report", targets)

    def test_existing_employee_is_not_recreated(self):
        # 멱등: 이미 있으면 user create 호출 없음(팀/채널 가입은 무해하게 재보장).
        self._write_employees([{
            "username": "lee6f", "display_name": "이서연",
            "team_channels": ["인사총무팀"],
        }])
        self._existing_users = [{"username": "lee6f"}]  # 이미 존재
        self.P.ensure_employees()
        creates = [c for c in self.calls if c[:2] == ("user", "create")]
        self.assertEqual(creates, [], "기존 직원을 재생성하면 안 됨(멱등 위반)")
        # 채널 멤버십은 멱등 재보장(슬러그 변환 확인).
        ch_adds = [c for c in self.calls if c[:3] == ("channel", "users", "add")]
        self.assertIn(f"{self.P.TEAM_NAME}:hr-team", {c[3] for c in ch_adds})

    def test_undefined_channel_is_skipped_not_crashed(self):
        # CHANNEL_SLUG 에 없는 채널은 경고 후 skip(크래시·잘못된 라우팅 방지).
        self._write_employees([{
            "username": "ghost", "display_name": "유령",
            "team_channels": ["없는채널xyz"],
        }])
        self._existing_users = []
        self.P.ensure_employees()  # 예외 없이 끝나야 함
        ch_adds = [c for c in self.calls if c[:3] == ("channel", "users", "add")]
        self.assertEqual(ch_adds, [], "미정의 채널이 멤버 배치로 새어나감")


class EmployeeExampleIntegrityTest(unittest.TestCase):
    """employees.json.example 의 team_channels 가 channels.json 실재 채널과 정합한가."""

    def test_example_channels_exist_in_channels_json(self):
        import provision_mm as P
        ex_path = os.path.join(P.HERE, "employees.json.example")
        self.assertTrue(os.path.isfile(ex_path), "employees.json.example 누락")
        with open(ex_path, encoding="utf-8") as f:
            ex = json.load(f)
        # channels.json(실재) + CHANNEL_SLUG(슬러그 변환표) 양쪽에 다 있어야 라우팅 성립.
        channels = P._read_json(os.path.join(P.HERE, "channels.json"))
        for emp in ex.get("employees", []):
            for ch in emp.get("team_channels", []):
                self.assertIn(ch, P.CHANNEL_SLUG,
                              f"example 채널 '{ch}' 가 CHANNEL_SLUG 에 없음")
                # channels.json 이 비어있을 수 있는 환경(미프로비저닝)도 있으므로
                # CHANNEL_SLUG 정합만 강제(채널 ID 는 프로비저닝 시 채워짐).
                if channels:
                    self.assertIn(ch, channels,
                                  f"example 채널 '{ch}' 가 channels.json 에 없음")


if __name__ == "__main__":
    unittest.main(verbosity=2)
