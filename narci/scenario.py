"""场景引擎：步骤编排 + 多设备并行执行 + 运行注册表（可中断）。

坐标系约定：步骤支持 pct（0..1 归一化）与 px（物理像素）两种，pct 优先。
所有动作走 adb 控制面，投屏在线与否不影响执行。
"""
import asyncio
import contextlib
import random
import time
import uuid
from typing import Any, Optional

from . import adb, db
from .events import bus

STEP_TYPES = {"tap", "swipe", "text", "key", "back", "home", "launch", "stop_app",
              "shell", "wait", "screenshot", "tap_text", "loop",
              "wait_text", "assert_text", "rand_wait", "start_activity",
              "tap_image", "wait_image", "assert_image", "ocr_tap", "ocr_get"}

_runs: dict[str, dict] = {}
_lock = asyncio.Lock()


class ScenarioError(Exception):
    pass


def validate_steps(steps: list) -> None:
    def _check(seq: list, depth: int = 0) -> None:
        if depth > 5:
            raise ScenarioError("loop 嵌套超过 5 层")
        for s in seq:
            if not isinstance(s, dict) or s.get("type") not in STEP_TYPES:
                raise ScenarioError(f"未知步骤类型: {s!r}")
            if s.get("type") == "loop":
                if not isinstance(s.get("steps"), list):
                    raise ScenarioError("loop 步骤缺少 steps 列表")
                _check(s["steps"], depth + 1)

    _check(steps)


def _coords(step: dict, size: tuple[int, int]) -> tuple[int, int]:
    w, h = size or (0, 0)
    x = int(round(float(step["x_pct"]) * w)) if "x_pct" in step else int(float(step.get("x", 0)))
    y = int(round(float(step["y_pct"]) * h)) if "y_pct" in step else int(float(step.get("y", 0)))
    return x, y


