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

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    name TEXT NOT NULL,
    username TEXT DEFAULT '',
    avatar_url TEXT DEFAULT '',
    api_token TEXT DEFAULT '',
    api_base TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS account_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    serial TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'text',
    text TEXT DEFAULT '',
    media_path TEXT DEFAULT '',
    status TEXT DEFAULT 'ready',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS distribution_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    scheduled_at REAL NOT NULL,
    status TEXT DEFAULT 'pending',
    published_at REAL DEFAULT 0,
    platform_post_id TEXT DEFAULT '',
    error TEXT DEFAULT '',
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    scenario_id INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS account_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    followers INTEGER DEFAULT 0,
    following INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    views INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS device_tags (
    serial TEXT NOT NULL,
    tag_key TEXT NOT NULL,
    tag_value TEXT DEFAULT '',
    PRIMARY KEY (serial, tag_key)
);

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

CREATE TABLE IF NOT EXISTS content_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    title_template TEXT DEFAULT '',
    text_template TEXT DEFAULT '',
    variables TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS interaction_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'comment',
    author_name TEXT DEFAULT '',
    author_id TEXT DEFAULT '',
    content TEXT DEFAULT '',
    post_title TEXT DEFAULT '',
    post_id TEXT DEFAULT '',
    status TEXT DEFAULT 'unread',
    replied INTEGER DEFAULT 0,
    reply_text TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_interaction_account ON interaction_messages(account_id);
CREATE INDEX IF NOT EXISTS idx_interaction_status ON interaction_messages(status);

CREATE TABLE IF NOT EXISTS reply_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    account_id INTEGER DEFAULT 0,
    platform TEXT DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'comment',
    match_type TEXT NOT NULL DEFAULT 'keyword',
    keywords TEXT DEFAULT '[]',
    reply_text TEXT NOT NULL DEFAULT '',
    priority INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS competitor_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    platform TEXT DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'rss',
    url TEXT NOT NULL DEFAULT '',
    api_key TEXT DEFAULT '',
    config TEXT DEFAULT '{}',
    collect_interval REAL DEFAULT 3600,
    enabled INTEGER DEFAULT 1,
    last_collect REAL DEFAULT 0,
    total_collected INTEGER DEFAULT 0,
    last_error TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS competitor_contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    platform TEXT DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    content TEXT DEFAULT '',
    url TEXT DEFAULT '',
    author TEXT DEFAULT '',
    metrics TEXT DEFAULT '{}',
    content_hash TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    status TEXT DEFAULT 'new',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (source_id) REFERENCES competitor_sources(id)
);
CREATE INDEX IF NOT EXISTS idx_cc_source ON competitor_contents(source_id);
CREATE INDEX IF NOT EXISTS idx_cc_hash ON competitor_contents(content_hash);
CREATE INDEX IF NOT EXISTS idx_cc_status ON competitor_contents(status);

CREATE TABLE IF NOT EXISTS ab_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    content_id INTEGER NOT NULL DEFAULT 0,
    status TEXT DEFAULT 'draft',
    winner_variant_id INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    started_at REAL DEFAULT 0,
    ended_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ab_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id INTEGER NOT NULL,
    variant_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    text TEXT DEFAULT '',
    media_path TEXT DEFAULT '',
    account_id INTEGER NOT NULL DEFAULT 0,
    task_id INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    views INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0,
    followers_gained INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (test_id) REFERENCES ab_tests(id)
);
CREATE INDEX IF NOT EXISTS idx_abv_test ON ab_variants(test_id);

CREATE TABLE IF NOT EXISTS relay_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    device_order TEXT NOT NULL DEFAULT '[]',
    status TEXT DEFAULT 'idle',
    current_device_idx INTEGER DEFAULT 0,
    run_id TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
