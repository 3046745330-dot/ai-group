# 技术审查：事件流与状态一致性 —— 可复现边界用例表

> 审查方式：静态通读 `group/engine.py`、`group/app.py`、`group/llm.py`、`group/config.py`、`web/app.js`、`web/index.html`、`config/models.yaml`、`scripts/ws_probe.py`（未运行服务、未改任何文件；本账号工具集仅 read/glob/grep/web，行号均取自当前磁盘版本）。
> 审查期间 `engine.py` 被工程成员并发修改过一次（新增 route_summary），已重新读取并按最新版本核对行号；`app.py` / `app.js` 与首次读取一致。
> **模型调用错误：无。** 本次未遇到 ModelNotOpen / 鉴权失败 / thinking 回传校验错误。

## 0. 复现环境（所有行共用）

- 启动离线可复现环境（零额度）：在 `ai-group/` 目录 `set SMOKE_MOCK_ONLY=1` 后执行 `.venv\Scripts\python.exe -m uvicorn group.app:app --port 8801`（`group/app.py:30-33` 调 `force_mock`，全部成员走 mock，`config/models.yaml:18-129` 的四个活动角色 captain/kimi/demo-bot/doubao + recorder 全部落到 provider=`mock`）。
- 浏览器开 `http://127.0.0.1:8801`；事件帧用 DevTools → Network → WS → Messages 观察；`scripts/ws_probe.py:43-54` 演示了从 run_start 收齐到 run_end 的脚本化抓帧方式。
- mock 流式每 **6 个字符**切一片：`group/llm.py:329`（`_mock_stream` 调 `_chunk(text, 6)`）+ `group/llm.py:431-432`，片间隔 4–12ms，一轮（组长+2 组员+组长收敛+记录员）约 2–5 秒，足够插入刷新/并发操作。
- mock 组长每轮固定 @ 名单前两位非组长成员（`llm.py:355-380,383-394`），即 `@kimi`、`@demo-bot`。

## 1. 事件协议全量映射（生产者 → 消费者）

| kind | 生产者（文件:行号） | web/app.js 消费者 | 结论 |
|---|---|---|---|
| run_start | engine.py:290 | app.js:244-249 | ✅ 对应 |
| message（仅用户消息走此 kind） | engine.py:302（292-302 构造 user_msg） | app.js:222-224 → addMessage 118-145 | ✅ 对应 |
| room_state | engine.py:148-154（调用点 303、319） | app.js:215-218 | ✅ |
| agent_status | engine.py:156-158 | app.js:219-221 | ✅ |
| message_start | engine.py:471-481、573-582 | app.js:225-227 | ✅ |
| message_delta | engine.py:506-508、617（汇总 609-617） | app.js:228-230 → appendDelta 169-177 | ✅ |
| message_end | engine.py:509-520（失败）、550-553（成功）、613-620、647 | app.js:231-233 → endMessage 191-207 | ✅ |
| notice | engine.py:248-255、343-348、354-359、495-497、518-521、525-535、598、621-629 | app.js:238-240 | ✅ |
| error | engine.py:272、309-312、326-327 | app.js:241-243 | ✅ |
| run_end | engine.py:317-318 | app.js:250-255 | ✅ |
| route_changed（仅降级时产出） | engine.py:496-505、599-614 | app.js:234-237 | ✅ 健康配置下收不到属正常 |
| **context_trimmed** | engine.py:225-236（`_build_messages`） | **无**（switch 210-265 无 case） | ❌ 产出了前端不处理（R2） |
| **route_summary** | engine.py:307（每轮必发）、442-450；快照字段 engine.py:696 | **无**（无 case；applySnapshot 267-290 也不读 snapshot.route_summary） | ❌ 产出了前端不处理（R1） |
| hello | app.py:228-242（连接首帧）、261-266（snapshot action，前端从不发该 action） | app.js:212-214 | ✅（261-266 为死分支） |
| **pong** | app.py:259-260 | **无**；且 app.js 全文不发 `{action:"ping"}`（无心跳定时器） | ❌ 双向都不通（R3） |
| cleared | app.py:208-209、272-273 | app.js:256-261 | ✅ |

**“前端处理了但可能永远收不到”的 kind：未发现**（除 pong 是反向死信）。另有一个协议不对称：agent 消息从不走 `message`，只走 start/delta/end 三件套；`message` 仅用户消息使用——消费者两侧都覆盖了，但协议文档/注释未说明，checker 不要按“每条消息必有 message 事件”断言。

## 2. 边界用例表

