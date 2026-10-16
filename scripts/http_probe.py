"""只读 HTTP 冒烟（checker 用）：对已在跑的服务做 /api/health 与 / 的检查，不发聊天请求。

用法（在 ai-group 目录下）::

    ..\\campus-market\\backend\\.venv\\Scripts\\python.exe scripts\\http_probe.py --port 8801

设计成不发 /api/chat：这样可以对**真实配置**的服务做验收 1/2，而完全不消耗模型额度。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

RESULTS: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} —— {detail}")


def http_get(url: str) -> tuple[int, bytes, float]:
    t0 = time.time()
    with urllib.request.urlopen(url, timeout=15) as resp:
        body = resp.read()
        return resp.status, body, time.time() - t0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument("--expect-mock", action="store_true", help="断言服务处于 mock_only 模式")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    status, raw, dt = http_get(f"{base}/api/health")
    health = json.loads(raw)
    providers = health.get("providers") or []
    members = health.get("members") or []
    providers_ok = bool(providers) and all(
        isinstance(p.get("provider"), str) and isinstance(p.get("ok"), bool) and p.get("detail")
        for p in providers
    )
    members_ok = bool(members) and all(
        {"id", "name", "role", "route", "enabled", "max_turns_per_run"} <= set(m) for m in members
    )
    report(
        "GET /api/health 返回 200，providers 有 ok/detail，members 列出启用成员表",
        status == 200 and providers_ok and members_ok,
        f"HTTP {status} {dt*1000:.0f}ms；providers="
        + ", ".join(f"{p['provider']}={'ok' if p['ok'] else 'unavailable'}" for p in providers)
        + f"；members({len(members)})=" + ", ".join(f"{m['id']}[{'on' if m['enabled'] else 'off'}]" for m in members),
    )
    if args.expect_mock:
        report(
            "服务处于 SMOKE_MOCK_ONLY 模式（不调用真实模型）",
            health.get("mock_only") is True,
            f"mock_only={health.get('mock_only')}",
        )

    status2, raw2, dt2 = http_get(f"{base}/")
    html = raw2.decode("utf-8", errors="replace")
    hits = [k for k in ("群", "chat", "ws/group", "app.js") if k.lower() in html.lower()]
    report(
        "GET / 返回 200 且 HTML 含群聊界面关键元素",
        status2 == 200 and len(html) > 500 and len(hits) >= 2,
        f"HTTP {status2} {dt2*1000:.0f}ms，{len(html)} 字节，命中 {hits}",
    )

    print("\n" + "=" * 78)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"结果：{len(RESULTS)-len(failed)}/{len(RESULTS)} 通过" + (f"，未通过：{failed}" if failed else " ALL PASS"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
