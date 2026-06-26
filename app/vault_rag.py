"""
Vault 로컬 RAG 인덱스 — FTS5 어휘검색 + 로컬 임베딩 의미검색의 RRF 하이브리드.

전부 로컬·오프라인이다(API 키·네트워크 임베딩 절대 없음):
  - 어휘검색: SQLite FTS5(표준 라이브러리 sqlite3). 한국어는 토크나이저가 약하므로
    bigram 보강 컬럼(공백 제거 2-gram)을 함께 색인해 부분일치 회수율을 끌어올린다.
  - 의미검색: sentence-transformers 로컬 모델(all-MiniLM-L6-v2). 최초 1회 로컬 캐시.
    미설치/모델 없음/로드 실패 시 '에러가 아니라' FTS5 단독으로 graceful degrade(경고만).
    임베딩은 numpy 없이도 동작하도록 파이썬 list[float] + 순수파이썬 코사인으로 처리한다.
  - 융합: RRF(Reciprocal Rank Fusion, k=60). 두 랭킹의 순위 역수 합으로 정렬 -> 점수 스케일
    차이에 둔감하고 구현이 단순(스케일 정규화 불필요).
  - 증분 인덱싱: 파일 sha256 content-hash 로 변경분만 재색인. 삭제된 파일의 인덱스 행 정리.

인덱스 위치: vault/.rag_index.db(.gitignore — 파생물). 노트 md 만 git 추적.

CLI:
  python vault_rag.py index            # 증분 인덱싱(변경분만)
  python vault_rag.py reindex          # 전체 재색인(인덱스 비우고 재구축)
  python vault_rag.py search "<쿼리>" [--role R --team T --top-k N]
"""
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys

import vault_schema as S

# numpy 가 있으면 의미검색 코사인을 행렬 연산으로 벡터화한다(대량 노트에서 순수파이썬 대비
# 수십~수백배 빠름). 없으면 순수파이썬 폴백으로 동일 결과를 낸다(정확도 불변).
try:
    import numpy as _np
except Exception:  # noqa: BLE001 — numpy 없으면 순수파이썬 경로
    _np = None

DB_PATH = os.path.join(S.VAULT_ROOT, ".rag_index.db")
# 로컬 임베딩 모델명(최초 1회 로컬 캐시 후 오프라인 동작). 설치 안 돼 있으면 FTS5-only.
EMBED_MODEL_NAME = os.environ.get("VAULT_EMBED_MODEL", "all-MiniLM-L6-v2")
# RRF 상수(표준 권장값). 순위가 낮아도 0 으로 죽지 않게 하는 완충.
RRF_K = 60
# 임베딩 모델 1프로세스 1회 로드 캐시. None=미시도, False=불가, obj=로드됨.
_EMBED_MODEL = None
# ── 임베딩 행렬 인메모리 캐시 ─────────────────────────────────────────────────
# 매 검색마다 embeddings 테이블 전수 파싱(json.loads N건)은 비싸다. 인덱스 변경 카운트를
# 버전으로 삼아 (행렬, rowid 목록)을 캐시하고, 버전이 바뀌면 무효화한다.
# 구조: {db_path: {"version": int, "rowids": [..], "matrix": np.ndarray|list[list]}}
_EMBED_CACHE = {}


def _bigrams(text):
    """한국어 부분일치 회수율 보강용 공백제거 2-gram 토큰열. FTS5 가 한글 어절을 잘
    못 쪼개는 문제를 우회한다(영문은 원형 토큰이 이미 잘 잡혀 보조 역할)."""
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 2:
        return compact
    return " ".join(compact[i:i + 2] for i in range(len(compact) - 1))


def connect(db_path=None):
    """RAG 인덱스 DB 연결 + 스키마 보장. WAL 로 동시 읽기/쓰기 내성을 높인다."""
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    _init_schema(con)
    return con


