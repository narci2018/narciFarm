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
              "tap_image", "wait_image", "assert_image", "find_image",
              "ocr_tap", "ocr_get", "ocr_find",
              "tap_el", "wait_el", "assert_el", "swipe_el", "assert_value",
              "reboot", "locate", "airplane", "lock", "unlock",
              "verify_publish", "detect_risk", "relay", "relay_barrier"}

_runs: dict[str, dict] = {}
_lock = asyncio.Lock()


class ScenarioError(Exception):
    pass


class SoftAssertionError(ScenarioError):
    """软断言失败：记录到运行结果但不中止该设备的后续步骤。"""
    soft = True


def _as_bool(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _text_matches(actual: str, expected: str, mode: str) -> bool:
    """文本匹配：exact 精确 / contains 包含 / regex 正则 / startswith / endswith。"""
    mode = (mode or "exact").strip().lower()
    if mode == "exact":
        return actual == expected
    if mode == "contains":
        return expected in actual
    if mode == "startswith":
        return actual.startswith(expected)
    if mode == "endswith":
        return actual.endswith(expected)
    if mode == "regex":
        import re
        try:
            return re.search(expected, actual) is not None
        except re.error:
            raise ScenarioError(f"正则表达式无效: {expected}")
    raise ScenarioError(f"未知匹配模式: {mode}")


def _compare(actual, expected, op: str) -> tuple[bool, str]:
    """数值/字符串比较，返回 (是否成立, 说明)。"""
    op = (op or "eq").strip().lower()
    numeric = False
    try:
        a, b = float(actual), float(expected)
        numeric = True
    except (TypeError, ValueError):
        a, b = str(actual), str(expected)
    if op in ("eq", "==", "="):
        return a == b, f"{a} == {b}"
    if op in ("ne", "!=", "<>"):
        return a != b, f"{a} != {b}"
    if op in ("gt", ">"):
        if not numeric:
            return False, "非数值无法比较大小"
        return a > b, f"{a} > {b}"
    if op in ("gte", ">="):
        if not numeric:
            return False, "非数值无法比较大小"
        return a >= b, f"{a} >= {b}"
    if op in ("lt", "<"):
        if not numeric:
            return False, "非数值无法比较大小"
        return a < b, f"{a} < {b}"
    if op in ("lte", "<="):
        if not numeric:
            return False, "非数值无法比较大小"
        return a <= b, f"{a} <= {b}"
    if op == "contains":
        return str(expected) in str(actual), f"{expected} in {actual}"
    if op == "regex":
        import re
        try:
            return re.search(str(expected), str(actual)) is not None, f"{expected} ~ {actual}"
        except re.error:
            raise ScenarioError(f"正则表达式无效: {expected}")
    raise ScenarioError(f"未知比较操作符: {op}")


def _raise_assert(step: dict, message: str) -> None:
    """按 soft 标记决定抛软断言还是硬断言。"""
    if _as_bool(step.get("soft", False)):
        raise SoftAssertionError(message)
    raise ScenarioError(message)


def validate_steps(steps: list) -> None:
    def _check(seq: list, depth: int = 0) -> None:
        if depth > 5:
            raise ScenarioError("loop 嵌套超过 5 层")
        from .plugins import REGISTRY
        known = STEP_TYPES | set(REGISTRY._handlers)
        for s in seq:
            if not isinstance(s, dict) or s.get("type") not in known:
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
        # 默认 contains 以兼容旧场景（旧实现先精确后子串，等价于包含匹配）
        mode = str(step.get("match", "contains"))
        # 取当前界面全部节点，按 text / content-desc 做匹配
        nodes = await adb.dump_hierarchy(serial)
        hit = None
        for n in nodes:
            if _text_matches(n.get("text", ""), text, mode) or                _text_matches(n.get("content_desc", ""), text, mode):
                hit = n
                break
        center = hit["center"] if hit else None
        if expect == "exists" and center is None:
            _raise_assert(step, f"断言失败: 未找到文本 {text} (模式 {mode})")
        if expect == "absent" and center is not None:
            _raise_assert(step, f"断言失败: 文本仍存在 {text}")
        return {"expect": expect, "match": mode, "met": True, "center": center}
    if stype == "assert_value":
        actual = step.get("actual", step.get("value", ""))
        expected = step.get("expected", "")
        op = str(step.get("op", "eq"))
        ok, detail = _compare(actual, expected, op)
        if not ok:
            _raise_assert(step, f"断言失败: {actual} {op} {expected} 不成立 ({detail})")
        return {"actual": actual, "expected": expected, "op": op, "met": True}
    if stype == "rand_wait":
        lo = float(step.get("min_ms", 500))
        hi = max(float(step.get("max_ms", 2000)), lo)
        ms = random.uniform(lo, hi)
        await asyncio.sleep(ms / 1000.0)
        return {"ms": round(ms)}
    if stype == "reboot":
        wait_boot = bool(step.get("wait", False))
        await adb.reboot_device(serial, wait=wait_boot)
        return {"rebooted": True, "wait_boot": wait_boot}
    if stype == "locate":
        await adb.locate_device(serial)
        return {"located": True}
    if stype == "airplane":
        on = bool(step.get("on", True))
        await adb.set_airplane_mode(serial, on)
        return {"airplane": on}
    if stype == "lock":
        await adb.lock_device(serial)
        return {}
    if stype == "unlock":
        await adb.unlock_device(serial)
        return {}
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
            _raise_assert(step, f"断言失败: 未匹配到图像 {path}")
        if expect == "absent" and res is not None:
            _raise_assert(step, f"断言失败: 图像仍存在 {path}")
        return {"expect": expect, "met": True}
    if stype in ("ocr_tap", "ocr_get", "ocr_find"):
        from . import vision
        target = str(step.get("text", step.get("target", "")))
        region = step.get("region")
        if isinstance(region, (list, tuple)) and len(region) == 4:
            region = tuple(int(v) for v in region)
        else:
            region = None
        results = await vision.ocr_find_on_device(serial, target=target or None, region=region)
        if stype == "ocr_find":
            return {"matches": results, "count": len(results)}
        if stype == "ocr_get":
            return {"text": " ".join(r["text"] for r in results)}
        if not results:
            raise ScenarioError(f"OCR 未找到文本: {target}")
        best = max(results, key=lambda r: r["confidence"] if r["confidence"] >= 0 else -1)
        cx, cy = best["center"]
        await adb.input_tap(serial, cx, cy)
        return {"center": (cx, cy), "text": best["text"], "confidence": best["confidence"]}
    if stype == "find_image":
        from . import vision
        path = vision.resolve_template(str(step["image"]))
        res = await vision.match_all_on_device(
            serial, path, float(step.get("threshold", 0.8)),
            max_results=int(step.get("max_results", 1)),
            region=step.get("region"),
            scales=step.get("scales"),
        )
        return {"matches": res, "count": len(res)}
    if stype in ("tap_el", "wait_el", "assert_el", "swipe_el"):
        return await _execute_element_step(step, serial)
    if stype == "verify_publish":
        return await _verify_publish(step, serial)
    if stype == "detect_risk":
        return await _detect_risk(step, serial)
    if stype == "relay":
        return await _execute_relay(step, serial, run_id)
    if stype == "relay_barrier":
        return await _execute_relay_barrier(step, serial, run_id)
    # 插件步骤：内置类型优先，未识别的交给注册表（插件不会覆盖内置行为）
    from .plugins import REGISTRY
    handler = REGISTRY.get(stype)
    if handler is not None:
        return await handler(step, serial, size, run_id)
    raise ScenarioError(f"未实现步骤: {stype}")


async def _execute_element_step(step: dict, serial: str) -> dict:
    """控件树 / 多维选择器步骤：tap_el / wait_el / assert_el / swipe_el。"""
    spec = step.get("selector") or {
        "kind": step.get("kind", "text"),
        "value": step.get("value", step.get("text", "")),
    }
    kind, value, contains, index = adb.parse_selector(spec)
    stype = step["type"]

    if stype == "wait_el":
        timeout = float(step.get("timeout_ms", 10000)) / 1000.0
        interval = max(float(step.get("interval_ms", 1000)) / 1000.0, 0.5)
        deadline = time.time() + timeout
        while True:
            el = await adb.find_element(serial, kind, value, contains=contains, index=index)
            if el is not None:
                return {"found": el["center"], "element": el}
            if time.time() >= deadline:
                raise ScenarioError(f"等待控件超时({timeout:.0f}s): {kind}={value}")
            await asyncio.sleep(interval)

    if stype == "assert_el":
        expect = str(step.get("expect", "exists"))
        mode = str(step.get("match", "contains" if contains else "exact"))
        if mode in ("regex", "startswith", "endswith"):
            els = await adb.find_elements(serial, kind, "", contains=True)
            el = next((e for e in els if _text_matches(e.get("text", ""), value, mode)), None)
        else:
            el = await adb.find_element(serial, kind, value, contains=(mode == "contains"), index=index)
        if expect == "exists" and el is None:
            _raise_assert(step, f"断言失败: 未找到控件 {kind}={value} (模式 {mode})")
        if expect == "absent" and el is not None:
            _raise_assert(step, f"断言失败: 控件仍存在 {kind}={value}")
        return {"expect": expect, "match": mode, "met": True, "element": el}

    if stype == "swipe_el":
        el = await adb.find_element(serial, kind, value, contains=contains, index=index)
        if el is None or el["center"] is None:
            raise ScenarioError(f"未找到控件: {kind}={value}")
        x1, y1 = el["center"]
        x2 = int(step.get("x2", x1))
        y2 = int(step.get("y2", y1))
        dur = int(step.get("duration_ms", 300))
        await adb.input_swipe(serial, x1, y1, x2, y2, dur)
        return {"from": (x1, y1), "to": (x2, y2), "duration_ms": dur}

    el = await adb.tap_element(serial, kind, value, contains=contains, index=index)
    return {"center": el["center"], "element": el}


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


async def _verify_publish(step: dict, serial: str) -> dict:
    """发布验证步骤：截图并与参考模板对比，确认发布成功。

    参数:
      template: 参考模板图片名（在 templates 目录中）
      threshold: 匹配阈值（默认 0.7）
      ocr_text: 可选，用 OCR 检测屏幕上是否出现指定文本
      wait_ms: 截图前等待时间（默认 3000ms，等待页面刷新）
    """
    from . import vision
    wait_ms = int(step.get("wait_ms", 3000))
    if wait_ms > 0:
        await asyncio.sleep(wait_ms / 1000.0)

    results = {"screenshot": None, "template_match": None, "ocr_found": None}

    # OCR 验证
    ocr_text = str(step.get("ocr_text", ""))
    if ocr_text:
        ocr_results = await vision.ocr_find_on_device(serial, target=ocr_text)
        results["ocr_found"] = len(ocr_results) > 0
        if not results["ocr_found"] and step.get("strict_ocr"):
            raise ScenarioError(f"发布验证失败: OCR 未找到 '{ocr_text}'")

    # 模板匹配验证
    template = str(step.get("template", ""))
    if template:
        path = vision.resolve_template(template)
        threshold = float(step.get("threshold", 0.7))
        res = await vision.match_on_device(serial, path, threshold)
        results["template_match"] = res is not None
        if res is None and step.get("strict_template"):
            raise ScenarioError(f"发布验证失败: 模板不匹配 '{template}'")

    # 截图存档
    from .config import CAPTURES_DIR
    data = await adb.screenshot_bytes(serial)
    name = f"verify_{serial}_{int(time.time()*1000)}.png"
    path = CAPTURES_DIR / name
    path.write_bytes(data)
    results["screenshot"] = str(path)

    return results


async def _detect_risk(step: dict, serial: str) -> dict:
    """风控检测步骤：检查屏幕是否出现验证码、限流提示等风险信号。

    参数:
      ocr_keywords: 风险关键词列表（如 ["验证码", "请完成安全验证", "操作过于频繁"]）
      risk_images: 风险信号模板图片列表（如 captcha 弹窗截图）
      threshold: 图片匹配阈值（默认 0.8）
      on_risk: 检测到风险时的动作 - "abort"(默认) / "continue" / "screenshot"
    """
    from . import vision
    risk_detected = []

    # OCR 关键词检测
    keywords = step.get("ocr_keywords") or []
    if keywords:
        for kw in keywords:
            ocr_results = await vision.ocr_find_on_device(serial, target=str(kw))
            if ocr_results:
                risk_detected.append({"type": "ocr", "keyword": kw,
                                      "confidence": ocr_results[0].get("confidence", 0)})

    # 模板图片检测
    risk_images = step.get("risk_images") or []
    threshold = float(step.get("threshold", 0.8))
    for img_name in risk_images:
        path = vision.resolve_template(str(img_name))
        res = await vision.match_on_device(serial, path, threshold)
        if res is not None:
            risk_detected.append({"type": "image", "template": img_name,
                                  "score": round(res[2], 3)})

    on_risk = str(step.get("on_risk", "abort"))

    if risk_detected:
        # 截图存档
        from .config import CAPTURES_DIR
        data = await adb.screenshot_bytes(serial)
        name = f"risk_{serial}_{int(time.time()*1000)}.png"
        risk_path = CAPTURES_DIR / name
        risk_path.write_bytes(data)

        bus.warn(f"风控告警 [{serial}]: 检测到 {len(risk_detected)} 个风险信号")

        if on_risk == "abort":
            raise ScenarioError(f"检测到风控信号: {risk_detected}")
        return {"risk": True, "details": risk_detected, "screenshot": str(risk_path)}

    return {"risk": False, "details": []}


async def _execute_relay(step: dict, serial: str, run_id: str) -> dict:
    """多机协作接力步骤：将后续操作委托给另一台设备执行。

    参数:
      target_serial: 目标设备序列号（或变量 {{relay_target}}）
      steps: 要在目标设备上执行的子步骤列表
      wait_complete: 是否等待目标设备完成（默认 True）
      on_fail: 目标设备失败时的行为 - "abort"(默认) / "continue" / "skip"
    """
    target_serial = str(step.get("target_serial", ""))
    if not target_serial:
        raise ScenarioError("relay 步骤缺少 target_serial")

    sub_steps = step.get("steps", [])
    if not sub_steps:
        raise ScenarioError("relay 步骤缺少 steps 子步骤列表")

    wait_complete = bool(step.get("wait_complete", True))
    on_fail = str(step.get("on_fail", "abort"))

    bus.info(f"接力 [{serial}] → [{target_serial}]: {len(sub_steps)} 个步骤")

    # 检查目标设备是否在线
    devices = await adb.list_devices()
    online = {d["serial"] for d in devices if d["state"] == "device"}
    if target_serial not in online:
        msg = f"接力目标设备不在线: {target_serial}"
        if on_fail == "abort":
            raise ScenarioError(msg)
        return {"relayed": False, "error": msg, "target": target_serial}

    # 在目标设备上执行子步骤
    target_size = await adb.get_screen_size(target_serial)
    abort = asyncio.Event()
    fake_state = {"status": "running", "done": 0, "current": 0, "results": []}

    try:
        await _run_step_list(sub_steps, target_serial, target_size,
                             run_id, abort, fake_state, {})
    except (ScenarioError, adb.AdbError) as e:
        msg = f"接力设备[{target_serial}]执行失败: {e}"
        if on_fail == "abort":
            raise ScenarioError(msg)
        return {"relayed": True, "error": msg, "target": target_serial,
                "done_steps": fake_state["done"]}

    return {
        "relayed": True,
        "target": target_serial,
        "done_steps": fake_state["done"],
        "results": fake_state.get("results", []),
    }


async def _execute_relay_barrier(step: dict, serial: str, run_id: str) -> dict:
    """接力同步屏障：等待所有指定设备到达同一阶段后才继续。

    参数:
      barrier_id: 屏障标识（相同 barrier_id 的设备互相等待）
      timeout_ms: 等待超时时间（默认 30000ms）
    """
    barrier_id = str(step.get("barrier_id", "default"))
    timeout_ms = float(step.get("timeout_ms", 30000))

    run = _runs.get(run_id)
    if not run:
        return {"barrier": barrier_id, "status": "no_run"}

    # 在运行记录中标记此设备已到达屏障
    device_key = f"barrier:{barrier_id}"
    run.setdefault(device_key, {})
    run[device_key][serial] = time.time()

    # 检查是否所有参与设备都已到达
    expected_serials = set(run["devices"].keys())
    arrived = set(run[device_key].keys())
    waiting_for = expected_serials - arrived

    if not waiting_for:
        # 所有设备到达，清除屏障
        run.pop(device_key, None)
        return {"barrier": barrier_id, "status": "passed",
                "devices": list(arrived)}

    # 等待其他设备
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        current_arrived = set(run.get(device_key, {}).keys())
        if len(current_arrived) >= len(expected_serials):
            run.pop(device_key, None)
            return {"barrier": barrier_id, "status": "passed",
                    "devices": list(current_arrived)}
        await asyncio.sleep(0.5)

    # 超时
    arrived_final = set(run.get(device_key, {}).keys())
    run.pop(device_key, None)
    missing = expected_serials - arrived_final
    raise ScenarioError(
        f"接力屏障超时({timeout_ms}ms): 等待设备 {list(missing)}")


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
        # 步骤级重试：retry 次重试，retry_interval_ms 退避，on_fail 决定最终失败时的行为
        retries = max(0, int(step.get("retry", 0) or 0))
        retry_interval = max(0.0, float(step.get("retry_interval_ms", 500) or 0) / 1000.0)
        on_fail = str(step.get("on_fail", "abort"))
        result: Optional[dict] = None
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            if abort.is_set():
                return
            try:
                result = await _execute_step(substitute_vars(step, variables or {}), serial, size, run_id)
                last_err = None
                break
            except SoftAssertionError as e:
                # 软断言：不重试，直接按失败处理（由下方逻辑记录并继续）
                last_err = e
                break
            except (ScenarioError, adb.AdbError) as e:
                last_err = e
                if attempt < retries and not abort.is_set():
                    bus.publish("info",
                                f"场景运行[{run_id[:8]}] {serial} 步骤{i + 1}/{len(steps)} {step['type']} "
                                f"第{attempt + 1}次失败，{retry_interval:.2f}s 后重试: {e}",
                                run_id=run_id, serial=serial, step=step.get("type"))
                    await asyncio.sleep(retry_interval)
        if last_err is not None:
            # 失败自动截图取证（尽力而为）
            with contextlib.suppress(Exception):
                from .config import CAPTURES_DIR
                data = await adb.screenshot_bytes(serial)
                path = CAPTURES_DIR / "auto" / f"{serial}_{int(time.time() * 1000)}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                state["failure_shot"] = str(path)
            is_soft = isinstance(last_err, SoftAssertionError)
            if is_soft or on_fail == "continue":
                state.setdefault("results", []).append(
                    {"index": i + 1, "type": step["type"],
                     "skipped": not is_soft, "soft_fail": is_soft,
                     "error": str(last_err), "elapsed": round(time.time() - started, 3)})
                state["done"] = state.get("done", 0) + 1
                bus.publish("info",
                            f"场景运行[{run_id[:8]}] {serial} 步骤{i + 1}/{len(steps)} {step['type']} "
                            f"软断言失败，继续执行: {last_err}",
                            run_id=run_id, serial=serial, step=step.get("type"))
                continue
            raise last_err
        bus.publish("info",
                    f"场景运行[{run_id[:8]}] {serial} 步骤{i + 1}/{len(steps)} {step['type']} "
                    f"耗时{time.time() - started:.2f}s",
                    run_id=run_id, serial=serial, step=step.get("type"))
        state["done"] = state.get("done", 0) + 1
        if result:
            state.setdefault("results", []).append(
                {"index": i + 1, "type": step["type"],
                 "elapsed": round(time.time() - started, 3), **result})


async def start_run(steps: list, repeat: int, serials: list[str], variables: dict = None,
                    scenario_name: str = "", collect_metrics: bool = False,
                    metrics_interval: float = 2.0) -> str:
    validate_steps(steps)
    run_id = uuid.uuid4().hex
    abort = asyncio.Event()
    async with _lock:
        _runs[run_id] = {
            "run_id": run_id,
            "status": "running",
            "scenario": scenario_name,
            "steps": steps,
            "repeat": repeat,
            "devices": {s: {"status": "running", "done": 0, "current": 0,
                             "metrics": [] if collect_metrics else None} for s in serials},
            "abort": abort,
            "tasks": [],
            "started_at": time.time(),
            "collect_metrics": collect_metrics,
            "metrics_interval": max(0.5, float(metrics_interval)),
        }
    device_tasks = []
    for serial in serials:
        task = asyncio.create_task(_run_device(run_id, steps, repeat, serial, abort, variables or {}))
        device_tasks.append(task)
        _runs[run_id]["tasks"].append(task)
    sampler_tasks = []
    if collect_metrics:
        for serial in serials:
            sampler_tasks.append(asyncio.create_task(
                _metrics_sampler(run_id, serial, abort, metrics_interval)))
    _runs[run_id]["tasks"].append(asyncio.create_task(
        _finalize_run(run_id, device_tasks, sampler_tasks)))
    return run_id


async def _metrics_sampler(run_id: str, serial: str, abort: asyncio.Event,
                           interval: float) -> None:
    """运行期间周期性采集设备性能指标，写入该设备的 metrics 列表。

    正常结束后由 _finalize_run 取消；用户中止时随 abort 事件退出。
    """
    run = _runs.get(run_id)
    if not run:
        return
    state = run["devices"].get(serial)
    if not state:
        return
    try:
        while not abort.is_set():
            try:
                m = await adb.read_metrics(serial)
            except Exception:
                m = {}
            if m:
                m["t"] = round(time.time() - run["started_at"], 2)
                state["metrics"].append(m)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(abort.wait(), timeout=interval)
    except asyncio.CancelledError:
        pass


async def _finalize_run(run_id: str, device_tasks: list, sampler_tasks: list) -> None:
    """所有设备任务结束后停止采样、生成并落盘测试报告。"""
    for task in device_tasks:
        try:
            await task
        except Exception:
            pass
    for task in sampler_tasks:
        task.cancel()
    for task in sampler_tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    run = _runs.get(run_id)
    if not run:
        return
    run["status"] = "finished"
    try:
        from . import reports
        rep = reports.build_report(run_id, run, scenario_name=run.get("scenario", ""),
                                   steps=run.get("steps"))
        reports.save_report(rep)
        run["report_id"] = rep["report_id"]
    except Exception as e:  # 报告失败不影响运行结果
        bus.warn(f"报告生成失败[{run_id[:8]}]: {e}")


def get_run(run_id: str) -> Optional[dict]:
    """取运行记录（含步骤结果），供报告构建使用。"""
    return _runs.get(run_id)


def get_report_id(run_id: str) -> Optional[str]:
    run = _runs.get(run_id)
    return run.get("report_id") if run else None


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
