"""步骤级重试 与 测试报告 单元测试。"""
import asyncio
import time

import pytest

from narci import reports, scenario


# ---------- 重试 ----------

def _run_steps(monkeypatch, steps):
    """在离屏环境下执行步骤列表，返回设备状态。"""
    async def fake_size(serial):
        return (1080, 2400)

    monkeypatch.setattr(scenario.adb, "get_screen_size", fake_size)
    run_id = "testrun"
    state = {"status": "running", "done": 0, "current": 0}
    scenario._runs[run_id] = {"run_id": run_id, "devices": {}}
    abort = asyncio.Event()

    async def runner():
        await scenario._run_step_list(steps, "S", (1080, 2400), run_id, abort, state, {})

    asyncio.run(runner())
    scenario._runs.pop(run_id, None)
    return state


def test_retry_recovers_after_transient_failure(monkeypatch):
    calls = []

    async def fake_step(step, serial, size, run_id):
        calls.append(1)
        if len(calls) < 3:
            raise scenario.ScenarioError("暂时失败")
        return {"ok": True}

    monkeypatch.setattr(scenario, "_execute_step", fake_step)
    state = _run_steps(monkeypatch, [{"type": "tap", "x_pct": 0.5, "y_pct": 0.5,
                                      "retry": 3, "retry_interval_ms": 0}])
    assert len(calls) == 3
    assert state["status"] == "running"
    assert state["done"] == 1


def test_retry_exhaustion_raises(monkeypatch):
    async def fake_step(step, serial, size, run_id):
        raise scenario.ScenarioError("一直失败")

    monkeypatch.setattr(scenario, "_execute_step", fake_step)
    with pytest.raises(scenario.ScenarioError):
        _run_steps(monkeypatch, [{"type": "tap", "x_pct": 0.5, "y_pct": 0.5,
                                  "retry": 2, "retry_interval_ms": 0}])


def test_on_fail_continue_records_skip(monkeypatch):
    async def fake_step(step, serial, size, run_id):
        if step["type"] == "tap":
            raise scenario.ScenarioError("跳过我")
        return {}

    monkeypatch.setattr(scenario, "_execute_step", fake_step)
    state = _run_steps(monkeypatch, [
        {"type": "tap", "x_pct": 0.5, "y_pct": 0.5, "retry": 0, "on_fail": "continue"},
        {"type": "wait", "ms": 0},
    ])
    assert state["done"] == 2
    assert state["results"][0]["skipped"] is True
    assert "跳过我" in state["results"][0]["error"]


def test_on_fail_abort_is_default(monkeypatch):
    async def fake_step(step, serial, size, run_id):
        raise scenario.ScenarioError("中止")

    monkeypatch.setattr(scenario, "_execute_step", fake_step)
    with pytest.raises(scenario.ScenarioError):
        _run_steps(monkeypatch, [{"type": "tap", "x_pct": 0.5, "y_pct": 0.5,
                                  "retry": 0}])


def test_retry_does_not_trigger_without_config(monkeypatch):
    calls = []

    async def fake_step(step, serial, size, run_id):
        calls.append(1)
        raise scenario.ScenarioError("失败")

    monkeypatch.setattr(scenario, "_execute_step", fake_step)
    with pytest.raises(scenario.ScenarioError):
        _run_steps(monkeypatch, [{"type": "tap", "x_pct": 0.5, "y_pct": 0.5}])
    assert len(calls) == 1


# ---------- 报告 ----------

def _sample_run():
    return {
        "run_id": "abc123",
        "status": "finished",
        "repeat": 1,
        "started_at": time.time() - 5,
        "scenario": "冒烟测试",
        "steps": [{"type": "tap", "x_pct": 0.5, "y_pct": 0.5}],
        "devices": {
            "DEV01": {"status": "done", "done": 2, "current": 2, "repeat": 1,
                      "results": [{"index": 1, "type": "tap", "x": 540, "y": 1200, "elapsed": 0.1}]},
            "DEV02": {"status": "error", "done": 1, "current": 1, "repeat": 1,
                      "error": "未找到文本节点: 设置", "failure_shot": "/tmp/x.png",
                      "results": [{"index": 1, "type": "tap_text", "skipped": True,
                                   "error": "未找到文本节点: 设置", "elapsed": 2.3}]},
        },
    }


def test_build_report_summary_and_status():
    rep = reports.build_report("abc123", _sample_run(), scenario_name="冒烟测试")
    assert rep["status"] == "failed"
    assert rep["scenario"] == "冒烟测试"
    assert rep["summary"]["devices"] == 2
    assert rep["summary"]["steps_total"] == 3
    assert rep["summary"]["steps_failed"] == 1
    assert rep["devices"]["DEV01"]["passed"] == 1
    assert rep["devices"]["DEV02"]["failed"] == 1


def test_report_failed_when_step_skipped():
    """on_fail=continue 跳过步骤也应让报告判失败，否则 CI 拦不住。"""
    rep = reports.build_report("abc", {
        "run_id": "abc", "status": "finished", "repeat": 1, "started_at": 0,
        "scenario": "跳过", "steps": [],
        "devices": {"DEV01": {"status": "done", "done": 1, "current": 1, "repeat": 1,
                              "results": [{"index": 1, "type": "tap", "skipped": True,
                                           "error": "设备拒绝", "elapsed": 0.1}]}},
    }, scenario_name="跳过")
    assert rep["status"] == "failed"
    assert rep["summary"]["steps_failed"] == 1


def test_build_report_handles_missing_run():
    rep = reports.build_report("none", None, scenario_name="x")
    assert rep["status"] == "unknown"
    assert rep["steps"] == []


def test_report_roundtrip_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path)
    rep = reports.build_report("abc", _sample_run(), scenario_name="冒烟测试")
    path = reports.save_report(rep)
    assert path.exists()

    loaded = reports.load_report(rep["report_id"])
    assert loaded["report_id"] == rep["report_id"]
    assert loaded["summary"]["devices"] == 2

    listed = reports.list_reports()
    assert any(r["report_id"] == rep["report_id"] for r in listed)

    assert reports.delete_report(rep["report_id"]) is True
    assert reports.load_report(rep["report_id"]) is None


def test_render_junit_contains_devices_and_failures():
    rep = reports.build_report("abc", _sample_run(), scenario_name="冒烟测试")
    xml = reports.render_junit(rep)
    assert xml.startswith("<?xml version")
    assert 'tests="2"' in xml
    assert 'failures="1"' in xml
    assert 'name="DEV01"' in xml and 'name="DEV02"' in xml
    assert "<failure" in xml and "未找到文本节点" in xml


def test_render_html_is_self_contained():
    rep = reports.build_report("abc", _sample_run(), scenario_name="冒烟测试")
    html = reports.render_html(rep)
    assert "<style>" in html
    assert "narciFarm 场景测试报告" in html
    assert "DEV01" in html and "DEV02" in html
    assert "冒烟测试" in html
    assert "取证截图" in html
    # 无外部资源依赖
    assert "http://" not in html and "https://" not in html


def test_render_html_escapes_error_text():
    run = _sample_run()
    run["devices"]["DEV02"]["error"] = "<script>alert(1)</script>"
    run["devices"]["DEV02"]["results"][0]["error"] = "<script>alert(1)</script>"
    rep = reports.build_report("abc", run, scenario_name="<b>x</b>")
    html = reports.render_html(rep)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>x</b>" not in html
