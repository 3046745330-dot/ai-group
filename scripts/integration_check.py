"""真实链路联调（非 mock）：起真正的 uvicorn 服务 + 真正的多厂商模型，驱动一轮群聊。

这是「服务端能启动并对外服务」与「群聊逻辑真实可用」两条验收的可复现证据脚本。

用法（在 ai-group 目录下）::

    ..\\campus-market\\backend\\.venv\\Scripts\\python.exe scripts\\integration_check.py
    ..\\campus-market\\backend\\.venv\\Scripts\\python.exe scripts\\integration_check.py --port 8823

它会：
  1. 用真实配置在子进程里起 `uvicorn group.app:app`（**不注入任何密钥**）；
  2. GET /api/health 检查 providers / members，GET / 检查页面 HTML；
  3. WS /ws/group 连接，检查首帧是 hello 且带 members 快照；
  4. POST /api/chat 投喂一句需要协作的问题，通过 WS 观察
     「组长发言 → 被 @ 的组员发言 → 记录员纪要」的完整过程；
  5. 打印每个发言人**实际生效**的路由与降级情况，最后关掉服务。

退出码 0 = 全部通过。任何一项不通过都以非 0 退出，并把失败原因打印出来。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# Windows 控制台是 GBK：非 GBK 字符会让 print 抛异常并把退出码变成 1
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_ROOT.parent / "campus-market" / "backend" / ".venv" / "Scripts" / "python.exe"
USAGE_LEDGER = Path.home() / ".dsh" / ".dshw-usage.json"


def ledger_snapshot() -> dict:
    """DSH 用量账本快照（只读）。用来回答「声明的路由真的被调用了吗」这一个问题。

    注意边界：账本只证明「调用发生过」，不证明产出内容有效；而且它记录的是
    DSH 账号侧的用量，本应用是直连各厂商的，账本**不一定**覆盖（见报告里的说明）。
    """
    if not USAGE_LEDGER.exists():
        return {"available": False, "events": [], "models": {}}
    try:
        data = json.loads(USAGE_LEDGER.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover
        return {"available": False, "events": [], "models": {}, "error": str(exc)}
    events = data.get("events") or []
    models: dict[str, int] = {}
    for event in events:
        models[event.get("model", "?")] = models.get(event.get("model", "?"), 0) + 1
    return {
        "available": True,
        "count": len(events),
        "last_ts": max((e.get("ts", 0) for e in events), default=0),
        "models": models,
    }


def ledger_diff(before: dict, after: dict) -> dict:
    """账本差值：本次运行期间新增的条目（按 ts 过滤）。"""
    if not before.get("available") or not after.get("available"):
        return {"available": False, "new": []}
    started = before.get("last_ts", 0)
    fresh: list[dict] = []
    try:
        data = json.loads(USAGE_LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False, "new": []}
    for event in data.get("events") or []:
        if event.get("ts", 0) > started:
            fresh.append(
                {
                    "model": event.get("model"),
                    "tokens": event.get("tokens"),
                    "cost": event.get("cost"),
                    "at": time.strftime("%H:%M:%S", time.localtime(event.get("ts", 0) / 1000)),
                }
            )
    return {"available": True, "new": fresh}


def free_port(preferred: int) -> int:
    for candidate in [preferred, 0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("找不到可用端口")


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.total = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.total += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)


async def wait_health(base: str, timeout: float) -> dict:
    import httpx

    deadline = time.time() + timeout
    last = ""
    async with httpx.AsyncClient(timeout=10.0) as client:
        while time.time() < deadline:
            try:
                res = await client.get(f"{base}/api/health")
                if res.status_code == 200:
                    return res.json()
                last = f"HTTP {res.status_code}"
            except Exception as exc:  # 服务还没起来
                last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.5)
    raise RuntimeError(f"服务在 {timeout:.0f}s 内没有就绪（最后一次：{last}）")


async def run_round(base: str, question: str, timeout: float) -> list[dict]:
    """连 WS 并投喂问题，收集到 run_end 为止的全部事件。"""
    import httpx
    import websockets

    events: list[dict] = []
    ws_url = base.replace("http://", "ws://") + "/ws/group"
    async with websockets.connect(ws_url, max_size=None) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        events.append(hello)

        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.post(f"{base}/api/chat", json={"text": question, "sender": "用户"})
            events.append({"kind": "_http_chat", "status": res.status_code, "body": res.json()})

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("kind") == "run_end":
                break
    return events


def summarize(events: list[dict]) -> dict:
    speakers: list[dict] = []
    for ev in events:
        if ev.get("kind") == "message_end" and ev.get("message"):
            m = ev["message"]
            speakers.append(
                {
                    "sender": m["sender"],
                    "role": m["role"],
                    "requested_route": m["requested_route"],
                    "route": m["route"],
                    "degraded": bool(m["warning"]),
                    "warning": m["warning"],
                    "chars": len(m["content"]),
                    "preview": m["content"][:220],
                    "content": m["content"],
                }
            )
    return {
        "kinds": [e["kind"] for e in events],
        "speakers": speakers,
        "notices": [
            {"level": e.get("level"), "message": e.get("message")}
            for e in events
            if e.get("kind") == "notice"
        ],
        "errors": [e.get("message") for e in events if e.get("kind") == "error"],
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8821)
    parser.add_argument(
        "--question",
        default=(
            "我们要不要把自研群聊内核换成 AG2 的 GroupChat？请组长拆解这个问题，"
            "并 @ 至少两名组员分别负责：@kimi 负责给出支持自研内核的论证，"
            "@doubao 负责反驳并指出风险，最后由组长收敛。"
        ),
    )
    parser.add_argument("--timeout", type=float, default=420.0, help="等待一轮讨论结束的秒数")
    args = parser.parse_args()

    port = free_port(args.port)
    base = f"http://127.0.0.1:{port}"
    python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

    env = dict(os.environ)
    deepseek_key_present = bool(env.get("DEEPSEEK_API_KEY", "").strip())
    env.pop("DEEPSEEK_API_KEY", None)  # 本次联调刻意不提供 DeepSeek key，用来验证降级
    env.pop("SMOKE_MOCK_ONLY", None)

    c = Checker()
    print("=" * 78)
    print("AI 群聊 · 真实链路联调（真实 uvicorn + 真实多厂商模型）")
    print("=" * 78)
    print(f"    解释器   ：{python}")
    print(f"    服务地址 ：{base}")
    print(f"    DEEPSEEK_API_KEY 环境变量：{'存在（已刻意移除）' if deepseek_key_present else '不存在'}")
    print(f"    问题     ：{args.question[:80]}…")

    log_path = PROJECT_ROOT / "scripts" / "integration_server.log"
    log = log_path.open("w", encoding="utf-8")
    ledger_before = ledger_snapshot()
    proc = subprocess.Popen(
        [python, "-m", "uvicorn", "group.app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    try:
        health = await asyncio.wait_for(wait_health(base, 60.0), timeout=70)
        c.check("服务已启动：GET /api/health 返回 200", True, f"ok={health.get('ok')}")
        c.check(
            "GET /api/health JSON 含 providers 与 members",
            bool(health.get("providers")) and bool(health.get("members")),
            f"{len(health.get('providers', []))} 个 provider / {len(health.get('members', []))} 个成员",
        )
        provider_map = {p["provider"]: p for p in health["providers"]}
        print("\n--- provider 可用性（来自 /api/health）---")
        for p in health["providers"]:
            print(f"    {p['provider']:<16} {'可用' if p['ok'] else '不可用':<4} {p['detail'][:56]}")
        print("\n--- 成员路由（来自 /api/health）---")
        for m in health["members"]:
            print(
                f"    {m['id']:<10} {'启用' if m['enabled'] else '未启用':<4} "
                f"{m['route']:<42} fallback={m.get('fallback_route') or '无'}"
            )
        c.check(
            "provider 探测到 Kimi 与豆包可用（实测凭据生效）",
            provider_map.get("moonshotai-cn", {}).get("ok") is True
            and provider_map.get("doubao", {}).get("ok") is True,
            f"moonshotai-cn={provider_map.get('moonshotai-cn', {}).get('ok')} "
            f"doubao={provider_map.get('doubao', {}).get('ok')}",
        )
        c.check(
            "无 DEEPSEEK_API_KEY 时 deepseek 明确报不可用（不假装可用）",
            provider_map.get("deepseek", {}).get("ok") is False,
            provider_map.get("deepseek", {}).get("detail", "")[:64],
        )

        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            page = await client.get(f"{base}/")
            c.check(
                "GET / 返回群聊页面 HTML",
                page.status_code == 200 and "text/html" in page.headers.get("content-type", "")
                and "多模型协作" in page.text and "/static/app.js" in page.text,
                f"HTTP {page.status_code} / {len(page.text)} 字节",
            )

        events = await run_round(base, args.question, args.timeout)
        hello = events[0]
        c.check(
            "WS /ws/group 首帧为 hello 且带 members 快照",
            hello.get("kind") == "hello" and bool(hello.get("snapshot", {}).get("members")),
            f"{len(hello.get('snapshot', {}).get('members', []))} 个成员 / limits={hello.get('snapshot', {}).get('limits')}",
        )

        chat_http = next((e for e in events if e["kind"] == "_http_chat"), None)
        c.check(
            "POST /api/chat 受理成功（200）",
            chat_http is not None and chat_http["status"] == 200,
            json.dumps(chat_http["body"], ensure_ascii=False) if chat_http else "无响应",
        )

        info = summarize(events)
        kinds = info["kinds"]
        print(f"\n--- 事件序列（{len(kinds)} 条）---")
        print("    " + " → ".join(kinds[:30]) + (" …" if len(kinds) > 30 else ""))
        print("\n--- 每个发言人的实际路由 ---")
        for s in info["speakers"]:
            flag = "（已降级）" if s["degraded"] else ""
            print(
                f"    {s['sender']:<10} {s['role']:<9} 声明={s['requested_route']:<40} "
                f"实际={s['route']:<40} {s['chars']}字{flag}"
            )
        if info["notices"]:
            print("\n--- 事件流 notice ---")
            for n in info["notices"]:
                print(f"    [{n['level']}] {n['message'][:110]}")

        roles = [s["role"] for s in info["speakers"]]
        c.check("事件序列包含 run_start → message → message_start → message_delta → message_end → run_end",
                all(k in kinds for k in
                    ("run_start", "message", "message_start", "message_delta", "message_end", "run_end")))
        c.check("组长（captain）真实发言", "captain" in roles)
        c.check(
            "被 @ 的组员真实发言（≥1 个非 mock 成员）",
            any(s["role"] == "member" and not s["route"].startswith("mock/") for s in info["speakers"]),
            "、".join(s["sender"] for s in info["speakers"] if s["role"] == "member"),
        )
        c.check("记录员纪要产出（顺序在最后）", roles and roles[-1] == "recorder",
                f"发言顺序：{' → '.join(s['sender'] for s in info['speakers'])}")
        c.check("全过程无 error 事件", not info["errors"], str(info["errors"])[:120])
        c.check(
            "降级可观测：captain 未静默换模型（有 warning + route_changed + notice）",
            all(s["degraded"] for s in info["speakers"] if s["role"] == "captain")
            and "route_changed" in kinds
            and any(n["level"] == "warn" for n in info["notices"]),
            next((s["warning"][:100] for s in info["speakers"] if s["role"] == "captain"), "无"),
        )
        c.check(
            "Kimi 与豆包至少各真实发言一次（各自声明的路由）",
            {"kimi", "doubao"}.issubset({s["sender"] for s in info["speakers"]}),
            "、".join(f"{s['sender']}@{s['route']}" for s in info["speakers"]),
        )

        # 跨厂商不变量：降级不得让所有人都塌缩到同一家厂商
        captain = next((s for s in info["speakers"] if s["sender"] == "captain"), None)
        kimi_speech = next((s for s in info["speakers"] if s["sender"] == "kimi"), None)
        captain_provider = (captain or {}).get("route", "").split("/", 1)[0]
        kimi_provider = (kimi_speech or {}).get("route", "").split("/", 1)[0]
        c.check(
            "异厂：captain 与 kimi 实际生效的 provider 不同（跨厂商而非同厂自辩）",
            bool(captain_provider) and bool(kimi_provider) and captain_provider != kimi_provider,
            f"captain={captain_provider} kimi={kimi_provider}",
        )
        summaries = [e for e in events if e.get("kind") == "route_summary"]
        c.check(
            "同源可见：事件流 route_summary 报出本轮 provider 分布与同源提示",
            bool(summaries) and isinstance(summaries[0].get("providers"), dict),
            summaries[0].get("message", "")[:120] if summaries else "没有 route_summary 事件",
        )
        recorder_speech = next((s for s in info["speakers"] if s["role"] == "recorder"), None)
        recorder_text = (recorder_speech or {}).get("content", "")
        c.check(
            "记录员纪要里也写明本轮实际参与模型（避免只看纪要的人被误导）",
            any(
                marker in recorder_text
                for marker in ("实际参与模型", "实际路由", "实际使用", "实际调用")
            )
            and any(p in recorder_text for p in ("doubao", "moonshotai", "kimi")),
            f"纪要里实际路由相关片段：{[line for line in recorder_text.splitlines() if '实际' in line or 'doubao' in line or 'kimi' in line][:2]}",
        )

        # ------------------------------------------------------------------ #
        # 硬证据 1：真实调用记录（每次调用的路由 / 目标 host / 成败 / 耗时）
        # ------------------------------------------------------------------ #
        async with httpx.AsyncClient(timeout=20.0) as diag_client:
            diag = (await diag_client.get(f"{base}/api/diagnostics")).json()
        calls = diag.get("call_log", [])
        print("\n--- 实际调用记录 /api/diagnostics（每个真实调用的路由 / 目标 host / 成败 / 耗时）---")
        for entry in calls:
            print(
                f"    {time.strftime('%H:%M:%S', time.localtime(entry['ts']))} "
                f"{entry['route']:<40} host={entry['host']:<32} ok={str(entry['ok']):<5} "
                f"chars={entry['chars']:<5} {entry['elapsed_s']}s {entry['error'][:56]}"
            )
        ok_routes = {e["route"] for e in calls if e["ok"]}
        failed_routes = {e["route"] for e in calls if not e["ok"]}
        silent = [
            s["sender"] for s in info["speakers"]
            if not s["route"].startswith("mock/") and s["route"] not in ok_routes
        ]
        c.check(
            "事件流 route 与真实调用记录一一对应（不存在静默换模型）",
            not silent,
            f"事件流路由 {sorted({s['route'] for s in info['speakers']})} / 成功调用 {sorted(ok_routes)}",
        )
        untried = [
            s["sender"] for s in info["speakers"]
            if s["degraded"] and s["requested_route"] not in failed_routes
        ]
        c.check(
            "降级前的主路由确实被尝试过（requested_route 有失败记录）",
            not untried,
            f"失败调用 {sorted(failed_routes)}",
        )

        # 正文片段：证明是真实模型产出，而不是本地模板
        print("\n--- 真实发言正文片段（来自事件流 message_end）---")
        for s in info["speakers"]:
            print(f"    [{s['sender']} @ {s['route']}] {s['preview'].replace(chr(10), ' ')[:200]}")

        # ------------------------------------------------------------------ #
        # 硬证据 2：DSH 用量账本交叉核对（只回答「路由有没有被真的调用」）
        # ------------------------------------------------------------------ #
        ledger_after = ledger_snapshot()
        diff = ledger_diff(ledger_before, ledger_after)
        print("\n--- DSH 用量账本交叉核对 ~/.dsh/.dshw-usage.json ---")
        print(f"    运行前账本条目：{ledger_before.get('count')}；本次运行期间新增：{len(diff.get('new', []))} 条")
        for entry in diff.get("new", []):
            print(f"    {entry['at']}  {entry['model']:<32} tokens={entry['tokens']} cost={entry['cost']}")
        real_routes = sorted({s["route"] for s in info["speakers"] if not s["route"].startswith("mock/")})
        ours = {route.split("/", 1)[1] for route in real_routes}
        ledger_models = sorted({e["model"] for e in diff.get("new", [])})
        if diff.get("new") and (set(ledger_models) & ours):
            covered = all(model in ledger_models for model in ours)
            ledger_ok = covered
            ledger_note = (
                f"账本新增模型 {ledger_models} 覆盖本应用实际路由模型 {sorted(ours)}：{covered}"
            )
        elif diff.get("new"):
            ledger_ok = True
            ledger_note = (
                f"账本本次新增条目只有 {ledger_models}，与本应用实际路由 {real_routes} 没有交集 —— "
                "说明账本计的是 DSH 账号侧用量，不覆盖本应用直连厂商的调用，"
                "因此它既不能证实也不能证伪本应用的路由；路由证据以 /api/diagnostics 的"
                "实际调用记录（目标 host + 上游真实报错）与事件流 message_end 正文为准"
            )
        else:
            ledger_ok = True
            ledger_note = (
                "账本本次没有任何新增条目 —— 它不覆盖本应用直连厂商的调用，"
                "不能用来证伪本应用的路由；路由证据以 /api/diagnostics 的实际调用记录为准"
            )
        c.check("账本 vs 事件流交叉核对已完成并给出明确结论", ledger_ok, ledger_note)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        print(f"\n    服务端日志：{log_path.relative_to(PROJECT_ROOT)}")

    print("\n" + "=" * 78)
    if c.failures:
        print(f"结果：{c.total - len(c.failures)}/{c.total} 通过，{len(c.failures)} 项未通过 —— {', '.join(c.failures)}")
        return 1
    print(f"结果：全部通过（{c.total} 项检查）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
