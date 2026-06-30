"""
공용 저수준 클라이언트 — OpenRouter LLM + Mattermost REST.

bogo_runtime.py(역할 에이전트)와 ceo_admin_runtime.py(CEO 업데이트 파이프라인)가 공유한다.
부작용(sys.argv 파싱 등) 없이 import 가능하도록 순수 함수/클래스만 둔다.
"""
import json
import os
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# NOTE: localhost(=::1 우선 해석) 대신 127.0.0.1 강제.
# colima ssh 포트포워드가 IPv4(*:8065)만 바인딩해 ::1 로는 Errno 61 refused 가 난다.
MM_BASE = "http://127.0.0.1:8065/api/v4"


# ── LLM 백엔드 스위치(이식성 핵심) ────────────────────────────────────────
# WHY: 기존엔 OpenRouter(클라우드) 전용이라 다른 PC 로 옮기면 OpenRouter 키를 반드시
#   손으로 넣어야 동작했다. LLM_BACKEND=local 이면 OpenAI 호환 로컬 서버(Ollama 등)를
#   기본 대상으로 잡아 키 없이도 완전 동작한다. 두 백엔드 모두 OpenAI 호환
#   /chat/completions 규약을 따르므로 호출 코드는 단일 경로로 유지된다(회귀 0).
#
# .env 로 조정 가능한 변수(전부 합리적 기본값 제공):
#   LLM_BACKEND        = local | openrouter   (기본 openrouter — 기존 동작 보존)
#   LLM_BASE_URL       = OpenAI 호환 base_url 강제 지정(설정 시 최우선)
#   LLM_MODEL          = 모델명 강제 지정
#   LLM_FALLBACK_MODEL = 폴백 모델명 강제 지정
#   OLLAMA_BASE_URL    = local 기본 base_url(기본 http://127.0.0.1:11434/v1)
#   OLLAMA_MODEL       = local 기본 모델(기본 qwen2.5:7b-instruct)
#   OPENROUTER_API_KEY = openrouter 백엔드일 때만 필요(local 이면 불필요).
DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_LOCAL_BASE = "http://127.0.0.1:11434/v1"   # Ollama OpenAI 호환 엔드포인트
DEFAULT_LOCAL_MODEL = "qwen2.5:7b-instruct"        # 도구호출·JSON 안정적인 경량 로컬 기본
DEFAULT_LOCAL_FALLBACK = "llama3.1:8b"


def _backend():
    """현재 LLM 백엔드 식별. 'local' 또는 'openrouter'(기본)."""
    return (os.environ.get("LLM_BACKEND") or "openrouter").strip().lower()


