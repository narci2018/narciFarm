"""Agent 密钥认证测试: 无/错误密钥拒绝, 正确密钥放行。"""
import os

os.environ["NARCI_AGENT_SECRET"] = "testsecret"
os.environ["NARCI_TEST_MODE"] = "1"

from fastapi.testclient import TestClient

from narci.agent_app import create_agent_app


client = TestClient(create_agent_app())


def test_command_requires_secret():
    r = client.post("/agent/command", json={"cmd": "shell", "params": {"serial": "X", "cmd": "echo hi"}})
    assert r.status_code == 401


def test_command_rejects_wrong_secret():
    r = client.post("/agent/command", json={"cmd": "shell", "params": {"serial": "X", "cmd": "echo hi"}},
                    headers={"X-Narci-Secret": "wrong"})
    assert r.status_code == 401


def test_command_accepts_correct_secret(monkeypatch):
    import narci.agent_app as aa
    monkeypatch.setattr(aa, "_dispatch_command",
                        lambda cmd, params: {"ok": True, "rc": 0, "out": "ok"})
    r = client.post("/agent/command", json={"cmd": "shell", "params": {"serial": "X", "cmd": "echo hi"}},
                    headers={"X-Narci-Secret": "testsecret"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
