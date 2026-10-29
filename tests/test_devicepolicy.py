"""设备托管（Device Owner）解析与下发 单元测试。"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import narci.main as nm
from narci import adb, devicepolicy


@pytest.fixture()
def api():
    with TestClient(nm.app) as tc:
        r = tc.post("/api/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 200, f"测试登录失败: {r.text}"
        headers = {"Authorization": "Bearer " + r.json()["token"]}
        yield tc, headers


SAMPLE_DUMPSYS = """
Device Policy Manager state:
  Device Owner:
    com.example.dpc
      mUserManager: 1 user
      mDeviceOwner: com.example.dpc/.MyDeviceAdminReceiver
  Admins:
    Admin{com.example.dpc}
  Lock Task Packages: [com.example.app, com.other.app]
  mCameraDisabled: true
  mFactoryResetAllowed: false
  mScreenCaptureDisabled: true
"""


def _fake_shell(text):
    async def fake(serial, cmd, timeout=30):
        return 0, text
    return fake


def test_status_finds_owner(monkeypatch):
    monkeypatch.setattr(adb, "shell", _fake_shell(SAMPLE_DUMPSYS))
    st = asyncio.run(devicepolicy.policy_status("S"))
    assert st["has_owner"] is True
    assert st["owner_package"] == "com.example.dpc"
    assert "com.example.dpc" in st["admin_packages"]
    assert st["lock_task_packages"] == ["com.example.app", "com.other.app"]
    assert st["camera_disabled"] is True
    assert st["factory_reset_allowed"] is False
    assert st["screen_capture_disabled"] is True
    assert st["parsed"] is True


def test_status_without_owner(monkeypatch):
    text = "Device Policy Manager state:\n  No Device Owner\n"
    monkeypatch.setattr(adb, "shell", _fake_shell(text))
    st = asyncio.run(devicepolicy.policy_status("S"))
    assert st["has_owner"] is False
    assert st["owner_package"] is None


def test_status_unknown_flags_are_none(monkeypatch):
    text = "Device Policy Manager state:\n  Device Owner:\n    com.x.dpc\n"
    monkeypatch.setattr(adb, "shell", _fake_shell(text))
    st = asyncio.run(devicepolicy.policy_status("S"))
    assert st["has_owner"] is True
    assert st["camera_disabled"] is None
    assert st["factory_reset_allowed"] is None


def test_status_tolerates_empty_output(monkeypatch):
    async def fake(serial, cmd, timeout=30):
        return 1, "dumpsys: couldn't get service"

    monkeypatch.setattr(adb, "shell", fake)
    st = asyncio.run(devicepolicy.policy_status("S"))
    assert st["has_owner"] is False
    assert st["parsed"] is False


def test_set_device_owner_validates_component(monkeypatch):
    calls = []

    async def fake_ok(args, timeout=20, device=None):
        calls.append(" ".join(args))
        return b""

    monkeypatch.setattr(adb, "_run_ok", fake_ok)
    asyncio.run(devicepolicy.set_device_owner("S", "com.example.dpc/.MyReceiver"))
    assert any("dpm set-device-owner" in c for c in calls)

    with pytest.raises(adb.AdbError):
        asyncio.run(devicepolicy.set_device_owner("S", "no-slash-here"))


def test_remove_device_owner(monkeypatch):
    calls = []

    async def fake_ok(args, timeout=20, device=None):
        calls.append(" ".join(args))
        return b""

    monkeypatch.setattr(adb, "_run_ok", fake_ok)
    asyncio.run(devicepolicy.remove_device_owner("S", "com.example.dpc/.MyReceiver"))
    assert any("dpm remove-active-admin" in c for c in calls)


def test_policy_command_requires_owner(monkeypatch):
    text = "Device Policy Manager state:\n  No Device Owner\n"
    monkeypatch.setattr(adb, "shell", _fake_shell(text))
    with pytest.raises(adb.AdbError):
        asyncio.run(devicepolicy.send_policy_command("S", "lock_now"))


def test_policy_command_broadcasts(monkeypatch):
    seen = []

    async def fake_shell(serial, cmd, timeout=30):
        return 0, SAMPLE_DUMPSYS

    async def fake_ok(args, timeout=20, device=None):
        seen.append(" ".join(args))
        return b""

    monkeypatch.setattr(adb, "shell", fake_shell)
    monkeypatch.setattr(adb, "_run_ok", fake_ok)
    r = asyncio.run(devicepolicy.send_policy_command(
        "S", "set_kiosk", {"package": "com.example.app", "on": True}))
    assert r["ok"] is True
    assert r["owner"] == "com.example.dpc"
    assert any("am broadcast" in c and "io.narcifarm.POLICY" in c for c in seen)
    assert any("--es command set_kiosk" in c for c in seen)


def test_policy_command_rejects_unknown(monkeypatch):
    monkeypatch.setattr(adb, "shell", _fake_shell(SAMPLE_DUMPSYS))
    with pytest.raises(adb.AdbError):
        asyncio.run(devicepolicy.send_policy_command("S", "hack_everything"))


def test_policy_commands_catalog():
    assert "lock_now" in devicepolicy.POLICY_COMMANDS
    assert "wipe" in devicepolicy.POLICY_COMMANDS
    assert "set_kiosk" in devicepolicy.POLICY_COMMANDS
    assert devicepolicy.POLICY_ACTION == "io.narcifarm.POLICY"


# ---------- 接口层 ----------

def test_policy_status_endpoint(api, monkeypatch):
    async def fake_status(serial):
        return {"serial": serial, "has_owner": True, "owner_package": "com.x.dpc",
                "admin_packages": ["com.x.dpc"], "lock_task_packages": None,
                "camera_disabled": False, "factory_reset_allowed": True,
                "screen_capture_disabled": None, "parsed": True}

    monkeypatch.setattr(devicepolicy, "policy_status", fake_status)
    tc, headers = api
    r = tc.get("/api/devices/DEV01/policy", headers=headers)
    assert r.status_code == 200
    assert r.json()["owner_package"] == "com.x.dpc"


def test_policy_commands_endpoint(api):
    tc, headers = api
    r = tc.get("/api/policy/commands", headers=headers)
    assert r.status_code == 200
    assert "lock_now" in r.json()["commands"]
    assert r.json()["action"] == "io.narcifarm.POLICY"


def test_policy_provision_endpoint(api, monkeypatch):
    seen = {}

    async def fake_set(serial, component):
        seen.update(serial=serial, component=component)
        return {"ok": True}

    monkeypatch.setattr(devicepolicy, "set_device_owner", fake_set)
    tc, headers = api
    r = tc.post("/api/devices/DEV01/policy/provision",
                json={"component": "com.x.dpc/.MyReceiver"}, headers=headers)
    assert r.status_code == 200
    assert seen["component"] == "com.x.dpc/.MyReceiver"


def test_policy_provision_requires_component(api):
    tc, headers = api
    r = tc.post("/api/devices/DEV01/policy/provision", json={}, headers=headers)
    assert r.status_code == 400


def test_policy_command_endpoint(api, monkeypatch):
    seen = {}

    async def fake_cmd(serial, command, params):
        seen.update(serial=serial, command=command, params=params)
        return {"ok": True, "command": command, "owner": "com.x.dpc"}

    monkeypatch.setattr(devicepolicy, "send_policy_command", fake_cmd)
    tc, headers = api
    r = tc.post("/api/devices/DEV01/policy/command",
                json={"command": "lock_now"}, headers=headers)
    assert r.status_code == 200
    assert seen["command"] == "lock_now"


def test_policy_command_surfaces_adberror(api, monkeypatch):
    async def fake_cmd(serial, command, params):
        raise adb.AdbError("设备未设置 Device Owner")

    monkeypatch.setattr(devicepolicy, "send_policy_command", fake_cmd)
    tc, headers = api
    r = tc.post("/api/devices/DEV01/policy/command",
                json={"command": "lock_now"}, headers=headers)
    assert r.status_code == 400
    assert "Device Owner" in r.json()["detail"]


# ---------- Wi-Fi / VPN / 证书 ----------

def test_policy_commands_include_network():
    for c in ("set_wifi", "set_always_on_vpn", "install_ca_cert", "clear_ca_certs"):
        assert c in devicepolicy.POLICY_COMMANDS


def test_push_ca_cert_uses_adb_push(monkeypatch):
    seen = {}

    async def fake_push(serial, local, remote):
        seen.update(serial=serial, local=local, remote=remote)

    monkeypatch.setattr(adb, "push", fake_push)
    out = asyncio.run(devicepolicy.push_ca_cert("S", "/tmp/ca.crt"))
    assert out.endswith("/tmp/narci_ca.crt") or out == "/data/local/tmp/narci_ca.crt"
    assert seen["serial"] == "S"


def test_ca_cert_broadcast_sends_path(monkeypatch):
    seen = []

    async def fake_shell(serial, cmd, timeout=30):
        return 0, SAMPLE_DUMPSYS

    async def fake_ok(args, timeout=20, device=None):
        seen.append(" ".join(args))
        return b""

    monkeypatch.setattr(adb, "shell", fake_shell)
    monkeypatch.setattr(adb, "_run_ok", fake_ok)
    asyncio.run(devicepolicy.send_policy_command(
        "S", "install_ca_cert", {"path": "/data/local/tmp/ca.crt", "alias": "ca.crt"}))
    assert any("--es command install_ca_cert" in c for c in seen)
    assert any("path" in c for c in seen)


def test_ca_cert_endpoint_rejects_bad_extension(api, tmp_path):
    import io
    tc, headers = api
    bad = tc.post("/api/devices/DEV01/ca-cert",
                  files={"file": ("cert.exe", io.BytesIO(b"x"), "application/octet-stream")},
                  headers=headers)
    assert bad.status_code == 400


def test_ca_cert_endpoint_rejects_empty(api):
    import io
    tc, headers = api
    empty = tc.post("/api/devices/DEV01/ca-cert",
                    files={"file": ("ca.crt", io.BytesIO(b""), "application/x-x509-ca-cert")},
                    headers=headers)
    assert empty.status_code == 400


def test_ca_cert_endpoint_installs(api, tmp_path, monkeypatch):
    import io
    monkeypatch.setenv("NARCI_UPLOADS_DIR", str(tmp_path))
    pushed = {}

    async def fake_push(serial, local, remote):
        pushed["remote"] = remote
        Path(local).write_bytes(b"CERTDATA")

    async def fake_cmd(serial, command, params):
        return {"ok": True, "command": command, "owner": "com.x.dpc"}

    from pathlib import Path
    monkeypatch.setattr(devicepolicy, "push_ca_cert", fake_push)
    monkeypatch.setattr(devicepolicy, "send_policy_command", fake_cmd)
    tc, headers = api
    r = tc.post("/api/devices/DEV01/ca-cert",
files={"file": ("ca.crt", io.BytesIO(b"-----BEGIN CERTIFICATE-----AA-----END CERTIFICATE-----"),
                        "application/x-x509-ca-cert")},
                headers=headers)
    assert r.status_code == 200
    assert r.json()["path"] == pushed["remote"]
