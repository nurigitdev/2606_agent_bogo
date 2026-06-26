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
import vault_rollup as RU
import vault_eval as EV


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
    tj = os.path.join(os.path.dirname(os.path.abspath(__file__)), "teams.json")
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


# ── (1) 공유 지식 계층(visibility) ────────────────────────────────────────────
def test_visibility_defaults_and_schema():
    """type 별 기본 visibility 와 검증 규칙."""
    assert S.default_visibility("report") == "team"
    assert S.default_visibility("decision") == "org"
    assert S.default_visibility("policy") == "org"
    assert S.default_visibility("profile") == "private"
    assert S.default_visibility("digest") == "org"
    assert S.normalize_visibility("", "report") == "team"
    assert S.normalize_visibility("ORG", "report") == "org"   # 대소문자 무관
    assert S.normalize_visibility("bogus", "policy") == "org"  # 잘못된 값 -> type 기본
    fm = S.default_frontmatter("report", "dev", "개발")
    fm["id"] = "n1"
    assert fm["visibility"] == "team"
    assert S.validate_frontmatter(fm) == []
    fm["visibility"] = "BAD"
    assert any("visibility 위반" in e for e in S.validate_frontmatter(fm))


def test_visibility_private_isolated_across_teams(vault):
    """타팀 private 노트는 회수되지 않고, org 는 전사 회수된다(가시성 필터 경계)."""
    # hr 팀 사람의 private profile(자기 격리).
    W.write_profile("hr", "인사총무", "인사 담당자 개인 메모 비밀")
    # dev 팀 사람의 팀 보고(team).
    W.append_report("dev", "개발", "개발 배포 자동화 보고")
    # CEO 의 전사 정책(org).
    W.append_policy("ceo", "", "전사 보안 정책 공개")
    R.index()
    # dev viewer 관점: 타팀(hr) private profile 은 안 보이고, 전사 org 정책은 보인다.
    dev_hits = R.search("메모 보고 정책", role="dev", team="개발", top_k=10)
    paths_types = {(h["type"], h["role"]) for h in dev_hits}
    assert ("profile", "hr") not in paths_types  # 타팀 private 격리
    assert any(h["type"] == "policy" and h["visibility"] == "org" for h in dev_hits)  # org 공개
    assert any(h["role"] == "dev" for h in dev_hits)  # 자기 기억


def test_visibility_team_shared_within_team(vault):
    """같은 팀의 team 가시성 노트는 다른 role 이어도 회수된다(팀 공유)."""
    W.append_report("dev", "개발", "팀원A 의 팀 보고")
    # 같은 '개발' 팀, 다른 role.
    W.append_report("dev2", "개발", "팀원B 의 팀 보고")
    R.index()
    # dev2 viewer, 같은 개발 팀 -> 팀원A(role=dev) 의 team 보고도 회수.
    hits = R.search("팀 보고", role="dev2", team="개발", top_k=10)
    roles = {h["role"] for h in hits}
    assert "dev" in roles and "dev2" in roles, roles


def test_visibility_dashboard_bypass(vault):
    """apply_visibility=False(대시보드 관리자) 는 가시성 무시하고 메타필터만 적용."""
    W.write_profile("hr", "인사총무", "hr 개인 비밀 메모")
    R.index()
    # viewer 가시성 모드면 dev 가 hr private 을 못 본다.
    assert R.search("비밀 메모", role="dev", top_k=5) == []
    # 관리자 모드(가시성 우회) + role 필터 없이는 전체 회수.
    admin = R.search("비밀 메모", apply_visibility=False, top_k=5)
    assert any(h["role"] == "hr" for h in admin)


# ── (2) 의미검색 numpy=순수파이썬 일치 ────────────────────────────────────────
def test_semantic_numpy_matches_purepython(vault, monkeypatch):
    """numpy 벡터화 결과와 순수파이썬 폴백 결과가 동일 순위를 내는지(정확도 불변)."""
    if not R.embed_available():
        pytest.skip("임베딩 모델 없음 — 의미검색 경로 검증 불가")
    for i in range(8):
        W.append_report("dev", "개발", f"보고 주제 {i} 캐시 전략 색인 최적화 {i}",
                        body=f"본문 {i} " * 5)
    R.reindex()
    con = R.connect()
    try:
        q = "캐시 전략 색인 최적화"
        np_rank = R._semantic_rank(con, q, "dev", "개발", None, 8)
        # numpy 를 강제로 끈 순수파이썬 경로.
        monkeypatch.setattr(R, "_np", None)
        R._EMBED_CACHE.clear()  # 캐시 형식(ndarray vs list) 재구축
        py_rank = R._semantic_rank(con, q, "dev", "개발", None, 8)
        assert [r for r, _ in np_rank] == [r for r, _ in py_rank]
    finally:
        con.close()
        R._EMBED_CACHE.clear()


