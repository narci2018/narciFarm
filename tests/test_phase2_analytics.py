"""阶段二：数据看板 / 互动管理 / 自动回复 / 内容模板 / 分发分析。"""
import json
import time
import pytest
from fastapi.testclient import TestClient

from narci import db
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


# ── 数据看板 ────────────────────────────────────────────

class TestMetricsRange:
    def test_range_returns_data(self):
        a = db.create_account("抖音", "号1")
        now = time.time()
        for i in range(5):
            db.record_metric(a["id"], now - i * 86400,
                             followers=100 + i * 10, likes=50 + i * 5,
                             comments=10 + i, views=1000 + i * 100)
        result = db.metrics_range(a["id"], now - 6 * 86400, now, "day")
        assert len(result) >= 1
        assert "period" in result[0]
        assert "followers" in result[0]


# ── 分发分析 ────────────────────────────────────────────

class TestDistributionAnalytics:
    def test_analytics_empty(self):
        data = db.distribution_analytics(7)
        assert data["total"] == 0
        assert data["success_rate"] == 0

    def test_analytics_with_tasks(self):
        a = db.create_account("抖音", "号1")
        c = db.create_content("分析内容")
        now = time.time()
        # 创建成功和失败的任务
        t1 = db.create_distribution_task(c["id"], a["id"], now - 100)
        db.update_distribution_task(t1["id"], status="published", published_at=now)
        t2 = db.create_distribution_task(c["id"], a["id"], now - 50)
        db.update_distribution_task(t2["id"], status="failed", error="timeout")
        data = db.distribution_analytics(30)
        assert data["total"] == 2
        assert data["success"] == 1
        assert data["failed"] == 1
        assert data["success_rate"] == 50.0
        assert len(data["by_platform"]) > 0


# ── 互动消息 ────────────────────────────────────────────

class TestInteractions:
    def test_create_and_list(self):
        a = db.create_account("抖音", "号1")
        m = db.create_interaction(a["id"], "抖音", "comment", "好棒!", "用户A", "ua001")
        assert m["id"] >= 1
        msgs = db.list_interactions()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "好棒!"

    def test_filter_by_status(self):
        a = db.create_account("抖音", "号1")
        db.create_interaction(a["id"], "抖音", "comment", "消息1")
        db.create_interaction(a["id"], "抖音", "dm", "消息2")
        unread = db.list_interactions(status="unread")
        assert len(unread) == 2
        comments = db.list_interactions(kind="comment")
        assert len(comments) == 1

    def test_mark_replied(self):
        a = db.create_account("抖音", "号1")
        m = db.create_interaction(a["id"], "抖音", "comment", "问题?")
        db.mark_interaction_replied(m["id"], "感谢关注!")
        msgs = db.list_interactions(status="replied")
        assert len(msgs) == 1
        assert msgs[0]["reply_text"] == "感谢关注!"

    def test_count_unread(self):
        a = db.create_account("抖音", "号1")
        db.create_interaction(a["id"], "抖音", "comment", "A")
        db.create_interaction(a["id"], "抖音", "comment", "B")
        assert db.count_unread_interactions() == 2
        assert db.count_unread_interactions(a["id"]) == 2

    def test_mark_read(self):
        a = db.create_account("抖音", "号1")
        m = db.create_interaction(a["id"], "抖音", "comment", "已读测试")
        db.mark_interaction_read(m["id"])
        assert db.count_unread_interactions() == 0


# ── 自动回复规则 ────────────────────────────────────────

class TestReplyRules:
    def test_create_and_list(self):
        r = db.create_reply_rule("欢迎", "感谢关注!", match_type="all", priority=0)
        assert r["id"] >= 1
        rules = db.list_reply_rules()
        assert len(rules) == 1

    def test_keyword_matching(self):
        a = db.create_account("抖音", "号1")
        kws = ["价格", "多少钱"]
        db.create_reply_rule("价格咨询", "请私信获取价格",
                             keywords=json.dumps(kws, ensure_ascii=False),
                             match_type="keyword", priority=10)
        rules = db.get_reply_rules_for_account(a["id"], "comment")
        assert len(rules) == 1
        stored_kws = json.loads(rules[0]["keywords"])
        assert "价格" in stored_kws

    def test_global_rules_apply(self):
        """account_id=0 的全局规则应对所有账号生效。"""
        a = db.create_account("抖音", "号1")
        db.create_reply_rule("全局欢迎", "欢迎关注!", match_type="all",
                             account_id=0, priority=0)
        rules = db.get_reply_rules_for_account(a["id"])
        assert len(rules) == 1

    def test_update_and_delete(self):
        r = db.create_reply_rule("测试", "回复", match_type="all")
        db.update_reply_rule(r["id"], enabled=0)
        got = db.query("SELECT * FROM reply_rules WHERE id=?", (r["id"],))[0]
        assert got["enabled"] == 0
        db.delete_reply_rule(r["id"])
        assert len(db.list_reply_rules()) == 0


# ── 内容模板 ────────────────────────────────────────────

