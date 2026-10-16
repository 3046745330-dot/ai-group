# 把 Codex / Kimi / 豆包 拉进一个群 —— 基于 DSH AgentTeams 的实现方案

> 版本：v1.1（已按「先做成 LLM，不一定是 agent」收敛范围）
> 适用环境：DSH Desktop，profile `desktop`，`@nanmicoder/dsh-agent-teams` 0.1.23
> **本期范围**：只接 LLM——即路径 A（模型级）。不做 Codex / WorkBuddy 的 CLI 桥成员，路径 B 推迟到二期。

---

## 0. 一句话结论

**AgentTeams 里的"群员"不是"某个 App"，而是"一个模型 + 一套工具策略 + 一段系统提示"。**

所以"把 Codex、豆包、Kimi 拉进群"必须先回答一个问题：

> 你要的是**它们的模型**，还是**它们的产品**？

- 要模型 → 在 DSH 里注册 provider，成员直接跑那个模型。**推荐，这才是真正的群员。**
- 要产品 → 用一个"桥成员"包装 CLI/API 调用。能用到完整产品能力，但没有原生多轮，慢。

这两条路成本差一个数量级，能做的事也完全不同。

---

## 1. 本机现状核查（已实测，非推测）

| 项目 | 结论 |
|---|---|
| AgentTeams 插件 | `@nanmicoder/dsh-agent-teams` **0.1.23**，已挂载在 profile `desktop` |
| 默认模型 | `deepseek-account` / `deepseek-flash`，`reasoningEffort: high` |
| 自定义模型 provider 能力 | **已具备**。`@deepseek-ai/dsh-llm-pi-ai` 已捆绑在 `app.asar` 内，设置页可直接加 OpenAI/Anthropic 兼容端点，**无需写代码** |
| 外部 AI CLI | `codex` / `kimi` / `doubao` / `claude` / `gemini` / `ollama` **全部未安装**；全局 npm 只有 `serve@14.2.5` |
| WorkBuddy (CodeBuddy) | **已安装**，CLI 在 `C:\Program Files\WorkBuddy\...\cli\bin\codebuddy` |
| 已配置的 provider | 仅 DeepSeek 一家（`~/.dsh/.credentials.yaml` 里没有 Kimi / 豆包 记录） |
| 成员之间能否互发消息 | **能**。`agent_teams_send_message` 的收件人可以是任意成员名，不只是群主 |
| 团队规模上限 | `maxMembers` 默认 **8** |

也就是说：**你目前一个外部 AI 都还没接进来，需要从注册 API Key 开始。** 好消息是通道是现成的。

---

## 2. 概念映射：你的"群" ↔ AgentTeams

| 你的概念 | AgentTeams 对应物 | 说明 |
|---|---|---|
| **群主** | Captain = 当前 DSH 主会话 | 定目标、拆任务、持审批门、调度、有终止权 |
| **群员** | member = 独立子会话 | 独立上下文 + 独立模型路由 + 独立工具策略 + 持久身份 |
| **群聊** | mailbox（`agent_teams_send_message`） | 成员之间可点对点互发，也是一条消息就是一次模型步骤 |
| **群规 / 人设** | `executionPrompt`、profile 的 `protocol` | 每个成员的系统级指令 |
| **群任务** | task DAG | 谁做、依赖谁、验收标准、谁复审 |
| **禁言 / 限权** | `toolPolicies`（allow / deny） | 成员只能**按 id 选**，不能自己造策略 |
| **踢人** | `agent_teams_remove_member` | |

**必须知道的限制**（从插件源码读出）：

- `maxMembers` 默认 **8**，"所有 AI"必须收敛，3–5 个最实用
- 一个群主**同时只能带一个活动团队**（一个群 = 一个主会话）
- 成员默认**不能再开子代理**（`memberMaxDepth: 0`）
- 跨 provider 由 `agent_teams_add_member` 的 `provider` + `model` 参数表达；`memberModel` 只是全局默认覆盖

---

## 3. 三种接入路径

### 路径 A：模型级（同构）⭐ 推荐

把对方的**模型**注册成 DSH 的 provider，然后 AgentTeams 成员直接指定这个 provider/model。

