$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskOldPath = $env:PATH
try {
    # Avoid collecting unrelated Poppler/Libheif ICU/UCRT DLLs from host PATH.
    $env:PATH = "$env:WINDIR\System32;$env:WINDIR;$(Split-Path -Parent $taskPython)"
    & $taskPython -m PyInstaller --clean --noconfirm --windowed --onedir --noupx --name '补货助手' --icon "$taskRoot\src\app.ico" --add-data "$taskRoot\src\default_config.json;." --add-data "$taskRoot\src\app.ico;." --exclude-module numpy --exclude-module pandas --exclude-module matplotlib --exclude-module scipy --exclude-module IPython --exclude-module lxml --exclude-module tkinter --distpath "$taskRoot\交付" --workpath "$taskRoot\build" --specpath $taskRoot "$taskRoot\src\app.py"
    if ($LASTEXITCODE -ne 0) { throw '打包失败' }
} finally { $env:PATH = $taskOldPath }
& $taskPython "$taskRoot\prepare_delivery.py"
if ($LASTEXITCODE -ne 0) { throw '交付文档整理失败' }