| # | 假设 | 文件:行号 | 触发步骤（可照做） | 预期错误现象 | 严重度 |
|---|---|---|---|---|---|
| R1 | 每轮结束后服务端广播的 **route_summary（同源/降级汇总，即“多厂商结论是否可信”的唯一结构化告警）前端完全不可见** | 产出：engine.py:307、442-450；快照：engine.py:696；消费缺失：app.js:210-265（default 262-263 静默丢弃）、applySnapshot app.js:267-290 不读 route_summary | ① mock-only 启动后页面发任意问题；② DevTools WS 帧里可看到 `{"kind":"route_summary",...,"shared_providers":{"mock":N},"level":"warn", message 含“同一个 provider（厂商/通道）承担”}`（mock 下 N≥2 必为 warn，engine.py:417-429）；③ 在页面 DOM 中查找“同源”二字 | 帧里明确有 warn 级同源告警，界面无任何横幅/角标/样式变化；刷新后的 hello 快照同样带 route_summary 仍不渲染。**真实密钥环境下当前 captain/kimi/recorder 主路由均无 key、全部降级到豆包时（models.yaml:15-17,25-27,47-49,121-123），“同厂自辩”告警同样不可见——系统外观完全健康而结论不可信**；mock 记录员回复是固定模板（llm.py:407-415），不会转述该信息，故事件是该告警的唯一载体 | 高 |
| R2 | context_trimmed（喂给模型的上下文被裁掉 N 条）事件前端不处理，且裁汰事实不进消息快照，刷新后无痕 | engine.py:225-236（emit）；app.js:210-265 无 case；消息体 Message 无裁剪字段 engine.py:39-57 | ① mock-only 启动，先 reset；② 连续发约 10–12 个不同问题（每轮约 5 条 × 每条 200–300 字，见 llm.py:417-428，累计 transcript 超过预算 `max(400, 9000-fixed)`，预算逻辑 engine.py:223-224 + 170-204）；③ WS 帧中出现 `kind:"context_trimmed", dropped:>0`（后端 scripts/smoke_test.py:248-250 正是据此断言）；④ 在 `.stream` 容器内查“上下文超限” | 后端确实在丢历史发言（模型看到的记录已残缺），WS 帧证据确凿，但页面不插入任何 notice、成员气泡无标记；F5 重连后快照连“曾裁剪”的痕迹都没有。用户/观察者会以为每个模型都看到了完整讨论 | 中 |
| R3 | pong 无消费者 + 前端不发 ping，连接无应用层心跳，半开连接无法被及时发现 | app.py:259-260（pong）；app.js:293-313（onclose 306-311 仅靠 TCP 关闭事件，无 setInterval ping；全文无 `action:"ping"`） | ① 页面连上后用防火墙/NAT 静默丢包（不拔线）掐断服务端到浏览器的链路；② 不等 TCP keepalive（Windows 默认约数小时级），直接在输入框打字点发送 | ws-dot 长时间保持绿色“已连接”（onclose 不触发、无心跳超时）；send() 的 readyState 检查（app.js:323-326）在浏览器缓冲未耗尽前仍为 OPEN，消息被静默吞掉，只有“断开，Ns 后重连”在 TCP 最终超时后才出现 | 低 |
| R4 | 页面在某成员**流式输出途中刷新**，该条发言在新页面里永久丢失（不是半截气泡），直到再次手动刷新 | 快照只含已完成消息：engine.py:552 先 append、553 才 emit message_end，snapshot 读 engine.py:697；新连接只收 hello 快照 app.py:228-242（connect 在 227 已入 clients 组，之后的广播帧才会收到）；迟到帧被前端按 id 丢弃：app.js:170-171（delta 无 node 直接 return）、app.js:192-193（message_end 无 node 直接 return） | ① mock-only 起服务，开标签 A；② 发“群里都有谁？请分工后给出结论”，在组长气泡出现流式光标（`.bubble.streaming`，app.js:160）瞬间按 F5；③ 等页面收到 run_end（标签变“空闲”，app.js:250-255）；④ 对照：另一个一直没刷新的标签 B 有完整组长发言；`GET /api/snapshot`（app.py:89-102）的 messages 里也有该 id；⑤ 在标签 A 再按一次 F5 | 标签 A 在 run_end 后仍缺这一整条成员气泡（用户消息在快照里所以不缺），成员状态芯片却显示“已发言”（snapshot.members 带 done，engine.py:552→158→snapshot 693），状态与气泡自相矛盾；第二次 F5 后气泡出现，证明消息没丢在服务端、纯粹是“刷新时刻在途 → 后续 end 帧因 id 无 node 被丢弃” | 高 |
| R5 | 短暂断线自动重连（不整页刷新）：**不会重复追加**，但离线期内的 notice/error/在途气泡全部丢失；服务端 `Hub.history` 从不参与重连 | 重连只发 hello+全量快照：app.py:228-242；applySnapshot 用快照整体替换：app.js:280-285（先 `innerHTML=''` 再按 id 重建）；history 只写不发：app.py:43-44、56-58（无任何读取/下发处）；重连定时器 app.js:306-311 | ① 发一轮讨论，运行中途用 DevTools→Network→Offline 切 3 秒再切回（触发 onclose→setTimeout 重连）；② 离线期间让另一标签触发一条会广播 error/notice 的事件（如另一标签在运行中发消息触发 engine.py:271-273）；③ 重连后对比两标签 | 已完成消息：两标签一致、无重复气泡（快照按消息 id 全量替换，非增量追加）；离线期内的 notice/error/context_trimmed/route_summary 在重连标签永久缺失（notice 从不进快照，app.js:111-116 只活在 DOM）；断线那一刻正在流的气泡按 R4 同样消失。即“重放历史还是重复追加”的答案是：**既不重放也不重复，而是用全量快照覆盖，非消息类事件静默丢失** | 中 |
| R6 | 断线期间另一客户端 reset，重连后本标签保留陈旧消息，且后续新消息追加在陈旧列表上 | reset 清服务端：app.py:268-273（WS）/202-210（REST）；applySnapshot 仅在 messages 非空时才清空 DOM：app.js:280 `if (snapshot.messages && snapshot.messages.length)` | ① 标签 A 先聊几轮；② A 切 Offline；③ 标签 B 点“清空聊天记录”（服务端 messages/history 清空并广播 cleared，A 收不到）；④ A 切回 Online 自动重连，收到 messages=[] 的 hello；⑤ 在 A 直接发一条新消息 | A 重连后旧气泡原样留在 DOM（空快照跳过清空分支，app.js:280-285），新问题的用户气泡和成员气泡直接追加在陈旧记录之后；B 标签只有新一轮，两标签历史永久分叉，直到 A 再刷新 | 低 |
| R7 | Hub.history 是写了从不读的死缓存，注释“供刷新后的页面补历史”与实现不符；将来若朴素接成“重放 history”，必然产生重复气泡与半截气泡 | app.py:43（注释）、54-58（每事件含每个 delta 都追加、截断 400）；全文件无 history 下发；addMessage 无 id 去重 app.js:118-145；start/delta/end 也无幂等保护 app.js:147-177 | ① grep 全仓 `history`：只有 app.py:43/56/57/208/272 五处，无一处 send；② 推一轮长讨论后查 `Hub.history`：400 条上限里绝大多数是 message_delta（一条发言约 40–60 个 delta，llm.py:329 每 6 字一片），run_start/message 等关键帧早被挤出；③ 思维验证：若在 connect（app.py:227）后把 history 逐帧补发，再接 hello 快照 | 现状：history 纯内存垃圾（reset 才清，app.py:208/272），刷新实际靠 hello 的 ENGINE 全量消息快照（无游标、无版本号、无去重）；一旦按字段名误用为重放源：(a) 快照 addMessage 与 delta 拼接出的同一 id 气泡重复（addMessage 不查重，app.js:142 直接 set 覆盖 map 但 DOM 已 append 两次）；(b) 400 条截断只剩 delta 帧、缺 start 帧时 appendDelta 全部 no-op（app.js:170-171），缺 end 帧时气泡永远挂 streaming 样式 | 低 |
| R8 | 任务假设“@成员 跨两个 delta 分片会被切断高亮”——**该永久缺陷不成立**；真实可复现缺陷是：正则对“尚未收完的前缀/非成员 @”立即且永久高亮，前端不做名单校验，与后端 resolve_mention 语义不一致 | 缓存位置 app.js:174（先拼 `bubble.dataset.raw`）、整串重渲 app.js:175、end 用完整 content 兜底 app.js:197-198；正则 app.js:40 `/@([A-Za-z0-9_\-.·\u4e00-\u9fff\u30fb\u2022]+)/g`（要求 @ 后≥1 字符）；后端解析会拒绝未知 token：engine.py:341-348 + 116-140；mock 6 字切片 llm.py:329、431-432 | 确定性注入法（页面控制台直接喂事件，不依赖网络）：<br>`handleEvent({kind:'message_start',id:'t1',role:'member',sender_name:'X'})`<br>`handleEvent({kind:'message_delta',id:'t1',delta:'请 @'})` → 查 `.mention`：0 个（@ 后 0 字符不匹配）<br>`handleEvent({kind:'message_delta',id:'t1',delta:'kimi 补充'})` → 出现 `<span class="mention">@kimi</span>`<br>`handleEvent({kind:'message_end',id:'t1',content:'请 @kimi 补充',message:{id:'t1',content:'请 @kimi 补充',route:'mock/mock-fast'}})` → DOM 与上一帧一致，**未切断**<br>反例第二组：`message_start id:t2` → delta `'联系 @'` → delta `'nobody 吧'`：`@nobody` 被永久包上 mention 高亮，而 nobody 不在成员名单（后端 extract_mentions 抽到 nobody、resolve_mention 返回 None，engine.py:343-348 只发“不存在的成员”notice，从不纠正前端颜色） | ① 跨分片本身：因正则作用于累积整串而非单片（app.js:173 注释自称的目的成立），最终帧高亮完整，checker 可断言 t1 最终恰好 1 个 `.mention` 且 textContent 含 `@kimi`；② 真实缺陷：流式过程中每来一片就对“半截 token”跑一次贪婪匹配——`@` 后一旦出现任意名单内字符集的片段就上色（如 `@ki` 瞬间变色），且对邮箱 `a@b.com`、非成员 `@nobody` 等同款误高亮并保持到最终帧（前端只做语法匹配、不拿 state.members 校验）；自然复现：mock 每 6 字切片，WS 帧里任一 @ 提及（llm.py:426-427 拼装）都跨分片，逐帧截图可见高亮前缀逐帧增长 | 低 |
| R9 | 两个标签同时发消息：第二个被服务端拒绝，但**用户无法从界面确认是自己的消息被拒**；被拒文本不留任何痕迹 | 拒绝点 engine.py:271-273（`_lock.locked()` → 全局 emit error，无消息 id/无原文回显）；广播给所有人 app.py:54-67；发送按钮在运行中可点（仅按输入框非空禁用）app.js:316-318；send() 先清空输入框 app.js:328，且无乐观气泡（用户气泡只靠服务端 message 事件 app.js:222-224 → engine.py:302） | ① 标签 A、B 同时打开；② A 发 Q1（mock 一轮 2–5 秒）；③ 运行中在 B 输入 Q2 回车（按钮未禁用，可直接点）；④ run_end 后在 B 看 DOM、并 `GET /api/snapshot` 查 Q2 | B 的输入框被清空、自己的用户气泡从未出现（engine 在加锁前就 return，Q2 未进 ENGINE.messages）；A、B 两标签同时出现无归属的红色 notice“⚠ 上一轮讨论还没结束，请稍候再发。”（engine.py:272 → app.js:241-242）——B 无法把这条全局通知和自己刚发的 Q2 对应，A 则会误以为是自己出了错；snapshot 中无 Q2 证实被吞 | 中 |
| R10 | REST 与 WS 两条入口的并发守卫不一致；且存在确定性窗口：服务端已广播 run_end、前端已显示“空闲”，但引擎锁尚未释放，此刻发送会拿到 `ok:true` 随后只收到一条拒绝广播 | REST 先查 `_running` 直接 409：app.py:191-193；WS action 完全不查 `_running`：app.py:250-258，只靠 engine.py:271 的锁检查；状态顺序：`_running=False` engine.py:314 → await run_end 318 → await room_state 319 → 退出 `async with` 才放锁（engine.py:275-280 的 finally 在锁内执行）；前端收到 run_end 立即置空闲 app.js:250-255 | ① 写一个 WS 脚本：连上后发一条 chat，**一收到 `kind:"run_end"` 帧立刻再发第二条 chat**（run_end 在 318 行发出，锁要等 319 行 room_state 发完才释放，窗口内必中）；② 对照 REST：运行中 `curl -X POST /api/chat` 一律 409（app.py:192-193） | WS 客户端收到的是：第二条消息无任何 ACK（app.py:250-258 不回帧），紧接着一条全局 error notice；若第二条走 REST 恰好在该窗口，则 HTTP 响应是 200 `{"ok":true,...}`（app.py:197-198，因 `_running` 已 False）但后台任务在 engine.py:271-273 被拒——**调用方拿到成功响应，消息实际没被处理**，只能从 WS 旁路 notice 推断。WS 路径永不返回 409，两种入口语义不一致 | 中 |
| R11 | Hub.broadcast 在一把锁内串行 await 所有客户端，单一慢/不读消息的客户端会造成全队头阻塞，连带把引擎本身卡住（emit 在引擎锁内 await sink） | app.py:54-67（55 取 Hub.lock，63 逐个 `await ws.send_text`，异常只在发送立即失败时才摘死连）；引擎 emit 链：engine.py:145-146 → sink=HUB.broadcast（app.py:71），且均在 engine `_lock`（engine.py:275）内调用 | ① 起一个只读 hello 后再不 recv 的 WS 客户端：`python -c "import asyncio;from websockets.asyncio.client import connect;asyncio.run((lambda: __import__('asyncio').get_event_loop())())" ` 或保存脚本 `async with connect('ws://127.0.0.1:8801/ws/group') as ws: await asyncio.sleep(300)`（不读任何帧）；② 另一浏览器标签正常连续 POST 15–20 轮 /api/chat（每轮帧载荷约 3–6KB）；③ 观察标签实时上屏 | 该“不读”客户端的 TCP 接收窗口在累计约 64KB（内核默认发送缓冲）后关闭，服务端在 app.py:63 阻塞且仍持有 Hub.lock：后续所有广播对所有标签排队，正常标签的逐字上屏整体冻结、run_end 也发不出去（引擎持锁卡在 emit 上，新请求在 engine.py:271 排队/拒绝）；要等到操作系统 TCP 超时（分钟级）send 抛异常才在 app.py:64-67 摘除。快客户端被慢客户端连坐 | 中 |
| R12 | 两个标签同时点“清空”，或 REST reset 与 WS reset 并发：服务端无“已清空”幂等状态，广播两次 cleared，界面叠两条清空提示 | WS reset 分支 app.py:268-273（仅查 `_running`，清完必广播）；REST app.py:202-210 同样无条件广播 209；前端收到 cleared 只 append notice 不去重 app.js:256-261 | ① A、B 两标签同时点“清空聊天记录”（index.html:29，app.js:443-450）；或一边点按钮一边 `curl -X POST /api/reset` | 两个标签都出现两条“聊天记录已清空”notice；消息本身因 `messages.clear()` 幂等不会损坏，仅提示重复、且无操作归属 | 低 |

