"""WS 控制链路回归测试：浏览器指令必须到达注入层。

回归背景: v0.2.0 的 ws_mirror 误判 ASGI 消息类型导致所有触控指令被静默丢弃
（症状: 投屏正常但无法操作手机）。本测试通过假 hub/client 验证指令全链路。
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import narci.main as nm


class FakeClient:
    def __init__(self):
        self.serial = "FAKE001"
        self.control_enabled = True
        self.running = True
        self.width = 1080
        self.height = 2400
        self.device_name = "测试设备"
        self.sent = []

    def fps(self):
        return 0.0

    async def send_control(self, payload):
        self.sent.append(payload)


class FakeHub:
    def __init__(self):
        self.client = FakeClient()

    async def acquire(self, serial, **kwargs):
        return self.client

    def subscribe(self, serial):
        return asyncio.Queue()

    def unsubscribe(self, serial, q):
        pass

    def refs(self, serial):
        return 1

    async def release(self, serial):
        pass

    async def stop_all(self):
        pass


@pytest.fixture()
def client(monkeypatch):
    fake_hub = FakeHub()
    monkeypatch.setattr(nm, "hub", fake_hub)
    with TestClient(nm.app) as tc:
        yield tc, fake_hub


def test_ws_tap_reaches_scrcpy_injection(client):
    tc, fake_hub = client
    with tc.websocket_connect("/ws/mirror/FAKE001?control=1") as ws:
        assert ws.receive_json()["type"] == "session"
        assert ws.receive_json()["type"] == "device"

        ws.send_json({"action": "tap", "nx": 0.5, "ny": 0.5, "targets": ["FAKE001"]})
        ack = ws.receive_json()
        assert ack["type"] == "injected" and ack["action"] == "tap"

        # 主控设备: scrcpy 注入路径, DOWN+UP 各一条 32 字节 TYPE_INJECT_TOUCH
        assert len(fake_hub.client.sent) == 2
        assert all(len(p) == 32 for p in fake_hub.client.sent)
        assert fake_hub.client.sent[0][0] == 2  # TYPE_INJECT_TOUCH_EVENT
        assert fake_hub.client.sent[1][0] == 2


def test_ws_key_and_text_commands(client, monkeypatch):
    tc, fake_hub = client
    called = []

    async def fake_input_action(serial, action, params):
        called.append((serial, action, params))

    monkeypatch.setattr(nm, "_input_action", fake_input_action)
    with tc.websocket_connect("/ws/mirror/FAKE001?control=1") as ws:
        ws.receive_json()  # session
        ws.receive_json()  # device

        ws.send_json({"action": "key", "keycode": "back", "targets": ["FAKE001"]})
        assert ws.receive_json()["action"] == "key"

        ws.send_json({"action": "text", "text": "hello", "targets": ["FAKE001"]})
        assert ws.receive_json()["action"] == "text"
        assert ("FAKE001", "key", {"keycode": "back"}) in called
        assert ("FAKE001", "text", {"text": "hello"}) in called

        # 再次验证 tap 仍可用（scrcpy 注入路径不受 adb 桩影响）
        ws.send_json({"action": "tap", "nx": 0.1, "ny": 0.2, "targets": ["FAKE001"]})
        assert ws.receive_json()["action"] == "tap"
        assert len(fake_hub.client.sent) == 2


def test_ws_broadcast_targets_use_adb_path_for_others(client, monkeypatch):
    """广播模式下: 主控走 scrcpy 注入, 其余目标走 adb(此处仅验证不抛异常且回执到达)。"""
    tc, fake_hub = client
    called = []

    async def fake_input_action(serial, action, params):
        called.append((serial, action))

    monkeypatch.setattr(nm, "_input_action", fake_input_action)
    with tc.websocket_connect("/ws/mirror/FAKE001?control=1") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"action": "tap", "nx": 0.3, "ny": 0.4,
                      "targets": ["FAKE001", "OTHER01"]})
        ack = ws.receive_json()
        assert ack["targets"] == ["FAKE001", "OTHER01"]
        assert ("OTHER01", "tap") in called
        assert len(fake_hub.client.sent) == 2  # 仅主控走 scrcpy
