# 技术审查：@ 触发派发循环的正确性与终止性

审查者：kimi（task t7 / attempt 220333fe-1fe4-40d2-a3ac-6d918834f7bd）
方式：**只读静态审查**（read / glob / grep），未执行任何命令，未修改任何文件（按队长更正：本会话无写权限，故交付物为正文，由队长落盘到 `ai-group/docs/tech-review-dispatch-loop.md`）。
审查快照：`ai-group/group/engine.py`（691 行）/ `llm.py`（432 行）/ `config.py`（322 行）/ `config/models.yaml`（129 行）/ `group/app.py:71` / `web/app.js:38-41`。
**行号漂移警告**：工程成员正在并发修改这些文件（我两次读 engine.py 之间文件从 631 行涨到 691 行，`_note_route`/`route_summary` 是后加的）。下面每条都附**被引用的代码原文**，请按文本定位，不要按行号盲改。
**交付说明**：命令执行数 = 0（无 shell 工具），故无 commandsRun；如需实测复现，见文末 4 条断言口径。本任务未修改任何文件。

---

## 0. 终止性判定（先给答案）

**不存在无限循环；终止性可证。但真正兜住终止的是 `_run_emitted` 单调递增，而不是 `max_depth`。**

- 证明：`while` 进入条件（`engine.py:339 while mentions and depth < self.limits.max_depth and self._budget_left():`）已保证预算有余；从 L339 到 L371 `got = await self._speak_counted(...)` 之间**没有任何代码**使 `_run_emitted` 增加（L342-346、L353-357 的 `notice` 不计数）；L363 `if not queue: break` 保证队列非空。因此每轮迭代至少执行一次 `_speak_counted`（`engine.py:452-455`，其中 `self._run_emitted += 1`）→ `_run_emitted` 每轮严格 +1 → 迭代数 ≤ `max_messages`。
- "A @ B、B @ A" 方向性结论：**成员→组长这一向不可能成环**，因为 `engine.py:350-352` 无条件 `continue`（`if agent.id == captain.id: continue` / `if agent.role == "captain": continue`）。可成环的只有成员↔成员（kimi、doubao 的 `can_dispatch` 都是 true，models.yaml:53 / :89），且被 depth 与 budget 双重限制。
- 但**"会终止" ≠ "会对"**：D1/D6 是"护栏失效"，D2/D3/D5 是"静默丢弃"——用户与运维在事件流里都看不到，这是本次审查最值得修的一类问题。

---

## D1 [high] 同一轮内同一成员可被重复入队，`max_turns_per_run` 硬上限被突破

**位置**：`engine.py:337-359`（队列构建）、`:369-373`（token 级去重）、`:354`（上限检查）、`:452-455`（计数）

```python
339: while mentions and depth < self.limits.max_depth and self._budget_left():
340:     queue: list[tuple[AgentSpec, str]] = []
341:     for token in mentions:
342:         agent = self.resolve_mention(token)
...
354:                 if self._run_spoke.get(agent.id, 0) >= agent.max_turns_per_run:
355:                     await self.emit("notice", ...)
358:                     continue
359:                 queue.append((agent, f"@{agent.id} 被组长点了名，..."))
...
370:                 for token in got:
371:                     if token not in next_mentions:      # ← 按“原始 token 字符串”去重
372:                         next_mentions.append(token)
```

**现象**：跨轮没有队列传递（`engine.py:386 mentions = next_mentions`，上一轮的 mentions 被整条丢弃），同一轮内的去重是**原始 token 字符串**级（L371-372），不是解析后的 agent id 级；而 L354 的每成员上限检查发生在**队列开跑之前**，用上一轮遗留的 `_run_spoke` 判定，队列执行期间（L369-373）**从不复查**。

