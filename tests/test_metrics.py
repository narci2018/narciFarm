"""性能指标采集与报告 单元测试。"""
import asyncio

from narci import adb, reports


def _fake_shell_factory(battery, meminfo, top):
    async def fake_shell(serial, cmd, timeout=30):
        if "dumpsys battery" in cmd:
            return 0, battery
        if "meminfo" in cmd:
            return 0, meminfo
        if "top" in cmd:
            return 0, top
        return 1, ""
    return fake_shell


def test_read_metrics_parses_all_fields(monkeypatch):
    battery = "  level: 87\n  temperature: 315\n  status: 2\n"
    meminfo = "MemTotal: 7823412 kB\nMemFree: 1234567 kB\nMemAvailable: 3456789 kB\n"
    top = "CPU: 12%usr 8%sys 0%nic 79%idle 0%io 0%irq\n"
    monkeypatch.setattr(adb, "shell", _fake_shell_factory(battery, meminfo, top))
    m = asyncio.run(adb.read_metrics("S"))
    assert m["battery_pct"] == 87
    assert m["battery_temp_c"] == 31.5
    assert m["mem_total_mb"] == round(7823412 / 1024, 1)
    assert m["mem_avail_mb"] == round(3456789 / 1024, 1)
    assert m["mem_used_pct"] == round((7823412 - 3456789) / 7823412 * 100, 1)
    assert m["cpu_used_pct"] == 21.0


def test_read_metrics_tolerates_partial_failure(monkeypatch):
    async def fake_shell(serial, cmd, timeout=30):
        if "dumpsys battery" in cmd:
            return 0, "  level: 50\n"
        return 1, "not found"
    monkeypatch.setattr(adb, "shell", fake_shell)
    m = asyncio.run(adb.read_metrics("S"))
    assert m == {"battery_pct": 50}


def test_read_metrics_empty_on_total_failure(monkeypatch):
    async def fake_shell(serial, cmd, timeout=30):
        return 1, ""
    monkeypatch.setattr(adb, "shell", fake_shell)
    assert asyncio.run(adb.read_metrics("S")) == {}


# ---------- 报告中的指标 ----------

def test_summarize_metrics_empty():
    assert reports._summarize_metrics([]) == {"samples": 0, "series": []}


def test_summarize_metrics_computes_peaks():
    samples = [
        {"t": 0.0, "cpu_used_pct": 10.0, "mem_used_pct": 40.0,
         "battery_pct": 90, "battery_temp_c": 30.0},
        {"t": 2.0, "cpu_used_pct": 60.0, "mem_used_pct": 55.0,
         "battery_pct": 88, "battery_temp_c": 33.5},
        {"t": 4.0, "cpu_used_pct": 25.0, "mem_used_pct": 45.0,
         "battery_pct": 86, "battery_temp_c": 32.0},
    ]
    m = reports._summarize_metrics(samples)
    assert m["samples"] == 3
    assert m["cpu_max"] == 60.0
    assert m["cpu_avg"] == 31.7
    assert m["mem_max"] == 55.0
    assert m["battery_start"] == 90
    assert m["battery_end"] == 86
    assert m["battery_drop"] == 4
    assert m["temp_max"] == 33.5


def test_report_includes_metrics_section():
    rep = reports.build_report("abc", {
        "run_id": "abc", "status": "finished", "repeat": 1, "started_at": 0,
        "scenario": "性能", "steps": [],
        "devices": {"DEV01": {
            "status": "done", "done": 1, "current": 1, "repeat": 1,
            "results": [{"index": 1, "type": "wait", "elapsed": 2.0}],
            "metrics": [{"t": 0.0, "cpu_used_pct": 42.0, "mem_used_pct": 51.0,
                         "battery_pct": 80, "battery_temp_c": 31.0}],
        }},
    }, scenario_name="性能")
    html = reports.render_html(rep)
    assert "性能指标" in html
    assert "DEV01" in html
    assert "42.0" in html


def test_report_metrics_endpoint_contract():
    rep = reports.build_report("abc", {
        "run_id": "abc", "status": "finished", "repeat": 1, "started_at": 0,
        "scenario": "x", "steps": [],
        "devices": {"D": {"status": "done", "done": 0, "current": 0, "repeat": 1,
                          "results": [], "metrics": []}},
    })
    assert rep["devices"]["D"]["metrics"]["samples"] == 0


# ---------- 采样器生命周期 ----------

def test_metrics_sampler_collects_then_stops(monkeypatch):
    import time as _t
    from narci import scenario

    async def fake_metrics(serial):
        return {"cpu_used_pct": 12.0, "mem_used_pct": 44.0,
                "battery_pct": 88, "battery_temp_c": 30.0}

    async def fake_size(serial):
        return (1080, 2400)

    monkeypatch.setattr(adb, "read_metrics", fake_metrics)
    monkeypatch.setattr(scenario.adb, "get_screen_size", fake_size)
    monkeypatch.setattr(reports, "save_report", lambda r: None)

    async def main():
        rid = await scenario.start_run(
            [{"type": "wait", "ms": 200}, {"type": "wait", "ms": 200}],
            1, ["DEV01"], scenario_name="采样", collect_metrics=True, metrics_interval=0.05)
        await asyncio.sleep(1.0)
        run = scenario.get_run(rid)
        return run

    run = asyncio.run(main())
    assert run is not None
    metrics = run["devices"]["DEV01"]["metrics"]
    assert metrics, "应采集到性能指标"
    assert run["status"] == "finished"
    # 采样器已停止：再等一会，采样数不应显著增长
    before = len(metrics)
    import time as _t
    _t.sleep(0.4)
    assert len(metrics) <= before + 1
