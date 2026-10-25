"""narciFarm 主应用：REST API + WebSocket（投屏桥接 / 事件流）+ 后台循环。"""
import asyncio
import json
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import adb, db, scenario, scheduler
from .config import (CAPTURES_DIR, DEVICE_REFRESH_INTERVAL, HOST, MAX_FPS,
                     MAX_SIZE, PORT, SNAPSHOT_INTERVAL, STATIC_DIR,
                     UPLOADS_DIR, VIDEO_BITRATE)
from .events import bus
from .scrcpy_client import ScrcpyClient, ScrcpyClientError, hub
from .scrcpy_protocol import (ACTION_DOWN, ACTION_MOVE, ACTION_UP,
                              encode_inject_scroll, encode_inject_touch,
                              encode_set_clipboard)

_wm_size_cache: dict[str, tuple[float, tuple[int, int]]] = {}
_info_fetch_at: dict[str, float] = {}
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


async def _input_action(serial: str, action: str, params: dict) -> None:
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
        if d["state"] == "device" and (time.time() - _info_fetch_at.get(serial, 0) > 300):
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
    return len(online)


# ---------- FastAPI ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    _background.append(asyncio.create_task(_discovery_loop()))
    _background.append(asyncio.create_task(scheduler.scheduler_loop()))
    bus.info(f"narciFarm 已启动: http://{HOST}:{PORT}")
    yield
    for t in _background:
        t.cancel()
    await hub.stop_all()


app = FastAPI(title="narciFarm", version="0.1.0", lifespan=lifespan)


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
    from .config import ADB_PATH, SERVER_JAR, SERVER_VERSION
    return {
        "adb": ADB_PATH, "adb_ok": adb.server_available(),
        "server_jar": str(SERVER_JAR), "server_jar_ok": SERVER_JAR.exists(),
        "scrcpy_version": SERVER_VERSION,
        "max_size": MAX_SIZE, "bitrate": VIDEO_BITRATE, "fps": MAX_FPS,
        "snapshot_interval": SNAPSHOT_INTERVAL,
    }


@app.get("/api/devices")
async def api_devices(refresh: bool = False):
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
    timeout = float(body.get("timeout", 20))
    rc, out = await adb.shell(serial, cmd, timeout=timeout)
    return {"rc": rc, "out": out}


@app.post("/api/devices/{serial}/input")
async def api_input(serial: str, body: dict):
    await _input_action(serial, body["action"], body.get("params", {}))
    return {"ok": True}


@app.post("/api/broadcast")
async def api_broadcast(body: dict):
    targets = _targets_body(body)
    action = body.get("action", "tap")
    params = body.get("params", {})

    async def one(serial: str):
        try:
            await _input_action(serial, action, params)
            return serial, None
        except Exception as e:
            return serial, str(e)

    results = await asyncio.gather(*(one(s) for s in targets))
    failed = {s: err for s, err in results if err}
    return {"ok": not failed, "sent": len(targets) - len(failed), "failed": failed}


@app.post("/api/devices/{serial}/screenshot")
async def api_screenshot_save(serial: str):
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
    run_id = await scenario.start_run(steps, repeat, targets)
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
    control = ws.query_params.get("control", "1") != "0"
    try:
        client = await hub.acquire(serial, control=control)
    except (ScrcpyClientError, adb.AdbError) as e:
        bus.error(f"[{serial}] 投屏连接失败: {e}")
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
                await _handle_ws_command(ws, client, data)
            except Exception as e:  # noqa: BLE001 - 注入失败必须呈现给前端而非断流
                bus.error(f"[{serial}] 处理指令 {data.get('action')} 失败: {type(e).__name__}: {e}")
                with suppress(Exception):
                    await ws.send_json({"type": "error", "msg": f"注入失败: {e}"})
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(serial, queue)
        writer_task.cancel()
        bus.info(f"[{serial}] 投屏连接关闭")
        await hub.release(serial)


