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

## 다른 PC에서 처음 시작하기

```bash
# 1. 저장소 클론
git clone <repo-url>
cd reporting

# 2. 가상환경 생성 및 의존성 설치 (Python 3.12 권장 — 3.14는 일부 wheel 미제공)
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. 시크릿 설정 파일 생성 (*.example → 실제 파일로 복사 후 값 입력)
cp llm_config.json.example llm_config.json       # OpenRouter API 키, 모델명
cp nk_config.json.example nk_config.json         # 박민철 봇 토큰·사용자 ID
cp genz_config.json.example genz_config.json     # 이다은 봇 토큰·사용자 ID
cp gyaru_config.json.example gyaru_config.json   # 최지현 봇 토큰·사용자 ID
cp channels.json.example channels.json           # 채널명 → 채널 ID 매핑

# 각 JSON 파일을 편집기로 열어 실제 값으로 채운다
# (토큰·API 키는 git에 절대 커밋되지 않음 — .gitignore에 의해 보호)

# 4. 에이전트 실행 (역할별 별도 터미널)
.venv/bin/python hermes_runtime.py orchestrator  # 박민철(비서실장)
.venv/bin/python hermes_runtime.py hr            # 이다은(인사총무)
.venv/bin/python hermes_runtime.py dev           # 최지현(개발)
```

### 설정 파일 참고

| 파일 | 내용 |
|------|------|
| `llm_config.json` | OpenRouter `api_key`, `model`(deepseek/deepseek-v4-flash), `base_url` |
| `nk_config.json` | 박민철 봇 Mattermost 토큰·사용자 ID |
| `genz_config.json` | 이다은 봇 Mattermost 토큰·사용자 ID |
| `gyaru_config.json` | 최지현 봇 Mattermost 토큰·사용자 ID |
| `channels.json` | 채널명 → 채널 ID 매핑 (Mattermost 관리자 패널에서 확인) |
| `teams.json` | 선언적 팀·라우팅 데이터(팀방·보고라인·팀에이전트·상향대상). 새 팀 추가는 여기 블록 1개로 |
| `agents/_shared/common_rules.md` | 전 에이전트 공통 규칙(런타임이 상속 주입). 시크릿 아님, git 추적 |

인증: OpenRouter 키(기존) 재사용. 신규 API 키 요구 없음.

---

## macOS 상시 무중단 가동 (launchd)

4개 역할(`orchestrator`/`hr`/`dev` + CEO 관리 봇 `admin`)을 launchd 사용자 에이전트로 등록한다.
**로그인 시 자동 기동(RunAtLoad) + 비정상 종료 시 자동 재시작(KeepAlive)**.
(`admin`은 `run_role.sh admin` → `ceo_admin_runtime.py`. 나머지는 `hermes_runtime.py <role>`.)

### 핵심 구조 — 왜 운영 복사본(`~/.hermes-bin/app`)이 별도로 있는가

macOS 개인정보 보호(TCC)는 launchd가 띄운 백그라운드 에이전트가 **`~/Desktop` 아래 파일의 내용을
읽는 것(open/read)을 차단**한다(디렉토리 목록은 되지만 `cat`은 "Operation not permitted").
이 저장소는 `~/Desktop` 아래 있어, launchd가 직접 실행하면 `.env`·`*_config.json`·`channels.json`·
`agents/*.md`·venv를 못 읽어 **즉시 종료(exit 127)**된다.

→ 해결: 운영 실행본을 비보호 ASCII 경로 **`~/.hermes-bin/app`** 에 복사해 두고 launchd는 그쪽을 실행한다.
**Desktop 저장소가 source of truth**(git 추적), `~/.hermes-bin/app`은 배포 복사본이다.
(런처/plist의 모든 경로가 ASCII인 이유도 동일 — launchd가 한글 경로 바이트를 깨뜨린다.)

| 위치 | 역할 |
|------|------|
| `~/Desktop/.../reporting` | 소스(git). 코드·설정 편집은 여기서. |
| `~/.hermes-bin/app` | launchd가 실제 실행하는 운영 복사본 |
| `~/.hermes-bin/run_role.sh` | launchd가 부르는 런처(.env 로드 후 venv python exec) |
| `~/Library/LaunchAgents/com.hermes.{orchestrator,hr,dev,admin}.plist` | 등록된 에이전트 |
| `~/.hermes-bin/app/logs/<role>.{out,err}.log` | 역할별 stdout/stderr 로그 |

### 최초 설치

```bash
# 1) 운영 복사본 동기화 (소스 -> ~/.hermes-bin/app)
launchd/sync_app.sh

# 2) 런처 배치
mkdir -p ~/.hermes-bin
cp launchd/run_role.sh ~/.hermes-bin/run_role.sh && chmod +x ~/.hermes-bin/run_role.sh

# 3) plist 설치 + 로드 (modern API). admin = CEO 에이전트 관리 봇(nk 봇 재사용)
for r in orchestrator hr dev admin; do
  cp launchd/com.hermes.$r.plist ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hermes.$r.plist
done
```

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

# 전체 중지
for r in orchestrator hr dev; do launchctl bootout gui/$UID/com.hermes.$r; done

# 전체 기동
for r in orchestrator hr dev; do launchctl bootstrap gui/$UID/com.hermes.$r.plist 2>/dev/null || \
  launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.hermes.$r.plist; done
```

### 코드/설정 수정 후 배포

```bash
# Desktop 저장소에서 편집한 뒤, 운영 복사본에 반영 + 3개 에이전트 재시작
launchd/sync_app.sh --restart
```

### 자동 재시작 검증(실증 완료)

`kill -9 <dev PID>` → 약 10초 내(ThrottleInterval) launchd가 새 PID로 부활,
로그에 부팅 배너 재출력됨을 확인했다. WS 끊김은 코드 내 지수 백오프가 자체 복구하고,
프로세스 자체가 죽으면 launchd KeepAlive가 되살린다(이중 복원).

> 단일 터미널 수동 기동이 필요하면: `~/.hermes-bin/run_role.sh <orchestrator|hr|dev>`
