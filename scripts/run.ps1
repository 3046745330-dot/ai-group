# =============================================================================
# AI 群聊 · 多模型协作系统 —— 一键启动脚本
# 用法：  .\scripts\run.ps1 [-Port 8801] [-MockOnly] [-Python <解释器路径>]
#
# 解释器选择规则（写死，不再「猜」）：
#   1) -Python 显式指定；
#   2) ai-group\.venv\Scripts\python.exe（本项目自己的 venv，存在就用）；
#   3) ..\campus-market\backend\.venv\Scripts\python.exe（本机已验证含 fastapi/uvicorn/openai）；
#   4) 都不满足 → 直接报错退出，并打印建 venv 的命令。
# 绝不静默退回到没有 fastapi 的解释器（例如 DSH 自带的 python），
# 因为那会以一个看不懂的堆栈结束。
# =============================================================================
param(
  [int]$Port = 8801,
  [switch]$MockOnly,
  [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# --- 1. 选一个「真的装了依赖」的 Python --------------------------------------
$candidates = New-Object System.Collections.Generic.List[string]
if ($Python) { $candidates.Add($Python) }
$candidates.Add((Join-Path $root ".venv\Scripts\python.exe"))
$candidates.Add((Join-Path $root "..\campus-market\backend\.venv\Scripts\python.exe"))

$exe = $null
foreach ($candidate in $candidates) {
  $full = [System.IO.Path]::GetFullPath($candidate)
  if (-not (Test-Path $full)) { continue }
  $probe = & $full -c "import fastapi, uvicorn, openai, yaml; print('ok')" 2>$null
  if ($LASTEXITCODE -eq 0 -and $probe -eq "ok") { $exe = $full; break }
  Write-Host "[run] 跳过（缺少 fastapi/uvicorn/openai/yaml）：$full" -ForegroundColor DarkGray
}

if (-not $exe) {
  Write-Host "[run] 找不到带依赖的 Python 解释器。" -ForegroundColor Red
  Write-Host "      本项目不静默退回到没装依赖的解释器（例如 DSH 自带 python）。" -ForegroundColor Red
  Write-Host "      请二选一：" -ForegroundColor Yellow
  Write-Host "        a) 复用已装好依赖的 venv：.\scripts\run.ps1 -Python ..\campus-market\backend\.venv\Scripts\python.exe"
  Write-Host "        b) 自建 venv：python -m venv .venv; .\.venv\Scripts\python.exe -m pip install fastapi `"uvicorn[standard]`" openai httpx pyyaml"
  exit 1
}

Write-Host "[run] 解释器：$exe" -ForegroundColor Cyan

# --- 2. 依赖自检（上面已探测，这里只做说明） --------------------------------
$version = & $exe -c "import sys; print(sys.version.split()[0])"
Write-Host "[run] Python $version，依赖已就绪" -ForegroundColor DarkGray

# --- 3. 端口占用检查 --------------------------------------------------------
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
  Write-Host "[run] 端口 $Port 已被占用，请换一个：.\scripts\run.ps1 -Port 8802" -ForegroundColor Yellow
  exit 1
}

# --- 4. 起服务 --------------------------------------------------------------
if ($MockOnly) { $env:SMOKE_MOCK_ONLY = "1"; Write-Host "[run] 已开启离线模式（不调用任何真实模型）" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  ╭──────────────────────────────────────────────╮" -ForegroundColor Green
Write-Host "  │  AI 群 · 多模型协作系统已启动                │" -ForegroundColor Green
Write-Host ("  │  请在浏览器打开： http://127.0.0.1:{0,-6} │" -f $Port) -ForegroundColor Green
Write-Host "  │  健康检查： /api/health  调用记录： /api/diagnostics" -ForegroundColor Green
Write-Host "  ╰──────────────────────────────────────────────╯" -ForegroundColor Green
Write-Host ""

& $exe -m uvicorn group.app:app --host 127.0.0.1 --port $Port
