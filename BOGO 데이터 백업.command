#!/bin/zsh
# ════════════════════════════════════════════════════════════════════════
#  BOGO 데이터 백업 (선택·수동 즉시 백업) — 보통은 누를 필요 없음
# ════════════════════════════════════════════════════════════════════════
#  ⚠ 평소엔 누를 필요 없습니다. 봇이 운영되는 동안 launchd/systemd 가 주기적으로(기본
#    6시간) 데이터를 폴더 안(app/migration/)에 자동 백업하고, 정상 종료 시에도 1회 백업합니다.
#    따라서 정상 사용자가 누르는 버튼은 새 PC 의 'BOGO 시작.command' 단 하나뿐입니다.
#
#  이 파일은 '지금 당장' 최신 상태를 강제로 떠내고 싶을 때만 쓰는 보조 수단입니다.
#  WHAT  더블클릭하면 app/migration/bogo_backup.sh 가 즉시 1회 실행되어 Mattermost
#    대화·계정·채널·보고(Postgres + MM 볼륨)를 app/migration/bogo_backup_latest.tar.gz 로
#    만든다(원본 무손상 read-only, 오래된 백업은 자동 정리).
#  Korean path safe: 자기 위치를 동적 해석하므로 한글/공백 경로에서 동작.
# ════════════════════════════════════════════════════════════════════════
set -u

SELF_DIR="${0:A:h}"
BACKUP="$SELF_DIR/app/migration/bogo_backup.sh"

C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
say()  { printf "%s[BOGO]%s %s\n" "$C_INFO" "$C_RST" "$*"; }
ok()   { printf "%s[성공]%s %s\n"   "$C_OK"   "$C_RST" "$*"; }
fail() { printf "%s[오류]%s %s\n"   "$C_ERR"  "$C_RST" "$*"; }

pause_exit() {
  print -r -- ""
  print -r -- "──────────────────────────────────────────────"
  print -r -- "이 창은 Enter 또는 아무 키나 누르면 닫힙니다."
  read -k1 -s 2>/dev/null || true
  exit "${1:-0}"
}

print -r -- ""
say "BOGO 데이터 백업 시작"
say "위치: $SELF_DIR"
print -r -- ""

if [[ ! -f "$BACKUP" ]]; then
  fail "백업 스크립트를 찾지 못했습니다: $BACKUP"
  fail "이 .command 파일은 'app' 폴더가 있는 프로젝트 루트에 두어야 합니다."
  pause_exit 1
fi
chmod +x "$BACKUP" 2>/dev/null || true

"$BACKUP"
rc=$?

print -r -- ""
if [[ $rc -eq 0 ]]; then
  ok "백업 완료. 'agent-bogo' 폴더 전체를 새 PC 로 옮긴 뒤 'BOGO 시작'을 더블클릭하세요."
  say "백업본 위치:  app/migration/bogo_backup_latest.tar.gz"
else
  fail "백업 중 문제가 발생했습니다. 위 로그를 확인하세요."
  say "흔한 원인: Docker/Colima 미기동 → 터미널에서 'colima start' 후 다시 시도."
fi

pause_exit $rc
