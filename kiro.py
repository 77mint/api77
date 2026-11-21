import base64
import hashlib
import json
import secrets
import struct
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx

from providers.base import BaseProvider, ProviderField, ProxyResult

_TOKEN_CACHE: Dict[str, Dict[str, Any]] = {}

DEFAULT_KIRO_MODELS = [
    "claude-sonnet-4.5",
    "claude-sonnet-4",
    "claude-3.7-sonnet",
    "claude-haiku-4.5",
    "claude-opus-4.5",
]

KIRO_REDIRECT_URI = "http://localhost:49153/oauth/callback"
KIRO_BUILDER_START_URL = "https://view.awsapps.com/start"
KIRO_OIDC_SCOPES = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
]


def generate_pkce_pair() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_social_auth_url(idp: str, region: str = "us-east-1") -> Dict[str, str]:
    """生成 Kiro Social (Github / Google) 三步授权登录链接。"""
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(16)
    norm_idp = "Google" if idp.lower() == "google" else "Github"
    url = (
        f"https://prod.{region}.auth.desktop.kiro.dev/login"
        f"?idp={norm_idp}"
        f"&redirect_uri={KIRO_REDIRECT_URI}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
        f"&state={state}"
    )
    return {
        "auth_url": url,
        "code_verifier": verifier,
        "state": state,
        "region": region,
        "idp": norm_idp,
        "redirect_uri": KIRO_REDIRECT_URI,
    }


async def exchange_social_code(
    code: str, code_verifier: str, region: str = "us-east-1"
) -> Tuple[Optional[Dict[str, Any]], str]:
    """用回调地址中的 code 换取 Kiro accessToken / refreshToken。"""
    url = f"https://prod.{region}.auth.desktop.kiro.dev/oauth/token"
    headers = {"Content-Type": "application/json", "User-Agent": "KiroIDE-0.8.0"}
    body = {
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": KIRO_REDIRECT_URI,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=headers, json=body)
    except Exception as exc:
        return None, f"换取凭证网络异常: {exc}"
    if resp.status_code != 200:
        return None, f"换取凭证失败 HTTP {resp.status_code}: {resp.text[:300]}"
    data = resp.json()
    if not data.get("refreshToken") and not data.get("accessToken"):
        return None, f"返回数据缺少令牌: {resp.text[:200]}"
    return data, ""


async def start_builder_device_auth(region: str = "us-east-1") -> Tuple[Optional[Dict[str, Any]], str]:
    """启动 AWS Builder ID 设备码登录（OAuth Device Flow）。"""
    oidc_base = f"https://oidc.{region}.amazonaws.com"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            reg_resp = await client.post(
                f"{oidc_base}/client/register",
                json={
                    "clientName": "Kiro",
                    "clientType": "public",
                    "scopes": KIRO_OIDC_SCOPES,
                },
            )
            if reg_resp.status_code != 200:
                return None, f"注册 OIDC 客户端失败 HTTP {reg_resp.status_code}: {reg_resp.text[:250]}"
            reg = reg_resp.json()
            client_id = reg.get("clientId", "")
            client_secret = reg.get("clientSecret", "")

            dev_resp = await client.post(
                f"{oidc_base}/device_authorization",
                json={
                    "clientId": client_id,
                    "clientSecret": client_secret,
                    "startUrl": KIRO_BUILDER_START_URL,
                },
            )
            if dev_resp.status_code != 200:
                return None, f"申请设备码失败 HTTP {dev_resp.status_code}: {dev_resp.text[:250]}"
            dev = dev_resp.json()
            return {
                "region": region,
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": dev.get("deviceCode", ""),
                "user_code": dev.get("userCode", ""),
                "verification_uri": dev.get("verificationUriComplete") or dev.get("verificationUri", ""),
                "interval": int(dev.get("interval") or 5),
                "expires_in": int(dev.get("expiresIn") or 600),
            }, ""
    except Exception as exc:
        return None, f"启动设备码登录异常: {exc}"


async def poll_builder_device_token(
    client_id: str, client_secret: str, device_code: str, region: str = "us-east-1"
) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """轮询 AWS Builder ID 设备码授权结果。返回 (status: pending|ok|error, data, msg)。"""
    url = f"https://oidc.{region}.amazonaws.com/token"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json={
                    "clientId": client_id,
                    "clientSecret": client_secret,
                    "deviceCode": device_code,
                    "grantType": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
    except Exception as exc:
        return "error", None, f"轮询异常: {exc}"

    if resp.status_code == 200:
        return "ok", resp.json(), ""

    text = resp.text[:300]
    if "authorization_pending" in text or "slow_down" in text:
        return "pending", None, "等待你在浏览器点击确认授权..."
    return "error", None, f"授权失败 HTTP {resp.status_code}: {text}"


async def fetch_kiro_account_info(
    access_token: str, region: str = "us-east-1", profile_arn: str = ""
) -> Dict[str, Any]:
    """调用 Kiro GetUsageLimits 接口自动识别 Free/Pro 等级、真实额度与账号邮箱。"""
    endpoints = [
        f"https://codewhisperer.{region}.amazonaws.com/",
        f"https://q.{region}.amazonaws.com/",
    ]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": "AmazonCodeWhispererService.GetUsageLimits",
        "User-Agent": "aws-sdk-js/1.0.0 KiroIDE-0.8.0",
    }
    body: Dict[str, Any] = {}
    if profile_arn:
        body["profileArn"] = profile_arn

    async with httpx.AsyncClient(timeout=15.0) as client:
        for ep in endpoints:
            try:
                resp = await client.post(ep, headers=headers, json=body)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                sub = data.get("subscriptionInfo") or {}
                title = str(sub.get("subscriptionTitle") or sub.get("type") or "")
                tier = "Pro" if "PRO" in title.upper() else ("Free" if title else "Free")

                used = 0
                limit = 50 if tier == "Free" else 0
                breakdowns = data.get("usageBreakdownList") or []
                if breakdowns and isinstance(breakdowns, list):
                    b0 = breakdowns[0]
                    raw_used = b0.get("currentUsageWithPrecision") or b0.get("currentUsage") or 0
                    raw_limit = b0.get("usageLimitWithPrecision") or b0.get("usageLimit") or 0
                    try:
                        used = int(round(float(raw_used)))
                    except Exception:
                        used = 0
                    try:
                        if float(raw_limit) > 0:
                            limit = int(round(float(raw_limit)))
                    except Exception:
                        pass

                user_info = data.get("userInfo") or {}
                email = str(user_info.get("email") or user_info.get("userId") or "")
                return {
                    "ok": True,
                    "tier": tier,
                    "subscription_title": title,
                    "quota_used": used,
                    "quota_limit": limit,
                    "quota_cycle": "monthly",
                    "email": email,
                }
            except Exception:
                continue

    return {
        "ok": False,
        "tier": "Free",
        "subscription_title": "",
        "quota_used": 0,
        "quota_limit": 50,
        "quota_cycle": "monthly",
        "email": "",
    }


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content or "")


