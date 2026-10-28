"""narciFarm 主应用：REST API + WebSocket（投屏桥接 / 事件流）+ 后台循环。"""
import asyncio
import base64
import json
import os
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import adb, db, scenario, scheduler
from .config import (CAPTURES_DIR, DEVICE_REFRESH_INTERVAL, HOST, MAX_FPS,
                     MAX_SIZE, PORT, SNAPSHOT_INTERVAL, STATIC_DIR,
                     UPLOADS_DIR, VIDEO_BITRATE)
from .events import bus
from .scrcpy_client import ScrcpyClient, ScrcpyClientError, ensure_jar_pushed, hub
from .scrcpy_protocol import encode_set_clipboard, encode_set_display_power

_START_TIME = time.time()
_wm_size_cache: dict[str, tuple[float, tuple[int, int]]] = {}
_info_fetch_at: dict[str, float] = {}
_wifi_retry_at: dict[str, float] = {}
_background: list[asyncio.Task] = []


async def _screen_size(serial: str, ttl: float = 60) -> tuple[int, int]:
    cached = _wm_size_cache.get(serial)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    size = await adb.get_screen_size(serial)
    _wm_size_cache[serial] = (time.time(), size)
    return size


async def _scale_xy(serial: str, step: dict) -> tuple[int, int]:
    size = await _screen_size(serial)
    w, h = size or (0, 0)
    x = int(round(float(step["x_pct"]) * w)) if "x_pct" in step else int(float(step.get("x", 0)))
    y = int(round(float(step["y_pct"]) * h)) if "y_pct" in step else int(float(step.get("y", 0)))
    return x, y


async def _input_action(serial: str, action: str, params: dict) -> Optional[str]:
    """执行单台设备的输入动作。

    先经 ensure_awake 守卫（熄屏时注入会 rc=0 但无效，需先唤醒）；
    power 键本身是亮/熄屏开关，例外跳过守卫（否则熄屏时按 power 会先被唤醒、再亮屏，语义错误）。
    返回附加说明（如“设备熄屏已自动唤醒”），正常情况下为 None。
    """
    note = None if action == "key" and str(params.get("keycode", params.get("key", ""))) == "power" \
        else await adb.ensure_awake(serial)
    if action == "tap":
        x, y = await _scale_xy(serial, params)
        await adb.input_tap(serial, x, y)
    elif action == "swipe":
        x1, y1 = await _scale_xy(serial, params)
        p2 = {"x_pct": params.get("x2_pct"), "y_pct": params.get("y2_pct"), "x": params.get("x2"), "y": params.get("y2")}
        x2, y2 = await _scale_xy(serial, {k: v for k, v in p2.items() if v is not None} or {"x": x1, "y": y1})
        await adb.input_swipe(serial, x1, y1, x2, y2, int(params.get("duration_ms", 300)))
    elif action == "text":
        await adb.input_text(serial, str(params.get("text", "")))
    elif action == "key":
        name = str(params.get("keycode", params.get("key", "")))
        code = adb.KEYCODES.get(name)
        if code is None:
            code = int(name)
        await adb.input_keyevent(serial, code)
    else:
        raise ValueError(f"未知输入动作: {action}")
    return note


# ---------- 后台：设备发现循环 ----------

async def _discovery_loop() -> None:
    while True:
        try:
            await refresh_devices()
        except asyncio.CancelledError:
            return
        except Exception as e:
            bus.error(f"设备发现异常: {type(e).__name__}: {e}")
        await asyncio.sleep(DEVICE_REFRESH_INTERVAL)


async def refresh_devices() -> int:
    devices = await adb.list_devices()
    online = {d["serial"]: d for d in devices if d["state"] == "device"}
    for d in devices:
        serial = d["serial"]
        row = db.get_device(serial)
        if row is None:
            db.upsert_device(serial, model=d["model"])
            row = db.get_device(serial)
            bus.info(f"发现新设备: {serial} ({d['model']})")
        else:
            db.touch_device(serial)
        if d["state"] == "device":
            asyncio.create_task(ensure_jar_pushed(serial))  # 预推送(幂等)，打开投屏即免 push
            if time.time() - _info_fetch_at.get(serial, 0) > 300:
                try:
                    info = await adb.get_build_info(serial)
                    w, h = await adb.get_screen_size(serial)
                    db.update_device(serial, model=info["model"] or d["model"],
                                     android_version=info["android_version"],
                                     sdk=info["sdk"], width=w, height=h)
                    _wm_size_cache[serial] = (time.time(), (w, h))
                    _info_fetch_at[serial] = time.time()
                except Exception as e:
                    bus.warn(f"读取设备信息失败 {serial}: {e}")
                    _info_fetch_at[serial] = time.time() - 240
    # WiFi 设备自动重连: 注册表里的无线设备(序列号含 ":")掉线时每 60s 尝试一次
    now = time.time()
    for row in db.list_devices(include_disabled=False):
        serial = row["serial"]
        if ":" not in serial or serial in online:
            continue
        if now - _wifi_retry_at.get(serial, 0) < 60:
            continue
        _wifi_retry_at[serial] = now
        try:
            out = await adb.connect(serial)
            bus.info(f"WiFi 重连 {serial}: {out}")
        except Exception as e:
            bus.warn(f"WiFi 重连失败 {serial}: {e}")
    return len(online)


