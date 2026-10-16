"""端到端自检：不启服务器，直接在进程内跑完整一轮群聊，验证内核链路与所有护栏。

用法（在 ai-group 目录下）::

    python scripts\\smoke_test.py --mock-only   # 强制全 mock，零额度（CI/验收用）
    python scripts\\smoke_test.py               # 用真实配置跑一遍

检查项（每一项都必须 PASS，否则退出码非 0）：

  1. 配置装载：成员、每个成员独立的角色提示词、记录员条目
  2. provider 凭据解析结果（只报告有没有，绝不打印密钥）
  3. @提及 抽取与宽松匹配（@Kimi → kimi；未知目标 → None）
  4. 事件序列完整：run_start → message → message_start → message_delta → message_end → run_end
  5. 「组长 + 组员 + 记录员」是否都发了言
  6. 无人被 @ 时不唤醒组员（mock 的 quiet 开关）
  7. 未知 @目标 只产生 notice，不抛错
  8. 降级路径：主路由不可用 / 调用失败 → 自动换路由 + 事件流标注「已降级」
  9. 额度护栏：单轮发言总数、成员级 max_turns_per_run、@ 触发深度、上下文长度上限
 10. 密钥零落盘：代码与配置里没有明文密钥
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

# Windows 控制台默认是 GBK，输出里任何非 GBK 字符都会让 print 抛 UnicodeEncodeError，
# 进而把退出码变成 1（重定向到文件时用 UTF-8，反而不复现）。这里统一把标准输出改成
# 「编码不变、无法编码的字符降级替换」，保证任何控制台下都干净退出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover - 老版本/特殊流上失败就算了
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from group import ag2_adapter  # noqa: E402
from group.config import (  # noqa: E402
    AgentSpec,
    ModelSpec,
    ProviderSpec,
    check_invariants,
    extract_mentions,
    force_mock,
    load_config,
    probe_availability,
)
from group.engine import GroupEngine, Message, RunLimits  # noqa: E402
from group.llm import LLMClient, LLMError  # noqa: E402

# 自检要干净的引擎：关掉聊天记录持久化。
# 否则本文件会创建几十个引擎，后一个把前一个存下的历史读回来，
# 「本轮几条发言」这类计数断言会全部错乱。
os.environ["AIGROUP_NO_HISTORY"] = "1"

# 单项检查的超时预算。真实模式由 main() 按上游限流情况放宽（可用 SMOKE_REAL_TIMEOUT 覆盖）。
CHECK_TIMEOUT: float = 600.0


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.total = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.total += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)

    def section(self, title: str) -> None:
        print(f"\n--- {title} ---")


def new_engine(cfg, limits: RunLimits | None = None):
    events: list[dict] = []

    async def sink(ev: dict) -> None:
        events.append(ev)

    engine = GroupEngine(cfg, LLMClient(cfg), sink, limits or RunLimits())
    return engine, events


def kinds_of(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


def speakers_of(events: list[dict]) -> list[str]:
    out: list[str] = []
    for ev in events:
        if ev["kind"] == "message_end" and ev.get("message"):
            sender = ev["message"]["sender"]
            if sender not in out:
                out.append(sender)
    return out


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #
def check_config(c: Checker, cfg, active) -> None:
    c.check("配置装载：启用成员 ≥ 2", len(active) >= 2, "、".join(a.id for a in active))
    c.check(
        "角色提示词：每个成员都有独立系统提示词（>200 字）",
        all(len(a.system_prompt) > 200 for a in active),
        "、".join(f"{a.id}:{len(a.system_prompt)}字" for a in active),
    )
    prompts = [a.system_prompt for a in active]
    c.check("角色提示词互不相同（一人一职责）", len(set(prompts)) == len(prompts))
    # 2026-10-13 加：角色文件必须在盘上且非空。
    # 起因是一次真实事故 —— 界面加/删成员的写文件逻辑把 member_analyst.md 清成了 0 字节、
    # 还删掉了另外两个模板，而系统提示词只有 200 字以上的断言，空文件会让它悄悄变短。
    from group.config import ROLES_DIR as _ROLES
    bad_files = []
    for _a in [*active, cfg.recorder]:
        _p = _ROLES / _a.role_file
        if not _p.exists() or _p.stat().st_size == 0:
            bad_files.append(_a.role_file or '(空文件名)')
    c.check(
        "每个成员的角色提示词文件都在盘上且非空",
        not bad_files,
        "、".join(bad_files) if bad_files else f"共 {len(active) + 1} 份",
    )
    c.check(
        "记录员：来自 models.yaml，且有独立提示词、不参与派发",
        cfg.recorder.is_recorder
        and cfg.recorder not in active
        and len(cfg.recorder.system_prompt) > 200,
        f"{cfg.recorder.id} / {cfg.recorder.route}",
    )
    desc = ag2_adapter.describe()
    c.check(
        "AG2 适配层：describe() 给出明确说明（装没装都不影响主流程）",
        isinstance(desc, dict) and bool(desc.get("detail")) and desc.get("kernel") == "group.engine.GroupEngine",
        desc["detail"][:72],
    )
    problems = check_invariants(cfg)
    c.check(
        "配置不变量：任何成员的 fallback 都不得与主路由同厂（防「同厂自辩」）",
        not problems,
        "；".join(problems) if problems else "、".join(
            f"{a.id}:{a.provider}→{a.fallback_route}" for a in cfg.agents if a.fallback
        ),
    )


def check_ag2_mapping(c: Checker, cfg, client) -> None:
    """不装 AG2 也要能验证「成员 → AG2 llm_config」的映射逻辑。"""
    ready, blocked = 0, 0
    for spec in cfg.agents:
        if not spec.enabled:
            continue
        try:
            mapping = ag2_adapter.build_llm_config(cfg, client, spec)
            assert mapping["config_list"][0]["model"] == spec.model
            ready += 1
        except LLMError as exc:
            assert "密钥" in str(exc), str(exc)
            blocked += 1
    c.check(
        "AG2 映射：build_llm_config 对每个启用成员都能编译（或给出明确的缺密钥说明）",
        ready + blocked == len([a for a in cfg.agents if a.enabled]),
        f"可编译 {ready} 个，缺密钥 {blocked} 个",
    )
    try:
        ag2_adapter.build_ag2_group(cfg, client)
        c.check("AG2 GroupChat：已安装时可构建", True, f"{ag2_adapter.AG2_PACKAGE}")
    except RuntimeError as exc:
        c.check(
            "AG2 GroupChat：未安装时给出明确降级说明（不抛未处理异常）",
            "pip install ag2" in str(exc),
            str(exc)[:72],
        )


def check_mentions(c: Checker, engine) -> None:
    tokens = extract_mentions("请 @kimi 给理由，再 @豆包 复核，最后 @不存在的名字 收尾")
    c.check(
        "@提及 抽取：整词不截断、去重、保序",
        tokens == ["kimi", "豆包", "不存在的名字"],
        str(tokens),
    )
    doubao = engine.resolve_mention("豆包")
    c.check("按显示名宽松匹配 @豆包 → doubao", doubao is not None and doubao.id == "doubao",
            doubao.id if doubao else "None")
    c.check("宽松匹配 @论证（标签）→ kimi",
            (lambda a: a is not None and a.id == "kimi")(engine.resolve_mention("论证")))
    # 2026-10-13：成员名只用模型原名。当前可见成员是 DeepSeek(组长) / Kimi / 豆包，
    # 显示名互不重复；解析顺序是「精确 id → 精确显示名 → 唯一标签 → 唯一子串」。
    # 这里同时钉住「名字唯一」与「按显示名能解析到人」两件事——
    # 之前 captain 也叫 Kimi 时是靠 id 精确匹配兜住的，改名后必须仍能按名字找到。
    names = [a.display_name for a in engine.active_agents]
    c.check(
        "成员显示名互不重复",
        len(names) == len(set(names)),
        f"显示名={names}",
    )
    _ds, _ds_why = engine.resolve_mention_detail("DeepSeek")
    c.check(
        "@DeepSeek 按显示名命中组长",
        _ds is not None and _ds.id == "captain",
        f"解析={_ds.id if _ds else 'None'} / {_ds_why}",
    )
    _k, _why = engine.resolve_mention_detail("Kimi")
    c.check(
        "@Kimi 命中 kimi 组员",
        _k is not None and _k.id == "kimi",
        f"解析={_k.id if _k else 'None'} / {_why}",
    )
    c.check("未知成员解析为 None（不抛错）", engine.resolve_mention("不存在的名字") is None)


async def check_happy_round(c: Checker, cfg, question: str) -> None:
    engine, events = new_engine(cfg)
    print(f"\n--- 跑一轮完整群聊 ---\n    问题：{question}")
    await engine.handle_user_message(question)

    kinds = kinds_of(events)
    print(f"    事件序列（{len(kinds)} 条）：{' → '.join(kinds[:26])}{' …' if len(kinds) > 26 else ''}")

    for required in ("run_start", "message", "message_start", "message_delta", "message_end", "run_end"):
        c.check(f"事件序列包含 {required}", required in kinds)
    c.check("事件顺序：run_start 在 run_end 之前", kinds.index("run_start") < kinds.index("run_end"))

    speakers = speakers_of(events)
    c.check("至少两个不同角色发言（组长 + 组员）", len(speakers) >= 2, "、".join(speakers))

    captain_msgs = [m for m in engine.messages if m.role == "captain"]
    member_msgs = [m for m in engine.messages if m.role == "member"]
    recorder_msgs = [m for m in engine.messages if m.role == "recorder"]
    c.check("组长发言", len(captain_msgs) >= 1, f"{len(captain_msgs)} 条")
    c.check("组员发言（@ 触发生效）", len(member_msgs) >= 1,
            "、".join(m.sender for m in member_msgs))
    c.check("记录员汇总产出", len(recorder_msgs) >= 1 and len(recorder_msgs[0].content) > 50,
            f"{len(recorder_msgs)} 条 / {len(recorder_msgs[0].content) if recorder_msgs else 0} 字")

    if member_msgs:
        print("\n--- 组员发言片段 ---")
        print("    " + member_msgs[0].content[:160].replace("\n", "\n    "))
    if recorder_msgs:
        print("\n--- 记录员纪要片段 ---")
        print("    " + recorder_msgs[0].content[:220].replace("\n", "\n    "))

    # 成员级额度：谁都不能超过 max_turns_per_run
    counters: dict[str, int] = {}
    for m in engine.messages:
        if m.role in ("captain", "member"):
            counters[m.sender] = counters.get(m.sender, 0) + 1
    over = [
        f"{aid}:{n}>{cfg.by_id(aid).max_turns_per_run}"
        for aid, n in counters.items()
        if cfg.by_id(aid) and n > cfg.by_id(aid).max_turns_per_run
    ]
    c.check("成员级额度 max_turns_per_run 生效", not over,
            "、".join(f"{k}={v}" for k, v in counters.items()) or "无")

    # 上下文长度上限：max_context_chars 是「system + user 总字符数」上限
    async def _ignore(_ev: dict) -> None:
        return None

    trim_events: list[dict] = []

    async def _sink(ev: dict) -> None:
        trim_events.append(ev)

    # 预算要**跟着 system 的实际长度走**，不能写死。
    # 理由：system = 角色提示词 + 成员名单 + 输出纪律，它会随功能增长
    #（比如加了「权限顺序」「@全体成员」之后从 ~2500 涨到 ~2830）。
    # 写死一个数会让这个检查悄悄变成「提示词别变长」的脆弱断言 ——
    # 2026-10-14 就真的这样误报过一次。
    #
    # 真正要验的行为是：**给定一个在 system 之上还留有余量的预算，聊天记录必须被裁到不超**。
    probe = GroupEngine(cfg, LLMClient(cfg), _ignore, RunLimits(max_context_chars=200000))
    probe.messages = [
        Message(id="seed", sender="user", sender_name="用户", sender_avatar="🧑",
                role="user", content="【硬约束 ZEBRA】这条约束绝对不能丢。"),
    ]
    probe_built = await probe._build_messages(cfg.by_id("captain"), "上限检查")
    sys_len = len(probe_built[0]["content"])

    limit_chars = sys_len + 700          # system 之外，给 user 留 700 字
    big = GroupEngine(cfg, LLMClient(cfg), _sink, RunLimits(max_context_chars=limit_chars))
    big.messages = [
        Message(id="seed", sender="user", sender_name="用户", sender_avatar="🧑",
                role="user", content="【硬约束 ZEBRA】这条约束绝对不能丢。"),
        *[
            Message(id=f"m{i}", sender="kimi", sender_name="Kimi",
                    sender_avatar="🌙", role="member", content=f"第{i}条历史发言" + "上下文填充" * 40)
            for i in range(60)
        ],
    ]
    built = await big._build_messages(cfg.by_id("captain"), "上限检查")
    total = len(built[0]["content"]) + len(built[1]["content"])
    c.check(
        "上下文长度上限 max_context_chars 生效（system + user 总字符数）",
        total <= limit_chars,
        f"{total} 字符 ≤ {limit_chars}（system {len(built[0]['content'])} + user {len(built[1]['content'])}）",
    )
    # system 本身就可能超过预算（角色提示词 + 名单 + 纪律 ≈ 2830 字）。
    # 这时必须**如实上报**，不能假装上限生效了 —— 否则用户以为省了钱，其实没有。
    tiny_events: list[dict] = []

    async def _tiny_sink(ev: dict) -> None:
        tiny_events.append(ev)

    tiny = GroupEngine(cfg, LLMClient(cfg), _tiny_sink, RunLimits(max_context_chars=800))
    tiny.messages = [
        Message(id="seed", sender="user", sender_name="用户", sender_avatar="🧑",
                role="user", content="【硬约束 ZEBRA】这条约束绝对不能丢。"),
    ]
    await tiny._build_messages(cfg.by_id("captain"), "极小预算")
    tiny_msgs = [str(e.get("message", "")) for e in tiny_events]
    c.check(
        "预算小于 system 时如实上报（不假装上限生效）",
        any("角色提示词本身较长" in m for m in tiny_msgs),
        next((m for m in tiny_msgs if "角色提示词本身较长" in m), "未上报")[:100],
    )
    c.check(
        "截断有事件上报：context_trimmed 明确给出丢弃条数",
        any(e["kind"] == "context_trimmed" and e.get("dropped", 0) > 0 for e in trim_events),
        next((e["message"] for e in trim_events if e["kind"] == "context_trimmed"), "无"),
    )
    c.check(
        "用户的硬约束在超长历史下仍被保留（最早一条被钉住）",
        "ZEBRA" in built[1]["content"],
        "命中约束 ZEBRA" if "ZEBRA" in built[1]["content"] else "约束已被丢弃",
    )
    c.check("超长历史下系统提示词仍完整",
            built[0]["content"].startswith(cfg.by_id("captain").system_prompt[:20]))

    await check_context_priority(c, cfg)


async def check_context_priority(c: Checker, cfg) -> None:
    """预算紧张时，本轮消息必须优先于更早轮次（防回归）。

    构造：本轮两条（用户约束 + 一条组员发言）与更早轮次一条，三者长度相当，
    预算只够「钉住的本轮用户原话 + 其中一条」。若填充顺序写反（先消耗更早轮次），
    被裁掉的就是本轮发言——这条断言会立刻变红。
    """
    events: list[dict] = []

    async def sink(ev: dict) -> None:
        events.append(ev)

    # max_context_chars 故意取小值：_transcript 的预算有 400 字符下限，
    # 于是实际预算确定为 400，与 captain 提示词长度无关，断言可复现。
    engine = GroupEngine(cfg, LLMClient(cfg), sink, RunLimits(max_context_chars=200))
    engine.current_run_id = "RUN-B"
    body = "填" * 240  # 240 字符 + 渲染前缀，两条候选都放不下第二条
    engine.messages = [
        Message(id="old", sender="kimi", sender_name="Kimi", sender_avatar="🌙",
                role="member", content="OLD-RUN-MARKER " + body, run_id="RUN-A"),
        Message(id="cur-user", sender="user", sender_name="用户", sender_avatar="🧑",
                role="user", content="CUR-RUN-USER-MARKER 本轮约束", run_id="RUN-B"),
        Message(id="cur-speech", sender="kimi", sender_name="Kimi", sender_avatar="🌙",
                role="member", content="CUR-RUN-SPEECH-MARKER " + body, run_id="RUN-B"),
    ]
    built = await engine._build_messages(cfg.by_id("captain"), "优先级检查")
    content = built[1]["content"]
    trimmed = [e for e in events if e["kind"] == "context_trimmed"]
    c.check(
        "上下文预算紧张时本轮消息优先于更早轮次（本轮发言进入 prompt、上一轮被裁掉）",
        "CUR-RUN-SPEECH-MARKER" in content and "OLD-RUN-MARKER" not in content
        and "CUR-RUN-USER-MARKER" in content,
        f"本轮发言={'在' if 'CUR-RUN-SPEECH-MARKER' in content else '被裁'}、"
        f"本轮用户原话={'在' if 'CUR-RUN-USER-MARKER' in content else '被裁'}、"
        f"更早轮次={'被裁' if 'OLD-RUN-MARKER' not in content else '仍在'}"
        f"；context_trimmed dropped={trimmed[0]['dropped'] if trimmed else '无'}",
    )


async def check_no_mention(c: Checker, cfg) -> None:
    """mock_hint=quiet：组长这一轮不 @ 任何人 → 组员一个都不许被唤醒。"""
    engine, events = new_engine(cfg)
    await engine.handle_user_message("只让组长自己回答这句就行", mock_hint="quiet")
    member_msgs = [m for m in engine.messages if m.role == "member"]
    captain_msgs = [m for m in engine.messages if m.role == "captain"]
    recorder_msgs = [m for m in engine.messages if m.role == "recorder"]
    c.check("无人被 @ 时不唤醒组员", len(member_msgs) == 0 and len(captain_msgs) >= 1,
            f"组长 {len(captain_msgs)} 条 / 组员 {len(member_msgs)} 条")
    c.check("只有一条发言时不做无谓的汇总调用", len(recorder_msgs) == 0)
    c.check("无人被 @ 的这一轮也没有 error 事件", not [e for e in events if e["kind"] == "error"])


async def check_unknown_mention(c: Checker, cfg) -> None:
    engine, events = new_engine(cfg)
    await engine.handle_user_message("再演示一次派发规则", mock_hint="ghost")
    notices = [
        e for e in events
        if e["kind"] == "notice" and "不存在的成员" in e.get("message", "")
    ]
    errors = [e for e in events if e["kind"] == "error"]
    c.check("未知 @目标 只产生 notice，不报错", bool(notices) and not errors,
            notices[0]["message"][:72] if notices else "未产生 notice")


async def check_direct_mention(c: Checker, cfg) -> None:
    """用户点名组员时，组长不得入场发言（2026-10-13 用户要求）。

    背景：原先组长是每一轮的固定入口，用户 `@kimi` 也会先把组长叫起来转达一次，
    白烧一次组长调用、还多两条啰嗦气泡。修法是「点名组员且未点组长 → 直接叫组员」。

    这组用例是那条新路径唯一的回归网：happy round 是无 @ 提问，
    其它 @ 类用例走的是组长派发路径，都覆盖不到「跳过组长」。
    """
    # 1) 点名组员 → 组长零发言
    engine, _ = new_engine(cfg)
    await engine.handle_user_message("@kimi 在不在")
    speakers = [m.sender for m in engine.messages if m.role in ("captain", "member")]
    seen: list[str] = []
    for s in speakers:
        if s not in seen:
            seen.append(s)
    c.check(
        "点名组员时组长不出场，且被点名者确实发言",
        "captain" not in seen and "kimi" in seen,
        f"发言者={seen}",
    )

    # 2) 点名组长 → 组长照常出场
    engine2, _ = new_engine(cfg)
    await engine2.handle_user_message("@captain 帮忙看看这个方案")
    seen2 = []
    for m in engine2.messages:
        if m.role in ("captain", "member") and m.sender not in seen2:
            seen2.append(m.sender)
    c.check("点名组长时组长出场", "captain" in seen2, f"发言者={seen2}")

    # 3) 谁都没点 → 组长照常拆解派发
    engine3, _ = new_engine(cfg)
    await engine3.handle_user_message("帮我分析一下桌面应用的工程隐患")
    seen3 = []
    for m in engine3.messages:
        if m.role in ("captain", "member") and m.sender not in seen3:
            seen3.append(m.sender)
    c.check("无人被点时长仍由组长拆解派发", "captain" in seen3, f"发言者={seen3}")


async def check_no_captain_summary(c: Checker, cfg) -> None:
    """用户要求：组长**不再做自动收尾总结**，它只在拆解派活时活跃。

    背景：原先每轮组员发言后，组长都会被无条件叫起来做一次「收敛/定稿」，
    结果是用户 @ 谁问一句，后面还要多挂一条组长的长篇总结 —— 既啰嗦又白烧一次调用。
    现在只有组员**明确 @ 了组长**时才让它起来回应。
    """
    engine, _ = new_engine(cfg)
    await engine.handle_user_message("帮我分析一下桌面应用的工程隐患")
    caps = [m for m in engine.messages if m.role == "captain"]
    members = [m for m in engine.messages if m.role == "member"]
    c.check(
        "无人 @ 组长时，组长只开场拆解一次（不再自动收尾总结）",
        len(caps) == 1,
        f"组长 {len(caps)} 条 / 组员 {len(members)} 条",
    )
    c.check(
        "组员确实被派发起来了（不是把整轮砍掉）",
        len(members) >= 1,
        f"组员 {len(members)} 条",
    )


async def check_degraded_resolve(c: Checker, cfg) -> None:
    """主路由未声明（等价于缺密钥）→ 自动换到 fallback，并在事件流里标注降级。"""
    captain = cfg.by_id("captain")
    saved = (captain.provider, captain.model, captain.fallback)
    captain.provider, captain.model = "no-such-provider", "ghost-model"
    captain.fallback = {"provider": "mock", "model": "mock-fast"}
    try:
        engine, events = new_engine(cfg)
        await asyncio.wait_for(
            engine.handle_user_message("降级路径演示：主路由不可用时怎么办"), timeout=60
        )
        await _assert_degraded(c, events, engine, "no-such-provider/ghost-model")
    finally:
        captain.provider, captain.model, captain.fallback = saved


async def check_degraded_call_failure(c: Checker, cfg) -> None:
    """主路由可解析、但调用真的失败（连不上）→ 同样必须降级且可见。"""
    # 自检用的假密钥：不是任何真实凭据，只是为了让「非 mock provider」能通过密钥存在性检查
    os.environ["AIGROUP_SELFTEST_KEY"] = "selftest-placeholder-not-a-real-key"
    cfg.providers["selftest-dead"] = ProviderSpec(
        id="selftest-dead",
        label="自检用不可达 provider",
        api="openai-chat",
        base_url="http://127.0.0.1:1/v1",  # 本机必然无人监听 → 连接立即失败
        credentials=[{"env": "AIGROUP_SELFTEST_KEY"}],
        models={"dead-model": ModelSpec(id="dead-model", label="dead")},
    )
    captain = cfg.by_id("captain")
    saved = (captain.provider, captain.model, captain.fallback)
    captain.provider, captain.model = "selftest-dead", "dead-model"
    captain.fallback = {"provider": "mock", "model": "mock-fast"}
    try:
        engine, events = new_engine(cfg)
        await asyncio.wait_for(
            engine.handle_user_message("降级路径演示：上游调用失败时怎么办"), timeout=60
        )
        await _assert_degraded(c, events, engine, "selftest-dead/dead-model")
    finally:
        captain.provider, captain.model, captain.fallback = saved
        cfg.providers.pop("selftest-dead", None)


async def _assert_degraded(c: Checker, events: list[dict], engine, requested: str) -> None:
    warnings = [e for e in events if e["kind"] == "notice" and e.get("level") == "warn"]
    routes = [e for e in events if e["kind"] == "route_changed"]
    captain_msgs = [m for m in engine.messages if m.role == "captain"]
    errors = [e for e in events if e["kind"] == "error"]
    c.check(f"降级可见：{requested} → 事件流给出 warn notice", bool(warnings),
            warnings[0]["message"][:88] if warnings else "无")
    c.check("降级可见：route_changed 事件标注 requested/actual 路由",
            bool(routes) and routes[0]["requested_route"] == requested
            and routes[0]["route"] == "mock/mock-fast" and routes[0]["degraded"] is True,
            f"{routes[0]['requested_route']} → {routes[0]['route']}" if routes else "无")
    c.check("降级可见：发言消息自带 warning 与真实 route（界面气泡可标注）",
            bool(captain_msgs) and captain_msgs[0].route == "mock/mock-fast"
            and captain_msgs[0].requested_route == requested and bool(captain_msgs[0].warning),
            f"route={captain_msgs[0].route} warning={captain_msgs[0].warning[:40]}" if captain_msgs else "无")
    c.check("降级后这一轮仍然跑完（未抛 error，且组长确实发言）",
            not errors and bool(captain_msgs))

    summaries = [e for e in events if e["kind"] == "route_summary"]
    shared = summaries[0]["shared_providers"] if summaries else {}
    c.check(
        "同源可见：降级后若多个角色落在同一 provider，事件流明确报出",
        bool(summaries) and bool(shared) and "同源" in summaries[0].get("message", ""),
        summaries[0]["message"][:96] if summaries else "没有 route_summary 事件",
    )
    c.check(
        "同源可见：route_summary 带结构化分布（供界面/纪要引用）",
        bool(summaries) and isinstance(summaries[0].get("providers"), dict)
        and summaries[0].get("degraded_count", 0) >= 1,
        f"providers={summaries[0].get('providers')} degraded={summaries[0].get('degraded_count')}" if summaries else "无",
    )


async def check_limits(c: Checker, cfg) -> None:
    """口径（与 README 一致）：max_messages 只约束「派发链」的**尝试次数**，
    记录员纪要不占该配额，但一轮最多一次，所以模型调用总数上界 = max_messages + 1。"""
    limits = RunLimits(max_messages=2, max_depth=1, max_context_chars=9000)
    engine, events = new_engine(cfg, limits)
    await engine.handle_user_message("额度护栏验证：这轮最多只允许两次发言")
    dispatch = [m for m in engine.messages if m.role in ("captain", "member")]
    recorder = [m for m in engine.messages if m.role == "recorder"]
    stats = engine.run_stats()
    c.check(
        "单轮派发链发言上限 max_messages=2 生效（记录员不计入）",
        stats["dispatch_calls"] <= 2 and len(dispatch) <= 2,
        f"尝试 {stats['dispatch_calls']} 次 / 成功 {stats['dispatch_speaks']} 条："
        f"{'、'.join(m.sender for m in dispatch)}；记录员额外 {len(recorder)} 条",
    )
    c.check("记录员纪要不占 max_messages 配额，但仍会受到「一轮最多一次」约束",
            len(recorder) <= 1 and stats["recorder_calls"] <= 1,
            f"recorder_calls={stats['recorder_calls']}")
    c.check(
        "单轮模型调用总数上界 = max_messages + 1",
        stats["model_calls_per_run"] <= limits.max_messages + 1,
        f"{stats['model_calls_per_run']} ≤ {limits.max_messages + 1}"
        f"（尝试 {stats['dispatch_calls']} + 纪要 {stats['recorder_calls']}）",
    )

    # 深度上限：max_depth=1 时不允许出现「组员再 @ 组员」的第二层
    depth2 = [m for m in engine.messages if m.role == "member"]
    c.check("@ 触发深度上限 max_depth 生效（不超过配置层数）",
            len(depth2) <= limits.max_depth * 2, f"组员发言 {len(depth2)} 条")


async def check_dedupe_and_matching(c: Checker, cfg) -> None:
    """D1/D4 复现用例：重复 @ 不得超额唤醒；弱 token 不得误命中真唤醒。"""
    engine, _events = new_engine(cfg)
    fake = (
        "【拆解】1. 论证 → @kimi；2. 复核 → @论证担当；3. 再补一条 → @论证；"
        "4. 顺手抄送 → @i 与 @k 与 @demo；5. 引用邮箱 → 请联系 zhang@kimi 复核。"
    )
    tokens = extract_mentions(fake)
    matched = [a.id for a, _reason in (engine.resolve_mention_detail(t) for t in tokens) if a]
    c.check(
        "D1 复现：@kimi / @论证担当 / @论证 三种写法只解析出同一个成员",
        set(matched) == {"kimi"},
        f"tokens={tokens} → {matched}",
    )
    weak = ["i", "k", "demo", "组"]
    weak_hits = {w: (engine.resolve_mention(w).id if engine.resolve_mention(w) else None) for w in weak}
    c.check(
        "D4 复现：@i / @k / @demo / @组 都不再误命中成员（短 token 不参与子串匹配）",
        all(v is None for v in weak_hits.values()),
        f"{weak_hits}",
    )
    c.check(
        "D4 复现：正文里的邮箱 zhang@kimi 不会被当成 @提及",
        [t.lower() for t in tokens].count("kimi") == 1,
        f"抽取结果 {tokens}",
    )
    tag_hit, tag_reason = engine.resolve_mention_detail("组员")
    c.check(
        "D4 复现：@组员（已不再是任何成员的标签）不静默命中，且给出原因",
        tag_hit is None and bool(tag_reason),
        tag_reason,
    )
    # 歧义路径仍然要覆盖：临时让两个成员共用标签，必须报歧义而不是选第一个。
    # 2026-10-13：原先借 demo-bot 当夹具，但交付配置里已删掉它，改用 doubao
    # （cfg_mock 是深拷贝，改完会在 finally 里还原，不影响其它用例）。
    victim = cfg.by_id("doubao")
    saved_tags = list(victim.tags)
    victim.tags = ["论证担当"]  # 与 kimi 同标签
    try:
        ambiguous, why = engine.resolve_mention_detail("论证担当")
        c.check(
            "D4 复现：标签重名时报歧义（要求显式 id），不静默选第一个",
            ambiguous is None and "歧义" in why,
            why,
        )
    finally:
        victim.tags = saved_tags
    all_tags: list[str] = []
    for agent in [*cfg.active_agents, cfg.recorder]:
        all_tags.extend(tag.lower() for tag in agent.tags)
    dupes = sorted({tag for tag in all_tags if all_tags.count(tag) > 1})
    c.check(
        "配置不变量：成员标签全局唯一（不给模型留走不通的 @地址）",
        not dupes,
        f"重复标签 {dupes}" if dupes else f"{len(all_tags)} 个标签均唯一",
    )

    # 队列去重：三种写法只唤醒 kimi 一次
    engine2, _events2 = new_engine(cfg)
    wake: list[str] = []
    calls = {"captain": 0}

    async def fake_speak(agent, task, run_id):
        wake.append(agent.id)
        if agent.role == "captain":
            calls["captain"] += 1
            if calls["captain"] == 1:  # 只有第一棒（拆解）才 @ 人
                return ["kimi", "论证担当", "论证"], True
            return [], True
        return [], True

    engine2._speak = fake_speak  # type: ignore[assignment]
    await engine2._dispatch_loop("selfcheck-run", seed_task="自检：重复 @ 去重")
    kimi_wakes = wake.count("kimi")
    c.check(
        "D1 复现：同一个成员被三种写法 @ 时，一轮只被唤醒一次（max_turns_per_run 不被突破）",
        kimi_wakes == 1 and wake.count("captain") >= 1,
        f"唤醒序列 {wake}（kimi {kimi_wakes} 次）",
    )
    c.check(
        "D1 复现：重复 @ 会给出「已合并」notice（不静默）",
        any("合并为一次派发" in e.get("message", "") for e in _events2 if e["kind"] == "notice"),
        "、".join(e["message"] for e in _events2 if e["kind"] == "notice")[:120] or "无",
    )

    # F2：@组长 过去是被静默丢弃的，现在必须发 notice
    engine3, events3 = new_engine(cfg)
    first = {"done": False}

    async def fake_speak3(agent, task, run_id):
        if agent.role == "captain" and not first["done"]:
            first["done"] = True
            return ["captain", "组长"], True  # 组长 @ 自己
        return [], True

    engine3._speak = fake_speak3  # type: ignore[assignment]
    await engine3._dispatch_loop("selfcheck-f2", seed_task="F2 自检：@组长")
    notices = [e.get("message", "") for e in events3 if e["kind"] == "notice"]
    c.check(
        "F2 复现：@组长 不再被静默丢弃，事件流给出原因 notice",
        any("指向组长" in n for n in notices),
        "、".join(notices)[:130] or "无 notice",
    )


async def check_failed_calls_do_not_eat_quota(c: Checker, cfg) -> None:
    """D6/F1：失败调用计入花费上界，但不能算成「这个人发过言」，且必须对纪要可见。

    2026-10-13：夹具原先是 demo-bot（交付配置里已删）。改用 doubao ——
    它是 mock 组长派发名单里的前两位之一（_roster_ids），一定会被唤醒，
    因此「让它必然失败」这条路径仍然被测到。
    """
    victim = cfg.by_id("doubao")
    saved = (victim.provider, victim.model, victim.fallback)
    victim.provider, victim.model, victim.fallback = "no-such-provider", "ghost-model", None
    try:
        engine, events = new_engine(cfg, RunLimits(max_messages=6, max_depth=1))
        await engine.handle_user_message("D6 自检：某成员一直失败")
        stats = engine.run_stats()
        failures = [e for e in events if e["kind"] == "message_end" and e.get("error")]
        c.check(
            "D6：失败调用计入尝试次数，但不算成功发言",
            stats["dispatch_calls"] >= 2 and stats["failed_calls"] >= 1
            and stats["member_success_counts"].get("doubao", 0) == 0,
            f"尝试 {stats['dispatch_calls']} 次 / 成功 {stats['dispatch_speaks']} 次 / "
            f"失败 {stats['failed_calls']} 次 / doubao 成功 "
            f"{stats['member_success_counts'].get('doubao', 0)} 次",
        )
        c.check(
            "D6：失败成员不会被误报「已达发言上限」",
            not any("已达上限" in e.get("message", "") for e in events if e["kind"] == "notice"),
            "、".join(e["message"] for e in events if e["kind"] == "notice")[:120] or "无 notice",
        )
        c.check(
            "F1：失败成员在事件流里有带 error 的 message_end 可感知",
            bool(failures),
            failures[0]["error"][:80] if failures else "无",
        )
        summary = engine.route_summary()
        c.check(
            "F1：route_summary 把失败成员单列，不记为正常参与",
            bool(summary["failed"]),
            summary["text"][:130],
        )
    finally:
        victim.provider, victim.model, victim.fallback = saved


async def check_all_providers_dead(c: Checker, cfg) -> None:
    """F5：全部路由不可用时必须有可判定的失败信号（顶层 error + run_end.aborted）。"""
    snapshot = {a.id: (a.provider, a.model, a.fallback) for a in [*cfg.agents, cfg.recorder]}
    for agent in [*cfg.agents, cfg.recorder]:
        agent.provider, agent.model, agent.fallback = "no-such-provider", "ghost-model", None
    try:
        engine, events = new_engine(cfg, RunLimits(max_messages=3, max_depth=1))
        await engine.handle_user_message("F5 自检：所有厂商都不可用")
        errors = [e for e in events if e["kind"] == "error"]
        run_end = next((e for e in events if e["kind"] == "run_end"), None)
        c.check(
            "F5：全部路由不可用时给出顶层 error 事件（可判定，而不是一条不起眼的 notice）",
            bool(errors),
            errors[0]["message"][:110] if errors else "没有 error 事件",
        )
        c.check("F5：run_end 明确标记 aborted=true",
                bool(run_end) and run_end.get("aborted") is True, f"run_end={run_end}")
    finally:
        for agent in [*cfg.agents, cfg.recorder]:
            agent.provider, agent.model, agent.fallback = snapshot[agent.id]


async def check_http_routes(c: Checker) -> None:
    """把每个 GET 路由都**真的调一遍**，断言不出现 5xx。

    补这条的原因：/api/health 里一个变量名写错就返回 500，
    前端启动时调它失败，整个界面起不来 —— 而自检 78/78 全过。
    因为自检只测引擎，**从来没碰过 HTTP 层**。

    「所有单元都对，程序却打不开」就是这么发生的。
    """
    c.section("HTTP 路由（每个都要能调通，不能 5xx）")
    try:
        from group.app import app
    except Exception as exc:  # noqa: BLE001
        c.check("能导入 app", False, f"{type(exc).__name__}: {exc}")
        return

    routes = []
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if "GET" not in methods or not path.startswith("/api/"):
            continue
        routes.append(path)

    c.check("发现 API 路由", len(routes) > 5, f"{len(routes)} 个")

    if True:
        for path in sorted(routes):
            # 带路径参数的路由，塞个占位值 —— 没有该实体时应当 404/400，
            # 但**绝不能是 500**（500 = 代码崩了，不是数据没有）
            call = path
            if "{" in path:
                call = re.sub(r"\{[^}]+\}", "___not_exist___", path)
            try:
                status = await _call_route(app, call)
                ok = status < 500
                note = "" if ok else "服务端崩溃"
            except Exception as exc:  # noqa: BLE001
                ok = False
                status = "异常"
                note = f"{type(exc).__name__}: {exc}"
            c.check(f"GET {path}", ok, f"{status} {note}".strip())


async def _call_route(app, path: str) -> int:
    """用 ASGI 传输层直接调一次路由，拿到状态码。"""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": [(b"host", b"127.0.0.1")],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
    }
    status = {"code": 0}
    sent = {"done": False}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            sent["done"] = True

    await app(scope, receive, send)
    return status["code"]


def check_no_plaintext_secrets(c: Checker) -> None:
    secret_re = re.compile(r"(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9_\-]{24,})")
    scanned = 0
    hits: list[str] = []
    for path in sorted(PROJECT_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".py", ".yaml", ".yml", ".md", ".js", ".html", ".css", ".ps1"}:
            continue
        if any(part in {".venv", "__pycache__", ".git"} for part in path.parts):
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        if secret_re.search(text):
            hits.append(str(path.relative_to(PROJECT_ROOT)))
    c.check(
        "密钥零落盘：代码与配置里没有明文密钥（全部走 config.resolve_credential）",
        not hits,
        f"扫描 {scanned} 个文件，命中 {hits or '无'}",
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
async def guarded(c: Checker, name: str, coro) -> None:
    """单项检查的兜底：任何异常都记成 FAIL，绝不把整份自检炸掉。

    超时预算按模式区分：mock 通道秒级完成，120s 足够；
    真实模式受上游限流拖累（例如 Moonshot 组织级 3 RPM，一次 429 就会退避重试），
    整轮主链路可能要好几分钟，因此给更宽的预算，并允许用 SMOKE_REAL_TIMEOUT 覆盖。
    """
    try:
        await asyncio.wait_for(coro, timeout=CHECK_TIMEOUT)
    except asyncio.TimeoutError:
        c.check(f"{name}（整体超时 {CHECK_TIMEOUT}s）", False, "该检查超时，已放弃并继续其它检查")
    except Exception as exc:  # noqa: BLE001 - 自检要兜住一切
        c.check(f"{name}（异常）", False, f"{type(exc).__name__}: {exc}")


def guarded_sync(c: Checker, name: str, func) -> None:
    try:
        func()
    except Exception as exc:  # noqa: BLE001
        c.check(f"{name}（异常）", False, f"{type(exc).__name__}: {exc}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock-only", action="store_true", help="全部走离线 mock 通道（零额度）")
    parser.add_argument(
        "--question",
        default="对比 AG2 与自研群聊内核的取舍，并给出两条风险",
        help="要投喂给群聊的问题",
    )
    args = parser.parse_args()

    mock_only = args.mock_only or os.environ.get("SMOKE_MOCK_ONLY") == "1"
    if args.mock_only:
        os.environ["SMOKE_MOCK_ONLY"] = "1"

    # 单项检查的超时预算：真实模式要留足上游限流重试的时间
    global CHECK_TIMEOUT
    if mock_only:
        CHECK_TIMEOUT = 120.0
    else:
        CHECK_TIMEOUT = float(os.environ.get("SMOKE_REAL_TIMEOUT", "2400"))

    import copy

    cfg = load_config()
    # 先记下「交付配置的原始状态」，再在测试副本上开记录员 —— 两件事都要测：
    #   ① 交付配置里确实没有离线兜底、记录员默认关闭（用户要求）；
    #   ② 引擎的「记录员汇总」能力本身没被改坏（下面的 check_happy_round 等仍要覆盖它）。
    shipped = {
        "has_demo": any(a.id == "demo-bot" for a in cfg.agents),
        "recorder_enabled": bool(cfg.recorder.enabled),
        "members": [a.id for a in cfg.agents if a.enabled and not a.is_recorder],
    }
    cfg.recorder.enabled = True
    if mock_only:
        force_mock(cfg)
    # 真实模式下：只有主链路那一轮用真实模型（真的花钱、真的慢），
    # 所有边界用例一律跑在离线副本上——既省额度，也不受上游限流影响。
    cfg_mock = copy.deepcopy(cfg)
    force_mock(cfg_mock)

    client = LLMClient(cfg)
    engine = GroupEngine(cfg, client, lambda ev: asyncio.sleep(0), RunLimits())
    engine_mock = GroupEngine(cfg_mock, LLMClient(cfg_mock), lambda ev: asyncio.sleep(0), RunLimits())

    c = Checker()
    print("=" * 78)
    print("AI 群聊内核 · 端到端自检" + ("（离线 mock 通道，零额度）" if mock_only else "（真实配置）"))
    print(f"单项检查超时预算：{CHECK_TIMEOUT:.0f}s"
          + ("" if mock_only else "（真实模式受上游限流影响，请耐心等待；可用 SMOKE_REAL_TIMEOUT 调整）"))
    print("=" * 78)

    guarded_sync(c, "配置与不变量", lambda: check_config(c, cfg, engine.active_agents))

    c.section("交付配置：成员名单符合用户要求")
    c.check(
        "已删除离线兜底成员（demo-bot）",
        not shipped["has_demo"],
        f"当前成员 id={shipped['members']}",
    )
    c.check(
        "记录员默认关闭（不再每轮额外调一次模型写纪要）",
        not shipped["recorder_enabled"],
        f"recorder.enabled={shipped['recorder_enabled']}",
    )
    c.check(
        "可见成员恰好 3 位（DeepSeek 组长 + Kimi + 豆包）",
        shipped["members"] == ["captain", "kimi", "doubao"],
        str(shipped["members"]),
    )
    guarded_sync(c, "AG2 适配层映射", lambda: check_ag2_mapping(c, cfg, client))

    c.section("provider 凭据解析（只报有无，不打印密钥）")
    for item in probe_availability(cfg):
        print(f"    {item.provider:<16} {'可用' if item.ok else '不可用':<4} {item.detail}")

    c.section("@提及 解析")
    guarded_sync(c, "@提及 解析", lambda: check_mentions(c, engine_mock))

    c.section(f"跑一轮完整群聊（{'离线 mock' if mock_only else '真实模型'}）")
    await guarded(c, "完整群聊轮次", check_happy_round(c, cfg_mock if mock_only else cfg, args.question))

    c.section("边界：无人被 @ / 未知 @目标（离线，零额度）")
    await guarded(c, "无人被 @", check_no_mention(c, cfg_mock))
    await guarded(c, "未知 @目标", check_unknown_mention(c, cfg_mock))
    c.section("用户点名组员：组长不出场（离线，零额度）")
    await guarded(c, "点名派发", check_direct_mention(c, cfg_mock))
    await guarded(c, "组长不做收尾总结", check_no_captain_summary(c, cfg_mock))

    c.section("降级路径（绝不静默换模型，离线，零额度）")
    await guarded(c, "降级：主路由不可用", check_degraded_resolve(c, cfg_mock))
    await guarded(c, "降级：调用失败", check_degraded_call_failure(c, cfg_mock))

    c.section("D1/D4 复现用例：重复 @ 与弱 token（离线）")
    await guarded(c, "D1/D4 复现", check_dedupe_and_matching(c, cfg_mock))

    c.section("D6/F1/F5：失败调用的计数、可见性与失败信号（离线）")
    await guarded(c, "失败调用计数", check_failed_calls_do_not_eat_quota(c, cfg_mock))
    await guarded(c, "全部路由不可用", check_all_providers_dead(c, cfg_mock))

    c.section("额度护栏（离线）")
    await guarded(c, "额度护栏", check_limits(c, cfg_mock))

    c.section("密钥与配置卫生")
    await guarded(c, "HTTP 路由", check_http_routes(c))
    guarded_sync(c, "密钥零落盘", lambda: check_no_plaintext_secrets(c))

    print("\n" + "=" * 78)
    if c.failures:
        print(f"结果：{c.total - len(c.failures)}/{c.total} 通过，{len(c.failures)} 项未通过 —— {', '.join(c.failures)}")
        return 1
    print(f"结果：全部通过（{c.total} 项检查）")
    return 0


class _Tee:
    """把自检输出同时写到控制台与 UTF-8 证据文件。

    存在理由：Windows 下用 `>` 重定向会把 UTF-8 字节按控制台代码页重新编码，
    证据文件直接变成乱码（PowerShell 默认还会写成 UTF-16），
    于是「留档的输出」反而不可读、也无法被验证者复核。
    这里显式以 UTF-8 落盘，并在结束时附上退出码。
    """

    def __init__(self, stream, file):
        self._stream = stream
        self._file = file

    def write(self, text: str) -> int:
        self._stream.write(text)
        self._file.write(text)
        return len(text)

    def flush(self) -> None:
        self._stream.flush()
        self._file.flush()

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name):
        return getattr(self._stream, name)


def run() -> int:
    """顶层兜底：任何未捕获异常都必须产出总结行 + 确定性退出码。"""
    # 强制 UTF-8 输出：避免 GBK 控制台上 print 非 ASCII 字符直接抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 环境不支持时继续，不影响判定
        pass

    log_path = os.environ.get("SMOKE_LOG")
    tee_file = None
    original_stdout = sys.stdout
    tee_installed = False
    if log_path:
        try:
            tee_file = open(log_path, "w", encoding="utf-8", newline="")
            sys.stdout = _Tee(original_stdout, tee_file)
            tee_installed = True
        except OSError:
            tee_file = None

    code = 1
    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        print("\n结果：自检被中断（KeyboardInterrupt）—— 未通过")
        code = 130
    except BaseException as exc:  # noqa: BLE001 - 绝不允许「没有结论行」的输出
        import traceback

        print("\n" + "=" * 78)
        print(f"结果：自检崩溃，未通过 —— {type(exc).__name__}: {exc}")
        traceback.print_exc()
        code = 1
    finally:
        if tee_installed:
            print(f"\nEXIT={code}")
            sys.stdout.flush()
            sys.stdout = original_stdout
            if tee_file is not None:
                tee_file.close()
    return code


if __name__ == "__main__":
    raise SystemExit(run())
