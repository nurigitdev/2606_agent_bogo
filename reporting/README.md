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

## macOS 데몬 자동 실행 (launchd)

`launchd/` 디렉토리의 plist 파일을 사용해 macOS 로그인 시 자동 시작할 수 있다.

```bash
# plist 설치 (사용자 LaunchAgent)
cp launchd/com.hermes.orchestrator.plist ~/Library/LaunchAgents/
cp launchd/com.hermes.hr.plist ~/Library/LaunchAgents/
cp launchd/com.hermes.dev.plist ~/Library/LaunchAgents/

# 로드
launchctl load ~/Library/LaunchAgents/com.hermes.orchestrator.plist
launchctl load ~/Library/LaunchAgents/com.hermes.hr.plist
launchctl load ~/Library/LaunchAgents/com.hermes.dev.plist
```

또는 `run_role.sh <orchestrator|hr|dev>` 스크립트로 수동 기동할 수 있다.
