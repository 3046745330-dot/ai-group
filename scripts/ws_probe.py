"""独立端到端验证（checker 用，不属于实现）：真实进程边界上打 HTTP + WebSocket。

用法（在 ai-group 目录下，服务已用 uvicorn 起在 127.0.0.1:8801）::

    ..\\campus-market\\backend\\.venv\\Scripts\\python.exe scripts\\ws_probe.py --port 8801

只发请求、只观察事件，不修改任何实现文件。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request

from websockets.asyncio.client import connect

# 控制台可能是 GBK（cp936）：不重配 stdout 时，结尾的 ✅ 会抛 UnicodeEncodeError，
# 让「8/8 全 PASS」以退出码 1 结束（builder 已实测到这一误导现象）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

RESULTS: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} —— {detail}")


def http_get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.status, resp.read()


def http_post(url: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


async def collect_run(ws, timeout: float = 90.0) -> list[dict]:
    """从 run_start 收到 run_end。"""
    events: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
        ev = json.loads(raw)
        events.append(ev)
        if ev.get("kind") == "run_end":
            break
    return events


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8801)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    ws_url = f"ws://127.0.0.1:{args.port}/ws/group"
    print("=" * 78)
    print(f"独立端到端验证：HTTP + WebSocket @ {base}")
    print("=" * 78)

    # 1. health
    t0 = time.time()
    status, raw = http_get(f"{base}/api/health")
    health = json.loads(raw)
    report(
        "GET /api/health 返回 200 且含 providers/members",
        status == 200 and "providers" in health and "members" in health,
        f"HTTP {status}，{time.time()-t0:.2f}s，providers={[p['provider'] for p in health['providers']]}，"
        f"members={len(health['members'])} 个，mock_only={health.get('mock_only')}",
    )

    # 2. index page
    status, raw = http_get(f"{base}/")
    html = raw.decode("utf-8", errors="replace")
    ui_hits = [k for k in ("ws/group", "群", "chat", "message") if k.lower() in html.lower()]
    report(
        "GET / 返回群聊页面 HTML",
        status == 200 and len(html) > 500 and len(ui_hits) >= 2,
        f"HTTP {status}，{len(html)} 字节，命中关键元素 {ui_hits}",
    )

    async with connect(ws_url, max_size=4 * 1024 * 1024) as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        report(
            "WS /ws/group 首帧 kind=hello 且带 members 快照",
            first.get("kind") == "hello" and "members" in (first.get("snapshot") or {}),
            f"kind={first.get('kind')}，members={[m['id'] for m in first['snapshot']['members']]}",
        )

        # 3. 正常协作一轮（mock 通道，零额度）
        http_post(f"{base}/api/reset", {})
        while True:  # 排空 reset 广播
            try:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
            except (asyncio.TimeoutError, TimeoutError):
                break
            if ev.get("kind") == "cleared":
                break
        t0 = time.time()
        status, body = http_post(
            f"{base}/api/chat", {"text": "群里都有谁？请分工后给出结论", "sender": "验证员"}
        )
        events = await collect_run(ws)
        kinds = [e["kind"] for e in events]
        ends = [e for e in events if e["kind"] == "message_end"]
        senders = [e["message"]["sender"] for e in ends if "message" in e]
        recorder = [s for s in senders if s == "recorder"]
        report(
            "POST /api/chat 后 WS 收到完整事件流（run_start→…→run_end）",
            status == 200 and "run_start" in kinds and "run_end" in kinds and "message_delta" in kinds,
            f"HTTP {status}，{len(events)} 个事件，{time.time()-t0:.2f}s，kinds={sorted(set(kinds))}",
        )
        report(
            "message_end 覆盖 captain + 至少一个组员 + recorder 纪要",
            "captain" in senders and len([s for s in senders if s not in ("captain", "recorder")]) >= 1
            and bool(recorder),
            f"senders={senders}",
        )
        routes = [
            (e["message"]["sender"], e["message"]["route"], e["message"]["requested_route"])
            for e in ends if "message" in e
        ]
        report(
            "每条发言都带实际 route / requested_route（降级可追踪）",
            all(r and q for _, r, q in routes),
            f"{routes}",
        )

        # 3b. 额度护栏：逐成员计数 ≤ max_turns_per_run，派发链总数 ≤ max_messages
        member_limits = {m["id"]: m["max_turns_per_run"] for m in first["snapshot"]["members"]}
        limits = first["snapshot"].get("limits", {})
        max_messages = limits.get("max_messages")
        dispatch_senders = [s for s in senders if s != "recorder"]
        counts: dict[str, int] = {}
        for s in dispatch_senders:
            counts[s] = counts.get(s, 0) + 1
        over = [
            f"{k}:{v}>{member_limits[k]}"
            for k, v in counts.items()
            if k in member_limits and v > member_limits[k]
        ]
        report(
            "额度护栏：逐成员发言数 ≤ max_turns_per_run，派发链总数 ≤ max_messages",
            not over and isinstance(max_messages, int) and len(dispatch_senders) <= max_messages,
            f"计数={counts}；limits={limits}；派发链 {len(dispatch_senders)} 条 ≤ max_messages={max_messages}；"
            f"recorder {senders.count('recorder')} 条（不占派发链配额）",
        )

        # 4. 无人被 @ 时不唤醒组员
        http_post(f"{base}/api/reset", {})
        await asyncio.sleep(0.2)
        http_post(f"{base}/api/chat", {"text": "只回答我是谁", "mock_hint": "quiet"})
        ev2 = await collect_run(ws)
        ends2 = [e for e in ev2 if e["kind"] == "message_end"]
        senders2 = [e["message"]["sender"] for e in ends2 if "message" in e]
        report(
            "无人被 @ 时不唤醒任何组员",
            "captain" in senders2 and not [s for s in senders2 if s == "demo-bot"],
            f"senders={senders2}",
        )

        # 5. 未知 @目标 只产生 notice
        http_post(f"{base}/api/reset", {})
        await asyncio.sleep(0.2)
        http_post(f"{base}/api/chat", {"text": "再派一次活", "mock_hint": "ghost"})
        ev3 = await collect_run(ws)
        notices = [e for e in ev3 if e["kind"] == "notice" and "不存在的成员" in e.get("message", "")]
        errors = [e for e in ev3 if e["kind"] == "error"]
        report(
            "不存在的 @ 目标只产生 notice，不报错",
            bool(notices) and not errors,
            f"notice={notices[0]['message'][:50] if notices else '无'}，errors={len(errors)}",
        )

    print("\n" + "=" * 78)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"结果：{len(RESULTS)-len(failed)}/{len(RESULTS)} 通过" + (f"，未通过：{failed}" if failed else "，全部通过"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
