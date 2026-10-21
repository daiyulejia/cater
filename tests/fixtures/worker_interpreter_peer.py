"""Local Python peers and original-handle ownership for worker interpreter tests."""

import ctypes
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from unittest.mock import patch


SCRIPT = Path(__file__).resolve()


def kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        ("GetCurrentProcess", [], W.HANDLE),
        ("DuplicateHandle", [W.HANDLE, W.HANDLE, W.HANDLE, ctypes.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD], W.BOOL),
        ("GetProcessTimes", [W.HANDLE, *([ctypes.POINTER(W.FILETIME)] * 4)], W.BOOL),
        ("QueryFullProcessImageNameW", [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)], W.BOOL),
        ("WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
        ("TerminateProcess", [W.HANDLE, W.UINT], W.BOOL),
        ("CloseHandle", [W.HANDLE], W.BOOL),
        ("IsProcessInJob", [W.HANDLE, W.HANDLE, ctypes.POINTER(W.BOOL)], W.BOOL),
        ("QueryInformationJobObject", [W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL),
    ):
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def identity(api, handle):
    times = [W.FILETIME() for _ in range(4)]
    checked(api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)))
    image = ctypes.create_unicode_buffer(32768)
    length = W.DWORD(len(image))
    checked(api.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)))
    return {"creation_filetime": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
            "image": str(Path(image.value).resolve())}


class ProcessHandles:
    def __init__(self):
        self.api = kernel()
        self.held = {}

    def member(self, handle, job):
        value = W.BOOL()
        checked(self.api.IsProcessInJob(handle, job._handle, ctypes.byref(value)))
        return bool(value.value)

    def pids(self, job):
        body = ctypes.create_string_buffer(65536)
        used = W.DWORD()
        checked(self.api.QueryInformationJobObject(job._handle, 3, body, len(body), ctypes.byref(used)))
        count = ctypes.c_uint32.from_buffer(body, 4).value
        return list((ctypes.c_size_t * count).from_buffer(body, 8))

    def capture(self, pid, handle=None):
        if pid in self.held:
            return self.held[pid]
        if handle is None:
            handle = checked(self.api.OpenProcess(0x1000 | 0x100000 | 1, False, pid))
        try:
            row = {"pid": pid, **identity(self.api, handle)}
        except BaseException:
            self.api.CloseHandle(handle)
            raise
        self.held[pid] = (handle, row)
        return handle, row

    def capture_popen(self, process):
        duplicate = W.HANDLE()
        current = self.api.GetCurrentProcess()
        checked(self.api.DuplicateHandle(current, int(process._handle), current,
                                         ctypes.byref(duplicate), 0, False, 2))
        return self.capture(process.pid, duplicate.value)

    def capture_job(self, job, *, exclude=()):
        for pid in self.pids(job):
            if pid in exclude or pid in self.held:
                continue
            try:
                handle, row = self.capture(pid)
            except OSError:
                if pid not in self.pids(job):
                    continue
                raise
            assert self.member(handle, job), "Process was not in the private fixture Job"

    def wait_all(self):
        for handle, row in self.held.values():
            assert self.api.WaitForSingleObject(handle, 5000) == 0, row
            row["original_handle_stopped"] = True

    def stop_and_close(self):
        forced, errors = [], []
        for handle, row in reversed(list(self.held.values())):
            try:
                if self.api.WaitForSingleObject(handle, 100) == 0x102:
                    # This is the same held handle whose image and FILETIME were
                    # verified on capture; an exited PID cannot retarget it.
                    if self.api.TerminateProcess(handle, 73):
                        forced.append(row["pid"])
                    else:
                        assert self.api.WaitForSingleObject(handle, 5000) == 0, row
                assert self.api.WaitForSingleObject(handle, 5000) == 0, row
                row["original_handle_stopped"] = True
            except BaseException:
                errors.append(traceback.format_exc())
            finally:
                checked(self.api.CloseHandle(handle))
        self.held.clear()
        assert not errors, errors
        return forced


def save(path, value):
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value), encoding="utf-8")
    pending.replace(path)


def own_identity():
    api = kernel()
    return {"pid": os.getpid(), "parent_pid": os.getppid(),
            **identity(api, api.GetCurrentProcess()), "argv": sys.argv,
            "python": sys.executable, "prefix": sys.prefix}


