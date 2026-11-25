"""Own the process tree used by the public Kanban snapshot transport."""

from contextlib import contextmanager
import os
import signal
import subprocess
import time


_REAP_TIMEOUT = 2


def snapshot_exit_status(child, timeout):
    """Observe exit without freeing the POSIX group leader PID before cleanup."""
    if os.name == 'nt':
        return child.wait(timeout=timeout)
    end = time.monotonic() + timeout
    while True:
        status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        if status is not None and status.si_pid:
            return status.si_status
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(child.args, timeout)
        time.sleep(min(.01, remaining))


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

    class _BasicLimit(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                    ('PerJobUserTimeLimit', ctypes.c_longlong),
                    ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _BasicLimit), ('IoInfo', _IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _ntdll = ctypes.WinDLL('ntdll')
    _ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    _ntdll.NtResumeProcess.restype = ctypes.c_long


def _reap(child):
    try:
        child.wait(timeout=_REAP_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise OSError('snapshot process did not exit after cleanup') from exc


@contextmanager
def snapshot_process(argv):
    """Yield an owned binary child; the caller owns its stdout after yield."""
    child = None
    handed_off = False
    job = None
    assigned = False
    try:
        if os.name == 'nt':
            job = _kernel32.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0,
                                     creationflags=0x00000004)  # CREATE_SUSPENDED
            if not _kernel32.AssignProcessToJobObject(job, int(child._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            assigned = True
            status = _ntdll.NtResumeProcess(int(child._handle))
            if status < 0:
                raise OSError(f'NtResumeProcess failed with NTSTATUS 0x{status & 0xffffffff:08x}')
        else:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
        handed_off = True
        yield child
    finally:
        try:
            if os.name == 'nt':
                if job:
                    if not _kernel32.CloseHandle(job):
                        error = ctypes.WinError(ctypes.get_last_error())
                        # A failed close must not leave a live owned tree behind.
                        _kernel32.TerminateJobObject(job, 1)
                        _kernel32.CloseHandle(job)
                    else:
                        error = None
                    job = None
                else:
                    error = None
                if child is not None and not assigned and child.poll() is None:
                    child.kill()
                if error is not None:
                    raise error
            elif child is not None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            try:
                if child is not None:
                    _reap(child)
            finally:
                if child is not None and not handed_off:
                    child.stdout.close()