async def _mirror_writer(ws: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        kind, data = await queue.get()
        if kind == "json":
            await ws.send_json(data)
        else:
            await ws.send_bytes(data)


async def _handle_ws_command(ws: WebSocket, client: ScrcpyClient, data: dict) -> None:
    """前端命令统一入口。targets 缺省为当前投屏设备；群控广播时由前端携带完整目标列表。

    快速路径：主控设备若有控制通道则走 scrcpy 注入（低延迟）；其余设备走 adb。
    每条指令与每次注入均写日志（网页「日志」页与 data/narciFarm.log 可查）。
    """
    action = data.get("action")
    targets = data.get("targets") or [client.serial]
    bus.info(f"[{client.serial}] 指令 {action} 参数={json.dumps(data, ensure_ascii=False)[:160]} 目标={targets}")

    if action == "tap":
        nx, ny = float(data.get("nx", 0)), float(data.get("ny", 0))
        for serial in targets:
            if serial == client.serial and client.control_enabled and client.running:
                x, y = int(nx * client.width), int(ny * client.height)
                await client.send_control(encode_inject_touch(ACTION_DOWN, x, y, client.width, client.height))
                await client.send_control(encode_inject_touch(ACTION_UP, x, y, client.width, client.height, 0.0))
                bus.info(f"[{serial}] 注入[scrcpy] tap ({x},{y}) 坐标系({client.width}x{client.height})")
            else:
                await _input_action(serial, "tap", {"x_pct": nx, "y_pct": ny})
                bus.info(f"[{serial}] 注入[adb] tap ({nx:.3f},{ny:.3f})")
        await _ack(ws, "tap", targets)
        return

    if action == "swipe":
        nx1, ny1 = float(data.get("nx1", 0)), float(data.get("ny1", 0))
        nx2, ny2 = float(data.get("nx2", 0)), float(data.get("ny2", 0))
        dur = int(data.get("duration_ms", 200))
        for serial in targets:
            if serial == client.serial and client.control_enabled and client.running and dur <= 400:
                x1, y1 = int(nx1 * client.width), int(ny1 * client.height)
                x2, y2 = int(nx2 * client.width), int(ny2 * client.height)
                await client.send_control(encode_inject_touch(ACTION_DOWN, x1, y1, client.width, client.height))
                steps_n = 5
                for i in range(1, steps_n + 1):
                    t = i / steps_n
                    await asyncio.sleep(max(dur / 1000.0 / steps_n, 0.01))
                    await client.send_control(encode_inject_touch(
                        ACTION_MOVE, int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t),
                        client.width, client.height))
                await client.send_control(encode_inject_touch(ACTION_UP, x2, y2, client.width, client.height))
                bus.info(f"[{serial}] 注入[scrcpy] swipe ({x1},{y1})->({x2},{y2}) {dur}ms")
            else:
                await _input_action(serial, "swipe", {
                    "x_pct": nx1, "y_pct": ny1, "x2_pct": nx2, "y2_pct": ny2,
                    "duration_ms": dur})
                bus.info(f"[{serial}] 注入[adb] swipe ({nx1:.3f},{ny1:.3f})->({nx2:.3f},{ny2:.3f}) {dur}ms")
        await _ack(ws, "swipe", targets)
        return

    if action == "scroll":
        nx = float(data.get("nx", 0.5))
        ny = float(data.get("ny", 0.5))
        delta = float(data.get("delta", -1.0))  # 正值向上滚动
        if client.control_enabled and client.running:
            x, y = int(nx * client.width), int(ny * client.height)
            await client.send_control(encode_inject_scroll(x, y, client.width, client.height, 0.0, delta))
            bus.info(f"[{client.serial}] 注入[scrcpy] scroll ({x},{y}) delta={delta}")
        else:
            size = await _screen_size(client.serial)
            w, h = size or (0, 0)
            if w and h:
                cy = int(ny * h)
                dy = int(h * 0.15) * (1 if delta < 0 else -1)
                await adb.input_swipe(client.serial, int(w / 2), cy, int(w / 2), cy + dy, 120)
                bus.info(f"[{client.serial}] 注入[adb] scroll swipe delta={delta}")
        await _ack(ws, "scroll", [client.serial])
        return

    if action == "key":
        targets = data.get("targets") or [client.serial]
        for serial in targets:
            await _input_action(serial, "key", {"keycode": str(data.get("keycode", ""))})
            bus.info(f"[{serial}] 注入[adb] key {data.get('keycode')}")
        await _ack(ws, "key", targets)
        return

    if action == "text":
        targets = data.get("targets") or [client.serial]
        for serial in targets:
            await _input_action(serial, "text", {"text": str(data.get("text", ""))})
            bus.info(f"[{serial}] 注入[adb] text {str(data.get('text', ''))[:40]!r}")
        await _ack(ws, "text", targets)
        return

    if action == "set_clipboard":
        if client.control_enabled and client.running:
            await client.send_control(encode_set_clipboard(str(data.get("text", "")), paste=bool(data.get("paste"))))
            bus.info(f"[{client.serial}] 注入[scrcpy] set_clipboard {str(data.get('text', ''))[:40]!r}")
        await _ack(ws, "set_clipboard", [client.serial])
        return

    if action == "stats":
        await ws.send_json({"type": "stats", "fps": round(client.fps(), 1),
                            "frames": client.frames, "width": client.width, "height": client.height})
        return


async def _ack(ws: WebSocket, action: str, targets: list) -> None:
    """指令处理完成回执：前端可据此确认链路通到后端。"""
    with suppress(Exception):
        await ws.send_json({"type": "injected", "action": action, "targets": targets})


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
    bus.info("收到关闭请求，服务即将退出")
    return {"ok": True}


# ---------- 静态资源 ----------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/captures", StaticFiles(directory=CAPTURES_DIR), name="captures")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("narci.main:app", host=HOST, port=PORT, log_level="warning")
