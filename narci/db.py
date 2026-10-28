"""SQLite 持久层：设备注册表 / 分组 / 场景 / 调度 / 设置。

查询都是毫秒级小操作，用同步 sqlite3 + 互斥锁即可（避免 MVP 阶段引入 aiosqlite）。
"""
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .config import DB_PATH

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """

CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    threshold INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    serial TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'warn',
    kind TEXT NOT NULL,
    message TEXT DEFAULT '',
    acked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS webhook_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    url TEXT DEFAULT '',
    enabled INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    serial TEXT NOT NULL,
    agent_id TEXT DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'mirror',
    started_at REAL NOT NULL,
    ended_at REAL DEFAULT 0,
    duration_sec INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage_counters (
    user_id INTEGER NOT NULL,
    serial TEXT NOT NULL,
    day TEXT NOT NULL,
    control_count INTEGER DEFAULT 0,
    mirror_sec INTEGER DEFAULT 0,
    last_ts REAL DEFAULT 0,
    PRIMARY KEY (user_id, serial, day)
);
CREATE INDEX IF NOT EXISTS idx_usage_serial ON usage_sessions(serial);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_sessions(user_id);

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    secret TEXT DEFAULT '',
    last_seen REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS remote_devices (
    serial TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    model TEXT DEFAULT '',
    android_version TEXT DEFAULT '',
    sdk INTEGER DEFAULT 0,
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    last_seen REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS partitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS partition_members (
    partition_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (partition_id, user_id)
);
CREATE TABLE IF NOT EXISTS device_partitions (
    serial TEXT NOT NULL,
    partition_id INTEGER NOT NULL,
    PRIMARY KEY (serial, partition_id)
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    serial TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS devices (
    serial TEXT PRIMARY KEY,
    model TEXT DEFAULT '',
    android_version TEXT DEFAULT '',
    sdk INTEGER DEFAULT 0,
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    group_id INTEGER,
    notes TEXT DEFAULT '',
    last_seen TEXT DEFAULT '',
    disabled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    repeat_count INTEGER DEFAULT 1,
    steps TEXT NOT NULL DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    scenario_id INTEGER NOT NULL,
    target TEXT NOT NULL DEFAULT '[]',
    kind TEXT NOT NULL DEFAULT 'interval',
    value TEXT NOT NULL DEFAULT '3600',
    enabled INTEGER DEFAULT 1,
    last_run TEXT DEFAULT '',
    next_run REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: Path = DB_PATH) -> None:
    global _conn
    with _lock:
        _conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        _conn.commit()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def execute(sql: str, params: tuple = ()) -> None:
    with _lock:
        _conn.execute(sql, params)
        _conn.commit()


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def query_one(sql: str, params: tuple = ()) -> Optional[dict]:
    rows = query(sql, params)
    return rows[0] if rows else None


# ---------- devices ----------

def upsert_device(serial: str, model: str = "", android_version: str = "",
                  sdk: int = 0, width: int = 0, height: int = 0) -> None:
    execute(
        """INSERT INTO devices(serial, model, android_version, sdk, width, height, last_seen)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(serial) DO UPDATE SET
             model=excluded.model, android_version=excluded.android_version,
             sdk=excluded.sdk, width=excluded.width, height=excluded.height,
             last_seen=excluded.last_seen""",
        (serial, model, android_version, sdk, width, height, _now()),
    )


def touch_device(serial: str) -> None:
    execute("UPDATE devices SET last_seen=? WHERE serial=?", (_now(), serial))


def list_devices(include_disabled: bool = True) -> list[dict]:
    sql = "SELECT * FROM devices"
    if not include_disabled:
        sql += " WHERE disabled=0"
    return query(sql + " ORDER BY serial")


def get_device(serial: str) -> Optional[dict]:
    return query_one("SELECT * FROM devices WHERE serial=?", (serial,))


def update_device(serial: str, **fields) -> None:
    allowed = {"model", "android_version", "sdk", "width", "height", "group_id", "notes", "disabled"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(serial)
    execute(f"UPDATE devices SET {', '.join(sets)} WHERE serial=?", tuple(params))


# ---------- groups ----------

def list_groups() -> list[dict]:
    groups = query("SELECT * FROM groups ORDER BY id")
    for g in groups:
        g["members"] = query(
            "SELECT serial FROM devices WHERE group_id=? AND disabled=0 ORDER BY serial", (g["id"],)
        )
        g["members"] = [m["serial"] for m in g["members"]]
    return groups


def create_group(name: str) -> dict:
    execute("INSERT OR IGNORE INTO groups(name) VALUES(?)", (name,))
    return query_one("SELECT * FROM groups WHERE name=?", (name,))


def delete_group(group_id: int) -> None:
    execute("UPDATE devices SET group_id=NULL WHERE group_id=?", (group_id,))
    execute("DELETE FROM groups WHERE id=?", (group_id,))


def set_group_members(group_id: int, serials: list[str]) -> None:
    execute("UPDATE devices SET group_id=NULL WHERE group_id=?", (group_id,))
    for s in serials:
        execute("UPDATE devices SET group_id=? WHERE serial=?", (group_id, s))


# ---------- scenarios ----------

def list_scenarios() -> list[dict]:
    return query("SELECT * FROM scenarios ORDER BY id")


def get_scenario(scenario_id: int) -> Optional[dict]:
    return query_one("SELECT * FROM scenarios WHERE id=?", (scenario_id,))


def create_scenario(name: str, steps: str, repeat_count: int = 1) -> dict:
    execute(
        "INSERT INTO scenarios(name, steps, repeat_count) VALUES(?,?,?)",
        (name, steps, repeat_count),
    )
    return query_one("SELECT * FROM scenarios WHERE name=?", (name,))


def update_scenario(scenario_id: int, name: Optional[str] = None,
                    steps: Optional[str] = None, repeat_count: Optional[int] = None) -> None:
    sets, params = ["updated_at=datetime('now','localtime')"], []
    if name is not None:
        sets.append("name=?")
        params.append(name)
    if steps is not None:
        sets.append("steps=?")
        params.append(steps)
    if repeat_count is not None:
        sets.append("repeat_count=?")
        params.append(repeat_count)
    params.append(scenario_id)
    execute(f"UPDATE scenarios SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_scenario(scenario_id: int) -> None:
    execute("DELETE FROM scenarios WHERE id=?", (scenario_id,))
    execute("DELETE FROM schedules WHERE scenario_id=?", (scenario_id,))


# ---------- schedules ----------

def list_schedules() -> list[dict]:
    return query("SELECT * FROM schedules ORDER BY id")


def get_schedule(schedule_id: int) -> Optional[dict]:
    return query_one("SELECT * FROM schedules WHERE id=?", (schedule_id,))


def create_schedule(name: str, scenario_id: int, target: str, kind: str,
                    value: str, next_run: float) -> dict:
    execute(
        "INSERT INTO schedules(name, scenario_id, target, kind, value, next_run) VALUES(?,?,?,?,?,?)",
        (name, scenario_id, target, kind, value, next_run),
    )
    return query_one("SELECT * FROM schedules ORDER BY id DESC LIMIT 1", ())


def update_schedule(schedule_id: int, **fields) -> None:
    allowed = {"name", "scenario_id", "target", "kind", "value", "enabled", "last_run", "next_run"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(schedule_id)
    execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_schedule(schedule_id: int) -> None:
    execute("DELETE FROM schedules WHERE id=?", (schedule_id,))


def due_schedules(now: float) -> list[dict]:
    return query("SELECT * FROM schedules WHERE enabled=1 AND next_run<=?", (now,))


# ---------- settings ----------

def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )

# ---------- 用户 / 会话 ----------

def create_user(username: str, password_hash: str, salt: str, role: str = "user") -> dict:
    execute("INSERT INTO users(username, password_hash, salt, role) VALUES(?,?,?,?)",
            (username, password_hash, salt, role))
    return query_one("SELECT id, username, role, created_at FROM users WHERE username=?", (username,))


def get_user_by_name(username: str) -> Optional[dict]:
    return query_one("SELECT * FROM users WHERE username=?", (username,))


def get_user(user_id: int) -> Optional[dict]:
    return query_one("SELECT id, username, role, created_at FROM users WHERE id=?", (user_id,))


def list_users() -> list[dict]:
    return query("SELECT id, username, role, created_at FROM users ORDER BY id")


def delete_user(user_id: int) -> None:
    execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    execute("DELETE FROM partition_members WHERE user_id=?", (user_id,))
    execute("DELETE FROM bookings WHERE user_id=?", (user_id,))
    execute("DELETE FROM users WHERE id=?", (user_id,))


def create_session(token: str, user_id: int, expires_at: float) -> None:
    execute("INSERT INTO sessions(token, user_id, expires_at) VALUES(?,?,?)", (token, user_id, expires_at))


def get_session(token: str) -> Optional[dict]:
    return query_one("SELECT * FROM sessions WHERE token=? AND expires_at>?", (token, time.time()))


def delete_session(token: str) -> None:
    execute("DELETE FROM sessions WHERE token=?", (token,))


def delete_user_sessions(user_id: int) -> None:
    execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


# ---------- 分区 ----------

def create_partition(name: str, description: str = "") -> dict:
    execute("INSERT OR IGNORE INTO partitions(name, description) VALUES(?,?)", (name, description))
    return query_one("SELECT * FROM partitions WHERE name=?", (name,))


def list_partitions() -> list[dict]:
    parts = query("SELECT * FROM partitions ORDER BY id")
    for p in parts:
        p["members"] = [r["user_id"] for r in query(
            "SELECT user_id FROM partition_members WHERE partition_id=?", (p["id"],))]
        p["devices"] = [r["serial"] for r in query(
            "SELECT serial FROM device_partitions WHERE partition_id=?", (p["id"],))]
    return parts


def delete_partition(partition_id: int) -> None:
    execute("DELETE FROM partition_members WHERE partition_id=?", (partition_id,))
    execute("DELETE FROM device_partitions WHERE partition_id=?", (partition_id,))
    execute("DELETE FROM partitions WHERE id=?", (partition_id,))


def set_partition_members(partition_id: int, user_ids: list[int]) -> None:
    execute("DELETE FROM partition_members WHERE partition_id=?", (partition_id,))
    for uid in user_ids:
        execute("INSERT OR IGNORE INTO partition_members(partition_id, user_id) VALUES(?,?)", (partition_id, uid))


def set_partition_devices(partition_id: int, serials: list[str]) -> None:
    execute("DELETE FROM device_partitions WHERE partition_id=?", (partition_id,))
    for s in serials:
        execute("INSERT OR IGNORE INTO device_partitions(serial, partition_id) VALUES(?,?)", (s, partition_id))


def user_partitions(user_id: int) -> list[int]:
    return [r["partition_id"] for r in query(
        "SELECT partition_id FROM partition_members WHERE user_id=?", (user_id,))]


def partition_devices(partition_id: int) -> list[str]:
    return [r["serial"] for r in query(
        "SELECT serial FROM device_partitions WHERE partition_id=?", (partition_id,))]


# ---------- 预约 ----------

def create_booking(user_id: int, serial: str, start_ts: float, end_ts: float, note: str = "") -> dict:
    execute("INSERT INTO bookings(user_id, serial, start_ts, end_ts, note) VALUES(?,?,?,?,?)",
            (user_id, serial, start_ts, end_ts, note))
    return query_one("SELECT * FROM bookings ORDER BY id DESC LIMIT 1", ())


def list_bookings(user_id: Optional[int] = None) -> list[dict]:
    if user_id is None:
        rows = query("SELECT * FROM bookings ORDER BY start_ts DESC")
    else:
        rows = query("SELECT * FROM bookings WHERE user_id=? ORDER BY start_ts DESC", (user_id,))
    for r in rows:
        u = get_user(r["user_id"])
        r["username"] = u["username"] if u else "?"
    return rows


def delete_booking(booking_id: int, user_id: Optional[int] = None) -> None:
    if user_id is None:
        execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    else:
        execute("DELETE FROM bookings WHERE id=? AND user_id=?", (booking_id, user_id))


def active_booking(serial: str, now: Optional[float] = None) -> Optional[dict]:
    now = now or time.time()
    row = query_one("SELECT * FROM bookings WHERE serial=? AND start_ts<=? AND end_ts>=? "
                    "ORDER BY end_ts LIMIT 1", (serial, now, now))
    if row:
        u = get_user(row["user_id"])
        row["username"] = u["username"] if u else "?"
    return row

# ---------- 分布式: Agent / 远程设备 ----------

def upsert_agent(agent_id: str, name: str, url: str, secret: str = "") -> None:
    import time
    execute("INSERT INTO agents(agent_id, name, url, secret, last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET name=excluded.name, url=excluded.url, "
            "secret=excluded.secret, last_seen=excluded.last_seen",
            (agent_id, name, url, secret, time.time()))


def list_agents() -> list[dict]:
    return query("SELECT * FROM agents ORDER BY last_seen DESC")


def delete_agent(agent_id: str) -> None:
    execute("DELETE FROM remote_devices WHERE agent_id=?", (agent_id,))
    execute("DELETE FROM agents WHERE agent_id=?", (agent_id,))


def upsert_remote_devices(agent_id: str, devices: list[dict]) -> None:
    import time
    seen = set()
    for d in devices:
        serial = d.get("serial", "")
        if not serial:
            continue
        seen.add(serial)
        execute("INSERT INTO remote_devices(serial, agent_id, model, android_version, sdk, width, height, last_seen) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(serial) DO UPDATE SET agent_id=excluded.agent_id, "
                "model=excluded.model, android_version=excluded.android_version, sdk=excluded.sdk, "
                "width=excluded.width, height=excluded.height, last_seen=excluded.last_seen",
                (serial, agent_id, d.get("model", ""), d.get("android_version", ""), d.get("sdk", 0),
                 d.get("width", 0), d.get("height", 0), time.time()))
    # 清理该 agent 已下线的设备
    for row in query("SELECT serial FROM remote_devices WHERE agent_id=?", (agent_id,)):
        if row["serial"] not in seen:
            execute("DELETE FROM remote_devices WHERE serial=?", (row["serial"],))


def list_remote_devices() -> list[dict]:
    return query("SELECT * FROM remote_devices ORDER BY agent_id, serial")


def get_remote_device(serial: str) -> Optional[dict]:
    return query_one("SELECT * FROM remote_devices WHERE serial=?", (serial,))


def agent_of(serial: str) -> Optional[dict]:
    row = query_one("SELECT a.* FROM agents a JOIN remote_devices r ON r.agent_id=a.agent_id "
                    "WHERE r.serial=?", (serial,))
    return row

# ---------- 使用统计 ----------

def start_usage_session(user_id: int, username: str, serial: str, agent_id: str = "",
                        kind: str = "mirror") -> int:
    import time
    execute("INSERT INTO usage_sessions(user_id, username, serial, agent_id, kind, started_at) "
            "VALUES(?,?,?,?,?,?)", (user_id, username, serial, agent_id, kind, time.time()))
    return query_one("SELECT id FROM usage_sessions ORDER BY id DESC LIMIT 1", ())["id"]


def end_usage_session(session_id: int) -> None:
    import time
    row = query_one("SELECT * FROM usage_sessions WHERE id=?", (session_id,))
    if not row or row["ended_at"]:
        return
    dur = max(0, int(time.time() - row["started_at"]))
    execute("UPDATE usage_sessions SET ended_at=?, duration_sec=? WHERE id=?",
            (time.time(), dur, session_id))
    # 同步到按天聚合
    add_usage_counter(row["user_id"], row["serial"], int(dur))


def add_usage_counter(user_id: int, serial: str, mirror_sec: int = 0, control: int = 0) -> None:
    import time
    day = time.strftime("%Y-%m-%d")
    execute("INSERT INTO usage_counters(user_id, serial, day, control_count, mirror_sec, last_ts) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id, serial, day) DO UPDATE SET "
            "control_count=control_count+excluded.control_count, "
            "mirror_sec=mirror_sec+excluded.mirror_sec, last_ts=excluded.last_ts",
            (user_id, serial, day, control, mirror_sec, time.time()))


def usage_summary() -> dict:
    """设备利用率 + 用户使用 + 总览。"""
    # 合并本地+远程设备, 按序列号去重(本地优先)
    seen = {}
    for row in query("SELECT serial, model FROM devices UNION ALL SELECT serial, model FROM remote_devices"):
        if row["serial"] not in seen:
            seen[row["serial"]] = row["model"]
    serials = list(seen.keys())
    per_device = []
    for s in serials:
        row = query_one("SELECT COUNT(*) AS sessions, COALESCE(SUM(duration_sec),0) AS total_sec "
                        "FROM usage_sessions WHERE serial=?", (s,))
        cnt = query_one("SELECT COALESCE(SUM(control_count),0) AS c FROM usage_counters WHERE serial=?", (s,))
        last = query_one("SELECT MAX(started_at) AS last FROM usage_sessions WHERE serial=?", (s,))
        who = query_one("SELECT username, COUNT(*) AS n FROM usage_sessions WHERE serial=? "
                        "GROUP BY username ORDER BY n DESC LIMIT 1", (s,))
        per_device.append({"serial": s, "sessions": row["sessions"] or 0,
                           "total_sec": row["total_sec"] or 0,
                           "control_count": cnt["c"] or 0,
                           "last_used": last["last"] or 0,
                           "top_user": who["username"] if who else ""})
    per_device.sort(key=lambda d: -d["total_sec"])
    users = []
    for u in query("SELECT id, username, role FROM users"):
        row = query_one("SELECT COUNT(DISTINCT serial) AS devs, COALESCE(SUM(duration_sec),0) AS sec "
                        "FROM usage_sessions WHERE user_id=?", (u["id"],))
        users.append({"username": u["username"], "role": u["role"],
                      "devices": row["devs"] or 0, "total_sec": row["sec"] or 0})
    users.sort(key=lambda x: -x["total_sec"])
    totals = query_one("SELECT COUNT(*) AS sessions, COALESCE(SUM(duration_sec),0) AS sec "
                       "FROM usage_sessions", ())
    today = query_one("SELECT COUNT(*) AS n FROM usage_sessions WHERE started_at>=?",
                      (time.time() - 86400,)) if False else None
    import time as _t
    today = query_one("SELECT COUNT(*) AS sessions, COALESCE(SUM(duration_sec),0) AS sec "
                      "FROM usage_sessions WHERE started_at>=?", (_t.time() - 86400,))
    return {"per_device": per_device, "per_user": users,
            "total_sessions": totals["sessions"] or 0, "total_sec": totals["sec"] or 0,
            "today_sessions": today["sessions"] or 0, "today_sec": today["sec"] or 0}


def recent_usage(limit: int = 30) -> list[dict]:
    rows = query("SELECT * FROM usage_sessions ORDER BY started_at DESC LIMIT ?", (limit,))
    return [{**r, "started_at": r["started_at"]} for r in rows]

# ---------- 监控告警 ----------

def create_alert_rule(name: str, kind: str, threshold: int = 0) -> dict:
    execute("INSERT INTO alert_rules(name, kind, threshold) VALUES(?,?,?)", (name, kind, int(threshold)))
    return query_one("SELECT * FROM alert_rules ORDER BY id DESC LIMIT 1", ())


def list_alert_rules() -> list[dict]:
    return query("SELECT * FROM alert_rules ORDER BY id")


def delete_alert_rule(rule_id: int) -> None:
    execute("DELETE FROM alert_rules WHERE id=?", (rule_id,))


def list_alerts(limit: int = 100, acked: Optional[bool] = None) -> list[dict]:
    if acked is None:
        return query("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,))
    return query("SELECT * FROM alerts WHERE acked=? ORDER BY ts DESC LIMIT ?",
                 (1 if acked else 0, limit))


def create_alert(serial: str, level: str, kind: str, message: str) -> dict:
    import time
    execute("INSERT INTO alerts(ts, serial, level, kind, message) VALUES(?,?,?,?,?)",
            (time.time(), serial, level, kind, message))
    return query_one("SELECT * FROM alerts ORDER BY id DESC LIMIT 1", ())


def ack_alert(alert_id: int) -> None:
    execute("UPDATE alerts SET acked=1 WHERE id=?", (alert_id,))


def recent_alerts_for(serial: str, kind: str, since: float) -> list[dict]:
    """查询某设备某类型在最近 since 秒内是否已告警(用于静默)。"""
    return query("SELECT * FROM alerts WHERE serial=? AND kind=? AND ts>=? AND acked=0",
                 (serial, kind, since))


def get_webhook() -> dict:
    row = query_one("SELECT * FROM webhook_config WHERE id=1", ())
    return row or {"url": "", "enabled": 0}


def set_webhook(url: str, enabled: bool) -> None:
    execute("INSERT INTO webhook_config(id, url, enabled) VALUES(1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET url=excluded.url, enabled=excluded.enabled",
            (url, 1 if enabled else 0))