async def _execute_step(step: dict, serial: str, size: tuple[int, int],
                        run_id: str) -> dict:
    stype = step["type"]
    if stype == "tap":
        x, y = _coords(step, size)
        await adb.input_tap(serial, x, y)
        return {"x": x, "y": y}
    if stype == "swipe":
        x1, y1 = _coords(step, size)
        w, h = size or (0, 0)
        if "x2_pct" in step or "y2_pct" in step:
            x2 = int(round(float(step.get("x2_pct", 0)) * w)) if "x2_pct" in step else int(float(step.get("x2", 0)))
            y2 = int(round(float(step.get("y2_pct", 0)) * h)) if "y2_pct" in step else int(float(step.get("y2", 0)))
        else:
            x2, y2 = int(float(step.get("x2", x1))), int(float(step.get("y2", y1)))
        dur = int(step.get("duration_ms", 300))
        await adb.input_swipe(serial, x1, y1, x2, y2, dur)
        return {"from": (x1, y1), "to": (x2, y2), "duration_ms": dur}
    if stype == "text":
        await adb.input_text(serial, str(step.get("text", "")))
        return {}
    if stype == "key":
        name = str(step.get("keycode", step.get("key", "")))
        code = adb.KEYCODES.get(name, None)
        if code is None:
            code = int(name)
        await adb.input_keyevent(serial, code)
        return {"keycode": code}
    if stype == "back":
        await adb.input_keyevent(serial, 4)
        return {}
    if stype == "home":
        await adb.input_keyevent(serial, 3)
        return {}
    if stype == "launch":
        pkg = str(step["package"])
        await adb.launch_app(serial, pkg)
        return {"package": pkg}
    if stype == "stop_app":
        pkg = str(step["package"])
        await adb.stop_app(serial, pkg)
        return {"package": pkg}
    if stype == "shell":
        cmd = str(step.get("cmd", ""))
        rc, out = await adb.shell(serial, cmd, timeout=float(step.get("timeout", 30)))
        return {"rc": rc, "out": out[:2000]}
    if stype == "wait":
        await asyncio.sleep(float(step.get("ms", 1000)) / 1000.0)
        return {}
    if stype == "screenshot":
        from .config import CAPTURES_DIR
        data = await adb.screenshot_bytes(serial)
        name = step.get("name") or f"{serial}_{int(time.time()*1000)}.png"
        if not name.endswith(".png"):
            name += ".png"
        path = CAPTURES_DIR / name
        path.write_bytes(data)
        return {"path": str(path)}
    if stype == "tap_text":
        cx, cy = await adb.tap_text(serial, str(step["text"]))
        return {"center": (cx, cy)}
    if stype == "wait_text":
        text = str(step["text"])
        timeout = float(step.get("timeout_ms", 10000)) / 1000.0
        interval = max(float(step.get("interval_ms", 1000)) / 1000.0, 0.5)
        deadline = time.time() + timeout
        while True:
            center = await adb.find_text(serial, text)
            if center:
                return {"found": center}
            if time.time() >= deadline:
                raise ScenarioError(f"等待文本超时({timeout:.0f}s): {text}")
            await asyncio.sleep(interval)
    if stype == "assert_text":
        text = str(step["text"])
        expect = str(step.get("expect", "exists"))
        center = await adb.find_text(serial, text)
        if expect == "exists" and center is None:
            raise ScenarioError(f"断言失败: 未找到文本 {text}")
        if expect == "absent" and center is not None:
            raise ScenarioError(f"断言失败: 文本仍存在 {text}")
        return {"expect": expect, "met": True}
    if stype == "rand_wait":
        lo = float(step.get("min_ms", 500))
        hi = max(float(step.get("max_ms", 2000)), lo)
        ms = random.uniform(lo, hi)
        await asyncio.sleep(ms / 1000.0)
        return {"ms": round(ms)}
    if stype == "start_activity":
        comp = str(step["component"])
        await adb.start_activity(serial, comp)
        return {"component": comp}
    if stype == "tap_image":
        from . import vision
        path = vision.resolve_template(str(step["image"]))
        res = await vision.match_on_device(serial, path, float(step.get("threshold", 0.8)))
        if res is None:
            raise ScenarioError(f"未匹配到图像: {path}")
        cx, cy, score = res
        await adb.input_tap(serial, cx, cy)
        return {"center": (cx, cy), "score": round(score, 3)}
    if stype == "wait_image":
        from . import vision
        path = vision.resolve_template(str(step["image"]))
        threshold = float(step.get("threshold", 0.8))
        timeout = float(step.get("timeout_ms", 10000)) / 1000.0
        interval = max(float(step.get("interval_ms", 1000)) / 1000.0, 0.5)
        deadline = time.time() + timeout
        while True:
            res = await vision.match_on_device(serial, path, threshold)
            if res is not None:
                return {"found": (res[0], res[1]), "score": round(res[2], 3)}
            if time.time() >= deadline:
                raise ScenarioError(f"等待图像超时({timeout:.0f}s): {path}")
            await asyncio.sleep(interval)
    if stype == "assert_image":
        from . import vision
        path = vision.resolve_template(str(step["image"]))
        threshold = float(step.get("threshold", 0.8))
        expect = str(step.get("expect", "exists"))
        res = await vision.match_on_device(serial, path, threshold)
        if expect == "exists" and res is None:
            raise ScenarioError(f"断言失败: 未匹配到图像 {path}")
        if expect == "absent" and res is not None:
            raise ScenarioError(f"断言失败: 图像仍存在 {path}")
        return {"expect": expect, "met": True}
    if stype in ("ocr_tap", "ocr_get"):
        from . import vision
        import tempfile
        data = await adb.screenshot_bytes(serial)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(data)
            tmp_path = f.name
        text = vision.ocr_text(tmp_path) or ""
        if stype == "ocr_get":
            return {"text": text}
        target = str(step["text"])
        if target not in text:
            raise ScenarioError(f"OCR 未找到文本: {target}")
        return {"text": text}
    raise ScenarioError(f"未实现步骤: {stype}")


def substitute_vars(step: dict, variables: dict) -> dict:
    """将步骤中的 {{变量}} 替换为运行期变量。"""
    if not variables:
        return step
    out = {}
    for k, v in step.items():
        if isinstance(v, str):
            for key, val in variables.items():
                v = v.replace("{{" + key + "}}", str(val))
        out[k] = v
    return out


