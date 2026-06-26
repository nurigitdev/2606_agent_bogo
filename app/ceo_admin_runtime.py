"""
에이전트 정의 개조 파이프라인 — '에이전트 관리자' 봇(역할별 학습방 기반).

각 역할의 전용 학습방(teams.json learning_rooms)에서 그 방의 owner(=담당 에이전트)에
대한 정의 변경을 자연어로 지시하면:
  1. 의도 파싱(LLM): 무슨 변경(대상 role 은 그 방의 owner 로 강제 — 방 격리)
  2. 해당 agents/<owner>.md 를 LLM으로 재작성 → unified diff 미리보기를 그 방에 게시 (pending)
  3. "적용" → md 파일 반영 + git commit(한국어) + 운영본 동기화 + 해당 역할 데몬 리로드
  4. "반려/취소" → 그 방의 pending 폐기

방 격리(중요): 각 학습방에서는 그 방 owner 의 정의만 수정할 수 있다. 한 방의 '적용/반려'는
그 방 owner 슬롯에만 작용한다(PENDING 은 role 별 dict). 다른 에이전트를 고치려면 그
에이전트의 학습방에서 지시해야 한다.

안전장치(하드 게이트):
  - 수정 가능 경로는 agents/*.md (페르소나) 뿐. 그 외 파일·시스템 명령 절대 불가.
  - 적용 전 반드시 diff 승인 게이트(미리보기 없이는 절대 파일을 건드리지 않음).
  - frontmatter 필수필드는 보존(LLM이 깨면 적용 거부). 모든 변경은 git 추적.
  - 봇은 자기 메시지에 반응하지 않음(메아리 차단).

기존 런타임과 코드 재사용: mm_client(LLM/Mattermost), agent_schema(파싱·검증).
실행: <venv>/python ceo_admin_runtime.py
인증: OpenRouter 키(.env OPENROUTER_API_KEY) + 기존 봇 토큰(nk_config.json). 신규 API 키 요구 없음.

배포 모델(중요):
  - source of truth = Desktop 원본 git repo(app/). 모든 .md 쓰기·git 커밋은 항상 여기에 한다.
  - macOS launchd 는 ASCII 미러(~/.bogo-bin/app)에서 데몬을 실행한다. 미러는 .git 이 제외된
    rsync 복사본이라 git/파일쓰기 대상이 될 수 없다(써도 다음 sync 에 소실).
  - 따라서 원본 repo 경로를 BOGO_REPO 환경변수로 주입받아 거기에 쓰고, 거기서 git 커밋한 뒤
    원본→미러 단방향 sync + 데몬 리로드를 한다. 미설정·부재·비 repo 면 즉시 에러 중단(거짓 양성 금지).
  - Linux/Windows 는 인플레이스 실행이라 미러=원본이며 BOGO_REPO 가 곧 실행 경로가 된다.
"""
import asyncio
import difflib
import json
import os
import subprocess
import sys

import websockets

import agent_schema as A
import mm_client as C

HERE = os.path.dirname(os.path.abspath(__file__))


def _resolve_repo():
    """파일 쓰기·git 커밋의 단일 진실 경로(Desktop 원본 repo)를 확정한다.

    우선순위: 환경변수 BOGO_REPO > HERE(인플레이스 실행). 어느 쪽이든 실제 git
    워크트리(.git 존재)여야 하며, 아니면 즉시 중단한다. launchd 미러(~/.bogo-bin/app)
    는 .git 이 없으므로 여기서 자연히 걸러져, 미러에 쓰고 '적용 완료'로 거짓 보고하는
    사태를 원천 차단한다.
    """
    cand = os.environ.get("BOGO_REPO", "").strip() or HERE
    repo = os.path.realpath(os.path.expanduser(cand))
    if not os.path.isdir(repo):
        sys.stderr.write(
            f"치명: 원본 repo 경로가 존재하지 않습니다: {repo}\n"
            "BOGO_REPO 환경변수에 Desktop 원본 app/ 절대경로를 설정하세요.\n")
        raise SystemExit(3)
    # git 워크트리 멤버십을 git 자체로 판별한다. app/ 이 git repo 의 하위 디렉터리이고
    # .git 은 상위(프로젝트 루트)에 있을 수 있으므로, .git 의 직접 존재가 아니라
    # 'is-inside-work-tree' 로 확인해야 한다(rsync 미러는 .git 추적 자체가 없어 걸러진다).
    try:
        inside = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True)
    except FileNotFoundError:
        sys.stderr.write("치명: git 실행 파일을 찾지 못했습니다(PATH 확인).\n")
        raise SystemExit(3)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        sys.stderr.write(
            f"치명: 원본 repo 가 git 워크트리가 아닙니다: {repo}\n"
            "이 경로는 rsync 미러일 가능성이 큽니다. launchd 데몬은 BOGO_REPO 로 "
            "Desktop 원본 repo 절대경로를 받아야 합니다(미러에 쓰면 다음 sync 에 소실됨).\n")
        raise SystemExit(3)
    if not os.path.isdir(os.path.join(repo, "agents")):
        sys.stderr.write(f"치명: 원본 repo 에 agents/ 디렉터리가 없습니다: {repo}\n")
        raise SystemExit(3)
    return repo