# ---------- FastAPI ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    if os.environ.get("NARCI_TEST_MODE"):
        bus.info("测试模式: 跳过后台循环")
    else:
        _background.append(asyncio.create_task(_discovery_loop()))
        _background.append(asyncio.create_task(scheduler.scheduler_loop()))
    if not db.list_users():
        ph, salt = _auth.hash_password("admin")
        db.create_user("admin", ph, salt, "admin")
        bus.warn("已创建默认管理员: admin / admin —— 请尽快登录修改密码")
    bus.info(f"narciFarm 已启动: http://{HOST}:{PORT}")
    yield
    bus.info("lifespan 关闭: 停止后台任务与投屏会话")
    for t in _background:
        t.cancel()
    await hub.stop_all()
    bus.info("lifespan 关闭完成")


__version__ = "0.5.0"
app = FastAPI(title="narciFarm", version=__version__, lifespan=lifespan)


def _targets_body(body: dict) -> list[str]:
    serials = body.get("targets") or []
    if body.get("group_id"):
        for g in db.list_groups():
            if g["id"] == body["group_id"]:
                serials = g["members"]
                break
    if not serials:
        raise HTTPException(400, "未指定目标设备")
    return list(dict.fromkeys(serials))


# ---------- 设备 ----------

@app.get("/api/settings")
async def api_settings():
    # getattr 防御: 防止 config 常量缺失/半接线提交导致 500（CI 冒烟测试曾因此失败）
    from . import config as _cfg
    from .config import ADB_PATH, SERVER_JAR, SERVER_VERSION
    return {
        "adb": ADB_PATH, "adb_ok": adb.server_available(),
        "server_jar": str(SERVER_JAR), "server_jar_ok": SERVER_JAR.exists(),
        "scrcpy_version": SERVER_VERSION,
        "max_size": MAX_SIZE, "bitrate": VIDEO_BITRATE, "fps": MAX_FPS,
        "snapshot_interval": SNAPSHOT_INTERVAL,
        "posthog_key": getattr(_cfg, "POSTHOG_KEY", ""),
        "posthog_host": getattr(_cfg, "POSTHOG_HOST", ""),
    }


@app.get("/api/version")
async def api_version():
    return {"version": __version__ or "0.1.0"}


@app.post("/api/diag")
async def api_diag(request: Request):
    """浏览器侧诊断上报（点击/页面错误/UA），写入服务日志，用于定位“点了没反应”类问题。"""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    ua = str(data.get("ua", ""))[:160]
    path = str(data.get("path", ""))[:40]
    payload = data.get("payload") or {}
    bus.warn(f"[前端诊断] {path} {json.dumps(payload, ensure_ascii=False)[:300]} | UA={ua}")
    return {"ok": True}


@app.get("/api/devices")
async def api_devices(refresh: bool = False, request: Request = None):
    if refresh:
        await refresh_devices()
    adb_list = {d["serial"]: d for d in await adb.list_devices()}
    rows = db.list_devices()
    out = []
    for row in rows:
        d = adb_list.get(row["serial"])
        out.append({**row, "state": d["state"] if d else "absent", "present": d is not None})
    # adb 里出现但注册表没有的（等待下一轮发现补齐，也立即补上）
    known = {r["serial"] for r in rows}
    for serial, d in adb_list.items():
        if serial not in known:
            db.upsert_device(serial, model=d["model"])
            out.append({**(db.get_device(serial) or {"serial": serial}), "state": d["state"], "present": True})
    # 附加远程设备(分布式 Agent)
    remote = {d["serial"]: d for d in db.list_remote_devices()}
    agents = {a["agent_id"]: a for a in db.list_agents()}
    out_serials = {d["serial"] for d in out}
    for serial, rd in remote.items():
        if serial in out_serials:
            continue
        ag = agents.get(rd["agent_id"], {})
        out.append({"serial": serial, "model": rd["model"], "android_version": rd["android_version"],
                    "sdk": rd["sdk"], "width": rd["width"], "height": rd["height"],
                    "state": "remote" if ag.get("last_seen", 0) and (time.time() - ag["last_seen"]) < 45 else "offline",
                    "present": True, "group_id": None, "notes": "", "last_seen": "",
                    "agent_id": rd["agent_id"], "agent_name": ag.get("name", rd["agent_id"])})
    # 非管理员: 只返回自己分区的设备
    user = getattr(request.state, "user", None)
    if user and user.get("role") != "admin":
        mine = set()
        for pid in db.user_partitions(user["id"]):
            mine.update(db.partition_devices(pid))
        out = [d for d in out if d["serial"] in mine]
    # 附加预约状态
    for d in out:
        b = db.active_booking(d["serial"])
        d["booked"] = bool(b)
        d["booked_by_me"] = bool(b and b["user_id"] == user["id"]) if user else False
    return out


@app.post("/api/devices/scan_lan")
async def api_scan_lan(body: dict = None):
    body = body or {}
    found = await adb.scan_lan(body.get("cidr", "192.168.1.0/24"), int(body.get("port", 5555)))
    return {"found": found}


@app.post("/api/devices/refresh")
async def api_refresh():
    n = await refresh_devices()
    return {"online": n}


@app.put("/api/devices/{serial}")
async def api_update_device(serial: str, body: dict):
    db.update_device(serial, **{k: v for k, v in body.items() if k in ("notes", "disabled", "group_id")})
    return db.get_device(serial)


@app.get("/api/devices/{serial}/stream_stats")
async def api_stream_stats(serial: str):
    client = hub.get(serial)
    if not client:
        return {"active": False}
    return {"active": True, "fps": round(client.fps(), 1), "frames": client.frames,
            "width": client.width, "height": client.height,
            "dropped_clients": len(hub._subscribers.get(serial, ()))}


# ---------- 应用管理 ----------

@app.get("/api/devices/{serial}/apps")
async def api_apps(serial: str):
    if db.agent_of(serial):
        r = await _proxy_to_agent(serial, "apps", {})
        return {"apps": r.get("apps", [])}
    apps = await adb.list_apps(serial)
    return {"apps": apps}


