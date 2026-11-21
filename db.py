import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "hub.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _cycle_key(cycle: str) -> str:
    now = datetime.now(timezone.utc)
    if cycle == "daily":
        return now.strftime("%Y-%m-%d")
    if cycle == "monthly":
        return now.strftime("%Y-%m")
    return ""


def _mask_secret(val: str) -> str:
    val = (val or "").strip()
    if not val:
        return ""
    first = val.splitlines()[0].strip()
    if len(first) <= 8:
        return first[:2] + "••••"
    return f"{first[:4]}••••{first[-4:]}"


def _extract_credential_hint(config: Dict[str, Any]) -> str:
    for k in ("refresh_token", "api_keys", "api_key", "token"):
        v = str(config.get(k) or "").strip()
        if v:
            lines = [x.strip() for x in v.replace(",", "\n").splitlines() if x.strip()]
            hint = _mask_secret(lines[0])
            if len(lines) > 1:
                return f"{hint} 等 {len(lines)} 把"
            return hint
    base = str(config.get("base_url") or "").strip()
    if base:
        return base.replace("https://", "").replace("http://", "")[:28]
    return "未填写凭证"


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    if "$" not in stored:
        return False
    salt, expected = stored.split("$", 1)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000).hex()
    return secrets.compare_digest(actual, expected)


@contextmanager
def get_conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, ddl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db() -> Dict[str, str]:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 10,
                models TEXT NOT NULL DEFAULT '',
                config_json TEXT NOT NULL DEFAULT '{}',
                tier TEXT NOT NULL DEFAULT '',
                quota_limit INTEGER NOT NULL DEFAULT 0,
                quota_used INTEGER NOT NULL DEFAULT 0,
                quota_cycle TEXT NOT NULL DEFAULT 'none',
                quota_reset_key TEXT NOT NULL DEFAULT '',
                last_status TEXT DEFAULT '',
                last_latency_ms INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                last_checked_at TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                channel_id INTEGER,
                channel_name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                model TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                success INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                error_detail TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_logs_created ON request_logs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_logs_success ON request_logs(success);
            """
        )

        # Safe migration for existing databases
        _ensure_column(conn, "channels", "tier", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "channels", "quota_limit", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "channels", "quota_used", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "channels", "quota_cycle", "TEXT NOT NULL DEFAULT 'none'")
        _ensure_column(conn, "channels", "quota_reset_key", "TEXT NOT NULL DEFAULT ''")

        created_info: Dict[str, str] = {}
        if not conn.execute("SELECT value FROM settings WHERE key='admin_password_hash'").fetchone():
            default_pwd = os.getenv("ADMIN_PASSWORD", "admin123456")
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("admin_password_hash", hash_password(default_pwd)),
            )
            created_info["initial_admin_password"] = default_pwd

        if not conn.execute("SELECT value FROM settings WHERE key='proxy_api_key'").fetchone():
            default_key = os.getenv("PROXY_API_KEY", f"sk-77api-{secrets.token_urlsafe(24)}")
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("proxy_api_key", default_key),
            )
            created_info["initial_proxy_api_key"] = default_key

        if not conn.execute("SELECT value FROM settings WHERE key='session_secret'").fetchone():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("session_secret", secrets.token_hex(32)),
            )

        if not conn.execute("SELECT value FROM settings WHERE key='auto_failover'").fetchone():
            conn.execute("INSERT INTO settings(key, value) VALUES ('auto_failover', '1')")

        return created_info


def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def _hydrate_channel(r: sqlite3.Row, conn: sqlite3.Connection) -> Dict[str, Any]:
    item = dict(r)
    item["enabled"] = bool(item["enabled"])
    item["config"] = json.loads(item.pop("config_json") or "{}")
    item["model_list"] = [m.strip() for m in (item["models"] or "").split(",") if m.strip()]
    item["credential_hint"] = _extract_credential_hint(item["config"])

    # Auto-reset quota counter if cycle rolled over
    cycle = item.get("quota_cycle") or "none"
    cur_key = _cycle_key(cycle)
    if cur_key and item.get("quota_reset_key") != cur_key:
        item["quota_used"] = 0
        item["quota_reset_key"] = cur_key
        conn.execute(
            "UPDATE channels SET quota_used=0, quota_reset_key=? WHERE id=?",
            (cur_key, item["id"]),
        )
    return item


def list_channels(only_enabled: bool = False) -> List[Dict[str, Any]]:
    query = "SELECT * FROM channels"
    if only_enabled:
        query += " WHERE enabled = 1"
    query += " ORDER BY priority DESC, id ASC"
    with get_conn() as conn:
        rows = conn.execute(query).fetchall()
        return [_hydrate_channel(r, conn) for r in rows]


def get_channel(channel_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        if not r:
            return None
        return _hydrate_channel(r, conn)


def create_channel(
    name: str,
    provider_type: str,
    config: Dict[str, Any],
    models: str = "",
    enabled: bool = True,
    priority: int = 10,
    tier: str = "",
    quota_limit: int = 0,
    quota_used: int = 0,
    quota_cycle: str = "none",
) -> int:
    now = _utc_now()
    reset_key = _cycle_key(quota_cycle)
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO channels (
                name, provider_type, enabled, priority, models, config_json,
                tier, quota_limit, quota_used, quota_cycle, quota_reset_key,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                provider_type,
                1 if enabled else 0,
                priority,
                models,
                json.dumps(config, ensure_ascii=False),
                tier.strip(),
                max(0, int(quota_limit or 0)),
                max(0, int(quota_used or 0)),
                quota_cycle or "none",
                reset_key,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def update_channel(
    channel_id: int,
    name: Optional[str] = None,
    provider_type: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    models: Optional[str] = None,
    enabled: Optional[bool] = None,
    priority: Optional[int] = None,
    tier: Optional[str] = None,
    quota_limit: Optional[int] = None,
    quota_used: Optional[int] = None,
    quota_cycle: Optional[str] = None,
) -> bool:
    existing = get_channel(channel_id)
    if not existing:
        return False
    now = _utc_now()
    new_cycle = quota_cycle if quota_cycle is not None else existing.get("quota_cycle", "none")
    reset_key = _cycle_key(new_cycle) if quota_cycle is not None else existing.get("quota_reset_key", "")
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE channels
            SET name=?, provider_type=?, enabled=?, priority=?, models=?, config_json=?,
                tier=?, quota_limit=?, quota_used=?, quota_cycle=?, quota_reset_key=?, updated_at=?
            WHERE id=?
            """,
            (
                name if name is not None else existing["name"],
                provider_type if provider_type is not None else existing["provider_type"],
                1 if (enabled if enabled is not None else existing["enabled"]) else 0,
                priority if priority is not None else existing["priority"],
                models if models is not None else existing["models"],
                json.dumps(config if config is not None else existing["config"], ensure_ascii=False),
                tier.strip() if tier is not None else existing.get("tier", ""),
                max(0, int(quota_limit)) if quota_limit is not None else int(existing.get("quota_limit", 0)),
                max(0, int(quota_used)) if quota_used is not None else int(existing.get("quota_used", 0)),
                new_cycle,
                reset_key,
                now,
                channel_id,
            ),
        )
        return True


