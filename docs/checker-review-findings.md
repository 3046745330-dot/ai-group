# checker · 独立验证与评审记录（interim）

> **状态：未提交的中间证据，不是 t3 的正式判定。**
> t2/t3 依赖 t1；t1 仍被 builder 持有（`claim_task(t3)` 返回
> `blocked by unfinished dependencies: t1, t2`），实现文件在我验证期间仍在被修改
> （21:28 → 21:53 之间有多次落盘）。本文所有结论只对下面钉住的修订成立；
> t1 完成、修订冻结后必须重跑本目录脚本再出正式 verdict。

## 1. 被验证的修订（本节唯一有效的时间锚点）

| 文件 | LastWriteTime | SHA256 前 12 位 |
|---|---|---|
| group/engine.py | 21:45:12 | 748934F0EF40 |
| group/llm.py | 21:42:10 | 10C02B72AF70 |
| group/app.py | 21:44:53 | A0D72031EDEF |
| group/config.py | 21:46:02 | 8D8B5D5ED619 |
| group/ag2_adapter.py | 21:25:32 | B109CFEE6465 |
| config/providers.yaml | 21:29:21 | ECDFA993CA3F |
| config/models.yaml | 21:44:57 | A73BEC6622FF |
| scripts/smoke_test.py | 21:45:18 | 93A07DF94AF0 |
| README.md | 21:18:03 | 4B41BF7DB4AA |

复算命令：

```powershell
cd ai-group
Get-ChildItem group\*.py, config\*.yaml, README.md | Get-FileHash
```

## 2. 独立复现（真实命令 + 真实输出，端口 8801）

### 2.1 mock-only 自检：**49/49 通过，退出码 0**

```powershell
cd ai-group
.\.venv\Scripts\python.exe scripts\smoke_test.py --mock-only
# 结果：全部通过 ✅（49 项检查）      EXIT=0
```

原始输出：`docs/_smoke_mockonly.txt`。

### 2.2 进程边界端到端（后台 uvicorn + HTTP + WebSocket）：**8/8 通过**

```powershell
# 后台作业启动（未占用 8801，无需换端口）
$env:SMOKE_MOCK_ONLY="1"
.\.venv\Scripts\python.exe -m uvicorn group.app:app --host 127.0.0.1 --port 8801
# 另一进程：
.\.venv\Scripts\python.exe scripts\ws_probe.py --port 8801
```

观察到的事实（`docs/_ws_probe.txt`）：

- `GET /api/health` → 200，0.05s，providers=`deepseek/moonshotai-cn/doubao/openai/mock`，members=6，`mock_only=true`
- `GET /` → 200，2146 字节，命中 `群`/`chat`
- `WS /ws/group` 首帧 `kind=hello`，members=`captain,kimi,demo-bot,doubao,recorder`
- `POST /api/chat` → WS 406 个事件，`run_start … run_end`，覆盖 `message_start/delta/end`
- message_end 覆盖 captain + 组员 + recorder：`['captain','kimi','demo-bot','captain','kimi','demo-bot','captain','recorder']`
- 每条 message 都带 `route` 与 `requested_route`
- 无 @ 时只唤醒 captain；未知 @ 目标只产生 notice，0 error

### 2.3 真实模型路径（无环境变量、走 DSH 凭据库）

provider 可用性（不打印密钥，只报来源）：

```
deepseek          ok=False 未配置（DEEPSEEK_API_KEY）
moonshotai-cn     ok=True  密钥来源 dsh:MOONSHOTAI_CN_API_KEY
doubao            ok=True  密钥来源 dsh:ARK_API_KEY
openai            ok=False 未配置（OPENAI_API_KEY）
mock              ok=True  离线通道，无需密钥
```

真实配置自检 `scripts/smoke_test.py`（不带 `--mock-only`，在 21:28 修订上启动、21:45 结束）：
**happy round 真的跑通了多厂商**——`captain、kimi、doubao、recorder` 均发言，
组员是真实 Kimi 与真实豆包产出（`captain=2、kimi=2、doubao=1`，记录员 678 字纪要）。
但随后 `check_degraded_resolve` 因 `asyncio.wait_for(...timeout=60)` 超时抛出**未捕获的
TimeoutError**，进程退出码 1（`docs/_smoke_real.txt`）。
注意：这次真实自检启动时 engine.py 还是 21:28 修订，不能代表第 1 节的修订；
它只证明「真实厂商通道确实可用、降级链确实生效」。

## 3. 对 t4 论证者第 1、2、3 条指控的逐条核实

探针：`scripts/claim_probe.py`（只读，不修改实现）。原始输出 `docs/_claim_probe.txt`。
**8/11 条成立，3 条在当前修订上已被实现者修掉。**

### 第 1 条：上下文预算不可观测

