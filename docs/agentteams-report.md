# AgentTeams 本轮成果总报告 + ai-group 运行时真实证据

- 报告人：builder（实现工程师，团队成员）
- 日期：2026-10-12
- 说明：本报告分两部分 —— ① 团队成果整合；② ai-group 系统**真实运行**的事件流证据。
  凡是我自己没有亲手跑过的结论，都标注为「转述（出处）」；亲手跑的标注为「独立复现」。
- 保密：报告不含任何密钥明文（`providers.yaml` 只声明环境变量名与 DSH 凭据库的 ref 名）。

---

## 第一部分 · 团队做了什么

### 1.1 团队与成员

Team：`ai-group-team`（profile `llm-group`，组长 = captain）。成员：

| 成员 | 角色 | 路由 | 在本轮里的产出 |
|---|---|---|---|
| captain | 队长 / 协调者 | —（DSH 侧） | 派单、修订契约、统一安排真实自检与额度 |
| builder（我） | 实现工程师 | deepseek-account/deepseek-flash | ai-group 系统实现、修复与复验、本报告 |
| checker | 验证与评审工程师 | deepseek-account/deepseek-flash | t2 独立验证、t3 评审、t10 复审、多个独立探针 |
| kimi | 群员 · 中文长文与论证 | moonshotai-cn/kimi-k2.6 | llm-group 群 t1 发言（真实 Moonshot 产出）+ t7 技术审查 |
| doubao | 群员 · 快速总结与反驳 | doubao/doubao-seed-2-1-pro-260915 | llm-group 群 t2 发言 + t6 反驳 + t8 技术审查 |
| analyst | 群内发言人（论证） | deepseek-account/deepseek-flash | t4 论证发言（原定 Kimi，实际由 DeepSeek 路由完成） |

### 1.2 任务清单与依赖

| 任务 | 类型 | 负责 | 依赖 | 状态 | 结论摘要 |
|---|---|---|---|---|---|
| t1 | implementation | builder | — | completed | ai-group 系统实现完成；离线自检 64/64、`EXIT=0` |
| t2 | verification | checker | t1 | completed | 6 条验收标准全部 passed；哈希前后一致（未改实现） |
| t3 | review | checker | t1,t2 | failed(needs_revision) | 8 条验收中第 8 条 failed，提出 F1（窗口本轮优先）/F2（snapshot 字段）两条缺陷 |
| t4 | work（群内论证） | analyst | — | completed | 多模型群聊的收益与边界（见 1.4） |
| t5 | integration | builder（我） | t10,t4 | 本任务 | 本报告 + 运行时证据 |
| t6 | work（群内反驳） | doubao | — | completed | 「把协调者绑死单一厂商」属于可检验的设计缺陷 |
| t7 | work（技术审查） | kimi | — | completed | @触发派发循环的正确性与终止性（只读静态审查） |
| t8 | work（技术审查） | doubao | — | completed | 事件流与状态一致性边界用例表 |
| t9 | repair round 2 | builder | t1 | completed | 复验 F1/F2 在当前 head 上均已修复（探针 7/7、`EXIT=0`） |
| t10 | review round 2 | checker | t9 | completed | **verdict=pass**，t1 八条验收全部 passed |

### 1.3 关键结论（工程侧）

1. **系统可用且已验证**：mock 自检 65/65 `EXIT=0`；进程边界（HTTP + WebSocket）探针全绿；
   真实配置实例可起、可服务、可跑完整一轮真实模型群聊（见第二部分）。
2. **两条被评审指出的缺陷已闭环**：F1（上下文窗口「本轮优先」被写反）与 F2（`/api/snapshot`
   缺 `run_stats`/`route_summary`）在 t9 由可执行探针复验通过，t10 复审给出 pass。
3. **诚实边界**（源自 t2/t10 与我的实测）：
   - 本机**没有可用的 DeepSeek platform key**（`DEEPSEEK_API_KEY` 未配置；DSH 凭据库里那条
     `deepseek-account-platform/default` token 实测调用返回 401，已从 `providers.yaml` 删除），
     因此组长在**配置层**是 `deepseek/deepseek-v4-flash`，但**每轮都以可观测降级形态运行**。
   - 真正发生的多厂商产出，是在 **kimi（moonshotai-cn/kimi-k2.6）与豆包（doubao）** 之间。
   - AgentTeams 侧的群员 kimi 受 Moonshot 的 thinking 回传校验与 3 RPM 限制；
     **应用层直连不受同样约束**（应用走标准 `/chat/completions` 且不回传 thinking）。

