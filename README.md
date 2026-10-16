# AI 群聊 · 多模型协作系统

个人实验项目：把多个厂商的大模型拉进**同一个群**，由一个「组长」模型负责任务拆解与派发，
其余模型作为「组员」被 @ 到才发言，最后再由记录员模型汇总成纪要，全过程通过 Web 界面实时展示。

这与「用 DSH 自带的 AgentTeams 插件拉群」是两条不同路线：本项目是**自己写框架代码**，
产物是一个可独立部署的 Python 服务 + 浏览器界面。

```
ai-group/
├── config/
│   ├── providers.yaml        # 模型提供方路由（DeepSeek / Kimi / 豆包 / OpenAI / 离线）
│   ├── models.yaml           # 群成员表：模型 + 角色 + 额度约束（含记录员）
│   └── roles/*.md            # 每个角色的独立系统提示词
├── group/
│   ├── config.py             # 配置装载 + 凭据解析（环境变量 → DSH 凭据库）+ 配置不变量校验
│   ├── llm.py                # 统一 LLM 客户端（OpenAI 兼容 + 流式 + 降级 + 调用记录 + mock）
│   ├── engine.py             # ★ 群聊内核：任务分配 / @触发 / 回复汇总 / 额度护栏
│   ├── ag2_adapter.py        # AG2（AutoGen）可选适配层
│   └── app.py                # FastAPI：WebSocket 事件流 + 静态页面
├── web/                      # 群聊界面（原生 HTML/CSS/JS，无构建步骤）
├── scripts/
│   ├── smoke_test.py         # 进程内端到端自检（--mock-only 零额度）
│   ├── integration_check.py  # 起真实 uvicorn + 真实多厂商模型的一轮联调
│   ├── run.ps1               # 一键启动（自动挑一个装了依赖的解释器）
│   ├── claim_probe.py        # checker 的独立验证探针（非本项目产物）
│   └── ws_probe.py           # checker 的进程边界探针（非本项目产物）
└── docs/evidence/            # 自检留档
```

## 1. 环境准备（先装依赖，再启动）

本项目自带 `requirements.txt`，**不依赖任何外部目录**：

```powershell
cd ai-group
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> 为什么要自建 venv：早期版本复用相邻项目的 venv，导致仓库 clone 到别的机器上跑不起来。
> 现在依赖清单在本仓库内自足，换机器只要 `pip install -r requirements.txt`。

`scripts/run.ps1` 的选择规则（找不到就**报错退出**，绝不静默退回没有依赖的解释器）：

1. `-Python <路径>` 显式指定；
2. `ai-group\.venv\Scripts\python.exe`（推荐，上面的命令就是建它）；
3. 系统 PATH 里的 `python` / `python3` / `py`（会探测依赖是否齐全）；
4. 都不满足 → 打印建 venv 的命令并以退出码 1 结束。

## 2. 快速开始

```powershell
cd ai-group
.\scripts\run.ps1 -Port 8801        # 起服务
# 浏览器打开 http://127.0.0.1:8801
```

手动启动（等价）：

```powershell
cd ai-group
.\.venv\Scripts\python.exe -m uvicorn group.app:app --host 127.0.0.1 --port 8801
```

离线模式（不调用任何真实模型，零费用）：

```powershell
.\scripts\run.ps1 -MockOnly
```

**真实可用性（如实说明，不夸大）**：本机实测可用的厂商是 `moonshotai-cn`（Kimi K2.6）
与 `doubao`（doubao-seed-2-1-pro-260915）。`deepseek` 目前**不可用**——DSH 凭据库里
`deepseek-account-platform/default` 那个 token 不是 platform API key（实测
`GET https://api.deepseek.com/v1/models` 返回 401），因此 `providers.yaml` 里已把它删掉，
只保留 `DEEPSEEK_API_KEY` 环境变量作为来源。没有配置该环境变量时：
组长的主路由不可用 → 自动降级到豆包（**并在事件流与界面气泡上明确标注降级**），
组员 Kimi/豆包各自走自己的真实路由。也就是说，「组长由 DeepSeek 担任」这项配置在当前机器上
实际是以**可观测的降级形态**运行的，而不是静默地换成一个别的模型。

## 3. 密钥从哪来

密钥**不写进任何配置文件**。`providers.yaml` 里每个 provider 声明一串 `credentials`，
按顺序解析，命中即用：

1. 环境变量，例如 `$env:ARK_API_KEY`
2. DSH 凭据库 `~/.dsh/.credentials.yaml`（按 **refs 名**引用）

