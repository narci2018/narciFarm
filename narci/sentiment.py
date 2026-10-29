"""评论情感分析模块：基于关键词规则识别负面评论并预警。

无需外部 NLP 依赖，使用可配置的关键词/正则规则库进行情感判定。
支持：
- 负面关键词匹配
- 正则表达式模式匹配
- 情感评分（-1.0 到 1.0）
- 自动标记互动消息的情感倾向
- 负面评论告警
"""
import json
import re
import time
from typing import Optional

from . import db
from .events import bus


# 内置负面关键词库（可按权重打分）
NEGATIVE_KEYWORDS: dict[str, float] = {
    # 强烈负面
    "垃圾": -1.0, "骗子": -1.0, "举报": -0.9, "投诉": -0.8,
    "封号": -0.9, "违规": -0.8, "下架": -0.8,
    # 中度负面
    "难看": -0.6, "无聊": -0.5, "失望": -0.6, "差劲": -0.7,
    "恶心": -0.8, "抄袭": -0.9, "搬运": -0.7, "盗图": -0.9,
    "广告": -0.5, "营销": -0.4, "取关": -0.6, "脱粉": -0.7,
    "拉黑": -0.7, "举报了": -0.9,
    # 轻度负面
    "一般": -0.2, "还行吧": -0.1, "不太好": -0.3,
    "退步": -0.4, "不如以前": -0.5, "越来越差": -0.6,
    "没意思": -0.4, "看腻了": -0.3, "审美疲劳": -0.4,
}

# 正面关键词（用于对比）
POSITIVE_KEYWORDS: dict[str, float] = {
    "好看": 0.6, "优秀": 0.7, "棒": 0.6, "赞": 0.7, "喜欢": 0.5,
    "支持": 0.5, "加油": 0.4, "厉害": 0.6, "精彩": 0.6, "牛": 0.5,
    "爱了": 0.6, "收藏": 0.4, "转发": 0.3, "关注": 0.3,
}

# 否定词（翻转情感方向）
NEGATION_WORDS = {"不", "没", "没有", "别", "莫", "勿", "非", "无"}


def analyze_sentiment(text: str) -> dict:
    """分析文本情感倾向。

    返回:
      {
        "score": float,       # -1.0(极负面) 到 1.0(极正面)
        "label": str,         # "negative" / "neutral" / "positive"
        "confidence": float,  # 0.0 到 1.0
        "matched_negative": list[str],
        "matched_positive": list[str],
      }
    """
    if not text:
        return {"score": 0.0, "label": "neutral", "confidence": 0.0,
                "matched_negative": [], "matched_positive": []}

    text_lower = text.lower()
    neg_hits = []
    pos_hits = []
    neg_score = 0.0
    pos_score = 0.0

    for kw, weight in NEGATIVE_KEYWORDS.items():
        if kw in text_lower:
            neg_hits.append(kw)
            neg_score += weight

    for kw, weight in POSITIVE_KEYWORDS.items():
        if kw in text_lower:
            pos_hits.append(kw)
            pos_score += weight

    # 否定词检测：如果负面词前有否定词，减弱负面权重
    for kw in list(neg_hits):
        idx = text_lower.find(kw)
        prefix = text_lower[max(0, idx - 3):idx]
        kw_weight = NEGATIVE_KEYWORDS.get(kw, -0.5)
        if any(neg_word in prefix for neg_word in NEGATION_WORDS):
            neg_score -= kw_weight * 0.5  # 否定翻转，减弱一半
            neg_hits.remove(kw)

    # 计算综合分数
    total = neg_score + pos_score
    if total == 0:
        score = 0.0
    else:
        score = total / max(abs(total), 1.0)
        score = max(-1.0, min(1.0, score))

    # 标签判定
    if score <= -0.3:
        label = "negative"
    elif score >= 0.3:
        label = "positive"
    else:
        label = "neutral"

    # 置信度
    hits_count = len(neg_hits) + len(pos_hits)
    confidence = min(1.0, hits_count / 3.0) if hits_count > 0 else 0.0

    return {
        "score": round(score, 3),
        "label": label,
        "confidence": round(confidence, 2),
        "matched_negative": neg_hits,
        "matched_positive": pos_hits,
    }


def process_interaction(msg: dict) -> dict:
    """对一条互动消息进行情感分析并更新数据库。

    返回分析结果，如果是负面评论且严重程度超过阈值则创建告警。
    """
    content = msg.get("content", "")
    result = analyze_sentiment(content)

    # 更新互动消息的情感标记
    try:
        db.update_interaction_sentiment(
            msg["id"],
            sentiment=result["label"],
            sentiment_score=result["score"],
            sentiment_detail=json.dumps(result, ensure_ascii=False),
        )
    except Exception:
        pass  # 字段可能不存在（旧库）

    # 负面评论告警
    if result["label"] == "negative" and result["score"] <= -0.5:
        account_id = msg.get("account_id", 0)
        account = db.get_account(account_id) if account_id else None
        account_name = account["name"] if account else f"账号{account_id}"

        try:
            db.create_alert(
                serial=f"interaction:{msg['id']}",
                level="warn",
                kind="negative_comment",
                message=f"负面评论预警 [{account_name}]: "
                        f"{content[:50]}... (情感分={result['score']}, "
                        f"关键词: {', '.join(result['matched_negative'][:3])})",
            )
        except Exception:
            pass

        bus.warn(f"负面评论: [{account_name}] {content[:60]}... "
                 f"情感分={result['score']}")

    return result


def batch_analyze_interactions(account_id: int = 0, limit: int = 100) -> dict:
    """批量分析未标记的互动消息情感。

    返回处理统计。
    """
    interactions = db.list_interactions(
        account_id=account_id, limit=limit,
        sentiment_filter="unknown",
    )
    stats = {"total": 0, "negative": 0, "positive": 0, "neutral": 0}

    for msg in interactions:
        result = process_interaction(msg)
        stats["total"] += 1
        stats[result["label"]] += 1

    bus.info(f"情感分析完成: 共 {stats['total']} 条, "
             f"负面 {stats['negative']}, 正面 {stats['positive']}, "
             f"中性 {stats['neutral']}")
    return stats


def get_sentiment_summary(account_id: int = 0) -> dict:
    """获取情感分析汇总统计。"""
    return db.sentiment_summary(account_id)
