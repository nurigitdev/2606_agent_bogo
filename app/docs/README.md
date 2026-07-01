# 사내 보고 멀티에이전트 (에이전트 BOGO + DeepSeek V4 Flash)

3개 업무 에이전트(박민철=비서실장, 이다은=인사총무, 최지현=개발)가 Mattermost 채널을 통로로
상향(팀→박민철→CEO)·하향(CEO→박민철→팀) 보고를 자율 라우팅한다.

## 아키텍처

| 레이어 | 구성 |
|--------|------|
| 두뇌(추론) | **에이전트 BOGO 두뇌**(Nous Research `AIAgent` 런타임, 외부 pip) `AIAgent` 런타임 + 모델 **DeepSeek V4 Flash**(OpenRouter 경유) |
| 통로(메시징) | **Mattermost** — WebSocket 수신 → REST 게시 (커스텀 게이트웨이 어댑터) |
| 라우팅 | 매 메시지마다 `decide()`가 LLM JSON으로 '내 차례인가 + 무엇을' 자율 판단. 멘션으로 다음 행위자 핸드오프 |

- 진입점: `bogo_runtime.py`  (실행: `<venv>/python bogo_runtime.py <orchestrator|hr|dev>`)
- 두뇌 호출은 `run_agent.AIAgent.chat()`이 담당(provider 레이어·재시도·대화 루프 내부 처리). 도구는 전부 비활성 → 순수 JSON 판단만 생성.
- 폭주 방지(규칙 아닌 구조): 봇 메시지는 멘션될 때만 판단(메아리 차단), 끝나면 `act=false` 침묵 / `task_status=closed`.
- 복원력(구조): WebSocket이 끊기면 지수 백오프(최대 30s)로 자동 재접속해 데몬이 죽지 않는다. 단일 메시지의 LLM/JSON/Mattermost 오류는 해당 메시지만 드롭하고 루프는 유지한다. 토큰 불량(WS 인증 실패)은 재시도 무의미하므로 즉시 중단해 운영자에게 알린다.

> 주의: 진입 파일명을 `agent.py`로 두면 Nous Research 외부 pip 런타임 패키지(requirements.txt 참조)의 `agent` 모듈을 가려(shadow) `run_agent` import가 깨진다.
> 반드시 `bogo_runtime.py` 등 다른 이름을 쓴다.

---

## 배포 모드 — 단일 PC vs 중앙 서버(층간)

같은 코드가 두 가지 모드로 동작한다. **모드 전환은 `.env` 환경변수만으로** 이뤄지며, 기본값은 항상 안전한 단일 PC(루프백 전용)다.

> **자동 구성(권장): `.env` 손으로 안 적어도 된다.** 멀티홈 중앙 서버는 NIC 를 각 망에 직결하고 사설 IP 만 부여한 뒤 `./bogo_oneclick.sh start`(Linux 더블클릭 런처도 동일)를 실행하면, 런처가 NIC/사설 IP 를 자동 감지해 모드를 판정하고 `.env` 의 네트워크 키(`BOGO_MULTIHOME`/`MM_BIND_HOST`/`MM_SITE_URL`/`MM_ALLOW_CORS_FROM`/`BOGO_DASHBOARD_HOST`)를 **멱등 자동 기록**한 뒤 기동까지 한 번에 한다(비밀·수동값 비파괴). 사설 NIC 2개↑=멀티홈, 1개=단일망 LAN, 0개=루프백으로 자동 판정하고, **공인 IP NIC 가 감지되면 0.0.0.0 자동활성을 중단·경고**한다(인터넷 노출 방지). 감지 로직은 `net_autodetect.py`(테스트: `test_net_autodetect.py`). 아래 표의 수동 환경변수 설정은 폴백/참고다.

| 구분 | 단일 PC 모드 (기본) | 중앙 서버(층간) 모드 |
|------|--------------------|---------------------|
| 언제 | 데모·소규모, 한 대에서 직원·봇·대표 전부 | 여러 망/PC(6·7·10층 등)가 한 중앙 서버 공유 |
| 네트워크 | `127.0.0.1` 루프백 전용 — 외부 노출 0 | **멀티홈(NIC 3장 직결, 권장)** 또는 LAN/Tailscale 사설 메시(`100.x`/`192.168.x`/`10.x`) |
| 환경변수 | 아무것도 설정 안 함(기본값) | 멀티홈: `BOGO_MULTIHOME=1`+`MM_BIND_HOST=0.0.0.0`+`MM_SITE_URL`+`MM_ALLOW_CORS_FROM`+`BOGO_DASHBOARD_HOST=0.0.0.0` / 단일 IP: `MM_BIND_HOST`·`MM_SITE_URL`·`BOGO_DASHBOARD_HOST` 에 사설 IP, 각 PC 에서 `MM_HOST` |
| 직원 계정 | 불필요(없으면 자동 skip) | `employees.json` 으로 발급 후 직원 로그인 |

흐름(멀티홈, 권장): `A/B/C 망 직원 브라우저(자기 망 NIC IP)` → `중앙 서버 1대(NIC 3장 + Mattermost + 팀봇/CEO봇 헤르메스 두뇌)` → `대표 브라우저(CEO 대시보드)`.

### 새로 추가된 환경변수 (전부 `.env.example`에 주석 포함)

