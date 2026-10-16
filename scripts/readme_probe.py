"""README 与实现一致性探针（checker 用，不属于实现）。

针对 README.md 里几条「文档写有」的能力逐条到代码/运行结果里核实：

* README:86 「依次唤醒被点名的组员（可配置并发或串行）」
* README:42 「没有任何 API 密钥也能跑……整条链路」
* README:100 「去重：同一成员同一条消息不会重复触发」

用法（在 ai-group 目录下）::

    ..\\campus-market\\backend\\.venv\\Scripts\\python.exe scripts\\readme_probe.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from group.config import load_config  # noqa: E402
from group.engine import GroupEngine, RunLimits  # noqa: E402
from group.llm import LLMClient  # noqa: E402


async def no_credential_scenario(cfg) -> None:
    """模拟「一台没有任何可用密钥的机器」：所有成员的主/备 provider 都解析不出密钥。"""
    saved = []
    for agent in [*cfg.agents]:
        saved.append((agent, agent.provider, agent.model, agent.fallback))
        agent.provider, agent.model = "no-cred-provider", "n/a"
        agent.fallback = None

    events: list[dict] = []

    async def sink(ev):
        events.append(ev)

    engine = GroupEngine(cfg, LLMClient(cfg), sink, RunLimits())
    await engine.handle_user_message("没有任何密钥的机器上，链路还能不能跑通？")
    for agent, provider, model, fallback in saved:
        agent.provider, agent.model, agent.fallback = provider, model, fallback

    speakers = [m.sender for m in engine.messages if m.role != "user"]
    errors = [e for e in events if e["kind"] == "error"]
    notices = [e for e in events if e["kind"] == "notice" and e.get("level") == "error"]
    print("\n### README:42「没有任何 API 密钥也能跑整条链路」")
    print(f"    模拟：所有成员主路由+fallback 均无密钥 → 本轮实际发言={speakers or '无'}；"
          f"error 事件={len(errors)}，成员失败 notice={len(notices)}；"
          f"mock 成员 demo-bot 是否被唤醒={'demo-bot' in speakers}")
    print("    判定：不成立——组长这一棒失败后不会产生 @，"
          "demo-bot 与记录员永远不会被唤醒，链路整体停摆（只有报错事件）")


def readme_claims() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    engine_src = (PROJECT_ROOT / "group" / "engine.py").read_text(encoding="utf-8")
    scanned = []
    for path in list((PROJECT_ROOT / "config").rglob("*.yaml")) + list((PROJECT_ROOT / "group").rglob("*.py")):
        scanned.append(path.read_text(encoding="utf-8"))
    blob = "\n".join(scanned)

    has_parallel_cfg = bool(re.search(r"(concurren|parallel|max_workers|并发数|并行度)", blob, re.I))
    print("\n### README:86「可配置并发或串行」")
    print(f"    README 原文：{'（可配置并发或串行）' in readme}；"
          f"config/*.yaml 与 group/*.py 中并发相关配置项={has_parallel_cfg}；"
          f"engine.py:302 是 `for agent, task_text in queue:` 串行 await")
    print("    判定：不成立——没有任何并发开关，实现是硬编码串行")

    has_pair_dedup = "(成员, 轮次) 去重" in engine_src
    code_dedup = bool(re.search(r"\(sender, turn\)|_seen_pairs|dedup", engine_src))
    print("\n### README:100「去重：同一成员同一条消息不会重复触发」/ engine.py:14「(成员, 轮次) 去重」")
    print(f"    engine.py:14 文案存在={has_pair_dedup}；代码里按 (sender, turn) 去重的实现={code_dedup}；"
          f"实际护栏=`_run_spoke` 计数 vs max_turns_per_run + max_depth + max_messages")
    print("    判定：文案与实现不符——不存在 (成员, 轮次) 去重；"
          "成员可在同一轮里被重复唤醒，直到 max_turns_per_run 用尽")


if __name__ == "__main__":
    cfg = load_config()
    asyncio.run(no_credential_scenario(cfg))
    readme_claims()
