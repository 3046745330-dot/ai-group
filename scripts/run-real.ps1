# =============================================================================
# AI 群聊 · 真实模式启动器
# -----------------------------------------------------------------------------
# 存在的理由：DEEPSEEK_API_KEY 若只写进「用户级环境变量」，那些在它被设置之前
# 就已启动的父进程（例如 DSH 本身）不会继承，于是服务端读不到 key，
# 「组长由 DeepSeek 担任」又会静默降级。
#
# 本脚本在启动 uvicorn 前显式补读用户级变量并注入子进程环境，
# **不把密钥写入任何文件、也不把密钥放进命令行参数**（命令行会被进程列表看到）。
#
# 用法：  .\scripts\run-real.ps1  [-Port 8801] [-Python <解释器路径>]
# =============================================================================
param(
  [int]$Port = 8801,
  [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# --- 1. 解析解释器（与 run.ps1 同规则，写死不改） ---------------------------
$candidates = New-Object System.Collections.Generic.List[string]
if ($Python) { $candidates.Add($Python) }
$candidates.Add((Join-Path $root ".venv\Scripts\python.exe"))
foreach ($name in @("python", "python3", "py")) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if ($cmd) { $candidates.Add($cmd.Source) }
}

$py = $null
foreach ($cand in $candidates) {
  $full = [System.IO.Path]::GetFullPath($cand)
  if (-not (Test-Path $full)) { continue }
  $probe = & $full -c "import fastapi, uvicorn, openai, yaml; print('ok')" 2>$null
  if ($LASTEXITCODE -eq 0 -and $probe -eq "ok") { $py = $full; break }
}
if (-not $py) {
  throw "找不到装了依赖的解释器。请先：python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}
Write-Host "[run-real] 解释器：$py" -ForegroundColor Cyan

# --- 2. 补读用户级 DEEPSEEK_API_KEY -----------------------------------------
$key = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
if (-not $key) { $key = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "Machine") }
if (-not $key) {
  Write-Host "[run-real] 未找到 DEEPSEEK_API_KEY（用户级/机器级）。" -ForegroundColor Yellow
  Write-Host "           组长会降级到 fallback；若要真实使用 DeepSeek：" -ForegroundColor Yellow
  Write-Host "           [Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY','sk-xxx','User')" -ForegroundColor Yellow
} else {
  $env:DEEPSEEK_API_KEY = $key
  Write-Host ("[run-real] DEEPSEEK_API_KEY 已注入（长度 {0}，前缀 {1}…）" -f $key.Length, $key.Substring(0, [Math]::Min(6, $key.Length))) -ForegroundColor Green
}

# 顺带把另外两家也补上（有就用，没有不影响）
foreach ($name in @("MOONSHOTAI_CN_API_KEY", "MOONSHOT_API_KEY", "ARK_API_KEY", "OPENAI_API_KEY")) {
  if (-not $env:$name) {
    $v = [Environment]::GetEnvironmentVariable($name, "User")
    if ($v) { Set-Item -Path "env:$name" -Value $v; Write-Host "[run-real] $name 已注入（用户级）" -ForegroundColor DarkGray }
  }
}

# --- 3. 端口检查 ------------------------------------------------------------
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) { throw "端口 $Port 已被占用（PID $($busy.OwningProcess)）。换一个：-Port 8802" }

# --- 4. 起服务（真实模式：不设 SMOKE_MOCK_ONLY） ----------------------------
Remove-Item Env:\SMOKE_MOCK_ONLY -ErrorAction SilentlyContinue
Write-Host ""
Write-Host ("  真实模式启动中…  浏览器打开 http://127.0.0.1:{0}" -f $Port) -ForegroundColor Green
Write-Host "  提示：Kimi 有组织级 3 RPM 限流，一轮群聊可能较慢；成员卡会显示「思考中」" -ForegroundColor DarkGray
Write-Host ""
& $py -m uvicorn group.app:app --host 127.0.0.1 --port $Port