# 모든 md 쓰기·git·diff 의 기준(원본 repo). HERE(미러일 수 있음)와 분리한다.
REPO = _resolve_repo()
REPO_AGENTS = os.path.join(REPO, "agents")
# 이 파이프라인 전용 봇 토큰: 기존 박민철(nk) 봇을 재사용. nk 봇은 system_admin 이라
# REST 로 자기 자신을 학습방 멤버로 추가할 수 있다(아래 ensure_bot_membership 참조).
ADMIN_CONFIG = "nk"

CH = A.load_channels()
ID2NAME = {v: k for k, v in CH.items()}
TEAMS = A.load_teams()

ROLES = A.load_roles()
ROLE_BY_NAME = {m["name"]: r for r, m in ROLES.items()}  # 박민철 -> orchestrator
ROLE_BY_USER = {m["username"]: r for r, m in ROLES.items()}  # minchul -> orchestrator

# 수신할 학습방 맵: {채널명 -> owner role}. teams.json learning_rooms 에서, owner 가 실재
# 역할이고 channel 이 channels.json 에 등록된 것만 채택한다. 단일 'CEO-에이전트관리' 방
# 의존을 폐지하고, 역할별 학습방 N개를 동시 수신한다(방마다 그 owner 정의만 개조).
LEARN_ROOMS = {}  # channel name -> owner role
for _room in A.load_learning_rooms(TEAMS):
    _ch, _owner = _room.get("channel"), _room.get("owner")
    if _ch in CH and _owner in ROLES:
        LEARN_ROOMS[_ch] = _owner
if not LEARN_ROOMS:
    sys.stderr.write(
        "수신할 학습방이 없습니다. teams.json 의 learning_rooms 와 channels.json 정합성을 "
        "확인하세요(owner 가 실재 역할이고 channel 이 channels.json 에 등록돼야 함).\n")
    raise SystemExit(2)
# CID 집합과 역맵(CID -> (채널명, owner role)). websocket 수신 필터·라우팅에 쓴다.
LEARN_CIDS = {CH[ch] for ch in LEARN_ROOMS}
CID_TO_ROOM = {CH[ch]: (ch, owner) for ch, owner in LEARN_ROOMS.items()}

CFG = json.load(open(os.path.join(HERE, f"{ADMIN_CONFIG}_config.json"), encoding="utf-8"))
BOT_ID = CFG["bot_id"]
LLM = C.load_llm_config()
mm = C.MM(CFG["bot_token"])

# 승인 대기 중인 변경안 — 방(owner role)별 dict. 한 방의 '적용/반려'는 그 방 owner
# 슬롯에만 작용해, 다른 방 대기 건이 섞이지 않는다(방 격리). 같은 방에 새 변경 지시가
# 오면 그 방 직전 대기 건만 덮어쓰여 폐기된다(미적용이므로 안전).
# 형태: PENDING[role] = {"role","path","new_text","diff","summary"}.
PENDING = {}


def ensure_bot_membership():
    """nk 봇(BOT_ID)을 모든 학습방 채널의 멤버로 보장한다(멱등, 기동 시 1회).

    ceo_admin 이 nk 봇 토큰으로 각 학습방에 게시하려면 그 봇이 채널 멤버여야 한다.
    nk 봇은 system_admin 이라 자기 자신을 추가할 수 있다. 이미 멤버면 무해, 실패해도
    기동은 계속하고 경고만 남긴다(개별 방 게시 시점에 다시 시도될 수 있음).
    """
    for ch, cid in ((c, CH[c]) for c in LEARN_ROOMS):
        try:
            mm.add_member(cid, BOT_ID)
        except Exception as e:
            print(f"[CEO관리봇] 학습방 멤버십 보장 실패({ch}): {type(e).__name__} — 게시 시 재시도")