@app.post("/api/devices/{serial}/apps/{package}/launch")
async def api_app_launch(serial: str, package: str):
    await adb.launch_app(serial, package)
    return {"ok": True}


@app.post("/api/devices/{serial}/apps/{package}/stop")
async def api_app_stop(serial: str, package: str):
    await adb.stop_app(serial, package)
    return {"ok": True}


@app.post("/api/devices/{serial}/apps/{package}/uninstall")
async def api_app_uninstall(serial: str, package: str):
    out = await adb.uninstall_app(serial, package)
    return {"ok": True, "out": out}


@app.post("/api/devices/{serial}/disable")
async def api_disable_device(serial: str, body: dict):
    db.update_device(serial, disabled=1 if body.get("disabled") else 0)
    return {"ok": True}


@app.get("/api/devices/{serial}/info")
async def api_device_info(serial: str):
    info = await adb.get_build_info(serial)
    battery = await adb.get_battery(serial)
    size = await _screen_size(serial, ttl=10)
    focus = ""
    with suppress(Exception):
        focus = await adb.get_current_focus(serial)
    return {**info, "battery": battery, "size": size, "focus": focus}


@app.post("/api/devices/{serial}/shell")
async def api_shell(serial: str, body: dict):
    cmd = str(body.get("cmd", "")).strip()
    if not cmd:
        raise HTTPException(400, "命令为空")
    if db.agent_of(serial):
        r = await _proxy_to_agent(serial, "shell", {"cmd": cmd, "timeout": body.get("timeout", 20)})
        return {"rc": r.get("rc", -1), "out": r.get("out", "")}
    timeout = float(body.get("timeout", 20))
    rc, out = await adb.shell(serial, cmd, timeout=timeout)
    return {"rc": rc, "out": out}


@app.post("/api/input")
async def api_input_multi(body: dict):
    """统一交互注入入口（重构后 PC 操作手机的唯一链路）。

    body: {serials: [...], action: tap|swipe|text|key, params: {...}}
    全部走 adb 控制面（与批量操作/群发同一条已被验证的路径），逐台执行并记录日志。
    """
    serials = body.get("serials") or []
    action = body.get("action")
    params = body.get("params", {})
    if not serials:
        raise HTTPException(400, "未指定目标设备")
    if action not in ("tap", "swipe", "text", "key"):
        raise HTTPException(400, f"不支持的操作: {action}")

    async def one(serial: str) -> dict:
        try:
            note = await _input_action(serial, action, params)
            bus.info(f"[{serial}] 注入[adb] {action} {json.dumps(params, ensure_ascii=False)[:100]}")
            return {"ok": True, **({"note": note} if note else {})}
        except adb.AdbError as e:
            bus.error(f"[{serial}] 注入失败: {e}")
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            bus.error(f"[{serial}] 注入异常: {type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    pairs = await asyncio.gather(*(one(s) for s in serials))
    results = dict(zip(serials, pairs))
    return {"ok": all(r["ok"] for r in results.values()), "results": results}


@app.post("/api/devices/{serial}/input")
async def api_input(serial: str, body: dict, request: Request):
    _booking_guard(serial, request.state.user)
    if db.agent_of(serial):
        r = await _proxy_to_agent(serial, "input", {"action": body.get("action"), "params": body.get("params", {})})
        db.add_usage_counter(request.state.user["id"], serial, control=1)
        return {"ok": r.get("ok", False), **({"error": r["error"]} if r.get("error") else {})}
    db.add_usage_counter(request.state.user["id"], serial, control=1)
    try:
        note = await _input_action(serial, body["action"], body.get("params", {}))
    except adb.AdbError as e:
        bus.error(f"[{serial}] 注入失败: {e}")
        return {"ok": False, "error": str(e)}
    bus.info(f"[{serial}] 注入[adb] {body['action']} {json.dumps(body.get('params', {}), ensure_ascii=False)[:100]}")
    return {"ok": True, **({"note": note} if note else {})}


@app.post("/api/broadcast")
async def api_broadcast(body: dict, request: Request):
    _booking_guard_multi(body.get("targets", []), request.state.user)
    targets = _targets_body(body)
    action = body.get("action", "tap")
    params = body.get("params", {})

    local, remote = _split_local_remote(targets)

    async def one(serial: str):
        try:
            note = await _input_action(serial, action, params)
            return serial, None, note
        except Exception as e:
            return serial, str(e), None

    async def one_remote(serial: str):
        try:
            r = await _proxy_to_agent(serial, "input", {"action": action, "params": params})
            return serial, (r.get("error") if not r.get("ok") else None), None
        except Exception as e:
            return serial, str(e), None

    results = await asyncio.gather(*(one(x) for x in local), *(one_remote(x) for x in remote))
    failed = {s: err for s, err, _ in results if err}
    notes = {s: n for s, _, n in results if n}
    return {"ok": not failed, "sent": len(targets) - len(failed), "failed": failed, "notes": notes}


@app.post("/api/devices/{serial}/screenshot")
async def api_screenshot_save(serial: str):
    if db.agent_of(serial):
        r = await _proxy_to_agent(serial, "screenshot", {})
        if r.get("ok") and r.get("image_base64"):
            data = base64.b64decode(r["image_base64"])
        else:
            raise HTTPException(502, r.get("error", "Agent 截图失败"))
    else:
        data = await adb.screenshot_bytes(serial)
    name = f"{serial}_{time.strftime('%Y%m%d_%H%M%S')}.png"
    path = CAPTURES_DIR / name
    path.write_bytes(data)
    return {"file": name, "url": f"/captures/{name}"}


@app.get("/api/devices/{serial}/screenshot.png")
async def api_screenshot_live(serial: str):
    data = await adb.screenshot_bytes(serial)
    return Response(content=data, media_type="image/png")


@app.post("/api/devices/{serial}/install")
async def api_install(serial: str, apk: UploadFile = File(...), args: str = Form("")):
    extra = [a for a in args.split() if a]
    dest = UPLOADS_DIR / apk.filename
    with open(dest, "wb") as f:
        while chunk := await apk.read(1 << 20):
            f.write(chunk)
    out = await adb.install(serial, str(dest), extra_args=extra)
    return {"out": out}


@app.post("/api/devices/{serial}/push")
async def api_push(serial: str, file: UploadFile = File(...), remote: str = Form(...)):
    dest = UPLOADS_DIR / file.filename
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    await adb.push(serial, str(dest), remote)
    return {"ok": True}


@app.post("/api/devices/{serial}/stay_awake")
async def api_stay_awake(serial: str, body: dict):
    await adb.stay_awake(serial, bool(body.get("on", True)))
    return {"ok": True}


# ---------- 分组 ----------

@app.get("/api/groups")
async def api_groups():
    return db.list_groups()


@app.post("/api/groups")
async def api_create_group(body: dict):
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "分组名为空")
    return db.create_group(name)


