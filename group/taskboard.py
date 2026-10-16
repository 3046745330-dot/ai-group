"""任务板 —— 让「做到哪了」跨轮次、跨重启地留下来。

它解决什么
----------
组长验收只管**一轮**。你关掉 app、第二天回来说"继续"，上一轮做到哪了就
只剩聊天记录 —— 没有"任务"这个东西，也就没有"还没做完"这个状态。

有了任务板：

    第一轮  组长拆解 → 建三条任务（前端/后端/说明），分别派人
            成员干活，产出文件
            组长验收：前两个 done，第三个还没动 → 更新状态

    （关掉 app，第二天重开）

    第二轮  组长一进场就看到任务板：第三条还是 todo
            → 直接接着干，不用你从头再说一遍

谁写谁读
--------
**只有组长写**（派活和验收是它的职责），**所有人读**（提示词里带着板子）。
这样不会出现"组员互相改任务状态"的混乱。

状态取值
--------
``todo`` 待办 / ``doing`` 进行中 / ``done`` 已完成 / ``blocked`` 卡住了

用 ``set_task`` 按**标题**做 upsert：标题一样就是更新，不会重复建。
组长不需要记 id —— 它只要说"把『前端页面』标成 done"就行。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

STATUSES = ("todo", "doing", "done", "blocked")
STATUS_CN = {"todo": "待办", "doing": "进行中", "done": "已完成", "blocked": "卡住"}
# 板子上最多留多少条。太长的板子既占提示词，也没人看。
MAX_TASKS = 60


def _file() -> Path:
    from .config import user_data_dir

    return user_data_dir() / "tasks.json"


def _load() -> dict:
    path = _file()
    if not path.is_file():
        return {"version": 1, "tasks": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            return {"version": 1, "tasks": []}
        return data
    except Exception:  # noqa: BLE001 —— 文件坏了不该拦住启动
        return {"version": 1, "tasks": []}


def _save(data: dict) -> None:
    path = _file()
    tasks = data.get("tasks") or []
    # 超量时先丢最老的 done，尽量保留未完成的
    if len(tasks) > MAX_TASKS:
        done = [t for t in tasks if t.get("status") == "done"]
        rest = [t for t in tasks if t.get("status") != "done"]
        keep = max(0, MAX_TASKS - len(rest))
        data["tasks"] = (done[-keep:] if keep else []) + rest
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _norm(title: str) -> str:
    """标题归一化：忽略首尾空白和大小写，避免"前端页面"和"前端页面 "变成两条。"""
    return " ".join(str(title or "").split()).strip().lower()


def set_task(title: str, owner: str = "", status: str = "todo", note: str = "") -> dict:
    """按标题 upsert 一条任务，返回这条任务。"""
    title = " ".join(str(title or "").split()).strip()
    if not title:
        raise ValueError("任务标题不能为空")
    if len(title) > 120:
        title = title[:120]
    status = (status or "todo").strip().lower()
    if status not in STATUSES:
        raise ValueError(f"状态只能是 {STATUSES} 之一，收到 {status!r}")
    note = " ".join(str(note or "").split()).strip()[:300]

    data = _load()
    tasks = data.setdefault("tasks", [])
    key = _norm(title)
    now = time.time()
    for t in tasks:
        if _norm(t.get("title")) == key:
            if owner:
                t["owner"] = owner
            t["status"] = status
            if note:
                t["note"] = note
            t["updated"] = now
            _save(data)
            return dict(t, changed=True)
    item = {
        "id": f"t{int(now) % 100000}{len(tasks)}",
        "title": title,
        "owner": owner or "",
        "status": status,
        "note": note,
        "created": now,
        "updated": now,
    }
    tasks.append(item)
    _save(data)
    return dict(item, changed=True)


def all_tasks() -> list[dict]:
    return list(_load().get("tasks") or [])


def digest(limit: int = 40) -> str:
    """给模型看的一行式清单。**未完成的排前面** —— 那才是要继续干的。"""
    tasks = all_tasks()
    if not tasks:
        return "（还没有任务。你要派活的话，可以先用 set_task 把任务登记下来。）"
    order = {"doing": 0, "todo": 1, "blocked": 2, "done": 3}
    tasks = sorted(tasks, key=lambda t: (order.get(t.get("status"), 9), -float(t.get("updated") or 0)))
    lines: list[str] = []
    for t in tasks[:limit]:
        st = STATUS_CN.get(t.get("status"), t.get("status") or "?")
        owner = f" @{t['owner']}" if t.get("owner") else ""
        note = f"｜{t['note']}" if t.get("note") else ""
        lines.append(f"- [{st}] {t.get('title')}{owner}{note}")
    if len(tasks) > limit:
        lines.append(f"- …还有 {len(tasks) - limit} 条")
    return "\n".join(lines)


def clear() -> None:
    _save({"version": 1, "tasks": []})


def counts() -> dict:
    out = {k: 0 for k in STATUSES}
    for t in all_tasks():
        s = t.get("status")
        if s in out:
            out[s] += 1
    out["total"] = sum(out.values())
    return out


def tool_schemas() -> list[dict]:
    """给**组长**声明的任务工具。

    只给组长：派活和验收是它的职责。组员能读板子（提示词里带着），
    但不该改 —— 否则会出现"组员互相改状态"的混乱。
    """
    return [{
        "type": "function",
        "function": {
            "name": "set_task",
            "description": (
                "登记或更新一条任务。派活前先把任务登记下来，"
                "验收后把状态改成 done（或 blocked 并说明卡在哪）。"
                "**按标题匹配**：标题一样就是更新那条，不会重复建。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "任务标题，例如「后端接口」"},
                    "owner": {"type": "string", "description": "负责人的成员 id"},
                    "status": {
                        "type": "string",
                        "enum": ["todo", "doing", "done", "blocked"],
                        "description": "todo 待办 / doing 进行中 / done 已完成 / blocked 卡住",
                    },
                    "note": {"type": "string", "description": "备注：产出在哪、卡在什么地方"},
                },
                "required": ["title"],
            },
        },
    }]
