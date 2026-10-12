$ErrorActionPreference = "Stop"

function Find-Python {
    if ($env:PYTHON -and (Test-Path $env:PYTHON)) {
        return $env:PYTHON
    }

    $candidates = @()
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $candidates += $pythonCommand.Source
    }
    $candidates += Get-ChildItem -Path (Join-Path $env:APPDATA "uv\python") -Recurse -Filter python.exe -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName

    foreach ($candidate in $candidates) {
        if (-not $candidate) {
            continue
        }
        & $candidate --version *> $null
        if ($LASTEXITCODE -eq 0) {
            return $candidate
        }
    }

    throw "No working Python interpreter found. Set PYTHON to a Python 3.11+ executable."
}

$python = Find-Python
Write-Host "Using Python: $python"
& $python scripts/check.py
exit $LASTEXITCODE