**构造输入（真实配置值）**：组长回复里写 `@kimi @Kimi·论证担当 @论证`。
- `extract_mentions`（config.py:264-277）按小写 key 去重，得到 3 个互不相同的 key：`kimi` / `kimi·论证担当` / `论证`；
- `resolve_mention` 三次都解析到 **kimi**：L126（id 精确）、L129（display_name 精确）、L132（display_name 子串 `token_norm in agent.display_name.lower()`，`"论证" in "kimi·论证担当"`）；
- L354 三次检查时 `_run_spoke["kimi"]` 都还是 0 < 2（models.yaml:52 `max_turns_per_run: 2`）→ `queue = [kimi, kimi, kimi]`；
- 执行阶段 L369-373 无复查 → kimi 在一次派发轮里被唤醒 **3 次**，超过声明的 2 次。

**为什么是问题**：声明的额度护栏失效（同一成员的多个别名是模型输出的常态：id / 全名 / 标签都印在 `_member_roster()`，engine.py:163-168）；3 次 = 3 个额度单位（真金白银），并挤掉后续成员的预算。

**最小修法**：按解析后的 `agent.id` 去重 + 执行前复查。

```python
# engine.py:337-359 队列构建
queue: list[tuple[AgentSpec, str]] = []
seen: set[str] = set()
for token in mentions:
    agent = self.resolve_mention(token)
    if agent is None:
        await self.emit("notice", message=f"@{token} 无法定位，已忽略。", level="warn")
        continue
    if agent.role == "captain":
        await self.emit("notice",
            message=f"@{token} 指向组长：组员不能把组长叫回来（组长在每轮末尾由内核自动收敛），已忽略。",
            level="info")
        continue
    if agent.id in seen:                       # ← D1 修复：按 id 去重
        continue
    if self._run_spoke.get(agent.id, 0) >= agent.max_turns_per_run:
        await self.emit("notice",
            message=f"{agent.display_name} 本轮成功发言已达上限（{agent.max_turns_per_run} 次），跳过。",
            level="info")
        continue
    seen.add(agent.id)
    queue.append((agent, f"@{agent.id} 被组长点了名，请就上面的拆解给出你这一部分的产出。"))
...
for agent, task_text in queue:
    if not self._budget_left():
        await self.emit("notice",
            message=f"预算用尽（max_messages={self.limits.max_messages}），{agent.display_name} 未被唤醒。",
            level="warn")
        break
    if self._run_spoke.get(agent.id, 0) >= agent.max_turns_per_run:   # ← 执行前复查
        continue
    ...
```

---

## D2 [high / 可观测性] `can_dispatch` 过滤发生在 `_speak` 出口，且**完全静默**

**位置**：`engine.py:556-559`（过滤）

```python
555:        # 只有拥有派发权的角色，其 @ 才会真正触发新发言（防止互相 @ 吵起来）
559:        return raw_mentions if agent.can_dispatch else []
```

**过滤生效的层级**：在 `GroupEngine._speak()` 的**返回语句**处（engine.py:559），即模型输出已经流式播完、消息已经落库之后。对比：
- `engine.py:542 mentions=raw_mentions` → **原始 @ 被原样写进 `Message`**，并通过 `engine.py:550 await self.emit("message_end", ..., message=recorded.to_dict())` 广播；
- 四种丢弃里，只有这一种**没有任何事件**：不存在的成员 → `notice/warn`（L342-346）；组长 → 无事件（见 D3）；额度超限 → `notice/info`（L353-357）；`can_dispatch: false` → **零事件**。

**可观测性缺口（可检验）**：`demo-bot` 的 `can_dispatch: false`（models.yaml:69）。若它在回复里 @ 了别人：
1. 事件流里出现 `message_end.message.mentions=["doubao"]`，却**没有任何** `notice`/`dispatch_skipped` 说明为何没唤醒；
2. `web/app.js:38-41` 的 `renderRich` 用与后端一致的正则把任何 `@token` 渲染成高亮 `<span class="mention">` → **界面把一个永远不会生效的 @ 显示得和能生效的一模一样**；
3. `app.py:146 "call_log": CLIENT.call_log[-120:]` 也不会记录"某次 @ 被拒"（本来就没发起调用），事后无法归因；`run_stats()`（engine.py:672-679）同样不区分。

**最小修法**：在 L559 前发 notice，并用**已有的** `Message.meta` 字段（engine.py:54 `meta: dict = field(default_factory=dict)`）把"是否可派发"带给前端。

