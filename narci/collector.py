"""竞品内容采集引擎：定时抓取同领域热门内容作为素材。

采集源通过 competitor_sources 表配置，支持：
- RSS 订阅（通用）
- 网页抓取（通过 CSS 选择器 / 正则提取）
- API 对接（第三方数据服务）

采集结果存入 competitor_contents 表，可作为内容创作参考。
"""
import asyncio
import hashlib
import json
import re
import time
from typing import Optional

import httpx

from . import db
from .events import bus

COLLECT_INTERVAL = 3600  # 默认每小时采集一次
USER_AGENT = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0"


async def collect_source(source: dict) -> list[dict]:
    """对单个采集源执行采集，返回新发现的内容列表。"""
    source_type = source.get("source_type", "rss")
    results = []

    try:
        if source_type == "rss":
            results = await _collect_rss(source)
        elif source_type == "web":
            results = await _collect_web(source)
        elif source_type == "api":
            results = await _collect_api(source)
        else:
            bus.warn(f"未知采集源类型: {source_type}")
            return []
    except Exception as e:
        bus.error(f"采集源[{source.get('name', '?')}] 异常: {type(e).__name__}: {e}")
        db.update_competitor_source(source["id"], last_error=str(e))
        return []

    new_items = []
    for item in results:
        content_hash = hashlib.md5(
            (item.get("title", "") + item.get("url", "")).encode()
        ).hexdigest()
        existing = db.get_competitor_content_by_hash(content_hash)
        if existing:
            continue
        record = db.create_competitor_content(
            source_id=source["id"],
            platform=source.get("platform", ""),
            title=item.get("title", ""),
            content=item.get("content", ""),
            url=item.get("url", ""),
            author=item.get("author", ""),
            metrics=json.dumps(item.get("metrics", {})),
            content_hash=content_hash,
            tags=json.dumps(item.get("tags", []), ensure_ascii=False),
        )
        if record:
            new_items.append(record)

    # 从 DB 获取最新的 total_collected（避免使用可能已过期的内存值）
    fresh_source = db.get_competitor_source(source["id"])
    current_total = (fresh_source.get("total_collected", 0) or 0) if fresh_source else 0
    db.update_competitor_source(
        source["id"],
        last_collect=time.time(),
        total_collected=current_total + len(new_items),
        last_error="",
    )
    if new_items:
        bus.info(f"采集源[{source.get('name')}] 新增 {len(new_items)} 条内容")
    return new_items


async def _collect_rss(source: dict) -> list[dict]:
    """RSS 采集：解析 XML feed。"""
    url = source.get("url", "")
    if not url:
        return []
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        r = await client.get(url)
        r.raise_for_status()
        text = r.text

    items = []
    # 简易 XML 解析（避免引入 lxml 依赖）
    item_blocks = re.findall(r"<item[^>]*>(.*?)</item>", text, re.DOTALL)
    if not item_blocks:
        item_blocks = re.findall(r"<entry[^>]*>(.*?)</entry>", text, re.DOTALL)

    for block in item_blocks[:20]:  # 最多取 20 条
        title = _extract_tag(block, "title")
        link = _extract_tag(block, "link") or _extract_tag_attr(block, "link", "href")
        desc = _extract_tag(block, "description") or _extract_tag(block, "summary") or _extract_tag(block, "content")
        author = _extract_tag(block, "author") or _extract_tag(block, "dc:creator")
        # 清理 HTML 标签
        desc = re.sub(r"<[^>]+>", "", desc or "")[:500]
        items.append({
            "title": title or "",
            "url": link or "",
            "content": desc or "",
            "author": author or "",
            "tags": [],
            "metrics": {},
        })
    return items


async def _collect_web(source: dict) -> list[dict]:
    """网页采集：通过 HTTP 获取页面，用正则提取内容。"""
    url = source.get("url", "")
    config = {}
    try:
        config = json.loads(source.get("config", "{}") or "{}")
    except json.JSONDecodeError:
        config = {}

    if not url:
        return []

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        r = await client.get(url)
        r.raise_for_status()
        html = r.text

    items = []

    # 从配置中提取匹配模式
    item_pattern = config.get("item_pattern", r'<article[^>]*>(.*?)</article>')
    title_pattern = config.get("title_pattern", r'<h[1-4][^>]*>(.*?)</h[1-4]>')
    link_pattern = config.get("link_pattern", r'href="(https?://[^"]+)"')

    blocks = re.findall(item_pattern, html, re.DOTALL)
    for block in blocks[:20]:
        title_m = re.search(title_pattern, block, re.DOTALL)
        link_m = re.search(link_pattern, block)
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
        link = link_m.group(1) if link_m else ""
        if title:
            items.append({
                "title": title,
                "url": link,
                "content": re.sub(r"<[^>]+>", "", block)[:500],
                "author": "",
                "tags": [],
                "metrics": {},
            })
    return items


async def _collect_api(source: dict) -> list[dict]:
    """API 采集：调用第三方数据接口。"""
    url = source.get("url", "")
    config = {}
    try:
        config = json.loads(source.get("config", "{}") or "{}")
    except json.JSONDecodeError:
        config = {}

    if not url:
        return []

    headers = {"User-Agent": USER_AGENT}
    api_key = source.get("api_key", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        params = config.get("params", {})
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    items = []
    data_key = config.get("data_key", "data")
    if isinstance(data, dict):
        records = data.get(data_key, [])
    else:
        records = []
    if isinstance(records, list):
        for rec in records[:20]:
            items.append({
                "title": rec.get(config.get("title_key", "title"), ""),
                "url": rec.get(config.get("url_key", "url"), ""),
                "content": rec.get(config.get("content_key", "content"), "")[:500],
                "author": rec.get(config.get("author_key", "author"), ""),
                "tags": rec.get(config.get("tags_key", "tags"), []),
                "metrics": {
                    "likes": rec.get("likes", 0),
                    "comments": rec.get("comments", 0),
                    "shares": rec.get("shares", 0),
                },
            })
    return items


def _extract_tag(text: str, tag: str) -> str:
    """从 XML 文本中提取标签内容。"""
    m = re.search(f"<{tag}[^>]*>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_tag_attr(text: str, tag: str, attr: str) -> str:
    """从 XML 文本中提取标签属性。"""
    m = re.search(f'<{tag}[^>]*{attr}="([^"]*)"', text)
    return m.group(1).strip() if m else ""


async def run_collection_cycle() -> int:
    """执行一轮完整采集，返回新增内容总数。"""
    sources = db.list_competitor_sources(enabled_only=True)
    total = 0
    for source in sources:
        now = time.time()
        interval = float(source.get("collect_interval", COLLECT_INTERVAL))
        last = float(source.get("last_collect", 0) or 0)
        if now - last < interval:
            continue
        items = await collect_source(source)
        total += len(items)
    return total


async def collection_loop() -> None:
    """后台采集循环。"""
    bus.info("竞品采集器已启动")
    while True:
        try:
            await run_collection_cycle()
        except asyncio.CancelledError:
            bus.info("竞品采集器停止")
            return
        except Exception as e:
            bus.error(f"竞品采集异常: {type(e).__name__}: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


CHECK_INTERVAL = 300  # 每 5 分钟检查一次是否有到期需要采集的源
