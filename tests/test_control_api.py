"""统一注入入口 /api/input 回归测试（v0.3.0 重构后 PC 操作手机的唯一链路）。"""
import pytest
from fastapi.testclient import TestClient

import narci.main as nm


@pytest.fixture()
def api(monkeypatch):
    called = []

    async def fake_input_action(serial, action, params):
        called.append((serial, action, params))
        if serial == "BROKEN01":
            raise nm.adb.AdbError("设备拒绝了输入注入（厂商安全限制）")

    monkeypatch.setattr(nm, "_input_action", fake_input_action)
    with TestClient(nm.app) as tc:
        yield tc, called


def test_input_multi_success(api):
    tc, called = api
    r = tc.post("/api/input", json={"serials": ["DEV01", "DEV02"],
                                    "action": "tap", "params": {"x_pct": 0.5, "y_pct": 0.5}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body["results"]) == {"DEV01", "DEV02"}
    assert ("DEV01", "tap", {"x_pct": 0.5, "y_pct": 0.5}) in called
    assert ("DEV02", "tap", {"x_pct": 0.5, "y_pct": 0.5}) in called


def test_input_multi_partial_failure(api):
    tc, called = api
    r = tc.post("/api/input", json={"serials": ["DEV01", "BROKEN01"],
                                    "action": "key", "params": {"keycode": "home"}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["results"]["DEV01"]["ok"] is True
    assert "厂商安全限制" in body["results"]["BROKEN01"]["error"]


def test_input_multi_rejects_unknown_action(api):
    tc, _ = api
    r = tc.post("/api/input", json={"serials": ["DEV01"], "action": "hack", "params": {}})
    assert r.status_code == 400


def test_input_multi_requires_targets(api):
    tc, called = api
    r = tc.post("/api/input", json={"serials": [], "action": "tap", "params": {}})
    assert r.status_code == 400
    assert called == []


def test_ws_rejects_injection_actions(api):
    """重构后 WS 不再承载注入: tap 必须收到引导性错误帧而非静默丢弃。"""
    class FakeClient:
        serial = "FAKE001"
        control_enabled = True
        running = True
        width = 1080
        height = 2400
        device_name = "t"

        def fps(self):
            return 0.0

    class FakeHub:
        async def acquire(self, serial, **kwargs):
            return FakeClient()

        def subscribe(self, serial):
            import asyncio
            return asyncio.Queue()

        def unsubscribe(self, serial, q):
            pass

        def refs(self, serial):
            return 1

        async def release(self, serial):
            pass

        async def stop_all(self):
            pass

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(nm, "hub", FakeHub())
    try:
        with TestClient(nm.app) as tc:
            with tc.websocket_connect("/ws/mirror/FAKE001?control=1") as ws:
                ws.receive_json()  # session
                ws.receive_json()  # device
                ws.send_json({"action": "tap", "nx": 0.5, "ny": 0.5})
                msg = ws.receive_json()
                assert msg["type"] == "error"
                assert "/api/input" in msg["msg"]
    finally:
        monkeypatch.undo()
