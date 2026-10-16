$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$web = Join-Path $root "web"
$stagingRoot = [System.IO.Path]::GetFullPath((Join-Path $root ".cater\web-build"))
$source = Join-Path $stagingRoot ([guid]::NewGuid().ToString('N'))
$target = Join-Path $root "cater\static\workroom"
$staticRoot = [System.IO.Path]::GetFullPath((Join-Path $root "cater\static"))
$targetPath = [System.IO.Path]::GetFullPath($target)
$replacement = "$targetPath-new-$([guid]::NewGuid().ToString('N'))"
$previous = "$targetPath-old-$([guid]::NewGuid().ToString('N'))"
$separator = [System.IO.Path]::DirectorySeparatorChar

if (-not $targetPath.StartsWith("$staticRoot$separator", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to replace a workroom directory outside Cater static assets."
}

function Assert-LocalDirectory([string]$path, [string]$parent) {
    $absolute = [System.IO.Path]::GetFullPath($path)
    $boundary = [System.IO.Path]::GetFullPath($parent)
    if (-not $absolute.StartsWith("$boundary$separator", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to change a directory outside the expected build boundary."
    }
    $current = $absolute
    while ($current -and $current.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -Force -LiteralPath $current).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing to change linked build directories."
            }
        }
        $current = Split-Path -Parent $current
    }
    if (Test-Path -LiteralPath $absolute) {
        $links = Get-ChildItem -Force -Recurse -LiteralPath $absolute | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } | Select-Object -First 1
        if ($links) { throw "Refusing to remove a build tree containing links." }
    }
}

Push-Location $root
$published = $false
try {
    Assert-LocalDirectory $source $stagingRoot
    pnpm --dir $web build --outDir $source
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    if (-not (Test-Path -LiteralPath (Join-Path $source 'index.html') -PathType Leaf)) {
        throw "Build did not produce an entry document; existing bundle was preserved."
    }

    Assert-LocalDirectory $targetPath $staticRoot
    Assert-LocalDirectory $replacement $staticRoot
    Assert-LocalDirectory $previous $staticRoot
    New-Item -ItemType Directory -Path $replacement | Out-Null
    Copy-Item -Recurse -Force (Join-Path $source "*") $replacement
    if (Test-Path -LiteralPath $targetPath) {
        Move-Item -LiteralPath $targetPath -Destination $previous
    }
    try {
        Move-Item -LiteralPath $replacement -Destination $targetPath
    }
    catch {
        if (Test-Path -LiteralPath $previous) {
            Assert-LocalDirectory $previous $staticRoot
            Assert-LocalDirectory $targetPath $staticRoot
            Move-Item -LiteralPath $previous -Destination $targetPath
        }
        throw
    }
    $published = $true
    Write-Host "Bundled Cater workroom into $target"
}
finally {
    try {
        if ($published) {
            if (Test-Path -LiteralPath $previous) {
                Assert-LocalDirectory $previous $staticRoot
                Remove-Item -LiteralPath $previous -Recurse -Force
            }
            if (Test-Path -LiteralPath $source) {
                Assert-LocalDirectory $source $stagingRoot
                Remove-Item -LiteralPath $source -Recurse -Force
            }
        }
    }
    finally { Pop-Location }
}
