"""t5 运行时取证：对**真实启动的 ai-group 服务**投喂一句需要协作的问题，抓取 WS 事件流。

用法（在 ai-group 目录下，服务已在 8801 端口跑着真实配置）::

    .\\.venv\\Scripts\\python.exe docs\\capture_runtime_evidence.py --port 8801

它只发请求、只读事件，不修改任何实现文件。输出：
  * 终端：人类可读的事实摘要（发言顺序 / 实际生效路由 / 降级 / 纪要长度 / notice / error）
  * 文件：docs/evidence/runtime-<port>-events.json（原始事件）与 runtime-<port>-summary.txt（摘要）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = PROJECT_ROOT / "docs" / "evidence"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover
        pass


async def main() -> int:
    import httpx
    import websockets

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument(
        "--question",
        default=(
            "用一句话回答：多模型群聊相对「单模型多次调用」的不可替代价值是什么？"
            "请组长拆解并 @kimi 用一句话给出论证、@doubao 用一句话给出反驳，然后收敛。"
        ),
    )
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    ws_url = f"ws://127.0.0.1:{args.port}/ws/group"
    lines: list[str] = []

    def say(text: str) -> None:
        print(text)
        lines.append(text)

    say("=" * 78)
    say(f"t5 运行时取证：真实服务 @ {base}")
    say(f"问题：{args.question}")
    say("=" * 78)

    async with httpx.AsyncClient(timeout=20.0) as client:
        health = (await client.get(f"{base}/api/health")).json()
    say(f"GET /api/health → 200，mock_only={health.get('mock_only')}")
    for p in health.get("providers", []):
        say(f"    provider {p['provider']:<16} ok={str(p['ok']):<5} {p['detail'][:60]}")
    for m in health.get("members", []):
        say(f"    member   {m['id']:<10} enabled={str(m['enabled']):<5} route={m['route']:<42} fallback={m.get('fallback_route') or '无'}")

    events: list[dict] = []
    async with websockets.connect(ws_url, max_size=None) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        events.append(hello)
        snapshot = hello.get("snapshot", {})
        say(f"\nWS 首帧 kind={hello.get('kind')}，members={[m['id'] for m in snapshot.get('members', [])]}，"
            f"limits={snapshot.get('limits')}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(f"{base}/api/chat", json={"text": args.question, "sender": "用户"})
            say(f"POST /api/chat → HTTP {res.status_code} {json.dumps(res.json(), ensure_ascii=False)}")

        started = time.time()
        deadline = started + args.timeout
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.time()))
            except asyncio.TimeoutError:
                say(f"\n[warning] {args.timeout:.0f}s 内没有等到 run_end，采集到此为止")
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("kind") == "run_end":
                break
        elapsed = time.time() - started

    say(f"\n事件总数 {len(events)}（含 hello），一轮耗时 {elapsed:.1f}s")
    kinds: list[str] = []
    for ev in events:
        if ev["kind"] not in kinds:
            kinds.append(ev["kind"])
    say(f"出现的事件种类：{kinds}")

    say("\n--- 发言顺序与实际生效路由（来自 message_end）---")
    order: list[str] = []
    for ev in events:
        if ev.get("kind") == "message_end" and ev.get("message"):
            m = ev["message"]
            order.append(m["sender"])
            flag = "（已降级）" if m["warning"] else ""
            err = f" ERROR={m['error'][:80]}" if m.get("error") else ""
            say(f"    {m['sender']:<10} role={m['role']:<9} 声明={m['requested_route']:<42} "
                f"实际={m['route']:<42} {len(m['content'])}字{flag}{err}")

    say("\n--- route_summary 事件（同源/降级/失败）---")
    for ev in events:
        if ev.get("kind") == "route_summary":
            say(f"    providers={ev.get('providers')} degraded={ev.get('degraded_count')} "
                f"shared={ev.get('shared_providers')} failed={[f['name'] for f in ev.get('failed', [])]}")
            say(f"    text={ev.get('message')}")

    say("\n--- notice / error ---")
    for ev in events:
        if ev.get("kind") in ("notice", "error", "route_changed", "context_trimmed"):
            detail = ev.get("message") or f"{ev.get('requested_route')} → {ev.get('route')}"
            say(f"    [{ev['kind']}/{ev.get('level', '-')}] {str(detail)[:160]}")

    say("\n--- 记录员纪要 ---")
    for ev in events:
        if ev.get("kind") == "message_end" and ev.get("message", {}).get("role") == "recorder":
            content = ev["message"]["content"]
            say(f"    实际路由 {ev['message']['route']}，{len(content)} 字")
            say("    " + content[:600].replace("\n", "\n    "))

    async with httpx.AsyncClient(timeout=20.0) as client:
        diag = (await client.get(f"{base}/api/diagnostics")).json()
    say("\n--- 实际调用记录 /api/diagnostics ---")
    for e in diag.get("call_log", []):
        say(f"    {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['route']:<40} "
            f"host={e['host']:<32} ok={str(e['ok']):<5} chars={e['chars']:<5} "
            f"{e['elapsed_s']}s finish={e.get('finish_reason') or '-'} {e['error'][:60]}")

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / f"runtime-{args.port}-events.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (EVIDENCE / f"runtime-{args.port}-summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"\n已写入 docs/evidence/runtime-{args.port}-events.json 与 runtime-{args.port}-summary.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
