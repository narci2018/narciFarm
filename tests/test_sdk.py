"""narciFarm Python SDK 集成测试（起真实服务，走真实 HTTP）。"""
import os
import socket
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))

from narcifarm import Client, NarciError  # noqa: E402

import narci.main as nm  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """在临时数据目录上起一个真实服务，供 SDK 测试使用。"""
    tmp = tmp_path_factory.mktemp("sdk")
    os.environ["NARCI_DATA_DIR"] = str(tmp / "data")
    os.environ["NARCI_REPORTS_DIR"] = str(tmp / "reports")
    os.environ["NARCI_TEMPLATES_DIR"] = str(tmp / "templates")
    os.environ["NARCI_TEST_MODE"] = "1"

    import uvicorn
    port = _free_port()
    config = uvicorn.Config(nm.app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            import urllib.request
            urllib.request.urlopen(base + "/api/version", timeout=2).read()
            break
        except Exception:
            time.sleep(0.3)
    else:
        pytest.skip("测试服务未能启动")
    yield base
    srv.should_exit = True
    thread.join(timeout=5)


def test_login_and_devices(server):
    c = Client(server)
    user = c.login("admin", "admin")
    assert user["username"] == "admin"
    assert isinstance(c.devices(), list)
    c.logout()


def test_context_manager_logs_out(server):
    with Client(server) as c:
        c.login("admin", "admin")
        assert c.token
    assert c.token is None


def test_error_on_bad_credentials(server):
    c = Client(server)
    with pytest.raises(NarciError):
        c.login("admin", "wrong-password")


def test_error_on_unreachable_server():
    c = Client("http://127.0.0.1:1", timeout=1.0)
    with pytest.raises(NarciError):
        c.devices()


def test_scenario_crud_and_run(server, monkeypatch):
    c = Client(server)
    c.login("admin", "admin")
    sc = c.create_scenario("sdk 冒烟", [{"type": "wait", "ms": 1}], repeat_count=1)
    assert sc["id"]
    names = [s["name"] for s in c.scenarios()]
    assert "sdk 冒烟" in names

    # 无在线设备时运行应返回 400 -> NarciError
    with pytest.raises(NarciError):
        c.run_scenario([{"type": "wait", "ms": 1}], targets=["NOPE"],
                       scenario_id=sc["id"])


def test_plugins_endpoint(server):
    c = Client(server)
    c.login("admin", "admin")
    body = c.plugins()
    assert "tap" in body["builtin"]
    assert "plugins" in body
    assert body["plugin_dir"]


def test_reports_list_empty(server):
    c = Client(server)
    c.login("admin", "admin")
    assert isinstance(c.reports(), list)


def test_hierarchy_and_elements_on_unknown_device(server):
    c = Client(server)
    c.login("admin", "admin")
    with pytest.raises(NarciError):
        c.hierarchy("NOPE")


def test_metrics_on_unknown_device(server):
    c = Client(server)
    c.login("admin", "admin")
    with pytest.raises(NarciError):
        c.metrics("NOPE")


def test_mirror_url_shape(server):
    c = Client("http://127.0.0.1:8555")
    assert c.mirror_url("ABC") == "ws://127.0.0.1:8555/ws/mirror/ABC"


def test_input_requires_targets(server):
    c = Client(server)
    c.login("admin", "admin")
    with pytest.raises(NarciError):
        c.input("tap", {"x_pct": 0.5, "y_pct": 0.5}, [])