| provider | 凭据来源（按顺序） | 本机实测 |
|---|---|---|
| `moonshotai-cn` | `MOONSHOTAI_CN_API_KEY` / `MOONSHOT_API_KEY` / DSH refs `MOONSHOTAI_CN_API_KEY` | 可用（可见模型 `kimi-k2.6`、`kimi-k2.7-code`） |
| `doubao` | `ARK_API_KEY` / DSH refs `ARK_API_KEY` | 可用（`doubao-seed-2-1-pro-260915` 真实返回） |
| `deepseek` | **仅** `DEEPSEEK_API_KEY` | 不可用：DSH 账号 token ≠ platform API key，实测 401 |
| `openai` | `OPENAI_API_KEY` | 未配置，国内直连需自备代理 |
| `mock` | 无需密钥 | 离线演示通道 |

拿密钥的两种方式：设环境变量，或把值写进 DSH 凭据库的 `refs` 段（键名就是上面表里的名字）。
代码只调用 `config.resolve_credential`，自身不落盘任何密钥。

## 4. 角色定义与配置不变量

| 成员 id | 角色 | 主路由 | 降级路由 | 系统提示词 | 启用 |
|---|---|---|---|---|---|
| `captain` | 组长 | `deepseek/deepseek-v4-flash` | `doubao/doubao-seed-2-1-pro-260915` | `roles/captain.md` | 是 |
| `kimi` | 组员·论证 | `moonshotai-cn/kimi-k2.6` | `doubao/doubao-seed-2-1-pro-260915` | `roles/member_analyst.md` | 是 |
| `doubao` | 组员·反驳 | `doubao/doubao-seed-2-1-pro-260915` | `moonshotai-cn/kimi-k2.6` | `roles/member_critic.md` | 是 |
| `demo-bot` | 组员·离线 | `mock/mock-fast` | 无（离线通道不会失败） | `roles/member_mock.md` | 是 |
| `gpt` | 组员·交叉视角 | `openai/gpt-5.1-mini` | `moonshotai-cn/kimi-k2.6` | `roles/member_critic.md` | 否 |
| `recorder` | 记录员 | `deepseek/deepseek-v4-flash` | `doubao/doubao-seed-2-1-pro-260915` | `roles/recorder.md` | 是 |

**不变量（新增配置时必须遵守，`config.check_invariants` 会在 `/api/health` 与自检里核对）**：

1. **任何成员的 `fallback.provider` 不得与主路由同厂**。否则一旦降级，整个群会塌缩成
   同一家厂商自辩——界面看起来完全正常，结论却可能只是同一家的自说自话，这是会让人
   误信结论的高危缺陷。
2. **同源必须可见**：只要一轮里多个角色实际落在同一个 provider，事件流会广播
   `route_summary`（带 `providers` / `shared_providers` / `degraded_count` / `failed`），
   并且**记录员纪要里必须带一行「【实际参与模型】」**，写明谁在哪条路由、谁降级了、
   有没有同源、谁失败导致缺口。
3. **`kimi-k2.6` 只接受 `temperature=1`**（给 0.6/0.7 实测都返回 HTTP 400
   `invalid temperature: only 1 is allowed for this model`），且它是思考型模型：
   `reasoning_content` **计入 `max_tokens`**，给 1500 会正文为空，给 3000 才有正文。
   配置里因此写死 `temperature: 1.0`、`max_tokens: 3000`。
   `llm.py` 另有一次「不传 temperature」的参数自愈（**只重试一次并缓存结论，绝不循环重试**），
   避免上游哪天改规则就把成员打死；Moonshot 该账号有组织级 **3 RPM** 限流，循环重试只会自伤。

改提示词 = 改 `config/roles/*.md`；改成员 = 改 `config/models.yaml`；两者都不需要动代码。

## 5. 群聊逻辑

一次用户发言的生命周期：

```
用户发言
   └─ 组长被唤醒：给结论，若需协助则输出 @成员id + 具体任务
        └─ 内核解析 @提及（@kimi / @Kimi·论证担当 / @论证 等宽松写法，先解析成成员身份再去重）
             └─ 依次唤醒被点名的组员 → 逐字流式上屏
                  └─ 组员回复里若又有 @ 且该成员有派发权，进入下一层（受深度上限约束）
                       └─ 组长回来做一次「收敛」发言
                            └─ route_summary（同源/降级/失败可见）
                                 └─ 记录员输出【本轮结论/共识/未决分歧/下一步】+【实际参与模型】
```

实现要点：

- **`@` 就是派工单**：群规则写在组长提示词里，调度权在内核里，模型不能绕过。
- **`@` 解析规则（防误派）**：`@` 前面必须是行首或空白/标点，`zhang@kimi` 这种正文里的邮箱
  不会被当作提及；匹配顺序是 精确 id → 精确显示名 → 精确标签（要求唯一）→ 前缀子串
  （要求 ≥4 字符且长度过半、且唯一），**歧义或太短一律不派发**，只发一条写明原因的 notice。
