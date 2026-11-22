import json
import os
from typing import Any, AsyncIterator, Dict, List, Tuple

import httpx

from providers.base import BaseProvider, ProviderField, ProxyResult

DEFAULT_BRIDGE_URL = os.environ.get("QUICK_BRIDGE_URL", "http://quick-bridge:3426").rstrip("/")

QUICK_MODELS = ["smart", "fast", "balanced", "advanced"]


class AwsQuickProvider(BaseProvider):
    provider_type = "aws_quick"
    label = "AWS Quick (免费 Opus 5.5)"
    description = "通过官方 OAuth（OIDC + PKCE，零 Cookie）接入 Amazon Quick，支持 smart (Claude Opus 5.5) 与 fast (Haiku 5.5)。"
    fields = [
        ProviderField(
            key="account_key",
            label="Quick 账号标识（授权后自动填入）",
            type="text",
            required=True,
            placeholder="oauth-xxxx",
            help="通过「添加凭证 → AWS Quick」完成浏览器 OAuth 后自动生成。",
        ),
        ProviderField(
            key="bridge_url",
            label="桥接服务地址（留空默认内网直连）",
            type="url",
            required=False,
            placeholder="http://quick-bridge:3426",
            help="默认使用 Docker 内网 http://quick-bridge:3426，无需修改。",
        ),
    ]

    def _bridge_url(self, config: Dict[str, Any]) -> str:
        return (config.get("bridge_url") or DEFAULT_BRIDGE_URL).strip().rstrip("/")

    async def list_models(self, config: Dict[str, Any]) -> List[str]:
        return list(QUICK_MODELS)

    async def test_connection(self, config: Dict[str, Any]) -> Tuple[bool, str]:
        account_key = (config.get("account_key") or "").strip()
        if not account_key:
            return False, "缺少 Quick 账号标识，请重新 OAuth 授权"
        url = f"{self._bridge_url(config)}/usage/{account_key}"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    usage = data.get("usage") or {}
                    plan = usage.get("plan") or usage.get("label") or ""
                    return True, f"AWS Quick 连通正常{f'（{plan}）' if plan else ''}"
                return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
        except Exception as exc:
            return False, f"无法连接 quick-bridge 服务: {exc}"

    async def chat(self, config: Dict[str, Any], payload: Dict[str, Any]) -> ProxyResult:
        account_key = (config.get("account_key") or "").strip()
        url = f"{self._bridge_url(config)}/v1/chat/completions"
        req_body = dict(payload)
        req_body["stream"] = False
        if req_body.get("model") not in QUICK_MODELS:
            req_body["model"] = "smart"
        headers = {"Content-Type": "application/json", "X-Quick-Account": account_key}
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await client.post(url, headers=headers, json=req_body)
        except Exception as exc:
            return ProxyResult(502, {"error": {"message": f"AWS Quick 请求异常: {exc}"}}, error=str(exc))

        if resp.status_code == 200:
            data = resp.json()
            u = data.get("usage") or {}
            p_tok = int(u.get("prompt_tokens") or 0)
            c_tok = int(u.get("completion_tokens") or 0)
            t_tok = int(u.get("total_tokens") or (p_tok + c_tok))
            return ProxyResult(200, data, p_tok, c_tok, t_tok)

        err = f"HTTP {resp.status_code}: {resp.text[:500]}"
        return ProxyResult(resp.status_code, {"error": {"message": err}}, error=err)

    async def chat_stream(
        self, config: Dict[str, Any], payload: Dict[str, Any]
    ) -> Tuple[int, AsyncIterator[bytes], Dict[str, Any]]:
        account_key = (config.get("account_key") or "").strip()
        url = f"{self._bridge_url(config)}/v1/chat/completions"
        req_body = dict(payload)
        req_body["stream"] = True
        if req_body.get("model") not in QUICK_MODELS:
            req_body["model"] = "smart"
        headers = {"Content-Type": "application/json", "X-Quick-Account": account_key}
        usage_info: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "error": ""}

        async def _stream_gen() -> AsyncIterator[bytes]:
            try:
                async with httpx.AsyncClient(timeout=180.0) as client:
                    async with client.stream("POST", url, headers=headers, json=req_body) as resp:
                        if resp.status_code != 200:
                            raw_err = (await resp.aread()).decode("utf-8", errors="ignore")
                            usage_info["error"] = f"HTTP {resp.status_code}: {raw_err[:500]}"
                            yield f"data: {json.dumps({'error': {'message': usage_info['error']}}, ensure_ascii=False)}\n\n".encode("utf-8")
                            return
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                                try:
                                    evt = json.loads(line[5:].strip())
                                    u = evt.get("usage") or {}
                                    if u:
                                        usage_info["prompt_tokens"] = int(u.get("prompt_tokens") or 0)
                                        usage_info["completion_tokens"] = int(u.get("completion_tokens") or 0)
                                        usage_info["total_tokens"] = int(u.get("total_tokens") or 0)
                                except Exception:
                                    pass
                            yield (line + "\n\n").encode("utf-8")
            except Exception as exc:
                usage_info["error"] = f"AWS Quick 流式异常: {exc}"
                yield f"data: {json.dumps({'error': {'message': usage_info['error']}}, ensure_ascii=False)}\n\n".encode("utf-8")

        return 200, _stream_gen(), usage_info