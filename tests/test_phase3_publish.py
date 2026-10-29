"""阶段三：视觉验证 / 风控感知 / 发布场景模板 / 分发-场景闭环。"""
import json
import time
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from narci import db, scenario, risk_control
import narci.main as nm


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.connect(tmp_path / "test.db")
    yield


@pytest.fixture()
def client():
    with TestClient(nm.app) as tc:
        r = tc.post("/api/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 200
        tc.headers = {"Authorization": "Bearer " + r.json()["token"]}
        yield tc


# ── 风控模块 ────────────────────────────────────────────

class TestRiskControl:
    def setup_method(self):
        risk_control._risk_state.clear()

    def test_set_and_check_risk(self):
        assert not risk_control.is_at_risk("DEV001")
        risk_control.set_risk("DEV001", "验证码弹窗")
        assert risk_control.is_at_risk("DEV001")
        state = risk_control.get_risk_state("DEV001")
        assert state["risk"] is True
        assert "验证码" in state["reason"]

    def test_clear_risk(self):
        risk_control.set_risk("DEV001", "限流")
        assert risk_control.is_at_risk("DEV001")
        risk_control.clear_risk("DEV001")
        assert not risk_control.is_at_risk("DEV001")

    def test_get_all_states(self):
        risk_control.set_risk("DEV001", "验证码")
        risk_control.set_risk("DEV002", "限流")
        states = risk_control.get_all_risk_states()
        assert len(states) == 2
        assert states["DEV001"]["risk"] is True
        assert states["DEV002"]["risk"] is True

    def test_unknown_device_not_at_risk(self):
        assert not risk_control.is_at_risk("UNKNOWN")
        state = risk_control.get_risk_state("UNKNOWN")
        assert state["risk"] is False


# ── 场景步骤类型注册 ────────────────────────────────────

class TestStepTypes:
    def test_verify_publish_in_step_types(self):
        assert "verify_publish" in scenario.STEP_TYPES

    def test_detect_risk_in_step_types(self):
        assert "detect_risk" in scenario.STEP_TYPES


# ── 发布场景模板 API ────────────────────────────────────

class TestPublishTemplatesApi:
    def test_list_templates(self, client):
        r = client.get("/api/publish-templates")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        # 应该有预置模板
        if data:
            assert "name" in data[0]
            assert "steps" in data[0]

    def test_list_by_platform(self, client):
        r = client.get("/api/publish-templates?platform=抖音")
        assert r.status_code == 200
        data = r.json()
        for t in data:
            assert t["platform"] == "抖音"

    def test_get_template(self, client):
        # 先列出获取一个文件名
        r = client.get("/api/publish-templates")
        data = r.json()
        if data:
            filename = data[0]["file"]
            r2 = client.get(f"/api/publish-templates/{filename}")
            assert r2.status_code == 200
            assert "steps" in r2.json()

    def test_get_nonexistent_template(self, client):
        r = client.get("/api/publish-templates/nonexistent.json")
        assert r.status_code == 404

    def test_create_scenario_from_template(self, client):
        r = client.get("/api/publish-templates")
        data = r.json()
        if data:
            filename = data[0]["file"]
            r2 = client.post("/api/publish-templates/create-scenario", json={
                "filename": filename, "name": "测试场景"
            })
            assert r2.status_code == 200
            result = r2.json()
            assert result["ok"] is True
            assert result["scenario_id"] > 0
            assert result["steps_count"] > 0
            # 验证场景已创建
            sc = db.get_scenario(result["scenario_id"])
            assert sc is not None
            assert sc["name"] == "测试场景"

    def test_builtin_templates_packaged(self):
        """回归：内置模板必须存在于 assets（随 exe 打包），否则发布版无模板可用。"""
        from narci.config import PUBLISH_TEMPLATES_DIR
        assert PUBLISH_TEMPLATES_DIR.is_dir(), \
            f"内置模板目录不存在: {PUBLISH_TEMPLATES_DIR}"
        files = sorted(f.name for f in PUBLISH_TEMPLATES_DIR.glob("*.json"))
        assert len(files) >= 8, f"内置模板数量不足: {files}"

    def test_route_registered_on_list_endpoint(self, client):
        """回归：防止 @app.get 装饰器误挂到辅助函数导致路由返回非列表。"""
        from narci.main import _publish_templates_dir
        assert not hasattr(_publish_templates_dir, "routes"), \
            "辅助函数 _publish_templates_dir 被误注册为路由"
        r = client.get("/api/publish-templates")
        assert r.status_code == 200
        assert isinstance(r.json(), list), "路由应返回列表而非路径字符串"


# ── 风控 API ────────────────────────────────────────────

class TestRiskApi:
    def setup_method(self):
        risk_control._risk_state.clear()

    def test_risk_status_empty(self, client):
        r = client.get("/api/risk/status")
        assert r.status_code == 200
        assert r.json() == {}

    def test_risk_status_with_data(self, client):
        risk_control.set_risk("DEV001", "验证码")
        r = client.get("/api/risk/status")
        assert r.status_code == 200
        data = r.json()
        assert "DEV001" in data
        assert data["DEV001"]["risk"] is True

    def test_risk_device_status(self, client):
        risk_control.set_risk("DEV001", "限流")
        r = client.get("/api/risk/status/DEV001")
        assert r.status_code == 200
        assert r.json()["risk"] is True

    def test_risk_clear(self, client):
        risk_control.set_risk("DEV001", "验证码")
        r = client.post("/api/risk/clear/DEV001")
        assert r.status_code == 200
        assert not risk_control.is_at_risk("DEV001")


# ── 分发-场景闭环 ────────────────────────────────────────

class TestDistributionScenarioLink:
    def test_link_scenario_to_task(self, client):
        # 创建内容、账号、任务
        c = client.post("/api/contents", json={"title": "测试"}).json()
        a = client.post("/api/accounts", json={"platform": "抖音", "name": "号1"}).json()
        t = client.post("/api/distribution-tasks", json={
            "content_id": c["id"], "account_id": a["id"],
            "scheduled_at": time.time() + 3600
        }).json()
        # 创建场景
        sc = db.create_scenario("发布场景", "[]")
        # 关联
        r = client.post(f"/api/distribution-tasks/{t['id']}/link-scenario",
                        json={"scenario_id": sc["id"]})
        assert r.status_code == 200
        # 验证
        task = db.query("SELECT * FROM distribution_tasks WHERE id=?", (t["id"],))[0]
        assert task["scenario_id"] == sc["id"]

    def test_link_nonexistent_scenario(self, client):
        c = client.post("/api/contents", json={"title": "测试"}).json()
        a = client.post("/api/accounts", json={"platform": "抖音", "name": "号1"}).json()
        t = client.post("/api/distribution-tasks", json={
            "content_id": c["id"], "account_id": a["id"],
            "scheduled_at": time.time() + 3600
        }).json()
        r = client.post(f"/api/distribution-tasks/{t['id']}/link-scenario",
                        json={"scenario_id": 99999})
        assert r.status_code == 404