def increment_channel_quota(channel_id: Optional[int], delta: int = 1) -> None:
    if not channel_id:
        return
    ch = get_channel(channel_id)
    if not ch:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE channels SET quota_used = quota_used + ? WHERE id=?",
            (delta, channel_id),
        )


def update_channel_health(channel_id: int, status: str, latency_ms: int, error: str = "") -> None:
    now = _utc_now()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE channels
            SET last_status=?, last_latency_ms=?, last_error=?, last_checked_at=?
            WHERE id=?
            """,
            (status, latency_ms, error[:1000], now, channel_id),
        )


def delete_channel(channel_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))
        return cur.rowcount > 0


def add_log(
    channel_id: Optional[int],
    channel_name: str,
    provider_type: str,
    model: str,
    status_code: int,
    success: bool,
    latency_ms: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    error_detail: str = "",
) -> None:
    if total_tokens == 0 and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO request_logs
            (created_at, channel_id, channel_name, provider_type, model, status_code, success,
             latency_ms, prompt_tokens, completion_tokens, total_tokens, error_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now(),
                channel_id,
                channel_name,
                provider_type,
                model,
                status_code,
                1 if success else 0,
                latency_ms,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                (error_detail or "")[:4000],
            ),
        )
        conn.execute(
            """
            DELETE FROM request_logs
            WHERE id NOT IN (SELECT id FROM request_logs ORDER BY id DESC LIMIT 2000)
            """
        )
    if success and channel_id:
        increment_channel_quota(channel_id, 1)


def list_logs(limit: int = 80, only_errors: bool = False) -> List[Dict[str, Any]]:
    query = "SELECT * FROM request_logs"
    if only_errors:
        query += " WHERE success = 0"
    query += " ORDER BY id DESC LIMIT ?"
    with get_conn() as conn:
        rows = conn.execute(query, (limit,)).fetchall()
        res = []
        for r in rows:
            item = dict(r)
            item["success"] = bool(item["success"])
            res.append(item)
        return res


def clear_logs() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM request_logs")


def get_stats() -> Dict[str, Any]:
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_conn() as conn:
        total = conn.execute(
            """
            SELECT
                COUNT(*) AS total_requests,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_requests,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
            FROM request_logs
            """
        ).fetchone()

        today = conn.execute(
            """
            SELECT
                COUNT(*) AS today_requests,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS today_success,
                COALESCE(SUM(total_tokens), 0) AS today_tokens
            FROM request_logs
            WHERE created_at LIKE ?
            """,
            (f"{today_prefix}%",),
        ).fetchone()

        by_channel = conn.execute(
            """
            SELECT
                channel_name,
                provider_type,
                COUNT(*) AS requests,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successes,
                COALESCE(SUM(total_tokens), 0) AS tokens,
                COALESCE(ROUND(AVG(latency_ms)), 0) AS avg_latency_ms
            FROM request_logs
            GROUP BY channel_name, provider_type
            ORDER BY requests DESC
            """
        ).fetchall()

        total_req = int(total["total_requests"] or 0)
        succ_req = int(total["success_requests"] or 0)
        return {
            "total_requests": total_req,
            "success_requests": succ_req,
            "success_rate": round((succ_req / total_req) * 100, 1) if total_req > 0 else 100.0,
            "prompt_tokens": int(total["prompt_tokens"] or 0),
            "completion_tokens": int(total["completion_tokens"] or 0),
            "total_tokens": int(total["total_tokens"] or 0),
            "avg_latency_ms": int(round(total["avg_latency_ms"] or 0)),
            "today_requests": int(today["today_requests"] or 0),
            "today_success": int(today["today_success"] or 0),
            "today_tokens": int(today["today_tokens"] or 0),
            "by_channel": [dict(r) for r in by_channel],
        }