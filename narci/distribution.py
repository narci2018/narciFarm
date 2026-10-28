"""内容分发引擎: 发布器抽象 + 调度执行。

发布器(PlatformPublisher):
- WebhookPublisher: 账号配置了 api_base(自己的网关/官方API代理)时, POST 内容过去
- ManualPublisher: 无 API 时标记为手动发布(用于记录/人工补发)
- 可扩展: 官方 API 适配器(抖音开放平台等)实现 PlatformPublisher 接口注册即可
"""
import asyncio
import time
from typing import Optional

import httpx

from . import db
from .events import bus


class PlatformPublisher:
    name = "base"

    async def publish(self, content: dict, account: dict) -> dict:
        raise NotImplementedError


class WebhookPublisher(PlatformPublisher):
    """通过账号自带的 api_base 发布(官方 API 网关/自建服务)。"""
    name = "webhook"

    async def publish(self, content: dict, account: dict) -> dict:
        base = (account.get("api_base") or "").rstrip("/")
        if not base:
            return {"ok": False, "error": "账号未配置 api_base"}
        payload = {
            "title": content.get("title", ""),
            "type": content.get("type", "text"),
            "text": content.get("text", ""),
            "media_path": content.get("media_path", ""),
            "token": account.get("api_token", ""),
            "account_name": account.get("name", ""),
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{base}/publish", json=payload)
                if r.status_code != 200:
                    return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
                data = r.json()
                if not data.get("ok"):
                    return {"ok": False, "error": data.get("error", "发布失败")}
                return {"ok": True, "platform_post_id": str(data.get("post_id", ""))}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class ManualPublisher(PlatformPublisher):
    """无 API 的账号: 标记为待人工发布。"""
    name = "manual"

    async def publish(self, content: dict, account: dict) -> dict:
        return {"ok": True, "platform_post_id": "", "manual": True}


def get_publisher(account: dict) -> PlatformPublisher:
    if account.get("api_base"):
        return WebhookPublisher()
    return ManualPublisher()


async def run_due_tasks() -> int:
    """执行到期的分发任务, 返回执行数量。"""
    tasks = db.due_distribution_tasks()
    for task in tasks:
        content = db.get_content(task["content_id"]) if hasattr(db, "get_content") else None
        if content is None:
            rows = db.query("SELECT * FROM contents WHERE id=?", (task["content_id"],))
            content = rows[0] if rows else None
        account = db.get_account(task["account_id"])
        if not content or not account:
            db.update_distribution_task(task["id"], status="failed", error="内容或账号不存在")
            continue
        publisher = get_publisher(account)
        try:
            result = await publisher.publish(content, account)
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        if result.get("ok"):
            db.update_distribution_task(
                task["id"], status="published", published_at=time.time(),
                platform_post_id=result.get("platform_post_id", ""))
            bus.info(f"分发成功: [{content['title']}] → {account['name']} ({publisher.name})")
        else:
            db.update_distribution_task(task["id"], status="failed", error=result.get("error", ""))
            bus.warn(f"分发失败: [{content['title']}] → {account['name']}: {result.get('error')}")
    return len(tasks)