def resolve_role(token):
    """자연어 토큰에서 대상 role 식별(이름/username/role 키 모두 허용)."""
    token = (token or "").strip()
    if token in ROLES:
        return token
    if token in ROLE_BY_NAME:
        return ROLE_BY_NAME[token]
    if token in ROLE_BY_USER:
        return ROLE_BY_USER[token]
    # 부분 일치(이름 포함)
    for name, role in ROLE_BY_NAME.items():
        if name and name in token:
            return role
    return None


def _llm(messages, json_mode):
    try:
        return C.call_llm(messages, LLM["model"], LLM["key"], LLM["base_url"],
                          max_tokens=2000, temperature=0.3, json_mode=json_mode)
    except Exception:
        return C.call_llm(messages, LLM["fallback"], LLM["key"], LLM["base_url"],
                          max_tokens=2000, temperature=0.3, json_mode=json_mode)


def parse_intent(text):
    """CEO 자연어 → {action, target, instruction}. action=update|apply|reject|help|none.

    적용/반려 단독 명령 판별은 agent_schema 공용 함수(단일 출처)로 위임한다.
    update 의 대상(target)은 추출하되, 방 격리상 handle()에서 owner_role 로 강제되므로
    참고용이다."""
    t = text.strip()
    # 적용/반려는 짧은 단독 응답일 때만 명령으로 본다(LLM 불필요, 오판 위험 최소화).
    # 직전에 PENDING 이 없는데 '적용' 만 와도 handle()에서 안내 처리하므로 여기선 의도만 분류.
    if A.is_admin_short_command(t, A.ADMIN_APPLY_WORDS):
        return {"action": "apply"}
    if A.is_admin_short_command(t, A.ADMIN_REJECT_WORDS):
        return {"action": "reject"}
    if t in ("도움말", "help", "?", "사용법"):
        return {"action": "help"}
    roster = "\n".join(f"- role={r} / 이름={m['name']} / username={m['username']}" for r, m in ROLES.items())
    sysmsg = (
        "너는 사내 에이전트 정의 관리 봇의 의도 분석기다. CEO의 한국어 지시에서 "
        "(1)어느 에이전트를 (2)어떻게 바꾸려는지 추출한다.\n"
        f"[등록된 에이전트]\n{roster}\n"
        "반드시 JSON 하나만 출력: "
        '{"action":"update|none","target":"<role 또는 이름 또는 username>","instruction":"<변경 지시 요약>"}. '
        "에이전트 정의 변경 의도가 아니면 action=none."
    )
    try:
        out = _llm([{"role": "system", "content": sysmsg}, {"role": "user", "content": t}], json_mode=True)
        d = json.loads(C.strip_fence(out))
        if d.get("action") == "update" and d.get("target"):
            return d
    except Exception:
        pass
    return {"action": "none"}


def rewrite_md(role, instruction):
    """해당 role 의 md 를 instruction 대로 재작성. (frontmatter 보존, 본문만 변경 유도)

    읽기·쓰기 모두 원본 repo(REPO) 기준이다. 미러(HERE)가 아니라 원본을 편집해야
    git 추적·영속이 보장된다.
    """
    path = os.path.join(REPO_AGENTS, f"{role}.md")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"원본 repo 에 {role}.md 가 없습니다: {path}")
    original = open(path, encoding="utf-8").read()
    sysmsg = (
        "너는 사내 멀티에이전트 시스템의 에이전트 정의(.md) 편집기다. 아래 원본 md 를 CEO 지시대로 수정한다.\n"
        "엄수 규칙:\n"
        "- frontmatter(--- 사이 블록)의 필수 키(name, username, config, primary, channels)는 절대 삭제·변경하지 않는다.\n"
        "- 페르소나 본문만 지시에 맞게 고친다. 한국어로 작성한다.\n"
        "- 공통 규칙(보고 포맷·언어·완료조건 등)은 별도 파일에서 상속되므로 본문에 중복 서술하지 않는다.\n"
        "- 출력은 수정된 md 전체 텍스트만. 코드펜스·설명·머리말 금지."
    )
    user = f"[CEO 지시]\n{instruction}\n\n[원본 md]\n{original}"
    out = _llm([{"role": "system", "content": sysmsg}, {"role": "user", "content": user}], json_mode=False)
    new_text = out.strip()
    # 코드펜스로 감쌌으면 제거
    if new_text.startswith("```"):
        new_text = new_text.split("\n", 1)[1] if "\n" in new_text else new_text
        if new_text.rstrip().endswith("```"):
            new_text = new_text.rstrip()[:-3].rstrip()
    if not new_text.endswith("\n"):
        new_text += "\n"
    return path, original, new_text


