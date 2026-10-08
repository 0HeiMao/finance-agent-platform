$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

if (-not (Test-Path '.venv\Scripts\python.exe')) {
  python -m venv .venv
}

& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt

Set-Location (Join-Path $projectRoot 'frontend')
npm install

Write-Host '项目依赖安装完成。'
Write-Host '启动前端：Set-Location frontend; npm run dev'
Write-Host '启动后端：.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000'
