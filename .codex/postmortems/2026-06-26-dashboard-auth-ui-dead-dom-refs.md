# Postmortem — CEO 대시보드 인증 UI 무동작(로그아웃·로그인)

**날짜**: 2026-06-26
**심각도**: Medium (인증 UI 무동작 — 데이터 유출/손상 없음, 루프백 전용)
**감지 → 해결**: 약 1세션 (증상 접수 → 근본원인 특정 → 수정·검증·재기동)
**대상**: `app/ceo_dashboard.py` (127.0.0.1:8642 단일 파일 대시보드)

## 증상
- 로그아웃 버튼을 눌러도 동작하지 않음.
- ceo 등 일부 계정이 로그인 후 화면이 정상 진입하지 않는다는 보고.

## 타임라인
| 시각 | 이벤트 |
|------|--------|
| 13:38 | `ceo_dashboard.py` 디스크 수정(이전 세션, topbar/dock-hint 제거 후속) |
| 13:50 | 8642 프로세스 재기동(PID 65529) — 이 시점 디스크 코드 서빙 |
| (증상 접수) | 로그아웃 무동작·일부 로그인 실패 보고 |
| T+0 | 서버 API(POST /api/login·/api/logout) 정상 200 실증 |
| T+1 | 프론트 INDEX_HTML JS 에 죽은 DOM 참조 6개 발견(근본원인) |
| T+2 | 죽은 참조 전부 제거 + 회귀 테스트 추가 |
| T+3 | 대시보드 재기동(PID 79169) + 5계정 전 흐름 재검증 PASS |

## 근본 원인 (7-WHY)
- **증상**: 로그아웃 버튼 무동작 / 일부 계정 로그인 후 화면 깨짐.
- **Why 1**: 왜 로그아웃이 안 되는가? → logout 클릭 핸들러(`getElementById('logout').addEventListener`)가 바인딩되지 않음.
- **Why 2**: 왜 바인딩이 안 됐나? → 그 바인딩이 들어있는 `init()` 즉시실행(IIFE) 함수가 그 줄에 도달하기 전에 중단됨.
- **Why 3**: 왜 IIFE 가 중단되는가? → IIFE 본문에서 존재하지 않는 DOM 요소를 비가드로 `getElementById('x').멤버` 접근하면 `TypeError: Cannot set properties of null` 이 던져지고 이후 코드(logout 바인딩 포함)가 전부 실행되지 않음.
- **Why 4**: 왜 존재하지 않는 요소를 참조했나? → topbar/dock-hint 정리 커밋(4dbffbf)이 `stageTitle·statAgents·svAgents·svPending·svReports·targetName` DOM 을 삭제했으나, 그 요소를 가리키는 JS 참조(죽은 참조)는 그대로 남았다.
- **Why 5**: 왜 죽은 참조가 남았나? → HTML/CSS/JS 가 한 파이썬 파일에 문자열로 내장되어 있어, DOM 삭제와 참조 삭제가 같은 변경 단위로 강제되지 않음(타입 시스템·컴파일러가 잡아주지 않는 동적 문자열 영역).
- **Why 6**: 왜 그런 구조인가? → 의존성 0(http.server·표준 라이브러리만) 원칙으로 빌드 단계·정적 검사 없이 HTML 을 문자열로 직접 서빙하는 설계를 택함. 대가로 "DOM-참조 정합성"을 강제하는 게이트가 부재.
- **Why 7 (근본)**: 왜 정합성 게이트가 없었나? → 렌더된 HTML 의 `id` 정의 집합과 JS 의 `getElementById` 참조 집합이 일치하는지 검사하는 회귀 테스트가 없었다. 즉 **"죽은 DOM 참조"를 구조적으로 차단하는 안전망이 설계에 빠져 있었던 것**이 근본 원인.

> 참고: 최초 가설이었던 "중복 id='logout' 로 인한 getElementById 오작동"은 재현으로 반박됨. 두 logout 은 서로 다른 문서(INDEX_HTML, VAULT_HTML)에 하나씩 존재하며 한 페이지에 동시 로드되지 않으므로 중복 id 가 아니다. 전 문서 중복 id 0건 확인.

## 수정 내용
`app/ceo_dashboard.py` `build_index_html()` 의 죽은 DOM 참조 6개를 전부 제거(증상 억제 아닌 구조적 제거):
- `setStageTitle()` 함수와 그 전용 죽은 코드(`VIEW_TITLE`, `ellip`) 및 호출처 2곳 제거.
- `refreshStats()` 의 `svAgents/svPending/svReports` 참조 제거.
- role 분기의 `statAgents` 참조 2곳 제거(빈 else 블록 제거).
- 데이터 로드의 `targetName` 참조 제거.
- `cmdkKbd` 접근을 비가드 → 가드(`if(kb)`)로 방어적 처리.

## 버그 클래스 전수 조사 결과
- 조사 범위: 리포지토리 전체 `*.py/*.html/*.js`(node_modules·.venv 제외).
- `getElementById` 사용 파일: `app/ceo_dashboard.py` 1개(+테스트).
- **죽은 참조 보유 파일 0건**(수정 후). 동일 패턴 잔존 없음.

## 재발 방지
- **단기(적용 완료)**: `test_ceo_dashboard.py` 에 `RenderedHtmlIntegrityTest` 추가 —
  렌더된 LOGIN/INDEX/VAULT HTML 에서 (1) 중복 id 0건, (2) 모든 `getElementById`
  참조가 정의된 id 에 존재(죽은 참조 0건), (3) 로그인/로그아웃 트리거·핸들러 생존을
  강제. 이제 DOM 을 지우고 참조를 남기면 CI 에서 빨간불.
- **장기(권고)**: 내장 HTML 문자열을 서빙 전 1회 검증하는 부팅 가드(assert) 또는
  빌더가 id/참조 정합성을 자체 점검하도록 구조화. 동적 생성 id(템플릿 리터럴)는
  명시적 화이트리스트로 분리.

## 이 버그가 놓친 이유
- HTML/JS 가 파이썬 문자열에 내장되어 정적 분석·타입체커·린터가 DOM-참조 정합성을
  잡지 못함. DOM 삭제 변경에 대응하는 참조 삭제를 강제하는 테스트가 없었음.
- 가드(`if(x)`)가 일부에만 적용되어 일관성이 없었고, 비가드 1곳이라도 init 경로에
  걸리면 전체 부트스트랩이 무너지는 단일 실패점이 존재했음.
