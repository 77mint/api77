import itertools
import json
from typing import Any, AsyncIterator, Dict, List, Tuple

import httpx

from providers.base import BaseProvider, ProviderField, ProxyResult

_RR_COUNTER = itertools.count()


def _parse_keys(raw: str) -> List[str]:
    return [k.strip() for k in (raw or "").replace(",", "\n").splitlines() if k.strip()]


def _pick_keys_rotated(keys: List[str]) -> List[str]:
    if not keys:
        return [""]
    start = next(_RR_COUNTER) % len(keys)
    return keys[start:] + keys[:start]


class OpenAICompatProvider(BaseProvider):
    provider_type = "openai_compat"
    label = "万能 OpenAI /v1 兼容渠道"
    description = "可接入任意 OpenAI 兼容接口：DeepSeek、NVIDIA NIM、本机 5678 端口 Kiro 反代、或其他第三方反代。"
    fields = [
        ProviderField(
            key="base_url",
            label="上游接口地址（Base URL）",
            type="url",
            required=True,
            placeholder="例如 https://api.deepseek.com/v1 或 http://host.docker.internal:5678/v1",
            help="填到 /v1 即可；若接本机宿主机端口（如 Kiro 5678），Docker 内可用 http://host.docker.internal:5678/v1。",
        ),
        ProviderField(
            key="api_keys",
            label="API Key（支持多行轮询，无密钥可留空）",
            type="textarea",
            required=False,
            placeholder="sk-...",
            help="支持填多把 Key 每行一把，遇到 429 自动切换。",
        ),
    ]

    def _endpoint(self, config: Dict[str, Any], path: str) -> str:
        base = (config.get("base_url") or "").rstrip("/")
        return f"{base}{path}"

    def _headers(self, key: str) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    async def list_models(self, config: Dict[str, Any]) -> List[str]:
        keys = _parse_keys(config.get("api_keys", ""))
        key = keys[0] if keys else ""
        url = self._endpoint(config, "/models")
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=self._headers(key))
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]

    async def test_connection(self, config: Dict[str, Any]) -> Tuple[bool, str]:
        base = (config.get("base_url") or "").strip()
        if not base:
            return False, "未填写上游 Base URL"
        keys = _parse_keys(config.get("api_keys", ""))
        key = keys[0] if keys else ""
        url = self._endpoint(config, "/models")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=self._headers(key))
                if resp.status_code == 200:
                    models = resp.json().get("data", [])
                    return True, f"连通正常（获取到 {len(models)} 个模型）"
                return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
        except Exception as exc:
            return False, f"连接失败: {exc}"

    async def chat(self, config: Dict[str, Any], payload: Dict[str, Any]) -> ProxyResult:
        keys = _pick_keys_rotated(_parse_keys(config.get("api_keys", "")))
        url = self._endpoint(config, "/chat/completions")
        last_code = 502
        last_err = ""

        async with httpx.AsyncClient(timeout=120.0) as client:
            for key in keys:
                try:
                    resp = await client.post(url, headers=self._headers(key), json=payload)
                except Exception as exc:
                    last_code = 502
                    last_err = f"上游连接异常: {exc}"
                    continue

                if resp.status_code == 200:
                    data = resp.json()
                    usage = data.get("usage") or {}
                    p_tok = int(usage.get("prompt_tokens") or 0)
                    c_tok = int(usage.get("completion_tokens") or 0)
                    t_tok = int(usage.get("total_tokens") or (p_tok + c_tok))
                    return ProxyResult(200, data, p_tok, c_tok, t_tok)

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
        url = self._endpoint(config, "/chat/completions")
        usage_info: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "error": ""}

        async def _stream_gen() -> AsyncIterator[bytes]:
            async with httpx.AsyncClient(timeout=120.0) as client:
                for idx, key in enumerate(keys):
                    try:
                        async with client.stream("POST", url, headers=self._headers(key), json=payload) as resp:
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
                                if line.startswith("data:"):
                                    raw = line[5:].strip()
                                    if raw and raw != "[DONE]":
                                        try:
                                            evt = json.loads(raw)
                                            u = evt.get("usage") or {}
                                            if u:
                                                usage_info["prompt_tokens"] = int(u.get("prompt_tokens") or 0)
                                                usage_info["completion_tokens"] = int(u.get("completion_tokens") or 0)
                                                usage_info["total_tokens"] = int(u.get("total_tokens") or 0)
                                        except Exception:
                                            pass
                                yield (line + "\n").encode("utf-8")
                            return
                    except Exception as exc:
                        usage_info["error"] = f"流式连接异常: {exc}"
                        if idx < len(keys) - 1:
                            continue
                        err_chunk = {"error": {"message": usage_info["error"]}}
                        yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8")

        return 200, _stream_gen(), usage_info
