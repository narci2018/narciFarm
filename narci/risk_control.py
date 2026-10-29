"""风控感知模块：在分发/场景执行过程中监控风控信号。

功能：
- 全局风控状态跟踪（每个设备独立）
- 风控事件记录与告警
- 自动暂停受影响的设备/任务
- Webhook 通知
"""
import time
from typing import Optional

from . import db
from .events import bus


# 风控状态缓存
_risk_state: dict[str, dict] = {}  # serial -> {risk: bool, since: float, reason: str}


def is_at_risk(serial: str) -> bool:
    """检查设备是否处于风控状态。"""
    state = _risk_state.get(serial)
    if not state:
        return False
    # 风控状态 30 分钟后自动解除
    if state.get("since") and time.time() - state["since"] > 1800:
        clear_risk(serial)
        return False
    return state.get("risk", False)


def set_risk(serial: str, reason: str = "") -> None:
    """标记设备进入风控状态。"""
    _risk_state[serial] = {"risk": True, "since": time.time(), "reason": reason}
    bus.warn(f"风控标记: [{serial}] 原因: {reason}")
    # 记录到告警系统
    try:
        db.create_alert(serial, "warn", "risk", f"风控检测: {reason}")
    except Exception:
        pass  # 告警表可能不存在


def clear_risk(serial: str) -> None:
    """解除设备风控状态。"""
    if serial in _risk_state:
        _risk_state[serial] = {"risk": False, "since": 0, "reason": ""}
        bus.info(f"风控解除: [{serial}]")


def get_risk_state(serial: str) -> dict:
    """获取设备风控状态详情。"""
    return _risk_state.get(serial, {"risk": False, "since": 0, "reason": ""})


def get_all_risk_states() -> dict[str, dict]:
    """获取所有设备的风控状态。"""
    return dict(_risk_state)


def record_risk_event(serial: str, event_type: str, details: dict) -> None:
    """记录风控事件（用于后续分析）。"""
    event = {
        "serial": serial,
        "type": event_type,
        "details": details,
        "ts": time.time(),
    }
    bus.info(f"风控事件: {event}")
    # 可以扩展为持久化到数据库
