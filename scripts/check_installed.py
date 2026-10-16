"""Installed-only smoke check. Invoked by build_release with python -I -S."""

from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
from http.server import ThreadingHTTPServer
import importlib.metadata
import io
import json
from pathlib import Path, PurePosixPath
import platform
import sys
import threading
from urllib.request import ProxyHandler, build_opener


def _smoke_opener():
    return build_opener(ProxyHandler({}))


def check_installed(installed: Path, expected: dict, output: Path) -> dict:
    if not sys.flags.isolated or not sys.flags.no_site:
        raise ValueError("Installed checks require python -I -S")
    if output.exists():
        raise ValueError("Refusing to overwrite an existing installed-check report")
    installed = installed.resolve(strict=True)
    if Path.cwd().resolve().is_relative_to(installed):
        raise ValueError("Run installed checks outside the installation directory")
    files = expected["files"]
    actual = {path.relative_to(installed).as_posix() for path in (installed / "cater").rglob("*") if path.is_file()}
    if not files or actual != set(files):
        raise ValueError("Installed package file set differs from the selected wheel")
    for name, digest in files.items():
        parts = PurePosixPath(name).parts
        if not name.startswith("cater/") or "\\" in name or ":" in name or ".." in parts:
            raise ValueError("Invalid expected package path")
        path = installed.joinpath(*parts)
        if not path.resolve(strict=True).is_relative_to(installed) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Installed bytes differ: " + name)
    sys.path.insert(0, str(installed))
    import cater
    from cater.cli import main
    from cater.runtime import Runtime
    from cater.web import _workroom_dist, make_handler
    from cater.web_assets import capture_web_assets

    if not Path(cater.__file__).resolve().is_relative_to(installed):
        raise ValueError("Imported source instead of the installed distribution")
    distribution = importlib.metadata.distribution("cater")
    if distribution.version != expected["version"] or distribution.requires:
        raise ValueError("Installed metadata or runtime dependencies do not match")
    if cater.__version__ != expected["version"]:
        raise ValueError("Runtime version and distribution version disagree")
    entries = {entry.name: entry for entry in distribution.entry_points if entry.group == "console_scripts"}
    if set(entries) != {"cater"} or entries["cater"].load() is not main:
        raise ValueError("Installed CLI entry does not resolve to Cater")
    cli_output = io.StringIO()
    with redirect_stdout(cli_output):
        try:
            main(["--version"])
        except SystemExit as exc:
            if exc.code != 0:
                raise ValueError("Installed CLI did not complete") from None
        else:
            raise ValueError("Installed CLI version did not use the normal entry")
    if cli_output.getvalue().strip() != "cater " + expected["version"]:
        raise ValueError("Installed CLI reported another version")
    asset_root = Path(cater.__file__).parent / "static/workroom"
    if _workroom_dist() != asset_root:
        raise ValueError("Workroom resolution did not select the installed package")
    assets = capture_web_assets(asset_root)
    if assets.version != expected["ui_version"]:
        raise ValueError("Installed UI does not match the selected build")

    class NoModels:
        calls = 0

        def complete(self, **kwargs):
            type(self).calls += 1
            raise AssertionError("Installed smoke must not call a model")

        tool_turn = complete

    db = output.with_suffix(".db")
    if db.exists():
        raise ValueError("Refusing to reuse a smoke database")

    def runtime():
        result = Runtime(db)
        result.drivers = {name: NoModels() for name in result.drivers}
        return result

    with runtime() as rt:
        task_id = rt.create_task("安装包本地验证，不调用模型")
        before = rt.log.read()[-1].hash
        assert rt.verify_chain().ok
    opener = _smoke_opener()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, static_dir=asset_root, runtime_factory=runtime))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for name, body in assets.files.items():
            path = "/" if name == "/index.html" else name
            with opener.open(base + path, timeout=5) as response:
                if response.status != 200 or response.read() != body or response.headers.get("X-Cater-UI-Version") != assets.version:
                    raise ValueError("Installed HTTP resource does not match: " + name)
        with opener.open(base + "/api/chain", timeout=5) as response:
            chain = json.load(response)
            if chain.get("ok") is not True or chain.get("head_hash") != before:
                raise ValueError("Installed event chain changed or failed verification")
        with opener.open(base + "/api/tasks/" + task_id, timeout=5) as response:
            detail = json.load(response)
            if detail["task"]["id"] != task_id:
                raise ValueError("Installed task API did not return this smoke task")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise ValueError("Installed HTTP check did not stop")
    with runtime() as rt:
        if not rt.verify_chain().ok or rt.log.read()[-1].hash != before or NoModels.calls:
            raise ValueError("Smoke mutated the task or attempted a model invocation")
    report = {"status": "verified", "python": platform.python_version(), "ui_version": assets.version,
              "package_files": len(expected["files"]), "http_resources": len(assets.files), "model_calls": 0,
              "checks": ["installed_import", "zero_runtime_dependencies", "cli_entry", "package_bytes",
                         "installed_ui", "http_resources", "task_api", "event_chain", "no_model_calls"]}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: python -I -S check_installed.py INSTALLED EXPECTED_JSON NEW_REPORT")
    result = check_installed(Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")), Path(sys.argv[3]))
    print(json.dumps(result, ensure_ascii=False))
