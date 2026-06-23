"""
CEO 에이전트 업데이트 파이프라인 — '에이전트 관리자' 봇.

CEO가 전용 방('CEO-에이전트관리')에서 자연어로 에이전트 정의 변경을 지시하면:
  1. 의도 파싱(LLM): 어떤 에이전트(role) + 무슨 변경
  2. 해당 agents/<role>.md 를 LLM으로 재작성 → unified diff 미리보기를 방에 게시 (pending)
  3. CEO "적용" → md 파일 반영 + git commit(한국어) + 운영본 동기화 + 해당 역할 데몬 리로드
  4. CEO "반려/취소" → pending 폐기

안전장치(하드 게이트):
  - 수정 가능 경로는 agents/*.md (페르소나) 뿐. 그 외 파일·시스템 명령 절대 불가.
  - 적용 전 반드시 diff 승인 게이트(미리보기 없이는 절대 파일을 건드리지 않음).
  - frontmatter 필수필드는 보존(LLM이 깨면 적용 거부). 모든 변경은 git 추적.
  - 봇은 자기 메시지에 반응하지 않음(메아리 차단).

기존 런타임과 코드 재사용: mm_client(LLM/Mattermost), agent_schema(파싱·검증).
실행: <venv>/python ceo_admin_runtime.py
인증: OpenRouter 키(.env OPENROUTER_API_KEY) + 기존 봇 토큰(nk_config.json). 신규 API 키 요구 없음.
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
ADMIN_CHANNEL = "CEO-에이전트관리"
# 이 파이프라인 전용 봇 토큰: 기존 박민철(nk) 봇을 재사용(이미 채널 멤버, 별도 봇 계정 생성 권한 불요).
ADMIN_CONFIG = "nk"

CH = A.load_channels()
ID2NAME = {v: k for k, v in CH.items()}
if ADMIN_CHANNEL not in CH:
    sys.stderr.write(f"channels.json 에 '{ADMIN_CHANNEL}' 채널이 없습니다. 먼저 채널을 생성·등록하세요.\n")
    raise SystemExit(2)
ADMIN_CID = CH[ADMIN_CHANNEL]

CFG = json.load(open(os.path.join(HERE, f"{ADMIN_CONFIG}_config.json"), encoding="utf-8"))
BOT_ID = CFG["bot_id"]
LLM = C.load_llm_config()
mm = C.MM(CFG["bot_token"])

ROLES = A.load_roles()
ROLE_BY_NAME = {m["name"]: r for r, m in ROLES.items()}  # 박민철 -> orchestrator
ROLE_BY_USER = {m["username"]: r for r, m in ROLES.items()}  # minchul -> orchestrator

# 승인 대기 중인 변경안: role -> {"path","new_text","diff","summary"}
PENDING = {}


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
    """CEO 자연어 → {action, target, instruction}. action=update|apply|reject|help|none."""
    t = text.strip()
    # 적용/반려는 자연어 키워드로 직접 판정(LLM 불필요, 오판 위험 최소화)
    if any(k in t for k in ("적용", "반영", "승인", "확정")):
        return {"action": "apply"}
    if any(k in t for k in ("반려", "취소", "폐기", "거부", "안 해", "안해")):
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
    """해당 role 의 md 를 instruction 대로 재작성. (frontmatter 보존, 본문만 변경 유도)"""
    path = os.path.join(A.AGENTS_DIR, f"{role}.md")
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
    rel = os.path.relpath(path, HERE)
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))


def git(args, check=True):
    return subprocess.run(["git", "-C", HERE, *args], capture_output=True, text=True, check=check)


def apply_change(role):
    """승인된 변경을 파일 반영 + git commit + 운영본 동기화 + 데몬 리로드."""
    p = PENDING.get(role)
    if not p:
        return "적용할 대기 변경이 없습니다."
    path = p["path"]
    # 하드 게이트: agents/*.md 경로만 허용
    agents_real = os.path.realpath(A.AGENTS_DIR)
    if not os.path.realpath(path).startswith(agents_real + os.sep):
        return "거부: agents/ 외 경로는 수정할 수 없습니다."
    open(path, "w", encoding="utf-8").write(p["new_text"])
    rel = os.path.relpath(path, HERE)
    msg = f"에이전트 정의 업데이트: {ROLES[role]['name']}({role}) — {p['summary']}"
    try:
        git(["add", rel])
        git(["commit", "-m", msg])
        commit_line = git(["rev-parse", "--short", "HEAD"], check=False).stdout.strip()
    except subprocess.CalledProcessError as e:
        commit_line = f"(git 커밋 경고: {e.stderr.strip()[:120]})"
    # 운영본 동기화 + 해당 역할 리로드(있으면). 실패해도 파일·커밋은 유지.
    reload_note = _sync_and_reload(role)
    del PENDING[role]
    # 메모리상의 ROLES 갱신
    ROLES[role] = A.parse_md(path)
    return f"적용 완료 ({commit_line}). {reload_note}"


def _sync_and_reload(role):
    sync = os.path.join(HERE, "launchd", "sync_app.sh")
    notes = []
    if os.path.exists(sync):
        r = subprocess.run([sync], capture_output=True, text=True)
        notes.append("운영본 동기화" + ("" if r.returncode == 0 else " 실패"))
    label = f"com.hermes.{role}"
    try:
        uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
        r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                           capture_output=True, text=True)
        notes.append(f"{label} 리로드" + ("" if r.returncode == 0 else " 미적용(데몬 미등록)"))
    except Exception:
        notes.append(f"{label} 리로드 시도 실패")
    return " / ".join(notes) if notes else "리로드 대상 없음"


def handle(text):
    """CEO 메시지 1건 처리 → 방에 게시할 응답 문자열(없으면 None)."""
    intent = parse_intent(text)
    act = intent.get("action")

    if act == "help":
        return ("📣 **에이전트 관리 봇 사용법**\n"
                "- 변경: 예) `박민철 보고를 3줄로 줄여` → 수정안 diff 미리보기를 보여드립니다.\n"
                "- 적용: 미리보기 후 `적용`(또는 반영/승인) → 파일 반영 + git 커밋 + 데몬 리로드.\n"
                "- 반려: `반려`(또는 취소/폐기) → 대기 변경 폐기.\n"
                "안전장치: agents/*.md(페르소나)만 수정합니다. 다른 파일·시스템 명령은 불가합니다.")

    if act == "apply":
        if not PENDING:
            return "적용할 대기 변경이 없습니다. 먼저 변경을 지시해 미리보기를 받으세요."
        results = [apply_change(r) for r in list(PENDING)]
        return "✅ **적용 결과**\n" + "\n".join(f"- {x}" for x in results)

    if act == "reject":
        if not PENDING:
            return "대기 중인 변경이 없습니다."
        targets = ", ".join(ROLES[r]["name"] for r in PENDING)
        PENDING.clear()
        return f"📣 대기 변경을 폐기했습니다: {targets}"

    if act == "update":
        role = resolve_role(intent.get("target"))
        if not role:
            avail = ", ".join(f"{m['name']}" for m in ROLES.values())
            return f"⚠️ 대상 에이전트를 못 찾았습니다. 등록된 에이전트: {avail}"
        instruction = intent.get("instruction") or text
        try:
            path, original, new_text = rewrite_md(role, instruction)
        except Exception as e:
            return f"⚠️ 수정안 생성 중 오류: {str(e)[:160]}"
        if new_text.strip() == original.strip():
            return f"변경 사항이 없습니다({ROLES[role]['name']}). 지시를 더 구체적으로 주세요."
        errs = frontmatter_ok(role, new_text)
        if errs:
            return "⚠️ 수정안이 정의 규칙을 위반해 적용을 막았습니다:\n- " + "\n- ".join(errs)
        diff = make_diff(original, new_text, path)
        if not diff:
            return "변경 사항이 없습니다."
        PENDING[role] = {"path": path, "new_text": new_text, "diff": diff, "summary": instruction[:80]}
        shown = diff if len(diff) < 3000 else diff[:3000] + "\n…(생략)"
        return (f"📌 **{ROLES[role]['name']}({role}) 수정안 미리보기**\n지시: {instruction[:120]}\n\n"
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
    async with websockets.connect("ws://localhost:8065/api/v4/websocket") as ws:
        await ws.send(json.dumps({"seq": 1, "action": "authentication_challenge",
                                  "data": {"token": CFG["bot_token"]}}))
        print(f"에이전트 관리 봇 가동 — 방:{ADMIN_CHANNEL} 모델:{LLM['model']}")
        async for raw in ws:
            ev = json.loads(raw)
            if ev.get("event") != "posted":
                continue
            p = json.loads(ev["data"]["post"])
            if p.get("user_id") == BOT_ID:        # 메아리 차단
                continue
            if p.get("channel_id") != ADMIN_CID:  # 전용 방만 수신
                continue
            text = p.get("message", "")
            if not text.strip():
                continue
            try:
                reply = await asyncio.to_thread(handle, text)
            except Exception as e:
                print(f"HANDLE-FAIL: {e}")
                try:
                    mm.post(ADMIN_CID, "⚠️ 처리 중 오류가 발생했습니다. 다시 시도해 주세요.")
                except Exception:
                    pass
                continue
            if reply:
                try:
                    mm.post(ADMIN_CID, reply)
                except Exception as e:
                    print("post-err", e)


if __name__ == "__main__":
    asyncio.run(run())