def load_llm_config():
    """LLM 호출 설정 해석. .env 의 LLM_BACKEND 로 local/openrouter 를 고르고,
    각 변수는 env > llm_config.json > 백엔드별 기본값 순으로 결정한다(키 없는 local 안전)."""
    cfg = {}
    try:
        cfg = json.load(open(os.path.join(HERE, "llm_config.json"), encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    backend = _backend()
    if backend == "local":
        base_url = (os.environ.get("LLM_BASE_URL")
                    or os.environ.get("OLLAMA_BASE_URL")
                    or cfg.get("local_base_url")
                    or DEFAULT_LOCAL_BASE)
        model = (os.environ.get("LLM_MODEL")
                 or os.environ.get("OLLAMA_MODEL")
                 or cfg.get("local_model")
                 or DEFAULT_LOCAL_MODEL)
        fallback = (os.environ.get("LLM_FALLBACK_MODEL")
                    or cfg.get("local_fallback")
                    or DEFAULT_LOCAL_FALLBACK)
        # 로컬 서버는 대개 인증을 무시한다. 키가 있으면 그대로 통과(프록시 대비), 없으면 빈 값.
        key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
    else:
        base_url = (os.environ.get("LLM_BASE_URL")
                    or cfg.get("base_url")
                    or DEFAULT_OPENROUTER_BASE)
        model = os.environ.get("LLM_MODEL") or cfg.get("model", "deepseek/deepseek-v4-flash")
        fallback = (os.environ.get("LLM_FALLBACK_MODEL")
                    or cfg.get("fallback", "openai/gpt-4o-mini"))
        key = os.environ.get("OPENROUTER_API_KEY") or cfg.get("api_key", "")
    return {"backend": backend, "key": key, "model": model,
            "fallback": fallback, "base_url": base_url}


def call_llm(messages, model, key, base_url, max_tokens=1500, temperature=0.4, json_mode=True):
    """OpenAI 호환 chat completion(OpenRouter·Ollama 공통). 반환=응답 본문 문자열."""
    payload = {"model": model, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    # 키가 있을 때만 Authorization 을 보낸다. 로컬(키 없음)에서 'Bearer '(빈 키) 헤더가
    # 일부 서버에서 401 을 유발하는 것을 피한다.
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        base_url + "/chat/completions", data=json.dumps(payload).encode(),
        headers=headers, method="POST")
    r = json.loads(urllib.request.urlopen(req, timeout=120).read())
    return r["choices"][0]["message"]["content"]


def strip_fence(s):
    """LLM 출력에서 코드펜스를 벗기고 첫 { ~ 마지막 } 사이만 추출(JSON 강건 파싱용)."""
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    return s.strip()


class MM:
    """Mattermost REST 봇 클라이언트 (토큰 1개)."""

    def __init__(self, token):
        self.token = token

    def _req(self, method, path, body=None, timeout=15):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            MM_BASE + path, data=data,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method=method)
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

    def post(self, channel_id, message):
        return self._req("POST", "/posts", {"channel_id": channel_id, "message": message})

    def user(self, uid):
        return self._req("GET", f"/users/{uid}", timeout=10)

    def history(self, channel_id, n=12):
        return self._req("GET", f"/channels/{channel_id}/posts?per_page={n}", timeout=10)

    def channel(self, channel_id):
        """채널 메타(team_id·type·name 등) 조회(읽기)."""
        return self._req("GET", f"/channels/{channel_id}", timeout=10)

    def channel_by_name(self, team_id, name):
        """team 안의 슬러그(name)로 채널 조회. 없으면 None(멱등 ensure 의 존재 확인용)."""
        try:
            return self._req("GET", f"/teams/{team_id}/channels/name/{name}", timeout=10)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def create_channel(self, team_id, name, display_name, ctype="O"):
        """채널 생성. ctype: 'O'(공개)/'P'(비공개). 생성된 채널 dict 반환."""
        return self._req("POST", "/channels", {
            "team_id": team_id, "name": name,
            "display_name": display_name, "type": ctype})

    def ensure_channel(self, team_id, name, display_name, ctype="O"):
        """멱등 채널 확보: 같은 슬러그가 이미 있으면 그 채널을, 없으면 새로 만들어 반환한다."""
        existing = self.channel_by_name(team_id, name)
        return existing if existing else self.create_channel(team_id, name, display_name, ctype)

    def add_member(self, channel_id, user_id):
        """봇/에이전트 user 를 채널 멤버로 추가(멱등 — 이미 멤버면 Mattermost 가 그대로 반환)."""
        return self._req("POST", f"/channels/{channel_id}/members", {"user_id": user_id})

    def delete_post(self, post_id):
        """게시물 삭제(soft delete). 봇 본인이 올린 테스트 메시지 정리용."""
        req = urllib.request.Request(
            MM_BASE + f"/posts/{post_id}",
            headers={"Authorization": f"Bearer {self.token}"},
            method="DELETE")
        urllib.request.urlopen(req, timeout=10).read()
        return True

    def delete_channel(self, channel_id):
        """채널 아카이브(soft delete). 기존 메시지는 보존되고 채널만 비활성화된다."""
        req = urllib.request.Request(
            MM_BASE + f"/channels/{channel_id}",
            headers={"Authorization": f"Bearer {self.token}"},
            method="DELETE")
        urllib.request.urlopen(req, timeout=10).read()
        return True