| 변수 | 의미 | 누가 설정 | 기본값 |
|------|------|----------|--------|
| `MM_HOST` | 봇·대시보드·프로비저너가 **붙는** Mattermost 호스트 (`mm_client` 단일 진실원) | 모든 PC | `127.0.0.1` |
| `MM_PORT` | Mattermost 포트 | 모든 PC | `8065` |
| `MM_BIND_HOST` | Mattermost 컨테이너가 **받는** 포트 바인딩 호스트(`docker-compose`) | 중앙 서버만 | `127.0.0.1` |
| `MM_SITE_URL` | 클라이언트(데스크탑 앱·브라우저) 접속 표준 주소 — WebSocket·로그인 리다이렉트 기준 | 중앙 서버만 | `http://127.0.0.1:8065` |
| `MM_ALLOW_CORS_FROM` | 추가 접속 오리진(멀티홈에서 B/C 망 IP — 공백 구분) — 웹소켓 CheckOrigin·CORS 허용 | 중앙 서버(멀티홈)만 | 빈 값 |
| `BOGO_MULTIHOME` | 멀티홈 모드 스위치(`1`이면 `0.0.0.0` 바인딩 허용 — 공인 NIC 부재 전제) | 중앙 서버(멀티홈)만 | 미설정(off) |
| `BOGO_DASHBOARD_HOST` | CEO 대시보드 웹앱 바인딩 호스트(멀티홈 외에는 와일드카드 거부·루프백 강등) | 중앙 서버만 | `127.0.0.1` |
| `BOGO_DASHBOARD_PORT` | CEO 대시보드 포트 | 중앙 서버만 | `8642` |

> 보안 불변: 와일드카드(`0.0.0.0`/`::`)는 기본·LAN·Tailscale 모드에서 공인 NIC 까지 열릴 위험이라 거부되고 자동으로 `127.0.0.1` 로 강등된다. **예외 — 멀티홈 모드(`BOGO_MULTIHOME=1`)** 는 서버에 공인 NIC 가 없고(공인 IP 0) 사내 사설망 3개에만 NIC 직결된 환경이라, 3개 NIC 를 단일 listen 소켓으로 동시에 받기 위해 `0.0.0.0` 을 허용한다(인터넷 향 차단은 공인 NIC 부재 + 방화벽 이중 가드). `*` 는 어떤 모드에서도 거부. 공인 인터넷 노출은 어떤 경우에도 금지.
>
> SiteURL 멀티액세스: Mattermost 9.x 는 SiteURL 1개만 갖는다. 멀티홈에서는 주 망(A) IP 를 `MM_SITE_URL` 로, 나머지 2개 망(B/C) IP 오리진을 `MM_ALLOW_CORS_FROM` 에 공백 구분으로 등록해 3개 망 동시 접속의 웹소켓·CORS 차단을 푼다. 봇은 서버 로컬 `127.0.0.1` 로 붙어 SiteURL/CORS 영향이 없다.
>
> **층간 배선 전체 절차(멀티홈 NIC 배선/SiteURL/방화벽, 또는 Tailscale/LAN → 직원 계정 발급 → 연결 점검)는 [`DEPLOY_NETWORK.md`](DEPLOY_NETWORK.md) 참고.**

---

## 에이전트 정의 3계층 분리 (데이터 주도 구조)

라우팅·규칙·페르소나가 파이썬에 하드코딩되던 구조를 3계층으로 분리해, **새 팀/에이전트 추가 시 파이썬을 수정하지 않는다.**

| 계층 | 위치 | 내용 |
|------|------|------|
| 고유 페르소나 | `agents/<role>.md` 1파일 | 9섹션 구조(역할·범위·도구·출력계약·예시·가드레일·소싱·완료·에스컬레이션). 그 역할만의 행동·도메인 지식 |
| 공유 규칙(상속) | `agents/_shared/common_rules.md` | 보고 포맷·작성 원칙·진행 공유·안전·소싱·완료 조건·행동결정 JSON 스키마. **런타임이 모든 에이전트 시스템 프롬프트에 자동 주입**. 한 곳만 고치면 전원 반영 |
| 선언적 라우팅 | `teams.json` + `channels.json` | 팀(팀방·보고라인방·팀에이전트·상향대상)·채널(이름→ID) 데이터. 런타임이 `build_routing()`으로 ROUTING 텍스트를 동적 생성 |

공용 모듈: `agent_schema.py`(파싱·검증·ROUTING 생성·프롬프트 조립), `mm_client.py`(LLM·Mattermost REST). `bogo_runtime.py`와 `ceo_admin_runtime.py`가 공유한다.

### 새 팀/에이전트 추가법 (파이썬 수정 0)

```bash
# 1) 채널을 Mattermost에 만들고 channels.json 에 "이름":"ID" 등록
# 2) teams.json 의 "teams" 배열에 블록 1개 추가
#    {"id":"sales","label":"영업","team_channel":"영업팀",
#     "report_channel":"영업-보고라인","agent":"<이름>","escalate_to":"박민철"}
# 3) agents/sales.md 작성 (frontmatter: name/username/config/primary/channels + 9섹션 본문)
# 4) <config>_config.json 에 새 봇 토큰·ID 등록
# 5) 린트로 정합성 확인 (필수필드 누락·username 중복·미정의 채널 참조를 머지 전 차단)
.venv/bin/python lint_agents.py
# 6) 새 역할 데몬 등록 (plist 1개 추가 → bootstrap). 런타임 기동 시에도 자동 검증된다.
```

### 협업 작업방 (CEO 정책·기획 과제 — 여러 에이전트 공동 수행)

기본 라우팅은 *1팀 = 1에이전트 = 1팀방* 모델이다. 여기에 더해, **CEO가 정책·기획 같은 범부서 과제를 던지면 모든 업무 에이전트(박민철·이다은·최지현)가 같은 방에 들어가 함께 결과물을 만들어 CEO에게 제출하는 공동 작업방**을 지원한다.

| 구성 | 값(기본) |
|------|----------|
| 작업방 채널 | `정책기획실` |
| 참여 에이전트 | 박민철(비서실장)·이다은(인사총무)·최지현(개발) |
| 리드(취합·제출) | 박민철 |
| 제출처 | `CEO브리핑` |

동작 흐름:

1. CEO가 `정책기획실`에 정책·기획 과제를 게시한다.
2. 참여 에이전트가 **각자 자기 전문성**(인사·노무·예산 / 기술·일정·리스크)으로 분석·근거·초안을 같은 방에 올린다. 서로의 기여를 읽고 빠진 관점을 보탠다(복붙 금지).
3. 리드(박민철)가 기여를 하나의 결과물로 취합·구조화(우선순위·리스크·권고 포함)해 `CEO브리핑`에 제출하고 `task_status=closed`. 기여가 부족하면 그 방에서 해당 참여자를 멘션해 보완을 요청한다.

이 모든 흐름은 **데이터로 선언**된다. `teams.json` 의 `collab_rooms` 블록이 방·참여자·리드·제출처를 정의하고, 런타임 `build_routing()` 이 이를 라우팅 텍스트로 변환해 참여 에이전트 프롬프트에 주입한다. 협업 프로토콜 자체는 `agents/_shared/common_rules.md`(8-1절)에 명문화돼 전원에게 상속된다. **파이썬 분기 하드코딩은 없다.**

```jsonc
// teams.json — collab_rooms 블록
"collab_rooms": [
  {
    "id": "policy_planning", "label": "정책기획",
    "channel": "정책기획실",
    "participants": ["박민철", "이다은", "최지현"],
    "lead": "박민철", "deliver_to": "CEO브리핑",
    "desc": "CEO 정책·기획 범부서 과제 공동 작업방"
  }
]
```

구독·송신 권한은 채널 화이트리스트로 강제된다: 참여 에이전트의 `agents/<role>.md` frontmatter `channels` 에 작업방 채널이 들어 있어야 런타임이 그 방 메시지를 구독·판단하고 그 방으로 송신할 수 있다(린트가 교차 검증).

#### 새 협업 작업방 추가법 (파이썬 수정 0)

```bash
# 1) 작업방 채널을 Mattermost에 만들고 channels.json 에 "이름":"ID" 등록
#    (자동 생성 스크립트가 없으면 Mattermost 관리자 패널에서 공개 채널 생성 후 ID 입력)
# 2) teams.json 의 "collab_rooms" 배열에 블록 1개 추가
#    {"id":"...","channel":"<채널명>","participants":["박민철","이다은","최지현"],
#     "lead":"박민철","deliver_to":"CEO브리핑"}
# 3) 참여할 각 에이전트의 agents/<role>.md frontmatter channels 에 <채널명> 추가
# 4) 봇들을 그 채널의 멤버로 가입시킨다(Mattermost 관리자 패널 또는 채널 멤버 추가 API)
# 5) 린트로 정합성 확인 (channel·participants·lead·deliver_to 검증 + 참여자 channels 교차 확인)
.venv/bin/python lint_agents.py
# 6) 변경 배포(코드/데이터 반영 + 역할 재시작)
./bogo_ctl.sh restart
```

> 운영자 주의: 작업방 채널을 자동 생성하지 못하는 환경(토큰 권한 부족·서버 미가동)이면, Mattermost
> **관리자 패널 → 팀 → 채널 생성**으로 공개 채널을 만들고 참여 봇 3개를 멤버로 추가한 뒤,
> 채널 ID를 `channels.json`(시크릿, `.gitignore`)에 입력한다. `channels.json.example` 의
> `"정책기획실":"CHANNEL_ID"` 는 자리표시자이며 실제 ID를 커밋하지 않는다.

### 에이전트 개조 파이프라인 (역할별 학습방에서 자연어로 정의 수정)

각 에이전트의 **역할별 학습방**(`teams.json` `learning_rooms` — 인사총무·개발·비서실 학습방)에서 자연어로 피드백하면 **에이전트 관리 봇**(`ceo_admin_runtime.py`)이:

1. 의도 파싱 — 예: 비서실-학습방에서 `보고를 3줄로 줄여` → **그 방 담당 에이전트(박민철)** 기준으로 변경 지시 추출(방 격리: 그 방에서는 그 방 owner 만 수정)
2. 해당 `agents/<owner>.md` 를 LLM으로 재작성 → **unified diff 미리보기**를 그 방에 게시(pending)
3. `적용`(또는 반영/승인) → md 파일 반영 + **git commit(한국어)** + 운영본 동기화 + 해당 역할 데몬 리로드
4. `반려`(또는 취소/폐기) → 그 방 대기 변경 폐기

방 격리: 학습방마다 그 방 owner 의 정의만 수정되며, 대기 변경(PENDING)도 방(role)별로 분리된다. 다른 에이전트는 그 에이전트의 학습방에서 지시해야 한다.

안전장치(하드 게이트): **`agents/*.md`(페르소나)만 수정 가능** — 다른 파일·시스템 명령 불가. **적용 전 반드시 diff 승인 게이트**. frontmatter 필수필드를 깨면 적용 거부. 모든 변경은 git 추적. 봇은 자기 메시지에 반응하지 않는다(메아리 차단).

```bash
# 수동 기동
.venv/bin/python ceo_admin_runtime.py
# 또는 launchd (admin 역할)
~/.bogo-bin/run_role.sh admin
```

---

## CEO 대시보드 (한 곳에서 모니터링·지시)

CEO가 Mattermost 채널을 일일이 오가지 않고 **브라우저 한 곳**(`http://127.0.0.1:8642`)에서 부서 현황을 보고 지시를 내리는 로컬 웹앱(`ceo_dashboard.py`). 기존 통신 인프라를 그대로 재사용한다 — `mm_client.MM`(REST), `channels.json`/`teams.json`/`agents/*.md`(`agent_schema`). 신규 의존성·신규 API 키 0(표준 라이브러리 `http.server`만 사용).

