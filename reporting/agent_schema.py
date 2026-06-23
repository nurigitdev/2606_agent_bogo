"""
에이전트 정의 로딩·검증 공용 모듈.

hermes_runtime.py(런타임 기동)와 lint_agents.py(머지 차단 린트)가 공유한다.
- agents/*.md frontmatter 파싱 + 필수필드/중복/채널참조 검증
- teams.json 로딩 + ROUTING 텍스트 동적 생성 (하드코딩 제거)
- agents/_shared/common_rules.md 상속 주입 헬퍼
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS_DIR = os.path.join(HERE, "agents")
SHARED_DIR = os.path.join(AGENTS_DIR, "_shared")
COMMON_RULES_PATH = os.path.join(SHARED_DIR, "common_rules.md")

# frontmatter 필수 필드 (린트가 누락 시 머지 차단).
REQUIRED_FIELDS = ("name", "username", "config", "primary", "channels")
LIST_FIELDS = ("aliases", "channels")


def parse_md(path):
    """agents/*.md 한 파일을 {필드..., prompt} dict로 파싱."""
    txt = open(path, encoding="utf-8").read()
    parts = txt.split("---", 2)
    fm, body = (parts[1], parts[2]) if len(parts) >= 3 else ("", txt)
    meta = {}
    for line in fm.strip().splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        meta[k] = [x.strip() for x in v.split(",") if x.strip()] if k in LIST_FIELDS else v
    meta["prompt"] = body.strip()
    return meta


def _agent_files():
    return sorted(
        f for f in os.listdir(AGENTS_DIR)
        if f.endswith(".md") and not f.startswith("_")
    )


def load_roles():
    """role(파일명 stem) -> 파싱 dict. _로 시작하는 파일(_shared 등)은 제외."""
    return {f[:-3]: parse_md(os.path.join(AGENTS_DIR, f)) for f in _agent_files()}


def load_common_rules():
    """공유 규칙 본문. 없으면 빈 문자열(런타임이 죽지 않도록 graceful)."""
    try:
        return open(COMMON_RULES_PATH, encoding="utf-8").read().strip()
    except FileNotFoundError:
        return ""


def load_teams():
    return json.load(open(os.path.join(HERE, "teams.json"), encoding="utf-8"))


def load_channels():
    return json.load(open(os.path.join(HERE, "channels.json"), encoding="utf-8"))


def validate_roles(roles, valid_channels):
    """필수필드 누락·username 중복·미정의 채널 참조를 검사. 오류 문자열 리스트 반환(빈=정상)."""
    errors = []
    seen_users = {}
    for role, meta in sorted(roles.items()):
        for field in REQUIRED_FIELDS:
            if not meta.get(field):
                errors.append(f"[{role}] 필수 필드 누락: {field}")
        if not meta.get("prompt"):
            errors.append(f"[{role}] 페르소나 본문(prompt)이 비어 있음")
        user = meta.get("username")
        if user:
            if user in seen_users:
                errors.append(f"[{role}] username '{user}' 중복 (이미 {seen_users[user]})")
            else:
                seen_users[user] = role
        for ch in meta.get("channels", []):
            if ch not in valid_channels:
                errors.append(f"[{role}] 미정의 채널 참조: '{ch}' (channels.json에 없음)")
        primary = meta.get("primary")
        if primary and primary not in valid_channels:
            errors.append(f"[{role}] primary 채널 미정의: '{primary}'")
    return errors


def validate_teams(teams, valid_channels):
    """teams.json의 채널 참조 정합성 검사. 오류 리스트 반환."""
    errors = []
    orch = teams.get("orchestrator", {})
    bc = orch.get("briefing_channel")
    if bc and bc not in valid_channels:
        errors.append(f"[orchestrator] briefing_channel 미정의: '{bc}'")
    ids = set()
    for t in teams.get("teams", []):
        tid = t.get("id", "?")
        if tid in ids:
            errors.append(f"[teams] 팀 id 중복: '{tid}'")
        ids.add(tid)
        for key in ("team_channel", "report_channel"):
            ch = t.get(key)
            if not ch:
                errors.append(f"[teams:{tid}] 필수 필드 누락: {key}")
            elif ch not in valid_channels:
                errors.append(f"[teams:{tid}] {key} 미정의 채널: '{ch}'")
        if not t.get("agent"):
            errors.append(f"[teams:{tid}] 필수 필드 누락: agent")
    return errors


def build_routing(teams):
    """teams.json 데이터로 ROUTING 텍스트를 동적 생성. (이전 하드코딩 ROUTING의 라우팅 흐름 부분을 대체)"""
    orch = teams.get("orchestrator", {})
    tlist = teams.get("teams", [])
    orch_name = orch.get("role", "박민철")
    brief = orch.get("briefing_channel", "")
    principal = orch.get("principal", "CEO")

    lines = ["[채널과 업무 흐름]"]
    team_chs = " / ".join(t["team_channel"] for t in tlist)
    report_chs = " / ".join(t["report_channel"] for t in tlist)
    lines.append(
        f"- {team_chs}: 팀원(사람)이 보고를 올리는 방. 팀 에이전트는 정리해 자기 팀 보고라인에 "
        f"{orch_name}을 멘션해 상신한다. 상위 지시의 1차 수행 결과도 이 방에서 팀원에게 전달."
    )
    lines.append(
        f"- {report_chs}: 팀 에이전트와 {orch_name}이 보고·피드백·지시를 주고받는 방."
    )
    if brief:
        lines.append(f"- {brief}: {orch_name}과 {principal}(사람)만의 방. {orch_name}이 상신하고 {principal} 지시를 받는다.")

    lines.append("")
    lines.append("[역할별 행동]")
    for t in tlist:
        lines.append(
            f"- {t['agent']}({t['label']}): {t['team_channel']} 사람 보고 → '{t['report_channel']}'에 "
            f"{t['escalate_to']} 멘션 상신. {t['escalate_to']} 피드백 → 보완 회신. "
            f"{t['escalate_to']} 지시({principal}발) → 1차 수행 후 {t['team_channel']}에서 팀원에게 전달, closed."
        )
    # orchestrator(상향·하향) 행동을 팀 데이터로부터 동적 구성
    up_map = ", ".join(f"{t['label']} 건은 '{t['report_channel']}' {t['agent']}" for t in tlist)
    lines.append(
        f"- {orch_name}: 각 보고라인의 팀 보고를 받아 → 보완 필요하면 팀 에이전트 멘션해 반려, "
        f"충분하면 importance 판단(routine=간단 정리, decision_needed='{brief}' 상신). "
        f"{brief}의 {principal} 지시 → {up_map} 멘션해 분배. "
        "★팀 상신을 단어만 바꿔 복붙하지 마라(공통규칙 4 가공 원칙 준수)."
    )
    return "\n".join(lines)


def system_prompt(spec, common_rules, routing, memo=""):
    """에이전트 시스템 프롬프트 조립: 페르소나(고유) + 공유규칙(상속) + 라우팅(데이터) + 메모."""
    parts = [spec["prompt"]]
    if common_rules:
        parts.append("\n\n===== 전 에이전트 공통 규칙 (상속) =====\n" + common_rules)
    if memo:
        parts.append("\n\n[진행 중 업무 메모]\n" + memo)
    parts.append("\n\n===== 라우팅 (teams.json 기반) =====\n" + routing)
    parts.append(f"\n[너] 이름:{spec['name']} / 주 담당 방:{spec['primary']}")
    return "".join(parts)