## 3. 补充观察（非四个重点的直接结论，但 checker 可能踩到）

1. **快照消息无上限**：hello 每次发送 ENGINE.messages 全量（app.py:234-235 → engine.py:689-698，只有 reset 才清，engine.py:205-207），而 Hub.history 有 400 上限。长会话不 reset 持续累积后，每次重连首帧无界膨胀（ws_probe.py:88 把 max_size 放到 4MB 即为旁证），可能触发 WS 帧上限。
2. **error/notice 不进任何持久态**：R1 的降级“同厂”告警、R2 的裁剪、R9/R10 的拒绝，刷新后全部无痕（只有消息气泡上的 `warning` 随 message dict 持久化，engine.py:50、545-550，app.js:136-140/201 能恢复降级角标）。
3. **前端 @ 正则与后端字符集等价**：app.js:40 与 config.py:261 的字符类覆盖一致（均含 `·・•` 与中日韩区段），故 R8 不存在“前端切得出、后端抽不到”的分片问题；真正不一致在“是否校验名单成员”（前端纯语法高亮，后端 resolve_mention 宽松匹配 engine.py:116-140）与尾随标点处理（后端 config.py:269 会 rstrip 标点/间隔号，前端不处理，如 `@kimi·` 前端把间隔号高亮进 chip，后端解析时剥掉）。

## 4. 结论

- 事件协议存在 **3 个“产出无消费”事件**，其中 route_summary（R1，高）恰好承载“降级导致同厂自辩”这一核心可信度告警，context_trimmed（R2，中）掩盖模型上下文被裁剪的事实；无“消费了但永远收不到”的死 case。
- 断线恢复的实际语义是“hello 全量快照覆盖、不做 history 重放”：**不会重复追加**，代价是在途消息（R4，高）、离线期 notice（R5）静默丢失；Hub.history 是名不副实的死缓存（R7）。
- @跨分片高亮的“切断”假设被现有 dataset.raw 累积重渲机制证伪；实际缺陷是非成员/半截 @ 的误高亮（R8，低）。
- 多标签并发时被拒消息无 ACK、无气泡、无归属全局告警（R9，中），REST 还存在 run_end 后锁未释放窗口内的 200-ok-但拒绝（R10，中）；Hub 单锁串行发送有队头阻塞（R11，中）。

（报告完。本账号无写文件工具，全文在此，请队长落盘至 ai-group/docs/tech-review-event-stream.md。）