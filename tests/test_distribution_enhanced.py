"""阶段一：分发模块增强 — 测试 CRUD、批量分发、重试、立即发布、错峰调度。"""
import asyncio
import time
import pytest
from fastapi.testclient import TestClient

from narci import db, distribution
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
        assert r.status_code == 200, f"测试登录失败: {r.text}"
        tc.headers = {"Authorization": "Bearer " + r.json()["token"]}
        yield tc


# ── db 层 ──────────────────────────────────────────────

class TestDbAccountCRUD:
    def test_create_and_get(self):
        a = db.create_account("抖音", "测试号", "user1", "tok", "https://api.example.com")
        assert a["id"] >= 1
        got = db.get_account(a["id"])
        assert got["platform"] == "抖音"
        assert got["name"] == "测试号"

    def test_update_account(self):
        a = db.create_account("抖音", "旧名")
        db.update_account(a["id"], name="新名", status="paused")
        got = db.get_account(a["id"])
        assert got["name"] == "新名"
        assert got["status"] == "paused"

    def test_update_ignores_unknown_fields(self):
        a = db.create_account("抖音", "测试")
        db.update_account(a["id"], bogus="xxx")  # 应静默忽略
        got = db.get_account(a["id"])
        assert got["name"] == "测试"


class TestDbContentCRUD:
    def test_create_get_update(self):
        c = db.create_content("标题A", "text", "正文")
        assert c["status"] == "ready"
        got = db.get_content(c["id"])
        assert got["title"] == "标题A"
        db.update_content(c["id"], title="标题B", status="published")
        got2 = db.get_content(c["id"])
        assert got2["title"] == "标题B"
        assert got2["status"] == "published"


class TestDbDistributionRetry:
    def test_retry_fields_in_task(self):
        a = db.create_account("抖音", "号1")
        c = db.create_content("内容1")
        t = db.create_distribution_task(c["id"], a["id"], time.time(), max_retries=5)
        assert t["max_retries"] == 5
        assert t["retry_count"] == 0
        db.update_distribution_task(t["id"], retry_count=2, status="failed")
        got = db.query("SELECT * FROM distribution_tasks WHERE id=?", (t["id"],))[0]
        assert got["retry_count"] == 2
        assert got["status"] == "failed"

    def test_due_includes_retryable(self):
        a = db.create_account("抖音", "号1")
        c = db.create_content("内容1")
        # 创建一个待执行任务
        t1 = db.create_distribution_task(c["id"], a["id"], time.time() - 10)
        # 创建一个失败但可重试的任务
        t2 = db.create_distribution_task(c["id"], a["id"], time.time() - 10, max_retries=3)
        db.update_distribution_task(t2["id"], status="failed", retry_count=1)
        due = db.due_distribution_tasks()
        ids = {t["id"] for t in due}
        assert t1["id"] in ids
        assert t2["id"] in ids

    def test_due_excludes_exhausted_retries(self):
        a = db.create_account("抖音", "号1")
        c = db.create_content("内容1")
        t = db.create_distribution_task(c["id"], a["id"], time.time() - 10, max_retries=2)
        db.update_distribution_task(t["id"], status="failed", retry_count=2)
        due = db.due_distribution_tasks()
        ids = {x["id"] for x in due}
        assert t["id"] not in ids

    def test_cancel_task(self):
        a = db.create_account("抖音", "号1")
        c = db.create_content("内容1")
        t = db.create_distribution_task(c["id"], a["id"], time.time() + 100)
        db.cancel_distribution_task(t["id"])
        got = db.query("SELECT * FROM distribution_tasks WHERE id=?", (t["id"],))[0]
        assert got["status"] == "cancelled"

    def test_retry_task_resets_status(self):
        a = db.create_account("抖音", "号1")
        c = db.create_content("内容1")
        t = db.create_distribution_task(c["id"], a["id"], time.time() - 10)
        db.update_distribution_task(t["id"], status="failed", error="oops")
        db.retry_distribution_task(t["id"])
        got = db.query("SELECT * FROM distribution_tasks WHERE id=?", (t["id"],))[0]
        assert got["status"] == "pending"
        assert got["error"] == ""


class TestDbBatchDistribution:
    def test_batch_creates_multiple_tasks(self):
        c = db.create_content("批量内容")
        a1 = db.create_account("抖音", "号1")
        a2 = db.create_account("快手", "号2")
        a3 = db.create_account("小红书", "号3")
        now = time.time()
        tasks = db.create_distribution_tasks_batch(
            c["id"], [a1["id"], a2["id"], a3["id"]], now, interval_minutes=10)
        assert len(tasks) == 3
        # 验证错峰时间递增
        assert tasks[1]["scheduled_at"] > tasks[0]["scheduled_at"]
        assert tasks[2]["scheduled_at"] > tasks[1]["scheduled_at"]
        diff = tasks[1]["scheduled_at"] - tasks[0]["scheduled_at"]
        assert abs(diff - 600) < 2  # 10分钟 = 600秒

    def test_batch_zero_interval(self):
        c = db.create_content("同时发")
        a1 = db.create_account("抖音", "号1")
        a2 = db.create_account("快手", "号2")
        now = time.time()
        tasks = db.create_distribution_tasks_batch(
            c["id"], [a1["id"], a2["id"]], now, interval_minutes=0)
        assert tasks[0]["scheduled_at"] == tasks[1]["scheduled_at"]


# ── distribution 层 ────────────────────────────────────

