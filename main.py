import hashlib
import hmac
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import db
from providers import get_provider, list_provider_schemas, load_providers

app = FastAPI(title="77api", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
def on_startup():
    info = db.init_db()
    load_providers()
    if info:
        print("=" * 50)
        print("77api initialized!")
        if "initial_admin_password" in info:
            print(f"Initial Admin Password: {info['initial_admin_password']}")
        if "initial_proxy_api_key" in info:
            print(f"Initial Proxy API Key:  {info['initial_proxy_api_key']}")
        print("=" * 50)


# ---------- Admin Auth Helpers ----------
def _make_session_token() -> str:
    secret = db.get_setting("session_secret", "fallback")
    exp = int(time.time()) + 7 * 86400
    msg = f"admin:{exp}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _verify_session_token(token: str) -> bool:
    if "." not in token:
        return False
    exp_s, sig = token.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    secret = db.get_setting("session_secret", "fallback")
    expected = hmac.new(secret.encode("utf-8"), f"admin:{exp}".encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def require_admin(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先登录管理面板")
    token = authorization[7:].strip()
    if not _verify_session_token(token):
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")


def require_proxy_key(authorization: Optional[str] = Header(default=None)):
    expected = db.get_setting("proxy_api_key", "")
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization Bearer token")
    token = authorization[7:].strip()
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid Proxy API Key")


# ---------- Pydantic Models ----------
class LoginReq(BaseModel):
    password: str


class ChannelPayload(BaseModel):
    name: str
    provider_type: str
    models: str = ""
    enabled: bool = True
    priority: int = 10
    tier: str = ""
    quota_limit: int = 0
    quota_used: int = 0
    quota_cycle: str = "none"
    config: Dict[str, Any] = {}


class ChannelToggleReq(BaseModel):
    enabled: bool


class QuotaCalibrateReq(BaseModel):
    quota_used: int
    quota_limit: Optional[int] = None


class FetchModelsReq(BaseModel):
    provider_type: str
    config: Dict[str, Any] = {}


class PasswordChangeReq(BaseModel):
    old_password: str
    new_password: str


class FailoverReq(BaseModel):
    enabled: bool


class BackupImportReq(BaseModel):
    channels: List[Dict[str, Any]] = []
    replace: bool = False


# ---------- Web UI ----------
@app.get("/")
def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
def health():
    return {"status": "ok", "app": "77api", "providers": len(list_provider_schemas())}


# ---------- Admin APIs ----------
@app.post("/api/login")
def admin_login(req: LoginReq):
    stored_hash = db.get_setting("admin_password_hash", "")
    if not db.verify_password(req.password, stored_hash):
        raise HTTPException(status_code=401, detail="密码错误")
    return {"token": _make_session_token()}


@app.get("/api/providers", dependencies=[Depends(require_admin)])
def api_list_providers():
    return {"providers": list_provider_schemas()}


@app.post("/api/providers/fetch-models", dependencies=[Depends(require_admin)])
async def api_fetch_models_preview(req: FetchModelsReq):
    provider = get_provider(req.provider_type)
    if not provider:
        raise HTTPException(status_code=400, detail=f"未加载插件: {req.provider_type}")
    try:
        models = await provider.list_models(req.config)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"拉取模型失败: {exc}")
    return {"models": models}


@app.get("/api/stats", dependencies=[Depends(require_admin)])
def api_stats():
    stats = db.get_stats()
    channels = db.list_channels()
    stats["channel_total"] = len(channels)
    stats["channel_enabled"] = sum(1 for c in channels if c["enabled"])
    stats["proxy_api_key"] = db.get_setting("proxy_api_key", "")
    stats["auto_failover"] = db.get_setting("auto_failover", "1") == "1"
    return stats


@app.get("/api/channels", dependencies=[Depends(require_admin)])
def api_list_channels():
    return {"channels": db.list_channels()}


@app.post("/api/channels", dependencies=[Depends(require_admin)])
def api_create_channel(payload: ChannelPayload):
    if not get_provider(payload.provider_type):
        raise HTTPException(status_code=400, detail=f"未知渠道类型: {payload.provider_type}")
    cid = db.create_channel(
        name=payload.name.strip() or payload.provider_type,
        provider_type=payload.provider_type,
        config=payload.config,
        models=payload.models.strip(),
        enabled=payload.enabled,
        priority=payload.priority,
        tier=payload.tier.strip(),
        quota_limit=payload.quota_limit,
        quota_used=payload.quota_used,
        quota_cycle=payload.quota_cycle,
    )
    return {"id": cid, "channel": db.get_channel(cid)}


@app.put("/api/channels/{channel_id}", dependencies=[Depends(require_admin)])
def api_update_channel(channel_id: int, payload: ChannelPayload):
    if not get_provider(payload.provider_type):
        raise HTTPException(status_code=400, detail=f"未知渠道类型: {payload.provider_type}")
    ok = db.update_channel(
        channel_id=channel_id,
        name=payload.name.strip() or payload.provider_type,
        provider_type=payload.provider_type,
        config=payload.config,
        models=payload.models.strip(),
        enabled=payload.enabled,
        priority=payload.priority,
        tier=payload.tier.strip(),
        quota_limit=payload.quota_limit,
        quota_used=payload.quota_used,
        quota_cycle=payload.quota_cycle,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return {"channel": db.get_channel(channel_id)}


@app.patch("/api/channels/{channel_id}/toggle", dependencies=[Depends(require_admin)])
def api_toggle_channel(channel_id: int, req: ChannelToggleReq):
    ok = db.update_channel(channel_id=channel_id, enabled=req.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return {"ok": True, "enabled": req.enabled}


@app.patch("/api/channels/{channel_id}/quota", dependencies=[Depends(require_admin)])
def api_calibrate_quota(channel_id: int, req: QuotaCalibrateReq):
    ok = db.update_channel(
        channel_id=channel_id,
        quota_used=req.quota_used,
        quota_limit=req.quota_limit,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return {"channel": db.get_channel(channel_id)}


@app.delete("/api/channels/{channel_id}", dependencies=[Depends(require_admin)])
def api_delete_channel(channel_id: int):
    if not db.delete_channel(channel_id):
        raise HTTPException(status_code=404, detail="渠道不存在")
    return {"ok": True}


@app.post("/api/channels/{channel_id}/test", dependencies=[Depends(require_admin)])
async def api_test_channel(channel_id: int):
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="渠道不存在")
    provider = get_provider(ch["provider_type"])
    if not provider:
        raise HTTPException(status_code=400, detail=f"未加载插件: {ch['provider_type']}")
    t0 = time.perf_counter()
    try:
        ok, msg = await provider.test_connection(ch["config"])
    except Exception as exc:
        ok, msg = False, f"测试异常: {exc}"
    latency_ms = int((time.perf_counter() - t0) * 1000)
    db.update_channel_health(channel_id, "ok" if ok else "error", latency_ms, "" if ok else msg)
    return {"ok": ok, "message": msg, "latency_ms": latency_ms}


@app.post("/api/channels/{channel_id}/chat-test", dependencies=[Depends(require_admin)])
async def api_chat_test_channel(channel_id: int):
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="渠道不存在")
    provider = get_provider(ch["provider_type"])
    if not provider:
        raise HTTPException(status_code=400, detail="插件未加载")
    models = ch["model_list"]
    if not models:
        try:
            models = await provider.list_models(ch["config"])
        except Exception:
            models = []
    model = models[0] if models else "gemini-2.5-flash"
    test_payload = {
        "model": model,
        "messages": [{"role": "user", "content": "请只回复四个字：连接成功"}],
        "max_tokens": 32,
        "stream": False,
    }
    t0 = time.perf_counter()
    try:
        res = await provider.chat(ch["config"], test_payload)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {"ok": False, "model": model, "latency_ms": latency_ms, "reply": "", "error": str(exc)}
    latency_ms = int((time.perf_counter() - t0) * 1000)
    if res.status_code == 200:
        reply = ""
        try:
            reply = res.body["choices"][0]["message"]["content"]
        except Exception:
            reply = str(res.body)[:200]
        db.update_channel_health(channel_id, "ok", latency_ms, "")
        return {"ok": True, "model": model, "latency_ms": latency_ms, "reply": reply, "error": ""}
    db.update_channel_health(channel_id, "error", latency_ms, res.error)
    return {"ok": False, "model": model, "latency_ms": latency_ms, "reply": "", "error": res.error}


@app.post("/api/channels/{channel_id}/fetch-models", dependencies=[Depends(require_admin)])
async def api_fetch_channel_models(channel_id: int):
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="渠道不存在")
    provider = get_provider(ch["provider_type"])
    if not provider:
        raise HTTPException(status_code=400, detail="插件未加载")
    models = await provider.list_models(ch["config"])
    return {"models": models}


@app.get("/api/logs", dependencies=[Depends(require_admin)])
def api_logs(limit: int = 80, only_errors: bool = False):
    return {"logs": db.list_logs(limit=min(limit, 300), only_errors=only_errors)}


@app.delete("/api/logs", dependencies=[Depends(require_admin)])
def api_clear_logs():
    db.clear_logs()
    return {"ok": True}


@app.post("/api/settings/password", dependencies=[Depends(require_admin)])
def api_change_password(req: PasswordChangeReq):
    stored_hash = db.get_setting("admin_password_hash", "")
    if not db.verify_password(req.old_password, stored_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少需要 6 位")
    db.set_setting("admin_password_hash", db.hash_password(req.new_password))
    return {"ok": True}


@app.post("/api/settings/rotate-proxy-key", dependencies=[Depends(require_admin)])
def api_rotate_proxy_key():
    new_key = f"sk-77api-{secrets.token_urlsafe(24)}"
    db.set_setting("proxy_api_key", new_key)
    return {"proxy_api_key": new_key}


@app.post("/api/settings/failover", dependencies=[Depends(require_admin)])
def api_set_failover(req: FailoverReq):
    db.set_setting("auto_failover", "1" if req.enabled else "0")
    return {"auto_failover": req.enabled}


@app.get("/api/backup/export", dependencies=[Depends(require_admin)])
def api_backup_export():
    channels = db.list_channels()
    exported = [
        {
            "name": c["name"],
            "provider_type": c["provider_type"],
            "enabled": c["enabled"],
            "priority": c["priority"],
            "models": c["models"],
            "tier": c.get("tier", ""),
            "quota_limit": c.get("quota_limit", 0),
            "quota_used": c.get("quota_used", 0),
            "quota_cycle": c.get("quota_cycle", "none"),
            "config": c["config"],
        }
        for c in channels
    ]
    return {"app": "77api", "version": "2.0.0", "channels": exported}


@app.post("/api/backup/import", dependencies=[Depends(require_admin)])
def api_backup_import(req: BackupImportReq):
    if req.replace:
        for c in db.list_channels():
            db.delete_channel(c["id"])
    count = 0
    for c in req.channels:
        ptype = str(c.get("provider_type") or "")
        if not get_provider(ptype):
            continue
        db.create_channel(
            name=str(c.get("name") or ptype),
            provider_type=ptype,
            config=c.get("config") or {},
            models=str(c.get("models") or ""),
            enabled=bool(c.get("enabled", True)),
            priority=int(c.get("priority", 10)),
            tier=str(c.get("tier") or ""),
            quota_limit=int(c.get("quota_limit") or 0),
            quota_used=int(c.get("quota_used") or 0),
            quota_cycle=str(c.get("quota_cycle") or "none"),
        )
        count += 1
    return {"ok": True, "imported": count}


# ---------- OpenAI-Compatible Proxy Endpoints ----------
def _select_channels(model: str) -> List[Dict[str, Any]]:
    enabled = db.list_channels(only_enabled=True)
    exact: List[Dict[str, Any]] = []
    wildcard: List[Dict[str, Any]] = []
    for ch in enabled:
        mlist = ch["model_list"]
        if not mlist or "*" in mlist:
            wildcard.append(ch)
        elif model in mlist:
            exact.append(ch)
    return exact + wildcard


@app.get("/v1/models", dependencies=[Depends(require_proxy_key)])
async def proxy_list_models():
    seen = set()
    data = []
    for ch in db.list_channels(only_enabled=True):
        models = ch["model_list"]
        if not models:
            provider = get_provider(ch["provider_type"])
            if provider:
                try:
                    models = await provider.list_models(ch["config"])
                except Exception:
                    models = []
        for m in models:
            if m and m != "*" and m not in seen:
                seen.add(m)
                data.append({"id": m, "object": "model", "created": int(time.time()), "owned_by": ch["name"]})
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions", dependencies=[Depends(require_proxy_key)])
async def proxy_chat_completions(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    model = str(payload.get("model") or "").strip()
    is_stream = bool(payload.get("stream", False))
    auto_failover = db.get_setting("auto_failover", "1") == "1"
    candidates = _select_channels(model)

    if not candidates:
        err = f"没有启用的渠道匹配模型 '{model}'，请检查渠道开关或模型列表"
        db.add_log(None, "未匹配渠道", "none", model or "-", 404, False, 0, error_detail=err)
        return JSONResponse(status_code=404, content={"error": {"message": err}})

    attempt_list = candidates if auto_failover else candidates[:1]

    if is_stream:
        async def _failover_stream():
            for idx, ch in enumerate(attempt_list):
                provider = get_provider(ch["provider_type"])
                if not provider:
                    continue
                t0 = time.perf_counter()
                status_code, stream_iter, usage_info = await provider.chat_stream(ch["config"], payload)
                yielded_any = False
                buffered_err = ""
                try:
                    async for chunk in stream_iter:
                        # If provider reported an error before sending real content, allow failover
                        if not yielded_any and usage_info.get("error"):
                            buffered_err = usage_info["error"]
                            break
                        yielded_any = True
                        yield chunk
                finally:
                    latency = int((time.perf_counter() - t0) * 1000)
                    err_msg = usage_info.get("error", "") or buffered_err
                    ok = status_code == 200 and not err_msg
                    db.add_log(
                        channel_id=ch["id"],
                        channel_name=ch["name"],
                        provider_type=ch["provider_type"],
                        model=model,
                        status_code=status_code if ok else (status_code if status_code != 200 else 502),
                        success=ok,
                        latency_ms=latency,
                        prompt_tokens=int(usage_info.get("prompt_tokens") or 0),
                        completion_tokens=int(usage_info.get("completion_tokens") or 0),
                        total_tokens=int(usage_info.get("total_tokens") or 0),
                        error_detail=err_msg,
                    )
                if yielded_any or (not auto_failover) or idx == len(attempt_list) - 1:
                    if not yielded_any and buffered_err:
                        yield f'data: {{"error":{{"message":{repr(buffered_err)}}}}}\n\n'.encode("utf-8")
                    return

        return StreamingResponse(_failover_stream(), status_code=200, media_type="text/event-stream")

    last_status = 502
    last_body: Dict[str, Any] = {"error": {"message": "All channels failed"}}

    for ch in attempt_list:
        provider = get_provider(ch["provider_type"])
        if not provider:
            continue
        t0 = time.perf_counter()
        try:
            res = await provider.chat(ch["config"], payload)
        except Exception as exc:
            latency = int((time.perf_counter() - t0) * 1000)
            err_msg = f"插件执行异常: {exc}"
            db.add_log(ch["id"], ch["name"], ch["provider_type"], model, 502, False, latency, error_detail=err_msg)
            last_status, last_body = 502, {"error": {"message": err_msg}}
            continue

        latency = int((time.perf_counter() - t0) * 1000)
        ok = res.status_code == 200
        db.add_log(
            channel_id=ch["id"],
            channel_name=ch["name"],
            provider_type=ch["provider_type"],
            model=model,
            status_code=res.status_code,
            success=ok,
            latency_ms=latency,
            prompt_tokens=res.prompt_tokens,
            completion_tokens=res.completion_tokens,
            total_tokens=res.total_tokens,
            error_detail=res.error,
        )
        if ok:
            return JSONResponse(status_code=200, content=res.body)
        last_status, last_body = res.status_code, res.body

    return JSONResponse(status_code=last_status, content=last_body)