"""调度器：interval（跑完等 N 秒再跑，即 Evelin 的 N-th-run 语义）与 daily（HH:MM）两种模式。"""
import asyncio
import json
import time
from typing import Optional

from . import adb, db, scenario
from .events import bus

CHECK_INTERVAL = 15


def next_run_for(kind: str, value: str, now: Optional[float] = None) -> float:
    now = now or time.time()
    if kind == "interval":
        return now + max(int(value), 5)
    if kind == "daily":
        hh, mm = (list(map(int, value.split(":"))) + [0])[:2]
        import datetime
        d = datetime.datetime.now()
        target = d.replace(hour=hh % 24, minute=mm % 60, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += datetime.timedelta(days=1)
        return target.timestamp()
    if kind == "cron":
        return _next_cron(value, now)
    raise ValueError(f"未知调度类型: {kind}")


def _parse_cron_field(field: str, lo: int, hi: int) -> list[int]:
    """解析单个 cron 字段: 支持 * / *\/n / a-b / a,b / 数字。"""
    values = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            step = max(int(step_s), 1)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            start, _, end_s = part.partition("-")
            start, end = int(start), int(end_s)
        else:
            start = end = int(part)
        values.update(range(max(start, lo), min(end, hi) + 1, step))
    return sorted(values)


def _next_cron(expr: str, now: float) -> float:
    """5 段 cron(分 时 日 月 周), 返回下次触发时间戳。周字段 0/7 均视为周日。"""
    import datetime
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron 需要 5 段: 分 时 日 月 周")
    minutes = _parse_cron_field(parts[0], 0, 59)
    hours = _parse_cron_field(parts[1], 0, 23)
    doms = _parse_cron_field(parts[2], 1, 31)
    months = _parse_cron_field(parts[3], 1, 12)
    dows = {0 if v == 7 else v for v in _parse_cron_field(parts[4], 0, 7)}

    d = datetime.datetime.fromtimestamp(now) + datetime.timedelta(minutes=1)
    d = d.replace(second=0, microsecond=0)
    for _ in range(366 * 24 * 60):  # 最多找一年
        if d.month in months and d.minute in minutes and d.hour in hours:
            dom_ok = d.day in doms
            dow = (d.weekday() + 1) % 7  # 转 0=周日
            dow_ok = dow in dows
            # 标准 cron: 日与周同时指定时取并集(任一匹配)
            if (dom_ok or dow_ok) if (parts[2] != "*" and parts[4] != "*") else (dom_ok and dow_ok):
                return d.timestamp()
        d += datetime.timedelta(minutes=1)
    raise ValueError("cron 表达式一年内无触发时间")


def _targets_of(schedule: dict) -> list[str]:
    target = json.loads(schedule.get("target") or "[]")
    if target.get("group_id"):
        for g in db.list_groups():
            if g["id"] == target["group_id"]:
                return list(g["members"])
    return list(target.get("serials", []))


async def _execute_schedule(schedule: dict) -> None:
    sc = db.get_scenario(schedule["scenario_id"])
    if not sc:
        bus.warn(f"调度[{schedule['name']}] 关联场景不存在 id={schedule['scenario_id']}")
        return
    serials = _targets_of(schedule)
    if not serials:
        bus.warn(f"调度[{schedule['name']}] 目标设备为空")
        return
    # 过滤掉已离线设备
    online = {d["serial"] for d in await adb.list_devices() if d["state"] == "device"}
    serials = [s for s in serials if s in online]
    if not serials:
        bus.warn(f"调度[{schedule['name']}] 目标设备均不在线")
        return
    steps = json.loads(sc["steps"])
    repeat = int(sc.get("repeat_count") or 1)
    run_id = await scenario.start_run(steps, repeat, serials)
    bus.info(f"调度[{schedule['name']}] 触发场景[{sc['name']}] run={run_id[:8]} 设备={len(serials)}")


async def scheduler_loop() -> None:
    bus.info("调度器已启动")
    while True:
        try:
            now = time.time()
            for schedule in db.due_schedules(now):
                kind, value = schedule["kind"], schedule["value"]
                try:
                    nxt = next_run_for(kind, value, now)
                except ValueError:
                    db.update_schedule(schedule["id"], enabled=0)
                    continue
                db.update_schedule(schedule["id"], last_run=time.strftime("%Y-%m-%d %H:%M:%S"), next_run=nxt)
                asyncio.create_task(_execute_schedule(schedule))
        except asyncio.CancelledError:
            bus.info("调度器停止")
            return
        except Exception as e:
            bus.error(f"调度器异常: {type(e).__name__}: {e}")
        await asyncio.sleep(CHECK_INTERVAL)
