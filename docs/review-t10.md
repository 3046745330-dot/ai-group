# t10 复审报告（round 2）：ai-group 是否满足契约与工程可靠性要求

- 复审人：checker（独立于实现者）
- 被复审任务：t9（repair-round-2，来源于 t3 的两条 findings）
- **裁决：pass**
- 复审修订（复审前后哈希一致，未改动任何实现文件）：

| 文件 | SHA256 前 16 位 |
|---|---|
| group/engine.py | F4069B9B8AAF3A96（22:42:54） |
| group/app.py | 1CFB78D250CCE209（22:43:02） |
| group/llm.py | A4B8A224B1BEBEA4 |
| group/config.py | 53F3E55B76A52533 |
| group/ag2_adapter.py | B109CFEE6465F0F9 |
| config/providers.yaml | ECDFA993CA3F4881 |
| config/models.yaml | 1BFEB365FFF96911 |
| README.md | 5B736B181E051D87（22:44:18） |
| web/app.js·index.html·style.css | 759A1838A36918D2 / 9F7F9173FA5FCC74 / F039C520244BFBA5 |
| config/roles/*.md | D9D273053750D264 / D7ABDED3C87F5B9 / 9E4F04AD476BA8D1 / D4CC783360F12CDE / 9564079CFC9D150F |

## 1. 上一轮两条 findings 的独立复验（不采信实现者自述）

### F1（原 medium）上下文窗口优先级 —— 已修复

- 代码：`engine.py:251` `rest = earlier + [item for item in head_pool[1:]]`，
  `:262` `reversed(rest)`；注释明确写出「本轮消息必须排在更早轮次之后，反转后才会本轮优先」。
- 独立探针（`scripts/review_probe.py`，我自己构造的判别性用例，budget=300、
  1 条 RUN-A 旧消息 + 本轮 user + 本轮 member）：
  ```
  [PASS] README §5『窗口优先用本轮消息，有富余才带回更早轮次』
    —— budget=300：本轮成员发言保留、更早轮次发言被丢弃（dropped=1）
  ```
- 反向用例（旧实现会在这一步 FAIL）已在同一探针里覆盖；实现者另在自检里加了
  `[PASS] 上下文预算紧张时本轮消息优先于更早轮次`（smoke 第 65 项之一）。

### F2（原 low）README §7 `/api/snapshot` 字段 —— 已修复

- 代码：`app.py:89-112` 现在返回 `group / members / limits / run_stats / route_summary / messages / running`。
- 独立实测（mock-only uvicorn 8805）：
  ```
  [PASS] README §7 声称 /api/snapshot 含 run_stats 与 route_summary
    —— 实际 /api/snapshot 顶层键=['group','limits','members','messages','route_summary','run_stats','running']
  ```
- 文档：`README.md:200` 已同步为「顶层键与 WS hello 的 snapshot 一致，页面刷新后额度/同源/降级信息不会丢」。

## 2. t1 八条验收标准逐条核对（当前修订）

| # | 标准 | 判定 | 证据 |
|---|---|---|---|
| 1 | `smoke_test.py --mock-only` 退出码 0、逐项 PASS（配置装载/角色提示词/@抽取/@宽松匹配/事件序列/≥2 角色/记录员汇总） | passed | **EXIT=0，`结果：全部通过（65 项检查）`**；`docs/rereview-smoke-mock-only.txt`（含角色提示词独立、宽松匹配 @Kimi·论证担当→kimi、事件序列 6 类、captain+kimi+demo-bot+recorder、记录员 190 字纪要） |
| 2 | 起服务后 `/api/health` 200 含 providers+members；`/` 群聊 HTML；WS 首帧 hello 带 members | passed | `http_probe --port 8805 --expect-mock` 3/3（health 200/42ms，5 provider 各带 ok/detail，members 6 个含 enabled 位；`/` 200/2146 字节）；`ws_probe` 首帧 `kind=hello`，members=[captain,kimi,demo-bot,doubao,recorder] |
| 3 | POST /api/chat → WS 可见 captain → 组员 → 记录员；无 @ 时不多唤醒组员 | passed | `ws_probe`：413 事件、0.05s，senders=captain,kimi,demo-bot,captain,kimi,demo-bot,captain,recorder；`quiet` 时 senders=['captain'] |
| 4 | 额度护栏有代码约束；不存在的 @目标 只 notice 不报错 | passed | `ws_probe`：逐成员 3≤4/2≤2/2≤2、派发链 7≤10、recorder 1；smoke 覆盖 max_messages/max_depth/max_context_chars/max_model_calls_per_run；未知 @ → notice、errors=0 |
| 5 | 降级路径可观测（绝不静默换模型） | passed | smoke 两组降级用例均 PASS：warn `notice` + `route_changed`（requested/actual）+ 消息自带 warning/route；`llm.py:255-339` 仅在首增量前降级 |
| 6 | 密钥零落盘 | passed | smoke「密钥零落盘」扫 36 文件 0 命中；`review_probe` 10/10 中含零密钥正则扫描；`/api/diagnostics` 无 `sk-` 前缀 |
| 7 | AG2 适配层可用（未装时明确降级、不影响主流程） | passed | smoke：`describe()` 返回 kernel/fallback + `pip install ag2` 说明；`build_llm_config` 对 5 个启用成员全部可编译；`build_ag2_group` 未装时抛带说明的 RuntimeError（不抛未处理异常） |
| 8 | README 与 `config/roles/*.md` 与实际实现一致 | **passed** | `review_probe` 10/10；上一轮 F1/F2 均已消除；§4 角色表 ↔ `models.yaml`、§3 凭据表 ↔ `providers.yaml`、§1 `run.ps1` 规则、§8 `smoke_test`/`integration_check` 说明均逐条对得上 |

## 3. 工程质量专项（当前修订，均带行号）

| 审查点 | 结论 | 证据 |
|---|---|---|
| 静默换模型 | 无。降级必发 `notice(warn)` + `route_changed` + message `warning/route`；首增量之后中断不降级、报「不完整回复」 | `engine.py:656-671`、`llm.py:308-332` |
| 硬编码密钥 | 无。全部经 `config.resolve_credential`（env → DSH refs） | `config.py:66-78`、`llm.py:98-114`；两次独立扫描 0 命中 |
| 烧穿额度的循环 | 无。派发循环受 `max_depth` + `max_messages`(尝试数) + 成员 `max_turns_per_run` 约束，记录员一轮 ≤1；`openai` 客户端 `max_retries=0`；temperature 自愈最多额外 1 次并缓存 | `engine.py:419-503,511,621`、`llm.py:126-133,189-213`；smoke「尝试 2 + 纪要 1 ≤ 3」 |
| 异常路径崩溃 | 无。`TimeoutError`→`LLMError`（`llm.py:193-198,236-240`）；`engine._run` 捕获 `LLMError`/`Exception` 并发 `run_end{aborted}`（`engine.py:380-399`）；WS 处理函数整体 try/except + `finally` 断开（`app.py:284-288`）；`Hub.broadcast` 逐客户端 try/except（`app.py:64`） |
| 全厂商不可用 | 有可判定信号：顶层 `error` + `run_end{aborted:true}`，本轮零产出 | `engine.py:374-385`；smoke F5 两项 PASS |
| 失败成员是否冒充参与 | 否。失败留一条带 `error` 的 Message，`route_summary.failed` 单列，纪要任务显式写缺口 | `engine.py:523-535,544-545,559-570,760-778`；`review_probe` PASS |
| @触发与额度护栏是否代码强制 | 是。解析（最小长度/歧义拒绝）、队列按身份去重、逐成员计数、单轮上界全在内核 | `engine.py:125-187,404-510`；`review_probe` @ 解析 6 用例全对、邮箱不被抽取 |

## 4. 残余观察（不构成 findings，不影响 pass）

- `models.yaml:12-15` 声明标签全局唯一，但 `check_invariants`（`config.py:318-341`）只校验 fallback 异厂；
  标签唯一性靠注释约定 + 运行期 `engine.py:145-152` 判歧义兜底。当前配置无重复标签，无功能影响。
- README §7 的 WS 事件清单未列出对 `{action:"ping"}` 的 `pong` 应答（内部探活事件），属表述完备性，非功能缺失。
- 环境事实（沿用 t2/t3）：`deepseek` 无有效 key，真实模式下 captain/recorder 每轮必然降级到豆包
  （requested=deepseek/…、route=doubao/…、degraded=true），README §2 已如实写明；账本
  `~/.dsh/.dshw-usage.json` 不覆盖本应用直连厂商，真实路由以 `/api/diagnostics` 与事件流为准。

## 5. 复现命令

```powershell
cd ai-group
$py = ".\.venv\Scripts\python.exe"
& $py scripts\smoke_test.py --mock-only                     # EXIT=0, 65/65
$env:SMOKE_MOCK_ONLY="1"
Start-Job { Set-Location $using:PWD; & $using:py -m uvicorn group.app:app --port 8805 }
& $py scripts\http_probe.py --port 8805 --expect-mock       # 3/3
& $py scripts\ws_probe.py --port 8805                       # 9/9
& $py scripts\review_probe.py --port 8805                   # 10/10
```
