# 사내 보고 멀티에이전트 (Hermes Agent + DeepSeek V4 Flash)

3개 업무 에이전트(박민철=비서실장, 이다은=인사총무, 최지현=개발)가 Mattermost 채널을 통로로
상향(팀→박민철→CEO)·하향(CEO→박민철→팀) 보고를 자율 라우팅한다.

## 아키텍처

| 레이어 | 구성 |
|--------|------|
| 두뇌(추론) | **Hermes Agent**(Nous Research) `AIAgent` 런타임 + 모델 **DeepSeek V4 Flash**(OpenRouter 경유) |
| 통로(메시징) | **Mattermost** — WebSocket 수신 → REST 게시 (커스텀 게이트웨이 어댑터) |
| 라우팅 | 매 메시지마다 `decide()`가 LLM JSON으로 '내 차례인가 + 무엇을' 자율 판단. 멘션으로 다음 행위자 핸드오프 |

- 진입점: `hermes_runtime.py`  (실행: `<venv>/python hermes_runtime.py <orchestrator|hr|dev>`)
- 두뇌 호출은 `run_agent.AIAgent.chat()`이 담당(provider 레이어·재시도·대화 루프 내부 처리). 도구는 전부 비활성 → 순수 JSON 판단만 생성.
- 폭주 방지(규칙 아닌 구조): 봇 메시지는 멘션될 때만 판단(메아리 차단), 끝나면 `act=false` 침묵 / `task_status=closed`.
- 복원력(구조): WebSocket이 끊기면 지수 백오프(최대 30s)로 자동 재접속해 데몬이 죽지 않는다. 단일 메시지의 LLM/JSON/Mattermost 오류는 해당 메시지만 드롭하고 루프는 유지한다. 토큰 불량(WS 인증 실패)은 재시도 무의미하므로 즉시 중단해 운영자에게 알린다.

> 주의: 진입 파일명을 `agent.py`로 두면 hermes-agent 패키지의 `agent` 모듈을 가려(shadow) `run_agent` import가 깨진다.
> 반드시 `hermes_runtime.py` 등 다른 이름을 쓴다.

---

## 에이전트 정의 3계층 분리 (데이터 주도 구조)

라우팅·규칙·페르소나가 파이썬에 하드코딩되던 구조를 3계층으로 분리해, **새 팀/에이전트 추가 시 파이썬을 수정하지 않는다.**

| 계층 | 위치 | 내용 |
|------|------|------|
| 고유 페르소나 | `agents/<role>.md` 1파일 | 9섹션 구조(역할·범위·도구·출력계약·예시·가드레일·소싱·완료·에스컬레이션). 그 역할만의 행동·도메인 지식 |
| 공유 규칙(상속) | `agents/_shared/common_rules.md` | 보고 포맷·작성 원칙·진행 공유·안전·소싱·완료 조건·행동결정 JSON 스키마. **런타임이 모든 에이전트 시스템 프롬프트에 자동 주입**. 한 곳만 고치면 전원 반영 |
| 선언적 라우팅 | `teams.json` + `channels.json` | 팀(팀방·보고라인방·팀에이전트·상향대상)·채널(이름→ID) 데이터. 런타임이 `build_routing()`으로 ROUTING 텍스트를 동적 생성 |

