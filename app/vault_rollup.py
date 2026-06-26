"""
라이프사이클 롤업 — 기간(일/주)별 팀·역할 보고/피드백을 묶어 다이제스트 노트 생성.

목적: 시간이 갈수록 노트가 무한 누적되면 사람·에이전트가 '그 주에 무슨 일이 있었나'를 빠르게
조망하기 어렵다. 롤업은 기간·(team,role) 단위로 그 기간의 report/feedback 을 모아 한 장의
digest 노트(type=digest, visibility=org)로 압축하고, 원본 노트로 [[백링크]]를 건다.

요약은 100% 로컬 추출요약(extractive)이다 — LLM·네트워크·API 키 사용 0:
  - 문장을 분리하고, (1)단어 빈도 점수 (2)앞쪽 위치 보너스 로 핵심 문장 상위 N개를 뽑는다.
  - 새 문장을 '생성'하지 않으므로 환각이 없고 결정적이다(같은 입력 -> 같은 요약).

멱등성: digest 의 id 는 기간+team+role 로 결정적(digest_<period>_<team>_<role>_<start>). 같은 기간을
재실행하면 같은 id 의 기존 digest 를 찾아 갱신(덮어쓰기)한다 -> 중복 생성 0.

CLI:
  python vault_rollup.py daily  [--date YYYY-MM-DD]   # 그 날(UTC) 롤업
  python vault_rollup.py weekly [--date YYYY-MM-DD]   # 그 날이 속한 주(월~일) 롤업
"""
import argparse
import datetime
import hashlib
import os
import re

import vault_schema as S
import vault_writer as W

# 추출요약이 뽑는 핵심 문장 수 상한(digest 가 너무 길어지지 않게).
_SUMMARY_SENTENCES = int(os.environ.get("VAULT_ROLLUP_SENTENCES", "5") or "5")
# 한국어 불용어(요약 점수에서 가중 제외) — 빈도 지배 토큰 억제.
_STOPWORDS = {
    "그리고", "그러나", "하지만", "또한", "그래서", "이것", "저것", "그것",
    "the", "and", "for", "with", "this", "that", "from", "are", "was",
}
_SENT_SPLIT_RE = re.compile(r"[.!?。\n]+")
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")


def _sentences(text):
    """본문을 문장 리스트로 분리(빈 문장 제거, 공백 정리)."""
    out = []
    for raw in _SENT_SPLIT_RE.split(text or ""):
        s = raw.strip().strip("*#-> ").strip()
        if len(s) >= 2:
            out.append(s)
    return out


def extractive_summary(text, max_sentences=None):
    """순수 로컬 추출요약: 단어빈도 + 위치 보너스로 핵심 문장 상위 N개를 원순서로 반환.
    LLM·네트워크 0. 결정적(같은 입력 -> 같은 출력). 입력이 짧으면 그대로 돌려준다."""
    k = max_sentences or _SUMMARY_SENTENCES
    sents = _sentences(text)
    if len(sents) <= k:
        return sents
    # 단어 빈도(불용어·1글자 제외).
    freq = {}
    for s in sents:
        for tok in _TOKEN_RE.findall(s.lower()):
            if len(tok) < 2 or tok in _STOPWORDS:
                continue
            freq[tok] = freq.get(tok, 0) + 1
    if not freq:
        return sents[:k]
    maxf = max(freq.values())
    scored = []
    n = len(sents)
    for i, s in enumerate(sents):
        toks = [t for t in _TOKEN_RE.findall(s.lower()) if len(t) >= 2 and t not in _STOPWORDS]
        if not toks:
            score = 0.0
        else:
            score = sum(freq.get(t, 0) / maxf for t in toks) / (len(toks) ** 0.5)
        # 앞쪽 문장 위치 보너스(보고는 결론이 앞에 오는 경향).
        score += 0.15 * (1.0 - i / n)
        scored.append((i, score))
    top_idx = sorted(sorted(scored, key=lambda x: -x[1])[:k], key=lambda x: x[0])
    return [sents[i] for i, _ in top_idx]


def _period_bounds(period, date_iso=None):
    """기간 경계(시작/끝 ISO 날짜, 라벨)를 계산. daily=그 날, weekly=그 주(월~일, UTC)."""
    if date_iso:
        try:
            base = datetime.date.fromisoformat(date_iso[:10])
        except ValueError:
            base = datetime.datetime.now(datetime.timezone.utc).date()
    else:
        base = datetime.datetime.now(datetime.timezone.utc).date()
    if period == "weekly":
        start = base - datetime.timedelta(days=base.weekday())  # 월요일
        end = start + datetime.timedelta(days=6)
        label = f"{start.isoformat()}~{end.isoformat()}"
        tag = f"W{start.isocalendar()[1]:02d}"
    else:
        start = end = base
        label = base.isoformat()
        tag = base.isoformat()
    return start, end, label, tag