```python
# engine.py:535-548 里给 recorded 加一行
meta={"dispatchable": agent.can_dispatch},

# engine.py:555-559 改为
if raw_mentions and not agent.can_dispatch:
    await self.emit("notice",
        message=f"{agent.display_name} 没有派发权（can_dispatch=false），其 "
                + "、".join(f"@{m}" for m in raw_mentions) + " 只是文字，未唤醒任何成员。",
        level="info")
    return []
return raw_mentions
```
前端：`web/app.js:39-41` 在 `renderRich` 里对 `meta.dispatchable === false` 的消息把 `.mention` 渲染为灰色（`<span class="mention muted">`），CSS 加一条 `.mention.muted { color: var(--muted); text-decoration: line-through; }`。

---

## D3 [medium] `@组长` / `@DeepSeek` 被静默丢弃，与 `resolve_mention` 注释自相矛盾

**位置**：`engine.py:350-352`

```python
350:                 if agent.id == captain.id:
351:                     continue
352:                 if agent.role == "captain":
353:                     continue
```

**现象**：`resolve_mention` 的 docstring 明确宣称宽松匹配就是为兜住 `@DeepSeek`（`engine.py:119 "例如 @Kimi·论证担当、@组长、@DeepSeek、@论证（标签）"`）。`@DeepSeek` 确实在 L132 子串命中 captain，但紧接着在 L350 被无条件 `continue`，**没有任何 notice**。

**为什么是问题**：组员最自然的收尾动作"@组长 请再收敛一次"是一个静默黑洞——模型以为已交接，用户以为组长会回来，实际组长只在 `engine.py:375-384` 那一次**引擎内部**收敛里出现。这是"引擎行为与自身文档矛盾"的可检验实例。

**最小修法**：把 L350-352 换成一条 `notice/info`（见 D1 片段），文案明确"组员不能把组长叫回来"。

---

## D4 [high] `resolve_mention` 的宽松匹配有四处缺陷

**位置/顺序**：`engine.py:116-140`
id 精确（L125-127）→ display_name 精确（L128-130）→ **display_name 子串（L131-133）** → tag 精确（L134-136）→ **id 子串（L137-139）** → None（L140）。
两个子串分支**没有最小长度约束、也没有优先级保护**，且子串匹配排在 tag 精确匹配**之前**。

### D4a 单字符 token 误命中成员（真实误匹配，会花钱）
| 构造输入 | 命中分支 | 结果 | 后果 |
|---|---|---|---|
| `@i` | L132 `"i" in "kimi·论证担当"` | **kimi** | kimi 被唤醒、真发起一次付费调用 |
| `@m` | L132 | **kimi** | 同上 |
| `@o` | L137 `"o" in "demo-bot"` | **demo-bot** | 唤醒离线演示成员 |
| `@a` / `@t` | L137 `"a" in "captain"` / `"t" in "captain"` | captain | 随后被 L350 静默吃掉（污染预算判定） |

（已逐项核对：`组长·deepseek`/`kimi·论证担当`/`演示·离线模型`/`豆包·快速反驳` 四个 display_name 里不含 `a`，所以 `@a` 一定走到 id 子串分支；`@i` 一定在 L132 命中 kimi。**这些是构造输入，不是线上必现**，但一个被截断的 @、`x@i` 这类文本都会触发真实调用。）

**最小修法**：宽松匹配要求 `len(token_norm) >= 2`；精确匹配（id/display_name/tag 三类合并）先行且唯一。

### D4b 没有前置词边界：邮箱/URL 里的 @ 被当成群内提及
**位置**：`config.py:261`
```python
261: _MENTION_RE = re.compile(r"@([A-Za-z0-9_\-\.\u4e00-\u9fff·・•]+)")
```
- 输入 `请联系 zhang@kimi 复核` → 正则命中 `@kimi` → token `"kimi"` → `engine.py:125-127` id 精确匹配 → **kimi 被唤醒并计费**；
- 输入 `zhang@doubao.com` → 字符类含 `.` → token `"doubao.com"` → L140 返回 None → 用户看到 `engine.py:344` 的文案 **"组长 @ 了一个不存在的成员「doubao.com」"**（假警告）。

