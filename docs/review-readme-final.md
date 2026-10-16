# 独立复核：README.md 与当前实现/配置逐条一致性（t11）

> 由 kimi（llm-group 群员）独立审查，队长落盘（该成员 toolPolicy=llm_research，无写权限）。
> 队长补记：README.md 实测 sha256 = 53F431355370615B35D5D991BC6C9FF4C36F01859B62F19D1B50DDCD925F68DD（与 kimi 引用的团队记录值 53F431355370615B **一致**），
> 字节数 17782、行数 282、mtime 2026-10-12 23:03:10。
> 队长补记：kimi 无法执行命令，故本报告所有行号为「静态读取快照」，其 sha256 为引用值；以上复算消除了这一项不确定性。

---

# 独立复核：README.md 与当前实现/配置逐条一致性

> 交付物正文（**我无写权限，未落盘**；请队长把本正文写入 `ai-group/docs/review-readme-final.md`）
> 审查者：kimi（task t11 / attempt 269419ba-567e-48a0-8702-12e4ac185852）
> 方式：**只读**（read / glob / grep），未执行任何命令、未修改任何文件；本成员 toolPolicy=llm_research（无 exec、无写权限）。
> 目标文件：磁盘上当前的 `ai-group/README.md`（**282 行**）。我读到的版本包含 t5 在 23:03:10 新增的 `/api/snapshot` 行及其括号「顶层键与 WS `hello` 的 snapshot 一致」（README.md:200）与「本轮真实运行…563 秒 / 2315 事件 / 8 条发言」（README.md:243），即 t10 冻结版（5B736B18）之后的修订版。
> **sha256 说明（能力限制，非文档问题）**：验收要求给出 README 的 sha256，但我没有执行命令的工具，**无法独立计算**。团队记录值（t5 工单、t10 冻结表）为 `53F431355370615B`；该值我**只能引用、不能背书**。若队长/checker 复算得同一值，即与本报告目标字节一致；若不同，说明文件在我读取后又被改动，本 verdict 需重绑（内容锚点见文末「版本锚定」）。

## 结论

**verdict = pass**：README 的 8 项待核声明**逐条与当前实现/配置一致**（每条给 file:line 证据，见下表）。
另有 **3 处非契约级观察**（1 处措辞精度 + 2 处清单完整性，均含最小修法，见「残余观察」），**不涉及任何能力声明**，不影响 pass。
**范围声明（按要求明确区分）**：本次复查**只针对文档 README.md**。六个实现文件（engine.py F4069B9B8AAF3A96 / app.py 1CFB78D250CCE209 / llm.py A4B8A224B1BEBEA4 / config.py 53F3E55B76A52533 / providers.yaml ECDFA993CA3F4881 / models.yaml 1BFEB365FFF96911）自 t10 复审以来哈希未变，故 **t10 的 verdict=pass 仍然覆盖当前实现代码**；本次「文档已复查、代码未重新执行」**不等于**「代码未验证」。我全程未修改 group/、web/、config/ 下任何文件。

## 逐条核对（8/8 passed）

> **历史记录说明**：本文是某一时点的评审快照，记录的是**当时**的状态（当时解释器复用相邻项目的 venv）。
> 后续已修复为仓库自足（新增 `requirements.txt`，脚本不再引用外部路径），当前状态以 README.md 与
> `docs/evidence/STATUS.md` 为准。此文件保留原文不改，以维持评审记录的真实性。

**① 解释器规则 — passed**
README.md:31-53 声明：不创建 `ai-group/.venv`、复用 `.\.venv\Scripts\python.exe`、run.ps1 四条选择规则、找不到就 exit 1、DSH 自带 python 不会被选中。
证据：`scripts/run.ps1:13-17`（param：`-Python`/`-Port`/`-MockOnly`）、`:24-27`（候选顺序 = 显式指定 → 本项目 .venv → campus-market venv，**候选里没有 DSH python**）、`:30-35`（逐个探测 `import fastapi, uvicorn, openai, yaml`，缺依赖则跳过）、`:38-45`（找不到 → 红字报错 + `exit 1` + 打印建 venv 命令）、`:71`（`& $exe -m uvicorn group.app:app --host 127.0.0.1 --port $Port`）。
`ai-group/.venv` 不存在：glob `ai-group/{.venv,docs,scripts}/*` 无任何 `.venv` 命中（与该声明一致）。
README.md:52 的 pip 列表与 run.ps1:43 的提示一致。README.md:37 的「fastapi 0.115.6 / openai 3.29.0」是运行期版本事实，**我无法复测**（见盲点）。

