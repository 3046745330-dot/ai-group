# 证据留档状态（ai-group）

> 本目录只放**与当前 head 对齐**的证据。任何早于当前实现的"全绿"输出都不留在这里。

## 1. `smoke-mock-only.txt` — 离线自检（当前 head，零额度）

- 命令：`..\campus-market\backend\.venv\Scripts\python.exe scripts\smoke_test.py --mock-only`
- 结果：**64 项检查全部 PASS，`EXIT=0`**（在控制台直接取 `$LASTEXITCODE`，不是只看重定向文本）
- 覆盖：配置装载 / 每个成员独立角色提示词 / 记录员条目 / 配置不变量（fallback 异厂、标签唯一）/
  @提及抽取与宽松匹配 / 弱 token 与邮箱不误命中 / 事件序列 run_start→…→run_end /
  组长+组员+记录员三角色发言 / 无人被 @ 不唤醒组员 / 未知 @ 只 notice /
  降级可观测（notice + route_changed + 消息 warning）/ 同源与失败对纪要可见（route_summary）/
  D1 重复 @ 去重 / D4 弱 token 与歧义标签 / D6 失败调用不吃成功额度 / F1 失败可见 /
  F2 @组长 不静默丢弃 / F5 全厂商挂掉有顶层 error + run_end{aborted:true} /
  四类额度护栏 / 上下文预算与 context_trimmed / 密钥零落盘。

## 2. 真实模型自检（`smoke_test.py`，不带 `--mock-only`）

- **状态：本轮由队长统一安排运行**（`pwsh-158`）。原因：Moonshot 该账号有组织级 **3 RPM** 限流，
  两个进程并发跑真实自检会互相饿死、白烧额度。
- 命令（队长侧）：
  ```powershell
  cd ai-group
  ..\campus-market\backend\.venv\Scripts\python.exe scripts\smoke_test.py
  "EXIT=$LASTEXITCODE"
  ```
- 出结果后请把控制台原样输出追加到本目录（建议命名 `smoke-real.txt`，含 `EXIT=` 行）。
- 真实模式的行为：主链路那一轮用**真实模型**，其余边界用例自动切到离线副本（cfg_mock），
  单项超时预算 2400s（可用 `SMOKE_REAL_TIMEOUT` 覆盖）。

## 3. 真实进程边界联调 `integration_check.py`

- **状态：修复后尚未重跑**（同样为避免与真实自检并发抢 3 RPM 额度）。
- 命令：
  ```powershell
  cd ai-group
  ..\campus-market\backend\.venv\Scripts\python.exe scripts\integration_check.py --port 8831
  "EXIT=$LASTEXITCODE"
  ```
- 它真起 uvicorn、走 HTTP + WebSocket，打印每个发言人的**实际路由**、
  `/api/diagnostics` 的调用记录（目标 host / 成败 / 耗时 / 失败原文），
  并做 DSH 用量账本前后差值交叉核对。
- 上一次（21:47，**修复前**的代码）的实测结论留档要点：captain 主路由 deepseek 因缺密钥
  降级到 doubao（事件流有 notice + route_changed + 消息 warning），kimi 走 moonshotai-cn，
  doubao 走 doubao——**异厂**；但该次运行在 420s 超时内没等到 `run_end` 与记录员纪要，
  且当时还没有 `route_summary`（这两条已在当前 head 修掉）。该次输出**不作为当前 head 的证据**。

## 4. 关于别的成员留下的证据文件

- `docs/_claim_probe.txt`、`docs/_ws_probe.txt`、`docs/_readme_probe.txt`、`docs/checker-review-findings.md`、
  `docs/tech-review-*.md`、`scripts/claim_probe.py`、`scripts/ws_probe.py` 是 **checker 的独立取证产物**，
  不属于本实现，未做任何改动。
- `docs/checker-review-findings.md` 里 F1 提到的「`_smoke_mockonly.txt` 还是旧的 49 项版本」这一点，
  已由本目录的 `smoke-mock-only.txt`（64 项、当前 head）取代；该 findings 文件本身由 checker 维护。
- `scripts/integration_check.py` 是本实现的联调脚本（不是 checker 探针）。