화면 구성:
- **부서별 현황 카드** — 팀 채널·보고라인·CEO브리핑의 최근 메시지를 12초 주기로 폴링 표시(작성자·시각·본문). 모니터링 채널은 `teams.json`에서 동적으로 결정된다(하드코딩 없음).
- **CEO 지시 입력창** — 대상 채널 선택(기본 `CEO브리핑` → 박민철 비서실장에게 전달) 후 메시지 게시. 게시는 화이트리스트 채널로만 제한된다.
- **에이전트 현황** — 등록 role 목록(`load_roles`)과 각 봇의 활성 여부(Mattermost user API), 담당 채널 표시.
- **에이전트 개조 안내** — 정의 변경은 **역할별 학습방**(`ceo_admin_runtime`) 파이프라인으로 연계(그 방 담당 에이전트만 수정). 대시보드 자체는 정의 파일을 건드리지 않는다(모니터링·지시 전용).

```bash
# 수동 기동 (포트 기본 8642)
.venv/bin/python ceo_dashboard.py
# 포트 변경
BOGO_DASHBOARD_PORT=9000 .venv/bin/python ceo_dashboard.py
# 접속(단일 PC): http://127.0.0.1:8642   (기본 — 루프백 전용, 외부 노출 0)
# 층간 단일 IP 모드: 중앙 서버에서 BOGO_DASHBOARD_HOST 에 사설 IP 지정
#   → 대표 노트북 브라우저로 http://<중앙서버 사설IP>:8642 접속 (DEPLOY_NETWORK.md 참고)
# 멀티홈 모드(권장): BOGO_MULTIHOME=1 + BOGO_DASHBOARD_HOST=0.0.0.0 으로 3개 NIC 동시 수신
#   → 각 망 대표/직원이 자기 망 NIC IP:8642 로 접속 (DEPLOY_NETWORK.md 1절 참고)
```

게시용 봇 토큰은 박민철(`nk_config.json`)을 재사용한다(CEO브리핑·양 보고라인 멤버라 읽기/쓰기 권한 보유). 토큰이 비어 있거나 플레이스홀더면 기동을 거부한다. 테스트: `.venv/bin/python -m unittest test_ceo_dashboard`.

> 보안: 기본 바인딩은 `127.0.0.1`(루프백 전용 — 외부 노출 0). 대시보드는 **ceo_auth 로그인 게이트**(세션 쿠키, HttpOnly·SameSite=Strict) 뒤에 있어, 층간 모드에서는 `BOGO_DASHBOARD_HOST` 에 사내/Tailscale **사설 IP** 를 지정해 대표 노트북이 인증 경계 안에서 붙을 수 있다. 와일드카드(`0.0.0.0`/`::`/`*`)는 공개 노출 위험이라 코드가 거부하고 자동으로 루프백으로 강등한다. 공인 인터넷 노출은 어떤 경우에도 금지(사내망/메시 전용 — `DEPLOY_NETWORK.md`).

---

## 가장 쉬운 시작 — 더블클릭 (macOS)

터미널 타이핑이 귀찮으면 프로젝트 루트의 **`BOGO_start.command`** 파일을 Finder에서 더블클릭한다.
(파일 위치: `launchers/BOGO_start.command` — 최상위에 더블클릭하면 자동으로 열림)

- 아직 미설치면 → 자동으로 `setup`(venv+의존성+config+launchd 등록) 수행
- 이미 상시 가동 중이면 → 중복 기동 없이 최신 코드 재배포 + 4역할 재시작(`restart`)
- 끝나면 현재 상태(PID/종료코드/역할)를 한국어로 표시하고, 오류 시 창이 닫히지 않고 원인을 보여준다

내부적으로 `app/bogo_ctl.sh` 를 호출할 뿐이라 동작은 아래 명령들과 동일하다.
(처음 다운로드 시 `우클릭 → 열기` 한 번으로 Gatekeeper 허용)

## 가장 쉬운 시작 — 더블클릭 (Windows)

mac 절과 완전 대칭. 프로젝트 루트의 **`BOGO_start.bat`** 파일을 탐색기에서 더블클릭한다.
(파일 위치: `launchers/BOGO_start.bat` — 최상위에 더블클릭하면 자동으로 열림)

- 아직 미등록이면 → 자동으로 `setup`(venv+의존성+config+**Task Scheduler** 등록) 수행
- 이미 상시 가동 등록돼 있으면 → 중복 등록 없이 최신 코드 재배포 + 4역할 재시작(`restart`)
- 끝나면 현재 상태(역할별 Task State)를 한국어로 표시하고, 오류 시 창이 닫히지 않고(`pause`) 원인을 보여준다

내부 동작: `BOGO_start.bat`(UTF-8 `chcp 65001`, `cd /d "%~dp0"` 로 공백 포함 경로 고정)
→ `BOGO_start.launcher.ps1`(가동 상태 감지·분기 본체)
→ `app\bogo_ctl.ps1 {setup|restart|status}`. mac 의 `.command`→`bogo_ctl.sh` 경로와 1:1 등가다.

- 가동 상태는 `Get-ScheduledTask -TaskName "BOGO_*"` 존재 여부로 감지한다(mac 의 `launchctl list | grep com.bogo` 등가).
- PowerShell 7(`pwsh`)이 있으면 그것을, 없으면 Windows 기본 `powershell` 5.1 을 자동으로 사용한다.
- 처음이라면 `python.org`에서 Python 3(설치 시 "Add to PATH" 체크)을 설치한 뒤 더블클릭하면 부트스트랩이 venv 부터 자동 구성한다.

## 가장 쉬운 시작 — 더블클릭/아이콘 (Linux)