**② 凭据来源与「本机实测」表 — passed（机制一致；「可用/不可用」的运行期事实无法只读复测）**
证据：`config/providers.yaml:23-28`（deepseek：**仅** `env: DEEPSEEK_API_KEY`；`:17-21` 注释明确说明 DSH 的 `deepseek-account-platform/default` token 实测 401、**故意不列为凭据来源** → README.md:77-79「已把它删掉」成立）；`:54-61`（moonshotai-cn：`MOONSHOTAI_CN_API_KEY` → `MOONSHOT_API_KEY` → `dsh_credential: MOONSHOTAI_CN_API_KEY`）；`:78-84`（doubao：`env: ARK_API_KEY` → `dsh_credential: ARK_API_KEY`）；`:96-101`（openai：`OPENAI_API_KEY`）；`:115-118`（mock：`credentials: []`，无密钥）。
解析实现：`config.py:26-63`（只读 `~/.dsh/.credentials.yaml` 的 refs/records，按完整 ref 名或**末段名**匹配）+ `:66-78`（按声明顺序命中即用，返回值只报「有没有」，绝不打印密钥）。
与 README.md:85-102 的表格逐格一致；README.md:101「键名就是表里的名字」与 refs 匹配逻辑一致。

**③ 角色表与 fallback 列 — passed（且 fallback 全部异厂）**
证据（README.md:106-113 ↔ `config/models.yaml`）：captain `deepseek/deepseek-v4-flash` → fallback `doubao/doubao-seed-2-1-pro-260915`（models.yaml:23-38）；kimi `moonshotai-cn/kimi-k2.6` → fallback doubao（:48-63）；doubao `doubao/…` → fallback `moonshotai-cn/kimi-k2.6`（:84-99）；demo-bot `mock/mock-fast`、**无 fallback**（:67-79，与 README.md:111 一致）；gpt `openai/gpt-5.1-mini` → fallback moonshotai-cn、`enabled: false`（:102-117）；recorder `deepseek/deepseek-v4-flash` → fallback doubao（:122-137）。
`role_file` 列与 `config/roles/*.md` 实际文件一一对应（captain.md / member_analyst.md / member_critic.md / member_mock.md / recorder.md，glob 已确认 5 个文件）。
不变量：`config.check_invariants` 在 `/api/health`（`group/app.py:125`）与自检（`scripts/smoke_test.py:125-132`）里被执行；README.md:117-119「fallback 不得同厂」对当前 5 个有 fallback 的成员全部成立（deepseek→doubao、moonshotai-cn→doubao、doubao→moonshotai-cn、openai→moonshotai-cn、deepseek→doubao）。
README.md:124-129 的 `temperature: 1.0` / `max_tokens: 3000` 与 models.yaml:58-59 一致；「只重试一次并缓存」与 `llm.py:53-54`（`_omit_temperature` 缓存）+ `:205-211`（`attempt == 0` 且错误含 `invalid temperature` 才去掉该参数重试一次）一致。

**④ 额度语义 — passed**
README.md:158-163 的四行逐条对得上：
- `RunLimits` 默认值 `engine.py:72-74` = `max_messages: 10` / `max_depth: 2` / `max_context_chars: 9000`；
- README.md:160「max_messages = 单轮派发链**尝试次数**上限」→ `engine.py:502-504`（`return self._run_calls < self.limits.max_messages`），`_run_calls` 在 `_speak_counted`（engine.py:612 附近）无论成败 +1；
- 「记录员纪要不占该配额，但一轮最多调用一次，总数上界 = max_messages + 1」→ `_summarize` 由 `_run` 只调用一次、单独计入 `_run_recorder_calls`（engine.py:739-758 段），`limits_view` 直接给出 `"max_model_calls_per_run": self.limits.max_messages + 1`（engine.py:863-867）；
- README.md:161「max_turns_per_run = 单个成员一轮最多**成功发言**几次」→ `engine.py:436-441` 用 `_run_spoke`（成功计数）判定，失败路径不增长 `_run_spoke`；
- README.md:162/163 的 max_depth / max_context_chars 语义与 `engine.py:419`（while 条件）与 `:288-293`（system+user 总预算）一致。

