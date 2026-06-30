#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  BOGO 리눅스 시작 진입점 — 비개발자용 단일 실행 파일 (얇은 위임 셸)
# ════════════════════════════════════════════════════════════════════════
#  WHY  과거 이 파일은 'bogo_ctl.sh setup'(bootstrap→infra_up→service install)만
#    호출하는 '두번째 갈래'였다. 반면 프로젝트 루트의 'BOGO 시작.sh' 는
#    'bogo_oneclick.sh start'(풀 코어: venv→reindex→infra→데이터 자동복원→봇/대시보드
#    상시가동 등록→대시보드 헬스체크)를 호출한다. 두 진입점이 서로 다른 동작을 하면,
#    사용자가 어느 걸 클릭하느냐에 따라 결과가 갈리고(특히 step_restore 데이터 자동복원이
#    빠짐) 무인 운영의 일관성이 깨진다.
#
#  WHAT  이 파일은 더 이상 독자 코어를 갖지 않는다. 리눅스 단일 코어인 루트
#    'BOGO 시작.sh' 로 그대로 위임한다(단일 진실원본 = bogo_oneclick.sh start).
#    이로써 어느 진입점을 눌러도 완전히 동일한 풀 코어가 실행된다(동작 분기 제거).
#
#  사용:  파일 관리자에서 더블클릭(.desktop 경유) 또는 터미널에서  ./start_linux.sh
#  Korean/space 경로 안전: 자기 위치를 동적 해석. set -u.
set -u

# ── 자기 위치 = app 디렉토리 → 부모가 프로젝트 루트 ───────────────────────
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
START_SH="$ROOT/BOGO 시작.sh"   # 리눅스 단일 코어 진입점(풀 코어로 위임)

if [ ! -f "$START_SH" ]; then
  echo "[start_linux] 오류: 루트 진입점을 찾을 수 없습니다: $START_SH" >&2
  echo "[start_linux] 이 파일이 BOGO 프로젝트의 app 디렉토리 안에 있는지 확인하세요." >&2
  exit 1
fi
chmod +x "$START_SH" 2>/dev/null || true

echo "[start_linux] 단일 코어(풀)로 위임합니다 → \"$START_SH\""
# 루트 진입점으로 위임. 그 안에서 bogo_oneclick.sh start(풀 코어)가 돈다.
exec bash "$START_SH"
