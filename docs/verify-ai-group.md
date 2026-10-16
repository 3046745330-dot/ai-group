# ai-group 独立验证报告（t2）

- 验证人：checker（独立于实现者 builder）
- 验证方式：**自己起服务、自己发 HTTP/WebSocket 请求、自己读事件流**；不采信实现者自述
- 结论：**6 条验收标准全部 passed**
- 日期：2026-10-12

> 本报告所有输出片段都来自本次真跑，原始文本留在 `ai-group/docs/verify/` 下：
> `smoke-mock-only.txt`、`http-probe-real.txt`、`http-probe-mock.txt`、
> `ws-probe-mockonly.txt`、`uvicorn-8803-startup.log(.err)`。

## 0. 被验证的修订（前后哈希一致，证明验证过程未改动实现）

验证前冻结、验证结束后复算，`ai-group/group|web|config` 下非 `__pycache__` 文件
**逐一一致**（SHA256 前 16 位）：

| 文件 | 哈希 |
|---|---|
| group/engine.py | F3297505A21A29B7 |
| group/llm.py | A4B8A224B1BEBEA4 |
| group/app.py | A0D72031EDEF983F |
| group/config.py | 53F3E55B76A52533 |
| group/ag2_adapter.py | B109CFEE6465F0F9 |
| group/__init__.py | 4C75AA62B3FFAF47 |
| config/providers.yaml | ECDFA993CA3F4881 |
| config/models.yaml | 1BFEB365FFF96911 |
| config/roles/captain.md | D9D273053750D264 |
| config/roles/member_analyst.md | D7AB9DED3C87F5B9 |
| config/roles/member_critic.md | 9E4F04AD476BA8D1 |
| config/roles/member_mock.md | D4CC783360F12CDE |
| config/roles/recorder.md | 9564079CFC9D150F |
| web/index.html | 9F7F9173FA5FCC74 |
| web/app.js | 759A1838A36918D2 |
| web/style.css | F039C520244BFBA5 |

复算命令：

```powershell
cd ai-group
Get-ChildItem -Recurse -File group, web, config | Where-Object { $_.FullName -notmatch '__pycache__' } | Get-FileHash
```

## 1. 环境与端口

- 解释器：`..\campus-market\backend\.venv\Scripts\python.exe`（Python 3.12.14）
- **8801**：真实配置实例（验收 1/2），只发 `/api/health`、`/`，**不发聊天请求**，零额度消耗
- **8802**：`SMOKE_MOCK_ONLY=1` 实例（验收 3/4 的聊天/额度护栏），保证确定性与零花费
- **8803**：一次性冷启动计时实例，测完即杀
- 起服务全部用**后台作业**，未在前台阻塞终端

## 2. 验收逐条

### A1 后台起服务、30 秒内就绪、日志无 Traceback —— **passed**

冷启动计时（8803，独立进程，从 `Start-Process` 到 `/api/health` 首次 200）：

```
ready=True elapsed=1.35 s
--- stderr ---
INFO:     Started server process [39040]
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8803 (Press CTRL+C to quit)
```

8801 后台作业日志（真实配置实例）：

```
INFO:     Started server process [30040]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8801 (Press CTRL+C to quit)
```

命令（在 ai-group 目录下）：

```powershell
..\campus-market\backend\.venv\Scripts\python.exe -m uvicorn group.app:app --host 127.0.0.1 --port 8801
```

判定：就绪 1.35s ≪ 30s；`Uvicorn running` 出现；日志中无 Traceback
（PowerShell 把 stderr 的 INFO 行渲染成 `NativeCommandError` 只是显示包装，不是异常）。

### A2 `/api/health` 与 `/` 均返回 200 —— **passed**

真实配置实例（8801）：

```
[PASS] GET /api/health 返回 200，providers 有 ok/detail，members 列出启用成员表
  —— HTTP 200 40ms；providers=deepseek=unavailable, moonshotai-cn=ok, doubao=ok, openai=unavailable, mock=ok；
     members(6)=captain[on], kimi[on], demo-bot[on], doubao[on], gpt[off], recorder[on]
[PASS] GET / 返回 200 且 HTML 含群聊界面关键元素
  —— HTTP 200 97ms，2146 字节，命中 ['群', 'chat', 'app.js']
结果：2/2 通过 ALL PASS
```

命令（也是 t2 契约里的第二条 verify）：

```powershell
cd ai-group
..\campus-market\backend\.venv\Scripts\python.exe -c "import urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:8801/api/health'));print(d['providers'])"
# EXIT=0
# [{'provider': 'deepseek', 'ok': False, 'detail': '未配置（DEEPSEEK_API_KEY）'},
#  {'provider': 'moonshotai-cn', 'ok': True, 'detail': '密钥来源 dsh:MOONSHOTAI_CN_API_KEY'},
#  {'provider': 'doubao', 'ok': True, 'detail': '密钥来源 dsh:ARK_API_KEY'},
#  {'provider': 'openai', 'ok': False, 'detail': '未配置（OPENAI_API_KEY）'},
#  {'provider': 'mock', 'ok': True, 'detail': '离线通道，无需密钥'}]
```

探针：`scripts/http_probe.py`（只读，不发聊天）。

### A3 WebSocket 首帧 hello + 完整发言链 —— **passed**

mock-only 实例（8802），`scripts/ws_probe.py`：