- **只有 `can_dispatch: true` 的成员**，其回复里的 @ 才会触发新发言；被忽略的 @ 也会发 notice。
- **队列按成员身份去重**：`@kimi @Kimi·论证担当 @论证` 三种写法一轮只唤醒 kimi 一次。
- **硬性额度护栏**（`RunLimits` + 成员级 `max_turns_per_run`）：

  | 护栏 | 语义 |
  |---|---|
  | `max_messages`（默认 10） | 单轮**派发链尝试次数**上限（组长 + 被 @ 的组员）。**记录员纪要不占这个配额**，但一轮最多调用一次，所以单轮模型调用总数上界 = `max_messages + 1` |
  | `max_turns_per_run` | 单个成员一轮最多**成功发言**几次；失败调用不计次 |
  | `max_depth`（默认 2） | @ 触发的最大层数 |
  | `max_context_chars`（默认 9000） | 喂给模型的 **system + user 总字符数**上限 |

- **失败与尝试分开计数**：失败调用照样吃「尝试次数」上界（防烧钱），但**不算**该成员
  「发过言」，所以不会出现「界面说已达发言上限、实际一个字没说」。
- **失败对纪要可见**：调用失败的成员会留一条带 `error` 的消息，`route_summary.failed`
  单列，并且记录员的任务里会显式给出「谁失败、结论有缺口」。
- **上下文截断有事件**：`max_context_chars` 超限时丢弃最旧的中间发言并广播 `context_trimmed`；
  **本轮用户原话优先保留**（但钉住的那条也受预算约束，超长会截断并如实上报），
  窗口优先用本轮消息、有富余才带回更早轮次。
- **降级**：某 provider 密钥缺失 / 上游报错 / 超时，若成员配了 fallback，自动换路由一次，
  并在事件流（`notice` + `route_changed`）与界面气泡上明确标注「已降级」，绝不静默换模型。
  主路由已经吐出内容后再断流，按「不完整回复」如实报错，不悄悄重来。
- **全部路由不可用**：顶层 `error` 事件 + `run_end{aborted:true}`，本轮零产出可被程序判定。

## 6. AG2 适配层

```python
from group.ag2_adapter import AG2_AVAILABLE, build_llm_config, build_ag2_group, describe
print(describe())   # {"ag2": False, "detail": "...", "kernel": "group.engine.GroupEngine", ...}
```

- 装了 AG2（`pip install ag2`）时，`build_ag2_group(cfg, client)` 会把 `config/models.yaml`
  里的成员转成 AG2 的 `ConversableAgent`，并用原生 `GroupChat` + `GroupChatManager` 起一版对照实现；
- 没装时，全部能力由 `group/engine.py` 的自研内核提供，`describe()` 给出明确说明，
  主流程完全不受影响；`build_llm_config()` 不依赖 AG2，因此「成员 → llm_config」的映射
  在没装 AG2 的机器上也能被测到。

为什么自研内核是主路径而不是 AG2：AG2 的 `GroupChat` 是「按 speaker_selection_method
轮流发言」，而本项目要的是「@ 谁谁来」的显式派发、逐字流式上屏、以及严格的单轮额度上限——
这三点在自研内核里是直接可控的。AG2 因此作为可插拔的对照实现保留。

## 7. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 群聊界面 |
| GET | `/api/health` | 各 provider 可用性、成员表（含 fallback）、额度、配置不变量、AG2 状态 |
| GET | `/api/snapshot` | 当前群成员状态、额度、`run_stats`、`route_summary` + 完整聊天记录（顶层键与 WS `hello` 的 snapshot 一致，页面刷新后额度/同源/降级信息不会丢） |
| GET | `/api/diagnostics` | **每次真实调用的记录**：路由 / 目标 host / 成败 / 报错 / 字数 / 思考字数 / 耗时 |
| GET | `/api/ag2` | AG2 适配层状态 + 每个启用成员的 llm_config 是否可编译（不含密钥） |
| POST | `/api/chat` | `{"text": "...", "sender": "...", "mock_hint": ""}`，非流式入口，结果经 WS 广播 |
| POST | `/api/reset` | 清空聊天记录 |
| WS | `/ws/group` | 事件流：见下表 |

WS 事件：`hello` / `run_start` / `message` / `message_start` / `message_delta` / `message_end` /
`agent_status` / `room_state` / `route_changed` / `route_summary` / `context_trimmed` /
`notice` / `error` / `cleared` / `run_end`。

`mock_hint` 只在离线 `mock` 通道生效（演示用，对真实模型无作用）：`quiet` = 不 @ 任何人，
`ghost` = @ 一个不存在的成员。可用来演示「没人被点名就不唤醒组员」与「未知 @ 目标只 notice」。

