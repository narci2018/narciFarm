"""narciFarm Python SDK。

用 Python 驱动设备农场：登录、枚举设备、下发输入、运行场景、取报告。

    from narcifarm import Client

    with Client("http://127.0.0.1:8555") as c:
        c.login("admin", "admin")
        devices = c.devices()
        run_id = c.run_scenario(
            steps=[{"type": "launch", "package": "com.example.myapp"},
                   {"type": "wait", "ms": 3000},
                   {"type": "tap_el", "kind": "text", "value": "设置"},
                   {"type": "screenshot", "name": "settings"}],
            targets=[d["serial"] for d in devices],
            collect_metrics=True,
        )
        report = c.wait_report(run_id)

仅依赖 requests（或标准库 urllib），不涉及平台运营能力。
"""

from .client import Client, NarciError

__all__ = ["Client", "NarciError"]
__version__ = "0.9.12"