def _db_key(con):
    """임베딩 인메모리 캐시 키 = 연결된 DB 파일의 realpath(경로별 캐시 분리).
    sqlite3.Connection 은 임의 속성을 못 다므로 PRAGMA database_list 로 파일 경로를 얻는다.
    (테스트의 임시 DB 와 실 DB 캐시가 섞이지 않게 한다)."""
    try:
        for _seq, name, file in con.execute("PRAGMA database_list").fetchall():
            if name == "main" and file:
                return os.path.realpath(file)
    except sqlite3.Error:
        pass
    return id(con)


def _init_schema(con):
    """인덱스 테이블/FTS5 가상테이블 생성(idempotent).
    notes: 파일 메타(경로/해시/role/team/type/date) — 증분·필터·정리의 기준.
    notes_fts: FTS5(body+bigram). rowid 를 notes.rowid 와 1:1 동기화(수동 관리).
    content='' (contentless) 는 행 DELETE 가 불가하므로 쓰지 않는다(삭제 동기화 필요).
    embeddings: rowid -> 벡터(JSON 텍스트). 모델 차원에 무관하게 저장."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS notes(
            rowid      INTEGER PRIMARY KEY AUTOINCREMENT,
            path       TEXT UNIQUE NOT NULL,
            hash       TEXT NOT NULL,
            note_id    TEXT,
            type       TEXT,
            role       TEXT,
            team       TEXT,
            visibility TEXT,
            date       TEXT,
            title      TEXT,
            body       TEXT
        )""")
    con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
            body, bigram
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS embeddings(
            rowid  INTEGER PRIMARY KEY,
            vec    TEXT NOT NULL,
            dim    INTEGER NOT NULL
        )""")
    # 인덱스 변경 버전(임베딩 캐시 무효화 신호). 색인이 바뀔 때마다 +1.
    con.execute("""
        CREATE TABLE IF NOT EXISTS index_meta(
            key   TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )""")
    con.execute("INSERT OR IGNORE INTO index_meta(key, value) VALUES('version', 0)")
    _migrate_visibility_column(con)
    con.commit()


def _migrate_visibility_column(con):
    """기존(레거시) notes 테이블에 visibility 컬럼이 없으면 ALTER 로 추가(스키마 마이그레이션).
    추가 후 NULL/빈 값은 reindex 시 노트 frontmatter 에서 채워진다(여기선 컬럼 존재만 보장)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(notes)").fetchall()}
    if "visibility" not in cols:
        con.execute("ALTER TABLE notes ADD COLUMN visibility TEXT")


def _bump_version(con):
    """인덱스 버전 +1 -> 임베딩 인메모리 캐시 무효화 신호. 색인 변경 후 호출."""
    con.execute("UPDATE index_meta SET value=value+1 WHERE key='version'")


def _index_version(con):
    """현재 인덱스 버전(임베딩 캐시 키). 없으면 0."""
    row = con.execute("SELECT value FROM index_meta WHERE key='version'").fetchone()
    return row[0] if row else 0


def _iter_note_files(root=None):
    """Vault 내 모든 .md 노트 절대경로를 순회(임시파일/템플릿 제외)."""
    root = root or S.VAULT_ROOT
    for dirpath, _dirs, files in os.walk(root):
        # 템플릿 폴더는 검색 대상 아님(빈 양식이 결과를 오염시키지 않게).
        if os.path.basename(dirpath) == S.DIR_TEMPLATES:
            continue
        for fn in files:
            if fn.endswith(".md") and not fn.startswith(".tmp_"):
                yield os.path.join(dirpath, fn)


