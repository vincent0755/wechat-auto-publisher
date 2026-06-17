$ErrorActionPreference = "Stop"

$python = $null
if (Get-Command python -ErrorAction SilentlyContinue) {
    $python = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $python = "py -3"
} else {
    throw "没有找到 Python。请先安装 Python 3.10+，并勾选 Add python.exe to PATH。"
}

Invoke-Expression "$python -m pip install --upgrade pyinstaller"
Invoke-Expression "$python -m PyInstaller --noconsole --onefile --name WechatAutoPublisher app.py"

$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
    & $iscc installer.iss
    Write-Host "安装包已生成到 output 目录。"
} else {
    Write-Host "已生成 dist\WechatAutoPublisher.exe。"
    Write-Host "如需安装包，请安装 Inno Setup 6 后重新运行本脚本。"
}
