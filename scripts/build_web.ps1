$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$web = Join-Path $root "web"
$source = Join-Path $web "dist"
$target = Join-Path $root "cater\static\workroom"
$staticRoot = [System.IO.Path]::GetFullPath((Join-Path $root "cater\static"))
$targetPath = [System.IO.Path]::GetFullPath($target)
$separator = [System.IO.Path]::DirectorySeparatorChar

if (-not $targetPath.StartsWith("$staticRoot$separator", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to replace a workroom directory outside Cater static assets."
}

Push-Location $root
try {
    pnpm --dir $web build
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    if (Test-Path -LiteralPath $targetPath) {
        Remove-Item -LiteralPath $targetPath -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $targetPath | Out-Null
    Copy-Item -Recurse -Force (Join-Path $source "*") $targetPath
    Write-Host "Bundled Cater workroom into $target"
}
finally {
    Pop-Location
}
