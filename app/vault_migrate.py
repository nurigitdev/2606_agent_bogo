"""
기존 메모리(memory_*.json) -> Vault 노트 1회 마이그레이션(멱등).

대상:
  - memory_<role>.json        -> 역할 개인 진행 메모 -> report 노트(role 별).
  - memory_learn_<role>.json  -> 역할 학습 노트(교정/노하우) -> feedback 노트(role 별).
  - memory_ch_<slug>.json     -> 방 공유 메모 -> report 노트(team='', role='room').

멱등성 보장(핵심):
  - 각 원본 줄에 대해 결정적 dedup 키(role|type|sha256(line)[:16])를 만들고, 이미 같은 키의
    노트가 Vault 에 존재하면(=migrated-key 표식) 건너뛴다. 그래서 여러 번 돌려도 노트가
    중복 생성되지 않는다.
  - 원본 memory_*.json 은 삭제하지 않는다(보존). Vault 가 새 진실원천이 되되, 롤백 안전을 위해
    기존 평면 JSON 도 남긴다(런타임이 병행 갱신 — 회귀 0).

비파괴: 이 스크립트는 읽기(원본)+쓰기(Vault)만 한다. 원본 변경·삭제 없음.
"""
import glob
import hashlib
import json
import os
import re

import vault_schema as S
import vault_writer as W

HERE = os.path.dirname(os.path.abspath(__file__))
# migrated 노트 식별 태그(멱등 키 표식). 이 태그 + 키로 중복을 가린다.
MIGRATED_TAG = "migrated"
_DEDUP_RE = re.compile(r"^migrated-key:\s*([0-9a-f]+)\s*$", re.MULTILINE)


def _dedup_key(role, ntype, line):
    """원본 한 줄의 결정적 멱등 키. 같은 (role,type,내용)은 항상 같은 키 -> 중복 차단."""
    digest = hashlib.sha256(f"{role}|{ntype}|{line}".encode("utf-8")).hexdigest()[:16]
    return digest


def _existing_keys():
    """이미 마이그레이션된 노트들의 dedup 키 집합을 Vault 에서 수집(멱등 판정 기준)."""
    keys = set()
    if not os.path.isdir(S.VAULT_ROOT):
        return keys
    for dirpath, _d, files in os.walk(S.VAULT_ROOT):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            try:
                with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                    head = f.read(2048)
            except OSError:
                continue
            for m in _DEDUP_RE.finditer(head):
                keys.add(m.group(1))
    return keys


def _lines_from(json_path, field):
    """memory_*.json 의 field(summary|notes) 를 줄 단위 리스트로(빈 줄 제거)."""
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    text = data.get(field, "") or ""
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def _write_migrated(role, team, line, key, writer):
    """dedup 키를 본문에 박은 마이그레이션 노트 1개를 기록. writer 는 append_* 함수."""
    body = f"migrated-key: {key}\n\n{line}"
    return writer(role=role, team=team, summary=line[:120], body=body,
                  tags=[MIGRATED_TAG])


def migrate(dry_run=False):
    """모든 memory_*.json 을 Vault 노트로 1회 변환(멱등). 통계 dict 반환.
    dry_run=True 면 쓰지 않고 변환 대상 수만 센다(검증용).

    마이그레이션은 과거 데이터 '일괄 적재'이므로 노트마다 RAG 검색(자동 백링크)을 돌리는 것은
    비효율·무의미하다. 그래서 이 구간에서만 writer 의 자동 백링크를 끈다(원자 쓰기·멱등은 유지).
    """
    W.ensure_vault()
    prev_autolink = W._AUTOLINK_ENABLED
    W._AUTOLINK_ENABLED = False  # 일괄 적재 중 백링크 비활성(성능·무의미)
    try:
        return _migrate_inner(dry_run)
    finally:
        W._AUTOLINK_ENABLED = prev_autolink