# ── 로컬 임베딩(graceful degrade) ─────────────────────────────────────────────
def _load_embed_model():
    """sentence-transformers 로컬 모델을 1회 로드. 실패하면 False 를 캐시하고 None 반환
    (이후 호출은 즉시 FTS5-only 로 떨어진다). 네트워크 호출은 절대 하지 않는다 —
    HF_HUB_OFFLINE 를 켜 모델이 로컬 캐시에 없으면 다운로드 시도 없이 즉시 실패시킨다."""
    global _EMBED_MODEL
    if _EMBED_MODEL is not None:
        return _EMBED_MODEL or None
    # 오프라인 강제: 로컬 캐시에 모델이 없으면 다운로드 대신 실패(키·네트워크 사용 0).
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        from sentence_transformers import SentenceTransformer
        _EMBED_MODEL = SentenceTransformer(EMBED_MODEL_NAME)
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 FTS5-only 로 안전 강등
        sys.stderr.write(f"[vault_rag] 임베딩 비활성(FTS5 단독): {type(e).__name__}: {e}\n")
        _EMBED_MODEL = False
        return None
    return _EMBED_MODEL


def embed_available():
    """의미검색 사용 가능 여부(진단/CLI 표시용)."""
    return _load_embed_model() is not None


def _embed(text):
    """단일 텍스트 임베딩 -> list[float]. 모델 없으면 None(의미검색 비활성)."""
    model = _load_embed_model()
    if model is None:
        return None
    vec = model.encode([text], normalize_embeddings=True)[0]
    return [float(x) for x in vec]


