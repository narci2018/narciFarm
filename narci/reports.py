"""测试报告：运行结果持久化 + JSON / HTML / JUnit XML 导出。

报告在场景运行时增量写入磁盘，进程重启不丢失；前端可查看、下载与对比。
"""
import html
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from .config import REPORTS_DIR

REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _report_path(report_id: str) -> Path:
    return REPORTS_DIR / f"{report_id}.json"


def new_report_id() -> str:
    return uuid.uuid4().hex[:16]


def _summarize_metrics(samples: list) -> dict:
    """把性能指标采样序列整理成摘要 + 原始采样（上限 200 点，避免报告过大）。"""
    if not samples:
        return {"samples": 0, "series": []}

    def _col(key):
        return [m[key] for m in samples if m.get(key) is not None]

    cpu, mem = _col("cpu_used_pct"), _col("mem_used_pct")
    batt, temp = _col("battery_pct"), _col("battery_temp_c")
    return {
        "samples": len(samples),
        "cpu_max": round(max(cpu), 1) if cpu else None,
        "cpu_avg": round(sum(cpu) / len(cpu), 1) if cpu else None,
        "mem_max": round(max(mem), 1) if mem else None,
        "mem_avg": round(sum(mem) / len(mem), 1) if mem else None,
        "battery_start": batt[0] if batt else None,
        "battery_end": batt[-1] if batt else None,
        "battery_drop": (batt[0] - batt[-1]) if len(batt) >= 2 else None,
        "temp_max": round(max(temp), 1) if temp else None,
        "series": samples[:200],
    }


def build_report(run_id: str, run: Optional[dict], scenario_name: str = "",
                 steps: Optional[list] = None) -> dict:
    """把一次场景运行整理成结构化报告。run 为 scenario._runs 中的运行字典。"""
    if run is None:
        return {"report_id": None, "run_id": run_id, "status": "unknown",
                "scenario": scenario_name, "devices": {}, "steps": steps or [],
                "created_at": time.time()}
    devices = {}
    for serial, st in run.get("devices", {}).items():
        results = st.get("results", [])
        devices[serial] = {
            "status": st.get("status"),
            "done": st.get("done", 0),
            "current": st.get("current", 0),
            "repeat": st.get("repeat", 0),
            "error": st.get("error"),
            "failure_shot": st.get("failure_shot"),
            "results": results,
            "metrics": _summarize_metrics(st.get("metrics") or []),
            "passed": sum(1 for r in results if not r.get("skipped") and not r.get("error")),
            "failed": sum(1 for r in results if r.get("skipped") or r.get("error")),
        }
    total = sum(d["done"] for d in devices.values())
    failed = sum(d["failed"] for d in devices.values())
    # 任一设备报错/中止，或存在被跳过(未通过)的步骤，都算失败——CI 才能拦住
    overall = "passed"
    if (any(d["status"] in ("error", "aborted") for d in devices.values())
            or failed > 0):
        overall = "failed"
    elif any(d["status"] == "running" for d in devices.values()):
        overall = "running"
    return {
        "report_id": new_report_id(),
        "run_id": run_id,
        "scenario": scenario_name,
        "status": overall,
        "repeat": run.get("repeat", 1),
        "started_at": run.get("started_at"),
        "created_at": time.time(),
        "duration_s": round(time.time() - run.get("started_at", time.time()), 2),
        "steps": steps or [],
        "summary": {"devices": len(devices), "steps_total": total, "steps_failed": failed},
        "devices": devices,
    }


def save_report(report: dict) -> Path:
    """落盘报告，返回文件路径。"""
    path = _report_path(report["report_id"])
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_report(report_id: str) -> Optional[dict]:
    path = _report_path(report_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_reports() -> list[dict]:
    out = []
    for p in sorted(REPORTS_DIR.glob("*.json"), key=lambda x: -x.stat().st_mtime):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "report_id": r.get("report_id"),
            "run_id": r.get("run_id"),
            "scenario": r.get("scenario", ""),
            "status": r.get("status"),
            "created_at": r.get("created_at"),
            "duration_s": r.get("duration_s"),
            "summary": r.get("summary", {}),
        })
    return out


def delete_report(report_id: str) -> bool:
    path = _report_path(report_id)
    if path.exists():
        path.unlink()
        return True
    return False


def render_junit(report: dict) -> str:
    """JUnit XML：每台设备一个 testcase，失败步骤作为 failure。"""
    esc = html.escape
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    failures = report.get("summary", {}).get("steps_failed", 0)
    lines.append(
        f'<testsuite name="{esc(str(report.get("scenario", "narciFarm")))}" '
        f'tests="{len(report.get("devices", {}))}" failures="{failures}" '
        f'time="{report.get("duration_s", 0)}" timestamp="{_iso(report.get("created_at"))}">'
    )
    for serial, d in report.get("devices", {}).items():
        case_time = round(sum(float(r.get("elapsed", 0) or 0) for r in d.get("results", [])), 3)
        lines.append(
            f'  <testcase name="{esc(serial)}" classname="narciFarm.scenario" time="{case_time}">'
        )
        if d.get("error"):
            lines.append(
                f'    <failure message="{esc(str(d["error"]))}">'
                f'{esc(str(d.get("failure_shot") or ""))}</failure>'
            )
        elif d.get("status") == "aborted":
            lines.append('    <skipped message="运行被中止"/>')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines)


