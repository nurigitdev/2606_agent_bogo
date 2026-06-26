"""
두뇌 <-> Vault 통합 어댑터(graceful, 실패해도 기존 동작 보존).

hermes_runtime 의 decide()/run() 이 Vault RAG·writer 를 직접 import 하면 결합도가 높아지고
회귀 위험이 커진다. 그래서 모든 통합을 이 얇은 어댑터 한 곳에 가두고, 어떤 단계가 실패해도
예외를 삼켜 '기존 동작으로 폴백'한다(RAG 미설치/인덱스 손상/디스크 오류에도 봇은 계속 돈다).

제공:
  - team_of_role(teams, role)          : ROLE -> 팀 label 매핑(메타필터·노트 team 필드용).
  - retrieve_context(query, role, ...) : RAG 검색 -> 프롬프트 주입용 문자열([관련 과거 기억]).
                                         기존 역할별 세션 누적과 '공존'하도록 중복 라인은 거른다.
  - persist_report / persist_feedback  : 행동 결과를 Vault 에 영속(다음 검색에 노출).
  - ensure_indexed(throttle)           : 호출 빈도를 제한해 증분 인덱싱을 백그라운드처럼 수행.
"""
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 통합 토글(1=on 기본, 0=완전 비활성 -> 기존 동작과 100% 동일). 진단·롤백용.
VAULT_ENABLED = os.environ.get("HERMES_VAULT_ENABLED", "1") == "1"
# RAG 주입 top-k(과다 주입은 토큰·노이즈를 늘리므로 보수적).
RAG_TOP_K = int(os.environ.get("HERMES_VAULT_TOPK", "4") or "4")
# 증분 인덱싱 최소 간격(초). 매 메시지마다 전체 walk 를 돌리지 않게 스로틀.
INDEX_THROTTLE_SEC = int(os.environ.get("HERMES_VAULT_INDEX_THROTTLE", "60") or "60")
_last_index_at = 0.0


def _safe_import():
    """Vault 모듈을 지연 import(어댑터 자체가 import 실패에 강하도록). 실패 시 None 묶음."""
    try:
        import vault_rag as R
        import vault_schema as S
        import vault_writer as W
        return R, W, S
    except Exception:  # noqa: BLE001
        return None, None, None


def _log_retrieval(query, hits, role, team, source):
    """회수 텔레메트리를 append(실패 무해). 모듈 없으면 조용히 패스."""
    try:
        import vault_telemetry as T
        T.log_retrieval(query, hits, viewer_role=role or "", viewer_team=team or "",
                        source=source)
    except Exception:  # noqa: BLE001 — 텔레메트리 실패는 흐름을 막지 않는다
        pass


def team_of_role(teams, role):
    """ROLE(파일 stem: dev/hr/orchestrator/ceo)을 팀 label 로 매핑. 팀 없으면 빈 문자열.
    teams.json 의 teams[].id == role 이면 그 label, 아니면(오케스트레이터/ceo) ''."""
    if not isinstance(teams, dict):
        return ""
    for t in teams.get("teams", []):
        if t.get("id") == role:
            return t.get("label", "")
    return ""


def ensure_indexed(force=False):
    """증분 인덱싱을 스로틀과 함께 수행(실패 무해). 마지막 실행 후 일정 시간 지나야 재색인."""
    global _last_index_at
    if not VAULT_ENABLED:
        return False
    now = time.time()
    if not force and (now - _last_index_at) < INDEX_THROTTLE_SEC:
        return False
    R, _W, _S = _safe_import()
    if R is None:
        return False
    try:
        R.index()
        _last_index_at = now
        return True
    except Exception:  # noqa: BLE001 — 인덱싱 실패는 검색 품질만 떨어뜨릴 뿐 봇을 막지 않는다
        _last_index_at = now
        return False


def retrieve_context(query, role, team="", existing_text="", top_k=None):
    """RAG 로 관련 과거 보고·피드백 top-k 를 회수해 프롬프트 주입 문자열로 반환.
    - viewer 컨텍스트(호출 role/team)를 그대로 검색에 넘긴다 -> 가시성 모델에 의해 에이전트는
      '자기 기억 + 같은 팀 공유 + 전사 공개(org)' 기억을 함께 회수한다(격리·공유 양립).
    - 기존 누적(세션/학습노트, existing_text)과 중복되는 라인은 제외(이중 주입 회피).
    - 결과 없음/모듈 없음/오류 -> 빈 문자열(주입 없이 기존 흐름 유지)."""
    if not VAULT_ENABLED or not (query or "").strip():
        return ""
    R, _W, _S = _safe_import()
    if R is None:
        return ""
    try:
        ensure_indexed()
        # role/team = viewer 컨텍스트. 가시성 필터가 회수 가능 집합을 정의한다.
        hits = R.search(query, role=role or None, team=team or None,
                        top_k=top_k or RAG_TOP_K)
    except Exception:  # noqa: BLE001
        return ""
    _log_retrieval(query, hits, role, team, source="retrieve_context")
    if not hits:
        return ""
    existing = existing_text or ""
    lines = []
    for h in hits:
        snippet = (h.get("title") or h.get("snippet") or "").strip()
        if not snippet or snippet in existing:
            continue
        date = (h.get("date") or "")[:10]
        lines.append(f"- ({h.get('type', '')}/{date}) {snippet}")
    if not lines:
        return ""
    return "[관련 과거 기억(RAG 회수 — 참고용, 현재 지시 우선)]\n" + "\n".join(lines)


def persist_report(role, team, summary, body="", links=None, tags=None):
    """행동 결과(보고/진행 메모)를 Vault 에 영속. 성공 시 노트 절대경로, 실패/비활성 시 None."""
    if not VAULT_ENABLED or not (summary or "").strip():
        return None
    _R, W, _S = _safe_import()
    if W is None:
        return None
    try:
        return W.append_report(role=role, team=team or "", summary=summary,
                               body=body, links=links, tags=tags)
    except Exception:  # noqa: BLE001 — 영속 실패는 기존 memory_*.json 갱신으로 충분히 폴백됨
        return None


def persist_feedback(role, team, summary, body="", links=None, tags=None, kind="feedback"):
    """교정·피드백·노하우를 Vault 에 영속. 성공 시 경로, 실패/비활성 시 None."""
    if not VAULT_ENABLED or not (summary or "").strip():
        return None
    _R, W, _S = _safe_import()
    if W is None:
        return None
    try:
        return W.append_feedback(role=role, team=team or "", summary=summary,
                                 body=body, links=links, tags=tags, kind=kind)
    except Exception:  # noqa: BLE001
        return None
