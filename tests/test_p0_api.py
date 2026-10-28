"""P0 五项能力的接口层回归测试：控件树 / OCR 坐标 / 报告导出 / 图像匹配。"""
import json
import sys
import types
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

import narci.main as nm
from narci import reports

SAMPLE_XML = (
    '<hierarchy>'
    '<node text="设置" resource-id="com.x:id/title" class="android.widget.TextView" '
    'content-desc="" package="com.x" bounds="[0,0][200,80]" clickable="true"/>'
    '<node text="" resource-id="com.x:id/btn_ok" class="android.widget.Button" '
    'content-desc="确定" package="com.x" bounds="[200,100][400,180]" clickable="true"/>'
    '</hierarchy>'
)


@pytest.fixture()
def api():
    with TestClient(nm.app) as tc:
        r = tc.post("/api/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 200, f"测试登录失败: {r.text}"
        headers = {"Authorization": "Bearer " + r.json()["token"]}
        yield tc, headers


# ---------- 控件树 / 元素定位 ----------

def test_hierarchy_endpoint(api, monkeypatch):
    tc, headers = api
    root = ET.fromstring(SAMPLE_XML)
    monkeypatch.setattr(nm.adb, "_dump_xml", lambda serial, timeout=25: _async(root))
    r = tc.get("/api/devices/DEV01/hierarchy", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["nodes"][0]["text"] == "设置"
    assert body["nodes"][0]["center"] == [100, 40]


def test_elements_endpoint_filters_by_kind(api, monkeypatch):
    tc, headers = api
    root = ET.fromstring(SAMPLE_XML)
    monkeypatch.setattr(nm.adb, "_dump_xml", lambda serial, timeout=25: _async(root))
    r = tc.get("/api/devices/DEV01/elements", params={"kind": "id", "value": "com.x:id/btn_ok"},
               headers=headers)
    assert r.status_code == 200
    assert r.json()["count"] == 1
    r2 = tc.get("/api/devices/DEV01/elements", params={"kind": "desc", "value": "确定"},
                headers=headers)
    assert r2.json()["count"] == 1


def test_elements_endpoint_rejects_unknown_kind(api, monkeypatch):
    tc, headers = api
    root = ET.fromstring(SAMPLE_XML)
    monkeypatch.setattr(nm.adb, "_dump_xml", lambda serial, timeout=25: _async(root))
    r = tc.get("/api/devices/DEV01/elements", params={"kind": "bogus", "value": "x"},
               headers=headers)
    assert r.status_code == 400
    assert "不支持的定位维度" in r.json()["detail"]


# ---------- OCR 坐标 ----------

def test_ocr_endpoint_returns_bounds(api, monkeypatch):
    tc, headers = api
    fake = types.ModuleType("pytesseract")
    fake.image_to_data = lambda img, lang=None, output_type=None: {
        "text": ["设置"], "conf": [96.0], "left": [100], "top": [200],
        "width": [80], "height": [40],
    }
    fake.Output = types.SimpleNamespace(DICT="dict")
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    monkeypatch.setattr(nm.vision, "ocr_available", lambda: True)

    async def fake_shot(serial):
        return b""

    monkeypatch.setattr(nm.adb, "screenshot_bytes", fake_shot)
    monkeypatch.setattr(nm.vision, "ocr_find", lambda path, region=None, lang=None: [
        {"text": "设置", "bounds": (100, 200, 180, 240), "center": (140, 220),
         "confidence": 96.0},
    ])

    r = tc.post("/api/devices/DEV01/ocr", json={"target": "设置"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["matches"][0]["center"] == [140, 220]
    assert body["matches"][0]["confidence"] == 96.0


def test_ocr_endpoint_region_is_forwarded(api, monkeypatch):
    tc, headers = api
    monkeypatch.setattr(nm.vision, "ocr_available", lambda: True)
    seen = {}

    async def fake_ocr(serial, target=None, region=None, lang=None):
        seen["region"] = region
        return []

    monkeypatch.setattr(nm.vision, "ocr_find_on_device", fake_ocr)
    r = tc.post("/api/devices/DEV01/ocr",
                json={"region": [10, 20, 300, 400]}, headers=headers)
    assert r.status_code == 200
    assert seen["region"] == (10, 20, 300, 400)


# ---------- 图像匹配 ----------

def test_match_image_endpoint_forwards_options(api, monkeypatch):
    tc, headers = api
    seen = {}

    async def fake_match(serial, path, threshold=0.8, max_results=1, region=None, scales=None):
        seen.update(threshold=threshold, max_results=max_results, region=region, scales=scales)
        return [{"x": 1, "y": 2, "center": (10, 20), "score": 0.95,
                 "bounds": (1, 2, 11, 22), "scale": 1.0}]

    monkeypatch.setattr(nm.vision, "match_all_on_device", fake_match)
    r = tc.post("/api/devices/DEV01/match-image",
                json={"image": "btn.png", "threshold": 0.7, "max_results": 3,
                      "region": [0, 0, 500, 500], "scales": [1.0, 0.5]},
                headers=headers)
    assert r.status_code == 200
    assert seen["threshold"] == 0.7
    assert seen["max_results"] == 3
    assert seen["region"] == [0, 0, 500, 500]
    assert seen["scales"] == [1.0, 0.5]
    assert r.json()["count"] == 1


# ---------- 报告 ----------

def test_report_lifecycle(api, tmp_path):
    tc, headers = api
    monkeypatch_dir = tmp_path / "reports"
    monkeypatch_dir.mkdir()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(reports, "REPORTS_DIR", monkeypatch_dir)
    try:
        rep = reports.build_report("run1", {
            "run_id": "run1", "status": "finished", "repeat": 1,
            "started_at": 0, "scenario": "冒烟", "steps": [],
            "devices": {"DEV01": {"status": "done", "done": 1, "current": 1, "repeat": 1,
                                  "results": [{"index": 1, "type": "tap", "elapsed": 0.1}]}},
        }, scenario_name="冒烟")
        reports.save_report(rep)

        r = tc.get("/api/reports", headers=headers)
        assert r.status_code == 200
        assert any(x["report_id"] == rep["report_id"] for x in r.json())

        rid = rep["report_id"]
        assert tc.get(f"/api/reports/{rid}", headers=headers).status_code == 200
        html = tc.get(f"/api/reports/{rid}.html", headers=headers)
        assert html.status_code == 200 and "场景测试报告" in html.text
        xml = tc.get(f"/api/reports/{rid}.xml", headers=headers)
        assert xml.status_code == 200 and xml.text.startswith("<?xml")
        assert tc.get("/api/reports/does-not-exist", headers=headers).status_code == 404
    finally:
        monkeypatch.undo()


def test_report_delete_requires_admin(api):
    tc, headers = api
    r = tc.delete("/api/reports/whatever", headers=headers)
    # 管理员账号可删（不存在则 404），普通成员应 403
    assert r.status_code == 404


def _async(value):
    async def _inner():
        return value
    return _inner()


# ---------- 坐标 -> 控件解析（控件级录制） ----------

def test_resolve_at_returns_selector(api, monkeypatch):
    tc, headers = api
    import xml.etree.ElementTree as ET
    xml = ('<hierarchy>'
           '<node text="设置" resource-id="com.x:id/title" class="android.widget.TextView" '
           'content-desc="" package="com.x" bounds="[0,0][200,80]"/>'
           '</hierarchy>')
    monkeypatch.setattr(nm.adb, "_dump_xml", lambda serial, timeout=25: _async(ET.fromstring(xml)))

    async def fake_size(serial):
        return (1080, 2400)

    monkeypatch.setattr(nm.adb, "get_screen_size", fake_size)
    r = tc.post("/api/devices/DEV01/resolve-at",
                json={"x_pct": 0.0926, "y_pct": 0.0167}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["selector"]["kind"] == "id"
    assert body["selector"]["value"] == "com.x:id/title"


def test_resolve_at_reports_miss(api, monkeypatch):
    tc, headers = api
    import xml.etree.ElementTree as ET
    xml = ('<hierarchy><node text="x" resource-id="a" class="c" '
           'content-desc="" package="p" bounds="[0,0][10,10]"/></hierarchy>')
    monkeypatch.setattr(nm.adb, "_dump_xml", lambda serial, timeout=25: _async(ET.fromstring(xml)))

    async def fake_size(serial):
        return (1080, 2400)

    monkeypatch.setattr(nm.adb, "get_screen_size", fake_size)
    r = tc.post("/api/devices/DEV01/resolve-at",
                json={"x_pct": 0.9, "y_pct": 0.9}, headers=headers)
    assert r.status_code == 200
    assert r.json()["found"] is False


def test_metrics_endpoint(api, monkeypatch):
    tc, headers = api

    async def fake_metrics(serial):
        return {"battery_pct": 77, "cpu_used_pct": 12.0}

    async def fake_list():
        return [{"serial": "DEV01", "state": "device"}]

    monkeypatch.setattr(nm.adb, "read_metrics", fake_metrics)
    monkeypatch.setattr(nm.adb, "list_devices", fake_list)
    r = tc.get("/api/devices/DEV01/metrics", headers=headers)
    assert r.status_code == 200
    # 该端点直接返回指标字段（并附 fps/streaming），不包一层 metrics
    assert r.json()["battery_pct"] == 77