@app.delete("/api/groups/{group_id}")
async def api_delete_group(group_id: int):
    db.delete_group(group_id)
    return {"ok": True}


@app.post("/api/groups/{group_id}/members")
async def api_set_members(group_id: int, body: dict):
    db.set_group_members(group_id, body.get("serials", []))
    return {"ok": True}


# ---------- 场景 ----------

@app.get("/api/scenarios")
async def api_scenarios():
    return db.list_scenarios()


@app.post("/api/scenarios")
async def api_create_scenario(body: dict):
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "场景名为空")
    steps = body.get("steps", [])
    try:
        scenario.validate_steps(steps)
    except scenario.ScenarioError as e:
        raise HTTPException(400, str(e))
    repeat = int(body.get("repeat_count", 1))
    if db.query_one("SELECT id FROM scenarios WHERE name=?", (name,)):
        raise HTTPException(400, f"场景已存在: {name}")
    return db.create_scenario(name, json.dumps(steps, ensure_ascii=False), repeat)


@app.put("/api/scenarios/{scenario_id}")
async def api_update_scenario(scenario_id: int, body: dict):
    if not db.get_scenario(scenario_id):
        raise HTTPException(404, "场景不存在")
    steps = body.get("steps")
    if steps is not None:
        try:
            scenario.validate_steps(steps)
        except scenario.ScenarioError as e:
            raise HTTPException(400, str(e))
    db.update_scenario(scenario_id,
                       name=body.get("name"),
                       steps=json.dumps(steps, ensure_ascii=False) if steps is not None else None,
                       repeat_count=body.get("repeat_count"))
    return db.get_scenario(scenario_id)


@app.delete("/api/scenarios/{scenario_id}")
async def api_delete_scenario(scenario_id: int):
    db.delete_scenario(scenario_id)
    return {"ok": True}


@app.post("/api/scenarios/{scenario_id}/run")
async def api_run_scenario(scenario_id: int, body: dict):
    sc = db.get_scenario(scenario_id)
    if not sc:
        raise HTTPException(404, "场景不存在")
    targets = _targets_body(body)
    online = {d["serial"] for d in await adb.list_devices() if d["state"] == "device"}
    targets = [s for s in targets if s in online]
    if not targets:
        raise HTTPException(400, "目标设备均不在线")
    steps = json.loads(sc["steps"])
    repeat = int(body.get("repeat_count") or sc.get("repeat_count") or 1)
    local, remote = _split_local_remote(targets)
    if remote:
        r = await _proxy_to_agent(remote[0], "scenario_run",
                                  {"steps": steps, "repeat_count": repeat, "vars": body.get("vars") or {},
                                   "targets": remote})
        if not r.get("ok"):
            raise HTTPException(502, r.get("error", "Agent 场景执行失败"))
        return {"run_id": r.get("run_id"), "targets": targets, "remote": True}
    run_id = await scenario.start_run(steps, repeat, local, body.get("vars"))
    return {"run_id": run_id, "targets": targets}


@app.get("/api/runs")
async def api_runs():
    await scenario.cleanup_finished()
    return scenario.list_runs()


@app.post("/api/runs/{run_id}/abort")
async def api_abort_run(run_id: str):
    ok = await scenario.abort_run(run_id)
    if not ok:
        raise HTTPException(404, "运行不存在或已结束")
    return {"ok": True}


# ---------- 调度 ----------

@app.get("/api/schedules")
async def api_schedules():
    return db.list_schedules()


@app.post("/api/schedules")
async def api_create_schedule(body: dict):
    name = str(body.get("name", "")).strip() or "未命名调度"
    kind = body.get("kind", "interval")
    value = str(body.get("value", "3600"))
    nxt = scheduler.next_run_for(kind, value)
    return db.create_schedule(name, int(body["scenario_id"]),
                              json.dumps(body.get("target", {}), ensure_ascii=False),
                              kind, value, nxt)


@app.put("/api/schedules/{schedule_id}")
async def api_update_schedule(schedule_id: int, body: dict):
    if not db.get_schedule(schedule_id):
        raise HTTPException(404, "调度不存在")
    fields = {k: body[k] for k in ("name", "kind", "value", "enabled") if k in body}
    if "scenario_id" in body:
        fields["scenario_id"] = int(body["scenario_id"])
    if "target" in body:
        fields["target"] = json.dumps(body["target"], ensure_ascii=False)
    if "kind" in fields or "value" in fields:
        cur = db.get_schedule(schedule_id)
        kind = fields.get("kind", cur["kind"])
        value = str(fields.get("value", cur["value"]))
        fields["next_run"] = scheduler.next_run_for(kind, value)
    db.update_schedule(schedule_id, **fields)
    return db.get_schedule(schedule_id)