def _build_kiro_payload(payload: Dict[str, Any], profile_arn: str = "") -> Dict[str, Any]:
    model_id = str(payload.get("model") or "claude-sonnet-4.5").strip()
    raw_messages = payload.get("messages") or []

    system_chunks: List[str] = []
    chat_msgs: List[Tuple[str, str]] = []
    for m in raw_messages:
        role = m.get("role", "user")
        text = _extract_text(m.get("content", "")).strip()
        if role == "system":
            if text:
                system_chunks.append(text)
        else:
            norm_role = "assistant" if role == "assistant" else "user"
            if chat_msgs and chat_msgs[-1][0] == norm_role:
                chat_msgs[-1] = (norm_role, f"{chat_msgs[-1][1]}\n\n{text}".strip())
            else:
                chat_msgs.append((norm_role, text or "continue"))

    if not chat_msgs:
        chat_msgs = [("user", "Hello")]

    if system_chunks:
        sys_prefix = "\n\n".join(system_chunks)
        first_role, first_text = chat_msgs[0]
        if first_role == "user":
            chat_msgs[0] = ("user", f"{sys_prefix}\n\n{first_text}")
        else:
            chat_msgs.insert(0, ("user", sys_prefix))

    if chat_msgs[-1][0] != "user":
        chat_msgs.append(("user", "continue"))

    history: List[Dict[str, Any]] = []
    for role, text in chat_msgs[:-1]:
        if role == "user":
            history.append(
                {
                    "userInputMessage": {
                        "content": text or "continue",
                        "modelId": model_id,
                        "origin": "AI_EDITOR",
                    }
                }
            )
        else:
            history.append({"assistantResponseMessage": {"content": text or ""}})

    conv_state: Dict[str, Any] = {
        "conversationId": str(uuid.uuid4()),
        "chatTriggerType": "MANUAL",
        "currentMessage": {
            "userInputMessage": {
                "content": chat_msgs[-1][1] or "continue",
                "modelId": model_id,
                "origin": "AI_EDITOR",
            }
        },
    }
    if history:
        conv_state["history"] = history

    body: Dict[str, Any] = {"conversationState": conv_state}
    if profile_arn:
        body["profileArn"] = profile_arn
    return body