리눅스 파일관리자는 macOS 의 `.command` 를 실행하지 못한다. 그래서 **단일 진입점**은
`launchers/` 폴더의 **`BOGO_start.sh`** 이다. 이 파일 하나가 macOS `.command` 와 **완전히 동일한
풀 코어**(`app/bogo_oneclick.sh start`: venv→reindex→통신백본→**데이터 자동복원**→봇·대시보드
상시가동 등록→대시보드 헬스체크)로 수렴한다. 즉 "폴더 복사 후 1번 실행"이면 데이터까지 따라온다.

권장 순서는 다음 둘 중 하나다(둘 다 같은 풀 코어로 끝난다):

1. **앱 메뉴 아이콘(가장 권장)** — 프로젝트 루트에서 1회 설치:

   ```bash
   ./app/install_desktop_launcher.sh
   ```

   설치 스크립트가 (a)핵심 `.sh` 전부에 실행권한(+x) 자동 부여(clone 시 소실 대비)
   (b)`~/.local/share/applications/bogo-start.desktop` 설치 + 신뢰 플래그(`gio metadata::trusted`)
   (c)앱 메뉴 캐시 갱신(`update-desktop-database`)까지 해 준다. 이후 **앱 메뉴/파일관리자에서
   'BOGO Start' 아이콘을 클릭**하면 터미널 창에 로그가 뜨며 풀 코어가 돈다. (안 보이면 로그아웃→재로그인 1회.)

2. **파일관리자에서 `launchers/BOGO_start.sh` 더블클릭** — 일부 환경은 `.sh` 우클릭 →
   "속성 → 실행 허용"(또는 "Allow Launching")을 1회 켜야 한다. TTY 없이 실행돼도
   `BOGO_start.sh` 가 설치된 터미널(gnome-terminal/konsole/xterm 등)을 자동 탐지해 그 안에서
   자기 자신을 다시 띄워 로그를 보여준다(무한재귀 가드 포함). 터미널을 못 찾으면
   `BOGO_start_log.txt` 로 로그를 남기고 위치를 안내한다.

> **더블클릭이 막히면(편집기로 열리거나 권한 거부) 아래 한 줄을 터미널에 그대로 복붙하면 항상 동작한다:**
>
> **`bash "/실제/경로/launchers/BOGO_start.sh"`**
>
> (예: 홈에 압축을 풀었다면 **`bash "$HOME/agent-bogo/launchers/BOGO_start.sh"`**. 경로에 공백이 있을 수 있으므로
> 반드시 **따옴표로 감싼다.** 정지는 같은 방식으로 **`bash "/실제/경로/launchers/BOGO_stop.sh"`**.)

> 헤드리스 서버(GUI 없음)에서는 위 더블클릭 대신 아래 "다른 PC에서 시작"의 CLI 절차를 쓴다.

---

## 다른 PC에서 시작 (mac / Windows / Linux 공통)

어느 OS에서 `git clone` 하거나 폴더를 복사하든 **통신 백본(Mattermost) 기동 → 단일 부트스트랩 → 상시 가동**이
되도록 설계됐다. 경로는 사용자명·설치 위치·한글 폴더명과 무관하게 동작한다(`/Users/<이름>` 같은 하드코딩 0건).

```bash
git clone <repo-url>
cd app
```

### ⓪ 통신 백본(Mattermost + Postgres) 최초 기동 — 다른 PC에서 가장 먼저

봇과 대시보드는 모두 `ws://127.0.0.1:8065` 의 **Mattermost** 에 붙는다. 따라서 봇을 띄우기 전에
Mattermost·Postgres 컨테이너가 그 PC에 **존재**해야 한다. 저장소의 **`docker-compose.yml`** 이
이 두 컨테이너(`bogo-pg`/`bogo-mm`)를 어느 PC에서든 동일한 이름·네트워크·포트로 생성한다.

```bash
# 전제: Docker 가 동작해야 한다.
#   macOS  : Colima(권장)  brew install colima docker && colima start
#            또는 Docker Desktop 실행
#   Linux  : sudo apt install docker.io && sudo systemctl enable --now docker
#            (docker-compose-plugin 은 선택: 없으면 infra_up.sh 가 Docker CLI fallback 사용)
#   Windows: Docker Desktop(WSL2 백엔드) 실행

cd app
./infra_up.sh                   # bogo-pg + bogo-mm 최초 생성·기동(Compose 있으면 사용, 없으면 Docker CLI fallback)
# 또는 Compose 설치 환경에서: docker compose up -d
# MM 콜드 부팅은 수십 초 걸린다. 준비 확인:
curl -fsS http://127.0.0.1:8065/api/v4/system/ping   # {"status":"OK"} 면 준비됨
```

> `infra_up.sh`(및 `bogo_ctl.sh restart`·`bogo_oneclick.sh`)는 컨테이너가 **없으면 compose 또는 Docker CLI fallback 으로 자동
> 생성**하고, 이미 있으면 기동만 한다(데이터 보존). 즉 `./bogo_ctl.sh setup` 한 줄에도 통신 백본이 함께 선다.
> 명시적으로 백본만 올릴 때도 `./infra_up.sh` 를 쓰면 Compose 유무와 무관하게 멱등 부트 체인
> (Colima→컨테이너→MM readiness)을 한 번에 보장한다. Compose 설치 환경에서는 `docker compose up -d` 도 직접 쓸 수 있다.

#### 관리자·봇·채널 셋업 — 이제 자동 (무인 프로비저닝)