def wait_file(path, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError("Synthetic peer did not become ready: " + path.name)
        time.sleep(0.01)
    return json.loads(path.read_text(encoding="utf-8"))


def peer(mode, folder):
    save(folder / "target.json", own_identity())
    child = subprocess.Popen([sys.executable, "-I", "-B", str(SCRIPT), "leaf", str(folder)])
    try:
        wait_file(folder / "leaf.json")
        if mode == "check":
            time.sleep(45)
            return
        for line in sys.stdin.buffer:
            request = json.loads(line)
            if mode == "cli":
                reply = {"id": request["id"], "ok": True, "text": "fixture-ready", "tokens": 0}
            else:
                method = request.get("method")
                if method == "initialize":
                    result = {"protocolVersion": 1, "agentInfo": {"name": "local-worker-fixture"}}
                elif method == "session/new":
                    result = {"sessionId": "worker-interpreter-fixture"}
                elif method == "session/prompt":
                    result = {"stopReason": "end_turn"}
                else:
                    continue
                reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            sys.stdout.buffer.write(json.dumps(reply).encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()
    finally:
        # Normal test teardown uses the already-owned Job. This fallback keeps a
        # failed standalone fixture bounded without discovering any PID.
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def run_case(entry, folder, package_root):
    sys.path.insert(0, str(package_root))
    import cater
    from cater._windows_job import WindowsJob
    from cater.acp import AcpSession
    from cater.drivers import CliDriver
    from cater.processes import ProcessScope, worker_python
    from cater.verification import run_check
    from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy

    safety = WindowsJob(os.getpid())
    handles = ProcessHandles()
    mode = entry if entry in {"acp", "cli"} else "check"
    command = [sys.executable, "-I", "-B", str(SCRIPT), mode, str(folder), "--literal", "space in argument"]
    report = {"errors": [], "case_python": sys.executable, "base_python": worker_python(),
              "package_file": str(Path(cater.__file__).resolve()), "expected_argv": command[3:]}
    owner = None
    scopes = []

    class ObservedScope:
        def __init__(self, process):
            handle, row = handles.capture_popen(process)
            report["bootstrap_image"] = row["image"]
            assert Path(row["image"]) == Path(worker_python())
            time.sleep(0.5)
            report["before_assign_descendants"] = [
                pid for pid in handles.pids(safety) if pid not in {os.getpid(), process.pid}]
            assert not report["before_assign_descendants"], report
            assert not (folder / "target.json").exists(), "Configured target started before attachment"
            self.scope = ProcessScope(process)
            self.kind = self.scope.kind
            self.closed = False
            scopes.append(self)

        def close(self):
            if self.closed:
                return
            target = wait_file(folder / "target.json")
            leaf = wait_file(folder / "leaf.json")
            handles.capture_job(safety, exclude=(os.getpid(),))
            for peer_identity in (target, leaf):
                handle, row = handles.capture(peer_identity["pid"])
                assert all(row[key] == peer_identity[key] for key in ("creation_filetime", "image"))
                assert handles.member(handle, self.scope.job), row
            report.update(target_argv=target["argv"], target_python=target["python"], target_prefix=target["prefix"])
            report["all_target_job_members"] = all(
                handles.member(handle, self.scope.job) for handle, row in handles.held.values())
            assert report["all_target_job_members"], report
            self.scope.close()
            handles.wait_all()
            self.closed = True

    names = {"acp": "cater.acp.ProcessScope", "cli": "cater.drivers.ProcessScope",
             "workspace": "cater.workspace_tools.ProcessScope", "verification": "cater.verification._ProcessScope"}
    try:
        assert sys.prefix != sys.base_prefix, "Case must run under the newly-created real venv"
        with patch(names[entry], ObservedScope):
            if entry == "acp":
                owner = AcpSession(command, cwd=folder, timeout=15)
                owner.open()
                report["result"] = owner.prompt("synthetic protocol only").stop_reason
                owner.close()
                report["pipes_complete"] = owner._cleanup_done and not owner._reader.is_alive()
            elif entry == "cli":
                owner = CliDriver("synthetic-peer", command, cwd=folder, timeout=15, restart_limit=0)
                report["result"] = owner.complete(role="fixture", prompt="synthetic protocol only", context={}).text
                owner.close()
                report["pipes_complete"] = owner._proc is None and owner._io_thread is None
            else:
                if entry == "verification":
                    with patch("cater.verification._git_state", return_value={"head": None, "dirty": None}):
                        result = run_check(command, cwd=folder, timeout=8).payload
                    assert result["command"] == command
                else:
                    executor = WorkspaceExecutor(WorkspacePolicy.from_dict({
                        "root": str(folder), "mode": "execute", "commands": {"fixture": command}, "timeout": 8,
                    }))
                    result = executor.execute("workspace_run", {"command": "fixture"})
                    assert list(executor.policy.commands["fixture"]) == command
                report["result"] = result["outcome"]
                report["pipes_complete"] = result["stdout"]["complete"] and result["stderr"]["complete"]
                assert result["process_scope"] == "windows_job"
            assert len(scopes) == 1
            assert scopes[0].closed
    except BaseException:
        report["errors"].append(traceback.format_exc())
    finally:
        handles.capture_job(safety, exclude=(os.getpid(),))
        report["processes"] = [row for handle, row in handles.held.values()]
        report["forced_terminations"] = handles.stop_and_close()
        if owner is not None:
            try:
                owner.close()
            except BaseException:
                report["errors"].append(traceback.format_exc())
        report["remaining_fixture_pids"] = [pid for pid in handles.pids(safety) if pid != os.getpid()]
        report["all_original_handles_stopped"] = bool(report["processes"]) and all(
            row.get("original_handle_stopped") for row in report["processes"])
        save(folder / "report.json", report)
    assert not report["errors"], report
    assert not report["forced_terminations"], report
    assert not report["remaining_fixture_pids"], report


def main():
    mode = sys.argv[1]
    if mode == "guard":
        assert sys.stdin.buffer.readline() == b"start-private-fixture\n"
        python, entry, folder, package_root = sys.argv[2:]
        child = subprocess.Popen([python, "-I", "-B", str(SCRIPT), "case", entry, folder, package_root])
        raise SystemExit(child.wait(timeout=50))
    if mode == "case":
        run_case(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
    elif mode == "leaf":
        save(Path(sys.argv[2]) / "leaf.json", own_identity())
        time.sleep(45)
    elif mode in {"acp", "cli", "check"}:
        peer(mode, Path(sys.argv[2]))
    else:
        raise ValueError("Unknown synthetic fixture mode")


if __name__ == "__main__":
    main()