### 1.4 群内产出摘录（llm-group 的真实发言与失败）

> 以下四条的**原始文本**取自 AgentTeams 团队的持久化状态（`.agent-teams/ai-llm-group/team.json`，
> 只读查看）。它们是**真实模型产出**，不是我的转述改写；失败项照抄原始错误文本。

**t1 发言（产出者：kimi，路由 moonshotai-cn/kimi-k2.6，182 字）** —— 议题「校园二手交易 Agent
该先做智能估价还是撮合匹配」：

> **结论：先做「撮合匹配」。** 理由：校园二手交易的核心痛点不是定价，而是「有货没人要、
> 有人找不到货」。撮合匹配直接提升成交率……若先做估价，定价再准也无人成交，卖家只会流失。
> 反例：闲鱼早期并无智能估价，仅靠搜索和推荐就起量……

**t2 发言（产出者：doubao，路由 doubao-seed-2-1-pro-260915，218 字）**：

> 结论：先做撮合匹配。理由一：校园二手平台的死结是买家找不到货……理由三：匹配跑起来后沉淀的
> 点击、询单、成交价，正是日后做智能估价最缺的训练样本……对方漏掉的点：定价错一点还能议价、
> 能靠时间修正，供需错配则永远不会成交。

**t3（失败，产出者：kimi）** —— 任务「反驳豆包：指出对方观点里最站不住的一点」未完成，
原始错误文本（照抄，未做任何改写）：

```
The `content[].thinking` in the thinking mode must be passed back to the API.
(request_id: 3d842e93-3ed8-4d59-9630-236bc57ac9ac) (code INVALID_REQUEST)
```

**t4（未产出）**：任务「反驳 Kimi」状态停留在 `claimed`，输出为空 —— 即这一轮**没有完成**，
我没有为它编造任何发言。

**群主纪要：本轮 llm-group 没有产出纪要。** 该群的持久化状态里只有 t1~t4 四条发言类任务，
**不存在**汇总/纪要类任务，也没有任何 summary 字段；结合 t3 失败、t4 未执行，
可以确定这一轮**未产出群主纪要**。我不用任何模板代替它。

**t4 论证发言（产出者：analyst / DeepSeek 路由，原定 Kimi）** 关键结论，供参考：
多厂商协作的**唯一收益来源是误差去相关**（同模型重采样是同一误差分布的重抽，不构成独立证据）；
群聊把共享介质换成有上限的记录（本实现 `max_context_chars=9000`），这是「用上下文换成本与并发度」；
并且群聊在「有唯一客观判据」「需要全局一致性产出」「依赖长上下文整体推理」「时延敏感」四类任务上
明显劣于单模型顺序推理。该发言自带来由声明：因 AgentTeams 侧触发 Moonshot 的 thinking 回传校验
失败，实际由 DeepSeek 路由完成，不作为 Kimi 的产出。

---

## 第二部分 · ai-group 系统最终形态

### 2.1 目录结构

```
ai-group/
├── config/            providers.yaml（路由+凭据来源）/ models.yaml（成员表）/ roles/*.md（角色提示词）
├── group/             config.py / llm.py / engine.py（★群聊内核）/ ag2_adapter.py / app.py
├── web/               index.html / app.js / style.css（无构建步骤）
├── scripts/           smoke_test.py / integration_check.py / run.ps1（+ checker 的探针）
└── docs/              verify-ai-group.md、review-t*.md、tech-review-*.md、evidence/、本报告
```

### 2.2 启动命令（实测）

```powershell
cd ai-group
# 真实配置（会长真实调用 moonshotai-cn / doubao；deepseek 无 key → 降级）
.\.venv\Scripts\python.exe -m uvicorn group.app:app --port 8801

# 或一键脚本
.\scripts\run.ps1 -Port 8801            # 解释器规则写死在脚本里
.\scripts\run.ps1 -Port 8801 -MockOnly  # 全部走离线通道，零额度
```