@app.delete("/api/schedules/{schedule_id}")
async def api_delete_schedule(schedule_id: int):
    db.delete_schedule(schedule_id)
    return {"ok": True}


# ---------- 截图库 ----------

@app.get("/api/captures")
async def api_captures():
    files = sorted(CAPTURES_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"file": p.name, "url": f"/captures/{p.name}", "size": p.stat().st_size} for p in files[:200]]


# ---------- WebSocket：事件流 ----------

@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    await ws.accept()
    q = await bus.subscribe()
    try:
        for ev in bus.recent(100):
            await ws.send_json(ev)
        while True:
            ev = await q.get()
            await ws.send_json(ev)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(q)


# ---------- WebSocket：投屏桥接 ----------

@app.websocket("/ws/mirror/{serial}")
async def ws_mirror(ws: WebSocket, serial: str):
    await ws.accept()
    user = _auth.user_from_token(ws.query_params.get("token"))
    if not user:
        await ws.send_json({"type": "error", "msg": "未登录"})
        await ws.close()
        return
    usage_id = db.start_usage_session(user["id"], user["username"], serial)
    # 远程设备: 中继到所属 Agent 的投屏 WS
    if db.agent_of(serial):
        try:
            await _proxy_mirror(ws, serial, db.agent_of(serial), ws.query_params.get("quality", "medium"))
        finally:
            db.end_usage_session(usage_id)
        return
    control = ws.query_params.get("control", "1") != "0"
    quality = ws.query_params.get("quality", "medium")
    presets = {
        "high": {"max_size": 1280, "video_bit_rate": 8000000, "max_fps": 30},
        "medium": {"max_size": 960, "video_bit_rate": 4000000, "max_fps": 30},
        "low": {"max_size": 640, "video_bit_rate": 2000000, "max_fps": 24},
    }
    try:
        client = await hub.acquire(serial, control=control, **presets.get(quality, presets["medium"]))
    except Exception as e:  # noqa: BLE001 - 任何连接失败都必须可见：写日志 + 回前端错误帧
        bus.error(f"[{serial}] 投屏连接失败: {type(e).__name__}: {e}")
        with suppress(Exception):
            await ws.send_json({"type": "error", "msg": str(e)})
        await ws.close()
        return

    queue = hub.subscribe(serial)
    writer_task = asyncio.create_task(_mirror_writer(ws, queue))
    if client.width:
        await ws.send_json({"type": "session", "width": client.width, "height": client.height})
    await ws.send_json({"type": "device", "name": client.device_name,
                        "fps": round(client.fps(), 1)})
    bus.info(f"[{serial}] 投屏连接打开 (control={control}, 观看者={hub.refs(serial)})")

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            # ASGI 文本帧: type 恒为 "websocket.receive"，负载在 "text" 键
            text = msg.get("text")
            if text is None:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                bus.warn(f"[{serial}] 非法 WS 指令(非JSON): {text[:100]!r}")
                continue
            try:
                _booking_guard(serial, user)
                await _handle_ws_command(ws, client, data)
            except HTTPException as e:
                bus.warn(f"[{serial}] 预约守卫拦截: {e.detail}")
                with suppress(Exception):
                    await ws.send_json({"type": "error", "msg": e.detail})
            except Exception as e:  # noqa: BLE001 - 注入失败必须呈现给前端而非断流
                bus.error(f"[{serial}] 处理指令 {data.get('action')} 失败: {type(e).__name__}: {e}")
                with suppress(Exception):
                    await ws.send_json({"type": "error", "msg": f"注入失败: {e}"})
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001 - 接收循环异常也不能无声吞掉
        bus.error(f"[{serial}] 投屏 WS 异常: {type(e).__name__}: {e}")
        with suppress(Exception):
            await ws.send_json({"type": "error", "msg": f"投屏通道异常: {e}"})
    finally:
        db.end_usage_session(usage_id)
        hub.unsubscribe(serial, queue)
        writer_task.cancel()
        bus.info(f"[{serial}] 投屏连接关闭")
        await hub.release(serial)


async def _mirror_writer(ws: WebSocket, queue: asyncio.Queue) -> None:
    """向单个观看者推帧；推送失败必须收尾关连接（否则画面静默冻结）。"""
    try:
        while True:
            kind, data = await queue.get()
            if kind == "json":
                await ws.send_json(data)
            else:
                await ws.send_bytes(data)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        bus.error(f"投屏推送中断: {type(e).__name__}: {e}（关闭连接以触发前端重连/降级）")
        with suppress(Exception):
            await ws.close()


