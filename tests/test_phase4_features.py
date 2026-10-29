"""阶段四测试：竞品采集 / A-B测试 / 情感分析 / 多机协作 / 新增平台模板。"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["NARCI_TEST_MODE"] = "1"

# 确保测试能找到 narci 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from narci import db, collector, sentiment


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    """每个测试前初始化临时数据库。"""
    db._conn = None
    db.connect(tmp_path / "test.db")
    yield
    if db._conn:
        db._conn.close()
    db._conn = None


# ========== 竞品采集 ==========

class TestCompetitorSources:
    def test_create_source(self):
        s = db.create_competitor_source("测试RSS", "https://example.com/feed",
                                        platform="抖音", source_type="rss")
        assert s["id"] > 0
        assert s["name"] == "测试RSS"
        assert s["url"] == "https://example.com/feed"
        assert s["source_type"] == "rss"

    def test_list_sources(self):
        db.create_competitor_source("源1", "https://a.com/rss")
        db.create_competitor_source("源2", "https://b.com/rss")
        sources = db.list_competitor_sources()
        assert len(sources) == 2

    def test_list_sources_enabled_only(self):
        s1 = db.create_competitor_source("源1", "https://a.com/rss")
        s2 = db.create_competitor_source("源2", "https://b.com/rss")
        db.update_competitor_source(s2["id"], enabled=0)
        enabled = db.list_competitor_sources(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0]["name"] == "源1"

    def test_update_source(self):
        s = db.create_competitor_source("原名", "https://old.com")
        db.update_competitor_source(s["id"], name="新名", url="https://new.com")
        updated = db.get_competitor_source(s["id"])
        assert updated["name"] == "新名"
        assert updated["url"] == "https://new.com"

    def test_delete_source(self):
        s = db.create_competitor_source("要删的", "https://del.com")
        db.delete_competitor_source(s["id"])
        assert db.get_competitor_source(s["id"]) is None

    def test_update_collect_stats(self):
        s = db.create_competitor_source("统计测试", "https://stats.com")
        db.update_competitor_source(s["id"], last_collect=time.time(),
                                    total_collected=15, last_error="")
        updated = db.get_competitor_source(s["id"])
        assert updated["total_collected"] == 15
        assert updated["last_collect"] > 0


class TestCompetitorContents:
    def test_create_content(self):
        s = db.create_competitor_source("源", "https://src.com")
        c = db.create_competitor_content(
            source_id=s["id"], platform="抖音",
            title="热门视频", content="内容摘要",
            url="https://example.com/post/1",
            author="张三", content_hash="abc123",
        )
        assert c["id"] > 0
        assert c["title"] == "热门视频"
        assert c["content_hash"] == "abc123"

    def test_list_contents(self):
        s = db.create_competitor_source("源", "https://src.com")
        db.create_competitor_content(s["id"], "抖音", "标题1", content_hash="h1")
        db.create_competitor_content(s["id"], "快手", "标题2", content_hash="h2")
        items = db.list_competitor_contents()
        assert len(items) == 2

    def test_list_contents_filter_source(self):
        s1 = db.create_competitor_source("源1", "https://a.com")
        s2 = db.create_competitor_source("源2", "https://b.com")
        db.create_competitor_content(s1["id"], "抖音", "A的内容", content_hash="ha")
        db.create_competitor_content(s2["id"], "快手", "B的内容", content_hash="hb")
        filtered = db.list_competitor_contents(source_id=s1["id"])
        assert len(filtered) == 1
        assert filtered[0]["title"] == "A的内容"

    def test_dedup_by_hash(self):
        s = db.create_competitor_source("源", "https://src.com")
        db.create_competitor_content(s["id"], "抖音", "重复内容", content_hash="dup_hash")
        existing = db.get_competitor_content_by_hash("dup_hash")
        assert existing is not None
        # 再次查询应该返回同一条
        again = db.get_competitor_content_by_hash("dup_hash")
        assert again["id"] == existing["id"]

    def test_promote_content(self):
        s = db.create_competitor_source("源", "https://src.com")
        cc = db.create_competitor_content(
            s["id"], "抖音", "竞品好文", content="正文内容",
            content_hash="promote_test",
        )
        new_content = db.promote_competitor_content(cc["id"])
        assert new_content is not None
        assert new_content["title"] == "竞品好文"
        # 状态应变为 promoted
        updated = db.query_one("SELECT * FROM competitor_contents WHERE id=?", (cc["id"],))
        assert updated["status"] == "promoted"

    def test_delete_content(self):
        s = db.create_competitor_source("源", "https://src.com")
        cc = db.create_competitor_content(s["id"], "抖音", "要删的", content_hash="del_hash")
        db.delete_competitor_content(cc["id"])
        assert db.query_one("SELECT * FROM competitor_contents WHERE id=?", (cc["id"],)) is None


class TestCollectorEngine:
    def test_rss_parsing(self):
        """测试 RSS XML 解析逻辑。"""
        xml = """<?xml version="1.0"?>
        <rss><channel>
        <item><title>文章一</title><link>https://a.com/1</link>
        <description>摘要一</description><author>作者A</author></item>
        <item><title>文章二</title><link>https://a.com/2</link>
        <description>摘要二</description></item>
        </channel></rss>"""
        items = self._mock_rss_collect(xml)
        assert len(items) >= 2
        assert items[0]["title"] == "文章一"

    def _mock_rss_collect(self, xml):
        """直接测试 XML 解析。"""
        import re
        items = []
        item_blocks = re.findall(r"<item[^>]*>(.*?)</item>", xml, re.DOTALL)
        for block in item_blocks:
            title = collector._extract_tag(block, "title")
            link = collector._extract_tag(block, "link")
            desc = collector._extract_tag(block, "description")
            author = collector._extract_tag(block, "author")
            items.append({"title": title, "url": link, "content": desc,
                          "author": author, "tags": [], "metrics": {}})
        return items

    def test_extract_tag(self):
        xml = "<title>测试标题</title>"
        assert collector._extract_tag(xml, "title") == "测试标题"

    def test_extract_tag_missing(self):
        xml = "<title>只有标题</title>"
        assert collector._extract_tag(xml, "description") == ""

    def test_extract_tag_attr(self):
        xml = '<link href="https://example.com" rel="alternate"/>'
        assert collector._extract_tag_attr(xml, "link", "href") == "https://example.com"


# ========== 情感分析 ==========

class TestSentimentAnalysis:
    def test_negative_detection(self):
        result = sentiment.analyze_sentiment("这个视频真是垃圾，太无聊了")
        assert result["label"] == "negative"
        assert result["score"] < 0
        assert "垃圾" in result["matched_negative"]
        assert "无聊" in result["matched_negative"]

    def test_positive_detection(self):
        result = sentiment.analyze_sentiment("太好看了，支持博主，加油！")
        assert result["label"] == "positive"
        assert result["score"] > 0
        assert "好看" in result["matched_positive"]

    def test_neutral(self):
        result = sentiment.analyze_sentiment("今天天气不错")
        assert result["label"] == "neutral"
        assert abs(result["score"]) < 0.3

    def test_empty_text(self):
        result = sentiment.analyze_sentiment("")
        assert result["label"] == "neutral"
        assert result["score"] == 0.0

    def test_strong_negative(self):
        result = sentiment.analyze_sentiment("骗子！举报了，抄袭别人的")
        assert result["label"] == "negative"
        assert result["score"] <= -0.5
        assert "骗子" in result["matched_negative"]
        assert "举报" in result["matched_negative"]
        assert "抄袭" in result["matched_negative"]

    def test_mixed_sentiment(self):
        result = sentiment.analyze_sentiment("好看是好看，但有点失望")
        assert "好看" in result["matched_positive"]
        assert "失望" in result["matched_negative"]

    def test_confidence(self):
        # 多个关键词应该有更高置信度
        result1 = sentiment.analyze_sentiment("垃圾")
        result2 = sentiment.analyze_sentiment("垃圾 骗子 举报 投诉")
        assert result2["confidence"] >= result1["confidence"]

    def test_process_interaction(self):
        account = db.create_account("抖音", "测试账号")
        msg = db.create_interaction(
            account_id=account["id"], platform="抖音",
            kind="comment", content="这视频真垃圾",
            author_name="用户A",
        )
        result = sentiment.process_interaction(msg)
        assert result["label"] == "negative"
        # 数据库应已更新
        updated = db.query_one("SELECT * FROM interaction_messages WHERE id=?", (msg["id"],))
        assert updated["sentiment"] == "negative"
        assert updated["sentiment_score"] < 0

    def test_batch_analyze(self):
        account = db.create_account("快手", "批量测试账号")
        db.create_interaction(account_id=account["id"], platform="快手",
                              kind="comment", content="好看，赞！")
        db.create_interaction(account_id=account["id"], platform="快手",
                              kind="comment", content="垃圾内容")
        db.create_interaction(account_id=account["id"], platform="快手",
                              kind="comment", content="还行吧")
        stats = sentiment.batch_analyze_interactions(account["id"])
        assert stats["total"] == 3
        assert stats["negative"] >= 1
        assert stats["positive"] >= 1

    def test_sentiment_summary(self):
        account = db.create_account("小红书", "汇总测试")
        db.create_interaction(account_id=account["id"], platform="小红书",
                              kind="comment", content="太棒了")
        db.create_interaction(account_id=account["id"], platform="小红书",
                              kind="comment", content="太差了")
        # 先分析
        sentiment.batch_analyze_interactions(account["id"])
        summary = sentiment.get_sentiment_summary(account["id"])
        assert summary["total"] >= 2


# ========== A/B 测试 ==========

class TestAbTests:
    def test_create_test(self):
        t = db.create_ab_test("标题测试", content_id=0)
        assert t["id"] > 0
        assert t["name"] == "标题测试"
        assert t["status"] == "draft"

    def test_list_tests(self):
        db.create_ab_test("测试1")
        db.create_ab_test("测试2")
        tests = db.list_ab_tests()
        assert len(tests) >= 2

    def test_create_variant(self):
        t = db.create_ab_test("多变体测试")
        v1 = db.create_ab_variant(t["id"], "A", "标题A", "内容A", account_id=1)
        v2 = db.create_ab_variant(t["id"], "B", "标题B", "内容B", account_id=2)
        assert v1["variant_key"] == "A"
        assert v2["variant_key"] == "B"

    def test_get_test_with_variants(self):
        t = db.create_ab_test("含变体测试")
        db.create_ab_variant(t["id"], "A", "标题A")
        db.create_ab_variant(t["id"], "B", "标题B")
        full = db.get_ab_test(t["id"])
        assert len(full["variants"]) == 2

    def test_update_variant_metrics(self):
        t = db.create_ab_test("指标测试")
        v = db.create_ab_variant(t["id"], "A", "标题A", account_id=1)
        db.update_ab_variant(v["id"], views=1000, likes=50, comments=10, shares=5)
        updated = db.query_one("SELECT * FROM ab_variants WHERE id=?", (v["id"],))
        assert updated["views"] == 1000
        assert updated["likes"] == 50

    def test_ab_analytics(self):
        t = db.create_ab_test("分析测试")
        v1 = db.create_ab_variant(t["id"], "A", "标题A", account_id=1)
        v2 = db.create_ab_variant(t["id"], "B", "标题B", account_id=2)
        db.update_ab_variant(v1["id"], views=500, likes=30, comments=5, shares=2)
        db.update_ab_variant(v2["id"], views=600, likes=20, comments=3, shares=1)
        analytics = db.ab_test_analytics(t["id"])
        assert analytics["test"]["id"] == t["id"]
        assert len(analytics["variants"]) == 2
        # A 的互动分 = 500 + 30*3 + 5*5 + 2*8 = 500+90+25+16 = 631
        # B 的互动分 = 600 + 20*3 + 3*5 + 1*8 = 600+60+15+8 = 683
        assert analytics["ranked"][0]["engagement_score"] > 0

    def test_conclude_test(self):
        t = db.create_ab_test("结束测试")
        v1 = db.create_ab_variant(t["id"], "A", "标题A")
        db.update_ab_test(t["id"], status="running", started_at=time.time())
        db.update_ab_test(t["id"], status="concluded",
                          winner_variant_id=v1["id"], ended_at=time.time())
        updated = db.query_one("SELECT * FROM ab_tests WHERE id=?", (t["id"],))
        assert updated["status"] == "concluded"
        assert updated["winner_variant_id"] == v1["id"]

    def test_delete_test(self):
        t = db.create_ab_test("删除测试")
        db.create_ab_variant(t["id"], "A", "标题A")
        db.delete_ab_test(t["id"])
        assert db.query_one("SELECT * FROM ab_tests WHERE id=?", (t["id"],)) is None
        variants = db.query("SELECT * FROM ab_variants WHERE test_id=?", (t["id"],))
        assert len(variants) == 0


# ========== 多机协作 ==========

class TestRelayFlows:
    def test_create_flow(self):
        f = db.create_relay_flow("测试流水线", '[{"type":"tap","x":100,"y":200}]',
                                 '["device1","device2"]', "接力发布测试")
        assert f["id"] > 0
        assert f["name"] == "测试流水线"

    def test_list_flows(self):
        db.create_relay_flow("流水线1", "[]", "[]")
        db.create_relay_flow("流水线2", "[]", "[]")
        flows = db.list_relay_flows()
        assert len(flows) >= 2

    def test_update_flow(self):
        f = db.create_relay_flow("原名", "[]", "[]")
        db.update_relay_flow(f["id"], name="新名", status="running")
        updated = db.get_relay_flow(f["id"])
        assert updated["name"] == "新名"
        assert updated["status"] == "running"

    def test_delete_flow(self):
        f = db.create_relay_flow("要删的", "[]", "[]")
        db.delete_relay_flow(f["id"])
        assert db.get_relay_flow(f["id"]) is None


# ========== 平台模板 ==========

class TestPublishTemplates:
    def test_new_templates_exist(self):
        """验证新增的 5 个平台模板文件存在且可解析。"""
        tpl_dir = Path(__file__).resolve().parent.parent / "data" / "publish_templates"
        expected = ["weibo_post.json", "bilibili_video.json", "wechat_channels.json",
                    "toutiao_article.json", "zhihu_article.json"]
        for name in expected:
            path = tpl_dir / name
            assert path.exists(), f"模板文件不存在: {name}"
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["name"], f"模板缺少 name: {name}"
            assert data["platform"], f"模板缺少 platform: {name}"
            assert len(data["steps"]) > 0, f"模板步骤为空: {name}"
            assert "variables" in data, f"模板缺少 variables: {name}"

    def test_templates_have_risk_detection(self):
        """验证所有模板包含风控检测步骤。"""
        tpl_dir = Path(__file__).resolve().parent.parent / "data" / "publish_templates"
        for f in tpl_dir.glob("*.json"):
            data = json.loads(f.read_text(encoding="utf-8"))
            risk_steps = [s for s in data["steps"] if s["type"] == "detect_risk"]
            assert len(risk_steps) >= 1, f"{f.name} 缺少 detect_risk 步骤"

    def test_templates_have_verification(self):
        """验证所有模板包含发布验证步骤。"""
        tpl_dir = Path(__file__).resolve().parent.parent / "data" / "publish_templates"
        for f in tpl_dir.glob("*.json"):
            data = json.loads(f.read_text(encoding="utf-8"))
            verify_steps = [s for s in data["steps"] if s["type"] == "verify_publish"]
            assert len(verify_steps) >= 1, f"{f.name} 缺少 verify_publish 步骤"


# ========== 数据库迁移 ==========

class TestMigration:
    def test_sentiment_columns_added(self):
        """验证情感分析列已迁移。"""
        cols = {r["name"] for r in db.query("PRAGMA table_info(interaction_messages)")}
        assert "sentiment" in cols
        assert "sentiment_score" in cols
        assert "sentiment_detail" in cols

    def test_ab_variant_columns(self):
        """验证 A/B 测试列已迁移。"""
        cols = {r["name"] for r in db.query("PRAGMA table_info(distribution_tasks)")}
        assert "ab_variant_id" in cols
        assert "content_snapshot" in cols

    def test_new_tables_exist(self):
        """验证新表已创建。"""
        tables = db.query("SELECT name FROM sqlite_master WHERE type='table'")
        table_names = {t["name"] for t in tables}
        assert "competitor_sources" in table_names
        assert "competitor_contents" in table_names
        assert "ab_tests" in table_names
        assert "ab_variants" in table_names
        assert "relay_flows" in table_names


# ========== 场景步骤类型 ==========

class TestScenarioStepTypes:
    def test_relay_in_step_types(self):
        from narci.scenario import STEP_TYPES
        assert "relay" in STEP_TYPES
        assert "relay_barrier" in STEP_TYPES

    def test_validate_relay_step(self):
        from narci.scenario import validate_steps
        # 不应抛异常
        validate_steps([{"type": "relay", "target_serial": "dev1",
                         "steps": [{"type": "tap", "x": 100, "y": 200}]}])

    def test_validate_relay_barrier_step(self):
        from narci.scenario import validate_steps
        validate_steps([{"type": "relay_barrier", "barrier_id": "sync1"}])
