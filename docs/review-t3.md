# t3 评审报告：ai-group 是否真实满足契约与工程可靠性要求

- 评审人：checker（独立于实现者）
- 被评审任务：t1（实现）
- 评审修订（评审前后哈希一致，未改动任何实现文件）：

| 文件 | SHA256 前 16 位 |
|---|---|
| group/engine.py | F3297505A21A29B7 |
| group/llm.py | A4B8A224B1BEBEA4 |
| group/app.py | A0D72031EDEF983F |
| group/config.py | 53F3E55B76A52533 |
| group/ag2_adapter.py | B109CFEE6465F0F9 |
| config/providers.yaml | ECDFA993CA3F4881 |
| config/models.yaml | 1BFEB365FFF96911 |
| README.md | BCBB04C40F199A6F |
| web/app.js / index.html / style.css | 759A1838A36918D2 / 9F7F9173FA5FCC74 / F039C520244BFBA5 |
| config/roles/*.md | D9D273053750D264 / D7ABDED3C87F5B9…（5 个角色文件） |

> 说明：本次评审是**新的一次修订冻结**（README 已于 22:01:59 重写）。
> checker 在 t2 里预判的旧 README 问题（llm-pi-ai 凭据、可配置并发、无密钥也能跑完整链路、
> doubao 默认未启用等）**在当前磁盘上已全部不成立**，因此不作为本次 findings。

- 契约 verify：`smoke_test.py --mock-only` → **EXIT=0，64/64 PASS**（`docs/review-smoke-mock-only.txt`）
- 独立探针：`scripts/review_probe.py` 8/10、`scripts/http_probe.py` 3/3、`scripts/ws_probe.py` 9/9
  （原始输出 `docs/review-probe.txt`、`docs/review-http-8804.txt`、`docs/review-ws-8804.txt`）

---

## 裁决：needs_revision（不通过）

t1 的 8 条验收标准里 **第 8 条（README 与实现一致）failed**，其余 7 条 passed。
两条具体、可修、且相互独立的「文档写有、代码没有」：

### F1（medium）README §5 声称的上下文窗口优先级与代码相反

- 文档：`README.md:170-171`「**本轮用户原话优先保留**……窗口优先用本轮消息、有富余才带回更早轮次」；
  `group/engine.py:233-234` 的 docstring 同样声明「优先用本轮消息」。
- 代码：`group/engine.py:248` `rest = [item for item in head_pool[1:]] + earlier`，
  `:259` `for _run, mid, text in reversed(rest)`。
  这个拼接顺序 + `reversed` 让**更早轮次**的消息先占用预算，本轮消息排在其后。
- 实测（`review_probe.py`，budget=300，同一 engine 里 1 条 RUN-A 消息 + 本轮 user + 本轮 member）：
  ```
  [FAIL] README §5『窗口优先用本轮消息，有富余才带回更早轮次』
     —— budget=300：本轮成员发言被丢弃、更早轮次发言保留（dropped=1）
  ```
- 影响：历史较长时，组员/组长看到的是**上一轮的旧内容**，而本轮刚产生的发言（恰恰是当前任务的关键）
  会被丢进 dropped；与文档承诺和 `context_trimmed` 的文案（「最近发言保留」）都不一致。
- 最小修法：`rest = earlier + [item for item in head_pool[1:]]`（保留 `reversed`），
  并在 smoke_test 里补一条「预算紧张时本轮消息优先于更早轮次」的断言。

### F2（low）README §7 声称 `/api/snapshot` 含 `run_stats` 与 `route_summary`，实际没有

- 文档：`README.md:200`「GET /api/snapshot | 当前群成员状态、额度、`run_stats`、`route_summary` + 完整聊天记录」。
- 代码：`group/app.py:89-102` 只返回 `{"group": {...}, "messages": [...], "running": ...}`；
  带 `run_stats`/`route_summary` 的是 `ENGINE.snapshot()`（`engine.py:866-875`），它只用于 WS `hello`。
- 实测（`review_probe.py`）：
  ```
  [FAIL] README §7 声称 /api/snapshot 含 run_stats 与 route_summary
     —— 实际 /api/snapshot 顶层键=['group', 'messages', 'running']
  ```
- 最小修法：二选一——把 `run_stats`/`route_summary` 加进 `/api/snapshot`（推荐，界面刷新也需要），
  或把 README 那一行改成「快照（完整记录）+ WS hello（含 run_stats/route_summary）」。

---

## t1 八条验收标准逐条核对

| # | 标准 | 判定 | 证据 |
|---|---|---|---|
| 1 | `smoke_test.py --mock-only` 退出码 0，每项 PASS（配置装载/角色提示词/@抽取/@宽松匹配/事件序列/≥2 角色发言/记录员汇总） | **passed** | EXIT=0，`结果：全部通过（64 项检查）`；`docs/review-smoke-mock-only.txt` |
| 2 | 起服务后 `/api/health` 200 含 providers+members；`/` 返回群聊 HTML；WS 首帧 hello 带 members | **passed** | `http_probe`：health 200（5 provider 各带 ok/detail，members=6 含 enabled 位）、`/` 200 2146 字节；`ws_probe`：首帧 `kind=hello`，members=[captain,kimi,demo-bot,doubao,recorder] |
| 3 | POST /api/chat 后 WS 可见 captain → 组员 → 记录员；无 @ 时不多唤醒组员 | **passed** | `ws_probe`：413 事件，senders=captain,kimi,demo-bot,captain,kimi,demo-bot,captain,recorder；`mock_hint=quiet` 时 senders=['captain'] |
| 4 | 额度护栏有代码约束；不存在的 @目标 只 notice 不报错 | **passed** | `ws_probe`：逐成员 3≤4/2≤2/2≤2、派发链 7≤10、recorder 1；smoke 覆盖 max_messages/max_depth/max_context_chars/max_model_calls_per_run；未知 @ → `notice`，errors=0 |
| 5 | 降级路径可观测（绝不静默换模型） | **passed** | smoke 两组降级用例：warn notice + `route_changed`（requested/actual 路由）+ 消息自带 warning/route；`llm.py:255-339` 只在首个增量前降级，吐出内容后中断按不完整回复报错 |
| 6 | 密钥零落盘 | **passed** | smoke「密钥零落盘」扫描 32 文件命中无；`review_probe` 正则扫描 `.py/.yaml/.md/.js/.html/.css/.ps1` 命中无；`/api/diagnostics` 不含 `sk-` 前缀 |
| 7 | AG2 适配层可用（未安装时明确降级、不影响主流程） | **passed** | smoke：`describe()` 返回 kernel/fallback 与 pip 说明；`build_llm_config` 对每个启用成员可编译或给出缺密钥说明；`build_ag2_group` 未装时抛带 `pip install ag2` 的 RuntimeError |
| 8 | README 与 `config/roles/*.md` 与实际实现一致 | **failed** | 见上 F1、F2 |

## 工程质量专项

| 审查点 | 结论 | 证据 |
|---|---|---|
| 是否静默换模型 | **否**。降级必发 `notice(warn)` + `route_changed` + message 的 `warning`/`route`；首增量之后中断不降级、直接报「不完整回复」 | `engine.py:650-667`、`llm.py:308-332` |
| 是否硬编码密钥 | **否**。全部走 `config.resolve_credential`（env → DSH refs） | `config.py:66-78`、`llm.py:98-114`；两次独立扫描 0 命中 |
| 是否有烧穿额度的循环 | **否**。派发循环受 `max_depth` + `max_messages`(尝试数) + 成员 `max_turns_per_run` 三重约束，记录员一轮 ≤1；`openai` 客户端 `max_retries=0`；temperature 自愈最多额外 1 次并缓存结论 | `engine.py:419-503,508-510`、`llm.py:126-133,189-213` |
| 异常路径是否会让服务端崩溃 | **否（并已验证）**。`TimeoutError` 在 `llm.py:193-198,236-240` 归一为 `LLMError`；`engine._run` 捕获 `LLMError`/`Exception` 并发 `run_end{aborted}`；WS 处理函数整体 try/except；`Hub.broadcast` 逐个客户端 try/except | `engine.py:386-399`、`app.py:54-67,274-279` |
| 全厂商不可用是否有可判定失败信号 | **是**。顶层 `error` + `run_end{aborted:true}`，本轮零产出可被程序判定 | `engine.py:374-385`；`review_probe` 实测 error=1、aborted=True、成功发言=0 |
| 失败成员是否会被当成“参与过” | **否**。失败留一条带 `error` 的 Message，`route_summary.failed` 单列，记录员任务里显式写出缺口 | `engine.py:523-535,544-545,559-570,760-778`；`review_probe` 实测 |
| @触发与额度护栏是否代码强制（非仅提示词） | **是**。解析（含最小长度/歧义拒绝）、派发队列去重、逐成员计数、单轮上界全部在 `engine.py` 内核里执行，提示词只影响模型措辞 | `engine.py:125-187,404-510` |
| 标签全局唯一 | 当前 `models.yaml` 6 个成员标签无重复；**但唯一性只由 `models.yaml:12-15` 注释约束**，`check_invariants`（`config.py:318-341`）只查 fallback 异厂。运行期靠 `engine.py:145-152` 判歧义并拒绝派发兜底，因此不构成功能缺陷 | 记录为低优先加固建议 |

## 环境事实（与本次裁决无关，但影响 t5 的表述）

- `deepseek` 无可用 API key（`/api/health` 报 `ok=False`），captain/recorder 主路由是 deepseek，
  因此**真实模式每轮都会降级到豆包**；`requested_route=deepseek/...`、`route=doubao/...`、`degraded=true`。
  README §2 已如实写出这一点。
- 账本 `~/.dsh/.dshw-usage.json` 只记 DSH 自身用量，不覆盖本应用直连厂商的调用；
  判断真实路由请用 `/api/diagnostics` 与事件流，不能用「账本无条目」推断静默降级。

## 复现命令

```powershell
cd ai-group
$py = "..\campus-market\backend\.venv\Scripts\python.exe"
& $py scripts\smoke_test.py --mock-only                     # EXIT=0, 64/64
$env:SMOKE_MOCK_ONLY="1"
Start-Job { Set-Location $using:PWD; & $using:py -m uvicorn group.app:app --port 8804 }
& $py scripts\http_probe.py --port 8804 --expect-mock       # 3/3
& $py scripts\ws_probe.py --port 8804                       # 9/9
& $py scripts\review_probe.py --port 8804                   # 8/10（F1/F2）
```
