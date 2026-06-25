"""
Vault RAG·writer·schema·migrate·org 단위/통합 테스트.

검증 범위(SUCCESS_CRITERIA 대응):
  (a) writer atomic·동시쓰기 무충돌 — 멀티스레드 + 멀티프로세스 다발 쓰기 후 파일수/무결성.
  (b) RAG 인덱싱→검색 라운드트립 — 색인한 노트가 메타필터로 정확히 회수된다.
  (c) frontmatter 스키마 검증 — 필수키/타입/리스트 강제, traversal 차단.
  (d) FTS5-only fallback 경로 — 임베딩 비활성에서도 검색이 동작.
  (e) 마이그레이션 멱등성 — 두 번 돌려도 노트가 중복 생성되지 않는다.
  (+) 조직 동기화·증분 인덱싱·삭제 동기화·RRF 융합.

테스트는 임시 Vault 디렉토리에서만 동작한다(실 app/vault 오염 0).
실행: python3 -m pytest test_vault.py -q
"""
import json
import multiprocessing as mp
import os
import threading

import pytest

import vault_schema as S
import vault_writer as W
import vault_rag as R
import vault_org as O
import vault_migrate as M


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """임시 Vault 루트 + RAG DB 를 모든 vault_* 모듈에 주입(격리)."""
    root = str(tmp_path / "vault")
    monkeypatch.setattr(S, "VAULT_ROOT", root)
    monkeypatch.setattr(R, "DB_PATH", os.path.join(root, ".rag_index.db"))
    monkeypatch.setattr(M, "HERE", str(tmp_path))  # migrate 가 memory_*.json 을 여기서 찾게
    W.ensure_vault()
    return tmp_path


# ── (c) frontmatter 스키마 ────────────────────────────────────────────────────
def test_frontmatter_roundtrip():
    fm = S.default_frontmatter("report", "dev", "개발", tags=["a", "b"],
                               links=["[[최지현]]"])
    fm["id"] = "n1"
    assert S.validate_frontmatter(fm) == []
    parsed, body = S.parse_note(S.dump_frontmatter(fm) + "\n본문1\n본문2\n")
    assert parsed["type"] == "report"
    assert parsed["tags"] == ["a", "b"]
    assert parsed["links"] == ["[[최지현]]"]
    assert body.strip() == "본문1\n본문2"


def test_frontmatter_rejects_bad_type():
    fm = S.default_frontmatter("INVALID", "dev", "개발")
    fm["id"] = "x"
    errs = S.validate_frontmatter(fm)
    assert any("type 위반" in e for e in errs)


def test_frontmatter_rejects_nonlist_tags():
    fm = S.default_frontmatter("report", "dev", "개발")
    fm["id"] = "x"
    fm["tags"] = "notalist"
    errs = S.validate_frontmatter(fm)
    assert any("tags 는 리스트" in e for e in errs)


def test_path_traversal_blocked(vault):
    with pytest.raises(ValueError):
        S.vault_path("..", "..", "etc", "passwd")
    with pytest.raises(ValueError):
        S.vault_path("../../escape.md")


def test_safe_slug_strips_unsafe():
    assert "/" not in S.safe_slug("a/b:c*?")
    assert S.safe_slug("..") == "untitled"
    assert S.safe_slug("개발") == "개발"  # 한글 보존


# ── (a) writer atomic + 동시 쓰기 ────────────────────────────────────────────
def test_writer_creates_valid_note(vault):
    p = W.append_report("dev", "개발", "요약", body="본문", tags=["t"])
    assert p and os.path.exists(p)
    fm, body = S.parse_note(open(p, encoding="utf-8").read())
    assert S.validate_frontmatter(fm) == []
    assert fm["type"] == "report" and fm["role"] == "dev"
    assert "요약" in body


def test_writer_rejects_invalid_frontmatter(vault):
    fm = S.default_frontmatter("report", "dev", "개발")
    fm["type"] = "BAD"
    path, errs = W.write_note(fm, "x")
    assert path is None and errs  # 검증 실패 → 쓰지 않음


def test_concurrent_threads_no_conflict(vault):
    n_threads, m_each = 10, 30
    errors = []

    def worker(tid):
        for i in range(m_each):
            try:
                p = W.append_report(f"r{tid % 3}", "개발", f"t{tid} n{i}",
                                    body="본문 " * 8, tags=["c"])
                if not p or not os.path.exists(p):
                    errors.append((tid, i))
            except Exception as e:  # noqa: BLE001
                errors.append((tid, i, str(e)))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    md, tmp = _walk_md(S.VAULT_ROOT)
    assert errors == []
    assert len(md) == n_threads * m_each
    assert tmp == []  # 임시파일 잔존 0(원자성)
    ids = {S.parse_note(open(p, encoding="utf-8").read())[0].get("id") for p in md}
    assert len(ids) == n_threads * m_each  # id(파일명 uuid) 충돌 0


def test_concurrent_processes_no_conflict(vault):
    procs, n_each = 6, 25
    vroot = S.VAULT_ROOT
    ctx = mp.get_context("spawn")
    with ctx.Pool(procs) as pool:
        from _mp_worker import mp_write
        res = pool.map(mp_write, [(t, vroot, n_each) for t in range(procs)])
    md, tmp = _walk_md(vroot)
    assert sum(res) == procs * n_each
    assert len(md) == procs * n_each
    assert tmp == []


def _walk_md(root):
    md, tmp = [], []
    for dp, _d, fs in os.walk(root):
        for f in fs:
            if f.endswith(".md"):
                md.append(os.path.join(dp, f))
            if f.startswith(".tmp_"):
                tmp.append(f)
    return md, tmp


