"""t9 复验探针：在当前 head 上独立复验 t3 的 F1 / F2 两条 finding。

用法（在 ai-group 目录下）::

    ..\\campus-market\\backend\\.venv\\Scripts\\python.exe docs\\verify_f1_f2_probe.py

F1（上下文窗口本轮优先）：构造「12 条更早轮次长发言 + 1 条本轮短约束」，
直接调 `engine._transcript(300)`，断言**本轮约束被保留**、被丢弃的是更早轮次记录。
F2（/api/snapshot 字段）：以 SMOKE_MOCK_ONLY=1 真起 uvicorn，请求 `GET /api/snapshot`，
断言顶层键含 `route_summary` 与 `run_stats`（以及 members/limits/messages/running）。

本脚本只读代码、只发请求，不修改任何实现文件。退出码 0 = 两条全部复验通过。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from group.config import force_mock, load_config  # noqa: E402
from group.engine import GroupEngine, Message, RunLimits  # noqa: E402
from group.llm import LLMClient  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------------- #
# F1：上下文窗口「本轮优先」
# --------------------------------------------------------------------------- #
def verify_f1() -> None:
    cfg = force_mock(load_config())

    async def sink(_ev: dict) -> None:
        return None

    # ---- 场景 a：合同指定的 budget=300（12 条更早轮次长发言 + 1 条本轮短约束）----
    engine = GroupEngine(cfg, LLMClient(cfg), sink, RunLimits(max_context_chars=9000))
    engine.current_run_id = "RUN-CURRENT"
    earlier = [
        Message(
            id=f"old{i}",
            sender="kimi",
            sender_name="Kimi·论证担当",
            sender_avatar="🌙",
            role="member",
            content=f"OLD-{i} " + "旧轮次填充" * 40,  # 每条约 205 字
            run_id="RUN-EARLIER",
        )
        for i in range(12)
    ]
    current = Message(
        id="cur",
        sender="user",
        sender_name="用户",
        sender_avatar="🧑",
        role="user",
        content="【本轮硬约束 CURRENT-CONSTRAINT】必须以这条为准。",
        run_id="RUN-CURRENT",
    )
    engine.messages = [*earlier, current]

    text, dropped, truncated = engine._transcript(300)
    # 精确匹配标记，避免 "OLD-1" 命中 "OLD-11" 这种子串误判
    kept_old = sorted(
        (int(m.group(1)) for m in re.finditer(r"OLD-(\d+)(?!\d)", text)),
    )
    print(f"    [a] budget=300 → 返回 {len(text)} 字符；dropped={dropped}；truncated_pinned={truncated}")
    print(f"    [a] 保留下来的更早轮次编号={kept_old}；本轮约束={'在' if 'CURRENT-CONSTRAINT' in text else '被裁'}")
    print(f"    [a] transcript 预览：{text[:100].replace(chr(10), ' / ')} …")
    record(
        "F1a：budget=300 时本轮短约束被保留（本轮消息不会被更早轮次挤掉）",
        "CURRENT-CONSTRAINT" in text and not truncated,
        "命中 CURRENT-CONSTRAINT 且未被截断" if "CURRENT-CONSTRAINT" in text else "本轮约束被裁掉（回归）",
    )
    record(
        "F1a：被丢弃的全部是更早轮次记录（保留数 + 丢弃数 = 12）",
        len(kept_old) + dropped == 12,
        f"保留 {len(kept_old)} 条（编号 {kept_old}）+ 丢弃 {dropped} 条 = {len(kept_old) + dropped}（期望 12）",
    )
    record(
        "F1a：更早轮次内部也是「新的优先」（OLD-11 保留、OLD-0 被裁）",
        max(kept_old, default=-1) == 11 and 0 not in kept_old,
        f"保留编号 {kept_old}（旧实现会把 OLD-0 留下、把 OLD-11 裁掉）",
    )

    # ---- 场景 b：判别性场景（预算只够「钉住的本轮原话 + 一条发言」）----
    # max_context_chars 取小值使 _transcript 落到 400 字符下限，与实际提示词长度无关，可复现。
    engine2 = GroupEngine(cfg, LLMClient(cfg), sink, RunLimits(max_context_chars=200))
    engine2.current_run_id = "RUN-B"
    body = "填" * 240
    engine2.messages = [
        Message(id="old", sender="kimi", sender_name="Kimi·论证担当", sender_avatar="🌙",
                role="member", content="OLD-RUN-MARKER " + body, run_id="RUN-A"),
        Message(id="cur-user", sender="user", sender_name="用户", sender_avatar="🧑",
                role="user", content="CUR-RUN-USER-MARKER 本轮约束", run_id="RUN-B"),
        Message(id="cur-speech", sender="kimi", sender_name="Kimi·论证担当", sender_avatar="🌙",
                role="member", content="CUR-RUN-SPEECH-MARKER " + body, run_id="RUN-B"),
    ]
    text2, dropped2, _ = engine2._transcript(400)
    print(f"    [b] budget=400 → dropped={dropped2}；本轮发言="
          f"{'在' if 'CUR-RUN-SPEECH-MARKER' in text2 else '被裁'}；"
          f"更早轮次={'被裁' if 'OLD-RUN-MARKER' not in text2 else '仍在'}")
    record(
        "F1b：预算只够一条时，被保留的是本轮发言而不是上一轮（顺序写反会立刻失败）",
        "CUR-RUN-SPEECH-MARKER" in text2 and "OLD-RUN-MARKER" not in text2
        and "CUR-RUN-USER-MARKER" in text2,
        f"本轮发言={'在' if 'CUR-RUN-SPEECH-MARKER' in text2 else '被裁'}、"
        f"更早轮次={'被裁' if 'OLD-RUN-MARKER' not in text2 else '仍在'}",
    )


# --------------------------------------------------------------------------- #
# F2：/api/snapshot 顶层字段
# --------------------------------------------------------------------------- #
def verify_f2() -> None:
    import httpx

    port = free_port()
    env = dict(os.environ)
    env["SMOKE_MOCK_ONLY"] = "1"
    log = (PROJECT_ROOT / "docs" / "evidence" / "_probe_uvicorn.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "group.app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        ready = False
        deadline = time.time() + 60
        with httpx.Client(timeout=10.0) as client:
            while time.time() < deadline:
                try:
                    if client.get(f"{base}/api/health").status_code == 200:
                        ready = True
                        break
                except Exception:
                    time.sleep(0.5)
            record("F2：mock-only 服务已就绪", ready, base)
            if not ready:
                return
            payload = client.get(f"{base}/api/snapshot").json()
        keys = sorted(payload.keys())
        print(f"    GET /api/snapshot → sorted(keys) = {keys}")
        required = {"route_summary", "run_stats", "members", "limits", "messages", "running"}
        missing = sorted(required - set(keys))
        record(
            "F2：顶层键含 route_summary + run_stats（以及 members/limits/messages/running）",
            not missing,
            f"缺失={missing or '无'}；实际键={keys}",
        )
        summary = payload.get("route_summary") or {}
        stats = payload.get("run_stats") or {}
        record(
            "F2：route_summary / run_stats 是可用的结构化对象（不是 None/占位）",
            isinstance(summary, dict) and "providers" in summary and isinstance(stats, dict)
            and "model_calls_per_run" in stats,
            f"route_summary keys={sorted(summary.keys())}；run_stats={json.dumps(stats, ensure_ascii=False)}",
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def main() -> int:
    print("=" * 78)
    print("t9 复验探针：F1（窗口本轮优先） / F2（/api/snapshot 字段）")
    print("=" * 78)
    print("\n--- F1：engine._transcript 预算分配 ---")
    verify_f1()
    print("\n--- F2：/api/snapshot 顶层键 ---")
    verify_f2()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 78)
    if failed:
        print(f"复验结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过，未通过 —— {', '.join(failed)}")
        return 1
    print(f"复验结果：全部通过（{len(RESULTS)} 项）—— F1/F2 在当前 head 上均已修复")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
