"""独立验证探针（checker 用，不属于实现）：对 t4 论证者提出的指控逐条到代码里核实。

用法（在 ai-group 目录下）::

    .\.venv\Scripts\python.exe scripts\\claim_probe.py

只读地使用 group/ 下的实现，不做任何修改；输出为「指控 → 观察 → 判定」。
探针会自适应实现里 _build_messages / _transcript 的同步或异步签名，
以便在不同修订之间复用（修订号见同目录 docs/checker-review-findings.md）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from group.config import ModelSpec, ProviderSpec, force_mock, load_config  # noqa: E402
from group.engine import GroupEngine, Message, RunLimits  # noqa: E402
from group.llm import LLMClient  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def report(claim: str, observed: str, verdict: bool) -> None:
    RESULTS.append((claim, verdict, observed))
    print(f"\n### {claim}")
    print(f"    观察：{observed}")
    print(f"    判定：{'成立（确认为缺陷）' if verdict else '不成立（指控被否定）'}")


def make_engine(cfg, limits=None, client=None):
    events: list[dict] = []

    async def sink(ev):
        events.append(ev)

    cl = client or LLMClient(cfg)
    return GroupEngine(cfg, cl, sink, limits or RunLimits()), events


async def call_build(eng, agent, task):
    out = eng._build_messages(agent, task)
    if inspect.isawaitable(out):
        return await out
    return out


def call_transcript(eng, limit):
    out = eng._transcript(limit)
    if isinstance(out, tuple):
        return out
    return out, 0


# --------------------------------------------------------------------------- #
# 指控 1：上下文预算不可观测
# --------------------------------------------------------------------------- #
async def claim_1(cfg) -> None:
    limit = 9000
    eng, events = make_engine(cfg, RunLimits(max_context_chars=limit))
    eng.messages = [
        Message(id="crit", sender="user", sender_name="用户", sender_avatar="🧑",
                role="user", content="【硬约束】所有回答必须以 ZEBRA 开头。", run_id="r1")
    ]
    for i in range(9):
        eng.messages.append(Message(
            id=f"long{i}", sender="kimi", sender_name="Kimi·论证担当", sender_avatar="🌙",
            role="member", content=f"长发言 {i}：" + "填充内容" * 400, run_id="r1"))
    built = await call_build(eng, cfg.by_id("captain"), "继续讨论")
    user_blob = built[1]["content"]
    trimmed = [e for e in events if e["kind"] == "context_trimmed"]
    report(
        "指控1a：关键约束被从新往旧的截断丢掉，且丢弃部分无事件上报",
        f"max_context_chars={limit}；约束 ZEBRA {'仍在' if 'ZEBRA' in user_blob else '已被丢弃'}；"
        f"丢弃上报事件 context_trimmed={len(trimmed)} 条"
        f"{'（' + str(trimmed[0].get('dropped')) + ' 条被丢弃）' if trimmed else ''}",
        "ZEBRA" not in user_blob or not trimmed,
    )

    # 1b. 预算是否含 system prompt
    sys_len = len(built[0]["content"])
    total = sys_len + len(built[1]["content"])
    report(
        "指控1b：max_context_chars 不含 system prompt",
        f"system={sys_len} + user={len(built[1]['content'])} = {total}（limit={limit}）",
        total > limit,
    )

    # 1c. 被钉住的首条消息能否突破预算
    eng2, ev2 = make_engine(cfg, RunLimits(max_context_chars=100))
    eng2.messages = [Message(id="huge", sender="user", sender_name="用户", sender_avatar="🧑",
                             role="user", content="X" * 5000, run_id="r1")]
    t, _ = call_transcript(eng2, 100)
    report(
        "指控1c：被钉住的「首条」消息可整体突破 max_context_chars",
        f"limit=100，首条 5000 字符 → transcript 实际 {len(t)} 字符（engine.py:191-193 钉住首条）",
        len(t) > 100,
    )

    # 1d. 窗口跨 run 共享
    eng3, _ = make_engine(cfg)
    eng3.messages = [Message(id="old", sender="kimi", sender_name="K", sender_avatar="🌙",
                             role="member", content="上一轮的机密内容 SECRET-OLD", run_id="round-A")]
    sig = inspect.signature(eng3._transcript)
    t3, _ = call_transcript(eng3, 9000)
    report(
        "指控1d：上下文窗口跨 run 共享（不以 run_id 为界）",
        f"_transcript 签名 {sig}（无 run_id 参数）；round-B 仍能看到 round-A 的 'SECRET-OLD'："
        f"{'SECRET-OLD' in t3}；messages 在 run 之间从不清空",
        "run_id" not in sig.parameters and "SECRET-OLD" in t3,
    )


# --------------------------------------------------------------------------- #
# 指控 2：失败 / 顺序 / 去重语义
# --------------------------------------------------------------------------- #
async def claim_2(cfg) -> None:
    os.environ["AIGROUP_PROBE_KEY"] = "probe-placeholder"
    cfg.providers["probe-dead"] = ProviderSpec(
        id="probe-dead", label="探针不可达", api="openai-chat",
        base_url="http://127.0.0.1:1/v1",
        credentials=[{"env": "AIGROUP_PROBE_KEY"}],
        models={"dead": ModelSpec(id="dead")},
    )
    force_mock(cfg)
    kimi = cfg.by_id("kimi")
    saved = (kimi.provider, kimi.model, kimi.fallback)
    kimi.provider, kimi.model, kimi.fallback = "probe-dead", "dead", None
    try:
        eng, events = make_engine(cfg)
        await eng.handle_user_message("探针：组员异常路径下的汇总行为")
        failed = [e for e in events if e["kind"] == "message_end" and e.get("error")]
        kimi_msgs = [m for m in eng.messages if m.sender == "kimi"]
        rec = [m for m in eng.messages if m.role == "recorder"]
        cap = [m for m in eng.messages if m.role == "captain"]
        trans, _ = call_transcript(eng, 9000)
        gap_marked = any(k in trans for k in ("失败", "error", "不可用"))
        report(
            "指控2a：成员失败只发 notice 并 return []；记录员照常汇总，缺口与充分在产物上不可区分",
            f"失败的 message_end（带 error）{len(failed)} 条，错误={failed[0].get('error', '')[:50] if failed else '无'}；"
            f"kimi 的失败发言进入 engine.messages={bool(kimi_msgs)}（记录员看不到）；"
            f"本轮仍产出记录员纪要 {len(rec)} 条（captain 发言 {len(cap)} 条 + 其它成功发言 ≥2 即触发汇总）；"
            f"喂给记录员的 transcript {'含' if gap_marked else '不含'}失败/缺口标记",
            bool(failed) and not kimi_msgs and len(rec) == 1 and not gap_marked,
        )

        # 2f. 失败成员仍被 route_summary 记为「参与且未降级」
        routes = eng.route_summary().get("routes", [])
        failed_entries = [r for r in routes if r.get("declared", "").startswith("probe-dead")]
        report(
            "指控2f（附加发现）：调用失败的成员仍被 route_summary 记为「实际参与且未降级」",
            f"route_summary.routes={routes}；其中失败成员 kimi 的条目={failed_entries}；"
            f"engine.py:511 在 LLMError 分支调用 _note_route(...,degraded=False) 且用 requested_route 充当实际路由",
            bool(failed_entries) and failed_entries[0].get("degraded") is False,
        )
    finally:
        kimi.provider, kimi.model, kimi.fallback = saved
        cfg.providers.pop("probe-dead", None)

    # 2b. 并发入口是拒绝，不排队
    eng, events = make_engine(cfg)
    await eng._lock.acquire()
    try:
        await eng.handle_user_message("并发入口探针")
    finally:
        eng._lock.release()
    errs = [e for e in events if e["kind"] == "error"]
    report(
        "指控2b：并发入口是拒绝（emit error 后 return），不是排队",
        f"持锁期间第二次发言 → {errs[0]['message'] if errs else '无'}；该轮被直接丢弃（engine.py:271-273）",
        bool(errs) and "请稍候" in errs[0]["message"],
    )

    # 2c. message_start 缺 turn
    eng, events = make_engine(cfg)
    await eng.handle_user_message("探针：事件字段")
    starts = [e for e in events if e["kind"] == "message_start"]
    ends = [e for e in events if e["kind"] == "message_end"]
    report(
        "指控2c：message_start 事件缺 turn 序号",
        f"message_start 字段={sorted(starts[0].keys()) if starts else '无'}，含 turn={'turn' in starts[0] if starts else '?'}；"
        f"message_end {len(ends)} 条通过嵌套 message.turn 暴露序号",
        bool(starts) and "turn" not in starts[0],
    )

    # 2d. docstring 的 (成员,轮次) 去重
    src = (PROJECT_ROOT / "group" / "engine.py").read_text(encoding="utf-8")
    report(
        "指控2d：docstring 声称的 (成员, 轮次) 去重不存在",
        f"当前 engine.py 是否仍出现「(成员, 轮次) 去重」文案={'（成员, 轮次）去重' in src or '(成员, 轮次) 去重' in src}；"
        f"实际护栏=_run_spoke 计数 vs max_turns_per_run + max_depth + max_messages（engine.py:324-395）",
        "(成员, 轮次) 去重" in src,
    )

    # 2e. resolve_mention 宽松匹配副作用
    eng, _ = make_engine(cfg)
    resolved = {t: (eng.resolve_mention(t).id if eng.resolve_mention(t) else None)
                for t in ("k", "demo", "论证", "组", "Kimi·论证担当")}
    print("    " + "；".join(f"@{t} -> {v}" for t, v in resolved.items()))
    report(
        "指控2e：resolve_mention 宽松匹配有副作用（任意短串/子串都能命中某个成员）",
        f"实测 {resolved}；engine.py:131-139 三级子串匹配无最小长度、无唯一性校验，命中即返回第一个成员",
        resolved["k"] is not None and resolved["k"] != "kimi" and resolved["demo"] == "demo-bot",
    )


# --------------------------------------------------------------------------- #
# 指控 3：可度量性
# --------------------------------------------------------------------------- #
def claim_3() -> None:
    fields = [f.name for f in dataclasses.fields(Message)]
    token_fields = [f for f in fields if any(k in f for k in ("token", "usage", "cost"))]
    report(
        "指控3：Message 无 token/成本字段，事件流无「本轮实际喂进去的上下文清单」",
        f"Message 字段={fields}；token/usage/cost 字段={token_fields or '无'}；"
        f"上下文相关事件只有 context_trimmed（丢了多少条），没有「发了什么」的清单",
        not token_fields,
    )


async def main() -> int:
    cfg = load_config()
    print("=" * 78)
    print("独立验证探针：核实 t4 论证者对 ai-group 实现的三条指控")
    print("=" * 78)
    await claim_1(cfg)
    print("\n" + "-" * 78)
    await claim_2(cfg)
    print("\n" + "-" * 78)
    claim_3()
    print("\n" + "=" * 78)
    for claim, verdict, _ in RESULTS:
        print(f"[{'成立' if verdict else '否定'}] {claim}")
    print(f"合计 {sum(v for _, v, _ in RESULTS)}/{len(RESULTS)} 条指控成立")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
