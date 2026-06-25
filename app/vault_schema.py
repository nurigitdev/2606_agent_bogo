"""
Obsidian Vault 조직 스키마 — 단일 진실원천(SSOT).

이 모듈은 Vault 의 '구조 계약'만 책임진다(쓰기·검색·마이그레이션 로직 없음):
  - 폴더 스키마(00_CEO/10_Teams/20_Reports/30_Feedback/90_System/_templates/_index).
  - 노트 frontmatter 스키마(id/type/role/team/date/tags/links) + 검증 함수.
  - 경로 traversal 방지(노트 파일은 Vault 루트 밖으로 절대 못 나간다).
  - 최소 YAML frontmatter 직렬화/역직렬화(외부 의존 0 — 봇 런타임 venv 가
    PyYAML 없이도 동작하도록 표준 라이브러리만 쓴다. 값은 단순 스칼라/리스트로 제한).

설계 원칙:
  - frontmatter 는 '정렬 가능·관계 추적 가능'을 보장하는 최소 스키마. Obsidian 그래프뷰는
    links([[wikilink]]) 로 관계를 그린다 -> links 는 항상 리스트로 강제.
  - type 은 폐쇄 집합(report|feedback|decision|profile|policy). 위반 시 거부(거짓 데이터 차단).
  - role/team 은 메타필터(RAG 역할/팀 검색)의 키이므로 빈 문자열을 허용하되 None 은 거부.
  - 모든 검증은 예외가 아니라 '오류 리스트'를 반환 -> 호출부가 거부/로깅을 결정(부분 실패 가시화).
"""
import datetime
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
# Vault 루트(git 추적 대상). 인덱스 db 만 .gitignore 로 제외된다.
VAULT_ROOT = os.path.join(HERE, "vault")

# ── 폴더 스키마(정렬용 숫자 접두 + 확장 여지) ──────────────────────────────────
DIR_CEO = "00_CEO"          # CEO 디지털 트윈(한도윤) 판단·프로필
DIR_TEAMS = "10_Teams"      # 10_Teams/<team>/ — 팀별 작업·프로필
DIR_REPORTS = "20_Reports"  # 20_Reports/<YYYY>/<MM>/ — 모든 보고(시간축)
DIR_FEEDBACK = "30_Feedback"  # 30_Feedback/ — 교정·피드백·노하우
DIR_SYSTEM = "90_System"    # 90_System/ — 정책·시스템 노트
DIR_TEMPLATES = "_templates"  # 노트 템플릿(Obsidian Templates 플러그인 호환)
DIR_INDEX = "_index"        # MOC(HOME.md) 등 자동 생성 인덱스
TOP_DIRS = (DIR_CEO, DIR_TEAMS, DIR_REPORTS, DIR_FEEDBACK, DIR_SYSTEM,
            DIR_TEMPLATES, DIR_INDEX)

# ── frontmatter 스키마 ────────────────────────────────────────────────────────
REQUIRED_KEYS = ("id", "type", "role", "team", "date", "tags", "links")
VALID_TYPES = ("report", "feedback", "decision", "profile", "policy")
LIST_KEYS = ("tags", "links")

# 한글은 보존하되 경로 위험 문자만 제거(Obsidian 한글 파일명 지원 → 팀명 '개발' 살림).
_UNSAFE_PATH_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
_ID_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def utc_now_iso():
    """현재 UTC 시각 ISO8601(초 단위, Z 표기). frontmatter date 기본값."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp():
    """파일명용 정렬 가능 UTC 타임스탬프(YYYYMMDDThhmmssZ). 사전순=시간순."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_slug(value, max_len=48):
    """임의 문자열을 파일명 안전 슬러그로(경로 위험 문자 제거, 길이 상한). 한글 보존."""
    s = _UNSAFE_PATH_RE.sub("", (value or "").strip())
    s = s.replace("..", "").replace(" ", "_")
    s = s.strip("._-") or "untitled"
    return s[:max_len]


def id_slug(value, max_len=32):
    """id/파일 접두용 보수적 슬러그(영숫자+하이픈/언더스코어만). 빈 값이면 'x'."""
    s = _ID_SLUG_RE.sub("", (value or "")).strip("._-")
    return (s or "x")[:max_len]


def vault_path(*parts):
    """Vault 루트 기준 안전 경로 결합. traversal(상위 탈출) 시 ValueError.
    모든 노트 쓰기는 이 함수를 통과해야 한다 -> 루트 밖 파일 생성 원천 차단."""
    root = os.path.realpath(VAULT_ROOT)
    target = os.path.realpath(os.path.join(root, *parts))
    if os.path.commonpath([root, target]) != root:
        raise ValueError(f"Vault 경로 이탈 차단: {parts!r} -> {target!r}")
    return target