> **더 이상 손으로 만들 필요가 없다.** 통신 백본이 뜬 직후 `infra_up.sh` 가 `provision_mm.py` 를
> 호출해, 컨테이너 안의 `mmctl` 로 아래를 **전부 멱등 생성·발급**한다. 관리자·팀·채널은
> `mmctl --local`(로컬 소켓, 인증 불필요 — `docker-compose.yml` 의 `MM_SERVICESETTINGS_ENABLELOCALMODE=true`
> 로 활성)로, 봇 계정·토큰은 `--local` 에서 막혀 있어(mattermost#36353) 그 관리자 자격으로 만든
> 컨테이너 내부 인증 세션으로 발급한다(둘 다 127.0.0.1:8065 — 외부 노출 없음).

```text
provision_mm.py 가 자동으로 하는 일 (이미 있으면 skip, 재실행 안전):
  1) 시스템 관리자 계정 생성        — 기본 admin / .env BOGO_ADMIN_* 로 덮어쓰기 가능
  2) 팀 생성                          — 기본 슬러그 bogo / .env BOGO_TEAM_*
  3) 봇 3개 생성(박민철·이다은·최지현) + 각 Access Token 발급
     → nk_config.json / gyaru_config.json / genz_config.json 에 token·bot_id 자동 기록
  4) 운영봇(박민철) system-admin 부여, 봇 3개를 팀에 가입
  5) 채널 9종 생성(팀방·보고라인·CEO브리핑·정책기획실·학습방 3개)
     → channels.json 에 "이름":"ID" 자동 기록 + 봇을 각 채널 멤버로 추가
```

즉 **새 PC: `git pull` → 더블클릭 한 번** 이면 토큰·채널이 전부 자동 발급·기록되고 봇·대시보드가 뜬다.
이미 토큰이 채워져 동작 중인 PC에서 다시 실행해도 실재 계정/채널을 재사용하고 유효 토큰은 보존한다(불필요 재발급·회귀 없음).
수동 관리 환경에서 끄려면 `.env` 에 `BOGO_SKIP_PROVISION=1`.

> 이미 운영 중이던 PC를 그대로 옮기는 경우엔 **named volume(`bogo-pg-data`/`bogo-mm-data`)** 에 계정·채널·
> 메시지가 보존되므로 프로비저닝이 기존 상태를 그대로 재사용한다. 볼륨까지 새로 시작하려면 `docker compose down -v`(데이터 전체 소거 — 주의).

#### LLM 백엔드 — 클라우드(OpenRouter) 또는 로컬(키 불필요)

`.env` 의 `LLM_BACKEND` 한 줄로 고른다. 호출 코드는 OpenAI 호환 `/chat/completions` 단일 경로라 두 백엔드가 동일하게 동작한다.

| 모드 | `.env` | 필요한 것 |
|------|--------|-----------|
| 클라우드(기본) | `LLM_BACKEND=openrouter` | `OPENROUTER_API_KEY` 입력 |
| 로컬 | `LLM_BACKEND=local` | **API 키 불필요.** Ollama 등 OpenAI 호환 서버만 실행 |

로컬 모드 준비(예: Ollama):
```bash
# https://ollama.com 설치 후
ollama pull qwen2.5:7b-instruct      # 기본 모델(.env OLLAMA_MODEL 로 변경 가능)
ollama serve                          # 127.0.0.1:11434 (기본 base_url, OpenAI 호환 /v1)
# .env 에 LLM_BACKEND=local 만 두면 끝 — OpenRouter 키 없이 완전 동작
```
다른 OpenAI 호환 로컬 서버(vLLM·LM Studio 등)는 `.env` 의 `LLM_BASE_URL`/`LLM_MODEL` 로 강제 지정한다.

### macOS / Linux

```bash
./bogo_ctl.sh setup       # ① 통신 백본 보장(infra_up.sh — 컨테이너 없으면 compose 로 자동 생성)
                            #   ② venv 휴대용 재생성 + 의존성 설치 + *.example→config 복사
                            #   ③ OS 감지해 상시 가동 등록 (mac=launchd / linux=systemd --user)
# setup 안내대로 .env·*_config.json·channels.json 에 실제 값 입력 → 재시작
# (Mattermost 관리자·봇·채널 최초 셋업은 위 ⓪절 참고 — PC마다 1회)
./bogo_ctl.sh restart
```

부트스트랩만: `./bogo_ctl.sh bootstrap` · 서비스만: `./bogo_ctl.sh install`
수동 단일 실행: `./bogo_ctl.sh run orchestrator` · 상태: `./bogo_ctl.sh status`

### Windows (PowerShell)

```powershell
pwsh ./bogo_ctl.ps1 setup    # venv + 의존성 + config 복사 후 Task Scheduler 등록
# .env·*_config.json·channels.json 에 실제 값 입력 후
pwsh ./bogo_ctl.ps1 restart
```

> **Python 3.10-3.13 필요**(`python3` 우선, 없으면 `python`; venv 생성 시 지원 범위를 확인). 인터프리터가 없거나 지원 범위 밖이면 부트스트랩이 이유를 출력하고 멈춘다.
> mac: `brew install python` · Ubuntu: `sudo apt install python3 python3-venv`
> Windows: [python.org](https://www.python.org/downloads/) 최신 버전 (설치 시 "Add to PATH" 체크)

### 시크릿은 커밋되지 않는다 (부트스트랩이 복사)

`.env`·`*_config.json`·`channels.json` 은 `.gitignore` 로 커밋이 차단된다.
부트스트랩이 `*.example` 을 실제 파일로 **없을 때만** 복사하며(기존 값 보존), 그 뒤 직접 값을 채운다.
`.venv` 도 추적하지 않는다 — 부트스트랩이 그 PC에서 항상 새로 만들어 절대경로 핀 문제를 원천 차단한다.

### 설정 파일 참고

| 파일 | 내용 |
|------|------|
| `.env` | `LLM_BACKEND`(local/openrouter) 스위치, `OPENROUTER_API_KEY`(클라우드 시), `OLLAMA_*`(로컬 시), `BOGO_ADMIN_*`/`BOGO_TEAM_*`(프로비저닝, 전부 기본값) |
| `llm_config.json` | LLM 기본값. openrouter(`model`/`base_url`)와 local(`local_model`/`local_base_url`) 모두. `.env` 가 우선 |
| `nk_config.json` | 박민철 봇 Mattermost 토큰·ID — **프로비저닝이 자동 기록** |
| `gyaru_config.json` | 이다은 봇 Mattermost 토큰·ID — **자동 기록** |
| `genz_config.json` | 최지현 봇 Mattermost 토큰·ID — **자동 기록** |
| `channels.json` | 채널명 → 채널 ID 매핑 — **프로비저닝이 자동 기록** |
| `teams.json` | 선언적 팀·라우팅 데이터(팀방·보고라인·팀에이전트·상향대상) + `collab_rooms`(여러 에이전트 공동 작업방). 새 팀/협업방 추가는 여기 블록 1개로 |
| `provision_mm.py` | 무인 프로비저닝(mmctl --local). 시크릿 아님, git 추적 |
| `agents/_shared/common_rules.md` | 전 에이전트 공통 규칙(런타임이 상속 주입). 시크릿 아님, git 추적 |

인증: **로컬 모드면 어떤 API 키도 불필요**(OAuth/로컬 기본). 클라우드 모드일 때만 OpenRouter 키를 `.env` 에 둔다. 봇 토큰은 프로비저닝이 자동 발급하므로 손입력 불필요.

---

## 상시 무중단 가동 (3 OS 크로스플랫폼)

4개 역할(`orchestrator`/`hr`/`dev` + CEO 관리 봇 `admin`)을 OS별 네이티브 서비스로 등록한다.
**로그인/부팅 시 자동 기동 + 비정상 종료 시 자동 재시작**. 단일 진입점 `bogo_ctl` 이 OS를 감지해 분기한다.

| OS | 서비스 메커니즘 | 자동 재시작 | 한글 경로 |
|----|----------------|------------|----------|
| macOS | launchd 사용자 에이전트 (`RunAtLoad`+`KeepAlive`+`ThrottleInterval 10`) | KeepAlive | ASCII 미러 경유(아래 사유) |
| Linux | systemd `--user` 인스턴스 (`Restart=always`+`MemoryMax=1G`) | systemd + OOM 가드 | 직접 동작(인플레이스) |
| Windows | Task Scheduler (로그온 트리거 + 실패 시 1분마다 재시작) | 스케줄러 재시작 정책 | 직접 동작(인플레이스) |

```bash
# mac/linux
./bogo_ctl.sh install      # 등록+기동      ./bogo_ctl.sh uninstall   # 등록 해제
./bogo_ctl.sh restart      # 재배포+재시작  ./bogo_ctl.sh status      # 상태

# windows
pwsh ./bogo_ctl.ps1 install ; pwsh ./bogo_ctl.ps1 status
```

모든 서비스 파일은 **템플릿**(`service/templates/`)에서 설치 시점에 `${HOME}`·repo 절대경로로 치환 생성된다 —
사용자명·설치 위치가 어디든 자동 적응한다(하드코딩 0건).
Linux 로그: `journalctl --user -u bogo@orchestrator -f`. Windows: 작업 스케줄러 기록.

### NVIDIA DGX Spark / Ubuntu ARM64 상시 가동 (최종 사내 운영 타깃)

최종 운영 환경은 **NVIDIA DGX Spark**(DGX OS = Ubuntu 24.04, **ARM64/aarch64**, Python 3.12 이상)다.
이 저장소는 ARM64에서 **추가 빌드 도구 없이 `pip install` 만으로** 동작한다 — 근거:

- Nous Research 외부 pip 런타임 패키지는 순수 파이썬 휠(`py3-none-any`)이지만 버전별 Python 요구사항이 다르다. `requirements.txt` 는 Python marker 로 Python 3.11 이상에서는 `hermes-agent==0.17.0`, Python 3.10 계열에서는 PyPI 설치 가능한 `0.15.2` 를 선택한다.
- `websockets` 는 `manylinux_2_17_aarch64` + `cp312` 이상 휠을 제공해 ARM64 Python 3.12 이상에서 바로 설치된다.
- 따라서 DGX Spark 에서도 컴파일러·헤더 없이 `bootstrap.sh` 가 venv 를 만들고 의존성을 그대로 받는다.

설치·상시 가동 절차(Ubuntu ARM64):

```bash
# ① Python 3.12 이상 + venv 모듈 (DGX OS 기본이 아닐 경우 — 더 최신이면 그 버전 사용)
sudo apt update
sudo apt install -y python3.12 python3.12-venv

# ② clone 후 단일 부트스트랩 + systemd --user 등록 (OS 자동 감지)
git clone <repo-url>
cd app
./bogo_ctl.sh setup        # bootstrap(venv+deps+config) → systemd --user 4역할 enable --now

# ③ .env·*_config.json·channels.json 에 실제 값 입력 후 재시작
./bogo_ctl.sh restart
```

부팅 상시 가동(로그아웃·재부팅 후에도 자동 기동)은 **systemd `--user` + linger** 로 보장된다.
`install_service.sh` 가 설치 시 `loginctl enable-linger <user>` 를 자동 시도하며, 권한 문제로 실패하면
수동 1회 실행한다(헤드리스 서버는 로그인 세션이 없으므로 linger 가 필수):

```bash
sudo loginctl enable-linger "$USER"     # 사용자 세션 없이도 user 서비스가 부팅 시 기동
systemctl --user enable --now bogo@orchestrator.service   # (setup 이 이미 4역할 enable)
```

운영 확인·로그:

```bash
./bogo_ctl.sh status                                  # 4역할 active 여부
journalctl --user -u bogo@orchestrator -f             # 역할별 실시간 로그
loginctl show-user "$USER" | grep Linger                # Linger=yes 면 부팅 상시 가동 보장됨
```

> Linux 는 TCC·한글 경로 문제가 없어 **저장소를 인플레이스로 직접 실행**한다(macOS 같은 ASCII 미러 불필요).
> `service/templates/bogo@.service.template` 의 `__WORKDIR__` 가 설치 시점에 repo 절대경로로 치환되며,
> `Restart=always`+`RestartSec=10`+`MemoryMax=1G` 로 크래시·메모리 누수에서 자동 복원한다.

### macOS만의 특수 사정 — 왜 ASCII 미러(`~/.bogo-bin/app`)가 필요한가

macOS 개인정보 보호(TCC)는 launchd가 띄운 백그라운드 에이전트가 **`~/Desktop` 아래 파일의 내용을
읽는 것(open/read)을 차단**한다(디렉토리 목록은 되지만 `cat`은 "Operation not permitted").
이 저장소는 `~/Desktop` 아래 있어, launchd가 직접 실행하면 `.env`·`*_config.json`·`channels.json`·
`agents/*.md`·venv를 못 읽어 **즉시 종료(exit 127)**된다.

→ 해결: `bogo_ctl.sh install` 이 운영 실행본을 비보호 ASCII 경로 **`${HOME}/.bogo-bin/app`** 에 자동
미러링(rsync)하고, 거기서 venv를 만든 뒤 launchd가 그쪽을 실행한다. **저장소가 source of truth**(git 추적),
`${HOME}/.bogo-bin/app` 은 자동 생성·갱신되는 배포 복사본이다.

> **이 미러는 macOS 전용이며 launchd+TCC가 강제하는 우회다.** Linux·Windows 는 미러 없이 저장소를
> 한글 경로 그대로 인플레이스 실행한다. 코드·런처(`run_role.sh`/`run_role.ps1`)는 자기 위치를 동적으로
> 해석하므로 **한글·공백 경로에서도 직접 동작함이 실증됐다**(한글 경로에서 부트스트랩+venv import+런타임
> 로드 통과 확인). macOS 미러는 "런타임이 한글을 못 읽어서"가 아니라 "launchd 프로세스가 한글 경로 인자를
> 깨뜨리고 TCC가 Desktop 읽기를 막아서" 필요한 것이다.

| 위치 | 역할 |
|------|------|
| `<repo>/app` | 소스(git). 코드·설정 편집은 여기서. |
| `${HOME}/.bogo-bin/app` | launchd가 실제 실행하는 운영 복사본(자동 생성) |
| `${HOME}/.bogo-bin/run_role.sh` | launchd가 부르는 런처(.env 로드 후 venv python exec) |
| `${HOME}/Library/LaunchAgents/com.bogo.{orchestrator,hr,dev,admin}.plist` | 등록된 에이전트(템플릿에서 생성) |
| `${HOME}/.bogo-bin/app/logs/<role>.{out,err}.log` | 역할별 stdout/stderr 로그 |

### 최초 설치 (권장: 단일 명령)

```bash
./bogo_ctl.sh setup     # 부트스트랩(venv+의존성+config) → ASCII 미러 생성 → 4개 launchd 등록+기동
```

`setup` 은 내부적으로 미러 동기화, 미러 내 venv 생성, plist 템플릿 치환·설치를 모두 수행한다.
코드/설정 수정 후 재배포는 `./bogo_ctl.sh restart` 한 줄이면 된다(미러 재동기화 + 4역할 재시작 포함).

### 운영 명령 (start / stop / status / 로그)

```bash
UID=$(id -u)

# 상태: 1열=PID, 2열=마지막 종료코드(0=정상), 3열=라벨
launchctl list | grep bogo
ps -p $(launchctl list | grep com.bogo.dev | awk '{print $1}') -o pid,stat,command

# 로그 실시간 보기
tail -f ~/.bogo-bin/app/logs/orchestrator.out.log
tail -f ~/.bogo-bin/app/logs/dev.err.log

# 한 역할 재시작(코드 무관, 강제 재기동)
launchctl kickstart -k gui/$UID/com.bogo.dev

# 중지(stop = 정지, KeepAlive로 다시 살아남 → 완전 중지는 bootout)
launchctl bootout gui/$UID/com.bogo.dev

# 전체 중지/기동/재시작은 bogo_ctl 권장 (admin 포함 4역할 일괄)
./bogo_ctl.sh uninstall   # 전체 중지+해제
./bogo_ctl.sh install     # 전체 등록+기동
```

### 코드/설정 수정 후 배포

```bash
# 저장소에서 편집한 뒤 한 줄 — 미러 재동기화 + 4개 역할 재시작
./bogo_ctl.sh restart
```

### 자동 재시작 검증(실증 완료)

`kill -9 <dev PID>` → 약 10초 내(ThrottleInterval) launchd가 새 PID로 부활,
로그에 부팅 배너 재출력됨을 확인했다. WS 끊김은 코드 내 지수 백오프가 자체 복구하고,
프로세스 자체가 죽으면 launchd KeepAlive가 되살린다(이중 복원).

> 단일 터미널 수동 기동: `./bogo_ctl.sh run <orchestrator|hr|dev|admin>`
> (또는 미러 직접: `${HOME}/.bogo-bin/run_role.sh <role>`)

### 레거시 안내

구 `launchd/` 디렉터리(`sync_app.sh`·구 `run_role.sh`·고정 plist)는 신규 크로스플랫폼 시스템(`bogo_ctl`
+ `bootstrap.*` + `service/templates/`)으로 **완전히 대체되어 제거됐다**. 신규 시스템도 동일한
`${HOME}/.bogo-bin` 경로를 쓰므로, 구 launchd 로 가동 중이던 기존 환경도 `bogo_ctl install`(또는
`BOGO_start.command` 더블클릭) 한 번이면 그대로 인수인계된다(중복 라벨은 bootout 후 재등록).