def test_embedding_cache_invalidated_on_reindex(vault):
    """인덱스 변경 시 임베딩 캐시 버전이 올라 무효화되는지."""
    if not R.embed_available():
        pytest.skip("임베딩 모델 없음")
    W.append_report("dev", "개발", "첫 보고 캐시 테스트")
    R.index()
    con = R.connect()
    try:
        v1 = R._index_version(con)
        R._load_embed_matrix(con)
        W.append_report("dev", "개발", "둘째 보고 캐시 무효화")
        R.index(con)
        v2 = R._index_version(con)
        assert v2 > v1  # 색인 변경 -> 버전 상승
        rowids, _ = R._load_embed_matrix(con)
        assert len(rowids) == 2  # 새 노트 반영
    finally:
        con.close()


# ── (3) 라이프사이클 롤업 ─────────────────────────────────────────────────────
def test_extractive_summary_is_local_and_deterministic():
    text = ("배포 자동화 파이프라인을 구축했다. 보안 점검을 통과했다. "
            "테스트 커버리지가 올랐다. 캐시 전략을 도입했다. 응답속도가 개선됐다. "
            "모니터링 대시보드를 추가했다.")
    s1 = RU.extractive_summary(text, max_sentences=3)
    s2 = RU.extractive_summary(text, max_sentences=3)
    assert s1 == s2 and len(s1) == 3  # 결정적 + 상한 준수
    assert all(isinstance(x, str) and x for x in s1)


def test_rollup_idempotent(vault):
    """같은 기간 롤업 재실행 시 digest 가 갱신될 뿐 중복 생성되지 않는다(멱등)."""
    today = S.utc_now_iso()[:10]
    W.append_report("dev", "개발", "롤업 대상 보고1 캐시")
    W.append_report("dev", "개발", "롤업 대상 보고2 색인")
    s1 = RU.rollup("daily", date_iso=today)
    assert s1["created"] >= 1 and s1["groups"] >= 1
    digests1 = [p for p in _walk_md(S.VAULT_ROOT)[0] if "_digests" in p]
    s2 = RU.rollup("daily", date_iso=today)
    assert s2["updated"] >= 1 and s2["created"] == 0  # 재실행=갱신, 신규 0
    digests2 = [p for p in _walk_md(S.VAULT_ROOT)[0] if "_digests" in p]
    assert len(digests1) == len(digests2)  # digest 수 불변
    # digest frontmatter 가 org 가시성 + 백링크 보유.
    fm, _ = S.parse_note(open(digests2[0], encoding="utf-8").read())
    assert fm["type"] == "digest" and fm["visibility"] == "org"
    assert any("[[" in ln for ln in fm.get("links", []))


# ── (4) 자동 백링크 ───────────────────────────────────────────────────────────
def test_auto_backlink_links_related_no_self(vault):
    """쓰기 시 관련 과거 노트를 links 에 자동 채우되 자기참조는 없다."""
    p1 = W.append_report("dev", "개발", "캐시 전략 Redis L2 도입 보고")
    R.index()  # 첫 노트를 인덱싱해야 둘째가 백링크로 찾는다
    p2 = W.append_report("dev", "개발", "캐시 전략 Redis 후속 최적화 보고")
    fm2, _ = S.parse_note(open(p2, encoding="utf-8").read())
    id1 = os.path.splitext(os.path.basename(p1))[0]
    id2 = fm2["id"]
    links = fm2.get("links", [])
    assert f"[[{id1}]]" in links  # 관련 과거 노트 자동 연결
    assert f"[[{id2}]]" not in links  # 자기참조 없음


# ── (5) eval recall@k ────────────────────────────────────────────────────────
def test_eval_recall_baseline(vault):
    """self-retrieval recall@k 측정이 동작하고 합리적 기준선을 낸다."""
    for i in range(5):
        W.append_report("dev", "개발", f"고유한 보고 제목 알파{i} 베타{i} 감마{i}")
    R.reindex()
    res = EV.evaluate(ks=(1, 3, 5))
    assert res["n"] >= 5
    # 노트 자신의 제목으로 검색하므로 recall@5 는 높아야 한다(검색 정상성 기준선).
    assert res["recall"]["@5"] >= 0.8, res


# ── 마이그레이션 visibility 백필 ──────────────────────────────────────────────
def test_backfill_visibility_idempotent(vault):
    """레거시(visibility 누락) 노트에 type 기본 visibility 를 채우고 멱등."""
    # visibility 없는 레거시 노트를 수동으로 깐다(default_frontmatter 우회).
    fm = {"id": "legacy1", "type": "decision", "role": "ceo", "team": "",
          "date": S.utc_now_iso(), "tags": [], "links": []}
    path = S.vault_path(S.DIR_CEO, "legacy1.md")
    W._atomic_write(path, S.dump_frontmatter(fm) + "\n레거시 결정 노트\n")
    s1 = M.backfill_visibility()
    assert s1["updated"] == 1
    fm2, _ = S.parse_note(open(path, encoding="utf-8").read())
    assert fm2["visibility"] == "org"  # decision -> org
    s2 = M.backfill_visibility()
    assert s2["updated"] == 0 and s2["already"] >= 1  # 멱등
