"""步骤插件机制 单元测试。"""
import asyncio
import types

import pytest

from narci import plugins, scenario


@pytest.fixture()
def reg():
    return plugins.StepRegistry()


def test_registry_step_and_get(reg):
    async def handler(step, serial, size, run_id):
        return {"ok": True}

    reg.step("demo", handler, description="演示", fields=["a"])
    assert reg.get("demo") is handler
    assert "demo" in reg
    assert reg.get("missing") is None
    listed = reg.list()
    assert listed[0]["name"] == "demo"
    assert listed[0]["description"] == "演示"
    assert listed[0]["fields"] == ["a"]
    assert listed[0]["builtin"] is False


def test_registry_rejects_non_callable(reg):
    with pytest.raises(ValueError):
        reg.step("bad", "not-callable")


def test_load_plugins_from_directory(tmp_path, monkeypatch):
    (tmp_path / "good.py").write_text(
        "async def run(step, serial, size, run_id):\n"
        "    return {'v': step.get('v')}\n"
        "def register(reg):\n"
        "    reg.step('good_step', run, fields=['v'])\n",
        encoding="utf-8",
    )
    (tmp_path / "bad.py").write_text("def register(reg):\n    raise RuntimeError('boom')\n", encoding="utf-8")
    (tmp_path / "noreg.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "_skip.py").write_text("def register(reg):\n    reg.step('never', lambda *a: {})\n", encoding="utf-8")

    loaded = plugins.load_plugins(tmp_path)
    assert loaded == ["good"]
    assert plugins.REGISTRY.get("good_step") is not None
    assert plugins.REGISTRY.get("never") is None


def test_load_plugins_missing_dir(tmp_path):
    assert plugins.load_plugins(tmp_path / "nope") == []


def test_plugin_step_executed_by_engine(monkeypatch, tmp_path):
    async def handler(step, serial, size, run_id):
        return {"echo": step.get("text")}

    plugins.REGISTRY.step("echo_step", handler)
    try:
        async def fake_size(serial):
            return (1080, 2400)

        monkeypatch.setattr(scenario.adb, "get_screen_size", fake_size)
        run_id = "t"
        state = {"status": "running", "done": 0, "current": 0}
        scenario._runs[run_id] = {"run_id": run_id, "devices": {}}
        abort = asyncio.Event()

        async def runner():
            await scenario._run_step_list(
                [{"type": "echo_step", "text": "hello"}], "S", (1080, 2400), run_id, abort, state, {})

        asyncio.run(runner())
        scenario._runs.pop(run_id, None)
        assert state["results"][0]["echo"] == "hello"
    finally:
        plugins.REGISTRY._handlers.pop("echo_step", None)
        plugins.REGISTRY._meta.pop("echo_step", None)


def test_validate_steps_accepts_plugin_types(monkeypatch):
    async def handler(step, serial, size, run_id):
        return {}

    plugins.REGISTRY.step("tmp_step", handler)
    try:
        scenario.validate_steps([{"type": "tmp_step"}])
        with pytest.raises(scenario.ScenarioError):
            scenario.validate_steps([{"type": "still_unknown"}])
    finally:
        plugins.REGISTRY._handlers.pop("tmp_step", None)
        plugins.REGISTRY._meta.pop("tmp_step", None)


def test_builtin_steps_not_overridden_by_plugin(reg):
    """插件只能补充，不能覆盖内置步骤类型。"""
    assert "tap" not in reg
    assert scenario.STEP_TYPES


# ---------- 接口层 ----------

def test_plugins_endpoint_lists_builtin_and_plugins():
    from fastapi.testclient import TestClient
    import narci.main as nm

    async def handler(step, serial, size, run_id):
        return {}

    plugins.REGISTRY.step("api_demo_step", handler, description="接口演示")
    try:
        with TestClient(nm.app) as tc:
            r = tc.post("/api/login", json={"username": "admin", "password": "admin"})
            headers = {"Authorization": "Bearer " + r.json()["token"]}
            body = tc.get("/api/plugins", headers=headers).json()
        assert "tap" in body["builtin"]
        assert "tap_el" in body["builtin"]
        names = [p["name"] for p in body["plugins"]]
        assert "api_demo_step" in names
        assert body["plugin_dir"]
    finally:
        plugins.REGISTRY._handlers.pop("api_demo_step", None)
        plugins.REGISTRY._meta.pop("api_demo_step", None)