**⑤ `/api/snapshot` 顶层键含 `route_summary` 与 `run_stats` — passed**
证据：`group/app.py:89-112` 返回 `{group, members, limits, run_stats, route_summary, messages, running}`。README.md:200 的验收要求（含 route_summary/run_stats）成立。
（括号内「顶层键与 WS hello 的 snapshot 一致」的措辞精度问题另见残余观察 1。）

**⑥ WS 事件清单（含 route_summary / context_trimmed） — passed**
生产者全集（grep 全量）：`room_state` engine.py:196-201、`agent_status` :205、`notice` :208（`_notice` 统一出口）、`context_trimmed` :295-307、`run_start` :357、`message`（仅用户消息）:369、`error` :391/:394/:410、`run_end` :401、`route_summary` :584-599、`message_start` :638/:752、`route_changed` :663/:795、`message_delta` :673/:805、`message_end` :694/:730/:809/:836；`hello` app.py:239-252、`cleared` app.py:219、:283。
README.md:207-209 列出的 15 个 kind **全部有生产者**，其中 `route_summary`、`context_trimmed` 均在其中 ✓。
唯一未列的是 `app.py:270` 的 `pong`（仅应答客户端 `{"action":"ping"}`，**不进入广播流**）→ 见残余观察 2。

**⑦ AG2 适配层「未安装时明确降级说明」 — passed**
证据：`group/ag2_adapter.py:38-62`（未安装时 `describe()` 返回 `ag2: False` + detail「未安装 AG2：当前使用自研群聊内核 group.engine…主流程不受影响。想要 GroupChat 对照实现：pip install ag2」+ `kernel: group.engine.GroupEngine`）；`:87-97`（未安装时 `build_ag2_group` 抛 `RuntimeError`，消息含 `pip install ag2` 与「主流程不受影响」）；`:65-84`（`build_llm_config` 只依赖 `client.resolve`，不依赖 AG2 → 未安装也能测映射）。与 README.md:177-192 逐句一致；`app.py:164-183` 的 `/api/ag2` 把每个启用成员的 llm_config 可编译性逐条报出（不含密钥）。

**⑧ 已知边界章节如实（无「零密钥也能跑通链路」这类不成立声明） — passed**
- README.md:272-275 明确写「『没有任何密钥也能跑完整链路』**是不成立的**」，并给出真实行为：组长（唯一入口）失败 → 无人被 @ → 零产出 → 顶层 `error` + `run_end{aborted:true}`。与实现一致：`engine.py:369-401`（aborted 判定与 `run_end(aborted=...)`）、`:410`（无 captain 报 error）。
- README.md:270-271「没有工具调用」：`ag2_adapter.py:115`（`code_execution_config=False`），engine 全线无 tools 参数 ✓。
- README.md:276-277「串行发言」：`engine.py:450-467` 是顺序 `await` ✓。
- README.md:278-279「doubao-seed-2-1-lite-260915 未开通」：`providers.yaml:90` 注释同述 ✓。
- README.md:281「kimi 只给 2 次发言额度」：`models.yaml:60 max_turns_per_run: 2` ✓。
- README.md:282「单机单进程、重启即清空」：`app.py:71` 模块级单例 `ENGINE`、无持久化 ✓。
- README.md:263-266 的「DSH 用量账本边界」与 `scripts/integration_check.py:42/:46-49/:401-432` 的实现与结论文本一致 ✓。

## 残余观察（非 findings；不影响 pass；含最小修法）

1) **README.md:200 括号措辞可精确化（低）**。实现里三个快照形状并不字面相同：`/api/snapshot` 顶层 = `group, members, limits, run_stats, route_summary, messages, running`（app.py:97-112）；WS `hello.snapshot` = `ENGINE.snapshot()`（engine.py:869-878：running/run_id/members/limits/run_stats/route_summary/messages）+ `recorder_route` + `adapters`（app.py:244-248）；WS `{"action":"snapshot"}` 的 hello.snapshot 又是 `ENGINE.snapshot()` 本体（app.py:272-277）。三者**共有** `members/limits/run_stats/route_summary/messages/running`，但 hello 版本多 `run_id/recorder_route/adapters`，`/api/snapshot` 多 `group` 汇总块。括号里承诺的实质（「刷新后额度/同源/降级信息不会丢」）**成立**，故不判为契约级不一致。
   最小修法（一行）：把 README.md:200 的括号改为
   `（顶层键覆盖 WS hello snapshot 的核心字段 members/limits/run_stats/route_summary/messages，另含 group 汇总块；hello 的 snapshot 还带 run_id/recorder_route/adapters）`。