def frontmatter_ok(role, new_text):
    """재작성 결과가 필수 frontmatter·채널 정합성을 지키는지 검증."""
    # new_text 를 임시 파싱
    parts = new_text.split("---", 2)
    if len(parts) < 3:
        return ["frontmatter 블록(--- ... ---)이 사라짐"]
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        meta[k] = [x.strip() for x in v.split(",") if x.strip()] if k in A.LIST_FIELDS else v
    meta["prompt"] = parts[2].strip()
    return A.validate_roles({role: meta}, set(CH))


def make_diff(original, new_text, path):
    rel = os.path.relpath(path, REPO)
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))


def git(args, check=True):
    # 항상 원본 repo(REPO) 컨텍스트에서 실행한다 — 미러는 .git 이 없어 fatal 이 난다.
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, check=check)


def apply_change(role):
    """그 방(role)의 승인된 대기 변경을 원본 repo 에 반영 + git commit + 미러 sync + 리로드.

    실제 적용을 검증(커밋 생성·파일 내용 일치)한 뒤에만 '적용 완료'를 반환한다.
    검증 실패 시 PENDING[role] 을 유지해 재시도 가능하게 두고 실패 사유를 반환한다(거짓 양성 금지).
    """
    p = PENDING.get(role)
    if not p:
        return "적용할 대기 변경이 없습니다."
    path = p["path"]
    # 하드 게이트: 원본 repo 의 agents/*.md 경로만 허용.
    agents_real = os.path.realpath(REPO_AGENTS)
    if not os.path.realpath(path).startswith(agents_real + os.sep):
        return "거부: 원본 repo 의 agents/ 외 경로는 수정할 수 없습니다."

    # 1) 원본 파일 쓰기
    with open(path, "w", encoding="utf-8") as f:
        f.write(p["new_text"])
    # 쓰기 검증: 실제로 의도한 내용이 들어갔는가
    if open(path, encoding="utf-8").read() != p["new_text"]:
        return "⚠️ 적용 실패: 파일 쓰기 후 내용이 일치하지 않습니다. 변경을 유지하니 다시 시도하세요."

    rel = os.path.relpath(path, REPO)
    msg = f"에이전트 정의 업데이트: {ROLES[role]['name']}({role}) — {p['summary']}"

    # 2) git add/commit — 비 repo(미러)면 _resolve_repo 가 이미 막았지만 방어적으로 처리.
    add = git(["add", rel], check=False)
    if add.returncode != 0:
        return f"⚠️ 적용 실패: git add 오류({add.stderr.strip()[:140]}). 원본 repo 가 맞는지 확인하세요."
    commit = git(["commit", "-m", msg], check=False)
    if commit.returncode != 0:
        # nothing-to-commit(이미 같은 내용) 등은 경고만, 그 외는 실패로 본다.
        if "nothing to commit" in (commit.stdout + commit.stderr):
            commit_short = "(변경 없음 — 커밋 생략)"
        else:
            return f"⚠️ 적용 실패: git commit 오류({commit.stderr.strip()[:140]})."
    else:
        commit_short = git(["rev-parse", "--short", "HEAD"], check=False).stdout.strip()

    # 3) 적용 영속 검증: HEAD 가 가리키는 파일 내용이 새 텍스트와 같은지 확인.
    show = git(["show", f"HEAD:{rel}"], check=False)
    if show.returncode == 0 and show.stdout.rstrip("\n") != p["new_text"].rstrip("\n"):
        # 커밋은 됐지만 내용이 어긋나면 사용자에게 명확히 경고(거짓 양성 금지).
        commit_short += " ⚠️커밋내용 불일치 확인필요"

    # 4) 원본→미러 단방향 sync + 데몬 리로드(실패해도 커밋은 이미 영속).
    reload_note = _sync_and_reload(role)

    PENDING.pop(role, None)  # 그 방 슬롯만 정리(다른 방 대기 건 보존).
    # 메모리상의 ROLES 갱신(원본 파일 기준).
    ROLES[role] = A.parse_md(path)
    return f"적용 완료 ({commit_short}). {reload_note}"


