"""
Vault RAG 회수 텔레메트리 — append-only JSONL 로깅(실패 무해).

목적: 검색 품질을 사후 분석할 수 있게 '무엇을 누가 검색했고 무엇이 회수됐는가'를 가볍게 남긴다.
원칙(보안·프라이버시):
  - append-only. 한 줄 = 한 회수 이벤트. 실패해도 절대 호출부를 막지 않는다(검색은 계속 동작).
  - 본문(snippet) 원문은 남기지 않는다 — 쿼리/경로/score/viewer 요약만(민감정보 과다기록 차단).
  - 쿼리는 길이 상한으로 절단(프롬프트 통째 유출 방지).
로그 위치: logs/vault_rag.jsonl(app/logs). .gitignore 대상(파생 운영 로그).
"""
import datetime
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
LOG_PATH = os.path.join(LOG_DIR, "vault_rag.jsonl")
# 쿼리 절단 상한(프롬프트 통째 기록 방지). 분석엔 충분, 유출엔 부족하게.
_QUERY_MAX = 160


def log_retrieval(query, hits, viewer_role="", viewer_team="", source="search"):
    """회수 이벤트 1건을 JSONL 로 append. 어떤 예외도 삼킨다(텔레메트리는 부가기능)."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source,
            "viewer_role": viewer_role or "",
            "viewer_team": viewer_team or "",
            "query": (query or "")[:_QUERY_MAX],
            "n": len(hits or []),
            # 경로·type·score 만(본문 원문 제외 — 민감정보 과다기록 금지).
            "hits": [
                {"path": h.get("path", ""), "type": h.get("type", ""),
                 "visibility": h.get("visibility", ""), "score": h.get("score", 0)}
                for h in (hits or [])
            ],
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001 — 텔레메트리 실패는 검색을 막지 않는다
        return False