"""


def connect(db_path: Path = DB_PATH) -> None:
    global _conn
    with _lock:
        _conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        _migrate(_conn)
        _conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """增量迁移：为旧库添加新列。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(distribution_tasks)").fetchall()}
    if "retry_count" not in cols:
        conn.execute("ALTER TABLE distribution_tasks ADD COLUMN retry_count INTEGER DEFAULT 0")
    if "max_retries" not in cols:
        conn.execute("ALTER TABLE distribution_tasks ADD COLUMN max_retries INTEGER DEFAULT 3")
    if "scenario_id" not in cols:
        conn.execute("ALTER TABLE distribution_tasks ADD COLUMN scenario_id INTEGER DEFAULT 0")

    # 互动消息表增加情感分析列
    im_cols = {r["name"] for r in conn.execute("PRAGMA table_info(interaction_messages)").fetchall()}
    if "sentiment" not in im_cols:
        conn.execute("ALTER TABLE interaction_messages ADD COLUMN sentiment TEXT DEFAULT 'unknown'")
    if "sentiment_score" not in im_cols:
        conn.execute("ALTER TABLE interaction_messages ADD COLUMN sentiment_score REAL DEFAULT 0")
    if "sentiment_detail" not in im_cols:
        conn.execute("ALTER TABLE interaction_messages ADD COLUMN sentiment_detail TEXT DEFAULT ''")

    # 分发任务增加 A/B 测试列
    dt_cols = {r["name"] for r in conn.execute("PRAGMA table_info(distribution_tasks)").fetchall()}
    if "ab_variant_id" not in dt_cols:
        conn.execute("ALTER TABLE distribution_tasks ADD COLUMN ab_variant_id INTEGER DEFAULT 0")
    if "content_snapshot" not in dt_cols:
        conn.execute("ALTER TABLE distribution_tasks ADD COLUMN content_snapshot TEXT DEFAULT ''")


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

# ---------- 运营: 账号 / 内容 / 分发 / 指标 / 设备标签 ----------

def create_account(platform: str, name: str, username: str = "", api_token: str = "",
                   api_base: str = "", avatar_url: str = "") -> dict:
    execute("INSERT INTO accounts(platform, name, username, api_token, api_base, avatar_url) "
            "VALUES(?,?,?,?,?,?)", (platform, name, username, api_token, api_base, avatar_url))
    return query_one("SELECT * FROM accounts ORDER BY id DESC LIMIT 1", ())


def list_accounts() -> list[dict]:
    return query("SELECT * FROM accounts ORDER BY id")


def get_account(account_id: int) -> Optional[dict]:
    return query_one("SELECT * FROM accounts WHERE id=?", (account_id,))


