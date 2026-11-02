"""Scoped browser process cleanup and profile checks; no process-name-wide kills."""
import os
import time


def driver_pid(playwright):
    try:
        return int(playwright._impl_obj._connection._transport._proc.pid)
    except (AttributeError, TypeError, ValueError):
        return None


def _windows_api():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k.GetSystemTimeAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.CreateFileW.restype = wintypes.HANDLE
    return ctypes, wintypes, k


class OwnedBrowserProcesses:
    """Retain handles to this driver's browser descendants, protecting against PID reuse."""
    def __init__(self):
        self.handles = {}

    def capture(self, playwright):
        root = driver_pid(playwright)
        if os.name != 'nt' or not root:
            return
        ctypes, w, k = _windows_api()
        class Entry(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                        ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                        ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                        ('exe', w.WCHAR * 260)]
        k.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        k.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        observed = w.FILETIME()
        k.GetSystemTimeAsFileTime(ctypes.byref(observed))
        observed_at = (observed.dwHighDateTime << 32) | observed.dwLowDateTime
        snapshot = k.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        rows, item = [], Entry()
        item.size = ctypes.sizeof(item)
        try:
            ok = k.Process32FirstW(snapshot, ctypes.byref(item))
            while ok:
                rows.append((item.pid, item.parent, item.exe.lower()))
                ok = k.Process32NextW(snapshot, ctypes.byref(item))
        finally:
            k.CloseHandle(snapshot)
        descendants = {root}
        while True:
            new = {pid for pid, parent, _ in rows if parent in descendants}
            if new <= descendants:
                break
            descendants |= new
        for pid, _, name in rows:
            if pid in descendants and name in ('camoufox.exe', 'firefox.exe', 'chrome.exe', 'msedge.exe') and pid not in self.handles:
                # Include QUERY_LIMITED_INFORMATION to reject a PID reused between snapshot and OpenProcess.
                handle = k.OpenProcess(0x101001, False, pid)
                if handle:
                    created, exited, kernel, user = (w.FILETIME() for _ in range(4))
                    known = k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                              ctypes.byref(kernel), ctypes.byref(user))
                    born = (created.dwHighDateTime << 32) | created.dwLowDateTime
                    if known and born <= observed_at:
                        self.handles[pid] = handle
                    else:
                        k.CloseHandle(handle)

    def finish(self, grace=5):
        """Wait for normal exit, then stop only retained browser processes. Return killed/remaining PIDs."""
        if not self.handles:
            return [], []
        _, _, k = _windows_api()
        deadline = time.monotonic() + grace
        killed, remaining = [], []
        for pid, handle in self.handles.items():
            if k.WaitForSingleObject(handle, max(0, int((deadline - time.monotonic()) * 1000))) != 0:
                if k.TerminateProcess(handle, 1):
                    killed.append(pid)
        for pid, handle in self.handles.items():
            if k.WaitForSingleObject(handle, 2000) != 0:
                remaining.append(pid)
            k.CloseHandle(handle)
        self.handles.clear()
        return killed, remaining


def profile_is_free(profile, camoufox):
    """Check an existing lock without writing/deleting it. A leftover unlocked file is harmless."""
    lock = profile / ('parent.lock' if camoufox else 'lockfile')
    if not lock.exists():
        return True
    if os.name == 'nt':
        ctypes, _, k = _windows_api()
        handle = k.CreateFileW(str(lock), 0x80000000, 0, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            err = ctypes.get_last_error()
            if err in (2, 3):
                return True
            if err in (5, 32, 33):
                return False
            raise ctypes.WinError(err)
        k.CloseHandle(handle)
        return True
    import fcntl
    try:
        with lock.open('rb') as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(file, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
