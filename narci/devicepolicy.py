"""设备托管（Device Owner / MDM 层）能力。

**前置条件**：设备必须有一个 Device Owner（DPC）应用。设置方式（设备需无账号、未配置过）：

    adb shell dpm set-device-owner <dpc包名>/<DeviceAdminReceiver全限定名>

本模块提供：
- provisioning：设置 / 移除 Device Owner（走 `dpm` shell 命令）
- 状态探测：解析 `dumpsys device_policy`
- 策略下发：以广播方式把策略命令发给 DPC（广播契约见 docs/使用手册.md）

Android 版本众多，`dumpsys` 输出格式有差异，解析器做了容错；解析不到的字段留空而不报错。
"""
import contextlib
import re
import shlex
from typing import Optional

from . import adb
from .events import bus

# 广播契约：DPC 需监听该 action，并读取 extra "command" 与命令参数
POLICY_ACTION = "io.narcifarm.POLICY"

# 支持的策略命令
POLICY_COMMANDS = (
    "lock_now", "wipe", "set_kiosk", "clear_kiosk",
    "suspend_app", "unsuspend_app",
    "set_factory_reset_allowed", "set_camera_disabled", "set_screen_capture_disabled",
    # 网络与证书（需 Device Owner）
    "set_wifi", "set_always_on_vpn", "install_ca_cert", "clear_ca_certs",
)


def _find_owner(text: str) -> Optional[str]:
    """从 dumpsys 输出里找 Device Owner 包名（容错多种格式）。

    Android 各版本格式不一：有的同一行 `Device Owner: com.x.dpc`，
    有的把包名缩进到下一行，还有的写成 `mDeviceOwner: com.x/.Receiver`。
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        low = line.lower()
        if "device owner" not in low and "deviceowner" not in low.replace(" ", ""):
            continue
        tail = line.split(":", 1)[1].strip() if ":" in line else ""
        if re.fullmatch(r"[\w.]+", tail):
            return tail
        # 包名在下一行（缩进）
        for nxt in lines[i + 1:i + 4]:
            s = nxt.strip()
            if re.fullmatch(r"[\w.]+", s):
                return s
            if s:
                break
    # 兜底：mDeviceOwner: com.x.dpc/.MyDeviceAdminReceiver
    m = re.search(r"mDeviceOwner\s*[:=]\s*([\w.]+)", text)
    if m:
        return m.group(1)
    return None


def _flag(text: str, *needles: str) -> Optional[bool]:
    """在 dumpsys 输出里找形如 `key: value` 的标志位，返回布尔值（找不到返回 None）。"""
    for line in text.splitlines():
        for n in needles:
            if n.lower() in line.lower():
                m = re.search(r"[:=]\s*(true|false|1|0|yes|no)\s*$", line, re.I)
                if m:
                    return m.group(1).lower() in ("true", "1", "yes")
                return None
    return None


async def policy_status(serial: str) -> dict:
    """读取设备托管状态。解析不到的字段为 None（表示未知），不抛异常。"""
    rc, out = await adb.shell(serial, "dumpsys device_policy", timeout=25)
    text = out or ""
    owner = _find_owner(text)
    # Admin{<hash> <package>} —— 取花括号内的包名 token
    admins = []
    for m in re.finditer(r"Admin\{([^}]*)\}", text):
        for tok in m.group(1).split():
            if re.fullmatch(r"[\w.]+", tok) and "." in tok:
                admins.append(tok)
    admins = sorted(set(admins))
    lock_task = None
    m = re.search(r"[Ll]ock\s*[Tt]ask\s*[Pp]ackages?[\s:]*\[([^\]]*)\]", text)
    if m:
        lock_task = [p.strip() for p in m.group(1).split(",") if p.strip()] or None
    return {
        "serial": serial,
        "has_owner": owner is not None,
        "owner_package": owner,
        "admin_packages": admins,
        "lock_task_packages": lock_task,
        "camera_disabled": _flag(text, "mCameraDisabled", "camera disabled"),
        "factory_reset_allowed": _flag(text, "mFactoryResetAllowed", "factory reset allowed"),
        "screen_capture_disabled": _flag(text, "mScreenCaptureDisabled", "screen capture disabled"),
        # parsed 表示确实拿到了 dumpsys 输出（而非空响应或错误文本）
        "parsed": bool(text.strip()) and "device policy manager" in text.lower(),
    }


async def set_device_owner(serial: str, component: str) -> dict:
    """把指定 DPC 设为 Device Owner。

    component 形如 `com.example.dpc/.MyDeviceAdminReceiver`（需设备上已安装该应用，
    且设备未配置过任何账号/Device Owner）。失败抛 AdbError。
    """
    comp = component.strip()
    if "/" not in comp:
        raise adb.AdbError("component 格式应为 包名/DeviceAdminReceiver 全限定名")
    bus.info(f"设置 Device Owner: {serial} -> {comp}")
    await adb._run_ok(["shell", f"dpm set-device-owner {shlex.quote(comp)}"],
                      device=serial, timeout=30)
    return {"ok": True, "component": comp}


async def remove_device_owner(serial: str, component: str) -> dict:
    """移除 Device Owner。"""
    comp = component.strip()
    if "/" not in comp:
        raise adb.AdbError("component 格式应为 包名/DeviceAdminReceiver 全限定名")
    await adb._run_ok(["shell", f"dpm remove-active-admin --user 0 {shlex.quote(comp)}"],
                      device=serial, timeout=30)
    return {"ok": True, "component": comp}


async def push_ca_cert(serial: str, local_path: str, remote_path: str = "/data/local/tmp/narci_ca.crt") -> str:
    """把 CA 证书推到设备（安装证书前的前置步骤）。返回远端路径。"""
    await adb.push(serial, local_path, remote_path)
    return remote_path


async def send_policy_command(serial: str, command: str, params: Optional[dict] = None) -> dict:
    """把策略命令以广播形式发给 Device Owner（DPC 需监听 io.narcifarm.POLICY）。"""
    if command not in POLICY_COMMANDS:
        raise adb.AdbError(f"不支持的策略命令: {command}（可选 {', '.join(POLICY_COMMANDS)}）")
    status = await policy_status(serial)
    owner = status.get("owner_package")
    if not owner:
        raise adb.AdbError("设备未设置 Device Owner，无法下发策略命令")
    args = ["shell", f"am broadcast -a {POLICY_ACTION} -p {owner} --es command {command}"]
    for k, v in (params or {}).items():
        if isinstance(v, bool):
            args.append(f"--ez {k} {str(v).lower()}")
        elif isinstance(v, int):
            args.append(f"--ei {k} {v}")
        else:
            args.append(f"--es {k} {shlex.quote(str(v))}")
    with contextlib.suppress(adb.AdbError):
        await adb._run_ok(args, device=serial, timeout=20)
    bus.info(f"策略命令[{serial}]: {command} {params or ''}")
    return {"ok": True, "command": command, "owner": owner, "params": params or {}}
