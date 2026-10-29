param([Parameter(Mandatory=$true)][ValidatePattern('^[a-zA-Z0-9_-]+$')][string]$BuildId)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
Push-Location $taskRoot
try {
    $taskState = & git status --porcelain
    if ($LASTEXITCODE -ne 0 -or $taskState) { throw '请先提交源码；打包需要干净工作区。' }
    $taskVersion = (Get-Content -LiteralPath "$taskRoot\src\version.json" -Raw | ConvertFrom-Json).version
    $taskCommit = (& git rev-parse HEAD).Trim()
    $taskDist = Join-Path $taskRoot "交付_${taskVersion}_${BuildId}"
    $taskBuild = Join-Path $taskRoot "build_${taskVersion}_${BuildId}"
    if ((Test-Path -LiteralPath $taskDist) -or (Test-Path -LiteralPath $taskBuild)) { throw '构建编号已存在，请使用新编号；不覆盖旧产物。' }
    New-Item -ItemType Directory -Path $taskBuild | Out-Null
    $taskInfo = @{source_commit=$taskCommit; build_id=$BuildId; version=$taskVersion; built_at=(Get-Date -Format o)}
    $taskInfo | ConvertTo-Json | Set-Content -LiteralPath "$taskBuild\build_info.json" -Encoding UTF8
    $taskOldPath = $env:PATH
    try {
        # Avoid unrelated host Poppler/Libheif DLLs.
        $env:PATH = "$env:WINDIR\System32;$env:WINDIR;$(Split-Path -Parent $taskPython)"
        & $taskPython -m PyInstaller --clean --windowed --onedir --noupx --name '补货助手' --icon "$taskRoot\src\app.ico" --add-data "$taskRoot\src\default_config.json;." --add-data "$taskRoot\src\default_must_sell.json;." --add-data "$taskRoot\src\version.json;." --add-data "$taskBuild\build_info.json;." --add-data "$taskRoot\src\app.ico;." --exclude-module numpy --exclude-module pandas --exclude-module matplotlib --exclude-module scipy --exclude-module IPython --exclude-module lxml --exclude-module tkinter --distpath $taskDist --workpath "$taskBuild\work" --specpath $taskBuild "$taskRoot\src\app.py"
        if ($LASTEXITCODE -ne 0) { throw '打包失败；保留目录供排查，重试请使用新编号。' }
    } finally { $env:PATH = $taskOldPath }
    & $taskPython "$taskRoot\prepare_delivery.py" --delivery "$taskDist\补货助手"
    if ($LASTEXITCODE -ne 0) { throw '交付文档整理失败' }
    Write-Output $taskDist
} finally { Pop-Location }