| 编号 | 指控 | 核实结论 | 证据（当前修订） |
|---|---|---|---|
| 1a | 关键约束被从新往旧截断丢掉、丢弃无事件 | **已不成立（实现者已修）** | `engine.py:191-193` 钉住首条；`_transcript` 返回 `(文本, 丢弃条数)`（`:170-204`）；`_build_messages` 广播 `context_trimmed`（`:224-236`）。实测：1 约束 + 9 条长发言 → ZEBRA 仍在，`context_trimmed=1 条（丢 5 条）` |
| 1b | 预算不含 system prompt | **已不成立（实现者已修）** | `engine.py:221-224`：`fixed = len(system)+len(task)+64`，transcript 预算 = `max(400, max_context_chars - fixed)`；实测 system 991 + user 6538 = 7529 ≤ 9000 |
| 1c | 单条消息可整体突破预算 | **成立（残留）** | `engine.py:191-193` 无条件钉住首条：limit=100 时，5000 字符的首条仍原样进入 transcript（实测 5005 字符） |
| 1d | 窗口跨 run 共享 | **成立** | `_transcript` 签名无 `run_id`；`self.messages` 跨 run 不清空；round-B 仍能看到 round-A 的 `SECRET-OLD`。额外发现：被钉住的「首条」是**整个会话最早那条**，不是本轮用户的约束 |

### 第 2 条：失败 / 顺序 / 去重语义

| 编号 | 指控 | 核实结论 | 证据（当前修订） |
|---|---|---|---|
| 2a | 失败只发 notice + `return []`，记录员照常汇总，缺口不可区分 | **成立** | `engine.py:509-522` 失败分支 `return []` 且不 append 到 `self.messages`；`_summarize` 只数 `run_id` 内非 user 条数 ≥2（`:564-570`）。实测：kimi 两次调用失败 → 记录员仍出 1 条纪要，喂给它的 transcript 里没有任何失败标记 |
| 2b | 并发入口是拒绝不是排队 | **成立** | `engine.py:271-273`：持锁时 emit `error` 后 `return`，整轮被丢弃；实测消息「上一轮讨论还没结束，请稍候再发。」 |
| 2c | `message_start` 缺 turn 序号 | **成立** | `engine.py:472-481` 字段无 `turn`；turn 只在 `message_end` 的嵌套 `message` 里（`:550`、`:644`） |
| 2d | docstring 声称的 (成员,轮次) 去重不存在 | **已不成立（实现者已改文案）** | `engine.py:12-20` 现在如实写 max_messages/max_turns_per_run/max_depth/max_context_chars；全文件无 (sender,turn) 去重，护栏是 `_run_spoke` 计数。**但 README:100 仍写「去重：同一成员同一条消息不会重复触发」** |
| 2e | `resolve_mention` 宽松匹配副作用 | **成立（且比指控更严重）** | `engine.py:131-139`：三级子串匹配，无最小长度/唯一性校验。实测 `@k -> captain`（不是指控里说的 kimi）、`@demo -> demo-bot`、`@组 -> captain`。一个字母就能把活静默派给组长 |
| 2f | （附加发现）失败的成员仍被记为「实际参与且未降级」 | **成立** | `engine.py:511` 失败分支 `self._note_route(agent, agent.provider, requested_route, False)`；`route_summary()`（`:407-440`）把它当正常参与统计，并被写进记录员的任务描述（`:590`）。实测 route_summary.routes 中失败成员条目 `degraded=False`、route=声明路由 |

### 第 3 条：可度量性

- **事实成立**：`Message` 无任何 token/usage/cost 字段（`engine.py:39-57`）；事件流里
  与上下文有关的只有 `context_trimmed`（丢了几条），没有「本轮实际发了什么」的清单，
  因而系统自身无法回答「群聊是否比单模型更值」。
- **契约归属**：t1 的 8 条验收标准里没有 token/成本/时延指标要求（只要求「上下文长度上限
  有代码约束」）。因此**按契约只能记为改进建议，不是 blocker**。建议按论证者的四项指标
  （正确率增量 / 每正确结果成本 / P95 时延 / 归因可回放率）单独立任务。

## 4. 队长给出的「deepseek DSH 凭据失效」硬事实：影响面核实

- **当前修订里该来源已被移除**：`config/providers.yaml:23-28` 的 `credentials` 只剩
  `env: DEEPSEEK_API_KEY`，并在注释里写明「该 token 请求 api.deepseek.com/v1/models 返回
  HTTP 401，故意不列为凭据来源」。`probe_availability` 因此如实报 `deepseek ok=False
  未配置（DEEPSEEK_API_KEY）`，**不再出现「看起来可用、实则必 401」的假绿色**。
- **不会导致真实路径全部落空**：`moonshotai-cn` 与 `doubao` 分别通过 DSH 引用
  `MOONSHOTAI_CN_API_KEY` / `ARK_API_KEY` 解析成功（见 2.3）。真实自检的 happy round 里
  Kimi 与豆包都是真实产出。
- **真正的后果**：`captain` 与 `recorder` 的主路由是 deepseek → **每一轮都必然降级到豆包**，
  即团队目标里承诺的「组长 DeepSeek + Kimi/豆包」在当前环境下实际是
  「组长豆包 + Kimi + 豆包」。这不是代码缺陷，但 t5 的报告必须如实写出
  `requested_route=deepseek/...`、`route=doubao/...`、`degraded=true`，不能写成「组长由 DeepSeek 担任」。
