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

인증: OpenRouter 키(기존) 재사용. 신규 API 키 요구 없음.

---

## macOS 상시 무중단 가동 (launchd)

3개 역할(`orchestrator`/`hr`/`dev`)을 launchd 사용자 에이전트로 등록한다.
**로그인 시 자동 기동(RunAtLoad) + 비정상 종료 시 자동 재시작(KeepAlive)**.

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
| `~/Library/LaunchAgents/com.hermes.{orchestrator,hr,dev}.plist` | 등록된 에이전트 |
| `~/.hermes-bin/app/logs/<role>.{out,err}.log` | 역할별 stdout/stderr 로그 |

### 최초 설치

```bash
# 1) 운영 복사본 동기화 (소스 -> ~/.hermes-bin/app)
launchd/sync_app.sh

# 2) 런처 배치
mkdir -p ~/.hermes-bin
cp launchd/run_role.sh ~/.hermes-bin/run_role.sh && chmod +x ~/.hermes-bin/run_role.sh

# 3) plist 설치 + 로드 (modern API)
for r in orchestrator hr dev; do
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