class TestDistributionRetry:
    def test_failed_task_retries_with_backoff(self):
        """WebhookPublisher 连接失败时应递增 retry_count 并推后 scheduled_at。"""
        a = db.create_account("抖音", "号1", api_base="http://127.0.0.1:1/unreachable")
        c = db.create_content("重试内容")
        t = db.create_distribution_task(c["id"], a["id"], time.time(), max_retries=3)
        asyncio.run(distribution._execute_task(t))
        got = db.query("SELECT * FROM distribution_tasks WHERE id=?", (t["id"],))[0]
        assert got["status"] == "failed"
        assert got["retry_count"] == 1
        assert got["scheduled_at"] > time.time()

    def test_final_failure_when_exhausted(self):
        """max_retries=0 时失败一次即最终失败。"""
        a = db.create_account("抖音", "号1", api_base="http://127.0.0.1:1/unreachable")
        c = db.create_content("最终失败")
        t = db.create_distribution_task(c["id"], a["id"], time.time(), max_retries=0)
        db.update_distribution_task(t["id"], retry_count=0)
        asyncio.run(distribution._execute_task(t))
        got = db.query("SELECT * FROM distribution_tasks WHERE id=?", (t["id"],))[0]
        assert got["status"] == "failed"
        assert got["retry_count"] == 1

    def test_publish_now(self):
        """立即发布应创建任务并执行。"""
        a = db.create_account("抖音", "号1")  # 无 api_base → ManualPublisher
        c = db.create_content("立即发")
        results = asyncio.run(distribution.publish_now(c["id"], [a["id"]]))
        assert len(results) == 1
        assert results[0]["status"] == "published"


# ── API 层 ─────────────────────────────────────────────

class TestApiAccountContent:
    def test_put_account(self, client):
        # 先创建一个
        r = client.post("/api/accounts", json={"platform": "抖音", "name": "旧"})
        aid = r.json()["id"]
        # 更新
        r2 = client.put(f"/api/accounts/{aid}", json={"name": "新", "status": "paused"})
        assert r2.status_code == 200
        # 验证
        accounts = client.get("/api/accounts").json()
        found = [a for a in accounts if a["id"] == aid][0]
        assert found["name"] == "新"
        assert found["status"] == "paused"

    def test_put_content(self, client):
        r = client.post("/api/contents", json={"title": "旧标题", "type": "text"})
        cid = r.json()["id"]
        r2 = client.put(f"/api/contents/{cid}", json={"title": "新标题", "status": "draft"})
        assert r2.status_code == 200
        contents = client.get("/api/contents").json()
        found = [c for c in contents if c["id"] == cid][0]
        assert found["title"] == "新标题"
        assert found["status"] == "draft"


class TestApiBatchDistribution:
    def test_batch_endpoint(self, client):
        c = client.post("/api/contents", json={"title": "批量测试"}).json()
        a1 = client.post("/api/accounts", json={"platform": "抖音", "name": "号1"}).json()
        a2 = client.post("/api/accounts", json={"platform": "快手", "name": "号2"}).json()
        now = time.time() + 3600
        r = client.post("/api/distribution-tasks/batch", json={
            "content_id": c["id"],
            "account_ids": [a1["id"], a2["id"]],
            "scheduled_at": now,
            "interval_minutes": 15,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2
        tasks = client.get("/api/distribution-tasks").json()
        assert len(tasks) >= 2

    def test_publish_now_endpoint(self, client):
        c = client.post("/api/contents", json={"title": "立即发测试"}).json()
        a = client.post("/api/accounts", json={"platform": "抖音", "name": "号1"}).json()
        r = client.post("/api/distribution-tasks/publish-now", json={
            "content_id": c["id"],
            "account_ids": [a["id"]],
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True


class TestApiTaskCancelRetry:
    def test_cancel(self, client):
        c = client.post("/api/contents", json={"title": "取消测试"}).json()
        a = client.post("/api/accounts", json={"platform": "抖音", "name": "号1"}).json()
        t = client.post("/api/distribution-tasks", json={
            "content_id": c["id"], "account_id": a["id"],
            "scheduled_at": time.time() + 3600
        }).json()
        r = client.post(f"/api/distribution-tasks/{t['id']}/cancel")
        assert r.status_code == 200
        tasks = client.get("/api/distribution-tasks").json()
        found = [x for x in tasks if x["id"] == t["id"]][0]
        assert found["status"] == "cancelled"

    def test_retry(self, client):
        c = client.post("/api/contents", json={"title": "重试测试"}).json()
        a = client.post("/api/accounts", json={"platform": "抖音", "name": "号1"}).json()
        t = client.post("/api/distribution-tasks", json={
            "content_id": c["id"], "account_id": a["id"],
            "scheduled_at": time.time() - 10
        }).json()
        # 手动标记为失败
        db.update_distribution_task(t["id"], status="failed", error="test error")
        r = client.post(f"/api/distribution-tasks/{t['id']}/retry")
        assert r.status_code == 200
        tasks = client.get("/api/distribution-tasks").json()
        found = [x for x in tasks if x["id"] == t["id"]][0]
        assert found["status"] == "pending"


class TestApiDeleteTaskFixed:
    """验证 _lock bug 已修复。"""
    def test_delete_task_no_error(self, client):
        c = client.post("/api/contents", json={"title": "删除测试"}).json()
        a = client.post("/api/accounts", json={"platform": "抖音", "name": "号1"}).json()
        t = client.post("/api/distribution-tasks", json={
            "content_id": c["id"], "account_id": a["id"],
            "scheduled_at": time.time() + 3600
        }).json()
        r = client.delete(f"/api/distribution-tasks/{t['id']}")
        assert r.status_code == 200
        assert r.json()["ok"] is True