**最小修法**（前后端同步改，否则高亮与派发不一致）：
```python
# config.py:261
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_\-\.@])@([A-Za-z0-9_\-\.\u4e00-\u9fff·・•]+)")
```
```js
// web/app.js:40（同一 lookbehind）
esc(text).replace(/(?<![A-Za-z0-9_\-\.@])@([A-Za-z0-9_\-.·\u4e00-\u9fff\u30fb\u2022]+)/g, '<span class="mention">@$1</span>')
```

### D4c tag 重复且无歧义提示 → 该标签只能命中配置里第一个成员
**输入**：`@组员`
**路径**：四个 active display_name 都不含"组员" → 落到 L134-136 `any(token_norm == tag.lower() ...)`，返回 `active_agents` 里**第一个**命中者。`active_agents` 顺序 = models.yaml 声明顺序 → kimi（`models.yaml:54 tags: [组员, 长文, 论证]`）永远先于 doubao（`models.yaml:90 tags: [组员, 反驳]`）。**`@组员` 永远只命中 kimi，doubao 用这个标签不可达，且没有任何提示。**
**为什么是问题**：`_member_roster()`（engine.py:163-168）把两个成员的标签都打印给模型看（`标签 组员,长文,论证` / `标签 组员,反驳`），模型照抄 `@组员` 是完全自然的输出；结果一半组员静默失约。
**最小修法**：精确阶段收集全部命中，`len > 1` 时返回"歧义"而不是赌第一个（见下）；同时改 models.yaml：`kimi: tags: [论证, 长文]`、`doubao: tags: [反驳, 第二意见]`。

### D4d 顺序缺陷（子串先于 tag）是"一次配置编辑就变静默误路由"的隐患 —— 当前**无现网实例**
只要某个 tag 恰好是**另一个成员** display_name 的子串，tag 分支就永远不可达，会被路由到那个子串成员。**我逐项核对了当前 models.yaml 的 9 个 tag × 4 个 active display_name，目前不存在这种碰撞**（只有 组长⊂组长·DeepSeek、论证⊂Kimi·论证担当、离线⊂演示·离线模型、反驳⊂豆包·快速反驳，四条都是**同一个成员**）。所以这条是潜伏缺陷，不是正在发生的故障——这是本报告唯一"没有现网实例"的结论，明确标注。

**D4 统一修法（可直接替换 engine.py:116-140）**
```python
def resolve_mention(self, token: str) -> tuple[AgentSpec | None, str]:
    """返回 (成员, 原因)；原因 ∈ {"ok", "unknown", "ambiguous"}。"""
    token_norm = token.strip().lower().rstrip("·・•")
    if not token_norm:
        return None, "unknown"
    # ① 精确：id / display_name / tag 三类平等，且必须唯一
    exact = [a for a in self.active_agents
             if token_norm in (a.id.lower(), a.display_name.lower())
             or any(token_norm == t.lower() for t in a.tags)]
    if len(exact) == 1:
        return exact[0], "ok"
    if len(exact) > 1:
        return None, "ambiguous"          # D4c：@组员 归为歧义而不是赌第一个
    # ② 宽松：单字符一律拒绝；歧义一律拒绝
    if len(token_norm) < 2:               # D4a
        return None, "unknown"
    loose = [a for a in self.active_agents
             if token_norm in a.display_name.lower() or token_norm in a.id.lower()]
    if len(loose) == 1:
        return loose[0], "ok"
    return (None, "ambiguous") if len(loose) > 1 else (None, "unknown")
```
调用方（engine.py:340-347）：
```python
agent, why = self.resolve_mention(token)
if agent is None:
    await self.emit("notice",
        message=(f"「{token}」命中多个成员，无法唯一定位，已忽略。" if why == "ambiguous"
                 else f"@ 了一个不存在的成员「{token}」，已忽略。"), level="warn")
    continue
```

---

## D5 [high / 可观测性] 预算与深度的截断全部静默 + 丢弃归属被误标成"组长"

**位置**：`engine.py:339`（while 条件）、`:363-364`（`if not queue: break`）、`:369-370`（`if not self._budget_left(): break`）、`:375`（收敛前的预算检查）