解释器规则：优先 `ai-group\.venv`，否则回退系统 PATH 里的 python，
都不可用就报错退出（**不会**静默退回没有 fastapi 的 DSH python）。

### 2.3 接口清单（`app.routes` 实测 13 条 = 8 个项目路由 + FastAPI 内置 3 条 + docs 重定向 + `/static`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 群聊界面（HTML） |
| GET | `/api/health` | provider 可用性 + 成员表（含 fallback）+ 额度 + 配置不变量 + AG2 状态 |
| GET | `/api/snapshot` | 成员/额度/`run_stats`/`route_summary` + 完整聊天记录 |
| GET | `/api/diagnostics` | **每次真实调用的记录**：路由 / 目标 host / 成败 / 报错 / 字数 / 耗时 / finish_reason |
| GET | `/api/ag2` | AG2 适配层状态 + 成员 llm_config 可编译性（不含密钥） |
| POST | `/api/chat` | `{"text","sender","mock_hint"}`，结果经 WS 广播 |
| POST | `/api/reset` | 清空聊天记录 |
| WS | `/ws/group` | 事件流：`hello`/`run_start`/`message`/`message_start`/`message_delta`/`message_end`/`agent_status`/`room_state`/`route_changed`/`route_summary`/`context_trimmed`/`notice`/`error`/`cleared`/`run_end` |

### 2.4 角色表（与 `config/models.yaml` 一致）

| 成员 | 角色 | 主路由 | 降级路由 | 提示词 | 启用 |
|---|---|---|---|---|---|
| captain | 组长 | `deepseek/deepseek-v4-flash` | `doubao/doubao-seed-2-1-pro-260915` | `roles/captain.md` | 是 |
| kimi | 组员·论证 | `moonshotai-cn/kimi-k2.6` | `doubao/doubao-seed-2-1-pro-260915` | `roles/member_analyst.md` | 是 |
| doubao | 组员·反驳 | `doubao/doubao-seed-2-1-pro-260915` | `moonshotai-cn/kimi-k2.6` | `roles/member_critic.md` | 是 |
| demo-bot | 组员·离线 | `mock/mock-fast` | 无 | `roles/member_mock.md` | 是 |
| gpt | 组员·交叉视角 | `openai/gpt-5.1-mini` | `moonshotai-cn/kimi-k2.6` | `roles/member_critic.md` | 否 |
| recorder | 记录员 | `deepseek/deepseek-v4-flash` | `doubao/doubao-seed-2-1-pro-260915` | `roles/recorder.md` | 是 |

护栏语义：`max_messages=10` 只约束**派发链尝试次数**（记录员纪要不占配额、但一轮最多一次，
故单轮模型调用总数上界 = 11）；成员级 `max_turns_per_run` 只计**成功发言**（失败调用计入花费上界）；
`max_depth=2`；`max_context_chars=9000`（system + user 总字符数，截断会广播 `context_trimmed`）。

---

## 第三部分 · 验证与评审结论摘录

**独立复现（我亲手跑，证据见 `docs/evidence/`）**

| 项目 | 命令 | 结果 |
|---|---|---|
| 离线自检 | `scripts/smoke_test.py --mock-only` | `结果：全部通过（65 项检查）`，`EXIT=0`（`evidence/smoke-mock-only.txt`） |
| F1/F2 复验探针 | `docs/verify_f1_f2_probe.py` | 7 项全 PASS，`EXIT=0`（`evidence/verify-f1-f2.txt`、结论 `evidence/verify-f1-f2.md`） |
| 进程边界 | mock-only 起服务 + `scripts/ws_probe.py` | 8 项全 PASS（`evidence/ws-probe-mockonly.txt`；该探针自身在 GBK 控制台打印 `✅` 会退出 1，属 checker 文件问题） |
| 真实运行时一轮 | 见第四部分 | 8 条发言、含真实 Moonshot 与豆包产出、含 1 次真实超时降级 |

**转述（出处：checker 的报告与任务输出；我没有重跑这些）**

