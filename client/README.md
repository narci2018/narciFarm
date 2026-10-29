# narciFarm Python SDK

用 Python 驱动设备农场：登录、枚举设备、下发输入、运行场景、取报告。仅依赖标准库。

## 安装

```bash
pip install ./client
```

或直接：

```bash
set PYTHONPATH=client && python your_script.py
```

## 快速上手

```python
from narcifarm import Client

with Client("http://127.0.0.1:8555") as c:
    c.login("admin", "admin")
    devices = c.devices()
    targets = [d["serial"] for d in devices if d["state"] == "device"]

    run_id = c.run_scenario(
        steps=[
            {"type": "launch", "package": "com.example.myapp"},
            {"type": "wait", "ms": 3000},
            {"type": "tap_el", "kind": "text", "value": "设置"},
            {"type": "wait_el", "kind": "id", "value": "com.example:id/done",
             "timeout_ms": 8000, "retry": 2, "retry_interval_ms": 1000},
            {"type": "assert_text", "text": r"版本 \d+", "match": "regex"},
            {"type": "screenshot", "name": "settings"},
        ],
        targets=targets,
        collect_metrics=True,
        metrics_interval=2,
    )
    report = c.wait_report(run_id)
    print(report["status"], report["summary"])
```

## 主要方法

| 方法 | 说明 |
|---|---|
| `login(username, password)` / `logout()` | 认证；`with Client(...)` 会自动登出 |
| `devices()` / `device(serial)` | 设备列表 / 单台详情 |
| `hierarchy(serial)` | 控件树（含层级深度与属性） |
| `find_elements(serial, kind, value, contains)` | 按 text/id/desc/class/package/xpath 查找控件 |
| `metrics(serial)` | 即时性能指标（CPU/内存/电量/温度） |
| `input(action, params, serials)` | 批量输入注入（tap/swipe/key/text…） |
| `scenarios()` / `create_scenario(name, steps, repeat_count)` | 场景管理 |
| `run_scenario(steps, targets, ...)` | 运行场景，返回 `run_id` |
| `runs()` / `abort_run(run_id)` | 运行状态 / 中止 |
| `wait_finished(run_id)` / `wait_report(run_id)` | 等待结束 / 取回报告 |
| `reports()` / `report(id)` / `report_html(id)` / `report_junit(id)` | 报告列表与导出 |
| `plugins()` | 已注册的步骤插件 |
| `batch_actions()` / `batch_action(action, targets, package)` | 批量快捷操作（重启/飞行模式/找机/清数据） |
| `netlimit_status()` / `netlimit_start(...)` / `netlimit_stop(targets)` | 弱网模拟（带宽/延迟/丢包） |
| `policy_status(serial)` | 设备托管状态（Device Owner / Kiosk / 相机限制） |
| `policy_commands()` | 支持的设备托管策略命令 |
| `set_device_owner(serial, component)` / `remove_device_owner(...)` | 设置 / 移除 Device Owner |
| `policy_command(serial, command, params)` | 下发策略命令（锁屏/擦除/Kiosk/相机限制） |
| `install_ca_cert(serial, cert_path, alias)` | 上传并安装 CA 证书（Wi-Fi/VPN 命令走 policy_command） |

## CI 集成示例

```python
from narcifarm import Client

def test_regression():
    with Client("http://127.0.0.1:8555") as c:
        c.login("admin", "admin")
        targets = [d["serial"] for d in c.devices() if d["state"] == "device"]
        assert targets, "没有在线设备"
        run_id = c.run_scenario([{"type": "wait", "ms": 500}], targets)
        report = c.wait_report(run_id, timeout=300)
        assert report["status"] == "passed", report
```

## 弱网模拟示例

```python
from narcifarm import Client

with Client("http://127.0.0.1:8555") as c:
    c.login("admin", "admin")
    targets = [d["serial"] for d in c.devices() if d["state"] == "device"]
    # 500 kbps + 200ms 延迟 + 5% 丢包
    print(c.netlimit_start(targets, rate_kbps=500, latency_ms=200, loss_pct=5))
    # ... 跑场景 ...
    print(c.netlimit_stop(targets))
```

## 说明

- 所有方法都是对 REST 接口的薄封装，不引入额外依赖。
- 出错抛 `narcifarm.NarciError`（含 HTTP 状态码与服务端错误详情）。
- SDK 只做设备编排：不包含任何平台定向脚本、账号注册或流量操作。