```python
339:        while mentions and depth < self.limits.max_depth and self._budget_left():
363:            if not queue:
364:                break
369:                if not self._budget_left():
370:                    break
375:            if self._budget_left() and self._run_spoke.get(captain.id, 0) < captain.max_turns_per_run:
```

**现象**：
- (a) L339 因 `depth >= max_depth` 退出时，L386 刚赋值的 `mentions` 被直接丢弃，**零事件**；
- (b) L369-370 预算耗尽 `break`，剩余队列成员既不发言、也不报事件（`agent_status` 也不会变，仍是 idle）；
- (c) L375 预算不足/组长额度用尽 → 收敛静默不发生；
- (d) **默认配置下真正生效的是深度上限，不是额度上限**：`app.py:71 ENGINE = GroupEngine(CFG, CLIENT, HUB.broadcast, RunLimits())` → `engine.py:70-72` 的 `max_messages=10, max_depth=2`；默认 4 个可派发成员（captain/kimi/demo-bot/doubao）单轮最多 3 组员 + 1 收敛 = 4 次，两轮 + 开场 = **9 ≤ 10**。也就是说**正常情况永远撞不到额度上限，撞到的是深度上限——而深度截断恰恰是最静默的那条路径**。叠加 D1 的重复入队（+1 次 ≈ 10）就会把"静默 break"变成可达路径。

**为什么是问题**：prompt 明确鼓励组长"还有缺口就继续 @ 人"（L376-380），引擎却静默拒绝；用户看到记录员纪要，但不知道"有成员被点到却没发言"；`run_stats()`（engine.py:672-679）也不区分"没派"与"派了没做"。
**旁证（做对的部分）**：`context_trimmed`（engine.py:226-236）在丢历史时**确实**发了事件——这条"不静默"的纪律是对的，D5 的诉求只是把它推广到派发/预算/深度的所有丢弃路径。

**D5b（同处，低）**：`engine.py:344` 的文案硬编码 `"组长 @ 了一个不存在的成员…"`，但该 token 经常来自**组员的回复**（L369-373 / L382-384 把 `got` 累积进 `next_mentions`，来源信息在 L365/L386 就被丢掉了）。用户会把"某个组员 @ 错了人"读成"组长 @ 错了人"。

**最小修法**：把 `next_mentions` 改成 `list[tuple[str, str]]`（`(token, 来源 agent_id)`）；三处退出各发一条结构化事件（比如 `dispatch_skipped`，字段 `token/target/reason`，`reason ∈ {unknown, ambiguous, captain, no_dispatch_right, turn_limit, budget, depth}`），并在 `run_end` 附 `skipped` 计数：
```python
# 紧随 while 之后
if mentions and depth >= self.limits.max_depth:
    await self.emit("notice",
        message=f"已达 @ 触发深度上限 max_depth={self.limits.max_depth}，以下 @ 未执行："
                + "、".join(f"@{t}" for t, _ in mentions), level="warn")
elif mentions and not self._budget_left():
    await self.emit("notice",
        message=f"本轮调用预算（max_messages={self.limits.max_messages}）已用尽，以下 @ 未执行："
                + "、".join(f"@{t}" for t, _ in mentions), level="warn")
```

---

## D6 [high] 调用失败照样计入 `_run_spoke` 与 `_run_emitted`

**位置**：`engine.py:507-519`（`_speak` 捕 `LLMError` → `:522 return []`）→ `engine.py:452-455`

```python
450:    async def _speak_counted(self, agent: AgentSpec, task: str, run_id: str) -> list[str]:
451:        mentions = await self._speak(agent, task, run_id)
452:        self._run_emitted += 1
453:        self._run_spoke[agent.id] = self._run_spoke.get(agent.id, 0) + 1
454:        return mentions
```
```python
507:        except LLMError as exc:
508:            await self._set_state(agent.id, "error")
509:            await self.emit("message_end", id=message_id, content="".join(text_parts),
...
516:                error=str(exc),
517:            )
518:            await self.emit("notice", message=f"{agent.display_name} 发言失败：{exc}", level="error")
519:            return []
```

