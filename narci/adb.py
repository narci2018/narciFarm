"""adb 异步控制面：设备发现 / shell / 输入注入 / 文件 / 端口转发 / 截屏。

控制面与投屏面解耦：所有批量操作、场景步骤都走 adb，无需投屏在线。
"""
import asyncio
import contextlib
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import Optional

from .config import ADB_PATH
from .events import bus

# 无控制台 exe 中 spawn 控制台子进程(adb)会弹出黑窗，必须加 CREATE_NO_WINDOW
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class AdbError(Exception):
    pass


async def _run(args: list[str], timeout: float = 20, device: Optional[str] = None,
               input_data: Optional[bytes] = None) -> tuple[int, bytes, bytes]:
    cmd = [ADB_PATH]
    if device:
        cmd += ["-s", device]
    cmd += args
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        raise AdbError(f"找不到 adb: {ADB_PATH}（可用环境变量 NARCI_ADB 指定路径）")
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=input_data), timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise AdbError(f"adb 命令超时({timeout}s): {' '.join(args[:4])}...")
    return proc.returncode or 0, out, err


_INJECT_DENIED_MARKS = ("INJECT_EVENTS", "SecurityException")


def _friendly_adb_error(args: list[str], err_text: str) -> str:
    if any(m in err_text for m in _INJECT_DENIED_MARKS) and any(
            a in " ".join(args) for a in ("input", "keyevent")):
        return ("设备拒绝了输入注入（厂商安全限制）。"
                "小米/红米: 开发者选项 → 开启「USB 调试（安全设置）」后重插 USB；"
                "vivo: 「USB 模拟点击」；OPPO 类似。开启后 narciFarm 两条注入路径均会生效。")
    return err_text


async def _run_ok(args: list[str], timeout: float = 20, device: Optional[str] = None) -> bytes:
    rc, out, err = await _run(args, timeout, device)
    if rc != 0:
        err_text = err.decode("utf-8", "replace").strip()[:300]
        if rc != 0 and not err_text:
            raise AdbError(f"adb {' '.join(args[:3])} 失败(rc={rc})（无错误输出）")
        raise AdbError(f"adb {' '.join(args[:3])} 失败(rc={rc}): {_friendly_adb_error(args, err_text)}")
    return out


def server_available() -> bool:
    import os
    return os.path.exists(ADB_PATH)


# ---------- 设备发现 ----------

_LINE_RE = re.compile(r"^(\S+)\s+(device|offline|unauthorized|recovery|sideload)\b\s*(.*)$")


def parse_devices_list(text: str) -> list[dict]:
    devices = []
    for line in text.splitlines():
        line = line.strip()
        m = _LINE_RE.match(line)
        if not m:
            continue
        serial, state, props = m.group(1), m.group(2), m.group(3)
        info = {"serial": serial, "state": state, "model": "", "product": "", "device": "", "transport_id": ""}
        for kv in props.split():
            if ":" in kv:
                k, _, v = kv.partition(":")
                if k in info:
                    info[k] = v
        if info["model"]:
            info["model"] = info["model"].replace("_", " ")
        devices.append(info)
    return devices


async def list_devices() -> list[dict]:
    out = await _run_ok(["devices", "-l"], timeout=10)
    return parse_devices_list(out.decode("utf-8", "replace"))


async def connect(serial: str) -> str:
    out = await _run_ok(["connect", serial], timeout=10)
    return out.decode("utf-8", "replace").strip()


async def disconnect(serial: str) -> str:
    rc, out, _ = await _run(["disconnect", serial], timeout=10)
    return out.decode("utf-8", "replace").strip() or f"rc={rc}"


async def scan_lan(cidr: str = "192.168.1.0/24", port: int = 5555) -> list[str]:
    """扫描局域网开启 adb over TCP(5555) 的设备并尝试连接。"""
    import ipaddress
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = [str(h) for h in net.hosts()]
    found: list[str] = []
    sem = asyncio.Semaphore(64)

    async def probe(ip: str) -> Optional[str]:
        try:
            async with sem:
                fut = asyncio.open_connection(ip, port)
                reader, writer = await asyncio.wait_for(fut, timeout=0.4)
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            return ip
        except Exception:
            return None

    results = await asyncio.gather(*(probe(ip) for ip in hosts))
    found = [ip for ip in results if ip]
    for ip in found:
        with contextlib.suppress(AdbError):
            await connect(f"{ip}:{port}")
    return found


# ---------- 基本信息 ----------

_WM_RE = re.compile(r"(?:Physical|Override) size:\s*(\d+)x(\d+)")