- `docs/verify-ai-group.md`（t2）：**6 条验收标准全部 passed**；验证前后 `group|web|config`
  16 个文件哈希逐一一致；8801 真实配置实例只做只读接口验证、聊天跑在 8802 的 mock-only 实例上
  （为确定性与避开 3 RPM）。原始输出在 `docs/verify/`。
- t3 评审：`needs_revision`，8 条验收中第 8 条 failed，并提出 F1/F2。
- t10 复审：**verdict=pass**，t1 八条验收全部 passed；硬证据为 `smoke_test --mock-only` 65/65 `EXIT=0`、
  checker 自己的 `review_probe` 10/10、`http_probe` 3/3、`ws_probe` 9/9；工程质量四项（无静默换模型 /
  无硬编码密钥 / 无烧额度循环 / 异常不崩）均给出文件+行号。报告 `docs/review-t10.md`。
- 队长侧真实自检：`docs/evidence/smoke-real.txt`（12,102 字节，23:00:11），**65 PASS / 0 FAIL**，
  末行 `EXIT=0`；其中「组长发言 3 条」「组员 kimi、doubao、kimi」「记录员汇总 1 条 / 1320 字」。
  **历史如实记录**：更早一次真实跑曾出现「记录员汇总产出 0 条」的瞬时失败，复跑即通过 ——
  该失败是一次性瞬时现象，不是被修掉的稳定缺陷，此处不抹掉这段历史。

---

## 第四部分 · 运行时真实证据（我亲手跑）

### 4.1 命令与端口（原样）

```powershell
# 1) 后台起真实配置服务（契约 verify 命令，端口 8801）
cd ai-group
.\.venv\Scripts\python.exe -m uvicorn group.app:app --port 8801
# 启动日志：docs/evidence/runtime-8801-uvicorn.log（Uvicorn running on http://127.0.0.1:8801）

# 2) 投喂一句需要协作的问题并抓取 WS 事件流
.\.venv\Scripts\python.exe docs\capture_runtime_evidence.py --port 8801
```

- **实际端口：8801**（`/api/health` 实测 `mock_only=False`，即真实配置实例）。
- `POST /api/chat` 返回 `HTTP 200 {"ok": true, "mode": "streaming-over-websocket", ...}`。
- 投喂的问题：*「用一句话回答：多模型群聊相对「单模型多次调用」的不可替代价值是什么？
  请组长拆解并 @kimi 用一句话给出论证、@doubao 用一句话给出反驳，然后收敛。」*
- 一轮耗时 **563.0 秒**，共 **2315 个事件**（含 hello）。
- 原始事件：`docs/evidence/runtime-8801-events.json`；摘要：`docs/evidence/runtime-8801-summary.txt`。

### 4.2 服务端看到的可用性（`GET /api/health`，实测）

```
deepseek         ok=False  未配置（DEEPSEEK_API_KEY）
moonshotai-cn    ok=True   密钥来源 dsh:MOONSHOTAI_CN_API_KEY
doubao           ok=True   密钥来源 dsh:ARK_API_KEY
openai           ok=False  未配置（OPENAI_API_KEY）
mock             ok=True   离线通道，无需密钥
```

### 4.3 成员发言顺序与实际生效路由（来自 `message_end`，8 条）

| # | 成员 | 角色 | 声明路由（配置层） | **实际生效路由** | 字数 | 降级 |
|---|---|---|---|---|---|---|
| 1 | captain | 组长 | `deepseek/deepseek-v4-flash` | `doubao/doubao-seed-2-1-pro-260915` | 236 | **是** |
| 2 | kimi | 组员 | `moonshotai-cn/kimi-k2.6` | `moonshotai-cn/kimi-k2.6` | 128 | 否（真实 Moonshot 产出） |
| 3 | doubao | 组员 | `doubao/doubao-seed-2-1-pro-260915` | `doubao/doubao-seed-2-1-pro-260915` | 359 | 否 |
| 4 | captain | 组长 | `deepseek/deepseek-v4-flash` | `doubao/doubao-seed-2-1-pro-260915` | 362 | **是** |
| 5 | kimi | 组员 | `moonshotai-cn/kimi-k2.6` | `doubao/doubao-seed-2-1-pro-260915` | 105 | **是**（Moonshot 调用超时） |
| 6 | doubao | 组员 | `doubao/doubao-seed-2-1-pro-260915` | `doubao/doubao-seed-2-1-pro-260915` | 622 | 否 |
| 7 | captain | 组长 | `deepseek/deepseek-v4-flash` | `doubao/doubao-seed-2-1-pro-260915` | 944 | **是** |
| 8 | recorder | 记录员 | `deepseek/deepseek-v4-flash` | `doubao/doubao-seed-2-1-pro-260915` | 849 | **是** |