**做法**：设置 → 模型 → 添加模型提供方 → 自定义模型 API → 填 Base URL + API Key + 模型 id。

**优点**：真正的独立群员——有独立上下文、能收消息、能认领任务、能跑工具、能被复审，面板上会标出它用的模型。
**缺点**：拿到的是**模型**，不是对方的产品。豆包 App 的联网、Kimi 的深度研究模式，这些拿不到。

适用：Kimi、豆包、通义、GLM、DeepSeek、以及任何 OpenAI / Anthropic 兼容端点。

### 路径 B：进程级（异构）

对方只有 CLI / 云服务（Codex CLI、WorkBuddy），用一个**桥成员**包装：

- 桥成员的模型随便挑（越便宜越好），它的 `executionPrompt` 只规定一件事：把群里的任务写成命令 → `pwsh` 调外部 CLI → 原样回贴 stdout/stderr/退出码。
- 工具策略给 `pwsh`、`read`、`write`、`edit`。

**优点**：用到对方的完整产品能力（Codex 的沙箱、WorkBuddy 的登录态）。
**缺点**：**不是原生多轮**，每次调用都是冷启动（WorkBuddy 约 50 秒/次），串行、不共享上下文，本质是"把外部 agent 当工具调"。它像群里被 @ 的外包同事，不是能随时插话的群员。

### 路径 C：不要做的

**网页版豆包 / Kimi 用浏览器自动化接群** —— 违反 ToS、易封号、选择器随时失效。
（你在 `hy4-preview` 那件事上已经做过一次"不冒封号风险"的决定，这里同理。）

### 决策速查

```
对方有 OpenAI/Anthropic 兼容 API？ ──有──▶ 路径 A（注册 provider）
                │
                无
                ▼
对方有 CLI 且你已登录/付费？ ──有──▶ 路径 B（桥成员）
                │
                无
                ▼
        只有网页版 ──▶ 放弃接入（路径 C）
```

---

## 4. 群员候选与接入参数

### Kimi（月之暗面 / Moonshot）