공용 모듈: `agent_schema.py`(파싱·검증·ROUTING 생성·프롬프트 조립), `mm_client.py`(LLM·Mattermost REST). `hermes_runtime.py`와 `ceo_admin_runtime.py`가 공유한다.

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
./hermes_ctl.sh restart
```

> 운영자 주의: 작업방 채널을 자동 생성하지 못하는 환경(토큰 권한 부족·서버 미가동)이면, Mattermost
> **관리자 패널 → 팀 → 채널 생성**으로 공개 채널을 만들고 참여 봇 3개를 멤버로 추가한 뒤,
> 채널 ID를 `channels.json`(시크릿, `.gitignore`)에 입력한다. `channels.json.example` 의
> `"정책기획실":"CHANNEL_ID"` 는 자리표시자이며 실제 ID를 커밋하지 않는다.

### CEO 에이전트 업데이트 파이프라인 (자연어로 에이전트 정의 수정)

전용 방 **`CEO-에이전트관리`** 에서 CEO가 자연어로 피드백하면 **에이전트 관리 봇**(`ceo_admin_runtime.py`)이:

1. 의도 파싱 — 예: `박민철 보고를 3줄로 줄여` → 대상 에이전트 + 변경 지시 추출
2. 해당 `agents/<role>.md` 를 LLM으로 재작성 → **unified diff 미리보기**를 방에 게시(pending)
3. CEO `적용`(또는 반영/승인) → md 파일 반영 + **git commit(한국어)** + 운영본 동기화 + 해당 역할 데몬 리로드
4. CEO `반려`(또는 취소/폐기) → 대기 변경 폐기

안전장치(하드 게이트): **`agents/*.md`(페르소나)만 수정 가능** — 다른 파일·시스템 명령 불가. **적용 전 반드시 diff 승인 게이트**. frontmatter 필수필드를 깨면 적용 거부. 모든 변경은 git 추적. 봇은 자기 메시지에 반응하지 않는다(메아리 차단).

```bash
# 수동 기동
.venv/bin/python ceo_admin_runtime.py
# 또는 launchd (admin 역할)
~/.hermes-bin/run_role.sh admin
```

---

## CEO 대시보드 (한 곳에서 모니터링·지시)

CEO가 Mattermost 채널을 일일이 오가지 않고 **브라우저 한 곳**(`http://127.0.0.1:8787`)에서 부서 현황을 보고 지시를 내리는 로컬 웹앱(`ceo_dashboard.py`). 기존 통신 인프라를 그대로 재사용한다 — `mm_client.MM`(REST), `channels.json`/`teams.json`/`agents/*.md`(`agent_schema`). 신규 의존성·신규 API 키 0(표준 라이브러리 `http.server`만 사용).

화면 구성:
- **부서별 현황 카드** — 팀 채널·보고라인·CEO브리핑의 최근 메시지를 12초 주기로 폴링 표시(작성자·시각·본문). 모니터링 채널은 `teams.json`에서 동적으로 결정된다(하드코딩 없음).
- **CEO 지시 입력창** — 대상 채널 선택(기본 `CEO브리핑` → 박민철 비서실장에게 전달) 후 메시지 게시. 게시는 화이트리스트 채널로만 제한된다.
- **에이전트 현황** — 등록 role 목록(`load_roles`)과 각 봇의 활성 여부(Mattermost user API), 담당 채널 표시.
- **에이전트 관리 안내** — 정의 변경은 기존 `CEO-에이전트관리`(`ceo_admin_runtime`) 파이프라인으로 연계. 대시보드 자체는 정의 파일을 건드리지 않는다(모니터링·지시 전용).

```bash
# 수동 기동 (포트 기본 8787)
.venv/bin/python ceo_dashboard.py
# 포트 변경
HERMES_DASHBOARD_PORT=9000 .venv/bin/python ceo_dashboard.py
# 접속: http://127.0.0.1:8787   (외부 노출 안 됨 — 루프백 전용)
```

게시용 봇 토큰은 박민철(`nk_config.json`)을 재사용한다(CEO브리핑·양 보고라인 멤버라 읽기/쓰기 권한 보유). 토큰이 비어 있거나 플레이스홀더면 기동을 거부한다. 테스트: `.venv/bin/python -m unittest test_ceo_dashboard`.

> 보안: 웹서버는 `127.0.0.1`에서만 listen하며 외부(0.0.0.0)로 바꾸지 않는다. 인증 게이트가 없는 로컬 전용 대시보드이므로, 원격 접근이 필요하면 SSH 로컬 포워딩 등 별도 인증 경계를 둔다.

---

## 가장 쉬운 시작 — 더블클릭 (macOS)

터미널 타이핑이 귀찮으면 프로젝트 루트의 **`헤르메스 시작.command`** 파일을 Finder에서 더블클릭한다.

- 아직 미설치면 → 자동으로 `setup`(venv+의존성+config+launchd 등록) 수행
- 이미 상시 가동 중이면 → 중복 기동 없이 최신 코드 재배포 + 4역할 재시작(`restart`)
- 끝나면 현재 상태(PID/종료코드/역할)를 한국어로 표시하고, 오류 시 창이 닫히지 않고 원인을 보여준다

내부적으로 `reporting/hermes_ctl.sh` 를 호출할 뿐이라 동작은 아래 명령들과 동일하다.
(처음 다운로드 시 `우클릭 → 열기` 한 번으로 Gatekeeper 허용)

## 가장 쉬운 시작 — 더블클릭 (Windows)

mac 절과 완전 대칭. 프로젝트 루트의 **`헤르메스 시작.bat`** 파일을 탐색기에서 더블클릭한다.

- 아직 미등록이면 → 자동으로 `setup`(venv+의존성+config+**Task Scheduler** 등록) 수행
- 이미 상시 가동 등록돼 있으면 → 중복 등록 없이 최신 코드 재배포 + 4역할 재시작(`restart`)
- 끝나면 현재 상태(역할별 Task State)를 한국어로 표시하고, 오류 시 창이 닫히지 않고(`pause`) 원인을 보여준다

내부 동작: `헤르메스 시작.bat`(UTF-8 `chcp 65001`, `cd /d "%~dp0"` 로 한글·공백 경로 고정)
→ `헤르메스 시작.launcher.ps1`(가동 상태 감지·분기 본체)
→ `reporting\hermes_ctl.ps1 {setup|restart|status}`. mac 의 `.command`→`hermes_ctl.sh` 경로와 1:1 등가다.

- 가동 상태는 `Get-ScheduledTask -TaskName "Hermes_*"` 존재 여부로 감지한다(mac 의 `launchctl list | grep com.hermes` 등가).
- PowerShell 7(`pwsh`)이 있으면 그것을, 없으면 Windows 기본 `powershell` 5.1 을 자동으로 사용한다.
- 처음이라면 `python.org 3.12`(설치 시 "Add to PATH" 체크) 설치 후 더블클릭하면 부트스트랩이 venv 부터 자동 구성한다.

---

## 다른 PC에서 시작 (mac / Windows / Linux 공통)

어느 OS에서 `git clone` 하거나 폴더를 복사하든 **단일 부트스트랩 한 번 → 상시 가동**이 되도록 설계됐다.
경로는 사용자명·설치 위치·한글 폴더명과 무관하게 동작한다(`/Users/<이름>` 같은 하드코딩 0건).

```bash
git clone <repo-url>
cd reporting
```

### macOS / Linux

```bash
./hermes_ctl.sh setup       # ① venv 휴대용 재생성 + 의존성 설치 + *.example→config 복사
                            #   ② OS 감지해 상시 가동 등록 (mac=launchd / linux=systemd --user)
# setup 안내대로 .env·*_config.json·channels.json 에 실제 값 입력 → 재시작
./hermes_ctl.sh restart
```

부트스트랩만: `./hermes_ctl.sh bootstrap` · 서비스만: `./hermes_ctl.sh install`
수동 단일 실행: `./hermes_ctl.sh run orchestrator` · 상태: `./hermes_ctl.sh status`

### Windows (PowerShell)

```powershell
pwsh ./hermes_ctl.ps1 setup    # venv + 의존성 + config 복사 후 Task Scheduler 등록
# .env·*_config.json·channels.json 에 실제 값 입력 후
pwsh ./hermes_ctl.ps1 restart
```

> **Python 3.12 필수**(3.14는 일부 wheel 미제공). 없으면 부트스트랩이 설치 안내 후 멈춘다.
> mac: `brew install python@3.12` · Ubuntu: `sudo apt install python3.12 python3.12-venv`
> Windows: [python.org 3.12](https://www.python.org/downloads/release/python-3120/) (설치 시 "Add to PATH" 체크)

### 시크릿은 커밋되지 않는다 (부트스트랩이 복사)

`.env`·`*_config.json`·`channels.json` 은 `.gitignore` 로 커밋이 차단된다.
부트스트랩이 `*.example` 을 실제 파일로 **없을 때만** 복사하며(기존 값 보존), 그 뒤 직접 값을 채운다.
`.venv` 도 추적하지 않는다 — 부트스트랩이 그 PC에서 항상 새로 만들어 절대경로 핀 문제를 원천 차단한다.

### 설정 파일 참고

| 파일 | 내용 |
|------|------|
| `llm_config.json` | OpenRouter `api_key`, `model`(deepseek/deepseek-v4-flash), `base_url` |
| `nk_config.json` | 박민철 봇 Mattermost 토큰·사용자 ID |
| `genz_config.json` | 이다은 봇 Mattermost 토큰·사용자 ID |
| `gyaru_config.json` | 최지현 봇 Mattermost 토큰·사용자 ID |
| `channels.json` | 채널명 → 채널 ID 매핑 (Mattermost 관리자 패널에서 확인) |
| `teams.json` | 선언적 팀·라우팅 데이터(팀방·보고라인·팀에이전트·상향대상) + `collab_rooms`(여러 에이전트 공동 작업방). 새 팀/협업방 추가는 여기 블록 1개로 |
| `agents/_shared/common_rules.md` | 전 에이전트 공통 규칙(런타임이 상속 주입). 시크릿 아님, git 추적 |

인증: OpenRouter 키(기존) 재사용. 신규 API 키 요구 없음.

---

## 상시 무중단 가동 (3 OS 크로스플랫폼)

4개 역할(`orchestrator`/`hr`/`dev` + CEO 관리 봇 `admin`)을 OS별 네이티브 서비스로 등록한다.
**로그인/부팅 시 자동 기동 + 비정상 종료 시 자동 재시작**. 단일 진입점 `hermes_ctl` 이 OS를 감지해 분기한다.

| OS | 서비스 메커니즘 | 자동 재시작 | 한글 경로 |
|----|----------------|------------|----------|
| macOS | launchd 사용자 에이전트 (`RunAtLoad`+`KeepAlive`+`ThrottleInterval 10`) | KeepAlive | ASCII 미러 경유(아래 사유) |
| Linux | systemd `--user` 인스턴스 (`Restart=always`+`MemoryMax=1G`) | systemd + OOM 가드 | 직접 동작(인플레이스) |
| Windows | Task Scheduler (로그온 트리거 + 실패 시 1분마다 재시작) | 스케줄러 재시작 정책 | 직접 동작(인플레이스) |

```bash
# mac/linux
./hermes_ctl.sh install      # 등록+기동      ./hermes_ctl.sh uninstall   # 등록 해제
./hermes_ctl.sh restart      # 재배포+재시작  ./hermes_ctl.sh status      # 상태

# windows
pwsh ./hermes_ctl.ps1 install ; pwsh ./hermes_ctl.ps1 status
```

모든 서비스 파일은 **템플릿**(`service/templates/`)에서 설치 시점에 `${HOME}`·repo 절대경로로 치환 생성된다 —
사용자명·설치 위치가 어디든 자동 적응한다(하드코딩 0건).
Linux 로그: `journalctl --user -u hermes@orchestrator -f`. Windows: 작업 스케줄러 기록.

### macOS만의 특수 사정 — 왜 ASCII 미러(`~/.hermes-bin/app`)가 필요한가

macOS 개인정보 보호(TCC)는 launchd가 띄운 백그라운드 에이전트가 **`~/Desktop` 아래 파일의 내용을
읽는 것(open/read)을 차단**한다(디렉토리 목록은 되지만 `cat`은 "Operation not permitted").
이 저장소는 `~/Desktop` 아래 있어, launchd가 직접 실행하면 `.env`·`*_config.json`·`channels.json`·
`agents/*.md`·venv를 못 읽어 **즉시 종료(exit 127)**된다.

→ 해결: `hermes_ctl.sh install` 이 운영 실행본을 비보호 ASCII 경로 **`${HOME}/.hermes-bin/app`** 에 자동
미러링(rsync)하고, 거기서 venv를 만든 뒤 launchd가 그쪽을 실행한다. **저장소가 source of truth**(git 추적),
`${HOME}/.hermes-bin/app` 은 자동 생성·갱신되는 배포 복사본이다.

> **이 미러는 macOS 전용이며 launchd+TCC가 강제하는 우회다.** Linux·Windows 는 미러 없이 저장소를
> 한글 경로 그대로 인플레이스 실행한다. 코드·런처(`run_role.sh`/`run_role.ps1`)는 자기 위치를 동적으로
> 해석하므로 **한글·공백 경로에서도 직접 동작함이 실증됐다**(한글 경로에서 부트스트랩+venv import+런타임
> 로드 통과 확인). macOS 미러는 "런타임이 한글을 못 읽어서"가 아니라 "launchd 프로세스가 한글 경로 인자를
> 깨뜨리고 TCC가 Desktop 읽기를 막아서" 필요한 것이다.

| 위치 | 역할 |
|------|------|
| `<repo>/reporting` | 소스(git). 코드·설정 편집은 여기서. |
| `${HOME}/.hermes-bin/app` | launchd가 실제 실행하는 운영 복사본(자동 생성) |
| `${HOME}/.hermes-bin/run_role.sh` | launchd가 부르는 런처(.env 로드 후 venv python exec) |
| `${HOME}/Library/LaunchAgents/com.hermes.{orchestrator,hr,dev,admin}.plist` | 등록된 에이전트(템플릿에서 생성) |
| `${HOME}/.hermes-bin/app/logs/<role>.{out,err}.log` | 역할별 stdout/stderr 로그 |

### 최초 설치 (권장: 단일 명령)

```bash
./hermes_ctl.sh setup     # 부트스트랩(venv+의존성+config) → ASCII 미러 생성 → 4개 launchd 등록+기동
```

`setup` 은 내부적으로 미러 동기화, 미러 내 venv 생성, plist 템플릿 치환·설치를 모두 수행한다.
코드/설정 수정 후 재배포는 `./hermes_ctl.sh restart` 한 줄이면 된다(미러 재동기화 + 4역할 재시작 포함).

### 운영 명령 (start / stop / status / 로그)

```bash
UID=$(id -u)

# 상태: 1열=PID, 2열=마지막 종료코드(0=정상), 3열=라벨
launchctl list | grep hermes
ps -p $(launchctl list | grep com.hermes.dev | awk '{print $1}') -o pid,stat,command

# 로그 실시간 보기
tail -f ~/.hermes-bin/app/logs/orchestrator.out.log
tail -f ~/.hermes-bin/app/logs/dev.err.log

# 한 역할 재시작(코드 무관, 강제 재기동)
launchctl kickstart -k gui/$UID/com.hermes.dev

# 중지(stop = 정지, KeepAlive로 다시 살아남 → 완전 중지는 bootout)
launchctl bootout gui/$UID/com.hermes.dev

# 전체 중지/기동/재시작은 hermes_ctl 권장 (admin 포함 4역할 일괄)
./hermes_ctl.sh uninstall   # 전체 중지+해제
./hermes_ctl.sh install     # 전체 등록+기동
```

### 코드/설정 수정 후 배포

```bash
# 저장소에서 편집한 뒤 한 줄 — 미러 재동기화 + 4개 역할 재시작
./hermes_ctl.sh restart
```

### 자동 재시작 검증(실증 완료)

`kill -9 <dev PID>` → 약 10초 내(ThrottleInterval) launchd가 새 PID로 부활,
로그에 부팅 배너 재출력됨을 확인했다. WS 끊김은 코드 내 지수 백오프가 자체 복구하고,
프로세스 자체가 죽으면 launchd KeepAlive가 되살린다(이중 복원).

> 단일 터미널 수동 기동: `./hermes_ctl.sh run <orchestrator|hr|dev|admin>`
> (또는 미러 직접: `${HOME}/.hermes-bin/run_role.sh <role>`)

### 레거시 안내

`launchd/` 디렉터리(`sync_app.sh`·구 `run_role.sh`·고정 plist)는 신규 크로스플랫폼 시스템(`hermes_ctl`
+ `bootstrap.*` + `service/`)으로 대체됐다. 기존 macOS 가동과의 호환을 위해 보존만 하며, 신규 설치는
위의 `hermes_ctl` 경로를 사용한다. 신규 시스템도 동일한 `${HOME}/.hermes-bin` 경로를 쓰므로 기존
가동을 깨지 않고 그대로 인수인계된다.
