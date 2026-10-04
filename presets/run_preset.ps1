# RVC 实时变声 . 预设启动器
# 选一个预设 -> 写入 configs/config.json -> 关掉旧实例 -> 启动实时变声

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'

$presetDir = $PSScriptRoot
$root = Split-Path -Parent $presetDir
$cfg  = Join-Path $root 'configs\config.json'
$py   = Join-Path $root 'venv\Scripts\python.exe'

$files = @(Get-ChildItem $presetDir -Filter *.json | Sort-Object Name)
if ($files.Count -eq 0) { Write-Host '没有预设文件。'; Read-Host '按回车退出'; exit 1 }

Write-Host ''
Write-Host '==========  RVC 实时变声 . 预设表  =========='
$i = 1
foreach ($f in $files) {
    $j = Get-Content $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    $label = $f.BaseName
    if ($j.PSObject.Properties.Name -contains '_label') { $label = $j._label }
    Write-Host ('  [{0}] {1}' -f $i, $label)
    Write-Host ('        pitch={0}  formant={1}  index={2}  block={3}  fade={4}  extra={5}  f0={6}' -f $j.pitch, $j.formant, $j.index_rate, $j.block_time, $j.crossfade_length, $j.extra_time, $j.f0method)
    $i++
}
Write-Host ''

$sel = Read-Host '输入编号后回车'
$n = 0
if (-not ($sel -and [int]::TryParse($sel.Trim(), [ref]$n)) -or $n -lt 1 -or $n -gt $files.Count) {
    Write-Host '编号无效，已取消。'
    Read-Host '按回车退出'
    exit 1
}
$chosen = $files[$n - 1]

# 关掉正在运行的实例，避免抢麦克风/扬声器
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" | Where-Object { $_.CommandLine -match 'realtime_gui' })
foreach ($p in $procs) {
    Write-Host ('关闭正在运行的实例 PID {0}' -f $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($procs.Count -gt 0) { Start-Sleep -Seconds 3 }

# 把预设写入 config.json（去掉 _label 字段）
$obj = Get-Content $chosen.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
$obj.PSObject.Properties.Remove('_label')
$txt = $obj | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText($cfg, $txt, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ''
Write-Host ('已应用预设：{0}' -f $chosen.BaseName)
Start-Process -FilePath $py -ArgumentList 'realtime_gui.py' -WorkingDirectory $root
Write-Host '正在启动实时变声窗口，首次加载约 30~60 秒，请稍候……'
Start-Sleep -Seconds 2