async def _handle_ws_command(ws: WebSocket, client: ScrcpyClient, data: dict) -> None:
    """WS 仅承载视频与少量辅助命令。交互注入统一走 HTTP /api/input（与批量同路，可靠且全量留痕）。

    保留: stats(前端帧率显示) / set_clipboard(走 scrcpy 控制通道)。
    """
    action = data.get("action")

    if action == "stats":
        await ws.send_json({"type": "stats", "fps": round(client.fps(), 1),
                            "frames": client.frames, "width": client.width, "height": client.height})
        return

    if action == "screen_power":
        # 息屏/亮屏(scrcpy 控制通道 SET_DISPLAY_POWER)
        if client.control_enabled and client.running:
            await client.send_control(encode_set_display_power(bool(data.get("on", True))))
            bus.info(f"[{client.serial}] 注入[scrcpy] screen_power on={bool(data.get('on', True))}")
        return

    if action == "set_clipboard":
        if client.control_enabled and client.running:
            await client.send_control(encode_set_clipboard(str(data.get("text", "")), paste=bool(data.get("paste"))))
            bus.info(f"[{client.serial}] 注入[scrcpy] set_clipboard {str(data.get('text', ''))[:40]!r}")
        return

    # 其余动作一律拒绝, 引导走 HTTP 统一入口
    bus.warn(f"[{client.serial}] WS 收到不支持的操作 {action}（请走 /api/input）")
    with suppress(Exception):
        await ws.send_json({"type": "error", "msg": f"操作 {action} 应走 HTTP /api/input（前端版本过旧请刷新页面）"})


_shutdown_watchdog_started = False


def start_shutdown_watchdog(grace: float = 10.0) -> None:
    """优雅停机的兜底：超时未退出则强制结束进程，保证关闭请求必然生效。"""
    global _shutdown_watchdog_started
    if _shutdown_watchdog_started:
        return
    _shutdown_watchdog_started = True

    def _kill() -> None:
        bus.error(f"优雅停机超过 {grace:.0f}s 未完成，强制退出进程")
        os._exit(0)

    t = threading.Timer(grace, _kill)
    t.daemon = True
    t.start()


@app.post("/api/shutdown")
async def api_shutdown():
    """优雅停机（先经 lifespan 停掉投屏会话与后台任务）。

    仅在通过 main.py 入口（exe / python main.py）启动时可用；
    `python -m uvicorn` 模块方式启动时拿不到 Server 对象，提示用户用 Ctrl+C。
    """
    server = getattr(app.state, "server", None)
    if server is None:
        raise HTTPException(400, "当前启动方式不支持远程关闭：请在控制台按 Ctrl+C 或直接关闭窗口")
    server.should_exit = True
    start_shutdown_watchdog()
    bus.info("收到关闭请求，服务即将退出")
    return {"ok": True}


# ---------- 静态资源 ----------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/captures", StaticFiles(directory=CAPTURES_DIR), name="captures")




# ---------- 开发工具: ADB 隧道 / logcat / 文件 / 性能 / 启动耗时 ----------



@app.get("/api/devices/{serial}/logcat")
async def api_logcat(serial: str, lines: int = 300, level: str = "V", filter: str = ""):
    args = ["logcat", "-d", "-t", str(min(max(lines, 10), 2000)), f"*:{level}"]
    if db.agent_of(serial):
        r = await _proxy_to_agent(serial, "logcat", {"lines": lines, "level": level})
        return {"lines": r.get("lines", [])}
    if filter:
        args.append(filter)
    rc, out = await adb.shell(serial, " ".join(args), timeout=20)
    return {"lines": out.splitlines()[-min(max(lines, 10), 2000):]}


@app.get("/api/devices/{serial}/files")
async def api_list_files(serial: str, path: str = "/sdcard"):
    if db.agent_of(serial):
        r = await _proxy_to_agent(serial, "files", {"path": path})
        return {"path": path, "entries": r.get("entries", [])}
    return {"path": path, "entries": await adb.list_dir(serial, path)}


