# Postmortem — BOGO 원클릭 파이프라인 신선-설치 첫-실행 4대 결함

**날짜**: 2026-07-01
**심각도**: High (신선 리눅스 환경에서 ./BOGO_start.sh 가 매번 다른 단계에서 중단 → 첫 배포 불가)
**감지 → 해결**: 단일 세션 내 (신선 systemd 컨테이너 격리 재현 → 4개 결함 순차 발견·근본수정·재실증 완주)

## 요약
신선 리눅스(systemd, 단일망 LAN) 환경에서 `./BOGO_start.sh` 를 처음부터 끝까지 완주시키는
과정에서, 그동안 아무도 전 파이프라인을 신선 환경에서 실증하지 않아 숨어 있던 4개의
'첫-실행 전용' 결함이 [1/5]~[5/5] 각 단계에서 순차로 드러났다. 모두 근본 원인까지 추적해
구조적으로 수정하고, 수정할 때마다 처음부터 재실행해 무에러 수렴을 확인했다. 최종적으로
완전 초기화(venv·컨테이너·DB·systemd·프로비저닝 config 전부 삭제) 상태에서 실제
`BOGO_start.sh` 를 한 번 실행해 [1/5]~[5/5] 전 단계 rc=0 완주를 라이브 실증했다.

## 타임라인
| 시점 | 이벤트 |
|------|--------|
| T+0  | 신선 systemd Ubuntu 컨테이너 + 격리 dind 데몬(사용자 라이브 bogo-* 완전 분리) 구성 |
| T+1  | [1/5] bootstrap 중단 발견 (BUG#4: pip check 플랫폼-안내 오인) → 수정 → 재실증 |
| T+2  | [3/5] MM readiness 180s 거짓-중단 발견 (BUG#5: 이미지 도구 부재) → 수정 → 재실증 |
| T+3  | [5/5] systemd 유닛 전멸 발견 (BUG#6: WorkingDirectory 따옴표) → 수정 → 재실증 |
| T+4  | [5/5] 대시보드 크래시루프 발견 (BUG#8: accounts_config.json 부재) → 수정 → 재실증 |
| T+5  | 완전 초기화 후 BOGO_start.sh 첫-실행 rc=0 완주 라이브 실증 + 269 테스트 grün |

## 근본 원인 (단계별, 각 7-WHY 요지)

### BUG#4 — [1/5] bootstrap: pip check 가 선택적 GPU transitive 안내를 치명 실패로 오인
- 증상: `nvidia-cusparselt-cu13 0.8.1 is not supported on this platform` → `pip check` rc=1
  → `install_requirements_current_venv` 가 이를 무조건 치명 실패로 보고 부트스트랩 전체 중단.
- 근본: 부트스트랩의 성공 기준이 잘못 지정됨. 실제 런타임 계약은 `validate_runtime_dependencies`
  (websockets/hermes-agent/run_agent 임포트 — 이미 통과)인데, `pip check`(선택적 임베딩
  sentence-transformers→torch 가 끌어온 GPU transitive 의 플랫폼 안내까지 포함)를 엄격
  게이트로 삼았다. CPU-only/비지원 아키텍처(arm64·GPU 없는 x86 서버)에서 이 안내 한 줄이
  전체를 무너뜨린다.
- 수정: `pip_check_ok()` 게이트 신설 — "not supported on this platform" 안내 라인은 걸러내고
  진짜 의존성 충돌(requires ... not installed 등)만 실패시킨다. (`app/bootstrap.sh`)

### BUG#5 — [3/5] backbone: wait_mm_ready 가 이미지 도구 부재로 MM readiness 를 거짓 판정
- 증상: MM 이 실제로 8065 를 서빙 중인데 `health=starting`→`unhealthy` 에 갇혀 180s 소진 후
  "not ready" 로 [3/5] 중단.
- 근본: compose 가 심는 docker healthcheck(`curl -fsS ...`)와 infra_up 의 폴백 ping
  (`docker exec ... sh -c "curl... || wget..."`)이 둘 다 '컨테이너 이미지에 sh + curl/wget
  이 있다'고 가정한다. 최소/distroless 계열 MM 이미지(sh·curl 부재)에서는 healthcheck 명령이
  실행조차 못 돼 매번 실패로 집계, 상태가 unhealthy 로 굳는다. 과거 폴백 분기는 none/starting
  으로만 한정돼 unhealthy 로 굳는 순간 모든 폴백이 건너뛰어졌다.
- 수정: `wait_mm_ready` 가 healthy 가 아닌 '모든' 상태에서 `mmctl --local system version`
  (어떤 MM 이미지에도 존재하는 바이너리, 프로비저닝이 이미 의존)으로 이미지 도구·healthcheck
  판정과 무관한 진짜 readiness 를 확인. (`app/infra_up.sh`)

### BUG#6 — [5/5] systemd: WorkingDirectory 따옴표로 유닛 bad-setting → 전 봇/대시보드 기동 실패
- 증상: `bogo@{orchestrator,hr,dev,admin,dashboard}` + backup 타이머가 전부
  "Unit has a bad unit file setting" → `WorkingDirectory= path is not absolute: "..."`.
- 근본: 템플릿의 `WorkingDirectory="__WORKDIR__"` 가 값 선두에 `"` 를 남겨, systemd 가
  이를 비-절대경로로 판정하고 유닛을 치명 오류 처리한다. systemd 는 지시자별 인용 규칙이
  '다르다' — `WorkingDirectory=` 는 원문 경로(공백/한글도 따옴표 없이 정상), `ExecStart=`
  는 셸형 명령줄 파싱(인자 따옴표가 올바름). 템플릿 저자가 ExecStart 식 인용을 균일 적용.
  macOS launchd(plist)엔 없는 문제라, 리눅스 systemd 경로를 아무도 실증 안 해 방치됨.
- 수정: 두 서비스 템플릿(`bogo@.service.template`, `bogo-backup.service.template`)에서
  `WorkingDirectory=` 따옴표 제거(ExecStart 따옴표는 정당하므로 유지). 공백+한글 경로에서도
  `systemd-analyze verify` 통과 확인.

### BUG#8 — [5/5] dashboard: accounts_config.json 부재로 대시보드 크래시루프
- 증상: `ceo_dashboard.py:153 → ceo_auth.load_accounts → FileNotFoundError:
  accounts_config.json` → systemd 무한 재시작 → [5/5] dashboard health check 실패.
- 근본: `accounts_config.json` 은 운영자 로컬 시크릿이라 git 제외 대상이고, bootstrap 의
  config copy 목록·프로비저닝 어디에도 생성 경로가 없다. 그런데 `load_accounts` 가 파일을
  무조건 open 해 신선 설치에서 즉사한다. 인증은 이중 경로(1차 Mattermost 로그인 + 2차
  로컬 config 폴백)로 설계돼, 로컬 config 가 비어도 프로비저닝된 admin 계정으로 MM 인증이
  정상 동작한다. 즉 파일 부재는 '폴백 계정 0개'로 graceful degrade 하는 게 옳다.
- 수정: `load_accounts` 가 파일 부재/손상 시 빈 dict 반환(하드 크래시 금지, MM 인증 경로
  보존). (`app/ceo_auth.py`)

## 버그 클래스 전수 조사 결과
- BUG#4 (`pip check` 엄격 게이트): `bootstrap.sh` 단일 사용처. Windows `bootstrap.ps1` 엔
  해당 게이트 없음 → 병렬 수정 불요.
- BUG#5 (in-container sh/curl 가정): `wait_mm_ready` 단일 사용처. backup/restore 의
  `docker exec` 는 postgres 이미지 상시 존재 도구(pg_dump/psql)라 갭 없음.
- BUG#6 (`WorkingDirectory="..."` 따옴표): 2개 systemd 템플릿 모두에 존재 → 둘 다 수정.
  launchd plist(XML)는 구조가 달라 무관.
- BUG#8 (미프로비저닝 시크릿 무조건 open): 런타임 진입점 전수 조사 →
  `ceo_dashboard.py` 의 `{nk}_config.json` 은 이미 `os.path.isfile` 가드 + 명확한 에러 존재.
  `bogo_runtime`/`ceo_admin` 의 `*_config.json` 은 프로비저닝이 봇 기동 전에 생성. `accounts_config.json`
  만 무가드였음 → 유일 결함, 수정 완료.

## 재발 방지
- 단기(적용됨): 회귀 테스트 `app/tests/test_oneclick_fresh_pipeline_regression.py`(10건)로
  4개 버그를 각각 정확히 재현·고정. 기존 `test_infra_up_shell.py` 의 잘못된 계약
  (`WorkingDirectory="..."` 요구 — 버그를 코드화한 테스트)을 정정.
- 장기(권고): 신선 설치 파이프라인을 CI 에서 주기적으로 systemd 컨테이너로 완주 검증하는
  잡을 추가하면 '첫-실행 전용' 결함을 조기 포착할 수 있다(이번 4개 모두 idempotent 재실행
  경로에는 안 나타나고 오직 신선 첫-실행에서만 드러났다 — 이것이 근본 사각지대였다).

## 이 버그들이 놓친 이유
- 공통 근본: 전 파이프라인을 '신선 환경'에서 처음부터 끝까지 완주시켜 실증한 적이 없었다.
  기존 개발 머신은 이미 venv·컨테이너·토큰·config·git 이 갖춰져 idempotent 재실행 경로만
  타서, 첫-실행에서만 터지는 결함(플랫폼-안내 pip, 이미지 도구 부재, 유닛 따옴표, 미프로비저닝
  시크릿)이 전부 가려졌다. 리눅스 systemd 경로(macOS launchd 와 분기)는 특히 실증 공백이 컸다.
