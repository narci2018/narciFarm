"""设备快捷操作 与 批量操作接口 测试。"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import narci.main as nm
from narci import adb, scenario


@pytest.fixture()
def api():
    with TestClient(nm.app) as tc:
        r = tc.post("/api/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 200, f"测试登录失败: {r.text}"
        headers = {"Authorization": "Bearer " + r.json()["token"]}
        yield tc, headers


def _fake_run_factory(calls):
    async def fake_run(args, timeout=20, device=None, input_data=None):
        calls.append((device, list(args)))
        return 0, b"Success", b""
    return fake_run


def test_reboot_sends_reboot_command(monkeypatch):
    calls = []
    monkeypatch.setattr(adb, "_run", _fake_run_factory(calls))
    asyncio.run(adb.reboot_device("S"))
    assert ("S", ["reboot"]) in calls


def test_power_off_uses_reboot_p(monkeypatch):
    calls = []
    monkeypatch.setattr(adb, "_run", _fake_run_factory(calls))
    asyncio.run(adb.power_off("S"))
    assert ("S", ["shell", "reboot -p"]) in calls


def test_airplane_mode_toggles_setting(monkeypatch):
    calls = []
    async def fake_ok(args, timeout=20, device=None):
        calls.append(list(args))
        return b""
    monkeypatch.setattr(adb, "_run_ok", fake_ok)
    asyncio.run(adb.set_airplane_mode("S", True))
    asyncio.run(adb.set_airplane_mode("S", False))
    joined = [" ".join(c) for c in calls]
    assert any("airplane_mode_on 1" in c for c in joined)
    assert any("airplane_mode_on 0" in c for c in joined)


def test_kill_process_reports_success(monkeypatch):
    async def fake_ok(args, timeout=20, device=None):
        return b""
    monkeypatch.setattr(adb, "_run_ok", fake_ok)
    assert asyncio.run(adb.kill_process("S", "com.x")) is True

    async def fake_fail(args, timeout=20, device=None):
        raise adb.AdbError("denied")
    monkeypatch.setattr(adb, "_run_ok", fake_fail)
    assert asyncio.run(adb.kill_process("S", "com.x")) is False


def test_clear_app_data_detects_success(monkeypatch):
    async def ok(args, timeout=20, device=None):
        return b"Success"
    monkeypatch.setattr(adb, "_run_ok", ok)
    assert asyncio.run(adb.clear_app_data("S", "com.x")) is True

    async def fail(args, timeout=20, device=None):
        return b"Failed"
    monkeypatch.setattr(adb, "_run_ok", fail)
    assert asyncio.run(adb.clear_app_data("S", "com.x")) is False


def test_locate_wakes_and_maxes_brightness(monkeypatch):
    calls = []
    async def fake_wake(serial):
        calls.append(("wake", serial))
    async def fake_ok(args, timeout=20, device=None):
        calls.append(("cmd", " ".join(args)))
        return b""
    monkeypatch.setattr(adb, "wake_up", fake_wake)
    monkeypatch.setattr(adb, "_run_ok", fake_ok)
    asyncio.run(adb.locate_device("S"))
    assert ("wake", "S") in calls
    joined = " ".join(c[1] for c in calls if c[0] == "cmd")
    assert "screen_brightness 255" in joined


def test_lock_and_unlock(monkeypatch):
    calls = []
    async def fake_ok(args, timeout=20, device=None):
        calls.append(" ".join(args))
        return b""
    async def fake_key(serial, code):
        calls.append(f"key {code}")
    async def fake_wake(serial):
        calls.append("wake")
    monkeypatch.setattr(adb, "_run_ok", fake_ok)
    monkeypatch.setattr(adb, "input_keyevent", fake_key)
    monkeypatch.setattr(adb, "wake_up", fake_wake)
    asyncio.run(adb.lock_device("S"))
    asyncio.run(adb.unlock_device("S"))
    assert "shell input keyevent 26" in calls
    assert "key 82" in calls
    assert "wake" in calls


# ---------- 场景步骤 ----------

def test_quick_action_steps_execute(monkeypatch):
    async def fake_size(serial):
        return (1080, 2400)
    monkeypatch.setattr(scenario.adb, "get_screen_size", fake_size)

    called = []
    async def fake_reboot(serial, wait=False):
        called.append(("reboot", serial, wait))
    async def fake_locate(serial):
        called.append(("locate", serial))
    async def fake_airplane(serial, on):
        called.append(("airplane", serial, on))
    monkeypatch.setattr(scenario.adb, "reboot_device", fake_reboot)
    monkeypatch.setattr(scenario.adb, "locate_device", fake_locate)
    monkeypatch.setattr(scenario.adb, "set_airplane_mode", fake_airplane)

    async def main():
        for step in ({"type": "reboot", "wait": True}, {"type": "locate"},
                     {"type": "airplane", "on": False}):
            await scenario._execute_step(step, "S", (1080, 2400), "r")

    asyncio.run(main())
    assert ("reboot", "S", True) in called
    assert ("locate", "S") in called
    assert ("airplane", "S", False) in called


def test_reboot_wait_is_optional(monkeypatch):
    async def fake_size(serial):
        return (1080, 2400)
    monkeypatch.setattr(scenario.adb, "get_screen_size", fake_size)
    seen = {}
    async def fake_reboot(serial, wait=False):
        seen["wait"] = wait
    monkeypatch.setattr(scenario.adb, "reboot_device", fake_reboot)

    async def main():
        await scenario._execute_step({"type": "reboot"}, "S", (1080, 2400), "r")

    asyncio.run(main())
    assert seen["wait"] is False


# ---------- 批量接口 ----------

def test_batch_actions_endpoint_lists_actions(api):
    tc, headers = api
    body = tc.get("/api/batch/actions", headers=headers).json()
    names = {a["action"] for a in body}
    assert {"reboot", "locate", "airplane_on", "kill_app", "clear_data"} <= names
    kill = next(a for a in body if a["action"] == "kill_app")
    assert kill["needs_param"] is True
    assert kill["param"] == "package"


def test_batch_action_rejects_unknown(api):
    tc, headers = api
    r = tc.post("/api/batch/action", json={"action": "hack", "targets": ["DEV01"]}, headers=headers)
    assert r.status_code == 400


def test_batch_action_requires_online_devices(api):
    tc, headers = api
    r = tc.post("/api/batch/action", json={"action": "locate", "targets": ["OFFLINE"]}, headers=headers)
    assert r.status_code == 400


def test_batch_action_executes_on_online_devices(api, monkeypatch):
    import narci.main as nm

    async def fake_list():
        return [{"serial": "DEV01", "state": "device"}]

    async def fake_locate(serial):
        return None

    monkeypatch.setattr(nm.adb, "list_devices", fake_list)
    monkeypatch.setattr(nm.adb, "locate_device", fake_locate)
    tc, headers = api
    r = tc.post("/api/batch/action", json={"action": "locate", "targets": ["DEV01"]}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["results"]["DEV01"]["ok"] is True


def test_batch_action_reports_per_device_failure(api, monkeypatch):
    import narci.main as nm

    async def fake_list():
        return [{"serial": "DEV01", "state": "device"}, {"serial": "DEV02", "state": "device"}]

    async def fake_locate(serial):
        if serial == "DEV02":
            raise RuntimeError("device busy")

    monkeypatch.setattr(nm.adb, "list_devices", fake_list)
    monkeypatch.setattr(nm.adb, "locate_device", fake_locate)
    tc, headers = api
    r = tc.post("/api/batch/action", json={"action": "locate", "targets": ["DEV01", "DEV02"]},
                headers=headers)
    body = r.json()
    assert body["results"]["DEV01"]["ok"] is True
    assert body["results"]["DEV02"]["ok"] is False
    assert "busy" in body["results"]["DEV02"]["error"]
