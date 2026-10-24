"""Offline process-crash harness; imports only the explicitly selected package."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


TOOL = "workspace_restore_project_file"
BODY = bytes(range(256))
STAGES = ("before_link", "after_link", "after_install_before_receipt",
          "after_receipt_before_run_finished")


def denied(*args, **kwargs):
    raise AssertionError("offline harness forbids network and external processes")


@contextmanager
def offline(root=None):
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {}, clear=True))
        stack.enter_context(patch.object(socket, "socket", denied))
        stack.enter_context(patch.object(socket, "create_connection", denied))
        stack.enter_context(patch.object(subprocess, "Popen", denied))
        if root is not None:
            temporary = root / "os-temp"
            temporary.mkdir(exist_ok=True)
            stack.enter_context(patch.object(tempfile, "tempdir", str(temporary)))
        yield


class Driver:
    name = "crash_fake"

    def __init__(self):
        self.steps = []
        self.calls = []

    def tool_turn(self, *, role, prompt, history, tools):
        from cater.tool_protocol import ToolCall, ToolTurn

        self.calls.append({"context": json.loads(prompt.rsplit("\n", 1)[1]),
                           "history": copy.deepcopy(history), "tools": copy.deepcopy(tools)})
        calls = self.steps.pop(0) if self.steps else []
        parsed = [ToolCall(f"fixture-{len(self.calls)}-{i}", name, args)
                  for i, (name, args) in enumerate(calls)]
        return ToolTurn("", parsed, 0, {"token_usage": "reported"}, {
            "role": "assistant", "content": "", "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in parsed]})


def runtime(root, driver):
    from cater.runtime import Runtime

    return Runtime(root / "crash.db", roles_path=root / "roles.json",
                   workspaces_path=root / "workspaces.json", drivers={"crash_fake": driver})


def exchange(rt, driver, task, *calls):
    driver.steps = [list(calls), []]
    before = rt.log.read(task_id=task)[-1].seq
    role = rt.organization.leader
    rt.team.receive(task, f"@{role.name} offline fixture {before}", request_id=f"fixture-{task}-{before}")
    rt.collaborate(task)
    return [e.payload["result"] for e in rt.log.read(task_id=task)
            if e.seq > before and e.type == "team.tool_finished"]


def prepare(root, package_root):
    """Caller closes all guards and Runtime before launching the OS worker."""
    config = json.loads((package_root / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
    config["organization"]["leader"]["driver"] = "crash_fake"
    for role in config["roles"]:
        role["driver"] = "crash_fake"
    (root / "roles.json").write_text(json.dumps(config), encoding="utf-8")
    for name in ("source", "target"):
        (root / name).mkdir()
    (root / "source/binary.bin").write_bytes(BODY)
    (root / "workspaces.json").write_text(json.dumps({"workspaces": [
        {"id": name, "title": name, "root": str(root / name),
         "mode": "read_only" if name == "source" else "edit"}
        for name in ("source", "target")]}), encoding="utf-8")
    driver = Driver()
    with offline(root), runtime(root, driver) as rt:
        project = rt.rulebook.create_project("Offline crash fixture")["id"]
        tasks = [rt.create_task(name, project_id=project) for name in ("source", "target", "shared")]
        for task, workspace in zip(tasks, ("source", "target", "target")):
            rt.team.enable(task)
            rt.workspaces.bind(task, workspace, expected_revision=None)
        source, target, shared = tasks
        published, = exchange(rt, driver, source, ("publish_workspace_archive", {
            "filename": "fixture.zip", "summary": "Offline bytes", "paths": ["binary.bin"]}))
        assert published["status"] == "ok", published
        finished, = exchange(rt, driver, source, ("finish_work", {
            "summary": "Fixture", "verification": "Known fixture bytes",
            "artifact_seqs": [published["artifact_seq"]]}))
        assert finished["status"] == "ok", finished
        delivery = next(e for e in rt.log.read(task_id=source) if e.type == "team.objective_completed")
        args = {"as_of_seq": rt.team.project_history.index(target)["as_of_seq"],
                "delivery_seq": delivery.seq, "artifact_seq": published["artifact_seq"], "path": "binary.bin"}
        data = {"target": target, "shared": shared, "arguments": args, "package_root": str(package_root)}
    (root / "fixture.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def disk(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            st = path.stat()
            result[path.relative_to(root).as_posix()] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": st.st_size,
                "inode": st.st_ino, "device": st.st_dev, "nlink": st.st_nlink,
                "mtime_ns": st.st_mtime_ns, "mode": st.st_mode}
    return result


def worker(root, package_root, stage):
    # -I -S plus an explicit package root keeps installed runs out of the checkout.
    sys.path.insert(0, str(package_root))
    with offline(root):
        import cater
        from cater.workspace_tools import WorkspaceExecutor

        assert Path(cater.__file__).resolve().parent == package_root / "cater"
        assert not os.environ
        data = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
        driver = Driver()
        with runtime(root, driver) as rt, ExitStack() as stack:
            def stop():
                marker = {"pid": os.getpid(), "stage": stage, "package": str(Path(cater.__file__).resolve()),
                          "dont_write_bytecode": sys.dont_write_bytecode,
                          "environment_empty": not os.environ, "disk": disk(root / "target"),
                          "guard_checks": {}}
                for name, call in (("socket", socket.socket), ("connection", socket.create_connection),
                                   ("process", subprocess.Popen)):
                    try:
                        call()
                    except AssertionError:
                        marker["guard_checks"][name] = True
                    else:
                        raise AssertionError("offline guard missing")
                assert all(Path(module.__file__).resolve().is_relative_to(package_root / "cater")
                    for name, module in tuple(sys.modules.items())
                    if (name == "cater" or name.startswith("cater.")) and getattr(module, "__file__", None))
                with (root / "marker.json").open("x", encoding="utf-8") as stream:
                    json.dump(marker, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                while True:
                    time.sleep(0.05)

            original_link = os.link
            def link(*args, **kwargs):
                if stage == "before_link":
                    stop()
                result = original_link(*args, **kwargs)
                if stage == "after_link":
                    stop()
                return result

            original_install = WorkspaceExecutor.install_bytes
            def install(executor, *args, **kwargs):
                result = original_install(executor, *args, **kwargs)
                if stage == "after_install_before_receipt":
                    stop()
                return result

            original_emit = rt.log.emit
            def emit(**kwargs):
                result = original_emit(**kwargs)
                if (stage == "after_receipt_before_run_finished"
                        and kwargs.get("type") == "team.tool_finished"
                        and kwargs.get("payload", {}).get("name") == TOOL):
                    stop()
                return result

            stack.enter_context(patch("cater.workspace_tools.os.link", link))
            stack.enter_context(patch.object(WorkspaceExecutor, "install_bytes", install))
            stack.enter_context(patch.object(rt.log, "emit", emit))
            exchange(rt, driver, data["target"], (TOOL, data["arguments"]))
            raise AssertionError("worker returned without reaching marker")


if __name__ == "__main__":
    # Use the same driver class identity in both processes: Runtime fingerprints it.
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from support.restore_crash_worker import worker as run_worker

    run_worker(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3])
