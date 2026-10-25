"""控制（注入）链路诊断：定位"能投屏不能操作"的故障层。

用法: py -3 scripts/diagnose_control.py
前提: 手机已连接并被 adb 授权。

依次检测：
  [1] adb 基础注入（input tap 直连，排除 narciFarm 代码因素）
  [2] scrcpy 控制通道连接（是否成功建立控制 socket）
  [3] scrcpy 触控注入（直发 TYPE_INJECT_TOUCH，观察服务端是否报错）
每一步都会给出针对性结论。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from narci import adb
from narci.events import bus
from narci.scrcpy_client import ScrcpyClient
from narci.scrcpy_protocol import ACTION_DOWN, ACTION_UP, encode_inject_touch


async def step1_adb_input(serial: str) -> bool:
    print("\n===== [1] adb 基础注入: adb shell input tap 200 300 =====")
    rc, out = await adb.shell(serial, "input tap 200 300", timeout=15)
    text = (out or "").strip()
    print(f"rc={rc} output={text[:300]!r}")
    if rc != 0 or "Exception" in text or "SecurityException" in text or "Error" in text:
        print(">>> 结论: 设备层面就拒绝了 shell 注入（常见于 MIUI 未开「USB调试(安全设置)」、")
        print(">>> vivo「USB模拟点击」等厂商安全开关），与 narciFarm 无关。")
        return False
    print(">>> adb 注入命令执行成功（请确认手机画面上坐标(200,300)处是否产生了点击反馈）")
    return True


async def step2_and_3_scrcpy(serial: str) -> None:
    print("\n===== [2][3] scrcpy 控制通道 + 触控注入 =====")
    errors_before = _count_server_errors()
    client = ScrcpyClient(serial, max_size=960)
    try:
        await client.start()
        print(f"控制通道: {'已建立' if client._control else '未建立(!)'}")
        print(f"视频会话: {client.width}x{client.height}, device={client.device_name!r}")
        if not client.width:
            print(">>> 等待会话元数据...")
            for _ in range(20):
                await asyncio.sleep(0.2)
                if client.width:
                    break
        x, y = client.width // 2, client.height // 2
        print(f"注入测试: 点击屏幕中心 ({x},{y}) —— 请观察手机是否产生点击")
        await client.send_control(encode_inject_touch(ACTION_DOWN, x, y, client.width, client.height))
        await asyncio.sleep(0.05)
        await client.send_control(encode_inject_touch(ACTION_UP, x, y, client.width, client.height, 0.0))
        await asyncio.sleep(1.5)
        errors_after = _count_server_errors()
        new_errors = errors_after - errors_before
        if new_errors > 0:
            print(f">>> scrcpy 服务端报告了 {new_errors} 条注入相关错误（见上方 [server] 行）——")
            print(">>> 多为设备安全限制或显示映射问题，把 [server] 错误原文发给开发者。")
        else:
            print(">>> scrcpy 服务端无错误。若手机中心出现了点击，说明整条链路正常；")
            print(">>> 若仍无反应，问题在网页前端 → 后端的命令传递，请打开浏览器 F12 控制台截图反馈。")
    finally:
        await client.stop("诊断结束")


_server_error_marks = ("ERROR", "Exception", "Could not inject", "denied", "Security")


def _count_server_errors() -> int:
    return sum(1 for ev in bus.recent(800) if "[server]" in ev["msg"]
               and any(m.lower() in ev["msg"].lower() for m in _server_error_marks))


async def main() -> None:
    devices = [d for d in await adb.list_devices() if d["state"] == "device"]
    if not devices:
        print("没有在线设备。请插上手机、完成 USB 调试授权后重试。")
        sys.exit(1)
    serial = devices[0]["serial"]
    print(f"诊断设备: {serial} ({devices[0]['model']})")

    ok = await step1_adb_input(serial)
    await step2_and_3_scrcpy(serial)

    print("\n===== 汇总 =====")
    if not ok:
        print("故障层: 设备安全限制 → 按第 [1] 步提示开启对应厂商设置后重试。")
        print("小米/红米: 开发者选项 → 「USB 调试（安全设置）」（需登录小米账号并插 SIM 卡）。")
    else:
        print("adb 注入正常；请结合 [3] 的现场观察与服务端错误输出定位。")


if __name__ == "__main__":
    asyncio.run(main())