发言顺序即：**组长派发 → kimi → doubao → 组长收敛 → （第二轮）kimi → doubao → 组长收敛 → 记录员纪要**。
`run_end` 事件正常到达（`aborted=false`，本轮有产出）。

**降级原始文本（照抄事件流，未改写）**：

```
[notice/warn] deepseek/deepseek-v4-flash 不可用（provider `deepseek` 缺少可用密钥（未配置（DEEPSEEK_API_KEY）），
请配置环境变量或 DSH 凭据库后再试，或把该成员切到 mock 通道），已自动降级到 doubao/doubao-seed-2-1-pro-260915
[notice/warn] moonshotai-cn/kimi-k2.6 不可用（moonshotai-cn/kimi-k2.6 流式读取中断：Request timed out.），
已自动降级到 doubao/doubao-seed-2-1-pro-260915；本条回复由降级路由生成。
[notice/warn] 已达 @ 触发深度上限（max_depth=2），后续 kimi、captain、doubao 的 @ 不再展开。
```

**`route_summary` 事件（同源可见性）实测**：

```
providers={'doubao': 3}  degraded_count=2  shared_providers={'doubao': 3}  failed=[]
⚠ 同源提示：本轮 3 个角色实际只落在 1 个 provider 上，其中 doubao×3 由同一个 provider（厂商/通道）承担，
且 2 个角色是降级后落过去的。跨厂商去相关的效果已被打折，请谨慎看待结论。
```

> 诚实说明：`route_summary` 按**每个成员最后一条生效路由**聚合，因此 kimi 第 1 条真实走
> `moonshotai-cn` 的发言没有体现在这个聚合里（它出现在上面第 2 行的 `message_end` 里）。
> 逐条真实路由请以 `message_end` 与 `/api/diagnostics` 为准。

### 4.4 `/api/diagnostics` 真实调用记录（12 条，含失败原文）

```
22:52:14 deepseek/deepseek-v4-flash          host=api.deepseek.com              ok=False chars=0   0.0s   provider `deepseek` 缺少可用密钥（未配置（DEEPSEEK_API_KEY））
22:52:28 doubao/doubao-seed-2-1-pro-260915   host=ark.cn-beijing.volces.com     ok=True  chars=236 14.71s finish=stop
22:52:58 moonshotai-cn/kimi-k2.6             host=api.moonshot.cn               ok=True  chars=128 29.55s finish=stop
22:53:17 doubao/doubao-seed-2-1-pro-260915   host=ark.cn-beijing.volces.com     ok=True  chars=359 18.94s finish=stop
22:53:17 deepseek/deepseek-v4-flash          host=api.deepseek.com              ok=False chars=0   0.0s   （同上，缺密钥）
22:54:01 doubao/doubao-seed-2-1-pro-260915   host=ark.cn-beijing.volces.com     ok=True  chars=362 44.12s finish=stop
22:57:13 moonshotai-cn/kimi-k2.6             host=api.moonshot.cn               ok=False chars=0   192.43s Request timed out.
22:57:30 doubao/doubao-seed-2-1-pro-260915   host=ark.cn-beijing.volces.com     ok=True  chars=105 16.38s finish=stop
22:58:37 doubao/doubao-seed-2-1-pro-260915   host=ark.cn-beijing.volces.com     ok=True  chars=622 67.29s finish=stop
22:58:37 deepseek/deepseek-v4-flash          host=api.deepseek.com              ok=False chars=0   0.0s   （同上，缺密钥）
22:59:30 doubao/doubao-seed-2-1-pro-260915   host=ark.cn-beijing.volces.com     ok=True  chars=944 52.49s finish=stop
23:01:38 doubao/doubao-seed-2-1-pro-260915   host=ark.cn-beijing.volces.com     ok=True  chars=849 128.1s finish=stop
```

