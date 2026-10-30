#requires -Version 7.0
param(
    [ValidateSet('Help','Doctor','Test','Run','Build')][string]$Action='Help',
    [string]$BuildId,
    [switch]$Optimized
)
$ErrorActionPreference='Stop'
$taskRoot=$PSScriptRoot
$taskPython=Join-Path $taskRoot '.venv\Scripts\python.exe'
if ($Action -eq 'Help') {
    Write-Output @'
补货助手开发入口（从项目目录运行）
  .\dev.ps1 Doctor             核对Python、锁定依赖、Qt DLL及Git状态
  .\dev.ps1 Test               便携单测，结果留在qa/dev/runs
  .\dev.ps1 Test -Optimized    Python优化模式单测
  .\dev.ps1 Run                源码界面；隔离使用qa/dev/settings
  .\dev.ps1 Build -BuildId rcN  调用原构建脚本；干净提交、新目录、不覆盖
不会删除旧包、升级依赖、推送远端或访问日常用户配置。
'@
    exit 0
}
if (!(Test-Path -LiteralPath $taskPython)) { throw '缺少项目.venv，请按docs/开发环境.md重建到新目录，勿改全局Python。' }
if ($Optimized -and $Action -ne 'Test') { throw '-Optimized仅用于Test。' }
if ($Action -eq 'Build' -and !$BuildId) { throw 'Build必须明确指定新的-BuildId。' }
$taskKeys=@('REPLENISHMENT_DATA_DIR','PYTHONHASHSEED','PYTHONHOME','PYTHONPATH')
$taskPrevious=@{}
foreach ($taskKey in $taskKeys) { $taskPrevious[$taskKey]=[Environment]::GetEnvironmentVariable($taskKey,'Process') }
Push-Location $taskRoot
try {
    Remove-Item -LiteralPath Env:PYTHONHOME,Env:PYTHONPATH -ErrorAction SilentlyContinue
    $env:PYTHONHASHSEED='0'
    if ($Action -eq 'Run') {
        $taskData=Join-Path $taskRoot 'qa\dev\settings'
        $env:REPLENISHMENT_DATA_DIR=$taskData
        & $taskPython tools/check_environment.py
        if ($LASTEXITCODE -ne 0) { throw '环境检查失败，未启动应用。' }
        Write-Output "开发数据目录：$taskData（不读取日常用户资料）"
        & $taskPython src/app.py
        if ($LASTEXITCODE -ne 0) { throw '源码程序异常退出。' }
    } elseif ($Action -eq 'Build') {
        $env:REPLENISHMENT_DATA_DIR=Join-Path $taskRoot 'qa\dev\build-settings'
        & $taskPython tools/check_environment.py
        if ($LASTEXITCODE -ne 0) { throw '环境检查失败，未构建。' }
        & (Join-Path $taskRoot 'build.ps1') -BuildId $BuildId -PythonPath $taskPython
    } else {
        $taskRun=Join-Path $taskRoot ('qa\dev\runs\'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8))
        New-Item -ItemType Directory -Path $taskRun | Out-Null
        $env:REPLENISHMENT_DATA_DIR=Join-Path $taskRun 'settings'
        & $taskPython tools/check_environment.py --report (Join-Path $taskRun 'environment.json')
        if ($LASTEXITCODE -ne 0) { throw "环境检查失败，报告：$taskRun" }
        if ($Action -eq 'Test') {
            $taskArguments=@('-m','unittest','discover','-s','tests','-v')
            if ($Optimized) { $taskArguments=@('-O')+$taskArguments }
            # Native stderr contains unittest's normal output. Capture without treating it as a PowerShell exception.
            $taskOldError=$ErrorActionPreference
            try { $ErrorActionPreference='Continue'; & $taskPython @taskArguments *> (Join-Path $taskRun 'tests.txt'); $taskExit=$LASTEXITCODE }
            finally { $ErrorActionPreference=$taskOldError }
            Get-Content -LiteralPath (Join-Path $taskRun 'tests.txt') -Tail 5
            if ($taskExit -ne 0) { throw "单测失败，报告：$taskRun" }
        }
        Write-Output "本轮记录：$taskRun"
    }
} finally {
    foreach ($taskKey in $taskKeys) {
        if ($null -eq $taskPrevious[$taskKey]) { Remove-Item -LiteralPath ('Env:'+$taskKey) -ErrorAction SilentlyContinue }
        else { [Environment]::SetEnvironmentVariable($taskKey,$taskPrevious[$taskKey],'Process') }
    }
    Pop-Location
}