- **降级可观测性**：成立。`engine.py:494-505` 发 warn notice + `route_changed`；
  message 自带 `warning`/`route`；界面 `web/app.js:179-186`、`:234-236` 打「已降级」标记。
  但「所有路由都不可用」时只有一条 `notice(level=error)`、**没有顶层 `error` 事件**，
  `run_end` 照常发出、本轮零发言——可观测但容易被判为「正常结束」。

## 5. README 与实现一致性（t1 验收第 8 条）

README（21:18:03）明显滞后于 21:29-21:44 的配置改动，探针 `scripts/readme_probe.py`
输出见 `docs/_readme_probe.txt`：

| README 位置 | 文档说 | 代码/配置实际 |
|---|---|---|
| `README.md:42` | 没有任何 API 密钥也能跑完整链路 | 主路由+fallback 全部无凭据时，组长这一棒就失败，**不会产生 @**，demo-bot 与记录员永不唤醒；本轮零发言、零顶层 error（探针实测） |
| `README.md:57` | deepseek 可用 DSH 凭据库 token | `providers.yaml` 已删除该来源 |
| `README.md:58` | kimi 凭据 `llm-pi-ai/moonshotai-cn` | 实际 `dsh_credential: MOONSHOTAI_CN_API_KEY` |
| `README.md:59` | doubao 凭据 `llm-pi-ai/doubao` | 实际 `dsh_credential: ARK_API_KEY` |
| `README.md:72` | doubao「默认未启用」 | `models.yaml` 中 doubao `enabled: true` |
| `README.md:86` | 依次唤醒组员（可配置并发或串行） | 无任何并发配置项，`engine.py:368` 硬编码串行 `await` |
| `README.md:100` | 去重：同一成员同一条消息不会重复触发 | 无该去重；护栏是 `_run_spoke` 计数 vs `max_turns_per_run` |
| `README.md:153-154` | 豆包未启用、账号一个模型都没开通 | `providers.yaml:73-76` 注释与 `models.yaml` 均写明豆包实测可用并已启用 |

**结论：t1 验收第 8 条（README 与实现一致）在当前修订上不通过。**
其余 7 条在当前修订上均通过（证据见第 2 节与 2.1 的 49 项自检）。

## 6. 用于正式 t3 的 findings 草稿（严重度已评估）

| id | 严重度 | 问题 | 位置 | 建议修法 |
|---|---|---|---|---|
| F1 | high | 成员调用失败对记录员完全不可见，且被 route_summary 当成正常参与 → 有缺口的纪要会被当成完整结论 | `engine.py:509-522`、`:511`、`:564-570` | 失败也写一条带 `error` 的 Message（或在摘要任务里显式列出失败成员/缺口），route_summary 增加 `failed` 标记 |
| F2 | medium | README 与配置/实现不一致 8 处，含「无密钥也能跑完整链路」这种会误导演示者的说法 | 见第 5 节 | 同步 README；或明确写「无密钥时请用 -MockOnly」 |
| F3 | medium | `resolve_mention` 子串匹配无最小长度/唯一性校验，`@k`/`@组` 静默命中 captain → 派发对象可能错误 | `engine.py:131-139` | 子串匹配要求长度 ≥2 且唯一命中；多义时发 notice 而不是取第一个 |
| F4 | medium | 上下文窗口跨 run 共享，且钉住的「用户首条」是整个会话最早那条 → 新会话被旧约束/旧内容污染 | `engine.py:170-204` | 按 `run_id` 过滤，或钉住本轮的 user 消息 |
| F5 | low | 被钉住的首条消息可无上限地突破 `max_context_chars` | `engine.py:191-193` | 首条也做硬截断并广播 `context_trimmed` |
| F6 | low | `message_start` 无 `turn`，前端无法在流式阶段区分第几棒 | `engine.py:472-481` | 加 `turn=self._run_emitted` |
| F7 | low | 并发发言是拒绝不是排队，且无队列/错误码约定（`/api/chat` 返回 409，WS 只发 error 事件） | `engine.py:271-273` | 文档写明，或提供排队 |
| F8 | info（契约外） | 无 token/成本/时延指标与上下文清单，无法自证「群聊比单模型更值」 | `Message` / 事件流 | 单独立任务（论证者四项指标） |
| F9 | low | 真实配置自检在 `asyncio.wait_for(timeout=60)` 超时时抛未捕获 TimeoutError，整个自检崩掉 | `scripts/smoke_test.py:294,320` | `try/except TimeoutError` 转成一条 FAIL 检查 |

## 7. 复现脚本清单（均在 ai-group/scripts/ 下，只读实现）

```powershell
cd ai-group
$py = ".\.venv\Scripts\python.exe"
& $py scripts\smoke_test.py --mock-only     # 49/49, exit 0
& $py scripts\claim_probe.py                # t4 三条指控逐条核实
& $py scripts\readme_probe.py               # README 与实现一致性
# 先后台起服务，再：
& $py scripts\ws_probe.py --port 8801       # HTTP + WebSocket 端到端
```