async def _run_device(run_id: str, steps: list, repeat: int, serial: str,
                      abort: asyncio.Event, variables: dict) -> None:
    state = _runs[run_id]["devices"][serial]
    try:
        size = await adb.get_screen_size(serial)
        for rep in range(repeat):
            if abort.is_set():
                break
            state["repeat"] = rep + 1
            await _run_step_list(steps, serial, size, run_id, abort, state, variables)
        state["status"] = "aborted" if abort.is_set() else "done"
    except ScenarioError as e:
        state["status"] = "error"
        state["error"] = str(e)
    except asyncio.CancelledError:
        state["status"] = "aborted"
        raise
    except adb.AdbError as e:
        state["status"] = "error"
        state["error"] = str(e)
    except Exception as e:
        state["status"] = "error"
        state["error"] = f"{type(e).__name__}: {e}"
    bus.publish("info", f"场景运行[{run_id[:8]}] {serial}: {state['status']}"
                + (f" - {state.get('error', '')}" if state.get("error") else ""),
                run_id=run_id, serial=serial)


async def _run_step_list(steps: list, serial: str, size: tuple[int, int],
                         run_id: str, abort: asyncio.Event, state: dict, variables: dict) -> None:
    for i, step in enumerate(steps):
        if abort.is_set():
            return
        if step.get("type") == "loop":
            count = int(step.get("count", 1))
            for c in range(count):
                if abort.is_set():
                    return
                await _run_step_list(step["steps"], serial, size, run_id, abort, state, variables)
            continue
        state["current"] = i + 1
        started = time.time()
        try:
            result = await _execute_step(substitute_vars(step, variables or {}), serial, size, run_id)
        except (ScenarioError, adb.AdbError):
            # 失败自动截图取证（尽力而为）
            with contextlib.suppress(Exception):
                from .config import CAPTURES_DIR
                data = await adb.screenshot_bytes(serial)
                path = CAPTURES_DIR / "auto" / f"{serial}_{int(time.time() * 1000)}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                state["failure_shot"] = str(path)
            raise
        bus.publish("info",
                    f"场景运行[{run_id[:8]}] {serial} 步骤{i + 1}/{len(steps)} {step['type']} "
                    f"耗时{time.time() - started:.2f}s",
                    run_id=run_id, serial=serial, step=step.get("type"))
        state["done"] = state.get("done", 0) + 1
        if result:
            state.setdefault("results", []).append({"index": i + 1, **result})


async def start_run(steps: list, repeat: int, serials: list[str], variables: dict = None) -> str:
    validate_steps(steps)
    run_id = uuid.uuid4().hex
    abort = asyncio.Event()
    async with _lock:
        _runs[run_id] = {
            "run_id": run_id,
            "status": "running",
            "repeat": repeat,
            "devices": {s: {"status": "running", "done": 0, "current": 0} for s in serials},
            "abort": abort,
            "tasks": [],
            "started_at": time.time(),
        }
    for serial in serials:
        task = asyncio.create_task(_run_device(run_id, steps, repeat, serial, abort, variables or {}))
        _runs[run_id]["tasks"].append(task)
    return run_id


async def abort_run(run_id: str) -> bool:
    run = _runs.get(run_id)
    if not run:
        return False
    run["abort"].set()
    return True


def list_runs() -> list[dict]:
    out = []
    for run in _runs.values():
        running = any(d["status"] == "running" for d in run["devices"].values())
        out.append({
            "run_id": run["run_id"], "status": "running" if running else "finished",
            "repeat": run["repeat"], "started_at": run["started_at"],
            "devices": {k: {kk: vv for kk, vv in v.items() if kk != "results"}
                        for k, v in run["devices"].items()},
        })
    return sorted(out, key=lambda r: -r["started_at"])[-50:]


async def cleanup_finished() -> None:
    async with _lock:
        for run_id in list(_runs):
            run = _runs[run_id]
            if len(_runs) <= 100:
                break
            if all(t.done() for t in run["tasks"]):
                run["status"] = "finished"
                if time.time() - run["started_at"] > 3600:
                    _runs.pop(run_id, None)