用命令行驱动一次群聊：

```powershell
$body = @{ text = "群里都有谁？各自负责什么？" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8801/api/chat -ContentType application/json -Body $body
```

## 8. 自检与联调

```powershell
cd ai-group
$py = ".\.venv\Scripts\python.exe"

# 1) 离线自检（零额度，必须 EXIT=0）：配置/角色/@解析/事件序列/护栏/降级/失败可见性
& $py scripts\smoke_test.py --mock-only
"EXIT=$LASTEXITCODE"

# 2) 真实自检（会用真实模型跑一轮主链路，其余边界用例仍走离线副本）
& $py scripts\smoke_test.py
"EXIT=$LASTEXITCODE"

# 3) 真实进程边界联调：真起 uvicorn + HTTP + WS，端到端一轮
& $py scripts\integration_check.py --port 8831
"EXIT=$LASTEXITCODE"

# 4) 运行时取证：对已在跑的服务投喂一句协作问题，抓 WS 事件流与 /api/diagnostics
& $py docs\capture_runtime_evidence.py --port 8801
```

**本轮真实运行的结果**（端口 8801、真实配置、一轮 563 秒 / 2315 事件 / 8 条发言）与整个团队成果的
收口报告在 [`docs/agentteams-report.md`](docs/agentteams-report.md)，原始事件与调用记录在
`docs/evidence/runtime-8801-*.{json,txt}`。一句话结论：**组长在配置层是 DeepSeek，但本机没有可用的
platform key，所以每一轮都是以「可观测降级」形态跑在豆包上**；真正发生的多厂商产出在
**kimi（moonshotai-cn）与豆包（doubao）** 之间，且豆包可能同时承担多个角色（同源），
事件流与界面都会明确标注这一点。

`smoke_test.py` 的两个模式：

- `--mock-only`：**全部**走离线 mock 通道，零网络零费用，覆盖配置、角色提示词、@解析与
  宽松匹配、事件序列完整性、三个角色都发言、无人被 @ 不唤醒组员、未知 @ 只 notice、
  降级可观测、D1 重复 @ 去重、D4 弱 token 不误命中、D6 失败调用不吃成功额度、
  F1 失败对纪要可见、F5 全厂商挂掉有可判定失败信号、四类额度护栏、密钥零落盘。
- 不带参数：主链路那一轮用**真实模型**，其余边界用例自动切到离线副本（省额度、不受限流影响）。
- 脚本顶层有兜底：任何未捕获异常都会打印「结果：…」总结行并以确定性退出码结束，
  绝不会出现「没有任何结论行」的输出。

`integration_check.py` 会打印每个发言人的**实际路由**、`/api/diagnostics` 的调用记录
（含目标 host 与耗时），并做 DSH 用量账本 `~/.dsh/.dshw-usage.json` 的前后差值交叉核对。

> 账本边界（实测结论）：账本计的是 **DSH 账号侧**用量，本应用是**直连各厂商**的，
> 所以账本里通常看不到本应用的调用条目。因此「路由是否真的被调用」以
> `/api/diagnostics` 的调用记录（目标 host + 真实上游报错文本）与事件流的 `message_end` 正文为准，
> 账本只作为补充参考——不要拿账本"没有条目"去反推"发生了静默降级"。

## 9. 已知边界（如实写，不写做不到的能力）

- **没有工具调用**：成员只会说话，不会读写文件或执行命令——这是刻意的，
  群聊场景下放开工具权限等于放开额度与安全边界。
- **"没有任何密钥也能跑完整链路"是不成立的**：没有可用 provider 时，组长（唯一入口）
  会失败，于是没人被 @、组员与记录员都不会被唤醒，本轮零产出——这时系统会给出
  顶层 `error` + `run_end{aborted:true}`，而不是假装成功。真正的零密钥演示请用
  `--mock-only` / `-MockOnly`（全部成员切到离线通道）。
- **串行发言**：被 @ 的组员是**依次**唤醒的，当前没有并发派发实现（并发会让额度与顺序
  更难控制，暂不做）。
- **豆包**：只有 `doubao-seed-2-1-pro-260915` 实测可用；`doubao-seed-2-1-lite-260915`
  返回 `ModelNotOpen`（账号未开通），故未列入 `models`。
- **Kimi 成本**：`kimi-k2.6` 目录价约 $0.95/$4 每百万 token，比 DeepSeek 贵一个数量级，
  因此只给 2 次发言额度，定位是「第二意见」而不是主力；该账号另有 3 RPM 组织级限流。
- **单机单进程**：状态存在内存里，重启即清空；多人协作需要自行接 Redis 和数据库。
