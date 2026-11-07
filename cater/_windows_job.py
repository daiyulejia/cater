"""Windows process lifetime management using the standard-library ctypes API."""

from __future__ import annotations

import ctypes
import time


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("active_processes", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t), ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io", _IOCounters),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class WindowsJob:
    def __init__(self, pid: int):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel = kernel
        self._handle = None
        handle = ctypes.c_void_p
        kernel.CreateJobObjectW.argtypes = [handle, ctypes.c_wchar_p]
        kernel.CreateJobObjectW.restype = handle
        kernel.SetInformationJobObject.argtypes = [handle, ctypes.c_int, handle, ctypes.c_uint32]
        kernel.SetInformationJobObject.restype = ctypes.c_int
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = handle
        kernel.AssignProcessToJobObject.argtypes = [handle, handle]
        kernel.AssignProcessToJobObject.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [handle]
        kernel.CloseHandle.restype = ctypes.c_int

        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not kernel.AssignProcessToJobObject(self._handle, process_handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                kernel.CloseHandle(process_handle)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._handle is not None:
            if not self._kernel.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None

    def close_observed(self, timeout: float = 2.0) -> dict:
        """Terminate, then wait for captured members; never prove the whole tree.

        The Job remains kill-on-close until the final handle is closed. A PID
        snapshot can miss processes and is not an authorization to replay work.
        """
        from ctypes import wintypes
        kernel = self._kernel
        handle_type = ctypes.c_void_p
        kernel.QueryInformationJobObject.argtypes = [handle_type, ctypes.c_int, handle_type, ctypes.c_uint32, handle_type]
        kernel.QueryInformationJobObject.restype = ctypes.c_int
        kernel.IsProcessInJob.argtypes = [handle_type, handle_type, ctypes.POINTER(wintypes.BOOL)]
        kernel.IsProcessInJob.restype = ctypes.c_int
        kernel.TerminateJobObject.argtypes = [handle_type, ctypes.c_uint32]
        kernel.TerminateJobObject.restype = ctypes.c_int
        kernel.WaitForSingleObject.argtypes = [handle_type, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        observation = {"version": 1, "termination_requested": False, "snapshot": "unconfirmed",
                       "observed_members": 0, "wait": "unconfirmed", "job_closed": False}
        handles = []
        deadline = time.monotonic() + timeout
        failed = False
        try:
            try:
                if not self._handle:
                    raise OSError("Job is unavailable")
                capacity = 8
                while capacity <= 4096:
                    class PidList(ctypes.Structure):
                        _fields_ = [("assigned", ctypes.c_uint32), ("count", ctypes.c_uint32),
                                    ("ids", ctypes.c_size_t * capacity)]
                    values = PidList()
                    ok = kernel.QueryInformationJobObject(self._handle, 3, ctypes.byref(values), ctypes.sizeof(values), None)
                    if ok and values.count == values.assigned and values.count <= capacity:
                        break
                    if not ok and ctypes.get_last_error() not in (24, 122, 234):
                        raise OSError("Job snapshot failed")
                    capacity *= 2
                else:
                    raise OSError("Job snapshot too large")
                pids = list(values.ids[:values.count])
                if len(set(pids)) != len(pids) or any(not pid for pid in pids):
                    raise OSError("Invalid Job snapshot")
                for pid in pids:
                    if time.monotonic() >= deadline:
                        raise OSError("Job snapshot deadline elapsed")
                    member = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
                    if not member:
                        raise OSError("Job member unavailable")
                    handles.append(member)
                    belongs = wintypes.BOOL()
                    if not kernel.IsProcessInJob(member, self._handle, ctypes.byref(belongs)) or not belongs.value:
                        raise OSError("Job member is unconfirmed")
                observation["snapshot"] = "captured"
                observation["observed_members"] = len(handles)
            except OSError:
                failed = True
            if self._handle and kernel.TerminateJobObject(self._handle, 1):
                observation["termination_requested"] = True
            else:
                failed = True
            if not failed:
                for member in handles:
                    remaining = max(0, int((deadline - time.monotonic()) * 1000))
                    if kernel.WaitForSingleObject(member, remaining) != 0:
                        failed = True
                        break
                if not failed:
                    observation["wait"] = "observed_exited"
        finally:
            for member in handles:
                if not kernel.CloseHandle(member):
                    failed = True
            try:
                self.close()
                observation["job_closed"] = True
            except OSError:
                failed = True
        observation["status"] = "unconfirmed" if failed else "snapshot_observed"
        return observation