def _migrate_inner(dry_run=False):
    """migrate() 의 실제 변환 루프(자동 백링크 토글 컨텍스트 안에서 호출)."""
    existing = _existing_keys()
    stats = {"role_memo": 0, "learn": 0, "room": 0, "skipped": 0}

    # 1) 역할 개인 메모 memory_<role>.json (learn/ch 제외).
    for path in sorted(glob.glob(os.path.join(HERE, "memory_*.json"))):
        base = os.path.basename(path)
        if base.startswith("memory_learn_") or base.startswith("memory_ch_"):
            continue
        role = base[len("memory_"):-len(".json")]
        for line in _lines_from(path, "summary"):
            key = _dedup_key(role, "report", line)
            if key in existing:
                stats["skipped"] += 1
                continue
            if not dry_run:
                _write_migrated(role, "", line, key, W.append_report)
                existing.add(key)
            stats["role_memo"] += 1

    # 2) 학습 노트 memory_learn_<role>.json -> feedback.
    for path in sorted(glob.glob(os.path.join(HERE, "memory_learn_*.json"))):
        role = os.path.basename(path)[len("memory_learn_"):-len(".json")]
        for line in _lines_from(path, "notes"):
            key = _dedup_key(role, "feedback", line)
            if key in existing:
                stats["skipped"] += 1
                continue
            if not dry_run:
                _write_migrated(role, "", line, key, W.append_feedback)
                existing.add(key)
            stats["learn"] += 1

    # 3) 방 공유 메모 memory_ch_<slug>.json -> report(role='room').
    for path in sorted(glob.glob(os.path.join(HERE, "memory_ch_*.json"))):
        slug = os.path.basename(path)[len("memory_ch_"):-len(".json")]
        for line in _lines_from(path, "summary"):
            key = _dedup_key(f"room:{slug}", "report", line)
            if key in existing:
                stats["skipped"] += 1
                continue
            if not dry_run:
                _write_migrated("room", "", line, key, W.append_report)
                existing.add(key)
            stats["room"] += 1

    return stats


def backfill_visibility(dry_run=False):
    """기존(레거시) 노트의 frontmatter 에 visibility 를 채운다(스키마 진화 멱등 마이그레이션).

    규칙(vault_schema.default_visibility 와 동일): decision/policy=org, report/feedback=team,
    profile=private. 이미 유효한 visibility 가 있으면 건드리지 않는다(멱등). CEO 의 결정·정책
    노트가 org 로 승격돼 전 에이전트가 전사 규범으로 회수할 수 있게 된다.
    반환 통계 dict. dry_run=True 면 쓰지 않고 대상 수만 센다."""
    if not os.path.isdir(S.VAULT_ROOT):
        return {"updated": 0, "already": 0, "skipped": 0}
    stats = {"updated": 0, "already": 0, "skipped": 0}
    for dirpath, _d, files in os.walk(S.VAULT_ROOT):
        if os.path.basename(dirpath) == S.DIR_TEMPLATES:
            continue
        for fn in files:
            if not fn.endswith(".md") or fn.startswith(".tmp_"):
                continue
            abs_p = os.path.join(dirpath, fn)
            try:
                with open(abs_p, encoding="utf-8") as f:
                    raw = f.read()
            except OSError:
                stats["skipped"] += 1
                continue
            fm, body = S.parse_note(raw)
            if not fm:
                stats["skipped"] += 1
                continue
            ntype = fm.get("type", "")
            cur = fm.get("visibility")
            want = S.normalize_visibility(cur, ntype)
            if cur in S.VALID_VISIBILITY:
                stats["already"] += 1
                continue
            fm["visibility"] = want
            stats["updated"] += 1
            if not dry_run:
                content = S.dump_frontmatter(fm) + "\n" + (body or "").rstrip() + "\n"
                W._atomic_write(abs_p, content)
    return stats


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    if "--backfill-visibility" in sys.argv:
        print(("[dry-run] " if dry else "") + "backfill_visibility="
              + str(backfill_visibility(dry_run=dry)))
    else:
        print(("[dry-run] " if dry else "") + str(migrate(dry_run=dry)))