@app.post("/api/devices/{serial}/files/upload")
async def api_upload(serial: str, path: str = Form("/sdcard"), file: UploadFile = File(...)):
    dest = path.rstrip("/") + "/" + file.filename
    tmp = UPLOADS_DIR / file.filename
    with open(tmp, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    await adb.push(serial, str(tmp), dest)
    return {"ok": True, "path": dest}


@app.get("/api/devices/{serial}/files/download")
async def api_download(serial: str, path: str):
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / Path(path).name
    await adb.pull_file(serial, path, str(tmp))
    return FileResponse(str(tmp), filename=Path(path).name)


@app.post("/api/devices/{serial}/files/mkdir")
async def api_mkdir(serial: str, body: dict):
    await adb.mkdir(serial, body.get("path", "/sdcard/NewFolder"))
    return {"ok": True}


@app.delete("/api/devices/{serial}/files")
async def api_delete_file(serial: str, path: str = ""):
    if not path:
        raise HTTPException(400, "缺少路径")
    await adb.remove_path(serial, path)
    return {"ok": True}


@app.get("/api/devices/{serial}/metrics")
async def api_metrics(serial: str):
    if db.agent_of(serial):
        r = await _proxy_to_agent(serial, "metrics", {})
        return {k: v for k, v in r.items() if k != "ok"}
    m = await adb.get_metrics(serial)
    client = hub.get(serial)
    if client:
        m["fps"] = round(client.fps(), 1)
        m["streaming"] = True
    return m


@app.post("/api/devices/{serial}/startup_time")
async def api_startup_time(serial: str, body: dict):
    if db.agent_of(serial):
        r = await _proxy_to_agent(serial, "startup_time", {"package": body.get("package", ""), "cold": bool(body.get("cold", True))})
        return {k: v for k, v in r.items() if k != "ok"}
    return await adb.startup_time(serial, body.get("package", ""), cold=bool(body.get("cold", True)))




# ---------- 图像模板管理 ----------

@app.get("/api/templates")
async def api_templates():
    from . import vision
    return {"templates": vision.list_templates(), "ocr_available": vision.ocr_available()}


@app.post("/api/templates/upload")
async def api_template_upload(file: UploadFile = File(...)):
    from . import vision
    data = await file.read()
    p = vision.save_template(file.filename, data)
    return {"ok": True, "name": p.name}


@app.delete("/api/templates/{name}")
async def api_template_delete(name: str):
    from . import vision
    return {"ok": vision.delete_template(name)}


@app.get("/api/templates/{name}/preview")
async def api_template_preview(name: str):
    from . import vision
    p = vision.TEMPLATES_DIR / name
    if not p.exists():
        raise HTTPException(404, "模板不存在")
    return FileResponse(str(p))




# ---------- 团队平台: 认证 / 用户 / 分区 / 预约 ----------

from fastapi.responses import JSONResponse

from . import auth as _auth


@app.middleware("http")
async def auth_middleware(request, call_next):
    """保护 /api/*(登录除外); 用户注入 request.state.user。"""
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/login", "/api/agents/heartbeat"):
        user = _auth.user_from_token(_auth.extract_token(request))
        if not user:
            return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
        request.state.user = user
    return await call_next(request)


@app.post("/api/login")
async def api_login(body: dict):
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    user = db.get_user_by_name(username)
    if not user or not _auth.verify_password(password, user["password_hash"], user["salt"]):
        raise HTTPException(401, "用户名或密码错误")
    token = _auth.create_session(user["id"])
    bus.info(f"用户登录: {username} ({user['role']})")
    return {"token": token, "user": {"id": user["id"], "username": username, "role": user["role"]}}


@app.post("/api/logout")
async def api_logout(request: Request):
    token = _auth.extract_token(request)
    if token:
        _auth.destroy_session(token)
    return {"ok": True}


@app.get("/api/me")
async def api_me(request: Request):
    user = request.state.user
    return {"user": user, "partitions": db.user_partitions(user["id"])}


@app.get("/api/users")
async def api_users(_=Depends(_auth.require_admin)):
    return db.list_users()


@app.post("/api/users")
async def api_create_user(body: dict, _=Depends(_auth.require_admin)):
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    role = str(body.get("role", "user"))
    if not username or not password:
        raise HTTPException(400, "用户名和密码不能为空")
    if role not in ("admin", "user"):
        raise HTTPException(400, "角色必须是 admin 或 user")
    if db.get_user_by_name(username):
        raise HTTPException(400, f"用户已存在: {username}")
    ph, salt = _auth.hash_password(password)
    return db.create_user(username, ph, salt, role)


@app.delete("/api/users/{user_id}")
async def api_delete_user(user_id: int, _=Depends(_auth.require_admin)):
    if user_id == 1:
        raise HTTPException(400, "不能删除首个管理员")
    db.delete_user(user_id)
    return {"ok": True}


# ---------- 分区 ----------

@app.get("/api/partitions")
async def api_partitions(request: Request):
    parts = db.list_partitions()
    if request.state.user.get("role") != "admin":
        mine = set(db.user_partitions(request.state.user["id"]))
        parts = [p for p in parts if p["id"] in mine]
    return parts


@app.post("/api/partitions")
async def api_create_partition(body: dict, _=Depends(_auth.require_admin)):
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "分区名不能为空")
    return db.create_partition(name, str(body.get("description", "")))


@app.delete("/api/partitions/{partition_id}")
async def api_delete_partition(partition_id: int, _=Depends(_auth.require_admin)):
    db.delete_partition(partition_id)
    return {"ok": True}


@app.post("/api/partitions/{partition_id}/members")
async def api_set_members(partition_id: int, body: dict, _=Depends(_auth.require_admin)):
    db.set_partition_members(partition_id, [int(x) for x in body.get("user_ids", [])])
    return {"ok": True}


@app.post("/api/partitions/{partition_id}/devices")
async def api_set_devices(partition_id: int, body: dict, _=Depends(_auth.require_admin)):
    db.set_partition_devices(partition_id, [str(x) for x in body.get("serials", [])])
    return {"ok": True}


# ---------- 预约 ----------

@app.get("/api/bookings")
async def api_bookings(request: Request):
    user = request.state.user
    if user.get("role") == "admin":
        return db.list_bookings()
    return db.list_bookings(user["id"])


@app.post("/api/bookings")
async def api_create_booking(body: dict, request: Request):
    user = request.state.user
    serial = str(body.get("serial", ""))
    start_ts = float(body.get("start_ts", 0))
    end_ts = float(body.get("end_ts", 0))
    if end_ts <= start_ts:
        raise HTTPException(400, "结束时间必须晚于开始时间")
    # 分区校验: 只能预约自己分区内的设备
    if user.get("role") != "admin":
        mine = set()
        for pid in db.user_partitions(user["id"]):
            mine.update(db.partition_devices(pid))
        if serial not in mine:
            raise HTTPException(403, "该设备不在你的分区内")
    # 冲突校验: 同设备时间重叠
    now = time.time()
    for b in db.list_bookings():
        if b["serial"] == serial and not (b["end_ts"] <= start_ts or b["start_ts"] >= end_ts):
            raise HTTPException(409, f"时间冲突: {serial} 已被 {b.get('username', '?')} 预约")
    return db.create_booking(user["id"], serial, start_ts, end_ts, str(body.get("note", "")))


@app.delete("/api/bookings/{booking_id}")
async def api_delete_booking(booking_id: int, request: Request):
    user = request.state.user
    db.delete_booking(booking_id, None if user.get("role") == "admin" else user["id"])
    return {"ok": True}


def _booking_guard_multi(serials: list, user: dict) -> None:
    for serial in dict.fromkeys(serials):
        _booking_guard(serial, user)


def _booking_guard(serial: str, user: dict) -> None:
    """预约占用强制: 他人有效预约期间禁止控制。"""
    if user.get("role") == "admin":
        return
    b = db.active_booking(serial)
    if b and b["user_id"] != user["id"]:
        raise HTTPException(409, f"设备已被 {b.get('username', '?')} 预约占用")