**现象**：`_speak` 把 `LLMError` 吞掉并返回 `[]`（L519/L522），**不重新抛出**；`_speak_counted` 于是在**不区分成功/失败**的情况下 `_run_emitted += 1`、`_run_spoke[agent.id] += 1`。失败时 `self.messages` 里没有该成员的记录（L509-517 的 `message_end` 不带 `message=` 字段）。

**后果（用本环境记录的真实故障算）**：`models.yaml:36-37` 的注释记录了 kimi 主路由的真实 400（`invalid temperature: only 1 is allowed for this model`）。若 kimi 因此失败、且 fallback（doubao）也缺 `ARK_API_KEY`，`llm.py:284-288` 抛 `LLMError`。此时：
1. kimi 连续失败 2 次后，`engine.py:355` 会打印 **"Kimi·论证担当 本轮发言已达上限（2 次），跳过"** —— 而 kimi 实际发言 **0 次**。这是**假事实**，且该成员的缺口从此无人补；
2. 2/10 的额度被"失败"吃掉；极端情况 10 次失败烧光整轮额度，而 `run_stats()["dispatch_speaks"]` 仍报 10，看起来像"讨论了 10 次"；
3. 运维直觉"失败就重试"在这里是反向的——重试会把该成员永久踢出本轮。

**最小修法**：拆成"调用尝试数"（参与预算）与"成功发言数"（参与成员上限）两个计数器；**保留 `_run_calls` 参与预算，从而不破坏 `engine.py:19` 声明的 `max_messages + 1` 上界与 §0 的终止性证明**。
```python
# __init__（engine.py:99-101 附近）与 _run（:284-287）: self._run_calls = 0; self._run_failed = 0

def _budget_left(self) -> bool:                       # engine.py:393
    return self._run_calls < self.limits.max_messages  # 失败也占预算 → 上界不变

async def _speak_counted(self, agent, task, run_id) -> list[str]:   # engine.py:452-455
    mentions, ok = await self._speak(agent, task, run_id)
    self._run_calls += 1
    if ok:
        self._run_spoke[agent.id] = self._run_spoke.get(agent.id, 0) + 1
    else:
        self._run_failed += 1
    return mentions

# _speak: 失败路径 return [], False（engine.py:522）
#        正常路径 return (raw_mentions if agent.can_dispatch else []), True（engine.py:559）
# run_stats()（:672-679）追加 "dispatch_calls": self._run_calls, "failed_calls": self._run_failed
```
**连带注意**：`scripts/smoke_test.py:340-360` 直接断言 `stats["dispatch_speaks"] ≤ max_messages + 1`，把语义改成"成功发言数"后该断言仍成立，但**必须同时跑一次该自检脚本**确认；另外 `engine.py:570 self._run_recorder_calls += 1` 发生在记录员调用**之前**，同一类混淆（失败的纪要也算一次调用）也存在，顺手一起改。

---

## D7 [low] `error` 状态跨轮残留

**位置**：`engine.py:313-317`
```python
315:            for agent in [*self.active_agents, self.cfg.recorder]:
316:                if self.agent_state.get(agent.id) == "thinking":
317:                    await self._set_state(agent.id, "idle")
```
对比 `engine.py:508` 失败时置 `"error"`。**现象**：一轮结束后只有 `"thinking"` 被复位，`"error"` 会一直留着，并通过 `_member_view`（engine.py:651 `"state": self.agent_state.get(agent.id, "idle")`）广播给前端成员列表——用户会看到一个"永久报错"的成员。
**最小修法**：`if self.agent_state.get(agent.id) in ("thinking", "error"):`。

---

## D8 [low] 运行级 `LLMError` 分支是死代码，且缺少聚合失败信号

**位置**：`engine.py:309-310`
```python
309:        except LLMError as exc:
310:            await self.emit("error", message=str(exc))
```
`stream_agent` 抛出的 `LLMError` 在 `_speak`（L507-519）和 `_summarize`（L610-622）里**都被吃掉**，`_dispatch_loop` 内没有其它 `LLMError` 来源 → 该分支静态可达性为 0。所有 provider 全挂时，用户只看到 N 条散落的红 notice，`run_end`（L318）里没有任何"本轮失败"的聚合事实。
**最小修法**：`finally` 里给 `run_end` 附 `failed_calls`（配合 D6 的计数器，几乎零成本），或在 `_run` 末尾发一条 `notice/warn`："本轮 N 次调用失败，结论不完整"。

