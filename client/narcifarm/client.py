"""narciFarm 服务端 HTTP 客户端。

只封装设备农场本身的接口（设备、投屏地址、输入注入、场景运行、报告），
不做任何平台定向操作。所有方法都是对 REST 接口的薄封装，便于在 CI 中编排。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


class NarciError(RuntimeError):
    """请求失败或服务端返回错误。"""


class Client:
    def __init__(self, base_url: str, token: Optional[str] = None,
                 timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---------- 连接 ----------

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.logout()
        except Exception:
            pass

    # ---------- 底层请求 ----------

    def _request(self, method: str, path: str, body: Any = None,
                 params: Optional[dict] = None) -> Any:
        url = self.base_url + path
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url += ("&" if "?" in url else "?") + qs
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise NarciError(f"HTTP {e.code} {method} {path}: {detail}") from e
        except urllib.error.URLError as e:
            raise NarciError(f"无法连接 {self.base_url}: {e.reason}") from e
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    # ---------- 认证 ----------

    def login(self, username: str, password: str) -> dict:
        r = self._request("POST", "/api/login", {"username": username, "password": password})
        self.token = r.get("token")
        return r.get("user", {})

    def logout(self) -> None:
        if not self.token:
            return
        try:
            self._request("POST", "/api/logout")
        finally:
            self.token = None

    # ---------- 设备 ----------

    def devices(self) -> list:
        return self._request("GET", "/api/devices")

    def device(self, serial: str) -> dict:
        return self._request("GET", f"/api/devices/{serial}")

    def hierarchy(self, serial: str) -> list:
        r = self._request("GET", f"/api/devices/{serial}/hierarchy")
        return r.get("nodes", []) if isinstance(r, dict) else []

    def find_elements(self, serial: str, kind: str, value: str,
                      contains: bool = False) -> list:
        r = self._request("GET", f"/api/devices/{serial}/elements",
                          params={"kind": kind, "value": value, "contains": contains})
        return r.get("elements", []) if isinstance(r, dict) else []

    def metrics(self, serial: str) -> dict:
        return self._request("GET", f"/api/devices/{serial}/metrics")

    def input(self, action: str, params: dict, serials: list) -> dict:
        return self._request("POST", "/api/input",
                             {"action": action, "params": params, "serials": serials})

    def mirror_url(self, serial: str) -> str:
        return f"{self.base_url}/static/mirror.html?serial={serial}" if False else \
            f"ws://{self.base_url.split('//', 1)[-1]}/ws/mirror/{serial}"

    # ---------- 场景 ----------

    def scenarios(self) -> list:
        return self._request("GET", "/api/scenarios")

    def create_scenario(self, name: str, steps: list, repeat_count: int = 1) -> dict:
        return self._request("POST", "/api/scenarios",
                             {"name": name, "steps": steps, "repeat_count": repeat_count})

    def run_scenario(self, steps: list, targets: list, repeat_count: int = 1,
                     variables: Optional[dict] = None, collect_metrics: bool = False,
                     metrics_interval: float = 2.0, scenario_id: Optional[int] = None,
                     create_name: Optional[str] = None) -> str:
        """运行场景：可直接传步骤，也可传已保存的 scenario_id。返回 run_id。"""
        if scenario_id is not None:
            r = self._request("POST", f"/api/scenarios/{scenario_id}/run",
                              {"targets": targets})
            return r["run_id"]
        sc = self.create_scenario(create_name or f"sdk_{int(time.time())}", steps, repeat_count)
        r = self._request("POST", f"/api/scenarios/{sc['id']}/run",
                          {"targets": targets, "vars": variables or {},
                           "collect_metrics": collect_metrics,
                           "metrics_interval": metrics_interval})
        return r["run_id"]

    def runs(self) -> list:
        return self._request("GET", "/api/runs")

    def abort_run(self, run_id: str) -> bool:
        return bool(self._request("POST", f"/api/runs/{run_id}/abort").get("ok"))

    def wait_finished(self, run_id: str, timeout: float = 600.0,
                      poll: float = 1.0) -> str:
        """轮询直到运行结束，返回最终状态。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for r in self.runs():
                if r.get("run_id") == run_id:
                    running = any(d.get("status") == "running"
                                  for d in (r.get("devices") or {}).values())
                    if not running:
                        return "finished"
            time.sleep(poll)
        raise NarciError(f"等待运行超时({timeout:.0f}s): {run_id}")

    def report_id(self, run_id: str) -> Optional[str]:
        for r in self.runs():
            if r.get("run_id") == run_id:
                return r.get("report_id")
        return None

    def wait_report(self, run_id: str, timeout: float = 600.0,
                    poll: float = 1.0) -> dict:
        """等待运行结束并取回报告。超时抛 NarciError。"""
        self.wait_finished(run_id, timeout=timeout, poll=poll)
        # 报告由运行收尾时落盘，稍等片刻再取
        for _ in range(int(timeout)):
            rid = self.report_id(run_id)
            if rid:
                rep = self._request("GET", f"/api/reports/{rid}")
                if rep and rep.get("devices"):
                    return rep
            time.sleep(poll)
        raise NarciError(f"未取到报告: {run_id}")

    def reports(self) -> list:
        return self._request("GET", "/api/reports")

    def report(self, report_id: str) -> dict:
        return self._request("GET", f"/api/reports/{report_id}")

    def report_html(self, report_id: str) -> str:
        return self._request("GET", f"/api/reports/{report_id}.html")

    def report_junit(self, report_id: str) -> str:
        return self._request("GET", f"/api/reports/{report_id}.xml")

    # ---------- 批量快捷操作 ----------

    def batch_actions(self) -> list:
        """可用的批量操作清单。"""
        return self._request("GET", "/api/batch/actions")

    def batch_action(self, action: str, targets: list,
                     package: Optional[str] = None) -> dict:
        """对多台设备执行快捷操作（重启/飞行模式/找机/清除数据等）。"""
        body = {"action": action, "targets": targets}
        if package:
            body["package"] = package
        return self._request("POST", "/api/batch/action", body)

    # ---------- 弱网模拟 ----------

    def netlimit_status(self) -> dict:
        return self._request("GET", "/api/netlimit/status")

    def netlimit_start(self, targets: list, rate_kbps: float = 500,
                       latency_ms: int = 0, loss_pct: float = 0,
                       host: Optional[str] = None) -> dict:
        """开启弱网模拟：把设备代理指向本机节流代理。"""
        body = {"targets": targets, "rate_kbps": rate_kbps,
                "latency_ms": latency_ms, "loss_pct": loss_pct}
        if host:
            body["host"] = host
        return self._request("POST", "/api/netlimit/start", body)

    def netlimit_stop(self, targets: list) -> dict:
        """关闭弱网模拟并清除设备代理。"""
        return self._request("POST", "/api/netlimit/stop", {"targets": targets})

    # ---------- 其他 ----------

    def plugins(self) -> dict:
        return self._request("GET", "/api/plugins")

    def settings(self) -> dict:
        return self._request("GET", "/api/settings")