class TestContentTemplates:
    def test_create_and_render(self):
        t = db.create_content_template(
            "美食模板", "抖音",
            title_template="{{菜品名}} | {{城市}}美食推荐",
            text_template="今天给大家推荐{{城市}}的{{菜品名}}！{{标签}}",
            variables='{"菜品名":"火锅","城市":"重庆","标签":"#美食"}')
        assert t["id"] >= 1
        title = db.render_template(t["title_template"],
                                   {"菜品名": "火锅", "城市": "重庆"})
        assert title == "火锅 | 重庆美食推荐"
        text = db.render_template(t["text_template"],
                                  {"菜品名": "火锅", "城市": "重庆", "标签": "#美食"})
        assert "重庆" in text
        assert "#美食" in text

    def test_list_by_platform(self):
        db.create_content_template("抖音模板", "抖音", "标题")
        db.create_content_template("快手模板", "快手", "标题")
        db.create_content_template("通用模板", "", "标题")
        assert len(db.list_content_templates("抖音")) == 1
        assert len(db.list_content_templates()) == 3

    def test_update_and_delete(self):
        t = db.create_content_template("旧", "", "旧标题")
        db.update_content_template(t["id"], name="新", title_template="新标题")
        got = db.get_content_template(t["id"])
        assert got["name"] == "新"
        db.delete_content_template(t["id"])
        assert db.get_content_template(t["id"]) is None


# ── API 层 ─────────────────────────────────────────────

class TestApiAnalytics:
    def test_distribution_analytics(self, client):
        r = client.get("/api/distribution/analytics?days=30")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "success_rate" in data

    def test_metrics_range(self, client):
        a = client.post("/api/accounts", json={"platform": "抖音", "name": "号1"}).json()
        now = time.time()
        db.record_metric(a["id"], now, followers=100, likes=50)
        r = client.get(f"/api/metrics/range?account_id={a['id']}"
                       f"&start={now - 86400}&end={now + 86400}&granularity=day")
        assert r.status_code == 200


class TestApiInteractions:
    def test_crud_flow(self, client):
        a = client.post("/api/accounts", json={"platform": "抖音", "name": "号1"}).json()
        # 创建消息
        m = client.post("/api/interactions", json={
            "account_id": a["id"], "platform": "抖音",
            "kind": "comment", "content": "好棒!", "author_name": "用户A"
        }).json()
        assert m["id"] >= 1
        # 列表
        msgs = client.get("/api/interactions").json()
        assert len(msgs) == 1
        # 未读计数
        ur = client.get("/api/interactions/unread-count").json()
        assert ur["count"] == 1
        # 标记已读
        client.post(f"/api/interactions/{m['id']}/read")
        ur2 = client.get("/api/interactions/unread-count").json()
        assert ur2["count"] == 0
        # 回复
        client.post(f"/api/interactions/{m['id']}/reply", json={"reply_text": "谢谢!"})
        msgs2 = client.get("/api/interactions?status=replied").json()
        assert len(msgs2) == 1

    def test_auto_reply(self, client):
        a = client.post("/api/accounts", json={"platform": "抖音", "name": "号1"}).json()
        # 创建规则
        client.post("/api/reply-rules", json={
            "name": "价格", "reply_text": "请私信",
            "match_type": "keyword",
            "keywords": json.dumps(["价格", "多少钱"])
        })
        # 创建消息
        m = client.post("/api/interactions", json={
            "account_id": a["id"], "kind": "comment",
            "content": "这个多少钱?"
        }).json()
        # 自动回复
        r = client.post(f"/api/interactions/{m['id']}/auto-reply", json={})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert "请私信" in r.json()["reply_text"]


class TestApiReplyRules:
    def test_crud(self, client):
        r = client.post("/api/reply-rules", json={
            "name": "测试规则", "reply_text": "回复内容",
            "match_type": "all"
        })
        assert r.status_code == 200
        rid = r.json()["id"]
        # 更新
        r2 = client.put(f"/api/reply-rules/{rid}", json={"enabled": 0})
        assert r2.status_code == 200
        rules = client.get("/api/reply-rules").json()
        found = [x for x in rules if x["id"] == rid][0]
        assert found["enabled"] == 0
        # 删除
        client.delete(f"/api/reply-rules/{rid}")
        rules2 = client.get("/api/reply-rules").json()
        assert all(x["id"] != rid for x in rules2)


class TestApiTemplates:
    def test_crud_and_render(self, client):
        r = client.post("/api/content-templates", json={
            "name": "测试模板", "platform": "抖音",
            "title_template": "{{城市}}探店",
            "text_template": "今天来到{{城市}}的{{店名}}",
        })
        assert r.status_code == 200
        tid = r.json()["id"]
        # 渲染
        r2 = client.post(f"/api/content-templates/{tid}/render", json={
            "variables": {"城市": "北京", "店名": "老北京炸酱面"}
        })
        assert r2.status_code == 200
        assert r2.json()["title"] == "北京探店"
        assert "老北京炸酱面" in r2.json()["text"]
        # 更新
        client.put(f"/api/content-templates/{tid}", json={"name": "新模板"})
        t = client.get(f"/api/content-templates/{tid}").json()
        assert t["name"] == "新模板"
        # 删除
        client.delete(f"/api/content-templates/{tid}")
        r3 = client.get(f"/api/content-templates/{tid}")
        assert r3.status_code == 404
