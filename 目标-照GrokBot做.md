# ai-group 的目标：照 Grok Bot 做，只换模型

> 用户原话（2026-10-15）：
> **"我的初衷就是像 grokbot 那样，只不过是把他的模型换成各自厂商的模型罢了。"**

这句话把目标收拢了：**不是"参考它"，是"照它的功能做出来，然后每个 agent 换成不同厂商的模型"。**

---

## 一、Grok Bot 的功能清单 vs 你的现状

（依据：读过的源码 —— `group-chat.ts` / `agent-messaging.ts` / `inference-router.ts` / `component-names.json`）

| Grok Bot 的功能 | 源码依据 | 你的现状 |
|---|---|---|
| **1:1 私聊**（每个 agent 一条） | "This conversation is your private 1:1 DM with your user — no one else is here." | ❌ **没有** |
| **群聊** | `group-chat.ts` 整个文件 | ✅ 有 |
| **历史统一**（群聊发言出现在私聊里，带标签） | "your turns in these group chats appear in it, each tagged like [Group chat: ...]" | ❌ 没有 |
| **@ 唤醒**（@谁叫谁） | `parseGroupMentions` / `resolveResponders` | ✅ 有 |
| **未 @ 任何人 → 全员回应** | `if everyone \|\| mentioned.size === 0 → [...members]` | ❌ 你是"组长派活" |
| **(pass) 跳过发言** | `isPassContent()` | ✅ 刚补 |
| **@ 的分寸**（别刷屏） | agent-messaging.ts 一整段 | ✅ 刚补 |
| **`SendToAgent`**（agent 之间直接发消息，异步） | 同名工具 | ⚠️ 有 @，但**同步**（本轮就跑） |
| **`CreateAgent`**（运行时创建新 agent） | 同名工具 | ❌ 只能改 YAML |
| **`UpdateAgent`**（改别人的名字/描述） | 同名工具 | ⚠️ 只能改自己的 |
| **每个 agent 独立记忆** | `runner/sand-memory.ts` 26 KB | ❌ 没有 |
| **每轮换发言起点** | `orderRoundSpeakers()` | ❌ 固定顺序 |
| **禁止群嵌套** | `assertMembersAreNotGroups()` | — （你也不支持） |
| **私聊不外泄** | "Never reveal private one-on-one context" | — |
| **每条模型可不同** | ❌ 它是全局一个 provider | ✅ **你有，这是你唯一的差别** |
| **组长 + 验收** | ❌ 它没有 | ✅ **你的加分项** |

**结论：你缺的主要是「私聊」这一整块，加上动态创建/改 agent、独立记忆、异步消息。**

---

## 二、下一步要做：1:1 私聊 + 群聊并存

### 设计决定（已定）

**选项 A：私聊里的成员看得到群聊记录。**

理由：
- 你的成员**产出都在共享工作区**里，本来就互通；硬挡反而让它们在私聊里"不知道自己写过什么"
- Grok Bot 也选 A

**同时照抄它那条纪律**：**能看进来，不能漏出去** ——
`Never reveal private one-on-one context.`（你和它的私聊，别的 agent 看不到）

### 具体改动（四处）

**① 数据结构：`Message` 加一个字段**
```python
channel: str = "group"     # "group" 或 "dm:<成员id>"
```
**关键：必须有默认值** —— 旧的 `history.json` 里没有这个字段，
加了默认值才能**向后兼容**（旧消息读出来自动算群聊）。

**② 引擎：加一条"私聊路径"**
- 私聊消息 → **只唤醒那一个成员**，不走组长、不派发、不验收
- 那个成员的提示词 =
  ```
  你的私聊历史（channel == "dm:<我>"）
  你在群里的发言（channel == "group" 里 sender == 我），每条带 [群聊: "群名"]
  共享工作区 + 任务板（这些照旧）
  ```

**③ API**：`/api/chat` 加 `channel` 参数；新增一个"读某成员私聊"的接口

**④ 前端**：左栏点成员 → 切到私聊视图；顶部「正在和 X 私聊 · 回到群聊」

### 风险点与防护

| 风险 | 防护 |
|---|---|
| 旧 `history.json` 没有 `channel` → 读崩 | 字段带默认值；读的时候 `getattr(m, "channel", "group")` |
| 私聊把群聊视图搞乱 | 群视图**明确过滤** `channel == "group"` |
| 改坏用户现有 12 个工作区文件 / 聊天记录 | 改前先备份 `server/data/`；改完用哈希比对 `config/` |
| 上下文爆炸 | 私聊提示词里的群聊部分**只取该成员自己的发言**，不是整个群记录 |

### 验证方式

1. 旧 `history.json` 能正常加载（不丢消息、不报错）
2. 私聊只叫一个人（其它成员不发言）
3. 私聊里那个成员**看得到**自己在群里的发言
4. 群聊视图**看不到**私聊内容
5. 群聊功能不受影响（回归）
6. 打包版实测

---

## 三、再往后（按优先级）

1. **`CreateAgent` / `UpdateAgent`** —— 让组长能运行时建人、改人
2. **独立记忆**（`sand-memory.ts` 26 KB 是参考）
3. **异步 `SendToAgent`** —— 发出去就返回，对方之后被唤醒
4. **未 @ 任何人 → 全员回应**（自由讨论模式）
5. **每轮换发言起点**

---

## 四、我这几轮学到的（写给下一轮的自己）

**先读代码，再下结论。** 这几轮我至少猜错了四次：
- "它只有 Kimi 像智能体" → 实测 5/5 都能写文件
- "max_tokens 太小导致写不出文件" → 拉齐之前也能写
- "成员之间不能直接通信" → `can_dispatch` 本来就是 `True`
- "Grok Bot 是单 agent" → 它有 `Broadcast to agents` 和 `Agent network`

**每一次都是"看到文件名/表面现象就下判断"，然后被代码或实测推翻。**

**另一类错**：改了东西没验证它生效（`clear_stop()` 那句 patch 没落地、
`⚠` 字符在 GBK 控制台崩掉、半成品 patch 留成坏状态）。
→ **凡是要落地的改动，都要验证"它真的生效了吗"，不只是"跑完了"。**
