"""adb 异步控制面：设备发现 / shell / 输入注入 / 文件 / 端口转发 / 截屏。

控制面与投屏面解耦：所有批量操作、场景步骤都走 adb，无需投屏在线。
"""
import asyncio
import contextlib
import re
import shlex
import xml.etree.ElementTree as ET
from typing import Optional

from .config import ADB_PATH
from .events import bus


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


async def _run_ok(args: list[str], timeout: float = 20, device: Optional[str] = None) -> bytes:
    rc, out, err = await _run(args, timeout, device)
    if rc != 0:
        raise AdbError(f"adb {' '.join(args[:3])} 失败(rc={rc}): {err.decode('utf-8', 'replace').strip()[:300]}")
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
    await _run_ok(["shell", "uiautomator", "dump", "/data/local/tmp/narci_uidump.xml"],
                  device=serial, timeout=timeout)
    rc, xml_text = await shell(serial, "cat /data/local/tmp/narci_uidump.xml", timeout=15)
    root = ET.fromstring(xml_text)
    node = _find_node(root, text)
    if node is None:
        raise AdbError(f"未找到文本节点: {text}")
    center = _parse_bounds(node.get("bounds", ""))
    if not center:
        raise AdbError(f"节点 {text} 无有效 bounds")
    await input_tap(serial, center[0], center[1])
    return center


async def get_current_focus(serial: str) -> str:
    out = await _run_ok(["shell", "dumpsys", "window", "|", "grep", "-E", "mCurrentFocus"],
                        device=serial, timeout=10)
    return out.decode("utf-8", "replace").strip()
