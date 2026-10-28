"""统一注入入口 /api/input 回归测试（v0.3.0 重构后 PC 操作手机的唯一链路）。"""
import os
import tempfile

os.environ["NARCI_DATA_DIR"] = tempfile.mkdtemp(prefix="narci_test_")

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
        r = tc.post("/api/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 200, f"测试登录失败: {r.text}"
        headers = {"Authorization": "Bearer " + r.json()["token"]}
        yield tc, called, headers


def test_input_multi_success(api):
    tc, called, headers = api
    r = tc.post("/api/input", json={"serials": ["DEV01", "DEV02"],
                                    "action": "tap", "params": {"x_pct": 0.5, "y_pct": 0.5}}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body["results"]) == {"DEV01", "DEV02"}
    assert ("DEV01", "tap", {"x_pct": 0.5, "y_pct": 0.5}) in called
    assert ("DEV02", "tap", {"x_pct": 0.5, "y_pct": 0.5}) in called


def test_input_multi_partial_failure(api):
    tc, called, headers = api
    r = tc.post("/api/input", json={"serials": ["DEV01", "BROKEN01"],
                                    "action": "key", "params": {"keycode": "home"}}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["results"]["DEV01"]["ok"] is True
    assert "厂商安全限制" in body["results"]["BROKEN01"]["error"]


def test_input_multi_rejects_unknown_action(api):
    tc, _, headers = api
    r = tc.post("/api/input", json={"serials": ["DEV01"], "action": "hack", "params": {}}, headers=headers)
    assert r.status_code == 400


def test_input_multi_surfaces_wake_note(api, monkeypatch):
    """设备熄屏被自动唤醒时，说明必须回给前端（否则用户以为点击没反应）。"""
    tc, _, headers = api

    async def fake_note(serial, action, params):
        return "设备熄屏已自动唤醒；若停在锁屏请手动解锁后重新操作"

    monkeypatch.setattr(nm, "_input_action", fake_note)
    r = tc.post("/api/input", json={"serials": ["DEV01"],
                                    "action": "tap", "params": {"x_pct": 0.5, "y_pct": 0.5}}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "熄屏" in body["results"]["DEV01"]["note"]


def test_input_multi_requires_targets(api):
    headers = api[2]
    tc, called, headers = api
    r = tc.post("/api/input", json={"serials": [], "action": "tap", "params": {}}, headers=headers)
    assert r.status_code == 400
    assert called == []


def test_ws_reports_unexpected_acquire_error():
    """投屏连接的任何异常都必须回错误帧+日志，不能静默断开（旧版零日志无法排障）。"""
    class BoomHub:
        serial = "FAKE001"

        async def acquire(self, serial, **kwargs):
            raise RuntimeError("boom-acquire")

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
    monkeypatch.setattr(nm, "hub", BoomHub())
    try:
        with TestClient(nm.app) as tc:
            lr = tc.post("/api/login", json={"username": "admin", "password": "admin"})
            token = lr.json()["token"]
            with tc.websocket_connect(f"/ws/mirror/FAKE001?control=1&token={token}") as ws:
                msg = ws.receive_json()
                assert msg["type"] == "error"
                assert "boom-acquire" in msg["msg"]
    finally:
        monkeypatch.undo()


def test_ws_rejects_injection_actions(api):
    """重构后 WS 不再承载注入: tap 必须收到引导性错误帧而非静默丢弃。"""
    tc, _, headers = api

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
            with tc.websocket_connect(f"/ws/mirror/FAKE001?control=1&token={headers['Authorization'].split(' ')[1]}") as ws:
                ws.receive_json()  # session
                ws.receive_json()  # device
                ws.send_json({"action": "tap", "nx": 0.5, "ny": 0.5})
                msg = ws.receive_json()
                assert msg["type"] == "error"
                assert "/api/input" in msg["msg"]
    finally:
        monkeypatch.undo()