def update_account(account_id: int, **fields) -> None:
    allowed = {"platform", "name", "username", "api_token", "api_base", "avatar_url", "status"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(account_id)
    execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_account(account_id: int) -> None:
    execute("DELETE FROM account_bindings WHERE account_id=?", (account_id,))
    execute("DELETE FROM distribution_tasks WHERE account_id=?", (account_id,))
    execute("DELETE FROM account_metrics WHERE account_id=?", (account_id,))
    execute("DELETE FROM accounts WHERE id=?", (account_id,))


def bind_account(account_id: int, serial: str) -> None:
    execute("INSERT OR IGNORE INTO account_bindings(account_id, serial) VALUES(?,?)", (account_id, serial))


def account_devices(account_id: int) -> list[str]:
    return [r["serial"] for r in query("SELECT serial FROM account_bindings WHERE account_id=?", (account_id,))]


def accounts_on_device(serial: str) -> list[dict]:
    return query("SELECT a.* FROM accounts a JOIN account_bindings b ON b.account_id=a.id WHERE b.serial=?", (serial,))


def create_content(title: str, type: str = "text", text: str = "", media_path: str = "") -> dict:
    execute("INSERT INTO contents(title, type, text, media_path) VALUES(?,?,?,?)",
            (title, type, text, media_path))
    return query_one("SELECT * FROM contents ORDER BY id DESC LIMIT 1", ())


def list_contents() -> list[dict]:
    return query("SELECT * FROM contents ORDER BY id DESC")


def get_content(content_id: int) -> Optional[dict]:
    return query_one("SELECT * FROM contents WHERE id=?", (content_id,))


def update_content(content_id: int, **fields) -> None:
    allowed = {"title", "type", "text", "media_path", "status"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(content_id)
    execute(f"UPDATE contents SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_content(content_id: int) -> None:
    execute("DELETE FROM distribution_tasks WHERE content_id=?", (content_id,))
    execute("DELETE FROM contents WHERE id=?", (content_id,))


def create_distribution_task(content_id: int, account_id: int, scheduled_at: float,
                             max_retries: int = 3) -> dict:
    import time
    execute("INSERT INTO distribution_tasks(content_id, account_id, scheduled_at, max_retries) "
            "VALUES(?,?,?,?)", (content_id, account_id, scheduled_at, max_retries))
    return query_one("SELECT * FROM distribution_tasks ORDER BY id DESC LIMIT 1", ())


def create_distribution_tasks_batch(content_id: int, account_ids: list[int],
                                    scheduled_at: float, interval_minutes: float = 0,
                                    max_retries: int = 3) -> list[dict]:
    """批量创建分发任务：一条内容 → N 个账号，可配置错峰间隔。"""
    import time
    results = []
    for i, account_id in enumerate(account_ids):
        offset = interval_minutes * 60 * i  # 每个账号递增间隔
        task_time = scheduled_at + offset
        execute("INSERT INTO distribution_tasks(content_id, account_id, scheduled_at, max_retries) "
                "VALUES(?,?,?,?)", (content_id, account_id, task_time, max_retries))
        task = query_one("SELECT * FROM distribution_tasks ORDER BY id DESC LIMIT 1", ())
        if task:
            results.append(task)
    return results


def list_distribution_tasks(limit: int = 100) -> list[dict]:
    rows = query("SELECT * FROM distribution_tasks ORDER BY scheduled_at DESC LIMIT ?", (limit,))
    for r in rows:
        c = query_one("SELECT * FROM contents WHERE id=?", (r["content_id"],))
        a = get_account(r["account_id"])
        r["content_title"] = c["title"] if c else ""
        r["account_name"] = a["name"] if a else ""
        r["platform"] = a["platform"] if a else ""
    return rows


def due_distribution_tasks(now: Optional[float] = None) -> list[dict]:
    """到期任务 + 可重试的失败任务。"""
    now = now or time.time()
    pending = query("SELECT * FROM distribution_tasks WHERE status='pending' AND scheduled_at<=? "
                    "ORDER BY scheduled_at LIMIT 50", (now,))
    retryable = query("SELECT * FROM distribution_tasks WHERE status='failed' "
                      "AND retry_count < max_retries AND scheduled_at<=? "
                      "ORDER BY scheduled_at LIMIT 20", (now,))
    return pending + retryable


def update_distribution_task(task_id: int, **fields) -> None:
    allowed = {"status", "published_at", "platform_post_id", "error", "retry_count", "max_retries", "scheduled_at"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(task_id)
    execute(f"UPDATE distribution_tasks SET {', '.join(sets)} WHERE id=?", tuple(params))


def cancel_distribution_task(task_id: int) -> None:
    """取消待执行的任务。"""
    execute("UPDATE distribution_tasks SET status='cancelled' WHERE id=? AND status='pending'",
            (task_id,))


def retry_distribution_task(task_id: int) -> None:
    """手动重试失败的任务：重置状态并推后执行时间。"""
    import time
    execute("UPDATE distribution_tasks SET status='pending', scheduled_at=?, error='' "
            "WHERE id=? AND status='failed'", (time.time(), task_id))


def record_metric(account_id: int, ts: float, followers: int = 0, following: int = 0,
                  likes: int = 0, comments: int = 0, views: int = 0) -> None:
    import time
    execute("INSERT INTO account_metrics(account_id, ts, followers, following, likes, comments, views) "
            "VALUES(?,?,?,?,?,?,?)", (account_id, ts, followers, following, likes, comments, views))


def metrics_summary() -> dict:
    accounts = query("SELECT a.id, a.name, a.platform, a.username, "
                     "COALESCE(m.followers,0) AS followers, COALESCE(m.following,0) AS following, "
                     "COALESCE(m.likes,0) AS likes, COALESCE(m.comments,0) AS comments, COALESCE(m.views,0) AS views "
                     "FROM accounts a LEFT JOIN "
                     "(SELECT account_id, MAX(ts) AS latest FROM account_metrics GROUP BY account_id) latest "
                     "ON latest.account_id=a.id "
                     "LEFT JOIN account_metrics m ON m.account_id=latest.account_id AND m.ts=latest.latest "
                     "ORDER BY a.id")
    history = query("SELECT ts, account_id, followers, likes, comments, views FROM account_metrics "
                    "ORDER BY ts DESC LIMIT 200")
    return {"accounts": accounts, "history": history}


# ---------- 阶段二: 数据看板 / 分发分析 / 互动管理 / 模板 ----------

def metrics_range(account_id: int, start_ts: float, end_ts: float,
                  granularity: str = "day") -> list[dict]:
    """按时间粒度聚合指标（day/week/month）。"""
    if granularity == "day":
        fmt = "%Y-%m-%d"
    elif granularity == "week":
        fmt = "%Y-W%W"
    else:
        fmt = "%Y-%m"
    return query(
        f"SELECT strftime(?, ts, 'unixepoch') AS period, "
        f"MAX(followers) AS followers, MAX(likes) AS likes, "
        f"MAX(comments) AS comments, MAX(views) AS views "
        f"FROM account_metrics WHERE account_id=? AND ts>=? AND ts<=? "
        f"GROUP BY period ORDER BY period",
        (fmt, account_id, start_ts, end_ts))


def distribution_analytics(days: int = 30) -> dict:
    """分发统计：成功率、平台维度、时段效果。"""
    import time
    since = time.time() - days * 86400
    total = query_one(
        "SELECT COUNT(*) AS cnt FROM distribution_tasks WHERE scheduled_at>=?", (since,))
    success = query_one(
        "SELECT COUNT(*) AS cnt FROM distribution_tasks WHERE status='published' AND scheduled_at>=?",
        (since,))
    failed = query_one(
        "SELECT COUNT(*) AS cnt FROM distribution_tasks WHERE status='failed' AND scheduled_at>=?",
        (since,))
    by_platform = query(
        "SELECT a.platform, COUNT(*) AS total, "
        "SUM(CASE WHEN dt.status='published' THEN 1 ELSE 0 END) AS ok "
        "FROM distribution_tasks dt JOIN accounts a ON a.id=dt.account_id "
        "WHERE dt.scheduled_at>=? GROUP BY a.platform", (since,))
    by_hour = query(
        "SELECT CAST(strftime('%H', scheduled_at, 'unixepoch', 'localtime') AS INTEGER) AS hr, "
        "COUNT(*) AS cnt, "
        "SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) AS ok "
        "FROM distribution_tasks WHERE scheduled_at>=? GROUP BY hr ORDER BY hr", (since,))
    return {
        "period_days": days,
        "total": total["cnt"] if total else 0,
        "success": success["cnt"] if success else 0,
        "failed": failed["cnt"] if failed else 0,
        "success_rate": round(success["cnt"] / total["cnt"] * 100, 1) if total and total["cnt"] else 0,
        "by_platform": by_platform,
        "by_hour": by_hour,
    }


# --- 互动消息 ---

def create_interaction(account_id: int, platform: str, kind: str, content: str,
                       author_name: str = "", author_id: str = "",
                       post_title: str = "", post_id: str = "") -> dict:
    execute("INSERT INTO interaction_messages(account_id, platform, kind, content, "
            "author_name, author_id, post_title, post_id) VALUES(?,?,?,?,?,?,?,?)",
            (account_id, platform, kind, content, author_name, author_id, post_title, post_id))
    return query_one("SELECT * FROM interaction_messages ORDER BY id DESC LIMIT 1", ())


def list_interactions(account_id: int = 0, status: str = "", kind: str = "",
                      limit: int = 100, sentiment_filter: str = "") -> list[dict]:
    sql = "SELECT m.*, a.name AS account_name, a.platform AS acct_platform " \
          "FROM interaction_messages m LEFT JOIN accounts a ON a.id=m.account_id WHERE 1=1"
    params: list = []
    if account_id:
        sql += " AND m.account_id=?"
        params.append(account_id)
    if status:
        sql += " AND m.status=?"
        params.append(status)
    if kind:
        sql += " AND m.kind=?"
        params.append(kind)
    if sentiment_filter:
        sql += " AND m.sentiment=?"
        params.append(sentiment_filter)
    sql += " ORDER BY m.created_at DESC LIMIT ?"
    params.append(limit)
    return query(sql, tuple(params))


def mark_interaction_replied(msg_id: int, reply_text: str) -> None:
    execute("UPDATE interaction_messages SET replied=1, reply_text=?, status='replied' WHERE id=?",
            (reply_text, msg_id))


def mark_interaction_read(msg_id: int) -> None:
    execute("UPDATE interaction_messages SET status='read' WHERE id=? AND status='unread'", (msg_id,))


def count_unread_interactions(account_id: int = 0) -> int:
    if account_id:
        r = query_one("SELECT COUNT(*) AS cnt FROM interaction_messages "
                      "WHERE status='unread' AND account_id=?", (account_id,))
    else:
        r = query_one("SELECT COUNT(*) AS cnt FROM interaction_messages WHERE status='unread'", ())
    return r["cnt"] if r else 0


# --- 自动回复规则 ---

def create_reply_rule(name: str, reply_text: str, account_id: int = 0,
                      platform: str = "", kind: str = "comment",
                      match_type: str = "keyword", keywords: str = "[]",
                      priority: int = 0) -> dict:
    execute("INSERT INTO reply_rules(name, account_id, platform, kind, match_type, "
            "keywords, reply_text, priority) VALUES(?,?,?,?,?,?,?,?)",
            (name, account_id, platform, kind, match_type, keywords, reply_text, priority))
    return query_one("SELECT * FROM reply_rules ORDER BY id DESC LIMIT 1", ())


def list_reply_rules(account_id: int = 0) -> list[dict]:
    if account_id:
        return query("SELECT * FROM reply_rules WHERE account_id=? ORDER BY priority DESC, id",
                     (account_id,))
    return query("SELECT * FROM reply_rules ORDER BY priority DESC, id")


def get_reply_rules_for_account(account_id: int, kind: str = "") -> list[dict]:
    """获取指定账号的启用规则（含全局规则 kind=''）。"""
    if kind:
        return query(
            "SELECT * FROM reply_rules WHERE enabled=1 AND (account_id=? OR account_id=0) "
            "AND (platform='' OR platform=(SELECT platform FROM accounts WHERE id=?)) "
            "AND (kind=? OR kind='') ORDER BY priority DESC, id",
            (account_id, account_id, kind))
    return query(
        "SELECT * FROM reply_rules WHERE enabled=1 AND (account_id=? OR account_id=0) "
        "AND (platform='' OR platform=(SELECT platform FROM accounts WHERE id=?)) "
        "ORDER BY priority DESC, id",
        (account_id, account_id))


def update_reply_rule(rule_id: int, **fields) -> None:
    allowed = {"name", "account_id", "platform", "kind", "match_type",
               "keywords", "reply_text", "priority", "enabled"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(rule_id)
    execute(f"UPDATE reply_rules SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_reply_rule(rule_id: int) -> None:
    execute("DELETE FROM reply_rules WHERE id=?", (rule_id,))


# --- 内容模板 ---

def create_content_template(name: str, platform: str = "", title_template: str = "",
                            text_template: str = "", variables: str = "{}") -> dict:
    execute("INSERT INTO content_templates(name, platform, title_template, text_template, variables) "
            "VALUES(?,?,?,?,?)", (name, platform, title_template, text_template, variables))
    return query_one("SELECT * FROM content_templates ORDER BY id DESC LIMIT 1", ())


def list_content_templates(platform: str = "") -> list[dict]:
    if platform:
        return query("SELECT * FROM content_templates WHERE platform=? ORDER BY id DESC",
                     (platform,))
    return query("SELECT * FROM content_templates ORDER BY id DESC")


def get_content_template(template_id: int) -> Optional[dict]:
    return query_one("SELECT * FROM content_templates WHERE id=?", (template_id,))


def update_content_template(template_id: int, **fields) -> None:
    allowed = {"name", "platform", "title_template", "text_template", "variables"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(template_id)
    execute(f"UPDATE content_templates SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_content_template(template_id: int) -> None:
    execute("DELETE FROM content_templates WHERE id=?", (template_id,))


def render_template(template_text: str, variables: dict) -> str:
    """简单的变量替换：{{key}} → value。"""
    result = template_text
    for k, v in variables.items():
        result = result.replace("{{" + k + "}}", str(v))
    return result


def set_device_tag(serial: str, key: str, value: str) -> None:
    execute("INSERT INTO device_tags(serial, tag_key, tag_value) VALUES(?,?,?) "
            "ON CONFLICT(serial, tag_key) DO UPDATE SET tag_value=excluded.tag_value",
            (serial, key, value))


def get_device_tags(serial: str) -> dict:
    return {r["tag_key"]: r["tag_value"] for r in query(
        "SELECT tag_key, tag_value FROM device_tags WHERE serial=?", (serial,))}


def list_device_tags() -> dict:
    out = {}
    for r in query("SELECT * FROM device_tags"):
        out.setdefault(r["serial"], {})[r["tag_key"]] = r["tag_value"]
    return out


# ---------- 阶段四: 竞品采集 / 多机协作 / A-B测试 / 情感分析 ----------

# --- 竞品采集源 ---

def create_competitor_source(name: str, url: str, platform: str = "",
                             source_type: str = "rss", api_key: str = "",
                             config: str = "{}",
                             collect_interval: float = 3600) -> dict:
    execute("INSERT INTO competitor_sources(name, url, platform, source_type, "
            "api_key, config, collect_interval) VALUES(?,?,?,?,?,?,?)",
            (name, url, platform, source_type, api_key, config, collect_interval))
    return query_one("SELECT * FROM competitor_sources ORDER BY id DESC LIMIT 1", ())


def list_competitor_sources(enabled_only: bool = False) -> list[dict]:
    if enabled_only:
        return query("SELECT * FROM competitor_sources WHERE enabled=1 ORDER BY id")
    return query("SELECT * FROM competitor_sources ORDER BY id")


def get_competitor_source(source_id: int) -> Optional[dict]:
    return query_one("SELECT * FROM competitor_sources WHERE id=?", (source_id,))


def update_competitor_source(source_id: int, **fields) -> None:
    allowed = {"name", "url", "platform", "source_type", "api_key", "config",
               "collect_interval", "enabled", "last_collect", "total_collected", "last_error"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(source_id)
    execute(f"UPDATE competitor_sources SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_competitor_source(source_id: int) -> None:
    execute("DELETE FROM competitor_contents WHERE source_id=?", (source_id,))
    execute("DELETE FROM competitor_sources WHERE id=?", (source_id,))


# --- 竞品内容 ---

def create_competitor_content(source_id: int, platform: str, title: str,
                              content: str = "", url: str = "", author: str = "",
                              metrics: str = "{}", content_hash: str = "",
                              tags: str = "[]") -> dict:
    execute("INSERT INTO competitor_contents(source_id, platform, title, content, "
            "url, author, metrics, content_hash, tags) VALUES(?,?,?,?,?,?,?,?,?)",
            (source_id, platform, title, content, url, author, metrics, content_hash, tags))
    return query_one("SELECT * FROM competitor_contents ORDER BY id DESC LIMIT 1", ())


def list_competitor_contents(source_id: int = 0, platform: str = "",
                             status: str = "", limit: int = 100) -> list[dict]:
    sql = "SELECT c.*, s.name AS source_name FROM competitor_contents c " \
          "LEFT JOIN competitor_sources s ON s.id=c.source_id WHERE 1=1"
    params: list = []
    if source_id:
        sql += " AND c.source_id=?"
        params.append(source_id)
    if platform:
        sql += " AND c.platform=?"
        params.append(platform)
    if status:
        sql += " AND c.status=?"
        params.append(status)
    sql += " ORDER BY c.created_at DESC LIMIT ?"
    params.append(limit)
    return query(sql, tuple(params))


def get_competitor_content_by_hash(content_hash: str) -> Optional[dict]:
    return query_one("SELECT * FROM competitor_contents WHERE content_hash=?",
                     (content_hash,))


def update_competitor_content(content_id: int, **fields) -> None:
    allowed = {"title", "content", "status", "tags", "metrics"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(content_id)
    execute(f"UPDATE competitor_contents SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_competitor_content(content_id: int) -> None:
    execute("DELETE FROM competitor_contents WHERE id=?", (content_id,))


def promote_competitor_content(content_id: int) -> Optional[dict]:
    """将竞品内容转为自有内容（复制到 contents 表）。"""
    cc = query_one("SELECT * FROM competitor_contents WHERE id=?", (content_id,))
    if not cc:
        return None
    new_content = create_content(
        title=cc["title"],
        type="text",
        text=cc["content"],
    )
    update_competitor_content(content_id, status="promoted")
    return new_content


# --- 情感分析 ---

def update_interaction_sentiment(msg_id: int, sentiment: str,
                                 sentiment_score: float,
                                 sentiment_detail: str = "") -> None:
    execute("UPDATE interaction_messages SET sentiment=?, sentiment_score=?, "
            "sentiment_detail=? WHERE id=?",
            (sentiment, sentiment_score, sentiment_detail, msg_id))


def list_interactions_with_sentiment(account_id: int = 0, sentiment: str = "",
                                     limit: int = 100) -> list[dict]:
    sql = "SELECT m.*, a.name AS account_name, a.platform AS acct_platform " \
          "FROM interaction_messages m LEFT JOIN accounts a ON a.id=m.account_id WHERE 1=1"
    params: list = []
    if account_id:
        sql += " AND m.account_id=?"
        params.append(account_id)
    if sentiment:
        sql += " AND m.sentiment=?"
        params.append(sentiment)
    sql += " ORDER BY m.created_at DESC LIMIT ?"
    params.append(limit)
    return query(sql, tuple(params))


def sentiment_summary(account_id: int = 0) -> dict:
    """情感分析汇总统计。"""
    where = "WHERE 1=1"
    params: list = []
    if account_id:
        where += " AND account_id=?"
        params.append(account_id)
    total = query_one(f"SELECT COUNT(*) AS cnt FROM interaction_messages {where}", tuple(params))
    by_label = query(
        f"SELECT sentiment, COUNT(*) AS cnt, AVG(sentiment_score) AS avg_score "
        f"FROM interaction_messages {where} GROUP BY sentiment", tuple(params))
    negative = query(
        f"SELECT * FROM interaction_messages {where} AND sentiment='negative' "
        f"ORDER BY sentiment_score ASC LIMIT 20", tuple(params))
    return {
        "total": total["cnt"] if total else 0,
        "by_label": by_label,
        "recent_negative": negative,
    }


# --- A/B 测试 ---

def create_ab_test(name: str, content_id: int = 0) -> dict:
    execute("INSERT INTO ab_tests(name, content_id) VALUES(?,?)", (name, content_id))
    return query_one("SELECT * FROM ab_tests ORDER BY id DESC LIMIT 1", ())


def list_ab_tests() -> list[dict]:
    tests = query("SELECT * FROM ab_tests ORDER BY id DESC")
    for t in tests:
        t["variants"] = query("SELECT * FROM ab_variants WHERE test_id=? ORDER BY id",
                              (t["id"],))
    return tests


def get_ab_test(test_id: int) -> Optional[dict]:
    t = query_one("SELECT * FROM ab_tests WHERE id=?", (test_id,))
    if t:
        t["variants"] = query("SELECT * FROM ab_variants WHERE test_id=? ORDER BY id",
                              (test_id,))
    return t


def update_ab_test(test_id: int, **fields) -> None:
    allowed = {"name", "status", "winner_variant_id", "started_at", "ended_at"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(test_id)
    execute(f"UPDATE ab_tests SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_ab_test(test_id: int) -> None:
    execute("DELETE FROM ab_variants WHERE test_id=?", (test_id,))
    execute("DELETE FROM ab_tests WHERE id=?", (test_id,))


def create_ab_variant(test_id: int, variant_key: str, title: str,
                      text: str = "", media_path: str = "",
                      account_id: int = 0) -> dict:
    execute("INSERT INTO ab_variants(test_id, variant_key, title, text, "
            "media_path, account_id) VALUES(?,?,?,?,?,?)",
            (test_id, variant_key, title, text, media_path, account_id))
    return query_one("SELECT * FROM ab_variants ORDER BY id DESC LIMIT 1", ())


def update_ab_variant(variant_id: int, **fields) -> None:
    allowed = {"variant_key", "title", "text", "media_path", "account_id",
               "task_id", "status", "views", "likes", "comments", "shares",
               "followers_gained"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(variant_id)
    execute(f"UPDATE ab_variants SET {', '.join(sets)} WHERE id=?", tuple(params))


def ab_test_analytics(test_id: int) -> dict:
    """A/B 测试效果分析。"""
    test = query_one("SELECT * FROM ab_tests WHERE id=?", (test_id,))
    if not test:
        return {}
    variants = query("SELECT * FROM ab_variants WHERE test_id=? ORDER BY id", (test_id,))
    for v in variants:
        total_engagement = v["views"] + v["likes"] * 3 + v["comments"] * 5 + v["shares"] * 8
        v["engagement_score"] = total_engagement
        v["engagement_rate"] = round(total_engagement / max(v["views"], 1) * 100, 2)
    # 排序找出当前领先者
    ranked = sorted(variants, key=lambda v: -v["engagement_score"])
    return {
        "test": test,
        "variants": variants,
        "ranked": ranked,
        "leader": ranked[0] if ranked else None,
    }


# --- 多机协作流水线 ---

def create_relay_flow(name: str, steps: str, device_order: str,
                      description: str = "") -> dict:
    execute("INSERT INTO relay_flows(name, description, steps, device_order) "
            "VALUES(?,?,?,?)", (name, description, steps, device_order))
    return query_one("SELECT * FROM relay_flows ORDER BY id DESC LIMIT 1", ())


def list_relay_flows() -> list[dict]:
    return query("SELECT * FROM relay_flows ORDER BY id DESC")


def get_relay_flow(flow_id: int) -> Optional[dict]:
    return query_one("SELECT * FROM relay_flows WHERE id=?", (flow_id,))


def update_relay_flow(flow_id: int, **fields) -> None:
    allowed = {"name", "description", "steps", "device_order", "status",
               "current_device_idx", "run_id"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(flow_id)
    execute(f"UPDATE relay_flows SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_relay_flow(flow_id: int) -> None:
    execute("DELETE FROM relay_flows WHERE id=?", (flow_id,))

