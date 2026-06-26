"""
Vault RAG 검색 품질 eval — 기존 노트로 시드 쿼리→기대노트 쌍을 만들어 recall@k 측정.

목적: '검색이 실제로 맞는 노트를 회수하는가'를 수치로 못박아 회귀를 잡는다. 외부 정답셋 없이,
이미 Vault 에 있는 노트의 제목/요약을 쿼리로 삼고 '그 노트 자신이 top-k 에 들어오는가'를 본다
(self-retrieval recall). 완벽 검색이면 recall@1=1.0 에 수렴한다 -> 기준선 대비 하락=회귀 신호.

원칙: 100% 로컬. 네트워크·API 키·LLM 사용 0. 측정만 하고 어떤 노트도 새로 만들지 않는다.

CLI:
  python vault_eval.py            # 현재 인덱스로 recall@1,3,5 출력
  python vault_eval.py --k 1 3 5 10
"""
import argparse
import os

import vault_rag as R
import vault_schema as S


def _seed_queries(limit=200):
    """기존 노트에서 (query, expected_path, role, team) 시드 쌍을 만든다.
    query = 노트 제목(본문 첫 줄). digest/템플릿은 제외(파생물은 정답 기준에서 뺀다)."""
    seeds = []
    if not os.path.isdir(S.VAULT_ROOT):
        return seeds
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
            if fm.get("type") in ("digest",):
                continue
            title = body.strip().splitlines()[0][:120] if body.strip() else ""
            if not title:
                continue
            rel = os.path.relpath(abs_p, os.path.realpath(S.VAULT_ROOT))
            seeds.append({"query": title, "expected": rel,
                          "role": fm.get("role", ""), "team": fm.get("team", "")})
            if len(seeds) >= limit:
                return seeds
    return seeds


def evaluate(ks=(1, 3, 5), use_viewer=True, con=None):
    """recall@k 측정. use_viewer=True 면 노트 자신의 role/team 을 viewer 로 넘겨(현실 회수 경로)
    가시성 필터를 통과하는지까지 본다. 반환 {n, recall@k:{...}, misses:[...]}."""
    own = con is None
    con = con or R.connect()
    try:
        seeds = _seed_queries()
        if not seeds:
            return {"n": 0, "recall": {f"@{k}": 0.0 for k in ks}, "misses": []}
        maxk = max(ks)
        hit_at = {k: 0 for k in ks}
        misses = []
        for sd in seeds:
            role = sd["role"] if use_viewer else None
            team = sd["team"] if use_viewer else None
            hits = R.search(sd["query"], role=role or None, team=team or None,
                            top_k=maxk, con=con)
            paths = [h["path"] for h in hits]
            rank = paths.index(sd["expected"]) + 1 if sd["expected"] in paths else None
            for k in ks:
                if rank is not None and rank <= k:
                    hit_at[k] += 1
            if rank is None:
                misses.append(sd["query"][:60])
        n = len(seeds)
        return {
            "n": n,
            "recall": {f"@{k}": round(hit_at[k] / n, 4) for k in ks},
            "misses": misses[:20],
        }
    finally:
        if own:
            con.close()


def _main(argv=None):
    p = argparse.ArgumentParser(description="Vault RAG recall@k eval")
    p.add_argument("--k", type=int, nargs="+", default=[1, 3, 5])
    p.add_argument("--no-viewer", action="store_true",
                   help="가시성 필터 없이 평가(순수 검색 품질)")
    args = p.parse_args(argv)
    res = evaluate(ks=tuple(args.k), use_viewer=not args.no_viewer)
    mode = "어휘+의미" if R.embed_available() else "어휘(FTS5)단독"
    print(f"[eval] n={res['n']} 모드={mode} viewer={not args.no_viewer}")
    for k in args.k:
        print(f"  recall@{k} = {res['recall'].get(f'@{k}')}")
    if res["misses"]:
        print(f"  misses({len(res['misses'])}): " + " | ".join(res["misses"][:5]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
