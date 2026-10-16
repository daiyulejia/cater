"""Build and verify Cater from a clean working-tree snapshot, without network."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cater.artifact_store import _directories, _inspect, _open
from cater.verification import run_check
from cater.web_assets import MANIFEST_NAME, capture_web_assets
from scripts.release_validation import validate_wheel

MAX_BYTES = 64 * 1024 * 1024
MAX_FILES = 2048


class ReleaseError(ValueError):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    files: dict[str, bytes]
    package: dict[str, bytes]
    name: str
    version: str
    ui_version: str
    fingerprint: str


def _read(path: Path) -> bytes:
    before = _inspect(path, False)
    if before.st_size > 16 * 1024 * 1024:
        raise ReleaseError(f"Source file exceeds limit: {path.name}")
    with os.fdopen(_open(path, os.O_RDONLY), "rb") as stream:
        body = stream.read(before.st_size + 1)
        after = os.fstat(stream.fileno())
    latest = _inspect(path, False)
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if len(body) != before.st_size or signature(before) != signature(after) or signature(after) != signature(latest):
        raise ReleaseError(f"Source changed while reading: {path.name}")
    return body


def _pattern(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 256 or "\\" in value or ":" in value
            or value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ReleaseError("Package data patterns must stay within cater/")
    return value


def source_snapshot(root: Path) -> SourceSnapshot:
    root = root.absolute()
    _directories(root)
    files = {"pyproject.toml": _read(root / "pyproject.toml")}
    config = tomllib.loads(files["pyproject.toml"].decode("utf-8"))
    project, backend = config.get("project", {}), config.get("build-system", {})
    if not isinstance(project, dict) or not isinstance(backend, dict):
        raise ReleaseError("Project and build-system metadata must be tables")
    version = project.get("version")
    if (project.get("name") != "cater" or not isinstance(version, str)
            or not re.fullmatch(r"[0-9][0-9A-Za-z.!+_-]{0,79}", version)
            or project.get("dependencies") != [] or project.get("requires-python") != ">=3.11"
            or project.get("license") != "MIT" or project.get("license-files") != ["LICENSE"]
            or project.get("readme") != "README.md" or project.get("scripts") != {"cater": "cater.cli:main"}
            or project.get("dynamic")):
        raise ReleaseError("Release requires Cater's static metadata, MIT license, CLI and zero runtime dependencies")
    if backend.get("build-backend") != "setuptools.build_meta" or set(backend) - {"requires", "build-backend"}:
        raise ReleaseError("Only the declared setuptools backend is supported")
    tool = config.get("tool", {})
    setuptools = tool.get("setuptools", {}) if isinstance(tool, dict) else None
    if (not isinstance(setuptools, dict) or set(setuptools) - {"packages", "package-data"}
            or setuptools.get("packages") != {"find": {"include": ["cater*"]}}
            or not isinstance(setuptools.get("package-data"), dict)
            or set(setuptools.get("package-data", {})) != {"cater"}):
        raise ReleaseError("Package layout changed; update and review release inventory before building")
    patterns = setuptools["package-data"]["cater"]
    if not isinstance(patterns, list) or not patterns or len(patterns) > 32:
        raise ReleaseError("Package data must be an explicit bounded list")
    package_root = root / "cater"
    _directories(package_root)
    selected = set(path for path in package_root.rglob("*.py") if "__pycache__" not in path.parts)
    for pattern in patterns:
        for path in package_root.glob(_pattern(pattern)):
            info = _inspect(path, stat.S_ISDIR(path.lstat().st_mode))
            if stat.S_ISREG(info.st_mode):
                selected.add(path)
    if not selected or len(selected) > MAX_FILES:
        raise ReleaseError("Package source file count is invalid")
    package, size = {}, len(files["pyproject.toml"])
    for path in sorted(selected):
        info = _inspect(path, False)
        if size + info.st_size > MAX_BYTES:
            raise ReleaseError("Release source exceeds byte limit")
        body = _read(path)
        package[path.relative_to(root).as_posix()] = body
        size += len(body)
    files.update(package)
    files.update({name: _read(root / name) for name in ("README.md", "LICENSE")})
    if sum(map(len, files.values())) > MAX_BYTES:
        raise ReleaseError("Release source exceeds byte limit")
    ui = package_root / "static/workroom"
    manifest_name = "cater/static/workroom/" + MANIFEST_NAME
    if manifest_name not in package:
        raise ReleaseError("Build the workroom first: its manifest is required")
    assets = capture_web_assets(ui)
    if _read(ui / MANIFEST_NAME) != package[manifest_name]:
        raise ReleaseError("Workroom manifest changed while capturing source")
    manifest = json.loads(package[manifest_name])
    registered = {"/index.html"} | ({"/favicon.svg"} if "/favicon.svg" in assets.files else set())
    for chunk in manifest.values():
        registered.add("/" + chunk["file"])
        for field in ("css", "assets"):
            registered.update("/" + name for name in chunk.get(field, []))
    packaged = {"/" + name.removeprefix("cater/static/workroom/") for name in package
                if name.startswith("cater/static/workroom/") and name != manifest_name}
    if set(assets.files) != registered or packaged != registered:
        raise ReleaseError("Workroom contains missing or unregistered files; rebuild instead of shipping stale assets")
    if any(package["cater/static/workroom" + name] != body for name, body in assets.files.items()):
        raise ReleaseError("Workroom changed while capturing source")
    digest = hashlib.sha256()
    for name, body in sorted(files.items()):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big") + encoded + len(body).to_bytes(8, "big") + body)
    return SourceSnapshot(files, package, "cater", version, assets.version, digest.hexdigest())


def build_environment() -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
               "HOME", "USERPROFILE", "LOCALAPPDATA", "LANG", "LC_ALL", "SOURCE_DATE_EPOCH"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    # pip --isolated still loads system/site config unless this is explicit.
    environment["PIP_CONFIG_FILE"] = os.devnull
    return environment


def _step(label: str, command: list[str], *, cwd: Path, output: Path, timeout: float):
    result = run_check(command, cwd=cwd, timeout=timeout, record_output=True, env=build_environment())
    (output / f"{label}.json").write_text(json.dumps(result.payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.payload["outcome"] != "passed":
        raise ReleaseError(f"{label} failed; see {output / (label + '.json')}. Prepare the declared build tools; no automatic download was attempted.")
    return result


def build_release(root: Path, output: Path, *, timeout: float = 120) -> dict:
    root, output = root.absolute(), output.absolute()
    if any(part == ".." for part in (*root.parts, *output.parts)):
        raise ReleaseError("Source and output paths must not contain parent traversal")
    if output == root or any(output.is_relative_to(root / name) for name in ("cater", "web", ".git", "scripts", "tests")):
        raise ReleaseError("Output must not replace source, package, frontend, tests or repository metadata")
    if output.exists():
        raise ReleaseError("Output already exists; choose a fresh directory (old evidence is never overwritten)")
    snapshot = source_snapshot(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    _directories(output.parent)
    output.mkdir()
    report = {"status": "building", "name": snapshot.name, "version": snapshot.version,
              "source_sha256": snapshot.fingerprint, "ui_version": snapshot.ui_version,
              "package_files": len(snapshot.package), "runtime_dependencies": []}
    report_path = output / "release.json"
    try:
        stage, wheels, install, smoke = (output / name for name in ("source", "wheels", "installed", "smoke"))
        for directory in (stage, wheels, install, smoke):
            directory.mkdir()
        for name, body in snapshot.files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        python = [sys.executable, "-I", "-B"]
        tools = _step("toolchain", [*python, "-c", "import importlib.metadata as m,json,platform; print(json.dumps({'python':platform.python_version(),'pip':m.version('pip'),'setuptools':m.version('setuptools')}))"],
                      cwd=stage, output=output, timeout=timeout)
        report["toolchain"] = json.loads(tools.stdout_preview)
        _step("build", [*python, "-m", "pip", "--isolated", "wheel", "--no-index", "--no-deps",
                         "--no-build-isolation", "--disable-pip-version-check", str(stage), "--wheel-dir", str(wheels)],
              cwd=stage, output=output, timeout=timeout)
        candidates = list(wheels.glob("*.whl"))
        if len(candidates) != 1:
            raise ReleaseError("Build must produce exactly one wheel")
        wheel = candidates[0]
        checked = validate_wheel(wheel, snapshot.package, name=snapshot.name, version=snapshot.version,
                                 license_bytes=snapshot.files["LICENSE"])
        expected = {**checked, "ui_version": snapshot.ui_version}
        expected_path = output / "expected.json"
        expected_path.write_text(json.dumps(expected, indent=2), encoding="utf-8")
        _step("install", [*python, "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", "--no-compile",
                           "--disable-pip-version-check", "--target", str(install), str(wheel)],
              cwd=smoke, output=output, timeout=timeout)
        _step("smoke", [sys.executable, "-I", "-S", "-B", str(ROOT / "scripts/check_installed.py"),
                         str(install), str(expected_path), str(smoke / "installed-check.json")], cwd=smoke, output=output, timeout=timeout)
        smoke_report = json.loads((smoke / "installed-check.json").read_text(encoding="utf-8"))
        if smoke_report.get("status") != "verified" or smoke_report.get("ui_version") != snapshot.ui_version:
            raise ReleaseError("Installed verification did not confirm this exact build")
        if source_snapshot(root).fingerprint != snapshot.fingerprint:
            raise ReleaseError("Source changed during build; snapshot preserved, but release was not accepted")
        report.update(status="verified", wheel=wheel.relative_to(output).as_posix(),
                      wheel_sha256=checked["wheel_sha256"], checks=smoke_report["checks"],
                      python=smoke_report["python"])
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="从干净快照构建并安装验证 Cater；不下载依赖、不访问模型、不覆盖旧产物。")
    parser.add_argument("--source", type=Path, default=ROOT, help="Cater 源码目录，默认本仓库")
    parser.add_argument("--output", type=Path, help="必须尚不存在的产物目录；默认 dist/releases 下新建独立目录")
    parser.add_argument("--timeout", type=float, default=120, help="每个受控构建/安装/验证进程的超时秒数，最多600")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 600:
        parser.error("timeout must be between 0 and 600 seconds")
    output = args.output or args.source / "dist/releases" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    try:
        report = build_release(args.source, output, timeout=args.timeout)
    except (ValueError, OSError) as exc:
        print(f"构建验证未通过：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({**report, "output": str(output.absolute())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