def _sync_and_reload(role):
    """원본 repo → 미러(~/.bogo-bin/app) 단방향 동기화 후 해당 역할 데몬 리로드.

    핵심: SRC 를 항상 원본 REPO 로 고정한다. launchd 컨텍스트에서 sync 스크립트의
    SRC 가 미러 자신이 되면 self-copy(no-op)가 되어 변경이 반영되지 않으므로,
    여기서 BOGO_REPO=REPO 를 명시 주입해 단방향을 강제한다.
    Linux/Windows 는 인플레이스라 미러가 없으면 이 단계는 자연히 생략된다.
    """
    notes = []
    app = os.path.join(os.path.expanduser("~"), ".bogo-bin", "app")
    sync = os.path.join(REPO, "launchd", "sync_app.sh")
    # 미러가 실제로 존재하고 원본과 다른 경로일 때만 sync 한다(인플레이스 self-sync 차단).
    if os.path.isdir(app) and os.path.realpath(app) != os.path.realpath(REPO) and os.path.exists(sync):
        env = dict(os.environ, BOGO_REPO=REPO)
        r = subprocess.run([sync], capture_output=True, text=True, env=env)
        notes.append("미러 동기화" + ("" if r.returncode == 0 else f" 실패({r.stderr.strip()[:80]})"))
    label = f"com.bogo.{role}"
    try:
        uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
        r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                           capture_output=True, text=True)
        notes.append(f"{label} 리로드" + ("" if r.returncode == 0 else " 미적용(데몬 미등록)"))
    except Exception:
        notes.append(f"{label} 리로드 시도 실패")
    return " / ".join(notes) if notes else "리로드 대상 없음"


def handle(text, room_channel, owner_role):
    """학습방 메시지 1건 처리 → 그 방에 게시할 응답 문자열(없으면 None).

    방 격리: 이 방에서는 owner_role 의 정의만 수정한다. apply/reject 는 PENDING[owner_role]
    슬롯에만 작용한다. update 는 parse_intent 가 다른 대상을 뽑아도 owner_role 로 강제하고,
    어긋난 대상을 가리켰으면 안내 1줄을 덧붙인다(사용성 — 차단 대신 owner 로 진행).
    """
    owner_name = ROLES[owner_role]["name"]
    intent = parse_intent(text)
    act = intent.get("action")

    if act == "help":
        return ("📣 **에이전트 관리 봇 사용법**\n"
                f"- 이 방({room_channel})에서는 **{owner_name}** 의 정의만 수정합니다(방 격리).\n"
                f"- 변경: 예) `{owner_name} 보고를 3줄로 줄여` → 수정안 diff 미리보기를 보여드립니다.\n"
                "- 적용: 미리보기 후 `적용`(또는 반영/승인) → 파일 반영 + git 커밋 + 데몬 리로드.\n"
                "- 반려: `반려`(또는 취소/폐기) → 대기 변경 폐기.\n"
                "안전장치: agents/*.md(페르소나)만 수정합니다. 다른 파일·시스템 명령은 불가합니다.")

    if act == "apply":
        if not PENDING.get(owner_role):
            return "적용할 대기 변경이 없습니다. 먼저 변경을 지시해 미리보기를 받으세요."
        return "✅ **적용 결과**\n- " + apply_change(owner_role)

    if act == "reject":
        if not PENDING.get(owner_role):
            return "대기 중인 변경이 없습니다."
        PENDING.pop(owner_role, None)
        return f"📣 대기 변경을 폐기했습니다: {owner_name}"

    if act == "update":
        # 방 격리: 대상은 무조건 이 방의 owner_role. parse_intent 가 다른 대상을 뽑았고
        # 그게 owner 와 다른 실재 역할로 resolve 되면 안내 1줄을 덧붙이되, owner 로 진행한다.
        note = ""
        picked = resolve_role(intent.get("target"))
        if picked and picked != owner_role:
            note = (f"ℹ️ 이 방({room_channel})에서는 {owner_name}만 수정할 수 있습니다. "
                    f"다른 에이전트는 그 에이전트의 학습방에서 수정하세요. → {owner_name} 기준으로 진행합니다.\n")
        role = owner_role
        instruction = intent.get("instruction") or text
        try:
            path, original, new_text = rewrite_md(role, instruction)
        except Exception as e:
            return f"⚠️ 수정안 생성 중 오류: {str(e)[:160]}"
        if new_text.strip() == original.strip():
            return f"{note}변경 사항이 없습니다({owner_name}). 지시를 더 구체적으로 주세요."
        errs = frontmatter_ok(role, new_text)
        if errs:
            return note + "⚠️ 수정안이 정의 규칙을 위반해 적용을 막았습니다:\n- " + "\n- ".join(errs)
        diff = make_diff(original, new_text, path)
        if not diff:
            return f"{note}변경 사항이 없습니다."
        # 이 방 슬롯: 직전 대기 건이 있으면 덮어쓰며 폐기됨을 알린다(누적 일괄적용 방지).
        prev = ("(이 방의 이전 대기 건은 폐기됩니다)\n" if PENDING.get(role) else "")
        PENDING[role] = {"role": role, "path": path, "new_text": new_text,
                         "diff": diff, "summary": instruction[:80]}
        shown = diff if len(diff) < 3000 else diff[:3000] + "\n…(생략)"
        return (f"📌 **{owner_name}({role}) 수정안 미리보기**\n{note}{prev}지시: {instruction[:120]}\n\n"
                f"```diff\n{shown}\n```\n"
                "적용하려면 `적용`, 취소하려면 `반려`라고 답해 주세요.")

    return None  # 관리 의도 아님 → 침묵


