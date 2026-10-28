"""narciFarm 步骤插件示例。

用法：把本文件复制到插件目录（默认 <data>/plugins，或由 NARCI_PLUGINS_DIR 指定），
重启服务后在「开发工具 → 步骤插件」里能看到新类型，并可直接写进场景步骤。

插件只补充内置未识别的步骤类型，因此不会覆盖原有行为。
"""
from narci import adb


async def run_click_center(step, serial, size, run_id):
    """在屏幕中心点击（演示：最简单的步骤实现）。"""
    w, h = size or (0, 0)
    x, y = w // 2, h // 2
    await adb.input_tap(serial, x, y)
    return {"x": x, "y": y}


async def run_assert_pkg_installed(step, serial, size, run_id):
    """断言指定包已安装（演示：带返回值的断言型步骤）。"""
    pkg = step["package"]
    rc, out = await adb.shell(serial, f"pm list packages {pkg}", timeout=20)
    installed = pkg in (out or "")
    if step.get("expect", "installed") == "installed" and not installed:
        raise RuntimeError(f"包未安装: {pkg}")
    return {"package": pkg, "installed": installed}


async def run_shell_logged(step, serial, size, run_id):
    """执行 shell 并把输出写进运行结果（演示：透传 shell）。"""
    rc, out = await adb.shell(serial, step["cmd"], timeout=float(step.get("timeout", 30)))
    return {"rc": rc, "out": (out or "")[:2000]}


def register(reg):
    """服务启动时被调用一次，向注册表登记本文件提供的步骤类型。"""
    reg.step("click_center", run_click_center,
             description="点击屏幕中心", fields=[])
    reg.step("assert_pkg_installed", run_assert_pkg_installed,
             description="断言指定包已安装", fields=["package", "expect"])
    reg.step("shell_logged", run_shell_logged,
             description="执行 shell 并记录输出", fields=["cmd", "timeout"])