def _cosine(a, b):
    """순수 파이썬 코사인 유사도(numpy 비의존). 차원 불일치/영벡터는 0."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── 인덱싱 ────────────────────────────────────────────────────────────────────
def _index_one(con, path, store_embedding=True):
    """노트 1개를 색인(존재하면 갱신). 반환: 'indexed' | 'skipped'(해시 동일) | 'error'."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return "error"
    fhash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    rel = os.path.relpath(path, S.VAULT_ROOT)
    cur = con.execute("SELECT rowid, hash FROM notes WHERE path=?", (rel,)).fetchone()
    if cur and cur[1] == fhash:
        return "skipped"  # 내용 동일 -> 재색인 불필요(증분의 핵심)
    fm, body = S.parse_note(raw)
    title = (body.strip().splitlines()[0][:120] if body.strip() else "")
    fts_text = f"{title}\n{body}"
    # visibility 는 frontmatter 값을 정규화(누락/오류 -> type 기본값). 회수 필터의 1차 키.
    visibility = S.normalize_visibility(fm.get("visibility"), fm.get("type"))
    if cur:
        rowid = cur[0]
        con.execute("""UPDATE notes SET hash=?,note_id=?,type=?,role=?,team=?,visibility=?,
                       date=?,title=?,body=? WHERE rowid=?""",
                    (fhash, fm.get("id", ""), fm.get("type", ""), fm.get("role", ""),
                     fm.get("team", ""), visibility, fm.get("date", ""), title, body, rowid))
        con.execute("DELETE FROM notes_fts WHERE rowid=?", (rowid,))
        con.execute("DELETE FROM embeddings WHERE rowid=?", (rowid,))
    else:
        cur2 = con.execute("""INSERT INTO notes(path,hash,note_id,type,role,team,visibility,
                              date,title,body) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (rel, fhash, fm.get("id", ""), fm.get("type", ""),
                            fm.get("role", ""), fm.get("team", ""), visibility,
                            fm.get("date", ""), title, body))
        rowid = cur2.lastrowid
    con.execute("INSERT INTO notes_fts(rowid, body, bigram) VALUES(?,?,?)",
                (rowid, fts_text, _bigrams(fts_text)))
    if store_embedding:
        vec = _embed(fts_text)
        if vec is not None:
            con.execute("INSERT INTO embeddings(rowid, vec, dim) VALUES(?,?,?)",
                        (rowid, json.dumps(vec), len(vec)))
    return "indexed"


def _purge_deleted(con):
    """디스크에 더 이상 없는 노트의 인덱스 행을 정리(삭제 동기화). 반환: 정리된 개수."""
    existing = {os.path.relpath(p, S.VAULT_ROOT) for p in _iter_note_files()}
    rows = con.execute("SELECT rowid, path FROM notes").fetchall()
    removed = 0
    for rowid, rel in rows:
        if rel not in existing:
            con.execute("DELETE FROM notes WHERE rowid=?", (rowid,))
            con.execute("DELETE FROM notes_fts WHERE rowid=?", (rowid,))
            con.execute("DELETE FROM embeddings WHERE rowid=?", (rowid,))
            removed += 1
    return removed


def index(con=None, store_embedding=True):
    """증분 인덱싱: 변경/신규 노트만 재색인 + 삭제 노트 정리. 통계 dict 반환."""
    own = con is None
    con = con or connect()
    stats = {"indexed": 0, "skipped": 0, "error": 0, "purged": 0}
    try:
        for path in _iter_note_files():
            stats[_index_one(con, path, store_embedding)] += 1
        stats["purged"] = _purge_deleted(con)
        # 색인 내용이 바뀌었으면 버전을 올려 임베딩 인메모리 캐시를 무효화한다.
        if stats["indexed"] or stats["purged"]:
            _bump_version(con)
        con.commit()
    finally:
        if own:
            con.close()
    return stats


def reindex(con=None, store_embedding=True):
    """전체 재색인: 인덱스를 비우고 처음부터 구축(스키마/모델 변경 시 사용)."""
    own = con is None
    con = con or connect()
    try:
        con.execute("DELETE FROM notes")
        con.execute("DELETE FROM notes_fts")
        con.execute("DELETE FROM embeddings")
        _bump_version(con)  # 전체 비움도 캐시 무효화 신호
        con.commit()
        return index(con, store_embedding)
    finally:
        if own:
            con.close()


# ── 검색(하이브리드 + RRF) ────────────────────────────────────────────────────
def _fts_query_escape(query):
    """FTS5 MATCH 안전화: 특수 연산자 제거 후 토큰을 OR 로 묶는다(부분일치 회수율 우선)."""
    toks = re.findall(r"[0-9A-Za-z가-힣]+", query or "")
    if not toks:
        return None
    quoted = [f'"{t}"' for t in toks]
    bigram_toks = re.findall(r"[가-힣]{2,}", query or "")
    for bt in bigram_toks:
        quoted += [f'"{bt[i:i + 2]}"' for i in range(len(bt) - 1)]
    return " OR ".join(dict.fromkeys(quoted))


def _meta_where(role, team, types, visibility_filter=True):
    """회수 가능 집합 SQL 조각 + 파라미터(공유 지식 계층 가시성 모델).

    viewer_role/viewer_team 관점에서 '볼 수 있는' 노트만 남긴다. 핵심 규칙:
        회수 가능 = (n.role = viewer_role)                      # 자기 기억(visibility 무관)
                  OR (n.visibility = 'org')                     # 전사 공개
                  OR (n.team = viewer_team AND n.visibility IN ('team','org'))  # 같은 팀 공유
    이 모델이 기존 role/team 엄격 필터를 대체한다 — 격리(자기 기억)와 공유(전사·팀 공개)를
    동시에 만족시킨다. 레거시 visibility(빈/NULL)는 색인 시 type 기본값으로 정규화돼 있다.

    role/team 둘 다 비면(대시보드 전체 조망 등) 가시성 필터를 적용하지 않는다(관리자 열람).
    visibility_filter=False 면 순수 role/team 일치만(하위호환·내부 용도).
    """
    clauses, params = [], []
    if visibility_filter and (role or team):
        ors, vparams = [], []
        if role:
            ors.append("n.role=?")
            vparams.append(role)
        ors.append("n.visibility='org'")
        if team:
            ors.append("(n.team=? AND n.visibility IN ('team','org'))")
            vparams.append(team)
        clauses.append("(" + " OR ".join(ors) + ")")
        params.extend(vparams)
    elif not visibility_filter:
        if role:
            clauses.append("n.role=?")
            params.append(role)
        if team:
            clauses.append("(n.team=? OR n.team='' OR n.team IS NULL)")
            params.append(team)
    if types:
        clauses.append("n.type IN (%s)" % ",".join("?" * len(types)))
        params.extend(types)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _fts_rank(con, query, role, team, types, limit, apply_visibility=True):
    """FTS5 어휘검색 랭킹: [(rowid, rank_position)]. bm25 오름차순(작을수록 관련)."""
    match = _fts_query_escape(query)
    if not match:
        return []
    where, params = _meta_where(role, team, types, visibility_filter=apply_visibility)
    sql = f"""SELECT n.rowid FROM notes_fts f
              JOIN notes n ON n.rowid=f.rowid
              WHERE notes_fts MATCH ?{where}
              ORDER BY bm25(notes_fts) ASC LIMIT ?"""
    try:
        rows = con.execute(sql, [match, *params, limit]).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r[0], i) for i, r in enumerate(rows)]


def _load_embed_matrix(con):
    """embeddings 전체를 (rowids, matrix)로 로드해 인덱스 버전 키로 인메모리 캐시한다.
    matrix 는 numpy 가 있으면 정규화된 ndarray(행=노트, 코사인=행렬 dot), 없으면 list[list].
    인덱스 버전이 바뀌면 자동 무효화 -> 매 검색마다 json.loads N건 재파싱을 제거한다."""
    db_key = _db_key(con)
    version = _index_version(con)
    cached = _EMBED_CACHE.get(db_key)
    if cached and cached["version"] == version:
        return cached["rowids"], cached["matrix"]
    rowids, vecs = [], []
    for rowid, vec_json in con.execute("SELECT rowid, vec FROM embeddings").fetchall():
        try:
            vec = json.loads(vec_json)
        except (ValueError, TypeError):
            continue
        rowids.append(rowid)
        vecs.append(vec)
    if _np is not None and vecs:
        matrix = _np.asarray(vecs, dtype=_np.float32)
        # 저장 시 normalize_embeddings=True 라 정규화돼 있으나, 폴백/외부 입력 안전을 위해
        # 영벡터를 피하며 행 단위 L2 정규화를 한 번 더 적용(코사인=dot 동치 보장).
        norms = _np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = matrix / norms
    else:
        matrix = vecs  # 순수파이썬 폴백(list[list[float]])
    _EMBED_CACHE[db_key] = {"version": version, "rowids": rowids, "matrix": matrix}
    return rowids, matrix


def _semantic_rank(con, query, role, team, types, limit, apply_visibility=True):
    """임베딩 의미검색 랭킹: [(rowid, rank_position)]. 모델 없으면 빈 리스트(강등).
    numpy 가 있으면 캐시된 임베딩 행렬과의 dot 으로 전 노트 코사인을 한 번에 계산하고,
    없으면 순수파이썬 코사인으로 동일 결과를 낸다(정확도 불변, 메타필터는 사후 적용)."""
    qvec = _embed(query)
    if qvec is None:
        return []
    rowids, matrix = _load_embed_matrix(con)
    if not rowids:
        return []
    # 메타/가시성 필터에 걸리는 rowid 집합(SQL 한 번으로 산출 후 사후 마스킹).
    where, params = _meta_where(role, team, types, visibility_filter=apply_visibility)
    allowed = {r[0] for r in con.execute(
        f"SELECT n.rowid FROM notes n WHERE 1=1{where}", params).fetchall()}
    if _np is not None and isinstance(matrix, _np.ndarray):
        q = _np.asarray(qvec, dtype=_np.float32)
        qn = _np.linalg.norm(q)
        if qn == 0:
            return []
        sims = matrix @ (q / qn)  # 정규화 행렬 · 정규화 질의 = 코사인 유사도 벡터
        order = _np.argsort(-sims)  # 내림차순
        out, pos = [], 0
        for idx in order:
            rid = rowids[int(idx)]
            if rid in allowed:
                out.append((rid, pos))
                pos += 1
                if pos >= limit:
                    break
        return out
    # 순수파이썬 폴백(numpy 부재): 동일한 정렬 결과를 보장.
    scored = []
    for rid, vec in zip(rowids, matrix):
        if rid not in allowed:
            continue
        scored.append((rid, _cosine(qvec, vec)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [(rid, i) for i, (rid, _s) in enumerate(scored[:limit])]


def _rrf_fuse(rankings):
    """여러 랭킹을 RRF 로 융합: score(d)=sum 1/(k+rank). rowid->점수 dict 반환."""
    fused = {}
    for ranking in rankings:
        for rowid, pos in ranking:
            fused[rowid] = fused.get(rowid, 0.0) + 1.0 / (RRF_K + pos + 1)
    return fused


def search(query, role=None, team=None, types=None, top_k=5, con=None,
           apply_visibility=True):
    """하이브리드 검색: FTS5 + 의미검색을 RRF 로 융합해 상위 top_k 노트를 반환.
    반환: [{path,note_id,type,role,team,visibility,date,title,snippet,score}].

    가시성 모델(공유 지식 계층): role/team 은 viewer 컨텍스트로 해석된다 — 회수 가능 =
    자기 role 기억 OR 전사 공개(org) OR 같은 팀의 team/org 공개. 이로써 에이전트는 자기
    기억과 전사 공개 기억을 함께 회수한다(격리·공유 양립).
    apply_visibility=False 면 가시성 무시하고 순수 role/team 일치만(대시보드 관리자 전체 열람).
    의미검색 모델이 없으면 자동으로 FTS5 단독 결과를 반환한다(graceful degrade)."""
    own = con is None
    con = con or connect()
    try:
        pool = max(top_k * 4, 20)
        fts = _fts_rank(con, query, role, team, types, pool, apply_visibility)
        sem = _semantic_rank(con, query, role, team, types, pool, apply_visibility)
        fused = _rrf_fuse([fts, sem])
        if not fused:
            return []
        top = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:top_k]
        results = []
        for rowid, score in top:
            row = con.execute(
                "SELECT path,note_id,type,role,team,visibility,date,title,body "
                "FROM notes WHERE rowid=?", (rowid,)).fetchone()
            if not row:
                continue
            body = row[8] or ""
            results.append({
                "path": row[0], "note_id": row[1], "type": row[2], "role": row[3],
                "team": row[4], "visibility": row[5] or "", "date": row[6], "title": row[7],
                "snippet": body.strip().replace("\n", " ")[:200], "score": round(score, 6),
            })
        return results
    finally:
        if own:
            con.close()


# ── CLI ───────────────────────────────────────────────────────────────────────
def _main(argv=None):
    parser = argparse.ArgumentParser(description="Vault 로컬 RAG 인덱스/검색")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index", help="증분 인덱싱(변경분만)")
    sub.add_parser("reindex", help="전체 재색인")
    sp = sub.add_parser("search", help="하이브리드 검색")
    sp.add_argument("query")
    sp.add_argument("--role", default=None)
    sp.add_argument("--team", default=None)
    sp.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args(argv)

    if args.cmd in ("index", "reindex"):
        stats = (reindex if args.cmd == "reindex" else index)()
        mode = "의미+어휘" if embed_available() else "어휘(FTS5)단독"
        print(f"[{args.cmd}] {stats} | 검색모드={mode}")
        return 0
    if args.cmd == "search":
        hits = search(args.query, role=args.role, team=args.team, top_k=args.top_k)
        mode = "의미+어휘" if embed_available() else "어휘(FTS5)단독"
        print(f"검색='{args.query}' role={args.role} team={args.team} 모드={mode} -> {len(hits)}건")
        for i, h in enumerate(hits, 1):
            print(f"  {i}. [{h['type']}/{h['role']}/{h['team']}] {h['title']}")
            print(f"     score={h['score']} path={h['path']}")
            print(f"     {h['snippet']}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