结论：路由不是「声称」的 —— 每条发言都能在调用记录里找到对应的**目标 host、成败、字数、耗时**。
`deepseek` 的失败是**缺密钥**（0.0s 本地判定，不发网络请求），`kimi` 的第二次调用是**上游超时**
（192.43s，Moonshot 侧），两者都如实体现为降级而不是静默换模型。

### 4.5 记录员是否产出纪要 —— 是（849 字，含【实际参与模型】行）

实际路由 `doubao/doubao-seed-2-1-pro-260915`（由 `deepseek` 降级而来），全文 849 字，
结构完整：`【本轮结论】【形成的共识】（3 条）【未决分歧】（无）【下一步】`，
并按内核要求在末尾给出 `【实际参与模型】` 一行。该行的原文是：

> 【实际参与模型】本轮未披露@captain、@kimi、@doubao 对应的具体 provider/model 信息，仅可知三者均由
> 同一家 provider 承接发言，@doubao 的 3 次调用均由该 provider 承担；3 个发言角色中有 2 个为降级后
> 接入该 provider……存在单家厂商承担全部 3 个发言角色的同源情况，跨厂商去相关效果已打折，本轮结论需谨慎
> 看待；本轮无角色调用失败，无因此导致的结论缺口，@demo-bot 未被派发本轮任务、未参与讨论。

**如实指出一处模型行为问题**：这一行按模板给出了「同源/降级/失败」三项事实，
但它声称「未披露具体 provider/model」，而内核在汇总任务里是**给了**这些信息的
（`route_summary.text` 与每条消息的 `route`）。也就是说：**字段在、提示词在，
模型自己没把这几个名字抄全**。这不影响「同源+降级」这一关键结论被写进纪要，
但说明「纪要里点名到具体 model」目前不是可强制的行为 —— 逐条真实路由仍应以 `/api/diagnostics` 为准。

### 4.6 前端消费情况（转述 + 代码核对）

`web/app.js` 已消费 `route_summary`（渲染同源/降级/失败告警条）与 `context_trimmed`（按轮聚合），
`applySnapshot` 会读 `snapshot.route_summary`，因此页面刷新后告警不消失 —— 这一条由队长实现并已过
`node --check`；我这次只做了代码核对（读过对应分支），**没有**在浏览器里做人工目视验证。

### 4.7 `integration_check` 的两次真实运行 —— **两次都是 16/20、`EXIT=1`，如实记录**

`scripts/integration_check.py` 在本轮被真跑了两次（额度空闲期），**两次都没有全绿**。这里照抄事实，
不做粉饰；失败清单与原因如下（原始输出：`docs/evidence/integration-check-real.txt`、
`docs/evidence/integration-check-8834.txt`，均已转成 UTF-8 落档）。

| # | 命令 | 结果 | 失败项 | 直接原因 |
|---|---|---|---|---|
| 1 | `integration_check.py --port 8831`（采集窗口用默认 420s） | 16/20，`EXIT=1` | 事件序列不含 `run_end`、记录员未发言、无 `route_summary` 事件、纪要缺【实际参与模型】 | **采集窗口到点就收工**：真实一轮比 420s 长，脚本在讨论进行到「captain → kimi → doubao → captain」时就停止采集，所以看不到 `run_end` 与纪要（不是功能不存在） |
| 2 | `integration_check.py --port 8834 --timeout 1200` | 16/20，`EXIT=1` | 记录员未发言、纪要缺【实际参与模型】、captain 的降级标注断言、事件流 route 与成功调用一一对应 | **豆包账号欠费 + Moonshot 空正文**：`23:11:57 doubao/... host=ark.cn-beijing.volces.com ok=False 0.18s Error code: 403 - {'error': {'code': 'AccountOverdueErro…`，随后两次 `Connection error.`；组长第二/三棒因此失败，记录员也失败 |

两次都通过的关键断言（对验收有意义的部分）：

```
[PASS] 异厂：captain 与 kimi 实际生效的 provider 不同（跨厂商而非同厂自辩） —— captain=doubao kimi=moonshotai-cn
[PASS] 组长（captain）真实发言
[PASS] 被 @ 的组员真实发言（≥1 个非 mock 成员） —— kimi、doubao、kimi
[PASS] provider 探测到 Kimi 与豆包可用（实测凭据生效）
[PASS] 无 DEEPSEEK_API_KEY 时 deepseek 明确报不可用（不假装可用）
```

