"""Exercise publication and rollback; fake pnpm generates only fixture bytes."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


SHELL = shutil.which("pwsh") or shutil.which("powershell")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_web.ps1"


@unittest.skipUnless(os.name == "nt" and SHELL, "Windows PowerShell publication checks")
class WebBuildPublicationTests(unittest.TestCase):
    def fixture(self, mode):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        (root / "scripts").mkdir()
        shutil.copyfile(SCRIPT, root / "scripts/build_web.ps1")
        package = root / "cater/static/workroom"
        package.mkdir(parents=True)
        (package / "index.html").write_text("OLD", encoding="utf-8")
        development = root / "web/dist"
        development.mkdir(parents=True)
        (development / "index.html").write_text("DEVELOPMENT", encoding="utf-8")
        driver = root / "driver.ps1"
        driver.write_text(r'''
$ErrorActionPreference = 'Stop'
function pnpm {
    if ($env:CATER_BUILD_FIXTURE -eq 'build_fail') { $global:LASTEXITCODE = 42; return }
    $output = $args[[Array]::IndexOf($args, '--outDir') + 1]
    [IO.Directory]::CreateDirectory($output) | Out-Null
    if ($env:CATER_BUILD_FIXTURE -ne 'no_entry') { [IO.File]::WriteAllText((Join-Path $output 'index.html'), 'NEW') }
    $global:LASTEXITCODE = 0
}
function Copy-Item {
    param([switch]$Recurse, [switch]$Force, [string]$Path, [string]$Destination)
    if ($env:CATER_BUILD_FIXTURE -eq 'copy_fail') { throw 'injected copy failure' }
    Microsoft.PowerShell.Management\Copy-Item -Recurse:$Recurse -Force:$Force -Path $Path -Destination $Destination
}
function Move-Item {
    param([string]$LiteralPath, [string]$Destination)
    if ($env:CATER_BUILD_FIXTURE -eq 'move_fail' -and $LiteralPath -like '*-new-*') { throw 'injected publication failure' }
    Microsoft.PowerShell.Management\Move-Item -LiteralPath $LiteralPath -Destination $Destination
}
& (Join-Path $PSScriptRoot 'scripts/build_web.ps1')
exit $LASTEXITCODE
''', encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if not any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "COOKIE"))}
        env["CATER_BUILD_FIXTURE"] = mode
        result = subprocess.run([SHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
            cwd=root, env=env, capture_output=True, timeout=40)
        self.assertEqual((development / "index.html").read_text(encoding="utf-8"), "DEVELOPMENT")
        return root, package, result

    def test_success_replaces_only_package_and_cleans_previous_and_staging(self):
        root, package, result = self.fixture("success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((package / "index.html").read_text(encoding="utf-8"), "NEW")
        self.assertEqual(list((root / "cater/static").glob("workroom-*")), [])
        self.assertEqual(list((root / ".cater/web-build").iterdir()), [])

    def test_failed_build_preserves_existing_package(self):
        _, package, result = self.fixture("build_fail")
        self.assertEqual(result.returncode, 42)
        self.assertEqual((package / "index.html").read_text(encoding="utf-8"), "OLD")

    def test_missing_entry_preserves_existing_package(self):
        _, package, result = self.fixture("no_entry")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((package / "index.html").read_text(encoding="utf-8"), "OLD")

    def test_failed_copy_never_moves_existing_package(self):
        _, package, result = self.fixture("copy_fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((package / "index.html").read_text(encoding="utf-8"), "OLD")

    def test_failed_publication_restores_previous_package(self):
        _, package, result = self.fixture("move_fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((package / "index.html").read_text(encoding="utf-8"), "OLD")


if __name__ == "__main__":
    unittest.main()
