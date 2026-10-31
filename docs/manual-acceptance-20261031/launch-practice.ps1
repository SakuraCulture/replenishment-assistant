param(
    [ValidateSet('replenish','lifecycle')][string]$Mode='replenish',
    [switch]$Fresh,
    [string]$Exe,
    [switch]$NoLaunch
)
$ErrorActionPreference='Stop'
$packRoot=[IO.Path]::GetFullPath($PSScriptRoot)
$practiceRoot=Join-Path $packRoot '练习记录'
[IO.Directory]::CreateDirectory($practiceRoot) | Out-Null
$utf8=New-Object System.Text.UTF8Encoding($false)
$exeConfig=Join-Path $practiceRoot 'exe-path.txt'
if (-not $Exe -and (Test-Path -LiteralPath $exeConfig)) {$Exe=[IO.File]::ReadAllText($exeConfig).Trim()}
if (-not $Exe -or -not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog=New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title='选择现有交付包内的补货助手.exe（不会更新程序）'
    $dialog.Filter='补货助手|补货助手.exe|程序|*.exe'
    if ($dialog.ShowDialog() -ne 'OK') {exit 0}
    $Exe=$dialog.FileName
}
$Exe=(Resolve-Path -LiteralPath $Exe).Path
if ([IO.Path]::GetExtension($Exe) -ne '.exe') {throw '请选择EXE程序'}
[IO.File]::WriteAllText($exeConfig,$Exe,$utf8)
$pointer=Join-Path $practiceRoot ($Mode+'-current.txt')
$sessionRoot=$null
if (-not $Fresh -and (Test-Path -LiteralPath $pointer)) {
    $sessionRoot=[IO.Path]::GetFullPath([IO.File]::ReadAllText($pointer).Trim())
    if (-not $sessionRoot.StartsWith($practiceRoot+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {throw '练习目录越界，请核对练习记录中的路径'}
    if (-not (Test-Path -LiteralPath $sessionRoot -PathType Container)) {throw '原练习目录不存在，请使用新开练习入口'}
}
if (-not $sessionRoot) {
    $sessionRoot=Join-Path $practiceRoot ($Mode+'-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,6))
    [IO.Directory]::CreateDirectory($sessionRoot) | Out-Null
    if ($Mode -eq 'lifecycle') {Copy-Item -LiteralPath (Join-Path $packRoot 'seed\settings') -Destination (Join-Path $sessionRoot 'settings') -Recurse}
    else {[IO.Directory]::CreateDirectory((Join-Path $sessionRoot 'settings')) | Out-Null}
    [IO.File]::WriteAllText($pointer,$sessionRoot,$utf8)
}
$settingsDir=Join-Path $sessionRoot 'settings'
$exportDir=Join-Path $sessionRoot '导出'
[IO.Directory]::CreateDirectory($exportDir) | Out-Null
$exportConfig=Join-Path $settingsDir 'export_settings.json'
if (-not (Test-Path -LiteralPath $exportConfig)) {
    $value=@{schema=1;default_export_directory=$exportDir} | ConvertTo-Json
    [IO.File]::WriteAllText($exportConfig,$value,$utf8)
}
$pidFile=Join-Path $sessionRoot 'app-pid.txt'
if (Test-Path -LiteralPath $pidFile) {
    $oldProcess=Get-Process -Id ([int][IO.File]::ReadAllText($pidFile)) -ErrorAction SilentlyContinue
    if ($oldProcess -and $oldProcess.Path -eq $Exe) {throw '这份练习仍在运行，请先关闭对应窗口；不会强制结束程序'}
}
$env:REPLENISHMENT_DATA_DIR=$settingsDir
Write-Host ('练习数据库和配置：'+$settingsDir)
Write-Host ('默认导出目录：'+$exportDir)
if ($NoLaunch) {Write-Output $sessionRoot;exit 0}
$process=Start-Process -FilePath $Exe -WorkingDirectory (Split-Path -Parent $Exe) -WindowStyle Normal -PassThru
[IO.File]::WriteAllText($pidFile,[string]$process.Id,$utf8)
