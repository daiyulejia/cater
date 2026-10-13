$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$web = Join-Path $root "web"
$source = Join-Path $web "dist"
$target = Join-Path $root "cater\static\workroom"

Push-Location $root
try {
    pnpm --dir $web build
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item -Recurse -Force (Join-Path $source "*") $target
    Write-Host "Bundled Cater workroom into $target"
}
finally {
    Pop-Location
}