async def get_screen_size(serial: str) -> tuple[int, int]:
    out = await _run_ok(["shell", "wm", "size"], device=serial, timeout=10)
    text = out.decode("utf-8", "replace")
    sizes = _WM_RE.findall(text)
    if not sizes:
        return 0, 0
    # Override size 优先（用户可改分辨率），否则用 Physical
    w, h = sizes[-1] if "Override" in text else sizes[0]
    return int(w), int(h)


async def get_build_info(serial: str) -> dict:
    rc, out, _ = await _run(
        ["shell", "getprop", "ro.build.version.release", "&", "getprop", "ro.build.version.sdk",
         "&", "getprop", "ro.product.model"],
        device=serial, timeout=10,
    )
    # 有些 shell 不支持 & 串接，退化为逐条执行
    if rc != 0 or not out.strip():
        parts = []
        for prop in ("ro.build.version.release", "ro.build.version.sdk", "ro.product.model"):
            o = await _run_ok(["shell", "getprop", prop], device=serial, timeout=8)
            parts.append(o.decode("utf-8", "replace").strip())
        release, sdk, model = (parts + ["", "0", ""])[:3]
    else:
        lines = [x.strip() for x in out.decode("utf-8", "replace").splitlines() if x.strip()]
        release = lines[0] if lines else ""
        sdk = lines[1] if len(lines) > 1 else "0"
        model = " ".join(lines[2:]) if len(lines) > 2 else ""
    return {"android_version": release, "sdk": int(sdk) if sdk.isdigit() else 0, "model": model}


async def get_battery(serial: str) -> dict:
    out = await _run_ok(["shell", "dumpsys", "battery"], device=serial, timeout=10)
    text = out.decode("utf-8", "replace")
    level = re.search(r"level:\s*(\d+)", text)
    status = re.search(r"status:\s*(\d+)", text)
    return {"level": int(level.group(1)) if level else -1, "status": int(status.group(1)) if status else -1}


# ---------- shell / 输入 ----------

async def shell(serial: str, cmd: str, timeout: float = 20) -> tuple[int, str]:
    rc, out, err = await _run(["shell", cmd], timeout=timeout, device=serial)
    text = out.decode("utf-8", "replace")
    if rc != 0 and not text:
        text = err.decode("utf-8", "replace")
    return rc, text


def _escape_input_text(s: str) -> str:
    r"""adb `input text` 的参数会经过设备端 sh 解析，需双重转义。

    空格用 %s 表示；双引号内反斜杠、双引号、美元符、反引号需转义；换行不支持（需 keyevent 66）。
    """
    s = s.replace("\r", " ").replace("\n", " ")
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    s = s.replace(" ", "%s")
    return f'input text "{s}"'


async def input_tap(serial: str, x: int, y: int) -> None:
    await _run_ok(["shell", f"input tap {int(x)} {int(y)}"], device=serial, timeout=15)


