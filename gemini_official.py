import itertools
import json
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Tuple

import httpx

from providers.base import BaseProvider, ProviderField, ProxyResult

_RR_COUNTER = itertools.count()


def _parse_keys(raw: str) -> List[str]:
    return [k.strip() for k in (raw or "").replace(",", "\n").splitlines() if k.strip()]


def _pick_keys_rotated(keys: List[str]) -> List[str]:
    if not keys:
        return []
    start = next(_RR_COUNTER) % len(keys)
    return keys[start:] + keys[:start]


def _extract_text_from_content(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}]
    parts: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append({"text": item.get("text", "")})
    return parts or [{"text": ""}]


def _openai_to_gemini_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    messages = payload.get("messages") or []
    system_parts: List[Dict[str, Any]] = []
    contents: List[Dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "user")
        parts = _extract_text_from_content(msg.get("content", ""))
        if role == "system":
            system_parts.extend(parts)
        else:
            gem_role = "model" if role == "assistant" else "user"
            contents.append({"role": gem_role, "parts": parts})

    gen_cfg: Dict[str, Any] = {}
    if payload.get("temperature") is not None:
        gen_cfg["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        gen_cfg["topP"] = payload["top_p"]
    if payload.get("max_tokens") is not None:
        gen_cfg["maxOutputTokens"] = payload["max_tokens"]
    if payload.get("stop"):
        stop = payload["stop"]
        gen_cfg["stopSequences"] = stop if isinstance(stop, list) else [stop]

    body: Dict[str, Any] = {"contents": contents}
    if system_parts:
        body["systemInstruction"] = {"parts": system_parts}
    if gen_cfg:
        body["generationConfig"] = gen_cfg
    return body


class GeminiOfficialProvider(BaseProvider):
    provider_type = "gemini_official"
    label = "Google AI Studio (官方 Key 轮询)"
    description = "直连 Google 官方 Gemini API（免 Cookie），支持多把 AIza... Key 自动轮询与 429 切换。"
    fields = [
        ProviderField(
            key="api_keys",
            label="Gemini API Keys（每行一把，自动轮询）",
            type="textarea",
            required=True,
            placeholder="AIzaSy...\nAIzaSy...",
            help="填入 Google AI Studio 申请的官方密钥，支持多行填入实现自动轮询和故障切换。",
        ),
        ProviderField(
            key="api_base",
            label="接口根地址（默认不用改）",
            type="url",
            required=False,
            placeholder="https://generativelanguage.googleapis.com/v1beta",
            help="留空则直连 Google 官方接口。",
        ),
    ]

    def _base_url(self, config: Dict[str, Any]) -> str:
        return (config.get("api_base") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

    async def list_models(self, config: Dict[str, Any]) -> List[str]:
        keys = _parse_keys(config.get("api_keys", ""))
        if not keys:
            return []
        url = f"{self._base_url(config)}/models"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params={"key": keys[0]})
            if resp.status_code != 200:
                return ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]
            data = resp.json()
            models = []
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods:
                    name = m.get("name", "")
                    if name.startswith("models/"):
                        name = name[len("models/") :]
                    models.append(name)
            return models or ["gemini-2.5-flash", "gemini-2.5-pro"]

    async def test_connection(self, config: Dict[str, Any]) -> Tuple[bool, str]:
        keys = _parse_keys(config.get("api_keys", ""))
        if not keys:
            return False, "未填写任何 Gemini API Key"
        url = f"{self._base_url(config)}/models"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params={"key": keys[0]})
            if resp.status_code == 200:
                count = len(resp.json().get("models", []))
                return True, f"连通正常（已配置 {len(keys)} 把 Key，发现 {count} 个模型）"
            return False, f"HTTP {resp.status_code}: {resp.text[:300]}"

    async def chat(self, config: Dict[str, Any], payload: Dict[str, Any]) -> ProxyResult:
        keys = _pick_keys_rotated(_parse_keys(config.get("api_keys", "")))
        if not keys:
            return ProxyResult(status_code=400, body={"error": {"message": "No Gemini API keys configured"}}, error="未配置 Gemini Key")

        model = payload.get("model", "gemini-2.5-flash")
        gem_body = _openai_to_gemini_body(payload)
        base = self._base_url(config)
        last_err = ""
        last_code = 502

        async with httpx.AsyncClient(timeout=120.0) as client:
            for key in keys:
                url = f"{base}/models/{model}:generateContent"
                try:
                    resp = await client.post(url, params={"key": key}, json=gem_body)
                except Exception as exc:
                    last_code = 502
                    last_err = f"网络异常: {exc}"
                    continue

                if resp.status_code == 200:
                    data = resp.json()
                    text = ""
                    candidates = data.get("candidates") or []
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                    usage = data.get("usageMetadata") or {}
                    p_tok = int(usage.get("promptTokenCount") or 0)
                    c_tok = int(usage.get("candidatesTokenCount") or 0)
                    t_tok = int(usage.get("totalTokenCount") or (p_tok + c_tok))
                    openai_resp = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": p_tok,
                            "completion_tokens": c_tok,
                            "total_tokens": t_tok,
                        },
                    }
                    return ProxyResult(200, openai_resp, p_tok, c_tok, t_tok)

                last_code = resp.status_code
                last_err = f"HTTP {resp.status_code}: {resp.text[:500]}"
                if resp.status_code in (429, 500, 502, 503, 504):
                    continue
                break

        return ProxyResult(status_code=last_code, body={"error": {"message": last_err}}, error=last_err)

    async def chat_stream(
        self, config: Dict[str, Any], payload: Dict[str, Any]
    ) -> Tuple[int, AsyncIterator[bytes], Dict[str, Any]]:
        keys = _pick_keys_rotated(_parse_keys(config.get("api_keys", "")))
        usage_info: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "error": ""}
        if not keys:
            usage_info["error"] = "未配置 Gemini Key"

            async def _empty():
                yield b'data: {"error":{"message":"No Gemini API keys configured"}}\n\n'

            return 400, _empty(), usage_info

        model = payload.get("model", "gemini-2.5-flash")
        gem_body = _openai_to_gemini_body(payload)
        base = self._base_url(config)
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        created = int(time.time())

        async def _stream_gen() -> AsyncIterator[bytes]:
            async with httpx.AsyncClient(timeout=120.0) as client:
                for idx, key in enumerate(keys):
                    url = f"{base}/models/{model}:streamGenerateContent"
                    try:
                        async with client.stream("POST", url, params={"key": key, "alt": "sse"}, json=gem_body) as resp:
                            if resp.status_code != 200:
                                raw_err = (await resp.aread()).decode("utf-8", errors="ignore")
                                usage_info["error"] = f"HTTP {resp.status_code}: {raw_err[:500]}"
                                if resp.status_code in (429, 500, 502, 503, 504) and idx < len(keys) - 1:
                                    continue
                                err_chunk = {"error": {"message": usage_info["error"]}}
                                yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                                return

                            usage_info["error"] = ""
                            async for line in resp.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                raw = line[5:].strip()
                                if not raw:
                                    continue
                                try:
                                    evt = json.loads(raw)
                                except Exception:
                                    continue
                                u = evt.get("usageMetadata") or {}
                                if u:
                                    usage_info["prompt_tokens"] = int(u.get("promptTokenCount") or 0)
                                    usage_info["completion_tokens"] = int(u.get("candidatesTokenCount") or 0)
                                    usage_info["total_tokens"] = int(u.get("totalTokenCount") or 0)
                                cands = evt.get("candidates") or []
                                text = ""
                                if cands:
                                    parts = cands[0].get("content", {}).get("parts", [])
                                    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                                if text:
                                    chunk = {
                                        "id": chat_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                                    }
                                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                            done_chunk = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            }
                            yield f"data: {json.dumps(done_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            return
                    except Exception as exc:
                        usage_info["error"] = f"流式请求异常: {exc}"
                        if idx < len(keys) - 1:
                            continue
                        err_chunk = {"error": {"message": usage_info["error"]}}
                        yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8")

        return 200, _stream_gen(), usage_info
