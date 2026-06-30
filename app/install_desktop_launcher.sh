#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  Linux GUI 런처 설치 (권장 진입점) — 자기치유형
# ════════════════════════════════════════════════════════════════════════
#  WHAT  'BOGO 시작.desktop.template' 의 __START_SH__ 를 실제 절대경로로 치환해
#    ~/.local/share/applications 에 .desktop 을 설치한다. 앱 메뉴/파일관리자에서
#    'BOGO 시작' 아이콘 클릭으로 풀 코어(bogo_oneclick.sh start)를 띄울 수 있게 한다.
#
#  근본 원인 자기치유(비개발자가 더블클릭 한 번으로 못 띄우는 흔한 원인 제거):
#    (a) git clone/복사 시 .sh 실행권한 비트 소실 → 관련 .sh 전부 chmod +x 자동 부여
#    (b) .desktop 신뢰 미표시 → gio set metadata::trusted true(가능 시) + chmod +x
#    (c) 앱 메뉴 캐시 미갱신 → update-desktop-database 갱신
#    (d) 로그 가시성 → 템플릿 Terminal=true 유지(설치/기동 로그가 사용자에게 보임)
#
#  macOS 에선 .command 를 쓰므로 이 스크립트는 Linux 전용 — 다른 OS 는 안내만 하고 종료
#    (회귀 없음, 멱등). 재실행해도 안전(같은 결과로 덮어쓴다).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"   # 프로젝트 루트(= app 의 부모)
START_SH="$ROOT/BOGO 시작.sh"
TPL="$ROOT/BOGO 시작.desktop.template"

say() { printf '\033[0;36m[desktop]\033[0m %s\n' "$*"; }
err() { printf '\033[0;31m[desktop:오류]\033[0m %s\n' "$*" >&2; }

if [ "$(uname -s)" != "Linux" ]; then
  say "Linux 가 아님($(uname -s)) → macOS 는 'BOGO 시작.command' 더블클릭을 쓰세요. 설치 생략."
  exit 0
fi
[ -f "$TPL" ]      || { err "템플릿 없음: $TPL"; exit 1; }
[ -f "$START_SH" ] || { err "런처 없음: $START_SH"; exit 1; }

# ── (a) 실행권한 자기치유 — clone/복사 시 소실된 +x 비트 복구 ─────────────
# 더블클릭 진입점 + 그들이 호출하는 핵심 .sh 전부에 +x 를 보장한다.
heal_chmod() {
  local f
  for f in \
    "$START_SH" \
    "$ROOT/BOGO 정지.sh" \
    "$HERE/start_linux.sh" \
    "$HERE/bogo_oneclick.sh" \
    "$HERE/bogo_ctl.sh" \
    "$HERE/bootstrap.sh" \
    "$HERE/infra_up.sh" \
    "$HERE/run_role.sh" \
    "$HERE/migration/bogo_restore.sh" \
    "$HERE/service/install_service.sh"
  do
    [ -f "$f" ] && chmod +x "$f" 2>/dev/null || true
  done
}
heal_chmod
say "실행권한(+x) 자기치유 완료 — 핵심 .sh 에 실행 비트 부여(clone 시 소실 대비)."

# ── (b) .desktop 설치 + 절대경로 치환 ────────────────────────────────────
dest_dir="${HOME}/.local/share/applications"
mkdir -p "$dest_dir"
dest="$dest_dir/bogo-start.desktop"
# 절대경로 치환(공백/한글 안전: Exec 의 따옴표는 템플릿에 이미 있음). '#' 구분자로 sed.
# START_SH 에 '#' 가 들어갈 일은 없으나, 만약을 대비해 '|' 폴백 대신 '#' 유지(경로에 보통 #없음).
sed -e "s#__START_SH__#${START_SH}#g" "$TPL" > "$dest"
chmod +x "$dest" 2>/dev/null || true

# ── (c) 신뢰 플래그(가능한 환경에서) ─────────────────────────────────────
# GNOME(Nautilus) 계열은 metadata::trusted 가 있어야 더블클릭이 '편집' 아닌 '실행'이 된다.
if command -v gio >/dev/null 2>&1; then
  gio set "$dest" "metadata::trusted" true >/dev/null 2>&1 \
    && say "신뢰 플래그 설정(gio metadata::trusted)." \
    || say "신뢰 플래그 설정 시도(일부 환경은 무시) — 메뉴 실행은 그래도 동작."
fi

# ── (d) 앱 메뉴 캐시 갱신 ────────────────────────────────────────────────
command -v update-desktop-database >/dev/null 2>&1 \
  && update-desktop-database "$dest_dir" >/dev/null 2>&1 \
  && say "앱 메뉴 캐시 갱신(update-desktop-database)." || true

say "설치 완료: $dest"
say "앱 메뉴/파일관리자에서 'BOGO 시작' 아이콘을 클릭해 실행할 수 있습니다(풀 코어, 터미널 로그 표시)."
say "안 보이면 로그아웃/재로그인 1회로 메뉴 캐시가 갱신됩니다."
