"""今日用量持久化 —— 修「大退后今日消耗清零」。

原来的问题
----------
侧栏的「今日模型 ¥0.00」是**前端**从后端的 ``call_log`` 现算的，
而 ``call_log`` 只在内存里（还只保留最近 200 条）。所以：
  · 大退一次 → 归零；
  · 聊得久了 → 超出 200 条的老调用被丢掉，**金额会悄悄少算**。

设计取舍
--------
**只持久化「原始计数」，不持久化「金额」。**

因为计价公式在前端（``PRICE_CNY_PER_MTOK`` + 字符数换算 + 输入按 3 倍估的经验值）。
如果后端也算一遍金额，就会出现**两套价格表**，改一处忘一处必然对不上。

所以这里存的是前端算钱所需的最小输入：按 **provider** 聚合的
``chars / reasoning_chars / calls``。前端拿它当基线，
再加上本次启动后的增量 —— 公式仍然只有一份。

按天分桶，只保留最近 60 天（免得文件无限长）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

KEEP_DAYS = 60


def _usage_file() -> Path:
    from .config import user_data_dir

    return user_data_dir() / "usage.json"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _empty_day() -> dict:
    return {"providers": {}, "members": {}, "calls": 0, "failed": 0}


def _load() -> dict:
    path = _usage_file()
    if not path.is_file():
        return {"version": 1, "days": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "days" not in data:
            return {"version": 1, "days": {}}
        return data
    except Exception:  # noqa: BLE001 —— 文件坏了不该拦住启动
        return {"version": 1, "days": {}}


def _save(data: dict) -> None:
    path = _usage_file()
    days = data.get("days") or {}
    # 只留最近 KEEP_DAYS 天（按日期字符串排序即可，YYYY-MM-DD 天然有序）
    if len(days) > KEEP_DAYS:
        for key in sorted(days)[: len(days) - KEEP_DAYS]:
            days.pop(key, None)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def record_call(provider: str, *, agent_id: str = "", chars: int = 0,
                reasoning_chars: int = 0, ok: bool = True) -> None:
    """记一次调用。**只有成功的调用计费**（和前端 estimateCostByProvider 口径一致）。"""
    prov = (provider or "").strip()
    if not prov:
        return
    data = _load()
    day_key = _today()
    day = data["days"].setdefault(day_key, _empty_day())
    day.setdefault("providers", {})
    day.setdefault("members", {})
    day["calls"] = int(day.get("calls") or 0) + 1
    if not ok:
        day["failed"] = int(day.get("failed") or 0) + 1
        _save(data)
        return
    slot = day["providers"].setdefault(prov, {"chars": 0, "reasoning_chars": 0, "calls": 0})
    slot["chars"] = int(slot.get("chars") or 0) + max(0, int(chars))
    slot["reasoning_chars"] = int(slot.get("reasoning_chars") or 0) + max(0, int(reasoning_chars))
    slot["calls"] = int(slot.get("calls") or 0) + 1
    if agent_id:
        m = day["members"].setdefault(agent_id, {"chars": 0, "reasoning_chars": 0, "calls": 0})
        m["chars"] = int(m.get("chars") or 0) + max(0, int(chars))
        m["reasoning_chars"] = int(m.get("reasoning_chars") or 0) + max(0, int(reasoning_chars))
        m["calls"] = int(m.get("calls") or 0) + 1
    _save(data)


def today() -> dict:
    """今日已累计的原始计数。前端拿它当基线算钱。"""
    data = _load()
    day_key = _today()
    day = (data.get("days") or {}).get(day_key) or _empty_day()
    return {
        "date": day_key,
        "providers": day.get("providers") or {},
        "members": day.get("members") or {},
        "calls": int(day.get("calls") or 0),
        "failed": int(day.get("failed") or 0),
        "days_kept": len(data.get("days") or {}),
    }


def clear_today() -> None:
    """清掉今日用量（「清空聊天记录」时一并重置，保持语义一致）。"""
    data = _load()
    data["days"][_today()] = _empty_day()
    _save(data)