2) **WS 事件清单未提 `pong`（低）**。`app.py:269-270` 对客户端 `{"action":"ping"}` 回 `{"kind":"pong"}`。它不是广播事件，**不必**列入事件流；建议在 README.md:209 后补半句：`（客户端 {"action":"ping"} 会收到 {"kind":"pong"} 应答，不属于广播事件流）`。

3) **scripts/ 目录树不完整（低）**。README.md:22-27 列了 smoke_test.py / integration_check.py / run.ps1 / claim_probe.py / ws_probe.py，实际还有 `readme_probe.py`、`http_probe.py`、`review_probe.py`（后两个与已列的探针同属 checker 的独立验证脚本）。树是示意性的，不算错；建议补 `├── http_probe.py`、`└── review_probe.py  # checker 探针`。

## 复查命令与我实际执行的情况（**如实：我一条也没跑**）

我的工具集只有 read/glob/grep/web_*（无 shell、无写权限），因此**无法产出任何命令输出**，本报告的全部证据是**磁盘文件的静态逐行核对**（对「文档 ↔ 代码/配置」这类声明核对，静态读代码即为直接证据）。下列命令供队长/checker 复现与归档（我给出的期望值即我静态核对得到的结论）：

```powershell
cd ai-group
# (a) 绑定 README 字节（验收要求，我无法执行）
Get-FileHash README.md -Algorithm SHA256          # 期望前缀 53F43135…（团队记录值）
(Get-Content README.md).Count                     # 期望 282

# (b) 8 项核对里可直接打印的部分
.\.venv\Scripts\python.exe -c "from group.config import load_config,check_invariants;c=load_config();print([(a.id,a.route,a.fallback_route,a.enabled) for a in c.agents]);print('invariants',check_invariants(c))"
.\.venv\Scripts\python.exe -c "from group.engine import RunLimits;print(RunLimits())"
.\.venv\Scripts\python.exe -c "from group.app import app;print(sorted({r.path for r in app.routes}))"
.\.venv\Scripts\python.exe -c "from group.ag2_adapter import describe;print(describe()['detail'])"
```
（其中 `RunLimits()` 期望 `max_messages=10, max_depth=2, max_context_chars=9000`；`check_invariants` 期望 `[]`；`app.routes` 期望含 `/`、`/api/health`、`/api/snapshot`、`/api/diagnostics`、`/api/ag2`、`/api/chat`、`/api/reset`、`/ws/group`。）

## 版本锚定（替代 sha256 的内容锚点，供字节绑定核对）

我读到的是**同一份** README，依据其含以下仅在 t5 修订后出现的文本：`:200` 的「顶层键与 WS `hello` 的 snapshot 一致」、`:240` 的 `docs\capture_runtime_evidence.py --port 8801`、`:243-248` 的「563 秒 / 2315 事件 / 8 条发言」与「组长在配置层是 DeepSeek…以可观测降级形态跑在豆包上」。

## 盲点 / 未验证（不影响文档一致性结论）

- sha256 未由我计算（无 exec）；上述「团队记录值」为引用。
- 运行期事实未复测：provider 是否真的可用（②表）、fastapi/openai 版本号（①）、kimi `1500 空正文 / 3000 有正文`（models.yaml:46-47 与 README.md:126）、Moonshot 3 RPM、kimi 目录价 $0.95/$4（README.md:280）——这些是上游/环境事实，需真实调用或外部查询，静态复核只能确认「文档与配置/代码的表述一致」。
- 行号基于我的读取快照：README.md 282 行 / engine.py 878 行 / app.py 294 行 / models.yaml 137 行 / providers.yaml 128 行 / config.py 358 行；工程成员若在我读取（23:0x）之后又改动文件，请以引用文本定位。
- 交付限制：本报告**未落盘**（无写权限）+ sha256 未计算，这两项机械性验收需队长执行；其余结论均为我独立读到的证据。