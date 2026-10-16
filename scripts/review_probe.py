"""评审用独立探针（checker，不属于实现）：把 README/自检声明与代码实际行为逐条对照。

用法（在 ai-group 目录下；HTTP 部分需要先起一个 mock-only 服务）::

    ..\\campus-market\\backend\\.venv\\Scripts\\python.exe scripts\\review_probe.py --port 8804

只读实现，不修改任何 group/web/config 文件。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from group.config import check_invariants, extract_mentions, force_mock, load_config  # noqa: E402
from group.engine import GroupEngine, Message, RunLimits  # noqa: E402
from group.llm import LLMClient  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} —— {detail}")


def make_engine(cfg, limits=None):
    events: list[dict] = []

    async def sink(ev):
        events.append(ev)

    return GroupEngine(cfg, LLMClient(cfg), sink, limits or RunLimits()), events


def probe_transcript_priority(cfg) -> None:
    """README §5/engine._transcript docstring：窗口优先用本轮消息，有富余才带回更早轮次。"""
    eng, _ = make_engine(cfg)
    eng.current_run_id = "RUN-B"
    eng.messages = [
        Message(id="earlier", sender="kimi", sender_name="K", sender_avatar="🌙", role="member",
                content="EARLIER-RUN " + "E" * 190, run_id="RUN-A"),
        Message(id="b-user", sender="user", sender_name="用户", sender_avatar="🧑", role="user",
                content="CURRENT-USER " + "U" * 40, run_id="RUN-B"),
        Message(id="b-member", sender="kimi", sender_name="K", sender_avatar="🌙", role="member",
                content="CURRENT-MEMBER " + "M" * 190, run_id="RUN-B"),
    ]
    text, dropped, truncated = eng._transcript(300)
    kept_current = "CURRENT-MEMBER" in text
    kept_earlier = "EARLIER-RUN" in text
    report(
        "README §5『窗口优先用本轮消息，有富余才带回更早轮次』",
        kept_current and not kept_earlier,
        f"budget=300：本轮成员发言{'保留' if kept_current else '被丢弃'}、"
        f"更早轮次发言{'保留' if kept_earlier else '被丢弃'}（dropped={dropped}）；"
        f"engine.py:251 `rest = earlier + head_pool[1:]` + :262 `reversed(rest)` 使本轮消息先占用预算",
    )


def probe_mention_rules(cfg) -> None:
    eng, _ = make_engine(cfg)
    cases = {
        "@Kimi·论证担当": "kimi",
        "@kimi": "kimi",
        "@论证担当": "kimi",
        "@k": None,
        "@demo": None,
        "@不存在的名字": None,
    }
    bad = []
    for token, expected in cases.items():
        got = eng.resolve_mention(token.lstrip("@"))
        if (got.id if got else None) != expected:
            bad.append(f"{token}->{got.id if got else None}(期望 {expected})")
    report(
        "@ 解析规则（最小长度/歧义拒绝）与 README §5 一致",
        not bad,
        f"用例结果 {bad or '全部符合'}；MIN_SUBSTRING_LEN={eng.MIN_SUBSTRING_LEN}（engine.py:123,154-158）",
    )

    emails = ["请联系 zhang@kimi 复核", "邮箱 a@b.com 不是提及", "(@kimi)"]
    got = [extract_mentions(t) for t in emails]
    report(
        "正文里的邮箱不会被当成 @提及（`@` 前需行首/空白/标点）",
        got[0] == [] and got[1] == [] and got[2] == ["kimi"],
        f"extract_mentions 结果={got}；config.py:263-267 的 lookbehind",
    )


def probe_invariants(cfg) -> None:
    problems = check_invariants(cfg)
    tags: dict[str, list[str]] = {}
    seen_ids: set[int] = set()
    for agent in [*cfg.agents, cfg.recorder]:
        if id(agent) in seen_ids:   # recorder 同时在 cfg.agents 与 cfg.recorder 里，按对象去重
            continue
        seen_ids.add(id(agent))
        for tag in agent.tags:
            tags.setdefault(tag, []).append(agent.id)
    dupes = {t: ids for t, ids in tags.items() if len(ids) > 1}
    report(
        "models.yaml 标签全局唯一 + check_invariants 无违规（fallback 异厂）",
        not problems and not dupes,
        f"check_invariants={problems or '无违规'}；重复标签={dupes or '无'}"
        f"（注意：标签唯一性目前只由注释约束，不在 check_invariants 里做代码校验）",
    )


def probe_secrets() -> None:
    secret_re = re.compile(r"(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9_\-]{24,})")
    hits = []
    for path in sorted(PROJECT_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".py", ".yaml", ".yml", ".md", ".js", ".html", ".css", ".ps1"}:
            continue
        if any(part in {".venv", "__pycache__", ".git"} for part in path.parts):
            continue
        if secret_re.search(path.read_text(encoding="utf-8", errors="replace")):
            hits.append(str(path.relative_to(PROJECT_ROOT)))
    report("代码与配置零明文密钥", not hits, f"命中={hits or '无'}")


async def probe_failure_visibility(cfg) -> None:
    """失败成员：留痕 + route_summary.failed + 纪要任务里点明缺口。"""
    from group.config import ModelSpec, ProviderSpec

    import os

    os.environ["AIGROUP_REVIEW_KEY"] = "review-placeholder"
    cfg.providers["review-dead"] = ProviderSpec(
        id="review-dead", label="评审不可达", api="openai-chat",
        base_url="http://127.0.0.1:1/v1",
        credentials=[{"env": "AIGROUP_REVIEW_KEY"}],
        models={"dead": ModelSpec(id="dead")},
    )
    force_mock(cfg)
    kimi = cfg.by_id("kimi")
    saved = (kimi.provider, kimi.model, kimi.fallback)
    kimi.provider, kimi.model, kimi.fallback = "review-dead", "dead", None
    try:
        eng, events = make_engine(cfg)
        await eng.handle_user_message("评审：组员异常路径")
        failed_msgs = [m for m in eng.messages if m.error]
        summary = eng.route_summary()
        rec_task_has_gap = bool(summary.get("failed"))
        report(
            "失败成员对纪要可见（留 Message + route_summary.failed + 不冒充参与）",
            bool(failed_msgs) and rec_task_has_gap and summary["failed"][0]["route"] == "review-dead/dead",
            f"带 error 的 Message={len(failed_msgs)} 条；route_summary.failed={[f['name'] for f in summary['failed']]}；"
            f"正常参与列表={[r['name'] for r in summary['routes']]}",
        )

        # 全厂商挂掉：顶层 error + run_end{aborted:true}
        for agent in cfg.agents:
            agent.provider, agent.model, agent.fallback = "review-dead", "dead", None
        eng2, events2 = make_engine(cfg)
        await eng2.handle_user_message("评审：全厂商不可用")
        top_errors = [e for e in events2 if e["kind"] == "error"]
        run_end = [e for e in events2 if e["kind"] == "run_end"]
        spoke = sum(eng2.run_stats()["member_success_counts"].values())
        report(
            "全厂商不可用时给出顶层 error + run_end{aborted:true}（不假装成功）",
            bool(top_errors) and bool(run_end) and run_end[0].get("aborted") is True and spoke == 0,
            f"error 事件={len(top_errors)} 条，run_end.aborted={run_end[0].get('aborted') if run_end else '?'}，"
            f"成功发言={spoke}",
        )
    finally:
        kimi.provider, kimi.model, kimi.fallback = saved
        cfg.providers.pop("review-dead", None)


def http_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def probe_http(port: int) -> None:
    base = f"http://127.0.0.1:{port}"
    snap = http_json(f"{base}/api/snapshot")
    has_run_stats = "run_stats" in snap
    has_route_summary = "route_summary" in snap
    report(
        "README §7 声称 /api/snapshot 含 run_stats 与 route_summary",
        has_run_stats and has_route_summary,
        f"实际 /api/snapshot 顶层键={sorted(snap.keys())}；"
        f"run_stats={'有' if has_run_stats else '无'}，route_summary={'有' if has_route_summary else '无'}"
        f"（app.py:89-112 已把 ENGINE.run_stats()/route_summary() 放进响应）",
    )

    diag = http_json(f"{base}/api/diagnostics")
    blob = json.dumps(diag, ensure_ascii=False)
    leaked = bool(re.search(r"sk-[A-Za-z0-9]{16,}", blob))
    report(
        "/api/diagnostics 暴露调用记录但不泄露密钥",
        "call_log" in diag and not leaked,
        f"call_log {len(diag.get('call_log') or [])} 条，密钥前缀命中={leaked}",
    )

    health = http_json(f"{base}/api/health")
    report(
        "README §7 声称 /api/health 含额度与配置不变量",
        "limits" in health and "invariants" in health,
        f"limits={'有' if 'limits' in health else '无'}，invariants={'有' if 'invariants' in health else '无'}",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0, help="已运行的 mock-only 服务端口（做 HTTP 项检查）")
    args = parser.parse_args()

    cfg = load_config()
    probe_transcript_priority(cfg)
    probe_mention_rules(cfg)
    probe_invariants(cfg)
    probe_secrets()
    asyncio.run(probe_failure_visibility(cfg))
    if args.port:
        probe_http(args.port)

    print("\n" + "=" * 78)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"结果：{len(RESULTS)-len(failed)}/{len(RESULTS)} 通过" + (f"，未通过：{failed}" if failed else " ALL PASS"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
