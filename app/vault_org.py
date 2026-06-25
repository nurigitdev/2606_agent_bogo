"""
조직 구조 동기화 — teams.json 을 읽어 Vault 폴더·MOC(HOME.md)를 자동 생성.

설계: 팀/역할 정의의 진실원천은 teams.json 한 곳이다. 팀이 추가되면 여기 파이썬을 고치는
일 없이 sync_org() 만 다시 돌리면 (a)팀 폴더(10_Teams/<team>/) (b)MOC 의 팀·역할 [[링크]] 가
재생성된다 -> Obsidian 그래프뷰에서 조직 관계(오케스트레이터->팀->학습방)가 노드/엣지로 보인다.

MOC(Map of Content) = 사람(CEO)이 Vault 첫 화면에서 조직 전체를 조망하고 각 팀/역할 노트로
진입하는 허브. wikilink([[...]])로만 관계를 표현해 Obsidian 그래프가 자동으로 엣지를 그린다.
"""
import json
import os

import vault_schema as S
import vault_writer as W

HERE = os.path.dirname(os.path.abspath(__file__))
TEAMS_JSON = os.path.join(HERE, "teams.json")


def load_teams(path=None):
    """teams.json 로드. orchestrator/teams/collab_rooms/learning_rooms 블록을 그대로 반환."""
    with open(path or TEAMS_JSON, encoding="utf-8") as f:
        return json.load(f)


def _team_folder(team_label):
    """팀 폴더 상대경로 조각(10_Teams/<안전슬러그>). 한글 팀명 보존."""
    return (S.DIR_TEAMS, S.safe_slug(team_label))


def ensure_team_dirs(data):
    """teams.json 의 모든 팀에 대해 10_Teams/<team>/ 폴더를 보장(idempotent). 생성 폴더 경로 리스트 반환."""
    W.ensure_vault()
    created = []
    for t in data.get("teams", []):
        label = t.get("label") or t.get("id") or "team"
        d = S.vault_path(*_team_folder(label))
        os.makedirs(d, exist_ok=True)
        created.append(d)
    return created


def _moc_lines(data):
    """teams.json -> MOC 본문 라인. 오케스트레이터·팀·협업방·학습방을 [[링크]]로 연결."""
    orch = data.get("orchestrator", {})
    lines = ["# 조직 지도 (Map of Content)", "",
             "> 자동 생성(vault_org.sync_org). 직접 편집하지 말 것 — teams.json 이 진실원천.", "",
             "## 비서실 / 오케스트레이션", ""]
    if orch:
        name = orch.get("role", "오케스트레이터")
        lines.append(f"- [[{name}]] ({orch.get('title', '')}) — 상향·하향 게이트")
        lines.append(f"  - 브리핑: {orch.get('briefing_channel', '')} / 사람 사장: {orch.get('principal', '')}")
    lines += ["", "## 팀", ""]
    for t in data.get("teams", []):
        label = t.get("label", t.get("id", ""))
        agent = t.get("agent", "")
        folder = "/".join(_team_folder(label))
        lines.append(f"- [[{label}]] — 담당 [[{agent}]] · 보고라인 {t.get('report_channel', '')}")
        lines.append(f"  - 폴더: `{folder}/` · 에스컬레이션 -> {t.get('escalate_to', '')}")
    rooms = data.get("collab_rooms", [])
    if rooms:
        lines += ["", "## 협업방", ""]
        for r in rooms:
            parts = ", ".join(f"[[{p}]]" for p in r.get("participants", []))
            lines.append(f"- [[{r.get('label', r.get('id', ''))}]] (리드 [[{r.get('lead', '')}]]) — {parts}")
    learn = data.get("learning_rooms", [])
    if learn:
        lines += ["", "## 학습방(역할별 영구 누적)", ""]
        for lr in learn:
            lines.append(f"- [[{lr.get('label', lr.get('id', ''))}]] — owner `{lr.get('owner', '')}`")
    lines += ["", "## 진실원천 폴더", "",
              f"- `{S.DIR_CEO}/` CEO 판단 · `{S.DIR_REPORTS}/` 보고(연/월) · "
              f"`{S.DIR_FEEDBACK}/` 피드백 · `{S.DIR_SYSTEM}/` 정책", ""]
    return lines


def write_moc(data):
    """_index/HOME.md MOC 를 frontmatter 포함 노트로 원자적 기록. 반환: 절대경로."""
    W.ensure_vault()
    fm = S.default_frontmatter("policy", role="system", team="",
                               tags=["moc", "org-map"], note_id="HOME")
    errors = S.validate_frontmatter(fm)
    if errors:
        raise ValueError(f"MOC frontmatter 검증 실패: {errors}")
    body = "\n".join(_moc_lines(data))
    content = S.dump_frontmatter(fm) + "\n" + body + "\n"
    path = S.vault_path(S.DIR_INDEX, "HOME.md")
    W._atomic_write(path, content)
    return path


def write_note_template():
    """_templates/note.md — Obsidian Templates 플러그인용 빈 양식(스키마 자기문서화)."""
    W.ensure_vault()
    tpl = (
        "---\n"
        "id: \n"
        "type: report\n"
        "role: \n"
        "team: \n"
        "date: \n"
        "tags: []\n"
        "links: []\n"
        "---\n\n"
        "**한 줄 결론**\n\n본문\n"
    )
    path = S.vault_path(S.DIR_TEMPLATES, "note.md")
    W._atomic_write(path, tpl)
    return path


def sync_org(path=None):
    """teams.json -> Vault 구조 1회 동기화(폴더 + MOC + 템플릿). 통계 dict 반환.
    팀 추가 시 이 함수만 다시 돌리면 되므로 파이썬 수정이 필요 없다."""
    data = load_teams(path)
    dirs = ensure_team_dirs(data)
    moc = write_moc(data)
    tpl = write_note_template()
    return {"team_dirs": len(dirs), "moc": moc, "template": tpl,
            "teams": [t.get("label") for t in data.get("teams", [])]}


if __name__ == "__main__":
    print(sync_org())
