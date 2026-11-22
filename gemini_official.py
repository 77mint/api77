import itertools
import json
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Tuple

import httpx

from providers.base import BaseProvider, ProviderField, ProxyResult

_RR_COUNTER = itertools.count()

AISTUDIO_BASE = "https://generativelanguage.googleapis.com/v1beta"
VERTEX_EXPRESS_BASE = "https://aiplatform.googleapis.com/v1/publishers/google"

FALLBACK_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
]


def _parse_keys(raw: str) -> List[str]:
    return [k.strip() for k in (raw or "").replace(",", "\n").splitlines() if k.strip()]


def _is_aq_key(key: str) -> bool:
    return (key or "").strip().startswith("AQ.")


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
    label = "Google AI Studio (Gemini 官方 Key)"
    description = "直连 Google AI Studio 官方 API（兼容新版 AQ. 与旧版 AIza... 密钥），支持多 Key 轮询与 Vertex Express 自动兜底。"
    fields = [
        ProviderField(
            key="api_keys",
            label="Gemini API Keys（每行一把，自动轮询）",
            type="textarea",
            required=True,
            placeholder="AQ.... 或 AIzaSy...",
            help="填入 Google AI Studio 申请的官方密钥（兼容新版 AQ. 与旧版 AIza...），支持多行自动轮询。",
        ),
        ProviderField(
            key="api_base",
            label="接口根地址（默认留空即可）",
            type="url",
            required=False,
            placeholder="https://generativelanguage.googleapis.com/v1beta",
            help="留空默认直连 Google AI Studio，若为 Vertex Express 密钥会自动回退到 aiplatform.googleapis.com。",
        ),
    ]

    def _candidate_bases(self, config: Dict[str, Any], key: str) -> List[str]:
        custom = (config.get("api_base") or "").strip().rstrip("/")
        if custom:
            return [custom]
        # 新版 Google AI Studio 密钥也以 AQ. 开头，因此优先直连 AI Studio；
        # 仅当 AQ. 密钥在 AI Studio 返回 401/403 时，自动回退到 Vertex Express。
        if _is_aq_key(key):
            return [AISTUDIO_BASE, VERTEX_EXPRESS_BASE]
        return [AISTUDIO_BASE]

    async def list_models(self, config: Dict[str, Any]) -> List[str]:
        keys = _parse_keys(config.get("api_keys", ""))
        if not keys:
            return []
        base = (config.get("api_base") or AISTUDIO_BASE).strip().rstrip("/")
        if "aiplatform.googleapis.com" in base:
            return list(FALLBACK_MODELS)
        url = f"{base}/models"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers={"x-goog-api-key": keys[0]})
                if resp.status_code != 200:
                    return list(FALLBACK_MODELS)
                data = resp.json()
                models = []
                for m in data.get("models", []):
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" in methods:
                        name = m.get("name", "")
                        if name.startswith("models/"):
                            name = name[len("models/") :]
                        models.append(name)
                return models or list(FALLBACK_MODELS)
        except Exception:
            return list(FALLBACK_MODELS)

    async def test_connection(self, config: Dict[str, Any]) -> Tuple[bool, str]:
        keys = _parse_keys(config.get("api_keys", ""))
        if not keys:
            return False, "未填写任何 Gemini API Key"
        first_key = keys[0]
        headers = {"x-goog-api-key": first_key}
        bases = self._candidate_bases(config, first_key)
        last_err = ""

        async with httpx.AsyncClient(timeout=20.0) as client:
            for base in bases:
                if "aiplatform.googleapis.com" not in base:
                    try:
                        resp = await client.get(f"{base}/models", headers=headers)
                        if resp.status_code == 200:
                            count = len(resp.json().get("models", []))
                            return True, f"AI Studio 连通正常（已配置 {len(keys)} 把 Key，发现 {count} 个模型）"
                        last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                        if resp.status_code not in (401, 403):
                            return False, last_err
                    except Exception as exc:
                        last_err = f"网络异常: {exc}"
                else:
                    ping_body = {
                        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                        "generationConfig": {"maxOutputTokens": 4},
                    }
                    for test_model in ("gemini-2.5-flash", "gemini-2.0-flash"):
                        try:
                            resp = await client.post(
                                f"{base}/models/{test_model}:generateContent",
                                headers=headers,
                                json=ping_body,
                            )
                            if resp.status_code == 200:
                                return True, f"Vertex Express 连通正常（已配置 {len(keys)} 把 Key）"
                            last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                            if resp.status_code != 404:
                                break
                        except Exception as exc:
                            last_err = f"网络异常: {exc}"
        return False, last_err or "连通测试失败"

    async def chat(self, config: Dict[str, Any], payload: Dict[str, Any]) -> ProxyResult:
        keys = _pick_keys_rotated(_parse_keys(config.get("api_keys", "")))
        if not keys:
            return ProxyResult(status_code=400, body={"error": {"message": "No Gemini API keys configured"}}, error="未配置 Gemini Key")

        model = payload.get("model", "gemini-2.5-flash")
        gem_body = _openai_to_gemini_body(payload)
        last_err = ""
        last_code = 502

        async with httpx.AsyncClient(timeout=120.0) as client:
            for key in keys:
                headers = {"x-goog-api-key": key}
                bases = self._candidate_bases(config, key)
                should_try_next_key = False

                for b_idx, base in enumerate(bases):
                    url = f"{base}/models/{model}:generateContent"
                    try:
                        resp = await client.post(url, headers=headers, json=gem_body)
                    except Exception as exc:
                        last_code = 502
                        last_err = f"网络异常: {exc}"
                        should_try_next_key = True
                        break

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
                    if resp.status_code in (401, 403) and b_idx < len(bases) - 1:
                        continue
                    if resp.status_code in (429, 500, 502, 503, 504):
                        should_try_next_key = True
                    break

                if not should_try_next_key:
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
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        created = int(time.time())

        async def _stream_gen() -> AsyncIterator[bytes]:
            async with httpx.AsyncClient(timeout=120.0) as client:
                for idx, key in enumerate(keys):
                    headers = {"x-goog-api-key": key}
                    bases = self._candidate_bases(config, key)
                    for b_idx, base in enumerate(bases):
                        url = f"{base}/models/{model}:streamGenerateContent"
                        try:
                            async with client.stream("POST", url, params={"alt": "sse"}, headers=headers, json=gem_body) as resp:
                                if resp.status_code != 200:
                                    raw_err = (await resp.aread()).decode("utf-8", errors="ignore")
                                    usage_info["error"] = f"HTTP {resp.status_code}: {raw_err[:500]}"
                                    if resp.status_code in (401, 403) and b_idx < len(bases) - 1:
                                        continue
                                    if resp.status_code in (429, 500, 502, 503, 504) and idx < len(keys) - 1:
                                        break
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
                                break
                            err_chunk = {"error": {"message": usage_info["error"]}}
                            yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                            return

        return 200, _stream_gen(), usage_info