第 2 次的发言顺序与实际路由（来自事件流 `message_end`）：

```
captain  声明=deepseek/deepseek-v4-flash  实际=doubao/doubao-seed-2-1-pro-260915  293字（已降级）
kimi     声明=moonshotai-cn/kimi-k2.6     实际=moonshotai-cn/kimi-k2.6           534字
doubao   声明=doubao/...                  实际=doubao/doubao-seed-2-1-pro-260915  607字
captain  声明=deepseek/deepseek-v4-flash  实际=deepseek/deepseek-v4-flash        0字（失败）
kimi     声明=moonshotai-cn/kimi-k2.6     实际=moonshotai-cn/kimi-k2.6           0字（空正文）
captain  声明=deepseek/deepseek-v4-flash  实际=deepseek/deepseek-v4-flash        0字（失败）
```

事件流里对应的原始报错（照抄，证据文件本身在 160/60 字符处做了截断，未做改写）：

```
[error] 组长·DeepSeek 发言失败：deepseek/deepseek-v4-flash 与降级路由 doubao/doubao-seed-2-1-pro-260915
        均不可用：doubao/doubao-seed-2-
[warn] 汇总失败：deepseek/deepseek-v4-flash 与降级路由 doubao/doubao-seed-2-1-pro-260915 均不可用：
        doubao/doubao-seed-2-1-pro-260915
[warn] Kimi·论证担当 这一轮返回了空正文（路由 moonshotai-cn/kimi-k2.6，max_tokens=3000）
23:11:57 doubao/doubao-seed-2-1-pro-260915 host=ark.cn-beijing.volces.com ok=False 0.18s
        Error code: 403 - {'error': {'code': 'AccountOverdueErro…
23:13:18 doubao/doubao-seed-2-1-pro-260915 host=ark.cn-beijing.volces.com ok=False 11.03s Connection error.
```

**定性（一句话）**：这是**上游可用性问题**（豆包账号 `AccountOverdueError` 欠费 + Moonshot 超时/空正文），
**不是记录员路径的缺陷** —— 同一条代码路径在 23:00 的队长侧真实自检里产出「记录员汇总 1 条 / 1320 字」、
在 23:01 我这次 8801 真实运行里产出「记录员纪要 849 字（含【实际参与模型】行）」，
而 23:11 起豆包返回 403 欠费后，组长与记录员的唯一可用降级目标同时失效，整轮自然拿不到纪要。
内核在这种情况下**没有假装成功**：失败被写成 `notice/error` + 带 `error` 的 `message_end`，
并进入 `route_summary.failed`，`run_end` 仍正常到达。**无需为此改实现**；
按 t5 的写入范围（只允许 docs/ 与 README），本轮也不改任何实现文件。

**后续可复现的证据链与重试建议**：给 ARK 账号充值（或配置 `DEEPSEEK_API_KEY`）后重跑
`scripts/integration_check.py --port 8834 --timeout 1200`；该脚本对「讨论未跑完」这类情况
应当显式提示调大 `--timeout`（属 checker 维护的探针，未在本次范围内改动）。

### 4.8 其余说明

- `scripts/integration_server.log` 是 `integration_check.py` 每次运行自动覆盖的服务端日志
  （本轮结束时为 571 字节，非 0）；它并非本项目交付物，只是探针的副产物。

---

## 第五部分 · 结论与遗留

**可以下的结论**

1. ai-group 系统**可运行、可演示、可被第三方独立验证**：离线自检 65/65 `EXIT=0`；
   真实配置实例能起、能跑完整一轮真实多厂商群聊（563 秒、2315 事件、8 条发言、纪要 849 字）。
2. 多厂商是**真的**：kimi 的 128 字论证由 `api.moonshot.cn` 真实产出，doubao 的多条发言由
   `ark.cn-beijing.volces.com` 真实产出，均有 `/api/diagnostics` 的 host/耗时/字数佐证。
