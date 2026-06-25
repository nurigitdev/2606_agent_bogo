#!/usr/bin/env python3
"""
선언 ↔ 실재 채널 동기화 — 멱등 재발방지 스크립트.

문제: teams.json(learning_rooms/collab_rooms 등)이나 agents/*.md 가 채널을 '정의'했는데
channels.json 값이 placeholder(TODO_)·빈값이거나 Mattermost 에 실제 채널이 없으면,
런타임이 그 방을 구독·송신하지 못한다(직전 '학습방 신설'에서 정의만 추가하고 실생성 누락된 케이스).

이 스크립트는 그 갭을 감지해 자동으로 메운다:
  1) teams.json learning_rooms 의 채널을 대상으로(현재 갭 표면)
  2) channels.json 값이 TODO_/빈값/실재하지 않으면 → Mattermost 에 채널을 멱등 생성(있으면 재사용)
  3) 생성/확보된 channel_id 를 channels.json 에 기록
  4) owner 에이전트 봇 + 운영 봇을 그 채널 멤버로 추가(구독 성립 → 학습 누적 동작)

멱등: 이미 ID 가 채워졌고 실재하면 skip. 같은 슬러그가 이미 있으면 기존 id 를 가져와 채운다.
시크릿(봇 토큰)은 파일에서만 읽고 출력하지 않는다.

사용: python sync_channels.py            # 기준 team = 기존 정상 채널에서 자동 추론
종료코드 0=정상.
"""
import json
import os
import sys

import agent_schema as A
import mm_client

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNELS_PATH = os.path.join(HERE, "channels.json")

# 학습방 슬러그(Mattermost name) — 한글 채널명은 슬러그로 못 쓰므로 owner→영문 슬러그 매핑.
LEARN_SLUG = {"hr": "hr-learn", "dev": "dev-learn", "orchestrator": "orchestrator-learn"}
# owner role → 그 역할 에이전트의 config 파일 stem(봇 계정 매핑). 운영 봇과 별개로 함께 구독시킨다.
OWNER_CONFIG = {"hr": "gyaru", "dev": "genz", "orchestrator": "nk"}


def _load_json(path):
    return json.load(open(path, encoding="utf-8"))


def _bot_token():
    """운영 봇 토큰(nk_config). 시크릿은 반환만 하고 절대 출력하지 않는다."""
    return _load_json(os.path.join(HERE, "nk_config.json"))["bot_token"]


def _bot_id(config_stem):
    """config 파일(<stem>_config.json)의 bot_id. 없으면 None."""
    p = os.path.join(HERE, f"{config_stem}_config.json")
    return _load_json(p).get("bot_id") if os.path.exists(p) else None


def _reference_team_id(mm, channels):
    """기존 정상 채널 중 실재하는 첫 채널의 team_id 를 기준 team 으로 추론한다."""
    for name, cid in channels.items():
        if isinstance(cid, str) and not cid.startswith(A.LEARN_PLACEHOLDER_PREFIX) and cid:
            try:
                return mm.channel(cid)["team_id"]
            except Exception:
                continue
    raise SystemExit("기준 team_id 를 추론할 실재 채널이 channels.json 에 없음")


def _needs_sync(channels, name, mm):
    """channels.json 의 name 값이 placeholder/빈값/실재하지 않으면 True(동기화 필요)."""
    cid = channels.get(name, "")
    if not cid or cid.startswith(A.LEARN_PLACEHOLDER_PREFIX):
        return True
    try:
        mm.channel(cid)
        return False
    except Exception:
        return True


def _member_ids(owner):
    """이 학습방에 넣을 user_id 집합 = 운영 봇 + owner 에이전트 봇(중복 제거)."""
    ids = {_bot_id("nk")}
    oc = OWNER_CONFIG.get(owner)
    if oc:
        ids.add(_bot_id(oc))
    return {i for i in ids if i}


def sync_learning_rooms():
    teams = A.load_teams()
    channels = _load_json(CHANNELS_PATH)
    mm = mm_client.MM(_bot_token())
    team_id = _reference_team_id(mm, channels)

    report = []
    for room in A.load_learning_rooms(teams):
        name, owner = room.get("channel"), room.get("owner")
        if not name or not owner:
            continue
        if not _needs_sync(channels, name, mm):
            report.append(f"skip   {name} (이미 실재·등록됨: {channels[name]})")
            continue
        slug = LEARN_SLUG.get(owner, owner + "-learn")
        ch = mm.ensure_channel(team_id, slug, name, ctype="O")
        channels[name] = ch["id"]
        added = []
        for uid in _member_ids(owner):
            mm.add_member(ch["id"], uid)
            added.append(uid[:8] + "…")
        report.append(f"synced {name} -> {ch['id']} (slug={slug}, members={len(added)})")

    # channels.json 멱등 기록(키 순서·한글 유지).
    with open(CHANNELS_PATH, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False)
    return report, channels


def main():
    report, channels = sync_learning_rooms()
    for line in report:
        print(line)
    todo = [k for k, v in channels.items()
            if isinstance(v, str) and (not v or v.startswith(A.LEARN_PLACEHOLDER_PREFIX))]
    if todo:
        print(f"\n경고: 아직 미해결 placeholder 채널: {todo}")
        return 1
    print(f"\n동기화 완료: 학습방 {sum(1 for line in report if line.startswith('synced'))}건 생성/등록, "
          f"placeholder 잔여 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
