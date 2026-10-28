"""Agent: 注册本机设备到中心服务, 执行控制命令, 提供投屏流。

部署: 每台 PC 运行 py -3 agent.py --center http://中心IP:8555 --port 8600
Agent 通过 HTTP 向中心注册并心跳; 中心的控制请求转发到 /agent/command; 投屏 WS 在 /agent/mirror/<serial>。
"""
import asyncio
import base64
import os
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import adb
from .scrcpy_client import hub

AGENT_ID = os.environ.get("NARCI_AGENT_ID") or f"agent-{uuid.uuid4().hex[:8]}"
AGENT_NAME = os.environ.get("NARCI_AGENT_NAME") or AGENT_ID
CENTER_URL = os.environ.get("NARCI_CENTER_URL", "http://127.0.0.1:8555")
AGENT_SECRET = os.environ.get("NARCI_AGENT_SECRET", "")
PORT = int(os.environ.get("NARCI_AGENT_PORT", "8600"))
BIND = os.environ.get("NARCI_AGENT_BIND", "0.0.0.0")
PUBLIC_URL = os.environ.get("NARCI_AGENT_PUBLIC_URL", "")



async def _fetch_devices() -> list[dict]:
    out = []
    for d in await adb.list_devices():
        if d["state"] != "device":
            continue
        info = {}
        try:
            info = await adb.get_build_info(d["serial"])
        except Exception:
            pass
        try:
            w, h = await adb.get_screen_size(d["serial"])
        except Exception:
            w, h = 0, 0
        out.append({"serial": d["serial"], "model": info.get("model") or d.get("model", ""),
                    "android_version": info.get("android_version", ""),
                    "sdk": info.get("sdk", 0), "width": w, "height": h})
    return out


async def register_and_heartbeat() -> None:
    """向中心注册并周期性心跳(上报设备清单)。"""
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                devices = await _fetch_devices()
                await client.post(f"{CENTER_URL}/api/agents/heartbeat", json={
                    "agent_id": AGENT_ID, "name": AGENT_NAME, "secret": AGENT_SECRET,
                    "url": PUBLIC_URL or f"http://{_local_ip()}:{PORT}",
                    "devices": devices,
                })
            except Exception:
                pass
            await asyncio.sleep(15)


