"""
공용 저수준 클라이언트 — OpenRouter LLM + Mattermost REST.

hermes_runtime.py(역할 에이전트)와 ceo_admin_runtime.py(CEO 업데이트 파이프라인)가 공유한다.
부작용(sys.argv 파싱 등) 없이 import 가능하도록 순수 함수/클래스만 둔다.
"""
import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MM_BASE = "http://localhost:8065/api/v4"


def load_llm_config():
    cfg = json.load(open(os.path.join(HERE, "llm_config.json"), encoding="utf-8"))
    key = os.environ.get("OPENROUTER_API_KEY") or cfg.get("api_key", "")
    return {
        "key": key,
        "model": cfg["model"],
        "fallback": cfg.get("fallback", "openai/gpt-4o-mini"),
        "base_url": cfg.get("base_url", "https://openrouter.ai/api/v1"),
    }


def call_llm(messages, model, key, base_url, max_tokens=1500, temperature=0.4, json_mode=True):
    """OpenRouter chat completion. 반환=응답 본문 문자열."""
    payload = {"model": model, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        base_url + "/chat/completions", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
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