_STYLE = (
    "body{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:24px;color:#1c1c1e;background:#fff}\n"
    "h1{font-size:20px;margin:0 0 4px}\n"
    "h2{font-size:16px;margin:24px 0 8px}\n"
    ".meta{color:#666;font-size:13px;margin-bottom:16px}\n"
    ".badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;color:#fff}\n"
    ".pass{background:#2f9e44}.fail{background:#e03131}.run{background:#1971c2}\n"
    "table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:16px}\n"
    "th,td{border:1px solid #e0e0e0;padding:6px 8px;text-align:left;vertical-align:top}\n"
    "th{background:#f5f5f5}\n"
    "tr.fail td{background:#fff5f5}\n"
    ".hint{color:#888;font-size:12px}\n"
)


def render_html(report: dict) -> str:
    """自包含 HTML 报告（内联样式，无外部依赖）。"""
    esc = html.escape
    rows = []
    for serial, d in report.get("devices", {}).items():
        for r in d.get("results", []):
            is_fail = bool(r.get("skipped") or r.get("error"))
            detail = r.get("error") or json.dumps(
                {k: v for k, v in r.items() if k not in ("index", "type", "elapsed")},
                ensure_ascii=False,
            )
            if is_fail and d.get("failure_shot"):
                shot = "<td><div class='hint'>取证截图: " + esc(d["failure_shot"]) + "</div></td>"
            else:
                shot = "<td></td>"
            rows.append(
                "<tr class='" + ("fail" if is_fail else "ok") + "'>"
                "<td>" + esc(serial) + "</td>"
                "<td>" + esc(str(r.get("index", ""))) + "</td>"
                "<td>" + esc(str(r.get("type", ""))) + "</td>"
                "<td>" + esc(str(r.get("elapsed", ""))) + "</td>"
                "<td>" + esc(str(detail)) + "</td>" + shot + "</tr>"
            )
    body = "\n".join(rows) or "<tr><td colspan='6' class='hint'>无步骤记录</td></tr>"

    metric_rows = []
    for serial, d in report.get("devices", {}).items():
        m = d.get("metrics") or {}
        if not m.get("samples"):
            continue
        metric_rows.append(
            "<tr><td>" + esc(serial) + "</td>"
            "<td>" + esc(str(m.get("samples"))) + "</td>"
            "<td>" + esc(str(m.get("cpu_max"))) + "</td>"
            "<td>" + esc(str(m.get("mem_max"))) + "</td>"
            "<td>" + esc(str(m.get("battery_start"))) + " → "
            + esc(str(m.get("battery_end"))) + "</td>"
            "<td>" + esc(str(m.get("temp_max"))) + "</td></tr>"
        )
    metrics_table = ""
    if metric_rows:
        metrics_table = (
            "<h2>性能指标</h2>"
            "<table><thead><tr><th>设备</th><th>采样数</th><th>CPU 峰值%</th>"
            "<th>内存峰值%</th><th>电量</th><th>电池温度峰值℃</th></tr></thead><tbody>"
            + "".join(metric_rows) + "</tbody></table>"
        )

    status = report.get("status", "unknown")
    badge = {"passed": "pass", "failed": "fail"}.get(status, "run")
    summary = report.get("summary", {})
    return (
        "<!DOCTYPE html>\n"
        "<html lang='zh-CN'><head><meta charset='utf-8'>\n"
        "<title>narciFarm 报告 " + esc(str(report.get("report_id", ""))) + "</title>\n"
        "<style>\n" + _STYLE + "</style></head><body>\n"
        "<h1>narciFarm 场景测试报告</h1>\n"
        "<div class='meta'>\n"
        "场景: <b>" + esc(str(report.get("scenario", ""))) + "</b> ·\n"
        "运行: <code>" + esc(str(report.get("run_id", ""))) + "</code> ·\n"
        "状态: <span class='badge " + badge + "'>" + esc(status) + "</span> ·\n"
        "设备数: " + esc(str(summary.get("devices", 0))) + " ·\n"
        "步骤: " + esc(str(summary.get("steps_total", 0))) + " ·\n"
        "失败: " + esc(str(summary.get("steps_failed", 0))) + " ·\n"
        "耗时: " + esc(str(report.get("duration_s", 0))) + "s\n"
        "</div>\n"
        "<table><thead><tr><th>设备</th><th>#</th><th>步骤</th><th>耗时s</th><th>详情</th><th>取证</th></tr></thead>\n"
        "<tbody>\n" + body + "\n</tbody></table>\n"
        + metrics_table +
        "</body></html>"
    )


def _iso(ts) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
