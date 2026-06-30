#!/usr/bin/env python3
"""NIC/사설 IP 자동 감지 → 네트워크 모드 판정 → .env 네트워크 키 멱등 주입.

WHY
  멀티홈 중앙 서버(서버 1대 + NIC 3장으로 같은 건물 3개 분리망 A/B/C 직결)를
  쓰려면 운영자가 .env 에 BOGO_MULTIHOME / MM_BIND_HOST / MM_SITE_URL /
  MM_ALLOW_CORS_FROM / BOGO_DASHBOARD_HOST 를 손으로 채워야 했다. 사람이 사설
  IP 를 외워 적는 것은 오타·누락의 상시 원천이다. 이 모듈은 "랜선만 꽂으면"
  NIC 와 사설 IP 를 스스로 읽어 모드를 판정하고 .env 의 네트워크 키만 멱등
  갱신한다(비밀·수동값은 절대 파괴하지 않는다).

설계 원칙(불변)
  - 파싱 로직은 전부 순수 함수로 분리한다(셸에 묻으면 테스트 불가). 셸(launcher)
    은 이 모듈의 CLI 진입점만 호출한다.
  - 사설 IP 판정은 RFC1918(10/8, 172.16/12, 192.168/16) 을 정확히 적용한다.
  - lo(루프백)·docker0·br-*·veth*·virbr*·tailscale*(100.64/10 CGNAT) 등 가상·
    오버레이 인터페이스는 제외한다(물리 NIC 만 망 판정 근거).
  - 공인(글로벌 라우팅) IP 를 가진 NIC 가 하나라도 있으면 멀티홈 0.0.0.0 자동
    활성을 '중단'하고 경고한다 — 인터넷 노출 방지(공인 NIC 부재가 0.0.0.0 안전
    전제이기 때문).
  - 멱등: .env 의 비밀키·수동 설정값은 보존하고, 네트워크 관련 키만 in-place
    upsert 한다. 같은 NIC 구성에서 두 번 실행해도 결과는 동일하다.

CLI
  python net_autodetect.py detect            # 감지 결과를 JSON 으로 출력(진단용)
  python net_autodetect.py summary           # 사람이 읽는 1회 요약(런처가 출력)
  python net_autodetect.py apply --env PATH  # .env 에 네트워크 키 멱등 주입
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys

MM_PORT = "8065"
DASH_PORT = "8642"

# 망 판정에서 제외할 가상·오버레이 인터페이스 접두/이름. 물리 NIC 만 남긴다.
# (docker0/br-/veth = 컨테이너 브리지, virbr = libvirt, tailscale/wg = 오버레이 VPN,
#  utun/llw/awdl = macOS 가상, zt = ZeroTier 등.)
_VIRTUAL_PREFIXES = (
    "lo", "docker", "br-", "veth", "virbr", "tailscale", "tun", "tap",
    "wg", "zt", "utun", "llw", "awdl", "bridge", "vmnet", "vboxnet",
    "cni", "flannel", "kube", "cali", "nerdctl", "anpi", "ap", "stf", "gif",
)


def _is_virtual_iface(name: str) -> bool:
    """가상·오버레이 인터페이스인가(망 판정에서 제외 대상)."""
    n = name.lower()
    return any(n == p or n.startswith(p) for p in _VIRTUAL_PREFIXES)


def is_rfc1918(ip: str) -> bool:
    """RFC1918 사설 IPv4 인가(10/8, 172.16/12, 192.168/16) — 정확 판정.

    ipaddress.is_private 는 CGNAT(100.64/10)·링크로컬·루프백까지 사설로 보므로
    그대로 쓰면 Tailscale 100.x 가 사설 NIC 로 오분류된다. 여기서는 RFC1918
    3개 대역만 명시적으로 인정한다.
    """
    try:
        addr = ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError:
        return False
    return (
        addr in ipaddress.IPv4Network("10.0.0.0/8")
        or addr in ipaddress.IPv4Network("172.16.0.0/12")
        or addr in ipaddress.IPv4Network("192.168.0.0/16")
    )


def is_public_ipv4(ip: str) -> bool:
    """글로벌 라우팅(공인) IPv4 인가 — 인터넷 노출 가드의 판정 기준.

    사설(RFC1918)·루프백·링크로컬(169.254/16)·CGNAT(100.64/10)·멀티캐스트·예약
    대역을 모두 공인이 아닌 것으로 본다. 그 외 글로벌 라우팅 주소만 공인으로
    판정한다(보수적: 의심스러우면 공인으로 보지 않아 거짓 차단을 피한다).
    """
    try:
        addr = ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError:
        return False
    if is_rfc1918(ip):
        return False
    # 100.64/10 = CGNAT(Tailscale 등). 공인 아님.
    if addr in ipaddress.IPv4Network("100.64.0.0/10"):
        return False
    # is_global 이 True 이고 위 사설/CGNAT 가 아니면 진짜 공인.
    return bool(addr.is_global)


# ── ip 명령 출력 파서 ────────────────────────────────────────────────────
# `ip -o -4 addr show` 한 줄 예:
#   2: eth0    inet 10.0.0.10/24 brd 10.0.0.255 scope global eth0\       valid_lft ...
_IP_O_RE = re.compile(
    r"^\d+:\s+(?P<iface>[^\s:]+)\s+inet\s+(?P<ip>\d+\.\d+\.\d+\.\d+)/\d+"
)


def parse_ip_o_output(text: str) -> list[tuple[str, str]]:
    """`ip -o -4 addr show` 출력 → [(iface, ipv4), ...] (순수 함수, 테스트 대상).

    @ 별칭(eth0:1 같은 sub-label)은 기본 iface 로 정규화하지 않고 원문 iface
    토큰을 그대로 쓴다(가상 판정은 _is_virtual_iface 가 접두로 거른다).
    """
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = _IP_O_RE.match(line.strip())
        if m:
            out.append((m.group("iface"), m.group("ip")))
    return out


# `ifconfig` 폴백 파서(BSD/macOS). 블록 단위로 iface 와 inet 을 묶는다.
def parse_ifconfig_output(text: str) -> list[tuple[str, str]]:
    """`ifconfig` 출력 → [(iface, ipv4), ...] (ip 명령 부재 시 폴백 파서)."""
    out: list[tuple[str, str]] = []
    cur: str | None = None
    for raw in text.splitlines():
        if raw and not raw[0].isspace():
            # 새 인터페이스 블록 시작: "eth0: flags=..." 또는 "eth0  Link ..."
            cur = raw.split(":")[0].split()[0].strip()
            continue
        if cur is None:
            continue
        m = re.search(r"inet\s+(?:addr:)?(\d+\.\d+\.\d+\.\d+)", raw)
        if m:
            out.append((cur, m.group(1)))
    return out


def _read_iface_ips() -> list[tuple[str, str]]:
    """현재 호스트의 (iface, ipv4) 전체 목록을 수집(ip → ifconfig 폴백).

    네트워크 호출이 아니라 로컬 커널 인터페이스 조회다(테스트는 파서를 직접
    검증하고, 이 수집기는 명령 가용성에 따른 폴백만 담당한다).
    """
    if shutil.which("ip"):
        try:
            r = subprocess.run(
                ["ip", "-o", "-4", "addr", "show"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return parse_ip_o_output(r.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    if shutil.which("ifconfig"):
        try:
            r = subprocess.run(
                ["ifconfig"], capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return parse_ifconfig_output(r.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    return []


def classify_interfaces(
    pairs: list[tuple[str, str]],
) -> dict[str, list[tuple[str, str]]]:
    """(iface, ip) 목록을 private/public/virtual 로 분류한다(순수 함수).

    반환:
      {
        "private": [(iface, ip), ...],  # 물리 NIC + RFC1918, iface·ip 정렬·중복제거
        "public":  [(iface, ip), ...],  # 물리 NIC + 공인 IP(인터넷 노출 가드 근거)
        "virtual": [(iface, ip), ...],  # 제외된 가상/오버레이(진단용)
      }
    """
    private: list[tuple[str, str]] = []
    public: list[tuple[str, str]] = []
    virtual: list[tuple[str, str]] = []
    for iface, ip in pairs:
        if _is_virtual_iface(iface):
            virtual.append((iface, ip))
            continue
        if is_rfc1918(ip):
            private.append((iface, ip))
        elif is_public_ipv4(ip):
            public.append((iface, ip))
        # 그 외(루프백·링크로컬·CGNAT 등 물리 NIC 에 붙은 비사설/비공인)는 무시.
    # iface, ip 기준 정렬 + 중복 제거(동일 IP 가 별칭으로 두 번 잡히는 경우 대비).
    dedup = lambda xs: sorted(set(xs), key=lambda t: (t[0], t[1]))  # noqa: E731
    return {
        "private": dedup(private),
        "public": dedup(public),
        "virtual": dedup(virtual),
    }


def decide_mode(classified: dict[str, list[tuple[str, str]]]) -> dict:
    """분류 결과 → 네트워크 모드 판정(순수 함수 — 자동화의 두뇌).

    판정 규칙:
      - 공인 IP NIC 가 있으면 → mode="guard". 멀티홈 0.0.0.0 자동활성을 중단하고
        경고만 낸다(인터넷 노출 방지). 네트워크 키는 변경하지 않는다(loopback 유지).
      - 사설 NIC ≥ 2 → mode="multihome". 대표(첫) NIC 를 SiteURL 로, 나머지를
        CORS 오리진으로. MM_BIND_HOST=0.0.0.0, BOGO_DASHBOARD_HOST=0.0.0.0,
        BOGO_MULTIHOME=1.
      - 사설 NIC == 1 → mode="lan". 그 사설 IP 로 단일망 바인딩(0.0.0.0 아님).
      - 사설 NIC == 0 → mode="loopback". 루프백 유지(외부 노출 0).

    반환 dict 의 "env" 키 = .env 에 upsert 할 네트워크 키→값 매핑.
    """
    private = classified["private"]
    public = classified["public"]

    # (1) 공인 NIC 가드 — 멀티홈 0.0.0.0 자동활성을 막는다(인터넷 노출 방지).
    if public:
        return {
            "mode": "guard",
            "reason": "공인 IP NIC 감지 → 멀티홈 0.0.0.0 자동활성 중단(인터넷 노출 방지)",
            "private": private,
            "public": public,
            # 가드 시 네트워크 키를 건드리지 않는다(기존 .env 그대로 = 루프백 유지).
            "env": {},
        }

    # (2) 멀티홈 — 사설 NIC 2개 이상.
    if len(private) >= 2:
        rep_iface, rep_ip = private[0]
        rest = private[1:]
        cors = " ".join(f"http://{ip}:{MM_PORT}" for _, ip in rest)
        return {
            "mode": "multihome",
            "reason": f"사설 NIC {len(private)}개 감지 → 멀티홈(다중 NIC 직결) 자동 구성",
            "private": private,
            "public": public,
            "rep": (rep_iface, rep_ip),
            "env": {
                "BOGO_MULTIHOME": "1",
                "MM_BIND_HOST": "0.0.0.0",
                "MM_SITE_URL": f"http://{rep_ip}:{MM_PORT}",
                "MM_ALLOW_CORS_FROM": cors,
                "BOGO_DASHBOARD_HOST": "0.0.0.0",
            },
        }

    # (3) 단일망 LAN — 사설 NIC 1개.
    if len(private) == 1:
        _, ip = private[0]
        return {
            "mode": "lan",
            "reason": "사설 NIC 1개 감지 → 단일망 LAN 모드(해당 사설 IP 바인딩)",
            "private": private,
            "public": public,
            "rep": private[0],
            "env": {
                # 단일망은 멀티홈 0.0.0.0 가 아니라 특정 사설 IP 로만 바인딩한다.
                "BOGO_MULTIHOME": "0",
                "MM_BIND_HOST": ip,
                "MM_SITE_URL": f"http://{ip}:{MM_PORT}",
                "MM_ALLOW_CORS_FROM": "",
                "BOGO_DASHBOARD_HOST": ip,
            },
        }

    # (4) 루프백 — 사설 NIC 0개(외부 노출 0, 단일 PC 데모).
    return {
        "mode": "loopback",
        "reason": "사설 NIC 0개 → 루프백 유지(외부 노출 0)",
        "private": private,
        "public": public,
        "env": {
            "BOGO_MULTIHOME": "0",
            "MM_BIND_HOST": "127.0.0.1",
            "MM_SITE_URL": f"http://127.0.0.1:{MM_PORT}",
            "MM_ALLOW_CORS_FROM": "",
            "BOGO_DASHBOARD_HOST": "127.0.0.1",
        },
    }


def detect() -> dict:
    """현재 호스트를 감지해 모드 판정 dict 를 반환한다(수집 → 분류 → 판정)."""
    return decide_mode(classify_interfaces(_read_iface_ips()))


# ── .env 멱등 upsert ─────────────────────────────────────────────────────
# 네트워크 자동화가 소유하는 키. apply 는 '오직 이 키들만' 갱신하고, 그 외(비밀·
# 수동 설정)는 한 글자도 건드리지 않는다.
NETWORK_KEYS = (
    "BOGO_MULTIHOME",
    "MM_BIND_HOST",
    "MM_SITE_URL",
    "MM_ALLOW_CORS_FROM",
    "BOGO_DASHBOARD_HOST",
)

# 자동 주입 블록의 경계 마커(멱등 재실행 시 이 블록만 교체).
_BLOCK_BEGIN = "# >>> bogo net-autodetect (자동 생성 — 직접 편집 금지) >>>"
_BLOCK_END = "# <<< bogo net-autodetect <<<"


def _quote_value(v: str) -> str:
    """공백이 있는 값(CORS 다중 오리진)은 따옴표로 감싼다(셸 set -a 안전)."""
    if v == "" or re.fullmatch(r"[^\s\"'#]+", v):
        return v
    return '"' + v.replace('"', '\\"') + '"'


def upsert_env_text(text: str, env: dict[str, str]) -> str:
    """기존 .env 텍스트에 네트워크 키를 멱등 upsert 한 새 텍스트를 반환(순수 함수).

    멱등·비파괴 규칙:
      - 비-네트워크 키(비밀·수동값)는 라인·순서·주석까지 그대로 보존한다.
      - 활성(주석 아님) 네트워크 키가 본문에 이미 있으면 그 자리에서 값만 교체
        (in-place upsert) → 사용자가 수동으로 켜둔 위치를 흩뜨리지 않는다.
      - 본문에 없는 네트워크 키는 파일 끝의 자동 생성 블록에 모아 추가한다.
      - 자동 생성 블록은 마커로 식별해, 재실행 시 통째로 교체한다(중복 누적 방지).
      - env 가 비면(가드 모드) 본문 활성 키는 건드리지 않고 블록만 제거한다.
    """
    lines = text.splitlines()
    remaining = dict(env)  # 아직 본문에서 교체하지 못한 키들.

    # 1) 기존 자동 생성 블록 제거(나중에 새로 붙인다).
    cleaned: list[str] = []
    in_block = False
    for ln in lines:
        if ln.strip() == _BLOCK_BEGIN:
            in_block = True
            continue
        if ln.strip() == _BLOCK_END:
            in_block = False
            continue
        if not in_block:
            cleaned.append(ln)

    # 2) 본문의 활성 네트워크 키를 in-place 로 값 교체.
    key_re = {k: re.compile(rf"^(\s*){re.escape(k)}\s*=") for k in env}
    out: list[str] = []
    for ln in cleaned:
        replaced = False
        for k in list(remaining.keys()):
            if key_re[k].match(ln):
                indent = key_re[k].match(ln).group(1)
                out.append(f"{indent}{k}={_quote_value(remaining[k])}")
                del remaining[k]
                replaced = True
                break
        if not replaced:
            out.append(ln)

    # 3) 본문에 없던 키는 파일 끝 자동 생성 블록에 모아 추가.
    if remaining:
        # 끝의 빈 줄 정리 후 블록 추가(파일이 비어 있어도 안전).
        while out and out[-1].strip() == "":
            out.pop()
        out.append("")
        out.append(_BLOCK_BEGIN)
        for k in NETWORK_KEYS:
            if k in remaining:
                out.append(f"{k}={_quote_value(remaining[k])}")
        out.append(_BLOCK_END)

    result = "\n".join(out)
    if not result.endswith("\n"):
        result += "\n"
    return result


def apply_env(env_path: str, example_path: str | None = None) -> dict:
    """감지 → .env 멱등 갱신. .env 없으면 example 에서 생성 후 키 주입.

    반환: detect() 의 판정 dict(런처 요약 출력에 재사용).
    """
    decision = detect()
    env = decision["env"]

    # .env 없으면 example 에서 시드(비밀 템플릿 보존). example 도 없으면 빈 파일.
    if not os.path.exists(env_path):
        if example_path and os.path.exists(example_path):
            shutil.copyfile(example_path, env_path)
        else:
            open(env_path, "a", encoding="utf-8").close()

    with open(env_path, encoding="utf-8") as f:
        before = f.read()

    new_text = upsert_env_text(before, env)
    if new_text != before:
        # 원자적 교체(쓰다 죽어도 기존 .env 보존).
        tmp = env_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, env_path)
    return decision


# ── 사람이 읽는 1회 요약(런처가 출력) ────────────────────────────────────
def format_summary(decision: dict) -> str:
    """감지 결과를 운영자가 한눈에 읽도록 한국어로 1회 요약."""
    mode = decision["mode"]
    lines: list[str] = []
    private = decision.get("private", [])
    public = decision.get("public", [])

    if mode == "multihome":
        rep_iface, rep_ip = decision["rep"]
        lines.append("네트워크 자동 감지: 멀티홈(다중 NIC 직결) 모드")
        lines.append(f"  · 사설 NIC {len(private)}장 감지 → 3개 사내망 동시 수신(0.0.0.0)")
        lines.append(f"  · 대표 망(SiteURL): {rep_iface} = http://{rep_ip}:{MM_PORT}")
        for iface, ip in private[1:]:
            lines.append(f"  · 추가 망(CORS 허용): {iface} = http://{ip}:{MM_PORT}")
        lines.append("  · 각 망 직원은 자기 망 NIC IP:8065 로 브라우저 접속(클라이언트 설치 0)")
    elif mode == "lan":
        iface, ip = decision["rep"]
        lines.append("네트워크 자동 감지: 단일망 LAN 모드")
        lines.append(f"  · 사설 NIC 1장 → {iface} = http://{ip}:{MM_PORT} 바인딩(0.0.0.0 아님)")
        lines.append(f"  · 같은 망 직원은 http://{ip}:{MM_PORT} 로 접속")
    elif mode == "guard":
        lines.append("네트워크 자동 감지: 공인 IP NIC 감지 → 멀티홈 자동활성 중단(인터넷 노출 방지)")
        for iface, ip in public:
            lines.append(f"  · 공인 NIC: {iface} = {ip} (0.0.0.0 자동 바인딩 거부)")
        if private:
            lines.append("  · 사설 NIC 도 있으나 안전을 위해 자동 구성하지 않음 →")
            lines.append("    공인 NIC 분리 또는 .env 수동 구성(DEPLOY_NETWORK.md) 후 재실행")
        lines.append("  · 현재 바인딩은 기존 .env 값 유지(기본 루프백 = 외부 노출 0)")
    else:  # loopback
        lines.append("네트워크 자동 감지: 단일 PC(루프백) 모드 — 외부 노출 0")
        lines.append("  · 사설 NIC 미감지 → 127.0.0.1 유지(랜선 연결 후 재실행 시 자동 전환)")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "detect"
    if cmd == "detect":
        print(json.dumps(detect(), ensure_ascii=False, indent=2))
        return 0
    if cmd == "summary":
        print(format_summary(detect()))
        return 0
    if cmd == "apply":
        # --env PATH (필수), --example PATH (선택)
        env_path = None
        example_path = None
        i = 2
        while i < len(argv):
            if argv[i] == "--env" and i + 1 < len(argv):
                env_path = argv[i + 1]
                i += 2
            elif argv[i] == "--example" and i + 1 < len(argv):
                example_path = argv[i + 1]
                i += 2
            else:
                i += 1
        if not env_path:
            print("[net_autodetect] --env PATH 가 필요합니다.", file=sys.stderr)
            return 2
        decision = apply_env(env_path, example_path)
        print(format_summary(decision))
        return 0
    print(f"[net_autodetect] 알 수 없는 명령: {cmd} (detect|summary|apply)",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
