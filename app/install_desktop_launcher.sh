#!/usr/bin/env bash
# Linux GUI 런처 설치(선택) — 'BOGO 시작.desktop.template' 의 절대경로를 치환해
# ~/.local/share/applications 에 .desktop 을 설치한다. 파일관리자/앱메뉴에서 'BOGO 시작'을
# 클릭으로 실행할 수 있게 한다(터미널 없이도 시작 1번 UX 유지). macOS 에선 .command 를 쓰므로
# 이 스크립트는 Linux 전용 — 다른 OS 에서는 안내만 하고 종료(회귀 없음, 멱등).
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
chmod +x "$START_SH" 2>/dev/null || true

dest_dir="${HOME}/.local/share/applications"
mkdir -p "$dest_dir"
dest="$dest_dir/bogo-start.desktop"
# 절대경로 치환(공백/한글 안전: Exec 의 따옴표는 템플릿에 이미 있음). '#' 구분자로 sed.
sed -e "s#__START_SH__#${START_SH}#g" "$TPL" > "$dest"
chmod +x "$dest" 2>/dev/null || true
# 일부 데스크톱 환경은 신뢰 플래그를 요구 → 가능하면 설정(없어도 메뉴 실행은 동작).
command -v gio >/dev/null 2>&1 && gio set "$dest" "metadata::trusted" true >/dev/null 2>&1 || true
# 앱 메뉴 캐시 갱신(있으면).
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$dest_dir" >/dev/null 2>&1 || true

say "설치 완료: $dest"
say "앱 메뉴/파일관리자에서 'BOGO 시작' 을 클릭해 실행할 수 있습니다(터미널 없이 시작 1번)."