def speaker_name(uid):
    try:
        u = mm.user(uid)
        return u.get("nickname") or u.get("username") or "사람"
    except Exception:
        return "사람"


async def run():
    # open_timeout: MM 부재 시 connect 무한 대기 방지. ping_*: 좀비 연결 감지로 백오프
    # 재접속 루프(run_forever)가 동작하게 한다.
    # 기동 시 봇을 각 학습방 멤버로 보장(멱등) — 그래야 nk 봇 토큰으로 게시 가능.
    ensure_bot_membership()
    async with websockets.connect("ws://127.0.0.1:8065/api/v4/websocket",
                                  open_timeout=20, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"seq": 1, "action": "authentication_challenge",
                                  "data": {"token": CFG["bot_token"]}}))
        print(f"에이전트 개조 봇 가동 — 학습방:{', '.join(LEARN_ROOMS)} 모델:{LLM['model']}")
        async for raw in ws:
            ev = json.loads(raw)
            if ev.get("event") != "posted":
                continue
            p = json.loads(ev["data"]["post"])
            if p.get("user_id") == BOT_ID:        # 메아리 차단
                continue
            cid = p.get("channel_id")
            if cid not in LEARN_CIDS:             # 학습방들만 수신
                continue
            room_channel, owner_role = CID_TO_ROOM[cid]
            text = p.get("message", "")
            if not text.strip():
                continue
            try:
                reply = await asyncio.to_thread(handle, text, room_channel, owner_role)
            except Exception as e:
                print(f"HANDLE-FAIL [{room_channel}]: {e}")
                try:
                    mm.post(cid, "⚠️ 처리 중 오류가 발생했습니다. 다시 시도해 주세요.")
                except Exception:
                    pass
                continue
            if reply:
                try:
                    mm.post(cid, reply)
                except Exception as e:
                    print("post-err", e)


async def run_forever():
    # MM 재접속 내성: MM 일시 정지/WS 끊김에도 프로세스가 죽지 않고 지수 백오프로 재연결.
    # launchd KeepAlive 와 충돌하지 않는다(정상 운영 중 이 루프가 프로세스를 살려 둠).
    backoff = 2
    backoff_max = 60
    while True:
        try:
            await run()
            print("[CEO관리봇] WS 종료됨 — 재접속 시도.")
            backoff = 2
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
            print(f"[CEO관리봇] MM 연결 실패/끊김: {type(e).__name__}: {e} — {backoff}s 후 재접속.")
        except Exception as e:
            print(f"[CEO관리봇] 예기치 못한 오류: {type(e).__name__}: {e} — {backoff}s 후 재접속.")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, backoff_max)


if __name__ == "__main__":
    asyncio.run(run_forever())
