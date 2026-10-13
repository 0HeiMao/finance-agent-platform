$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    $projectPython = (Get-Command python -ErrorAction Stop).Source
}
& $projectPython (Join-Path $PSScriptRoot 'project_cli.py') start @args
exit $LASTEXITCODE