# ── (b)(d) RAG 인덱싱→검색 라운드트립 + FTS5-only fallback ─────────────────────
def test_rag_index_search_roundtrip(vault):
    W.append_report("dev", "개발", "중국 LLM 파일럿 비용절감 검토",
                    body="보안 리스크와 정확도 우려를 함께 평가", tags=["llm"])
    W.append_report("hr", "인사총무", "연차 정산 규정 변경",
                    body="휴가 이월 한도 조정", tags=["hr"])
    stats = R.index()
    assert stats["indexed"] == 2
    hits = R.search("LLM 파일럿 비용", top_k=5)
    assert len(hits) >= 1
    assert any("LLM" in h["title"] for h in hits)


def test_rag_meta_filter_isolation(vault):
    W.append_report("dev", "개발", "개발 배포 자동화", body="CI 파이프라인")
    W.append_report("hr", "인사총무", "개발 인력 채용 계획", body="채용 일정")
    R.index()
    dev_hits = R.search("개발", role="dev", top_k=5)
    assert dev_hits and all(h["role"] == "dev" for h in dev_hits)
    hr_hits = R.search("개발", role="hr", top_k=5)
    assert all(h["role"] == "hr" for h in hr_hits)


def test_rag_fts_only_fallback(vault, monkeypatch):
    # 임베딩 모델을 강제로 '불가' 상태로 만들어 FTS5 단독 경로를 탄다.
    monkeypatch.setattr(R, "_EMBED_MODEL", False)
    assert R.embed_available() is False
    W.append_report("dev", "개발", "캐시 전략 보고", body="Redis L2 캐시 도입")
    R.index()
    hits = R.search("캐시 전략", top_k=5)
    assert len(hits) >= 1  # 의미검색 없이도 어휘검색만으로 회수


def test_rag_incremental_and_purge(vault):
    p = W.append_report("dev", "개발", "증분 테스트", body="x")
    s1 = R.index()
    assert s1["indexed"] == 1
    s2 = R.index()
    assert s2["indexed"] == 0 and s2["skipped"] == 1  # 변경 없으면 재색인 안 함
    os.remove(p)
    s3 = R.index()
    assert s3["purged"] == 1  # 삭제 파일 인덱스 정리
    assert R.search("증분 테스트", top_k=5) == []


# ── 조직 동기화 ───────────────────────────────────────────────────────────────
def test_org_sync_creates_dirs_and_moc(vault):
    tj = "/Users/haris/Desktop/헤르메스 에이전트 간 통신/app/teams.json"
    res = O.sync_org(tj)
    assert res["team_dirs"] >= 2
    assert os.path.exists(res["moc"])
    moc = open(res["moc"], encoding="utf-8").read()
    assert "[[개발]]" in moc and "[[인사총무]]" in moc  # wikilink 관계
    # 멱등: 다시 돌려도 깨지지 않음
    res2 = O.sync_org(tj)
    assert os.path.exists(res2["moc"])


# ── (e) 마이그레이션 멱등성 ───────────────────────────────────────────────────
def test_migration_idempotent(vault, tmp_path):
    # 가짜 memory_*.json 3종을 tmp_path 에 깔고 migrate 가 거기서 읽게 한다.
    (tmp_path / "memory_dev.json").write_text(
        json.dumps({"summary": "보고A\n보고B"}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "memory_learn_dev.json").write_text(
        json.dumps({"notes": "교훈1\n교훈2"}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "memory_ch_abc.json").write_text(
        json.dumps({"summary": "방메모1"}, ensure_ascii=False), encoding="utf-8")

    s1 = M.migrate()
    total1 = s1["role_memo"] + s1["learn"] + s1["room"]
    assert total1 == 5  # 2 + 2 + 1
    md1, _ = _walk_md(S.VAULT_ROOT)
    n_migrated_1 = len(md1)

    # 2회차: 전부 skipped, 새 노트 0(멱등).
    s2 = M.migrate()
    assert s2["role_memo"] + s2["learn"] + s2["room"] == 0
    assert s2["skipped"] == 5
    md2, _ = _walk_md(S.VAULT_ROOT)
    assert len(md2) == n_migrated_1  # 노트 수 불변


def test_migration_preserves_originals(vault, tmp_path):
    src = tmp_path / "memory_dev.json"
    src.write_text(json.dumps({"summary": "원본보존확인"}, ensure_ascii=False),
                   encoding="utf-8")
    M.migrate()
    assert src.exists()  # 원본 비파괴
    assert json.loads(src.read_text(encoding="utf-8"))["summary"] == "원본보존확인"


def test_rag_team_filter_includes_blank_team(vault):
    """team 필터는 '해당 팀 + team 미표기(빈)' 를 함께 회수한다(마이그레이션 노트 누락 방지).
    role 은 1차 격리 키로 그대로 엄격 적용되고, team 은 보강 필터로 빈 team 을 배제하지 않는다."""
    W.append_report("dev", "", "팀 미표기 과거 보고", body="마이그레이션 모사")
    W.append_report("dev", "개발", "팀 표기 신규 보고", body="현행")
    R.index()
    hits = R.search("보고", role="dev", team="개발", top_k=5)
    teams = {h["team"] for h in hits}
    # 빈 team 노트와 '개발' 노트가 모두 회수돼야 한다.
    assert "" in teams and "개발" in teams, teams
    # 단, role 격리는 유지 — 전부 dev 여야 한다.
    assert all(h["role"] == "dev" for h in hits)
