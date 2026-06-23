#!/usr/bin/env python3
"""
에이전트 정의 린트 — 머지/배포 전 정합성 게이트.

검사:
  - agents/*.md frontmatter 필수필드(name, username, config, primary, channels) 누락
  - username 중복
  - 미정의 채널 참조(channels.json에 없는 채널)
  - 페르소나 본문(prompt) 누락
  - {config}_config.json 존재 여부(봇 토큰 파일)
  - teams.json 채널 참조 정합성
  - 공유 규칙 파일(common_rules.md) 존재

종료코드 0=정상, 1=오류. CI/pre-commit/배포 스크립트에서 호출해 깨진 정의 머지를 차단한다.
사용: python lint_agents.py
"""
import os
import sys

import agent_schema as A

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    errors = []
    warnings = []

    channels = A.load_channels()
    valid = set(channels)
    roles = A.load_roles()
    teams = A.load_teams()

    if not roles:
        errors.append("agents/ 아래 에이전트(.md) 정의가 하나도 없음")

    # 공유 규칙 존재 확인
    if not A.load_common_rules():
        warnings.append("agents/_shared/common_rules.md 가 비었거나 없음 — 공통 규칙 상속 주입이 동작하지 않음")

    # frontmatter / 채널 / 중복 검증
    errors += A.validate_roles(roles, valid)
    errors += A.validate_teams(teams, valid)

    # config(봇 토큰) 파일 존재 검증
    for role, meta in sorted(roles.items()):
        cfg = meta.get("config")
        if cfg and not os.path.exists(os.path.join(HERE, f"{cfg}_config.json")):
            errors.append(f"[{role}] config 파일 없음: {cfg}_config.json")

    # teams.json 에이전트 이름이 실제 에이전트(name)에 존재하는지 교차 확인
    role_names = {m.get("name") for m in roles.values()}
    for t in teams.get("teams", []):
        if t.get("agent") and t["agent"] not in role_names:
            warnings.append(f"[teams:{t.get('id')}] agent '{t['agent']}'에 대응하는 agents/*.md 가 없음")
    orch = teams.get("orchestrator", {})
    if orch.get("role") and orch["role"] not in role_names:
        warnings.append(f"[orchestrator] role '{orch['role']}'에 대응하는 agents/*.md 가 없음")

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")

    if errors:
        print(f"\n린트 실패: 오류 {len(errors)}건, 경고 {len(warnings)}건")
        return 1
    print(f"린트 통과: 에이전트 {len(roles)}개, 팀 {len(teams.get('teams', []))}개, 채널 {len(valid)}개. 경고 {len(warnings)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