3. 降级是**可观测的**：组长 3 条发言与记录员纪要都标着 `requested_route=deepseek/...`、
   `route=doubao/...`、`degraded=true`，并有 `notice` + `route_changed` + `route_summary` 三处留痕。
4. 额度护栏**在真实运行中也生效**：本轮触发了 `max_depth=2` 的「后续 @ 不再展开」notice；
   记录员只调用 1 次。

**遗留与风险（不做修饰）**

- 组长在配置层是 DeepSeek，本机无 key，**每轮必然降级到豆包**；本轮实际只有
  kimi 与 doubao 两家真厂商参与，且豆包承担了 3 个角色（同源），跨厂商去相关效果打折。
- Moonshot 侧存在**上游超时**（本轮 192.43s 一次）与组织级 3 RPM 限流；
  这会让 kimi 在真实运行中偶发降级到豆包（本轮第 5 条发言即是）。
- `route_summary` 只反映每个成员的**最后一条**路由，不体现中途换过路由的成员；
  逐条真实路由应查 `message_end` 或 `/api/diagnostics`。
- 记录员纪要里的 `【实际参与模型】` 行**格式在、但模型可能不点名具体 model**（4.5 已如实记录）。
- AgentTeams 侧群员 kimi 受 Moonshot thinking 回传校验限制（t3 原始错误见 1.4），
  与应用层直连无关。
- **间歇性失败（真实模式，已知限制）**：真实模型运行时，可能因为**上游超时/限流/账号欠费**
  导致「记录员未产出纪要」与「`route_summary` 未上报」（见 4.7 的两次 `integration_check`：
  都是 16/20、`EXIT=1`）。**结果对照**：队长侧真实自检 65/65（含「记录员汇总 1 条 / 1320 字」）、
  我 23:01 的 8801 真实运行（含「纪要 849 字 + 【实际参与模型】行」）都是**成功**的；
  23:11 起豆包返回 `403 AccountOverdueError`（账号欠费）后，组长与记录员共用的降级目标同时失效，
  整轮便拿不到纪要。**定性：上游可用性问题，不是记录员路径缺陷**（同一条代码路径在 23:00/23:01
  两次都产出了纪要），因此**不为此改实现**；建议：给 ARK 账号充值或配置 `DEEPSEEK_API_KEY`
  后重跑 `scripts/integration_check.py --port 8834 --timeout 1200`。
  另外注意第 1 次失败是**采集窗口（默认 420s）** 到点收工，而非功能缺失 —— 真实一轮可能超过该窗口，
  跑真实模式时请显式给足 `--timeout`。

---

## 附：本次报告的取证文件

| 文件 | 内容 |
|---|---|
| `docs/evidence/runtime-8801-summary.txt` | 本轮真实运行的摘要（发言顺序/路由/降级/纪要/调用记录） |
| `docs/evidence/runtime-8801-events.json` | 本轮 2315 个 WS 事件原始数据 |
| `docs/evidence/runtime-8801-uvicorn.log` | 8801 服务启动日志（契约 verify 命令 `python -m uvicorn group.app:app --port 8801`） |
| `docs/evidence/integration-check-real.txt` | `scripts/integration_check.py --port 8831` 的真实输出 —— **本次为 16/20、含 4 项失败（采集窗口 420s 到点，本轮未跑完；上游超时/欠费导致），`EXIT=1`**，详见 4.7 |
| `docs/evidence/integration-check-8834.txt` | `scripts/integration_check.py --port 8834 --timeout 1200` 的真实输出 —— **同样是 16/20、`EXIT=1`（豆包 `403 AccountOverdueError` + Moonshot 空正文），详见 4.7** |
| `docs/evidence/smoke-mock-only.txt` | 我跑的离线自检（65 项，`EXIT=0`） |
| `docs/evidence/verify-f1-f2.txt` / `.md` | F1/F2 复验探针输出与结论 |
| `docs/evidence/smoke-real.txt` | 队长侧真实自检（65 PASS / 0 FAIL，`EXIT=0`） |
| `docs/verify-ai-group.md`、`docs/review-t10.md`、`docs/review-t3.md` | checker 的验证/评审报告 |
| `docs/capture_runtime_evidence.py` | 本轮运行时取证的探针脚本（只发请求，不改实现） |
