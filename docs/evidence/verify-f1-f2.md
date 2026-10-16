# t9 复验结论：t3 的 F1 / F2 在当前 head 上均已修复

复验方式：**可执行探针**，只读代码 + 只发请求，不修改任何实现文件。
探针：`ai-group/docs/verify_f1_f2_probe.py`
原始输出（含 `EXIT=0`）：`ai-group/docs/evidence/verify-f1-f2.txt`

命令（在 ai-group 目录下）：

```powershell
..\campus-market\backend\.venv\Scripts\python.exe docs\verify_f1_f2_probe.py
"EXIT=$LASTEXITCODE"      # → EXIT=0，7 项全部 PASS
```

## F1 · 上下文窗口「本轮优先」— 已修复，探针通过

被复验实现：`group/engine.py` 的 `GroupEngine._transcript`，填充顺序为

```python
rest = earlier + [item for item in head_pool[1:]]   # 再 reversed(rest) 逐条占用预算
```

`reversed` 之后先走**本轮其余消息**，最后才轮到更早轮次；写成 `[head_pool[1:]] + earlier`
会把预算先给上一轮的旧发言。

探针两个场景（真实输出见留档）：

| 场景 | 构造 | 实测 |
|---|---|---|
| F1a（合同场景，budget=300） | 12 条更早轮次长发言（每条约 205 字）+ 1 条本轮短约束 | 返回 260 字符；本轮约束 `CURRENT-CONSTRAINT` **被保留且未被截断**；保留 1 条更早轮次（编号 11）+ 丢弃 11 条 = 12 条，**被丢弃的全部是更早轮次记录**；更早轮次内部也是「新的优先」（OLD-11 留下、OLD-0 被裁） |
| F1b（判别性场景，budget=400） | 上一轮 1 条 + 本轮 2 条（短约束 + 发言），两条候选等长，预算只够一条 | 本轮发言 `CUR-RUN-SPEECH-MARKER` **在**、本轮用户原话**在**、上一轮 `OLD-RUN-MARKER` **被裁**、dropped=1 |

说明：合同里举例「丢弃 12 条」，在本题构造下实际丢弃 11 条 —— 因为 budget=300 恰好还装得下
一条 205 字的旧发言，而本轮短约束始终被保留。这正是「本轮优先」应有的行为，
不是缺陷；F1b 才是能判别顺序写反的场景（旧顺序下本轮发言会被裁掉）。

回归防护：`scripts/smoke_test.py` 的 `check_context_priority` 覆盖同一逻辑
（本轮发言在、本轮原话在、更早轮次被裁、`context_trimmed` 有事件），
自检共 65 项全部 PASS。

## F2 · `/api/snapshot` 顶层字段 — 已修复，探针通过

被复验实现：`group/app.py` 的 `@app.get("/api/snapshot")`。默认取服务（`SMOKE_MOCK_ONLY=1`）
后请求 `GET /api/snapshot`，实测顶层键：

```
['group', 'limits', 'members', 'messages', 'route_summary', 'run_stats', 'running']
```

- `route_summary` 为结构化对象，键 = `providers / distinct_real_providers / degraded_count /
  shared_providers / failed / text / routes`；
- `run_stats` = `{"dispatch_calls": 0, "dispatch_speaks": 0, "member_success_counts": {},
  "failed_calls": 0, "recorder_calls": 0, "max_messages": 10, "model_calls_per_run": 0}`；
- `members / limits / messages / running` 齐备，且顶层键与 WS `hello` 的 snapshot 一致，
  页面刷新后额度/同源/降级信息不会丢。

（工单原文里第 2 条写的 `run_summary` 是笔误，断言按 `route_summary` + `run_stats`。）

## 本轮对实现的改动

| 文件 | 改动 | 为什么 |
|---|---|---|
| `group/engine.py` | `_transcript` 填充顺序改为 `earlier + head_pool[1:]` | 修 F1：原先 reversed 后先消耗更早轮次 |
| `group/app.py` | `/api/snapshot` 增加顶层 `members/limits/run_stats/route_summary`（保留原 `group` 段兼容） | 修 F2：README 已声明有、接口当时没有 |
| `scripts/smoke_test.py` | 新增 `check_context_priority` | F1 的回归防护 |
| `README.md` | `/api/snapshot` 行写明「顶层键与 WS hello 的 snapshot 一致」 | 与实现对齐 |

合同修订为「复验」之后**没有再改任何实现代码**；探针通过即为本次交付。

## 证据清单（均为当前 head）

- `docs/evidence/verify-f1-f2.txt` — 本次复验探针原样输出（7 项 PASS，`EXIT=0`）
- `docs/evidence/smoke-mock-only.txt` — `scripts/smoke_test.py --mock-only` 原样输出（65 项 PASS，`EXIT=0`）
- `docs/evidence/ws-probe-mockonly.txt` — 真实 uvicorn + HTTP + WS 的进程边界探针（8 项 PASS；
  该探针自身在 GBK 控制台打印 `✅` 会抛 UnicodeEncodeError 导致退出码 1，属 checker 文件的问题）
- `docs/evidence/STATUS.md` — 真实模型自检与 `integration_check` 的重跑安排
