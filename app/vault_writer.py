"""
Vault 쓰기 규율 모듈 — 다중 에이전트 동시 쓰기 무충돌.

핵심 규율(동시성 안전의 근거):
  - 한 파일 = 한 사실. 파일명에 UTC 타임스탬프 + 짧은 uuid 를 박아 '서로 다른 사실은
    서로 다른 파일'이 되게 한다 -> 여러 에이전트가 동시에 써도 같은 파일을 두고 경쟁하지
    않는다(쓰기 경합 0). append 로그 방식의 race(읽고-합치고-쓰기) 자체를 제거한다.
  - atomic write: 임시 파일에 전량 기록 -> fsync -> os.replace 로 원자적 교체. 동일 디렉토리
    내 rename 은 POSIX 에서 원자적이므로, 절반만 쓰인 노트가 인덱서/Obsidian 에 보이지 않는다.
  - frontmatter 자동 채움 + 스키마 검증(vault_schema). 검증 실패 노트는 쓰지 않고 오류 반환
    (거짓/불완전 데이터가 진실원천에 들어가는 것을 입구에서 차단).

공개 API:
  - write_note(frontmatter, body) : 저수준. 임의 type 노트 1개를 원자적으로 기록.
  - append_report(role, team, summary, body, links) : 보고 노트(20_Reports/<Y>/<M>).
  - append_feedback(role, team, summary, body, links, kind) : 피드백/교정 노트(30_Feedback).
  - append_decision / append_policy / write_profile : CEO 판단·정책·프로필 노트.
모두 '생성한 파일 절대경로'를 반환(실패 시 None). 멱등 키가 필요하면 호출부가 links/tags 로 표현.
"""
import os
import tempfile
import uuid

import vault_schema as S


def ensure_vault():
    """Vault 최상위 폴더 구조를 보장(idempotent). 부재 폴더만 생성한다."""
    os.makedirs(S.VAULT_ROOT, exist_ok=True)
    for d in S.TOP_DIRS:
        os.makedirs(S.vault_path(d), exist_ok=True)


def _atomic_write(abs_path, content):
    """abs_path 에 content 를 원자적으로 기록. 같은 디렉토리에 임시파일 작성 후 os.replace.
    임시파일을 같은 디렉토리에 두는 이유: cross-device rename(비원자적) 회피 + 권한 일관."""
    target_dir = os.path.dirname(abs_path)
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".md", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())  # 디스크 반영 보장(크래시 내성)
        os.replace(tmp, abs_path)  # 원자적 교체(동일 디렉토리)
    except BaseException:
        # 실패 시 임시파일 흔적 제거(부분 파일이 인덱서에 노출되지 않게).
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return abs_path


def _new_filename(ntype, role):
    """'한 사실 = 한 파일' 보장 파일명: <type>_<role>_<UTC타임스탬프>_<짧은uuid>.md.
    타임스탬프(정렬)+uuid(충돌 0)로 동시 쓰기에도 파일명이 겹치지 않는다."""
    short = uuid.uuid4().hex[:8]
    return f"{S.id_slug(ntype)}_{S.id_slug(role)}_{S.utc_stamp()}_{short}.md"


def _note_id(ntype, role, fname):
    """frontmatter id: 파일 stem 과 일치시켜 노트<->파일 1:1 추적이 항상 성립하게 한다."""
    return os.path.splitext(fname)[0]


def write_note(frontmatter, body, subdir_parts=None):
    """저수준 노트 기록. frontmatter 검증 통과 시에만 원자적으로 쓴다.
    subdir_parts 가 주어지면 그 하위 폴더(Vault 루트 기준)에, 없으면 type 별 기본 폴더에 둔다.
    반환: (abs_path, []) 성공 / (None, errors) 실패. 호출부가 오류를 로깅·거부."""
    ensure_vault()
    fm = dict(frontmatter)
    ntype = fm.get("type", "")
    role = fm.get("role", "") or ""
    # 폴더 결정: 명시 subdir > type 기본 매핑.
    if subdir_parts is None:
        subdir_parts = _default_subdir(ntype, fm.get("date"))
    fname = _new_filename(ntype, role)
    if not fm.get("id"):
        fm["id"] = _note_id(ntype, role, fname)
    errors = S.validate_frontmatter(fm)
    if errors:
        return None, errors
    content = S.dump_frontmatter(fm) + "\n" + (body or "").rstrip() + "\n"
    abs_path = S.vault_path(*subdir_parts, fname)
    _atomic_write(abs_path, content)
    return abs_path, []


def _default_subdir(ntype, date_iso):
    """type -> 기본 저장 폴더 매핑(시간축이 의미 있는 report 만 연/월 분할)."""
    if ntype == "report":
        return list(S.report_dir(date_iso))
    if ntype == "feedback":
        return [S.DIR_FEEDBACK]
    if ntype == "decision":
        return [S.DIR_CEO]
    if ntype == "policy":
        return [S.DIR_SYSTEM]
    if ntype == "profile":
        return [S.DIR_SYSTEM]
    return [S.DIR_SYSTEM]


def _compose_body(summary, body):
    """요약(굵게) + 본문을 노트 본문으로 합성. 검색·열람 모두에서 요약이 선두에 오게 한다."""
    summary = (summary or "").strip()
    body = (body or "").strip()
    head = f"**{summary}**\n\n" if summary else ""
    return head + body


def append_report(role, team, summary, body="", links=None, tags=None):
    """보고 노트 1건을 20_Reports/<YYYY>/<MM> 에 원자적으로 기록.
    summary 는 한 줄 결론, body 는 상세. links 로 관련 노트를 [[wikilink]] 연결한다."""
    fm = S.default_frontmatter("report", role, team, links=links, tags=tags)
    return write_note(fm, _compose_body(summary, body))[0]


def append_feedback(role, team, summary, body="", links=None, tags=None, kind="feedback"):
    """피드백/교정/노하우 노트 1건을 30_Feedback 에 기록. kind 는 tags 로 분류 보존."""
    tag_list = list(tags) if tags else []
    if kind and kind not in tag_list:
        tag_list.append(kind)
    fm = S.default_frontmatter("feedback", role, team, links=links, tags=tag_list)
    return write_note(fm, _compose_body(summary, body))[0]


def append_decision(role, team, summary, body="", links=None, tags=None):
    """CEO 디지털 트윈(한도윤)의 판단/결정 노트 1건을 00_CEO 에 기록."""
    fm = S.default_frontmatter("decision", role, team, links=links, tags=tags)
    return write_note(fm, _compose_body(summary, body))[0]


def append_policy(role, team, summary, body="", links=None, tags=None):
    """정책/규정 노트 1건을 90_System 에 기록."""
    fm = S.default_frontmatter("policy", role, team, links=links, tags=tags)
    return write_note(fm, _compose_body(summary, body))[0]


def write_profile(role, team, summary, body="", links=None, tags=None):
    """역할/팀 프로필 노트 1건을 기록(조직도/그래프 노드의 정체성 노트)."""
    fm = S.default_frontmatter("profile", role, team, links=links, tags=tags)
    return write_note(fm, _compose_body(summary, body))[0]