# ---------- 分布式: Agent 注册 / 设备合并 / 控制代理 ----------

import httpx


@app.post("/api/agents/heartbeat")
async def api_agent_heartbeat(body: dict):
    agent_id = str(body.get("agent_id", "")).strip()
    if not agent_id:
        raise HTTPException(400, "缺少 agent_id")
    db.upsert_agent(agent_id, str(body.get("name", agent_id)),
                    str(body.get("url", "")), str(body.get("secret", "")))
    db.upsert_remote_devices(agent_id, body.get("devices", []))
    return {"ok": True}


@app.get("/api/agents")
async def api_agents(_=Depends(_auth.require_admin)):
    agents = db.list_agents()
    for a in agents:
        a["device_count"] = len([d for d in db.list_remote_devices() if d["agent_id"] == a["agent_id"]])
        a["online"] = (time.time() - a.get("last_seen", 0)) < 45
    return agents


@app.delete("/api/agents/{agent_id}")
async def api_delete_agent(agent_id: str, _=Depends(_auth.require_admin)):
    db.delete_agent(agent_id)
    return {"ok": True}


async def _proxy_to_agent(serial: str, cmd: str, params: dict) -> dict:
    """把控制命令转发给设备所属的 Agent。"""
    agent = db.agent_of(serial)
    if not agent:
        raise HTTPException(404, "设备不在线或未注册")
    url = agent["url"].rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{url}/agent/command", json={"cmd": cmd, "params": {**params, "serial": serial}})
        return r.json()


def _split_local_remote(serials: list[str]) -> tuple[list, list]:
    remote = {d["serial"] for d in db.list_remote_devices()}
    return [s for s in serials if s not in remote], [s for s in serials if s in remote]


async def _proxy_mirror(ws: WebSocket, serial: str, agent: dict, quality: str) -> None:
    """远程投屏: 浏览器 ← 中心 ← Agent WS 中继。"""
    import websockets
    raw = agent["url"].rstrip("/")
    ws_url = ("wss://" + raw[len("https://"):]) if raw.startswith("https://") else              ("ws://" + raw[len("http://"):]) if raw.startswith("http://") else raw
    url = ws_url + f"/agent/mirror/{serial}"
    try:
        async with websockets.connect(url) as agent_ws:
            async def _to_browser():
                async for msg in agent_ws:
                    if isinstance(msg, str):
                        await ws.send_text(msg)
                    else:
                        await ws.send_bytes(msg)

            async def _to_agent():
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("text"):
                        await agent_ws.send(msg["text"])

            await asyncio.gather(_to_browser(), _to_agent())
    except Exception as e:
        with suppress(Exception):
            await ws.send_json({"type": "error", "msg": f"远程投屏失败: {e}"})
        with suppress(Exception):
            await ws.close()




@app.get("/api/usage/summary")
async def api_usage_summary(_=Depends(_auth.require_user)):
    return db.usage_summary()


@app.get("/api/usage/recent")
async def api_usage_recent(limit: int = 30, _=Depends(_auth.require_user)):
    return {"events": db.recent_usage(limit)}




@app.get("/api/usage/export.csv")
async def api_usage_export_csv(_=Depends(_auth.require_user)):
    """使用统计导出 CSV(设备明细 + 用户明细)。"""
    import csv
    import io
    s = db.usage_summary()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["类型", "名称", "会话数", "使用时长(秒)", "操作次数", "最近使用", "主要使用人"])
    for d in s["per_device"]:
        last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["last_used"])) if d["last_used"] else ""
        w.writerow(["设备", d["serial"], d["sessions"], d["total_sec"], d["control_count"], last, d["top_user"]])
    for u in s["per_user"]:
        w.writerow(["用户", u["username"], "", u["total_sec"], "", "", ""])
    w.writerow([])
    w.writerow(["总览", "总会话", s["total_sessions"], "总时长(秒)", s["total_sec"],
                "今日会话", s["today_sessions"], "今日时长(秒)", s["today_sec"]])
    data = buf.getvalue().encode("utf-8-sig")  # BOM 便于 Excel 识别中文
    return Response(content=data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="narciFarm_usage.csv"'})


@app.get("/api/usage/print")
async def api_usage_print(_=Depends(_auth.require_user)):
    """打印友好的报表数据(前端渲染后 window.print() 导出 PDF)。"""
    s = db.usage_summary()
    return {"summary": s, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/metrics")
async def api_metrics():
    """Prometheus 指标(text/plain)。"""
    rows = db.list_devices()
    online = sum(1 for r in rows if r["last_seen"] and (time.time() - _last_seen_ts(r)) < 15)
    active_streams = len(hub._clients)
    lines = [
        "# HELP narcifarm_devices_total 注册设备总数",
        "# TYPE narcifarm_devices_total gauge",
        f"narcifarm_devices_total {len(rows)}",
        "# HELP narcifarm_devices_online 当前在线设备数",
        "# TYPE narcifarm_devices_online gauge",
        f"narcifarm_devices_online {online}",
        "# HELP narcifarm_streams_active 活跃投屏会话数",
        "# TYPE narcifarm_streams_active gauge",
        f"narcifarm_streams_active {active_streams}",
        "# HELP narcifarm_uptime_seconds 服务运行时长(秒)",
        "# TYPE narcifarm_uptime_seconds counter",
        f"narcifarm_uptime_seconds {int(time.time() - _START_TIME)}",
    ]
    return Response(chr(10).join(lines) + chr(10), media_type="text/plain; version=0.0.4")


def _last_seen_ts(row: dict) -> float:
    try:
        return time.mktime(time.strptime(row["last_seen"], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("narci.main:app", host=HOST, port=PORT, log_level="warning")