def _collect_notes():
    """Vault 의 report/feedback 노트를 (frontmatter, body, abs_path) 로 수집(digest 자신 제외)."""
    out = []
    if not os.path.isdir(S.VAULT_ROOT):
        return out
    for dirpath, _d, files in os.walk(S.VAULT_ROOT):
        if os.path.basename(dirpath) in (S.DIR_TEMPLATES, S.DIR_INDEX):
            continue
        for fn in files:
            if not fn.endswith(".md") or fn.startswith(".tmp_"):
                continue
            abs_p = os.path.join(dirpath, fn)
            try:
                with open(abs_p, encoding="utf-8") as f:
                    raw = f.read()
            except OSError:
                continue
            fm, body = S.parse_note(raw)
            if fm.get("type") in ("report", "feedback"):
                out.append((fm, body, abs_p))
    return out


def _in_period(date_str, start, end):
    """노트 date(ISO)가 [start, end] 안에 드는지. 파싱 실패 시 False."""
    try:
        d = datetime.date.fromisoformat((date_str or "")[:10])
    except ValueError:
        return False
    return start <= d <= end


def _digest_id(period, team, role, start):
    """기간+team+role 로 결정적 digest id(멱등 키). 같은 기간 재실행 시 같은 id.
    한글 team/role 이 영숫자 슬러그에서 뭉개져 서로 다른 그룹이 같은 id 로 충돌하는 것을 막기
    위해, (team|role) 원문 해시 접미사를 붙여 그룹 유일성을 보장한다(멱등성 보존의 핵심)."""
    sig = hashlib.sha256(f"{team}|{role}".encode("utf-8")).hexdigest()[:8]
    rs = S.id_slug(role or "none", max_len=16)
    return f"digest_{period}_{start.isoformat()}_{rs}_{sig}"


def _find_existing_digest(digest_id):
    """같은 id 의 기존 digest 노트 절대경로를 찾는다(멱등 갱신용). 없으면 None."""
    if not os.path.isdir(S.VAULT_ROOT):
        return None
    for dirpath, _d, files in os.walk(S.VAULT_ROOT):
        for fn in files:
            if fn == f"{digest_id}.md":
                return os.path.join(dirpath, fn)
    return None


def rollup(period="daily", date_iso=None, dry_run=False):
    """기간별 (team,role) 다이제스트를 생성/갱신(멱등). 통계 dict 반환."""
    W.ensure_vault()
    start, end, label, tag = _period_bounds(period, date_iso)
    groups = {}  # (team, role) -> [(fm, body, abs_path)]
    for fm, body, abs_p in _collect_notes():
        if not _in_period(fm.get("date"), start, end):
            continue
        groups.setdefault((fm.get("team", ""), fm.get("role", "")), []).append((fm, body, abs_p))
    stats = {"groups": 0, "created": 0, "updated": 0, "notes": 0}
    for (team, role), items in sorted(groups.items()):
        stats["groups"] += 1
        stats["notes"] += len(items)
        digest_id = _digest_id(period, team, role, start)
        # 원본 노트 백링크(id 기준 [[wikilink]]) — digest 자신과 중복 제거.
        links, seen = [], set()
        corpus = []
        for fm, body, _p in items:
            nid = (fm.get("id") or "").strip()
            if nid and nid != digest_id and nid not in seen:
                links.append(f"[[{nid}]]")
                seen.add(nid)
            corpus.append((fm.get("title") or "") + " " + (body or ""))
        key_sents = extractive_summary("\n".join(corpus))
        head = (f"{period} 다이제스트 — {team or '미표기팀'}/{role or '미표기역할'} "
                f"({label}) · 원본 {len(items)}건")
        body_lines = [f"기간: {label}", f"대상: team={team or '-'} role={role or '-'}",
                      f"원본 노트 수: {len(items)}", "", "## 핵심 요약(로컬 추출)"]
        body_lines += [f"- {s}" for s in key_sents] or ["- (요약할 문장 없음)"]
        body = "\n".join(body_lines)
        existing = _find_existing_digest(digest_id)
        fm = S.default_frontmatter("digest", role, team, links=links,
                                   tags=["digest", period, tag], note_id=digest_id,
                                   visibility="org")
        errors = S.validate_frontmatter(fm)
        if errors:
            continue
        content = S.dump_frontmatter(fm) + "\n**" + head + "**\n\n" + body + "\n"
        if dry_run:
            stats["updated" if existing else "created"] += 1
            continue
        if existing:
            W._atomic_write(existing, content)
            stats["updated"] += 1
        else:
            # digest 는 90_System 아래 _digests 폴더에 둔다(시간축 보고와 분리).
            path = S.vault_path(S.DIR_SYSTEM, "_digests", f"{digest_id}.md")
            W._atomic_write(path, content)
            stats["created"] += 1
    return stats


def _main(argv=None):
    p = argparse.ArgumentParser(description="Vault 라이프사이클 롤업(일/주 다이제스트)")
    p.add_argument("period", choices=["daily", "weekly"])
    p.add_argument("--date", default=None, help="기준일 YYYY-MM-DD(기본: 오늘 UTC)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    stats = rollup(args.period, date_iso=args.date, dry_run=args.dry_run)
    print(("[dry-run] " if args.dry_run else "") + f"[{args.period}] {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