| 项 | 值 |
|---|---|
| Base URL | `https://api.moonshot.cn/v1` ✅ **已用你的 Key 实测通过**（`api.moonshot.ai` 返回 401，那是国际站，账号不通） |
| **provider id** | **`moonshotai-cn`** —— pi-ai 内置目录已有此路由，**不用手写 baseURL / api / models** |
| 协议 | `openai-completions` |
| 凭据 | pi-ai 默认读环境变量 `MOONSHOT_API_KEY`；在 DSH 设置页填 Key 则存入凭据库 `llm-pi-ai/moonshotai-cn` |
| pi-ai 目录内模型 | `kimi-k2.6`、`kimi-k2.7-code`、`kimi-k2.7-code-highspeed`、`kimi-k3` |
| **你的 Key 实际可用** | 只有 `kimi-k2.6`、`kimi-k2.7-code`（`GET /models` 实测）。**`kimi-k3` 用不了**，多半需充值或更高等级 |
| 价格（目录值，USD / 百万 token） | `kimi-k2.6`：$0.95 输入 / $4 输出（≈7 元 / 29 元）；`kimi-k3`：$3 / $15 |
| ⚠️ 已下线，不要用 | `moonshot-v1-*`（2026-08-31 下线）、`kimi-k2-0905-preview`（2026-05-25 下线）、`kimi-latest`、`kimi-k2.5` |
| 接入文档 | [获取 API Key](https://platform.kimi.com/docs/get-api-key) · [模型列表](https://platform.kimi.com/docs/models) · [定价](https://platform.kimi.com/docs/pricing/chat) |

> **重要发现**：Kimi 原生支持 Codex 使用的 **Responses API**，Codex CLI 可以**直连 `kimi-k3`**，不需要协议转换或本地代理，只需编辑 `~/.codex/config.toml`。
> 👉 这意味着你**不必买 ChatGPT 订阅**也能跑起 Codex —— 见 [在 Codex 中使用 Kimi K3](https://platform.kimi.com/docs/guide/codex-kimi)。

### 豆包（火山方舟 / Ark）

| 项 | 值 |
|---|---|
| Base URL | `https://ark.cn-beijing.volces.com/api/v3` ✅ **Key 已实测可鉴权**（`GET /models` 返回 135 个模型） |
| **pi-ai 内置 provider** | ❌ **没有**。pi-ai 的 41 个内置 provider 里无火山 / 豆包 / Ark，**必须手写路由**（`api` + `baseURL` + `models` 三件套缺一不可） |
| 协议 | `openai-completions` |
| ⚠️ **当前阻塞** | Ark 账号 **2132917122** 目前**一个模型都没开通**。实测 20 个候选模型，开通数为 **0** |
| 建议开通的模型 | `doubao-seed-2-1-pro-260915`（最新 pro，有免费额度活动）；省钱备选 `doubao-seed-2-1-lite-260915` |
| 开通路径 | [火山方舟控制台](https://console.volcengine.com/ark) → **开通管理** → 找到模型 → 开通 |
| 错误码辨析 | `ModelNotOpen` = 模型存在但**账号未开通**（去控制台开通即可）；`InvalidEndpointOrModel.NotFound` = 该模型 id 在此端点**根本不存在**（qwen / glm 等非豆包系都是这个），别浪费时间 |
| 接入文档 | [Base URL 及鉴权](https://docs.volcengine.com/docs/ark/base-url-and-authentication?lang=zh) · [API Key 配置](https://docs.volcengine.com/docs/ark/api-key?lang=zh) · [对话 API](https://docs.volcengine.com/docs/ark/chat-api?lang=zh) |

**手写路由的配置形状**（等模型开通后再启用）：

```yaml
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      doubao:                                   # provider id，小写连字符
        displayName: 火山方舟 Doubao
        api: openai-completions
        baseURL: https://ark.cn-beijing.volces.com/api/v3
        apiKeyEnv: ARK_API_KEY
        models:                                 # 手写路由必须给非空 models 列表
          - id: doubao-seed-2-1-pro-260915
            name: Doubao Seed 2.1 Pro
            contextWindow: 262144
```

> **注意**：pi-ai README 明确 —— 「A route pi-ai does not ship needs `api`, `baseURL`, and a non-empty `models` list；an unserviceable profile is refused where it is written」。所以这三项缺一个，配置会被直接拒绝。

### Codex（OpenAI）

| 项 | 值 |
|---|---|
| 有没有公开 API？ | **没有**。Codex CLI 的登录态是 ChatGPT 订阅，不能当 API 用 |
| 想当"模型"接 | 需要单独买 OpenAI API Key，接进来的是 `gpt-5-codex` **模型**，不是 Codex 这个 **agent** |
| 省钱替代方案 | **用 Kimi 的 key 驱动 Codex CLI**（见上方 Kimi 小节） |
| 安装 | `npm i -g @openai/codex`（本机未装） |

### 备选（同样是路径 A，改 Base URL 即可）

| 厂商 | Base URL |
|---|---|
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` |

---

## 5. 两种"群聊"玩法（这里有个关键区别）

### 玩法 1：任务式群 —— AgentTeams 原生，推荐先做

一个目标 → 群主拆成并行子任务 → 各群员分头干 → 一个汇总任务依赖它们全部完成。

```
        ┌─ kimi    : 写方案 A
目标 ────┼─ doubao  : 写方案 B
（群主） └─ codex   : 写方案 C
              └──────▶ 汇总（依赖 A/B/C）
```

特点：**并行真开工**，有验收标准，有失败重试，结果落进任务 DAG。适合"调研 / 实现 / 审计 / 对比"。

### 玩法 2：讨论式群 —— AgentTeams **不是**为它设计的

如果你要的是"三家就同一个问题**自由吵几轮**、互相看到对方的话、随时插嘴"，那 AgentTeams 的任务 DAG 模型其实不合适——成员被任务驱动，不是被聊天记录驱动。

真要做自由讨论，得自己写一个 orchestration loop：

```
chat_log = [用户提问]
for round in 1..N:
    for ai in [kimi, doubao, codex]:
        reply = call(ai, system_prompt + 完整 chat_log)
        chat_log.append((ai, reply))
```

这种**直接用 Python 脚本调各家 API 更简单**，不需要 AgentTeams，也不需要 DSH。

> **建议**：先做玩法 1（AgentTeams 原生，当天能出效果），确认通道都通了，再考虑要不要写玩法 2。

---

## 6. 配置方案

AgentTeams 的配置放在 `C:\Users\yyy\.dsh\profiles\desktop\cordis.patch.yml`，追加一个 `agent-teams` 条目即可定义"群模板"。

完整可粘贴的配置见同目录文件：**`cordis.patch.agent-teams.yml`**

插件 v0.1.23 的 profile schema（实测自 `lib/index.js`）：

| 字段 | 说明 |
|---|---|
| `description` | 群简介，会进群主的提示词 |
| `taskPlanning` | `captain` = 群主按目标现拆任务图（不写 `tasks`）；`seed` = 固定任务图（必须写 `tasks`） |
| `members[]` | `name`（必填）/ `role` / `provider` / `model` / `reasoning_effort` / `executionPrompt` / `fallback` / `tool_policy` |
| `toolPolicies` | 群规，按 id 复用；成员只能选，不能自己造 |
| `maxMembers` | 默认 8 |
| `memberMaxDepth` | 默认 0，群员不能再开子代理 |

### 6.1 关键认知：AgentTeams 里没有"零工具"的成员

这是本方案最需要先讲清楚的一点：

> **群员在架构上永远是一个 DSH 子会话（agent），不可能是"裸的 LLM"。**

但"做成 LLM"是可以做到的——**降级**：

1. 用 `toolPolicies` 把它的工具**削到只剩说话能力**
2. 用 `executionPrompt` 明确禁止它行动

两个现成的群规（`allow` 列表里只有真实存在的工具名，所以一定生效）：

```yaml
toolPolicies:
  llm_only:                 # 只会说话：read + 4 个协调工具
    allow: [read]
  llm_research:             # 会自己查资料：再加检索类工具
    allow: [read, glob, grep, web_search, web_fetch]
```

⚠️ **做不到绝对的零工具**：四个协调工具（`claim_task` / `update_task` / `send_message` / `status`）在 `allow` 策略下会被**自动保留**，这是插件写死的行为，用于让成员汇报和认领任务。

**这个设计的直接后果**：群员只负责"想和说"，**落地必须由群主承担**（写文件、跑命令、验证）。而群主就是 DSH 本体，本来就有全套工具——所以这恰好是合理的分工。

`fallback` 给成员兜底：某个成员的路由挂了（key 过期 / 限流 / 模型下线）就退回 DeepSeek，避免一个群员拖死整个群。

---

## 7. 推荐分阶段落地

### 阶段 0：拿 Key（现在就能做，零成本）

1. 注册 [Kimi 开放平台](https://platform.kimi.com/)，到 [API Keys](https://platform.kimi.com/console/api-keys) 创建并保管好 Key
2. 注册火山引擎，开通方舟，创建 API Key + 推理接入点，记下 `ep-xxxx` 或模型名
3. **记录两件事**：实际 Base URL、实际可用模型 id（这两样各家都改得很勤，以控制台为准）

### 阶段 1：单点打通（先只接 Kimi）

- 在 DSH 设置里加一个 provider，命名 `moonshot`
- 在设置页的模型选择器里试跑一句话，**确认这个 provider 真能用**
- 这一步不涉及 AgentTeams，排错面最小

### 阶段 2：建 2 人群试跑

```
/agent-teams 用一句话分别介绍你自己，然后各自给出"大学生做 AI Agent 项目"的一条具体建议
```

群主（DeepSeek）+ 1 个 Kimi 群员。跑完到 **AgentTeams 活动面板**确认三件事：
1. 成员节点上标的是 **moonshotai-cn / kimi-k2.6**，不是 deepseek（证明路由真的生效了，这是最容易假成功的地方）
2. 任务真的被 claim → completed，不是卡在 pending
3. 顺手确认群员**没有**乱调工具（`tool_policy: llm_research` 是否真的把 `write`/`pwsh` 拿掉了）

### 阶段 3：接入群模板 ✅ 已完成

`agent-teams` 条目已写入 `cordis.patch.yml`（备份 `.bak-20261012-203202`），**无需重启**就生效——
运行时已确认模板 `llm-group` 被插件识别。用法：

```
/agent-teams --profile llm-group 用一句话介绍你自己
```

### 阶段 4：加豆包（本期范围到此为止）

重复阶段 1–2 的流程，把 `provider: ark` / `model: ep-xxxx` 换成你控制台里的真实值。

**Codex 桥本期不做。** 等 LLM 群跑顺了、确认这套东西对你有用，再考虑接 CLI 类外部 agent —— 那是另一条技术路线（路径 B），投入产出比要重新算。

---

## 8. 成本控制（你的毕设预算 ≤100 元）

| 手段 | 说明 |
|---|---|
| 优先吃免费额度 | 豆包有开发者免费额度活动；Kimi 新账号通常有赠送额度 |
| 群员用便宜模型 | 群主用 DeepSeek 拆任务，群员里对话密集的可以用 `kimi-k2.6` 而不是 `kimi-k3` |
| 用 AgentTeams 的 `toolPolicies` 限制 | 给不需要写文件的群员只留 `read/glob/grep/web_search`，减少无效工具调用轮次 |
| 一个群不超过 3–4 人 | `maxMembers` 上限 8，但每多一个成员就是一条独立并发计费链路 |
| 单次试跑只看 token | DSH 的用量面板能看到实际消耗，先跑一次小任务量一下，再决定要不要放开 |
| 别用浏览器自动化 | 省不了钱，还会封号 |

**建议先设一个心理红线**：阶段 1–2 的验证总花费控制在个位数元。真正的成本会出现在阶段 4 之后的长任务上。

---

## 9. 风险与红线

| 风险 | 应对 |
|---|---|
| **假成功**：成员标着 `moonshot` 但实际还在用 DeepSeek | 每次接新 provider 后，**必须**去活动面板核对成员节点上的模型名 |
| 模型 id 过期 | Kimi 一年内下线了三代模型（`moonshot-v1` / `kimi-k2` / `kimi-k2.5` / `kimi-k2-0905-preview` 全下线）。配置前到官方模型列表页核对一次 |
| provider 挂掉拖死全群 | 用 `fallback` 兜底到 DeepSeek |
| 桥成员超时 | WorkBuddy 单次约 50 秒，Codex 冷启动也慢。给桥成员单独算超时预算，别和普通成员混在一个依赖链的同一层 |
| 一个群主只能带一个群 | 想同时跑多个群需要多个主会话 |
| 多进程改同一团队状态不一致 | 插件只保证单进程内串行；别同时从两个 DSH 实例操作同一团队 |
| **ToS 风险** | 网页版自动化接入绝对不做 |

---

## 10. 参考资料

- [AgentTeams 插件 `@nanmicoder/dsh-agent-teams`](https://www.npmjs.com/package/@nanmicoder/dsh-agent-teams)（本机 0.1.23；README_ZH 的"配置"与"命名多角色团队配置"两节是配置依据）
- [Kimi API 快速开始](https://platform.kimi.com/docs/get-api-key) · [模型列表](https://platform.kimi.com/docs/models) · [定价](https://platform.kimi.com/docs/pricing/chat) · [在 Codex 中使用 Kimi K3](https://platform.kimi.com/docs/guide/codex-kimi)
- [火山方舟 Base URL 及鉴权](https://docs.volcengine.com/docs/ark/base-url-and-authentication?lang=zh) · [API Key 配置](https://docs.volcengine.com/docs/ark/api-key?lang=zh) · [对话 API](https://docs.volcengine.com/docs/ark/chat-api?lang=zh)
- [Doubao-Seed-2.1-pro 免费额度解析（第三方文章，仅作参考）](https://www.volcengine.com/article/2609208)

---

## 11. 决策记录与剩余待确认项

**已定（v1.1）**

| 决策 | 结论 |
|---|---|
| 接入深度 | **只接 LLM**，不接 CLI 类外部 agent |
| 路径 | 路径 A（模型级）。路径 B / C 推迟或放弃 |
| 群形态 | 任务式群（AgentTeams 原生）；讨论式群暂不做 |
| 群模板 | 固化为 `/agent-teams --profile llm-group` |
| Codex 桥 | 本期不做 |

**仍需你确认**

1. **群员名单**：只接 Kimi，还是 Kimi + 豆包两个都上？
2. **Kimi 用哪档**：`kimi-k2.6`（便宜）还是 `kimi-k3`（旗舰，1M 上下文）？—— 注：你的 Key 目前**用不了 k3**
3. ~~要不要我把 provider 配置和 profile 写进 `cordis.patch.yml`~~ ✅ 已完成
4. **群主是否固定为 DeepSeek**？还是也想让你自己（用户）随时插话当"群管理员"？

---

## 12. 首次实跑记录（2026-10-12）✅ 通过

### 12.1 结果

团队 `ai-llm-group`，用模板 `llm-group`，任务 `t1` 由 Kimi 群员完成。

**Kimi 群员的回答**（原文）：

> (1) 最容易踩的坑：把 Agent 理解成"接大模型+挂一堆工具"，先搭编排框架再找场景，时间几乎全花在调 prompt 和修工具报错上；demo 能跑，却没有可评估的任务闭环，说不清效果，也写不出论文。
> (2) 规避：先选一个窄且有客观判分标准的小任务，手工做 20 条当基线，定下成功率与成本指标，用最简实现先跑通端到端，再逐个加工具与反思。
> 反例：若任务没有客观判分（如"做个研究助手"），工程量再大也只是看起来聪明，无法证明价值。

### 12.2 路由验证（关键）

| 证据 | 结果 |
|---|---|
| 团队成员节点 | `kimi · moonshotai-cn/kimi-k2.6 · policy llm_research` ✅ |
| **`.dshw-usage.json` 账本** | `{"model":"kimi-k2.6","tokens":11898,"cost":0.00810892}` ✅ **决定性证据** |
| 实际花费 | **不到 1 分钱**（11898 tokens） |

> **验证方法论（重要）**：判断"跨 provider 路由是否真生效"，**只看 UI 或只看回答内容都不可靠**——配了 `fallback` 后，路由失败会静默回落，UI 仍可能显示原模型。唯一可靠的证据是 `~/.dsh/.dshw-usage.json` 的 events 里是否出现目标 model 名。

### 12.3 踩到并修好的两个坑

**坑 1：`kimi-k2.6` 在 pi-ai 目录里缺 thinking 元数据（真 bug）**

首次运行直接失败：

```
The `content[].thinking` in the thinking mode must be passed back to the API.
(request_id: ad360851-..., code INVALID_REQUEST)
```

根因：pi-ai 内置目录里 `kimi-k3` 有 `compat.requiresReasoningContentOnAssistantMessages: true`，而 **`kimi-k2.6` 没有**。因此多轮 / 带工具调用时该回传的 thinking 被丢弃，Moonshot 拒收。

修法（已写入 `cordis.patch.yml`）：

```yaml
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      moonshotai-cn:
        apiKeyEnv: MOONSHOTAI_CN_API_KEY
        modelOverrides:
          kimi-k2.6:
            compat:
              requiresReasoningContentOnAssistantMessages: true
```

> 这个失败**反而排除了假成功风险**：错误来自 Moonshot 服务器（带 Moonshot 的 `request_id`），证明凭据解析 ✅、路由指向 ✅、请求真的到达 `api.moonshot.cn` ✅。
> **备选方案**（若该补丁失效）：`reasoningEfforts: false` 把 k2.6 声明为非推理模型，从根上绕开 thinking 回传要求。

**坑 2：我自己的操作失误 —— attempt 被白烧一次**

建任务时传了 `assignee: kimi`，调度器**立即开跑** attempt 1；我随后又调 `reassign_task`「确保唤醒」，结果**打断并重开了 attempt 2**。

> **规则**：`create_task` 已经给了 `assignee`，就**不要**再 `reassign_task`。两者叠加 = 杀掉第一次尝试。

### 12.4 结论

**"把所有 AI 拉进一个群"在 DSH 上可行，且已跑通。** 已验证的完整链路：

```
DSH 设置页填 Key
  → 写入 cordis.patch.yml (providers.moonshotai-cn)
  → 密钥存入 .credentials.yaml 的 refs 段
  → AgentTeams 模板 llm-group 展开成员（provider/model/tool_policy 全部生效）
  → 群主派任务 → Kimi 群员在 moonshotai-cn/kimi-k2.6 上执行 → 回贴结果
```