def report_dir(date_iso=None):
    """보고 저장 폴더(20_Reports/<YYYY>/<MM>) 상대경로 조각 반환(없으면 현재 UTC)."""
    if date_iso:
        try:
            dt = datetime.datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.datetime.now(datetime.timezone.utc)
    else:
        dt = datetime.datetime.now(datetime.timezone.utc)
    return (DIR_REPORTS, f"{dt.year:04d}", f"{dt.month:02d}")


def default_frontmatter(ntype, role, team, links=None, tags=None, note_id=None,
                        date_iso=None):
    """스키마를 만족하는 frontmatter dict 를 기본값으로 채워 생성한다."""
    return {
        "id": note_id or "",
        "type": ntype,
        "role": role if role is not None else "",
        "team": team if team is not None else "",
        "date": date_iso or utc_now_iso(),
        "tags": list(tags) if tags else [],
        "links": list(links) if links else [],
    }


def validate_frontmatter(fm):
    """frontmatter dict 를 스키마에 비춰 검증. 오류 메시지 리스트 반환(빈 리스트=정상)."""
    errors = []
    if not isinstance(fm, dict):
        return ["frontmatter 가 dict 가 아님"]
    for k in REQUIRED_KEYS:
        if k not in fm:
            errors.append(f"필수 키 누락: {k}")
    if fm.get("type") not in VALID_TYPES:
        errors.append(f"type 위반(허용={VALID_TYPES}): {fm.get('type')!r}")
    for k in ("role", "team", "id", "date"):
        if k in fm and not isinstance(fm.get(k), str):
            errors.append(f"{k} 는 문자열이어야 함: {type(fm.get(k)).__name__}")
    for k in LIST_KEYS:
        if k in fm and not isinstance(fm.get(k), list):
            errors.append(f"{k} 는 리스트여야 함: {type(fm.get(k)).__name__}")
    if isinstance(fm.get("id"), str) and not fm.get("id"):
        errors.append("id 가 빈 문자열")
    return errors


def extract_wikilinks(text):
    """본문/frontmatter 문자열에서 [[wikilink]] 대상 리스트 추출(그래프 관계 인덱싱용)."""
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(text or "")]


# ── 최소 YAML frontmatter 직렬화/역직렬화(표준 라이브러리만) ────────────────────
def _yaml_scalar(v):
    """스칼라 값을 frontmatter 안전 표기로. 특수문자/콜론 포함 시 따옴표로 감싼다."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{}\"']|^\s|\s$|^[&*!|>%@`]", s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def dump_frontmatter(fm):
    """frontmatter dict -> '---\\n...\\n---\\n' YAML 블록 문자열. 키 순서는 스키마 순서 고정."""
    lines = ["---"]
    ordered = list(REQUIRED_KEYS) + [k for k in fm if k not in REQUIRED_KEYS]
    seen = set()
    for k in ordered:
        if k in seen or k not in fm:
            continue
        seen.add(k)
        v = fm[k]
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                lines.extend(f"  - {_yaml_scalar(item)}" for item in v)
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _parse_scalar(raw):
    """frontmatter 스칼라 토큰을 파싱(따옴표 해제, true/false 변환)."""
    s = raw.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if s == "true":
        return True
    if s == "false":
        return False
    return s


def parse_note(text):
    """노트 전체 문자열을 (frontmatter dict, body str) 로 분리.
    frontmatter 블록(--- ... ---)이 없으면 ({}, 원문) 반환(graceful)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n")
    if parts[0].strip() != "---":
        return {}, text
    fm, body_start, cur_key = {}, None, None
    for i in range(1, len(parts)):
        line = parts[i]
        if line.strip() == "---":
            body_start = i + 1
            break
        if line.startswith("  - ") and cur_key:
            # 'key:'(값 없음) 다음에 '  - item' 들이 오는 블록 스칼라 → 리스트로 승격.
            # 직전에 빈 문자열로 들어간 키도 여기서 리스트로 교체한다(빈 값 + 항목 공존 방지).
            if not isinstance(fm.get(cur_key), list):
                fm[cur_key] = []
            fm[cur_key].append(_parse_scalar(line[4:]))
            continue
        m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2)
            cur_key = key
            if val.strip() == "" or val.strip() == "[]":
                fm[key] = [] if val.strip() == "[]" else ""
            else:
                fm[key] = _parse_scalar(val)
    if body_start is None:
        return {}, text
    body = "\n".join(parts[body_start:])
    return fm, body.lstrip("\n")