def _iter_eventstream_payloads(buf: bytearray) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    while len(buf) >= 12:
        total_len, headers_len = struct.unpack(">II", buf[:8])
        if total_len < 16 or total_len > 10 * 1024 * 1024:
            del buf[:1]
            continue
        if len(buf) < total_len:
            break
        frame = bytes(buf[:total_len])
        del buf[:total_len]
        payload_start = 12 + headers_len
        payload_end = total_len - 4
        if payload_start < payload_end:
            raw_payload = frame[payload_start:payload_end]
            try:
                events.append(json.loads(raw_payload.decode("utf-8", errors="ignore")))
            except Exception:
                pass
    return events


def _extract_delta_text(evt: Dict[str, Any]) -> str:
    if not isinstance(evt, dict):
        return ""
    if "content" in evt and isinstance(evt["content"], str):
        return evt["content"]
    nested = evt.get("assistantResponseEvent")
    if isinstance(nested, dict) and isinstance(nested.get("content"), str):
        return nested["content"]
    return ""


class KiroProvider(BaseProvider):
    provider_type = "kiro_native"
    label = "Kiro 原生反代"
    description = "支持 GitHub / Google 三步授权、AWS Builder ID 设备码登录及 JSON 导入，自动识别 Free/Pro 等级与额度。"
    fields = [
        ProviderField(
            key="refresh_token",
            label="Kiro RefreshToken",
            type="password",
            required=True,
            placeholder="aorAAAAAG...",
            help="可通过一键授权自动获取，也可直接粘贴。",
        ),
        ProviderField(
            key="region",
            label="区域（默认 us-east-1）",
            type="text",
            required=False,
            placeholder="us-east-1",
        ),
        ProviderField(
            key="profile_arn",
            label="Profile ARN（可选）",
            type="text",
            required=False,
            placeholder="自动获取，一般留空",
        ),
    ]

    def _region(self, config: Dict[str, Any]) -> str:
        return (config.get("region") or "us-east-1").strip()

    async def _get_access_token(
        self, config: Dict[str, Any], force_refresh: bool = False
    ) -> Tuple[Optional[str], str, str]:
        refresh_token = (config.get("refresh_token") or "").strip()
        if not refresh_token:
            return None, "", "未填写 Kiro RefreshToken"

        region = self._region(config)
        cache_key = f"{region}:{refresh_token[:24]}"
        now = time.time()
        cached = _TOKEN_CACHE.get(cache_key)
        if not force_refresh and cached and cached.get("expires_at", 0) > now + 60:
            arn = (config.get("profile_arn") or cached.get("profile_arn") or "").strip()
            return cached["access_token"], arn, ""

        auth_method = (config.get("auth_method") or "social").lower()
        client_id = (config.get("client_id") or "").strip()
        client_secret = (config.get("client_secret") or "").strip()

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                if (auth_method == "oidc" or (client_id and client_secret)) and client_id and client_secret:
                    url = f"https://oidc.{region}.amazonaws.com/token"
                    resp = await client.post(
                        url,
                        json={
                            "clientId": client_id,
                            "clientSecret": client_secret,
                            "refreshToken": refresh_token,
                            "grantType": "refresh_token",
                        },
                    )
                else:
                    url = f"https://prod.{region}.auth.desktop.kiro.dev/refreshToken"
                    resp = await client.post(
                        url,
                        headers={"Content-Type": "application/json", "User-Agent": "KiroIDE-0.8.0"},
                        json={"refreshToken": refresh_token},
                    )
        except Exception as exc:
            return None, "", f"刷新 Token 网络异常: {exc}"

        if resp.status_code != 200:
            return None, "", f"刷新 Token 失败 HTTP {resp.status_code}: {resp.text[:300]}"

        data = resp.json()
        access_token = data.get("accessToken") or ""
        if not access_token:
            return None, "", f"刷新响应中缺少 accessToken: {resp.text[:200]}"

        expires_in = int(data.get("expiresIn") or 3600)
        profile_arn = (config.get("profile_arn") or data.get("profileArn") or "").strip()
        _TOKEN_CACHE[cache_key] = {
            "access_token": access_token,
            "profile_arn": profile_arn,
            "expires_at": now + expires_in,
        }
        return access_token, profile_arn, ""

    async def inspect_account(self, config: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], str]:
        token, arn, err = await self._get_access_token(config, force_refresh=True)
        if not token:
            return False, {}, err
        info = await fetch_kiro_account_info(token, self._region(config), arn)
        info["profile_arn"] = arn
        return True, info, ""

    def _chat_endpoints(self, region: str) -> List[str]:
        return [
            f"https://q.{region}.amazonaws.com/generateAssistantResponse",
            f"https://codewhisperer.{region}.amazonaws.com/generateAssistantResponse",
        ]

    def _request_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.amazon.eventstream",
            "User-Agent": "aws-sdk-js/1.0.0 KiroIDE-0.8.0",
            "x-amzn-kiro-agent-mode": "vibe",
        }

    async def list_models(self, config: Dict[str, Any]) -> List[str]:
        return list(DEFAULT_KIRO_MODELS)

    async def test_connection(self, config: Dict[str, Any]) -> Tuple[bool, str]:
        ok, info, err = await self.inspect_account(config)
        if not ok:
            return False, err
        tier = info.get("tier") or "Free"
        used = info.get("quota_used", 0)
        limit = info.get("quota_limit", 50)
        return True, f"凭证有效（{tier}，额度 {used}/{limit}）"

    async def chat(self, config: Dict[str, Any], payload: Dict[str, Any]) -> ProxyResult:
        access_token, profile_arn, err = await self._get_access_token(config)
        if not access_token:
            return ProxyResult(401, {"error": {"message": err}}, error=err)

        region = self._region(config)
        body = _build_kiro_payload(payload, profile_arn)
        model = str(payload.get("model") or "claude-sonnet-4.5")
        last_code, last_err = 502, ""

        async with httpx.AsyncClient(timeout=120.0) as client:
            for endpoint in self._chat_endpoints(region):
                try:
                    resp = await client.post(endpoint, headers=self._request_headers(access_token), json=body)
                except Exception as exc:
                    last_code, last_err = 502, f"请求 Kiro 异常: {exc}"
                    continue

                if resp.status_code == 200:
                    buf = bytearray(resp.content)
                    pieces = [_extract_delta_text(e) for e in _iter_eventstream_payloads(buf)]
                    full_text = "".join(pieces)
                    est_out = max(1, len(full_text) // 3)
                    openai_resp = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": full_text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 0, "completion_tokens": est_out, "total_tokens": est_out},
                    }
                    return ProxyResult(200, openai_resp, 0, est_out, est_out)

                last_code = resp.status_code
                last_err = f"HTTP {resp.status_code}: {resp.text[:500]}"
                if resp.status_code in (401, 403):
                    break

        return ProxyResult(last_code, {"error": {"message": last_err}}, error=last_err)

    async def chat_stream(
        self, config: Dict[str, Any], payload: Dict[str, Any]
    ) -> Tuple[int, AsyncIterator[bytes], Dict[str, Any]]:
        usage_info: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "error": ""}
        access_token, profile_arn, err = await self._get_access_token(config)
        if not access_token:
            usage_info["error"] = err

            async def _err_gen():
                yield f"data: {json.dumps({'error': {'message': err}}, ensure_ascii=False)}\n\n".encode("utf-8")

            return 401, _err_gen(), usage_info

        region = self._region(config)
        body = _build_kiro_payload(payload, profile_arn)
        model = str(payload.get("model") or "claude-sonnet-4.5")
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        created = int(time.time())

        async def _stream_gen() -> AsyncIterator[bytes]:
            endpoints = self._chat_endpoints(region)
            async with httpx.AsyncClient(timeout=120.0) as client:
                for idx, endpoint in enumerate(endpoints):
                    try:
                        async with client.stream(
                            "POST", endpoint, headers=self._request_headers(access_token), json=body
                        ) as resp:
                            if resp.status_code != 200:
                                raw_err = (await resp.aread()).decode("utf-8", errors="ignore")
                                usage_info["error"] = f"HTTP {resp.status_code}: {raw_err[:500]}"
                                if resp.status_code not in (401, 403) and idx < len(endpoints) - 1:
                                    continue
                                yield f"data: {json.dumps({'error': {'message': usage_info['error']}}, ensure_ascii=False)}\n\n".encode("utf-8")
                                return

                            usage_info["error"] = ""
                            buf = bytearray()
                            char_count = 0
                            async for raw_chunk in resp.aiter_bytes():
                                buf.extend(raw_chunk)
                                for evt in _iter_eventstream_payloads(buf):
                                    text = _extract_delta_text(evt)
                                    if text:
                                        char_count += len(text)
                                        chunk = {
                                            "id": chat_id,
                                            "object": "chat.completion.chunk",
                                            "created": created,
                                            "model": model,
                                            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                                        }
                                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                            est_out = max(1, char_count // 3) if char_count else 0
                            usage_info["completion_tokens"] = est_out
                            usage_info["total_tokens"] = est_out
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
                        usage_info["error"] = f"Kiro 流式请求异常: {exc}"
                        if idx < len(endpoints) - 1:
                            continue
                        yield f"data: {json.dumps({'error': {'message': usage_info['error']}}, ensure_ascii=False)}\n\n".encode("utf-8")

        return 200, _stream_gen(), usage_info