```
[PASS] WS /ws/group 首帧 kind=hello 且带 members 快照
  —— kind=hello，members=['captain', 'kimi', 'demo-bot', 'doubao', 'recorder']
[PASS] POST /api/chat 后 WS 收到完整事件流（run_start→…→run_end）
  —— HTTP 200，413 个事件，0.06s，
     kinds=['agent_status','message','message_delta','message_end','message_start','notice','room_state','route_summary','run_end','run_start']
[PASS] message_end 覆盖 captain + 至少一个组员 + recorder 纪要
  —— senders=['captain','kimi','demo-bot','captain','kimi','demo-bot','captain','recorder']
[PASS] 每条发言都带实际 route / requested_route（降级可追踪）
  —— [('captain','mock/mock-fast','mock/mock-fast'), …, ('recorder','mock/mock-deep','mock/mock-deep')]
```

`message_start` / `message_delta` / `message_end` 三类事件均在流中出现，
`message_end.sender` 覆盖 `captain` + 组员（kimi、demo-bot），最后一条为 `recorder` 纪要。

### A4 额度护栏可复现 + 未知 @ 目标只 notice —— **passed**

同一轮（会诱发多个 @ 的协作型提问）的逐成员计数与上限对照：

```
[PASS] 额度护栏：逐成员发言数 ≤ max_turns_per_run，派发链总数 ≤ max_messages
  —— 计数={'captain': 3, 'kimi': 2, 'demo-bot': 2}；
     limits={'max_messages': 10, 'max_depth': 2, 'max_context_chars': 9000, 'max_model_calls_per_run': 11}；
     派发链 7 条 ≤ max_messages=10；recorder 1 条（不占派发链配额）
[PASS] 无人被 @ 时不唤醒任何组员 —— senders=['captain']
[PASS] 不存在的 @ 目标只产生 notice，不报错
  —— notice=@「不存在的成员」没有被派发：群里没有成员「不存在的成员」，errors=0
```

判定：`captain 3≤4`、`kimi 2≤2`、`demo-bot 2≤2`，派发链 7≤10，记录员恰好 1 条；
未知 @ 目标只产生 `notice`，`errors=0`。全部可复现。

### A5 本报告落盘并逐条判定 —— **passed**

本文件即证据；命令、原始输出、端口（8801/8802/8803）、耗时均已写入。

### A6 验证过程未修改实现文件 —— **passed**

- 验证前后对 `group/`、`web/`、`config/` 全部文件做哈希，逐条一致（见第 0 节）。
- 本次 checker 只写了 `scripts/`（新增 `http_probe.py`，修 `ws_probe.py` 自身的 GBK 打印缺陷）
  和 `docs/verify/`、本文件。

## 3. 契约 verify 命令逐条结果

| # | 命令 | 结果 |
|---|---|---|
| 1 | `cd ai-group; ..\campus-market\backend\.venv\Scripts\python.exe scripts\smoke_test.py --mock-only` | **EXIT=0**，`结果：全部通过（64 项检查）`，耗时 24.2s（`docs/verify/smoke-mock-only.txt`） |
| 2 | `cd ai-group; ..\campus-market\backend\.venv\Scripts\python.exe -c "import urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:8801/api/health'));print(d['providers'])"` | **EXIT=0**，打印 5 个 provider 的 ok/detail（见 A2） |

## 4. 独立探针清单（均在 `ai-group/scripts/`，只读实现）

```powershell
cd ai-group
$py = "..\campus-market\backend\.venv\Scripts\python.exe"
& $py scripts\smoke_test.py --mock-only            # EXIT=0, 64/64
& $py scripts\http_probe.py --port 8801            # 真实配置：2/2
& $py scripts\http_probe.py --port 8802 --expect-mock
& $py scripts\ws_probe.py --port 8802              # 9/9, EXIT=0
```

## 5. 如实记录的边界（不影响 above 判定，但影响对「真实模型路径」的解读）

1. 验收 3/4 的聊天部分我故意跑在 **mock-only 实例（8802）** 上：一是确定性（额度计数、
   未知 @、无 @ 场景需要可复现），二是该账号 Moonshot 有组织级 3 RPM 限流，避免与队长
   正在跑的真实自检抢额度。**真实配置实例（8801）只做了 A1/A2 的启动与只读接口验证。**
2. 当前环境下 `deepseek` 无有效 API key（`ok=False`），`captain`/`recorder` 的主路由是
   deepseek，因此**真实模式下每一轮都会降级到豆包**（`requested_route=deepseek/...`,
   `route=doubao/...`, `degraded=true`）。测试里已确认降级事件可见（notice + route_changed +
   消息 warning），但对外描述时不能写成「组长由 DeepSeek 担任」。
3. `scripts/ws_probe.py` 原先在结尾打印 `✅`，在 GBK 控制台下会抛 `UnicodeEncodeError`，
   造成「9 项全 PASS 却退出码 1」的误导。本次已由 checker 在自己的脚本里修掉
   （`sys.stdout.reconfigure(encoding="utf-8", errors="replace")`），修后 EXIT=0。
   该缺陷属于验证脚本自身，不涉及被验证的实现。

## 6. 复现步骤（从零）

```powershell
cd ai-group
$py = "..\campus-market\backend\.venv\Scripts\python.exe"

# A1/A2：真实配置实例（只读接口，零额度）
Start-Job -ScriptBlock { Set-Location $using:PWD; & $using:py -m uvicorn group.app:app --host 127.0.0.1 --port 8801 }
& $py scripts\http_probe.py --port 8801

# A3/A4：mock-only 实例
$env:SMOKE_MOCK_ONLY="1"
Start-Job -ScriptBlock { Set-Location $using:PWD; & $using:py -m uvicorn group.app:app --host 127.0.0.1 --port 8802 }
& $py scripts\ws_probe.py --port 8802

# 契约 verify
& $py scripts\smoke_test.py --mock-only
& $py -c "import urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:8801/api/health'));print(d['providers'])"
```