---

## 复现方式（每条都能照着验）

1. **D1**：用 `scripts/smoke_test.py:72 new_engine(cfg, RunLimits())` 起 mock 引擎，把 mock 的派发 token 列表改成 `["kimi", "Kimi·论证担当", "论证"]`（或直接连调三次 `engine._speak_counted(kimi, ...)`），断言本轮 `sender=="kimi"` 的 `message_end` 条数 == `min(3, max_turns_per_run)`；当前实现会得到 3 > 2。
2. **D4a/D4b**：`extract_mentions("请联系 zhang@kimi 复核")` 应为 `[]`（当前 `["kimi"]`）；`engine.resolve_mention("i")` 应为 `None`（当前返回 kimi）；`engine.resolve_mention("组员")` 应为歧义（当前返回 kimi）。
3. **D6**：照 `scripts/smoke_test.py:277-318` 的 `selftest-dead/dead-model` 手法，把某成员指到不存在的 provider 且**不给 fallback**，跑两轮后断言 `run_stats()["dispatch_speaks"] == 实际 message_end 条数`；当前前者 2、后者 0。
4. **D5**：`RunLimits(max_depth=1)`、mock 让组员回 @ 别人，收集完整事件流，断言存在 `dispatch_skipped`（当前实现里此类事件**完全不存在**）。

---

## 已核对为正确的部分（避免误伤）

- **终止性成立**（§0 的证明），且"成员→组长"方向被 L350/L352 掐断，"组长与组员互相 @ 到天荒地老"不可能发生。
- 失败的调用仍计入预算，对**终止性**其实是加分项（`_run_emitted` 单调）；修 D6 时必须保留独立的 `_run_calls` 参与预算，否则会破坏 `engine.py:19` 声明的上界。
- `llm.py` 的降级记账在本快照**已经修好**：`stream_agent` 现在把 `degraded=degraded_attempt` 传给 `stream_chat`（`llm.py:245`），且成功路径不再重复 `_record`（与我上一次读到的旧版相比少了一次重复记账）→ 每条真实调用恰好一条 `call_log`，降级标志正确。
- `context_trimmed`（engine.py:226-236）确实做到了"丢历史必报事件"。

## 我不确定 / 未验证

- **未运行任何命令**（本任务工具集只有 read/glob/grep/web_*，且无写权限），全部结论是静态推理；我给的是**可检验的断言口径**，不是实测结果。
- 行号以我读到的快照为准，且工程成员正在并发修改（engine.py 631 → 691 行）；请按引用文本定位。
- D4d 没有现网实例（当前 models.yaml 无 tag-子串碰撞），只是潜伏缺陷；D4a 是构造输入。
- 越界线索（**只给线索不下结论**，未审 `ag2_adapter.py` 与前端其余逻辑）：`llm.py:71-83` 的 `_record` entry 没有 `agent_id`/`run_id` 字段，`app.py:146` 只暴露最后 120 条 → 事后无法把"这次花钱的调用"归因到某个成员/某一轮。

## 环境异常（原样报告，未编造）

执行本任务之前，t4 的 claim 被工具层自动审查挡下两次，原文：
```
Error: Auto review of tool "agent_teams_claim_task" failed; its body was not executed: auto-review: reviewer ended with error INVALID_REQUEST: 400: {"message":"invalid temperature: only 0.6 is allowed for this model","type":"invalid_request_error"}
```
```
Error: Auto review of tool "agent_teams_claim_task" failed; its body was not executed: auto-review: reviewer ended with error RATE_LIMIT: 429: {"message":"Your account ... request reached organization max RPM: 3, please try again after 1 seconds","type":"rate_limit_reached_error"}
```
本任务（t7）的 claim 与全部 read/grep 调用**没有报错**，报告内容全部来自真实读到的文件。