"""步骤插件机制：允许以 Python 模块扩展场景步骤类型。

插件是一个放在插件目录下的 .py 文件，暴露一个 register(registry) 函数：

    # my_step.py
    from narci import adb

    async def run_wait_package(step, serial, size, run_id):
        pkg = step["package"]
        await adb.launch_app(serial, pkg)
        return {"package": pkg}

    def register(reg):
        reg.step("launch_by_name", run_wait_package)
        reg.step("demo_assert", lambda step, serial, size, run_id: {"ok": True})

内置步骤优先；插件只对未被内置识别的步骤类型生效，因此不会覆盖原有行为。
插件目录由 NARCI_PLUGINS_DIR 指定（默认 <data>/plugins），无需 root、不涉及平台操作。
"""
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("narci.plugins")


class StepRegistry:
    """步骤类型注册表。内置步骤不在此登记，插件仅作为补充。"""

    def __init__(self) -> None:
        self._handlers: dict[str, object] = {}
        self._meta: dict[str, dict] = {}

    def step(self, name: str, handler, description: str = "", fields: Optional[list] = None) -> None:
        """注册一个步骤处理器。handler(step, serial, size, run_id) -> dict。"""
        if not callable(handler):
            raise ValueError(f"插件步骤 {name} 的处理器不可调用")
        self._handlers[name] = handler
        self._meta[name] = {"name": name, "description": description,
                            "fields": fields or [], "builtin": False}

    def get(self, name: str):
        return self._handlers.get(name)

    def list(self) -> list[dict]:
        return sorted(self._meta.values(), key=lambda m: m["name"])

    def __contains__(self, name: str) -> bool:
        return name in self._handlers


REGISTRY = StepRegistry()


def load_plugins(directory: Optional[Path] = None) -> list[str]:
    """从目录加载所有 .py 插件，返回加载的插件名列表（失败项记日志跳过）。"""
    if directory is None:
        return []
    directory = Path(directory)
    if not directory.is_dir():
        return []
    loaded: list[str] = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"narci_plugin_{path.stem}", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            register = getattr(mod, "register", None)
            if callable(register):
                register(REGISTRY)
                loaded.append(path.stem)
                log.info("已加载插件: %s", path.stem)
            else:
                log.warning("插件 %s 缺少 register(registry) 函数，已跳过", path.name)
        except Exception as e:
            log.warning("插件 %s 加载失败: %s", path.name, e)
    return loaded


def plugin_dir() -> Path:
    """插件目录：NARCI_PLUGINS_DIR 或 <data>/plugins。"""
    import os
    from .config import DATA_DIR
    d = os.environ.get("NARCI_PLUGINS_DIR")
    return Path(d) if d else DATA_DIR / "plugins"


def load_all() -> list[str]:
    """按配置加载插件目录（服务启动时调用一次）。"""
    return load_plugins(plugin_dir())