def _local_ip() -> str:
    """本机出口 IP(局域网), 供中心回连。"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _dispatch_command(cmd: str, params: dict):
    """执行控制命令(复用 adb 控制面), 返回可 JSON 序列化的结果。"""
    serial = params.get("serial") or params.get("_serial")
    if not serial:
        raise ValueError("缺少 serial")

    async def _run():
        if cmd == "input":
            action = params.get("action")
            p = params.get("params", {})
            if action == "tap":
                x = int(round(float(p.get("x_pct", 0.5)) * (await adb.get_screen_size(serial))[0]))
                y = int(round(float(p.get("y_pct", 0.5)) * (await adb.get_screen_size(serial))[1]))
                await adb.input_tap(serial, x, y)
            elif action == "swipe":
                w, h = await adb.get_screen_size(serial)
                x1 = int(round(float(p.get("x_pct", 0.5)) * w))
                y1 = int(round(float(p.get("y_pct", 0.5)) * h))
                x2 = int(round(float(p.get("x2_pct", 0.5)) * w))
                y2 = int(round(float(p.get("y2_pct", 0.5)) * h))
                await adb.input_swipe(serial, x1, y1, x2, y2, int(p.get("duration_ms", 300)))
            elif action == "text":
                await adb.input_text(serial, str(p.get("text", "")))
            elif action == "key":
                name = str(p.get("keycode", p.get("key", "")))
                code = adb.KEYCODES.get(name)
                await adb.input_keyevent(serial, code if code is not None else int(name))
            else:
                raise ValueError(f"未知输入动作: {action}")
            return {"ok": True}
        if cmd == "screenshot":
            data = await adb.screenshot_bytes(serial)
            return {"ok": True, "image_base64": base64.b64encode(data).decode()}
        if cmd == "shell":
            rc, out = await adb.shell(serial, str(params.get("cmd", "")), timeout=float(params.get("timeout", 20)))
            return {"ok": rc == 0, "rc": rc, "out": out[:4000]}
        if cmd == "apps":
            return {"ok": True, "apps": await adb.list_apps(serial)}
        if cmd == "metrics":
            return {"ok": True, **await adb.get_metrics(serial)}
        if cmd == "startup_time":
            return {"ok": True, **await adb.startup_time(serial, str(params.get("package", "")),
                                                        cold=bool(params.get("cold", True)))}
        if cmd == "logcat":
            rc, out = await adb.shell(serial, " ".join(["logcat", "-d", "-t", str(int(params.get("lines", 300))),
                                                        f"*:{params.get('level', 'V')}"]), timeout=20)
            lines = out.splitlines()[-int(params.get("lines", 300)):]
            return {"ok": True, "lines": lines}
        if cmd == "files":
            return {"ok": True, "entries": await adb.list_dir(serial, str(params.get("path", "/sdcard")))}
        if cmd == "install_base64":
            apk = base64.b64decode(params.get("apk_base64", ""))
            tmp = Path(os.environ.get("TEMP", "/tmp")) / f"agent_{uuid.uuid4().hex[:8]}.apk"
            tmp.write_bytes(apk)
            out = await adb.install(serial, str(tmp))
            tmp.unlink(missing_ok=True)
            return {"ok": True, "out": out}
        if cmd == "scenario_run":
            from . import scenario
            steps = params.get("steps", [])
            repeat = int(params.get("repeat_count", 1))
            variables = params.get("vars") or {}
            targets = [x for x in params.get("targets", [])]
            run_id = await scenario.start_run(steps, repeat, targets, variables)
            return {"ok": True, "run_id": run_id}
        if cmd == "scenario_status":
            from . import scenario
            rid = str(params.get("run_id", ""))
            for r in scenario.list_runs():
                if r["run_id"] == rid:
                    return {"ok": True, "run": r}
            return {"ok": False, "error": "运行不存在"}
        raise ValueError(f"未知命令: {cmd}")

    return asyncio.run(_run())


def _check_secret(request, serial: str = "") -> None:
    """校验共享密钥(配置后生效)。支持 Header、secret query 或短期直连令牌 direct=token:exp。"""
    if not AGENT_SECRET:
        return
    got = (request.headers.get("X-Narci-Secret", "")
           or request.query_params.get("secret", "")
           or request.query_params.get("direct", ""))
    if got == AGENT_SECRET:
        return
    # 短期直连令牌: hmac(secret, "serial:exp")[:24] + ":" + exp, 60 秒有效
    if ":" in got and serial:
        import hashlib
        import hmac
        import time as _t
        try:
            tok, exp = got.rsplit(":", 1)
            if int(exp) >= int(_t.time()):
                expect = hmac.new(AGENT_SECRET.encode(), f"{serial}:{exp}".encode(),
                                  hashlib.sha256).hexdigest()[:24]
                if hmac.compare_digest(tok, expect):
                    return
        except Exception:
            pass
    raise HTTPException(401, "Agent 密钥错误")


def create_agent_app() -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        asyncio.create_task(register_and_heartbeat())
        yield

    app = FastAPI(title=f"narciFarm Agent {AGENT_ID}", lifespan=lifespan)

    @app.get("/agent/info")
    async def agent_info():
        return {"agent_id": AGENT_ID, "name": AGENT_NAME, "devices": await _fetch_devices()}

    @app.post("/agent/command")
    async def agent_command(body: dict, request: Request):
        _check_secret(request, str(body.get("params", {}).get("serial", "")))
        try:
            result = await asyncio.get_event_loop().run_in_executor(None, _dispatch_command,
                                                                    body.get("cmd", ""), body.get("params", {}))
            return result
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=200)

    @app.websocket("/agent/mirror/{serial}")
    async def agent_mirror(ws: WebSocket, serial: str):
        _check_secret(ws, serial)
        await ws.accept()
        client = await hub.acquire(serial, control=True)
        queue = hub.subscribe(serial)

        async def _relay():
            while True:
                kind, data = await queue.get()
                if kind == "json":
                    await ws.send_json(data)
                else:
                    await ws.send_bytes(data)

        task = asyncio.create_task(_relay())
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            task.cancel()
            hub.unsubscribe(serial, queue)
            await hub.release(serial)

    return app


app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_agent_app()
    return app