async def input_swipe(serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
    await _run_ok(
        ["shell", f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}"],
        device=serial, timeout=20,
    )


async def input_text(serial: str, text: str) -> None:
    await _run_ok(["shell", _escape_input_text(text)], device=serial, timeout=20)


async def input_keyevent(serial: str, keycode: int) -> None:
    await _run_ok(["shell", f"input keyevent {int(keycode)}"], device=serial, timeout=15)


KEYCODES = {
    "back": 4, "home": 3, "menu": 82, "power": 26, "volume_up": 24, "volume_down": 25,
    "mute": 164, "enter": 66, "delete": 67, "tab": 61, "recents": 187, "wake": 224, "sleep": 223,
}


async def launch_app(serial: str, package: str) -> None:
    rc, out = await shell(serial, f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1", timeout=25)
    if rc != 0 or "No activities found" in out or "Error" in out:
        await _run_ok(["shell", f"am start -n {shlex.quote(package)}"], device=serial, timeout=20)


async def stop_app(serial: str, package: str) -> None:
    await _run_ok(["shell", f"am force-stop {shlex.quote(package)}"], device=serial, timeout=15)


async def stay_awake(serial: str, on: bool) -> None:
    await _run_ok(["shell", f"svc power stayon {'true' if on else 'false'}"], device=serial, timeout=10)


# ---------- 屏幕唤醒（真机复现：熄屏时 input 注入 rc=0 但完全不生效） ----------

_wakefulness_re = re.compile(r"mWakefulness=(\w+)")
_awake_cache: dict[str, tuple[float, bool]] = {}
AWAKE_CHECK_TTL = 3.0  # 交互延迟权衡：3 秒内不重复探测


async def screen_awake(serial: str) -> Optional[bool]:
    """屏幕是否可交互（mWakefulness=Awake）。检测失败返回 None，由调用方放行。"""
    async def probe(args: list[str]) -> Optional[bool]:
        out = await _run_ok(args, device=serial, timeout=8)
        m = _wakefulness_re.search(out.decode("utf-8", "replace"))
        return (m.group(1) == "Awake") if m else None

    try:
        got = await probe(["shell", "dumpsys power | grep -m1 mWakefulness="])
        if got is not None:
            return got
    except AdbError:
        pass
    # 部分设备 shell 里没有 grep 或无该字段：退化为拉全量 dumpsys 本地解析
    try:
        return await probe(["shell", "dumpsys power"])
    except AdbError:
        return None


async def wake_up(serial: str) -> None:
    """KEYCODE_WAKEUP(224)：只唤醒，不会像 POWER(26) 那样有把屏幕按灭的风险。"""
    await input_keyevent(serial, 224)


async def ensure_awake(serial: str) -> Optional[str]:
    """注入前置守卫：熄屏时 input tap/keyevent 会“成功”但设备毫无反应。

    检测到熄屏先发唤醒键，并返回给界面展示的说明；探测/唤醒的任何异常都
    吞掉——检测本身绝不能拦下用户本来要执行的注入。
    """
    now = time.time()
    hit = _awake_cache.get(serial)
    awake = hit[1] if (hit and now - hit[0] < AWAKE_CHECK_TTL) else None
    if awake is None:
        awake = await screen_awake(serial)
        if awake is None:  # 探测不到（设备忙/不支持）：按原样放行
            return None
        _awake_cache[serial] = (now, awake)
    if awake:
        return None
    try:
        await wake_up(serial)
    except AdbError:
        return None
    _awake_cache[serial] = (time.time(), True)
    bus.warn(f"[{serial}] 设备熄屏，注入不会生效 → 已自动发送唤醒键（锁屏需手动解锁）")
    return "设备熄屏已自动唤醒；若停在锁屏请手动解锁后重新操作"


# ---------- 文件 ----------

async def push(serial: str, local: str, remote: str, timeout: float = 300) -> None:
    await _run_ok(["push", local, remote], device=serial, timeout=timeout)


async def install(serial: str, apk_path: str, extra_args: Optional[list[str]] = None,
                  timeout: float = 600) -> str:
    out = await _run_ok(["install", "-r", *(extra_args or []), apk_path], device=serial, timeout=timeout)
    return out.decode("utf-8", "replace").strip()


async def screenshot_bytes(serial: str, timeout: float = 20) -> bytes:
    """exec-out screencap -p 返回原始 PNG 字节。"""
    return await _run_ok(["exec-out", "screencap", "-p"], device=serial, timeout=timeout)


# ---------- 端口转发（scrcpy 隧道用） ----------

async def add_forward(serial: str, local_port: int, remote_abstract: str) -> None:
    await _run_ok(["forward", f"tcp:{local_port}", f"localabstract:{remote_abstract}"],
                  device=serial, timeout=10)


async def remove_forward(serial: str, local_port: int) -> None:
    with contextlib.suppress(AdbError):
        await _run(["forward", "--remove", f"tcp:{local_port}"], device=serial, timeout=10)


# ---------- UI 元素（uiautomator dump） ----------

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def _parse_bounds(s: str) -> Optional[tuple[int, int]]:
    m = _BOUNDS_RE.search(s or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _find_node(root: ET.Element, text: str) -> Optional[ET.Element]:
    for node in root.iter("node"):
        if node.get("text", "") == text or node.get("content-desc", "") == text:
            return node
    for node in root.iter("node"):
        t = node.get("text", "") + " " + node.get("content-desc", "")
        if text in t:
            return node
    return None


async def tap_text(serial: str, text: str, timeout: float = 25) -> tuple[int, int]:
    """通过 uiautomator dump 找到包含指定文本的节点并点击其中心。"""
    center = await find_text(serial, text, timeout=timeout)
    if center is None:
        raise AdbError(f"未找到文本节点: {text}")
    await input_tap(serial, center[0], center[1])
    return center


async def find_text(serial: str, text: str, timeout: float = 25) -> Optional[tuple[int, int]]:
    """查找包含指定文本的控件中心坐标（先精确匹配 text/content-desc，再模糊包含）。"""
    await _run_ok(["shell", "uiautomator", "dump", "/data/local/tmp/narci_uidump.xml"],
                  device=serial, timeout=timeout)
    rc, xml_text = await shell(serial, "cat /data/local/tmp/narci_uidump.xml", timeout=15)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    node = _find_node(root, text)
    if node is None:
        return None
    return _parse_bounds(node.get("bounds", ""))


async def list_apps(serial: str) -> list[str]:
    """列出第三方应用包名（pm list packages -3）。"""
    out = await _run_ok(["shell", "pm", "list", "packages", "-3"], device=serial, timeout=30)
    return sorted(line.strip()[8:] for line in out.decode("utf-8", "replace").splitlines()
                  if line.strip().startswith("package:"))


async def uninstall_app(serial: str, package: str) -> str:
    out = await _run_ok(["shell", f"pm uninstall {shlex.quote(package)}"], device=serial, timeout=60)
    return out.decode("utf-8", "replace").strip()


async def start_activity(serial: str, component: str) -> None:
    """直接拉起 Activity，component 形如 com.pkg/.MainActivity 或 com.pkg/com.pkg.MainActivity。"""
    await _run_ok(["shell", f"am start -n {shlex.quote(component)}"], device=serial, timeout=20)


async def get_current_focus(serial: str) -> str:
    out = await _run_ok(["shell", "dumpsys", "window", "|", "grep", "-E", "mCurrentFocus"],
                        device=serial, timeout=10)
    return out.decode("utf-8", "replace").strip()

# ---------- 开发工具: 文件管理 / 日志 / 性能 / 启动耗时 ----------

_LS_RE = re.compile(r"^([dl-])([rwxstST-]{9,10})\s+\d+\s+(\S+)\s+(\S+)\s+(\d+)\s+"
                    r"([\w\-: ]+\d{2}:\d{2})\s+(.+)$")


async def list_dir(serial: str, path: str) -> list[dict]:
    """列出设备目录(解析 ls -la)。/sdcard 等符号链接目录自动补 / 列出内容。"""
    out_path = path.rstrip("/") + "/"
    rc, out = await shell(serial, f"ls -la {shlex.quote(out_path)}", timeout=15)
    entries = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("total"):
            continue
        m = _LS_RE.match(line)
        if not m:
            continue
        ftype, perms, _uid, _gid, size, mtime, name = m.groups()
        if name in (".", ".."):
            continue
        entries.append({
            "name": name, "dir": ftype == "d", "size": int(size),
            "mtime": mtime.replace(" ", "T"), "perms": perms, "link": ftype == "l",
        })
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return entries


async def pull_file(serial: str, remote: str, local: str, timeout: float = 120) -> None:
    await _run_ok(["pull", remote, local], device=serial, timeout=timeout)


async def mkdir(serial: str, path: str) -> None:
    await _run_ok(["shell", f"mkdir -p {shlex.quote(path)}"], device=serial, timeout=15)


async def remove_path(serial: str, path: str) -> None:
    await _run_ok(["shell", f"rm -rf {shlex.quote(path)}"], device=serial, timeout=30)


async def resolve_launch_activity(serial: str, package: str) -> str:
    """返回应用的主 Activity 组件名(com.pkg/.MainActivity)。"""
    rc, out = await shell(serial, f"cmd package resolve-activity --brief {shlex.quote(package)}", timeout=20)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    for l in reversed(lines):
        if "/" in l and "." in l:
            return l
    raise AdbError(f"未找到启动 Activity: {package}")


async def startup_time(serial: str, package: str, cold: bool = True) -> dict:
    """测量应用启动耗时(am start -W 的 TotalTime/WaitTime)。"""
    if cold:
        with contextlib.suppress(AdbError):
            await stop_app(serial, package)
        await asyncio.sleep(0.5)
    comp = await resolve_launch_activity(serial, package)
    rc, out = await shell(serial, f"am start -W -n {shlex.quote(comp)}", timeout=40)
    total = re.search(r"TotalTime:\s*(\d+)", out)
    wait = re.search(r"WaitTime:\s*(\d+)", out)
    relaunch = re.search(r"thisTime:\s*(\d+)", out)
    return {
        "package": package, "component": comp, "cold": cold,
        "total_ms": int(total.group(1)) if total else -1,
        "wait_ms": int(wait.group(1)) if wait else -1,
        "this_ms": int(relaunch.group(1)) if relaunch else -1,
    }


async def get_metrics(serial: str) -> dict:
    """设备性能指标: CPU/内存/电池/温度。"""
    _, cpu_out = await shell(serial, "dumpsys cpuinfo", timeout=15)
    cpu_total = None
    m = re.search(r"(\d+(?:\.\d+)?)%\s*TOTAL", cpu_out)
    if m:
        cpu_total = float(m.group(1))
    _, mem_out = await shell(serial, "cat /proc/meminfo", timeout=15)
    mem_total = mem_free = None
    for line in mem_out.splitlines():
        if line.startswith("MemTotal:"):
            mem_total = int(re.search(r"(\d+)", line).group(1))
        elif line.startswith("MemAvailable:"):
            mem_free = int(re.search(r"(\d+)", line).group(1))
            break
    _, bat_out = await shell(serial, "dumpsys battery", timeout=15)
    battery = {}
    for key in ("level", "temperature", "status", "health"):
        mm = re.search(rf"{key}:\s*(\S+)", bat_out)
        if mm:
            battery[key] = mm.group(1)
    return {"cpu_total_pct": cpu_total, "mem_total_kb": mem_total, "mem_free_kb": mem_free,
            "battery": battery}

