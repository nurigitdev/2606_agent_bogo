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
> 반드시 `hermes_runtime.py` 등 다른 이름을 쓴다. (구버전 urllib 구현은 `agent_legacy_urllib.py.bak`에 보존)

## 설치

```bash
python3.12 -m venv .venv          # Python 3.12 권장(3.14는 일부 의존성 wheel 미제공)
.venv/bin/pip install -r requirements.txt
```

## 설정 (secret — git 미커밋, *.example 참고)

- `llm_config.json` : OpenRouter `api_key`, `model`(deepseek/deepseek-v4-flash), `base_url`
- `nk_config.json` / `genz_config.json` / `gyaru_config.json` : Mattermost 봇 토큰·ID
- `channels.json` : 채널명 → 채널 ID 매핑

인증: OpenRouter 키(기존) 재사용. 신규 API 키 요구 없음.

## 실행

```bash
.venv/bin/python hermes_runtime.py orchestrator   # 박민철
.venv/bin/python hermes_runtime.py hr             # 이다은
.venv/bin/python hermes_runtime.py dev            # 최지현
```
