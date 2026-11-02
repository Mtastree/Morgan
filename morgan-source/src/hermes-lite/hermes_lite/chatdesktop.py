"""Desktop launcher and lifecycle manager for hermes-lite local chat.

Features:
- pywebview + Windows WebView2 (Edge Chromium).
- Idempotent 5-state lifecycle state machine (not_started, initializing, ready, failed, shutting_down, exited).
- Single instance enforcement via Windows Named Mutex with 64-bit Win32 window activation.
- Strict service identity verification (project root, expected store path, model alias, context size).
- Resource ownership tracking (desktop instance, chat service, model service).
- Safe reuse of existing instances, or managed startup with early ownership registration.
- Graceful shutdown: bounded cancellation of in-flight generations and background tasks,
  thread-safe DB closing, and stopping only services owned by this desktop instance.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Optional
import urllib.error
import urllib.request
import webbrowser

import webview

# Optional .NET references for Windows Forms / Screen bounds
try:
    import clr
    clr.AddReference('System.Windows.Forms')
    clr.AddReference('System.Drawing')
    from System.Windows.Forms import Screen
except Exception:  # noqa: BLE001
    Screen = None

LITE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = LITE_DIR.parents[1]
# Round 43: `configs/deployment.json` is the DEPLOYMENT-level file (endpoint,
# runtime executable, port). Which MODEL runs is answered by the profile
# registry (`modelprofiles`), never by this file alone.
CONFIG_PATH = PROJECT_ROOT / 'configs' / 'deployment.json'
LOG_DIR = PROJECT_ROOT / 'logs'
STATE_PATH = LOG_DIR / 'desktop_state.json'
WINDOW_CONFIG_PATH = LOG_DIR / 'desktop_window.json'
STATIC_DIR = Path(__file__).resolve().parent / 'chat_static'
ICON_PATH = STATIC_DIR / 'chat.ico'

# Round 40: how long the close path waits for a web search already on the wire.
#
# Measured, not guessed. `on_closing` runs from pywebview's `closing` event,
# i.e. BEFORE the window disappears, so anything blocking here freezes the
# window. A 12s drain was tried and measured: the window took 13.1s to close.
# That is unacceptable, so the drain is capped at 1s: it only catches searches
# that are already finishing.
#
# The process still waits for an in-flight search after the window is gone,
# because concurrent.futures joins its (non-daemon) workers in `_python_exit`.
# In production that wait is bounded by the search layer's own 10s budget; it is
# reported in the close timeline rather than hidden.
SEARCH_DRAIN_TIMEOUT_S = 1.0

# Add scripts directory to path to reuse battle-tested local_model helpers
SCRIPTS_DIR = PROJECT_ROOT / 'scripts'
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import local_model  # type: ignore
except ImportError:
    local_model = None

# chatstore is already a dependency of this process (it opens the chat database);
# importing it at module level lets the close path timestamp its own timeline
# without a local import inside the handler.
from hermes_lite import chatparams, chatstore, modelprofiles  # noqa: E402

# Win32 Constants
SW_RESTORE = 9
SW_SHOW = 5
SW_MINIMIZE = 6
CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102

k32 = ctypes.windll.kernel32
u32 = ctypes.windll.user32

# Explicit 64-bit clean Win32 function signatures
k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
k32.CreateMutexW.restype = wintypes.HANDLE
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.WaitForSingleObject.restype = wintypes.DWORD
k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
k32.ReleaseMutex.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL
k32.GetCurrentThreadId.argtypes = []
k32.GetCurrentThreadId.restype = wintypes.DWORD

# Win32 Job Object API for robust process lifecycle containment
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

class IO_COUNTERS(ctypes.Structure):
    _fields_ = [('ReadOperationCount', ctypes.c_uint64),
                ('WriteOperationCount', ctypes.c_uint64),
                ('OtherOperationCount', ctypes.c_uint64),
                ('ReadTransferCount', ctypes.c_uint64),
                ('WriteTransferCount', ctypes.c_uint64),
                ('OtherTransferCount', ctypes.c_uint64)]

class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD)]

class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t)]

k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
k32.CreateJobObjectW.restype = wintypes.HANDLE
k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
k32.SetInformationJobObject.restype = wintypes.BOOL
k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
k32.AssignProcessToJobObject.restype = wintypes.BOOL
k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
k32.IsProcessInJob.restype = wintypes.BOOL
k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
k32.CreateEventW.restype = wintypes.HANDLE
k32.SetEvent.argtypes = [wintypes.HANDLE]
k32.SetEvent.restype = wintypes.BOOL

def create_kill_on_close_job(fail_limit_hook: bool = False) -> Optional[int]:
    """Create a Windows Job Object configured to terminate all assigned processes on handle close."""
    job = None
    try:
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        if fail_limit_hook:
            k32.CloseHandle(job)
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        res = k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))
        if not res:
            k32.CloseHandle(job)
            return None
        return int(job)
    except Exception:
        if job:
            try:
                k32.CloseHandle(job)
            except Exception:
                pass
        return None

def assign_handle_to_job(job_handle: Optional[int], proc_handle: Optional[int]) -> bool:
    """Assign an open process handle to a Windows Job Object."""
    if not job_handle or not proc_handle:
        return False
    try:
        return bool(k32.AssignProcessToJobObject(wintypes.HANDLE(job_handle), wintypes.HANDLE(proc_handle)))
    except Exception:
        return False

def assign_pid_to_job(job_handle: Optional[int], pid: Optional[int]) -> bool:
    """Assign a process by PID to a Windows Job Object."""
    if not job_handle or not pid:
        return False
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    try:
        h_proc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h_proc:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(wintypes.HANDLE(job_handle), h_proc))
        finally:
            k32.CloseHandle(h_proc)
    except Exception:
        return False

def is_pid_in_job(job_handle: Optional[int], pid: Optional[int]) -> bool:
    """Check if process with PID belongs to the specified Job Object."""
    if not job_handle or not pid:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        h_proc = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h_proc:
            return False
        try:
            in_job = wintypes.BOOL(False)
            if k32.IsProcessInJob(h_proc, wintypes.HANDLE(job_handle), ctypes.byref(in_job)):
                return bool(in_job.value)
            return False
        finally:
            k32.CloseHandle(h_proc)
    except Exception:
        return False

u32.IsWindow.argtypes = [wintypes.HWND]
u32.IsWindow.restype = wintypes.BOOL
u32.IsIconic.argtypes = [wintypes.HWND]
u32.IsIconic.restype = wintypes.BOOL
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.IsWindowVisible.restype = wintypes.BOOL
u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.ShowWindow.restype = wintypes.BOOL
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.SetForegroundWindow.restype = wintypes.BOOL
u32.SetFocus.argtypes = [wintypes.HWND]
u32.SetFocus.restype = wintypes.HWND
u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
u32.AttachThreadInput.restype = wintypes.BOOL
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.GetWindowThreadProcessId.restype = wintypes.DWORD
u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
u32.FindWindowW.restype = wintypes.HWND
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowTextW.restype = ctypes.c_int
u32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
u32.EnumWindows.restype = wintypes.BOOL


# --------------------------------------------------------------------------- #
# Win32 & Single Instance Utilities
# --------------------------------------------------------------------------- #
def get_mutex_name(project_path: Optional[Path] = None) -> str:
    p = str((project_path or PROJECT_ROOT).resolve()).lower()
    h = hashlib.sha256(p.encode('utf-8')).hexdigest()[:16]
    return f"Local\\Qwen25_LocalChat_Mutex_{h}"


def acquire_single_instance_mutex(project_path: Optional[Path] = None) -> tuple[bool, Optional[wintypes.HANDLE]]:
    """Acquire single-instance named mutex.
    Returns: (is_primary, mutex_handle)
    - If is_primary is True, this process is the legitimate single instance.
    - If False, another live instance holds the mutex; caller MUST exit immediately.
    """
    mutex_name = get_mutex_name(project_path)
    handle = k32.CreateMutexW(None, True, mutex_name)
    if not handle:
        return False, None
    last_err = k32.GetLastError()
    if last_err == ERROR_ALREADY_EXISTS:
        # Mutex exists! Check if actively held by a live instance or free/abandoned.
        wait_res = k32.WaitForSingleObject(handle, 0)
        if wait_res == WAIT_OBJECT_0:
            # Unowned mutex, ownership acquired by this call
            return True, handle
        elif wait_res == WAIT_ABANDONED:
            # Previous instance terminated abnormally, OS transferred ownership
            return True, handle
        elif wait_res == WAIT_TIMEOUT:
            # Actively held by live process; caller must NOT start!
            k32.CloseHandle(handle)
            return False, None
        else:
            k32.CloseHandle(handle)
            return False, None
    # Newly created and initially owned
    return True, handle


def process_identity(pid: Optional[int]) -> Optional[dict[str, Any]]:
    if not pid:
        return None
    if local_model and hasattr(local_model, 'process_identity'):
        return local_model.process_identity(pid)
    # Fallback ctypes implementation
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    handle = k.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = wintypes.DWORD(len(buf))
        if not k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(n)):
            return None
        times = [ctypes.c_ulonglong() for _ in range(4)]
        if not k.GetProcessTimes(handle, *[ctypes.byref(x) for x in times]):
            return None
        return {'exe': buf.value, 'creation_time': times[0].value}
    finally:
        k.CloseHandle(handle)


def terminate_process_with_identity(pid: int, expected_ident: Optional[dict[str, Any]], timeout_s: float = 3.0) -> tuple[bool, str]:
    """Safely terminate process only if identity matches recorded identity."""
    if local_model and hasattr(local_model, 'terminate_process_with_identity'):
        return local_model.terminate_process_with_identity(pid, expected_ident, timeout_s=timeout_s)
    if not pid or not expected_ident:
        return False, "Missing pid or expected_ident; refusing to terminate unverified process."
    curr = process_identity(pid)
    if curr is None:
        return True, "Process already stopped."
    if curr != expected_ident:
        return False, f"Process identity mismatch (expected {expected_ident}, got {curr}); refusing to terminate."
    try:
        subprocess.run(['taskkill', '/PID', str(pid)], capture_output=True, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass
    began = time.perf_counter()
    while time.perf_counter() - began < min(1.5, timeout_s):
        if process_identity(pid) is None:
            return True, "Process terminated gracefully."
        time.sleep(0.1)
    recheck = process_identity(pid)
    if recheck and recheck == expected_ident:
        subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True, creationflags=CREATE_NO_WINDOW)
        while time.perf_counter() - began < timeout_s:
            if process_identity(pid) is None:
                return True, "Process force-terminated."
            time.sleep(0.1)
        if process_identity(pid) is None:
            return True, "Process terminated."
        return False, "Process failed to terminate within timeout."
    return True, "Process terminated."


def verify_process_identity(ident: Any) -> bool:
    """Verify that an identity dictionary contains valid non-empty fields."""
    if not isinstance(ident, dict):
        return False
    exe = ident.get('exe')
    ctime = ident.get('creation_time')
    if not exe or not isinstance(exe, str):
        return False
    if not isinstance(ctime, int) or ctime <= 0:
        return False
    return True


def verify_handshake_payload(h_data: Any, expected_token: Optional[str],
                             allowed_events: tuple[str, ...]) -> tuple[bool, Optional[dict[str, Any]], str]:
    """Strictly verify handshake message structure, token, PID, and live process identity.
    Returns: (is_valid, verified_payload, error_reason)
    """
    if not isinstance(h_data, dict):
        return False, None, "握手载荷非合法字典结构"

    evt = h_data.get('event')
    if evt not in allowed_events:
        return False, None, f"握手事件 '{evt}' 不在允许事件集合 {allowed_events} 中"

    msg_token = h_data.get('token')
    if expected_token and msg_token != expected_token:
        return False, None, f"握手凭证 Token 不匹配 (预期: {expected_token}, 收到: {msg_token})"

    pid = h_data.get('pid')
    if not isinstance(pid, int) or pid <= 0:
        return False, None, f"无效的进程 PID: {pid}"

    msg_ident = h_data.get('identity')
    if not verify_process_identity(msg_ident):
        return False, None, f"握手消息中包含不完整或缺失的进程身份: {msg_ident}"

    # Query live OS process identity
    live_ident = process_identity(pid)
    if not live_ident:
        return False, None, f"进程 PID {pid} 不存在或已退出，无法核对身份"

    if not verify_process_identity(live_ident):
        return False, None, f"查询到的实时进程身份不完整: {live_ident}"

    # Compare paths (case-insensitive on Windows) and creation_time
    try:
        exe_live = Path(live_ident['exe']).resolve()
        exe_msg = Path(msg_ident['exe']).resolve()
        same_exe = (str(exe_live).lower() == str(exe_msg).lower())
    except Exception:
        same_exe = (str(live_ident['exe']).lower() == str(msg_ident['exe']).lower())

    if not same_exe or live_ident['creation_time'] != msg_ident['creation_time']:
        return False, None, f"握手身份与实时进程身份不匹配 (消息: {msg_ident}, 实际: {live_ident})"

    return True, h_data, ""


def listener_pid(port: int) -> Optional[int]:
    if local_model and hasattr(local_model, 'listener_pid'):
        return local_model.listener_pid(port)
    r = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                       text=True, encoding='utf-8', errors='replace',
                       creationflags=CREATE_NO_WINDOW)
    for line in (r.stdout or '').splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == 'LISTENING' and parts[1].endswith(f':{port}'):
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def port_free(host: str, port: int) -> bool:
    if local_model and hasattr(local_model, 'port_free'):
        return local_model.port_free(host, port)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        res = s.connect_ex((host, port))
        return res != 0
    finally:
        s.close()


def activate_window_hwnd(hwnd: int) -> bool:
    """Restore an existing window and bring it to foreground (64-bit clean)."""
    if not hwnd or not u32.IsWindow(hwnd):
        return False
    if hasattr(u32, 'IsIconic') and u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, SW_RESTORE)
    else:
        u32.ShowWindow(hwnd, SW_SHOW)
    cur_tid = k32.GetCurrentThreadId()
    target_pid = wintypes.DWORD()
    target_tid = u32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
    if cur_tid != target_tid:
        u32.AttachThreadInput(cur_tid, target_tid, True)
        u32.SetForegroundWindow(hwnd)
        u32.SetFocus(hwnd)
        u32.AttachThreadInput(cur_tid, target_tid, False)
    else:
        u32.SetForegroundWindow(hwnd)
        u32.SetFocus(hwnd)
    return True


def find_window_for_pid(target_pid: int) -> Optional[int]:
    """Find top-level window belonging to target_pid."""
    found_hwnd = None
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def enum_cb(hwnd, lparam):
        nonlocal found_hwnd
        if u32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == target_pid:
                buf = ctypes.create_unicode_buffer(512)
                u32.GetWindowTextW(hwnd, buf, 512)
                if 'Morgan' in buf.value or '本地聊天' in buf.value:
                    found_hwnd = hwnd
                    return False  # stop
        return True

    cb = WNDENUMPROC(enum_cb)
    u32.EnumWindows(cb, 0)
    return found_hwnd


def try_activate_existing_instance() -> tuple[bool, bool]:
    """Check if another verified instance is active.
    Returns: (is_running, window_activated)
    """
    if STATE_PATH.is_file():
        try:
            st = json.loads(STATE_PATH.read_text(encoding='utf-8'))
            pid = st.get('desktop', {}).get('pid')
            ident = process_identity(pid) if pid else None
            if ident and ident == st.get('desktop', {}).get('identity'):
                hwnd = st.get('desktop', {}).get('hwnd')
                if hwnd and activate_window_hwnd(hwnd):
                    return True, True
                found = find_window_for_pid(pid)
                if found and activate_window_hwnd(found):
                    return True, True
                # Process is alive even if window activation did not succeed
                return True, False
        except Exception:  # noqa: BLE001
            pass

    # Fallback search by title
    hwnd_title = u32.FindWindowW(None, 'Morgan') or u32.FindWindowW(None, '本地聊天')
    if hwnd_title:
        target_pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd_title, ctypes.byref(target_pid))
        ident = process_identity(target_pid.value) if target_pid.value else None
        if ident:
            act = activate_window_hwnd(hwnd_title)
            return True, act

    return False, False


# --------------------------------------------------------------------------- #
# Window Bounds & Multi-Monitor Protection
# --------------------------------------------------------------------------- #
def load_window_config() -> dict[str, Any]:
    if WINDOW_CONFIG_PATH.is_file():
        try:
            return json.loads(WINDOW_CONFIG_PATH.read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_window_config(x: int, y: int, width: int, height: int, maximized: bool = False) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        data = {'x': x, 'y': y, 'width': width, 'height': height, 'maximized': maximized}
        WINDOW_CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                      encoding='utf-8')
    except Exception:  # noqa: BLE001
        pass


def get_safe_initial_bounds() -> tuple[int, int, int, int, bool]:
    """Calculate window position & size, guaranteeing it is inside connected monitors."""
    default_w, default_h = 1100, 780
    saved = load_window_config()
    x = saved.get('x')
    y = saved.get('y')
    w = max(700, saved.get('width', default_w))
    h = max(480, saved.get('height', default_h))
    maximized = bool(saved.get('maximized', False))

    if Screen is not None and Screen.AllScreens and len(Screen.AllScreens) > 0:
        if x is not None and y is not None:
            for screen in Screen.AllScreens:
                wa = screen.WorkingArea
                if (wa.Left <= x + 100 and x <= wa.Right - 100 and
                    wa.Top <= y and y <= wa.Bottom - 60):
                    return x, y, w, h, maximized
        primary = Screen.PrimaryScreen.WorkingArea
        cx = primary.Left + (primary.Width - default_w) // 2
        cy = primary.Top + (primary.Height - default_h) // 2
        return cx, cy, default_w, default_h, maximized

    return 100, 100, default_w, default_h, maximized


# --------------------------------------------------------------------------- #
# Loading Page Template
# --------------------------------------------------------------------------- #
LOADING_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Morgan</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      user-select: none;
      -webkit-user-select: none;
      padding: 24px;
    }
    .card {
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      padding: 32px 36px;
      width: 100%;
      max-width: 520px;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4), 0 8px 10px -6px rgba(0, 0, 0, 0.3);
    }
    .header {
      display: flex;
      align-items: center;
      gap: 14px;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid #334155;
    }
    .header-icon {
      width: 36px;
      height: 36px;
      background: #2563eb;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: bold;
      font-size: 18px;
      color: white;
    }
    .header h1 {
      font-size: 20px;
      font-weight: 600;
      color: #f8fafc;
    }
    .header p {
      font-size: 13px;
      color: #94a3b8;
      margin-top: 2px;
    }
    .steps {
      display: flex;
      flex-direction: column;
      gap: 14px;
      margin-bottom: 20px;
    }
    .step {
      display: flex;
      align-items: flex-start;
      gap: 12px;
      font-size: 14px;
      color: #94a3b8;
      transition: all 0.2s ease;
    }
    .step.active {
      color: #38bdf8;
      font-weight: 500;
    }
    .step.done {
      color: #4ade80;
    }
    .step.error {
      color: #f87171;
    }
    .step-indicator {
      width: 20px;
      height: 20px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 12px;
      background: #334155;
      color: #94a3b8;
      flex-shrink: 0;
      margin-top: 1px;
    }
    .step.active .step-indicator {
      background: #0284c7;
      color: white;
      box-shadow: 0 0 10px #0284c7;
    }
    .step.done .step-indicator {
      background: #16a34a;
      color: white;
    }
    .step.error .step-indicator {
      background: #dc2626;
      color: white;
    }
    .step-body {
      flex: 1;
    }
    .step-detail {
      font-size: 12px;
      color: #64748b;
      margin-top: 2px;
    }
    .step.active .step-detail {
      color: #7dd3fc;
    }
    .step.error .step-detail {
      color: #fca5a5;
    }
    .error-box {
      display: none;
      background: #450a0a;
      border: 1px solid #991b1b;
      border-radius: 8px;
      padding: 16px;
      margin-top: 16px;
    }
    .error-title {
      font-weight: 600;
      color: #fecaca;
      font-size: 14px;
      margin-bottom: 6px;
    }
    .error-msg {
      font-size: 13px;
      color: #fca5a5;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .actions {
      display: none;
      gap: 10px;
      margin-top: 20px;
      justify-content: flex-end;
    }
    button {
      padding: 8px 16px;
      border-radius: 6px;
      font-size: 13px;
      cursor: pointer;
      font-weight: 500;
      border: 1px solid transparent;
      transition: all 0.15s ease;
    }
    .btn-retry {
      background: #2563eb;
      color: white;
    }
    .btn-retry:hover { background: #1d4ed8; }
    .btn-exit {
      background: #334155;
      color: #e2e8f0;
      border-color: #475569;
    }
    .btn-exit:hover { background: #475569; }
    .spinner {
      display: inline-block;
      width: 12px;
      height: 12px;
      border: 2px solid rgba(255,255,255,0.3);
      border-top-color: white;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="header-icon">💬</div>
      <div>
        <h1>Morgan</h1>
        <p>Qwen2.5-1.5B · 日常聊天与个性化记忆</p>
      </div>
    </div>
    <div class="steps">
      <div id="step-0" class="step">
        <div class="step-indicator" id="ind-0">1</div>
        <div class="step-body">
          <div class="step-title">检查运行环境与 WebView2</div>
          <div class="step-detail" id="det-0">等待执行...</div>
        </div>
      </div>
      <div id="step-1" class="step">
        <div class="step-indicator" id="ind-1">2</div>
        <div class="step-body">
          <div class="step-title">检查模型服务状态</div>
          <div class="step-detail" id="det-1">等待执行...</div>
        </div>
      </div>
      <div id="step-2" class="step">
        <div class="step-indicator" id="ind-2">3</div>
        <div class="step-body">
          <div class="step-title">加载模型并等待就绪</div>
          <div class="step-detail" id="det-2">等待执行...</div>
        </div>
      </div>
      <div id="step-3" class="step">
        <div class="step-indicator" id="ind-3">4</div>
        <div class="step-body">
          <div class="step-title">启动 Morgan 服务</div>
          <div class="step-detail" id="det-3">等待执行...</div>
        </div>
      </div>
      <div id="step-4" class="step">
        <div class="step-indicator" id="ind-4">5</div>
        <div class="step-body">
          <div class="step-title">准备进入聊天</div>
          <div class="step-detail" id="det-4">等待执行...</div>
        </div>
      </div>
    </div>
    <div id="error-box" class="error-box">
      <div class="error-title" id="error-title">启动遇到问题</div>
      <div class="error-msg" id="error-msg"></div>
    </div>
    <div id="actions" class="actions">
      <button class="btn-exit" onclick="window.pywebview.api.exit_app()">退出应用</button>
      <button class="btn-retry" onclick="window.pywebview.api.retry_init()">重新尝试</button>
    </div>
  </div>
  <script>
    function updateStep(idx, state, detail) {
      const el = document.getElementById('step-' + idx);
      const ind = document.getElementById('ind-' + idx);
      const det = document.getElementById('det-' + idx);
      if (!el) return;
      el.className = 'step ' + state;
      if (state === 'active') {
        ind.innerHTML = '<span class="spinner"></span>';
      } else if (state === 'done') {
        ind.textContent = '✓';
      } else if (state === 'error') {
        ind.textContent = '✕';
      } else {
        ind.textContent = (idx + 1);
      }
      if (detail) det.textContent = detail;
    }
    function showError(title, msg) {
      document.getElementById('error-title').textContent = title || '启动遇到问题';
      document.getElementById('error-msg').textContent = msg || '';
      document.getElementById('error-box').style.display = 'block';
      document.getElementById('actions').style.display = 'flex';
    }
    function clearError() {
      document.getElementById('error-box').style.display = 'none';
      document.getElementById('actions').style.display = 'none';
    }
  </script>
</body>
</html>
"""


class DesktopJsApi:
    """Dedicated minimal JS API object to avoid cyclic reflection in pywebview."""
    def __init__(self, ctrl: DesktopController):
        self._ctrl = ctrl

    def exit_app(self) -> None:
        self._ctrl.exit_app()

    def retry_init(self) -> None:
        self._ctrl.retry_init()

    def choose_model_file(self) -> dict:
        """Native file picker for a local GGUF path (round 43).

        A browser `<input type=file>` must NOT be used here: the browser hands
        back a fake path (`C:\\fakepath\\...`) that the model loader cannot open.
        The path has to come from the desktop shell, so it does.
        """
        return self._ctrl.choose_model_file()


# --------------------------------------------------------------------------- #
# Desktop Controller
# --------------------------------------------------------------------------- #
class DesktopController:
    """Manages window, loading sequence, service lifecycle and clean shutdown."""

    def __init__(self, port: int = 8771, model_port: Optional[int] = None,
                 expected_store_path: Optional[Path] = None,
                 model_config_path: Optional[Path] = None,
                 launch_profile_override: Optional[str] = None):
        self.port = port
        self.model_port = model_port
        self.expected_store_path = (expected_store_path or
                                    (PROJECT_ROOT / 'src' / 'hermes-lite' / 'chatdata' / 'chat.sqlite3')).resolve()
        self.model_config_path: Optional[Path] = Path(model_config_path).resolve() if model_config_path else None
        self.launch_profile_override: Optional[str] = (
            str(launch_profile_override).strip() if launch_profile_override else None
        )
        self.window: Optional[webview.Window] = None
        self.hwnd: Optional[int] = None
        self.mutex_handle: Optional[wintypes.HANDLE] = None

        # State machine
        self.init_state = 'not_started'  # not_started, initializing, ready, failed, shutting_down, exited
        self.init_attempt_id = 0
        self._state_lock = threading.Lock()

        # Ownership tracking
        self.model_service: dict[str, Any] = {
            'pid': None, 'owned': False, 'port': None, 'base_url': None, 'status': 'unknown',
            'identity': None, 'token': None}
        self.chat_service: dict[str, Any] = {
            'pid': None, 'owned': False, 'port': port, 'url': f'http://127.0.0.1:{port}/',
            'server': None}
        self.model_launcher_proc: Optional[subprocess.Popen] = None
        self.last_spawned_launcher: Optional[dict[str, Any]] = None
        self.last_abort_stage: Optional[str] = None
        self.model_handshake_file: Optional[Path] = None
        self.model_launch_token: Optional[str] = None
        self.model_job: Optional[int] = None
        self.launcher_stdout_lines: list[str] = []
        self.launcher_stderr_lines: list[str] = []

        self.shutting_down = False
        self.init_cancelled = False
        self._init_thread: Optional[threading.Thread] = None

        # Model reload & status tracking
        self.model_status: str = 'unknown'  # ready, stopping, loading, restoring, unavailable
        self.actual_window: Optional[int] = None
        self.target_window: Optional[int] = None
        self.last_reload_error: Optional[str] = None
        self._instance_seq: int = 1
        self._model_reload_lock = threading.Lock()
        self._switch_state_lock = threading.RLock()
        self._is_reloading: bool = False
        self.is_managed_owned: bool = False
        # Round 43: the alias is no longer a Qwen literal. It comes from the
        # profile that is actually loaded; until one is, it is explicitly empty
        # so the page cannot display a model name that is not running.
        self.model_alias: str = ''
        self._runtime_instance: Optional[Any] = None

        # ---- Round 43: model profile identity ---------------------------- #
        # These four are DIFFERENT and must never be collapsed into one field:
        #   launch_profile_id    what a cold start loads (registry default)
        #   target_profile_id    what the user asked to load (in flight)
        #   current_profile_id   what is ACTUALLY running (verified)
        #   last_success_profile_id  last profile that loaded and verified
        self.launch_profile_id: Optional[str] = None
        self.launch_profile_source: Optional[str] = None
        self.target_profile_id: Optional[str] = None
        self._rollback_profile_id: Optional[str] = None
        self.current_profile_id: Optional[str] = None
        self.current_profile: Optional[dict] = None
        #: The full config dict the running service was launched with. It is the
        #: ONLY source for "what is running", so an edit to a profile can never
        #: retroactively change the description of a live instance.
        self._current_runtime_config: Optional[dict] = None
        #: What THIS app passed to the launcher for the live instance. Used as
        #: file-consistency evidence ONLY when the service does not report
        #: `model_path` itself, and labelled as such in the verification result.
        self._launch_evidence: Optional[dict] = None
        #: Effective window override applied by a round-41 style window reload
        #: (a runtime change, not a profile edit).
        self._window_override: Optional[int] = None
        self.last_switch: Optional[dict] = None
        self.registry_error: Optional[dict] = None


    def _is_managed_owned_model(self) -> bool:
        """True if model was started and managed by this desktop (even if currently unavailable)."""
        return bool(self.is_managed_owned or self.model_service.get('owned'))

    def _sync_runtime_instance(self) -> Any:
        """Create and publish the authoritative RuntimeInstance descriptor to the ChatService."""
        from hermes_lite.chatweb import RuntimeInstance
        base = self.model_service.get('base_url') or f"http://127.0.0.1:{self.model_port or 18085}/v1"
        # Round 43: no Qwen fallback. An instance that does not know its alias
        # reports an empty one, and the page says "未加载" instead of a name.
        #
        # The profile fields are read defensively: this method is also reached
        # from partially-initialized controllers (a retry before any profile was
        # resolved, or a test acting on the real class), and publishing an
        # instance must never be the thing that crashes the app.
        alias = self.model_alias or ''
        inst = RuntimeInstance(
            base_url=base.rstrip('/'),
            model_alias=alias,
            actual_window=self.actual_window,
            actual_window_source='service-probe' if self.actual_window else 'unknown',
            owned=self._is_managed_owned_model(),
            pid=self.model_service.get('pid'),
            identity=self.model_service.get('identity'),
            instance_seq=self._instance_seq,
            status=self.model_status,
            error=self.last_reload_error,
            profile_id=getattr(self, 'current_profile_id', None),
            profile_display_name=(getattr(self, 'current_profile', None) or {}).get('display_name'),
            target_profile_id=getattr(self, 'target_profile_id', None),
        )
        self._runtime_instance = inst
        svc = self.chat_service.get('service')
        if svc and hasattr(svc, 'set_runtime_instance'):
            svc.set_runtime_instance(inst)
        return inst

    def get_runtime_instance(self) -> Any:
        if self._runtime_instance is None:
            return self._sync_runtime_instance()
        return self._runtime_instance

    # ----------------------------------------------------------------------- #
    # Round 43: profile resolution
    # ----------------------------------------------------------------------- #
    def _resolve_launch_profile(self) -> dict:
        """Which profile a cold start should load, and the config for it.

        Round 44: uses unified resolve_startup_target.
        Order: explicit `--model-config` (tests/compat) > registry default >
        last verified success > any usable profile > raw deployment file.
        Explicit targets fail loudly if invalid (never quietly start another model).
        A corrupt registry is recorded and reported, NEVER replaced by an empty
        list, and boot falls back to the deployment file so the app still starts.
        """
        self.registry_error = None
        self.launch_fallback_reason: Optional[str] = None
        target = modelprofiles.resolve_startup_target(
            explicit_config_path=self.model_config_path,
            explicit_profile_id=getattr(self, 'launch_profile_override', None),
        )
        self.launch_profile_id = target.get('profile_id')
        self.launch_profile_source = target.get('source')
        self.launch_fallback_reason = target.get('fallback_reason')
        self.registry_error = target.get('registry_error')
        return target['effective_config']

    def _config_for_profile(self, profile_id: Optional[str]) -> dict:
        """The full launch config for one profile (or the deployment default)."""
        return modelprofiles.effective_deployment(profile_id, strict=True)

    def _capture_active_model_config(self) -> dict:
        """Snapshot of the RUNNING model configuration, before any switch.

        Round 43: this is `_current_runtime_config` -- the exact dict the live
        service was launched with -- not a re-read of a file. Re-reading would
        silently substitute a profile the user just edited for the one actually
        running, which is how a rollback ends up restoring the wrong model.
        """
        if self._current_runtime_config:
            return dict(self._current_runtime_config)
        cfg = self._resolve_launch_profile()
        cfg = dict(cfg)
        port = self.model_port or int(self.model_service.get('port') or cfg.get('port', 18085))
        host = cfg.get('host', '127.0.0.1')
        cfg['port'] = port
        cfg['host'] = host
        cfg['base_url'] = f"http://{host}:{port}/v1"
        cfg['context_size'] = self.actual_window or cfg.get('context_size', 4096)
        return cfg

    def _write_runtime_config(self, cfg: dict, tag: str = 'model') -> Path:
        """Persist a fully-resolved launch config for `local_model.py --config`.

        The launcher receives STRUCTURED values in a file, never a concatenated
        shell string, so a path with spaces or Chinese characters cannot break
        argument parsing.
        """
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        port = cfg.get('port', 18085)
        path = LOG_DIR / f'runtime_{tag}_config_{port}.json'
        host = cfg.get('host', '127.0.0.1')
        payload = dict(cfg)
        payload['port'] = int(port)
        payload['host'] = host
        payload['base_url'] = f"http://{host}:{port}/v1"
        modelprofiles.atomic_write_json(path, payload)
        return path

    def _commit_current_profile(self, profile: Optional[dict], cfg: dict) -> None:
        """Commit profile identity AFTER a verified load. Never before."""
        if profile:
            pid = profile.get('profile_id')
            # 1. Pre-commit validation: confirm profile is still present in registry and valid
            try:
                reg = modelprofiles.load_registry()
                cur = modelprofiles.get_profile(reg, pid)
                if cur is None:
                    raise RuntimeError(f"目标配置「{pid}」已不存在于注册表中，提交被拒绝。")
            except modelprofiles.ProfileError as exc:
                raise RuntimeError(f"注册表校验失败，无法提交配置「{pid}」：{exc.message}") from exc

            # 2. Critical persistence: set_last_success_profile must not fail silently
            try:
                modelprofiles.set_last_success_profile(pid)
            except Exception as exc:
                raise RuntimeError(f"持久化最近成功配置「{pid}」失败：{exc}") from exc

            # Round 45: Running instance identity is bound to the verified launch snapshot,
            # never re-read from a mutable registry that may have been edited during load.
            self.current_profile = dict(profile)
            self.current_profile_id = pid
            self.model_alias = cfg.get('alias') or profile.get('serving_alias') or ''
        else:
            self.current_profile = None
            self.current_profile_id = None
            self.model_alias = cfg.get('alias') or ''

        self._current_runtime_config = dict(cfg)
        self._sync_runtime_instance()

    # -- model profile management API (page) -------------------------------- #
    def list_profiles(self) -> dict:
        with self._switch_state_lock:
            cur_id = self.current_profile_id
            cur_prof = dict(self.current_profile) if self.current_profile else {}
            tgt_id = self.target_profile_id
            m_status = self.model_status
            is_rel = self._is_reloading
        try:
            reg = modelprofiles.ensure_initialized()
            self.registry_error = None
        except modelprofiles.ProfileError as exc:
            self.registry_error = exc.to_dict()
            return {'ok': False, **exc.to_dict(),
                    'profiles': [], 'registry': str(modelprofiles.registry_path())}
        views = [modelprofiles.profile_public_view(
            p, current_id=cur_id,
            default_id=reg.get('default_profile_id'),
            last_success_id=reg.get('last_success_profile_id'))
            for p in reg['profiles']]
        return {
            'ok': True,
            'profiles': views,
            'count': len(views),
            'default_profile_id': reg.get('default_profile_id'),
            'last_success_profile_id': reg.get('last_success_profile_id'),
            'current_profile_id': cur_id,
            'current_profile_display_name': cur_prof.get('display_name'),
            'target_profile_id': tgt_id,
            'launch_profile_id': self.launch_profile_id,
            'launch_profile_source': self.launch_profile_source,
            'model_status': m_status,
            'reloading': is_rel or m_status in
                         ('stopping', 'loading', 'restoring'),
            'registry': str(modelprofiles.registry_path()),
            'deployment': str(modelprofiles.deployment_path()),
        }

    def profile_detail(self, profile_id: str) -> dict:
        try:
            reg = modelprofiles.ensure_initialized()
            prof = modelprofiles.require_profile(reg, profile_id)
        except modelprofiles.ProfileError as exc:
            return exc.to_dict()
        return {'ok': True,
                'profile': modelprofiles.profile_public_view(
                    prof, current_id=self.current_profile_id,
                    default_id=reg.get('default_profile_id'),
                    last_success_id=reg.get('last_success_profile_id'),
                    check_file=True)}

    def create_profile(self, data: dict) -> dict:
        try:
            prof = modelprofiles.create_profile(data or {})
        except modelprofiles.ProfileError as exc:
            return exc.to_dict()
        view = modelprofiles.profile_public_view(prof, check_file=True)
        return {'ok': True, 'profile': view,
                'message': f'已添加配置「{prof.get("display_name")}」。'
                           '这不会改变当前正在运行的模型。'}

    def update_profile(self, profile_id: str, data: dict) -> dict:
        with self._switch_state_lock:
            try:
                prof = modelprofiles.update_profile(profile_id, data or {})
            except modelprofiles.ProfileError as exc:
                return exc.to_dict()
            # An edit affects FUTURE loads only. If the edited profile is currently
            # running, target, or rollback dependency, the active process keeps the config it was launched with.
            is_running = (profile_id == self.current_profile_id)
            is_target = bool(self.target_profile_id and profile_id == self.target_profile_id)
            is_rollback = bool(getattr(self, '_rollback_profile_id', None) and profile_id == self._rollback_profile_id)

            note = None
            if is_running:
                note = ('该配置正在运行，本次修改不影响当前已加载的模型；'
                        '需要重新选择并加载后才会按新参数启动。')
            elif is_target:
                note = ('该配置正在作为目标加载切换中，本次修改不影响正在进行的加载；'
                        '下次重新选择并加载后才会按新参数启动。')
            elif is_rollback:
                note = ('该配置当前被切换流程作为回滚备份依赖，本次修改不影响当前切换。')

            view = modelprofiles.profile_public_view(
                prof, current_id=self.current_profile_id, check_file=True)
            return {'ok': True, 'profile': view, 'message': note,
                    'applies_to_running': False}

    def delete_profile(self, profile_id: str,
                       new_default_profile_id: Optional[str] = None) -> dict:
        with self._switch_state_lock:
            if profile_id == self.current_profile_id:
                return {'ok': False, 'error': 'cannot_delete_running_profile',
                        'message': '该配置当前正在运行，不能直接删除。请先切换到其他配置，或关闭模型服务后再删除。',
                        'profile_id': profile_id}
            if self._is_reloading or self.target_profile_id:
                if profile_id == self.target_profile_id:
                    return {'ok': False, 'error': 'cannot_delete_target_profile',
                            'message': '该配置正在作为目标加载切换中，不能直接删除。请等待切换完成或取消后再操作。',
                            'profile_id': profile_id}
                if getattr(self, '_rollback_profile_id', None) and profile_id == self._rollback_profile_id:
                    return {'ok': False, 'error': 'cannot_delete_protected_profile',
                            'message': '该配置正在切换流程中被依赖（如回滚备份），不能直接删除。',
                            'profile_id': profile_id}
            protected = set()
            if self.current_profile_id:
                protected.add(self.current_profile_id)
            if self.target_profile_id:
                protected.add(self.target_profile_id)
            if getattr(self, '_rollback_profile_id', None):
                protected.add(self._rollback_profile_id)
            try:
                res = modelprofiles.delete_profile(
                    profile_id, new_default_profile_id=new_default_profile_id,
                    current_profile_id=self.current_profile_id,
                    target_profile_id=self.target_profile_id,
                    protected_profile_ids=protected)
            except modelprofiles.ProfileError as exc:
                return exc.to_dict()
            return res

    def set_default_profile(self, profile_id: str) -> dict:
        try:
            res = modelprofiles.set_default_profile(profile_id)
        except modelprofiles.ProfileError as exc:
            return exc.to_dict()
        return res

    def validate_profile_draft(self, data: dict) -> dict:
        """Validate without saving, so the page can show field errors first."""
        try:
            fields = modelprofiles.validate_profile_fields(data or {})
        except modelprofiles.ProfileError as exc:
            return exc.to_dict()
        path = Path(fields['model_path'])
        info = modelprofiles.describe_model_file(path)
        warnings = []
        if not info['looks_like_gguf']:
            warnings.append('该路径下未找到 GGUF 文件（或不是 GGUF 格式），加载会失败。')
        return {'ok': True, 'fields': fields, 'file': info, 'warnings': warnings}

    def choose_model_file(self) -> dict:
        """Open the OS file picker for a model file.

        Returns {'ok': True, 'path': ...} or {'ok': False, 'cancelled': True}.
        The dialog is modal to the app window, and it starts in the project's
        `models` directory when that exists.
        """
        if not self.window:
            return {'ok': False, 'message': '窗口尚未就绪，无法打开文件选择器。'}
        start_dir = PROJECT_ROOT / 'models'
        try:
            import webview  # local import: only the desktop needs it
            result = self.window.create_file_dialog(
                webview.FileDialog.OPEN,
                directory=str(start_dir) if start_dir.is_dir() else str(PROJECT_ROOT),
                allow_multiple=False,
                file_types=('GGUF 模型 (*.gguf)', '所有文件 (*.*)'),
            )
        except Exception as exc:  # noqa: BLE001
            return {'ok': False, 'message': f'打开文件选择器失败：{exc}'}
        if not result:
            return {'ok': False, 'cancelled': True}
        picked = result[0] if isinstance(result, (list, tuple)) else result
        return {'ok': True, 'path': str(picked)}



    def _cleanup_owned_model(self, preserve_ownership: bool = False) -> None:
        """Safely clean up owned actual model process and launcher with strict identity verification.
        Preserves external reused services untouched. Idempotent across multiple invocations.
        """
        if preserve_ownership:
            self.is_managed_owned = True
        # 1. Before touching launcher, check if handshake file has actual model process that wasn't registered yet
        if not self.model_service.get('pid') and self.model_handshake_file and self.model_handshake_file.is_file():
            try:
                raw_json = json.loads(self.model_handshake_file.read_text(encoding='utf-8'))
                valid, payload, err_msg = verify_handshake_payload(
                    raw_json, self.model_launch_token, ('spawned', 'ready')
                )
                if valid and payload:
                    self.model_service['pid'] = payload['pid']
                    self.model_service['identity'] = payload['identity']
                    self.model_service['token'] = payload['token']
                    self.model_service['owned'] = True
                else:
                    self.model_service['handshake_verification_error'] = err_msg
            except Exception as exc:  # noqa: BLE001
                self.model_service['handshake_verification_error'] = str(exc)

        # 2. Terminate actual model process FIRST, ONLY if owned and identity strictly verified
        if self.model_service.get('owned') and self.model_service.get('pid'):
            pid = self.model_service.get('pid')
            ident = self.model_service.get('identity')
            if ident and verify_process_identity(ident):
                ok, msg = terminate_process_with_identity(pid, ident, timeout_s=3.0)
                if ok:
                    self.model_service['owned'] = False
                    self.model_service['pid'] = None
                    self.model_service['identity'] = None
                    self.model_service['status'] = 'stopped'
                    self.model_service['cleanup_result'] = msg
                else:
                    self.model_service['status'] = 'cleanup_failed'
                    self.model_service['cleanup_error'] = msg
                    print(f"CRITICAL: Failed to clean up model process PID {pid}: {msg}")
            else:
                self.model_service['status'] = 'unverified_identity_refused_stop'
                self.model_service['cleanup_error'] = f"Missing or unverified identity {ident} for PID {pid}; refusing termination."
                print(f"REFUSED: Missing or unverified identity for PID {pid}; refusing termination.")

        # 3. Terminate launcher process if still running
        if self.model_launcher_proc:
            try:
                self.model_launcher_proc.terminate()
                try:
                    self.model_launcher_proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.model_launcher_proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self.model_launcher_proc = None

        # 4. Remove handshake file
        if self.model_handshake_file and self.model_handshake_file.is_file():
            try:
                self.model_handshake_file.unlink()
            except Exception:  # noqa: BLE001
                pass
            self.model_handshake_file = None

        # 5. Close Job Object handle
        if self.model_job:
            try:
                k32.CloseHandle(self.model_job)
            except Exception:  # noqa: BLE001
                pass
            self.model_job = None

    # JS API exposed to window (for Retry and Exit buttons)
    def exit_app(self) -> None:
        if self.window:
            self.window.destroy()

    def retry_init(self) -> None:
        with self._state_lock:
            if self.init_state == 'initializing':
                # Ignore click while initializing
                return
            if self.init_state in ('ready', 'shutting_down', 'exited'):
                return
            if self.init_state != 'failed':
                return
            self.init_state = 'initializing'
            self.init_attempt_id += 1
            attempt_id = self.init_attempt_id

        self.init_cancelled = False
        if self.window:
            self._eval("clearError();")
        self._init_thread = threading.Thread(target=self._run_init, args=(attempt_id,), daemon=True)
        self._init_thread.start()

    def _eval(self, js: str) -> None:
        if self.window and not self.shutting_down:
            try:
                self.window.evaluate_js(js)
            except Exception:  # noqa: BLE001
                pass

    def _step(self, idx: int, state: str, detail: str) -> None:
        js_detail = json.dumps(detail, ensure_ascii=False)
        self._eval(f"updateStep({idx}, '{state}', {js_detail});")

    def _show_error(self, title: str, msg: str) -> None:
        js_title = json.dumps(title, ensure_ascii=False)
        js_msg = json.dumps(msg, ensure_ascii=False)
        self._eval(f"showError({js_title}, {js_msg});")

    # ----------------------------------------------------------------------- #
    # Step 1: Environment Check
    # ----------------------------------------------------------------------- #
    def _check_environment(self) -> bool:
        self._step(0, 'active', '正在核对配置文件与目录...')
        time.sleep(0.15)
        if not CONFIG_PATH.is_file():
            self._step(0, 'error', f'找不到配置文件: {CONFIG_PATH}')
            self._show_error('配置缺失', f'请确保配置文件存在：\n{CONFIG_PATH}')
            return False
        if not (STATIC_DIR / 'index.html').is_file():
            self._step(0, 'error', f'静态网页资源缺失: {STATIC_DIR}')
            self._show_error('静态资源缺失', f'无法找到聊天页面资源：\n{STATIC_DIR}')
            return False
        self._step(0, 'done', '运行环境与 WebView2 正常 (Edge Chromium)')
        return True

    # ----------------------------------------------------------------------- #
    # Step 2: Model Service Check & Startup
    # ----------------------------------------------------------------------- #
    def _probe_model(self, base_url: str, expected: Optional[dict] = None
                     ) -> tuple[bool, Optional[dict], Optional[str]]:
        """Health + identity + window probe.

        Round 43: the expected alias/window come from the config the caller is
        trying to load, not from re-reading a deployment FILE. A deployment file
        describes the configured model; this function must be told which model
        this particular probe is about, otherwise a switch can "verify" the old
        alias and call it a success.
        """
        root = base_url.removesuffix('/v1')
        if expected is None:
            try:
                expected = modelprofiles.effective_deployment(None, strict=False)
            except modelprofiles.ProfileError:
                expected = {}
        expected_alias = expected.get('alias') or ''
        expected_ctx = expected.get('context_size')

        # 1. Health check
        req = urllib.request.Request(f'{root}/health', method='GET')
        try:
            with urllib.request.urlopen(req, timeout=1.5) as r:
                if r.status != 200:
                    return False, None, f'/health 响应状态码: {r.status}'
                data = json.loads(r.read().decode('utf-8'))
                if data.get('status') != 'ok':
                    return False, None, f"/health 状态不正确: {data.get('status')}"
        except Exception as exc:  # noqa: BLE001
            return False, None, f"健康检查失败: {exc}"

        # 2. Model identity check (alias only -- it is necessary, NOT sufficient:
        #    the window and, when available, the model path are checked as well).
        models_req = urllib.request.Request(f'{root}/v1/models', method='GET')
        model_ids: list[str] = []
        try:
            with urllib.request.urlopen(models_req, timeout=2.0) as r:
                if r.status == 200:
                    m_data = json.loads(r.read().decode('utf-8'))
                    models_list = m_data.get('data') or m_data.get('models') or []
                    model_ids = [m.get('id') or m.get('name') for m in models_list]
                    if expected_alias and expected_alias not in model_ids:
                        return False, None, f"模型标识不匹配：预期 '{expected_alias}'，服务提供 {model_ids}"
        except Exception as exc:  # noqa: BLE001
            return False, None, f"模型身份核验失败: {exc}"

        # 3. Context window check
        if expected_ctx:
            try:
                props_req = urllib.request.Request(f'{root}/props', method='GET')
                with urllib.request.urlopen(props_req, timeout=2.0) as r:
                    if r.status == 200:
                        props_data = json.loads(r.read().decode('utf-8'))
                        gen_settings = props_data.get('default_generation_settings') or {}
                        n_ctx = gen_settings.get('n_ctx')
                        if n_ctx and n_ctx != expected_ctx:
                            return False, None, f"上下文长度不匹配：预期 {expected_ctx}，服务为 {n_ctx}"
            except Exception:  # noqa: BLE001
                pass

        return True, data, None

    def verify_loaded_profile(self, expected: dict) -> dict:
        """Independent verification of what is ACTUALLY serving.

        An alias alone cannot prove the right FILE was loaded: the alias is a
        label the server prints, and a stale process can still answer with the
        previous one. So every available fact is collected and the verdict only
        claims what the facts support:

          * window     -- `/props` n_ctx MUST equal the target. Hard requirement.
          * model file -- `/props.model_path` when the server reports it (llama.cpp
                          does); otherwise this app's own launch config for managed
                          processes, labelled `launch-config` (never presented as
                          an independent measurement).
          * alias      -- `/v1/models` reachable, non-empty, and expected alias present.
                          Required for all modes.
          * identity   -- for MANAGED processes, the PID we manage must still match
                          the process creation time we recorded. A missing identity
                          or a mismatch is FATAL.
                          For EXTERNAL services, process identity is not applicable
                          (recorded as None with note; no unverified operations).

        Returns {'ok': bool, 'reason': str|None, 'checks': {...}}.
        """
        base = self.model_service.get('base_url') or ''
        root = base.removesuffix('/v1')
        checks: dict[str, Any] = {}
        if not base:
            return {'ok': False, 'reason': '模型服务地址未知，无法核验。', 'checks': checks}

        is_managed = bool(self._is_managed_owned_model())
        checks['managed_service'] = is_managed

        expected_alias = (expected.get('alias') or '').strip()
        expected_ctx = expected.get('context_size')
        expected_model = Path(expected.get('model') or '') if expected.get('model') else None

        props: dict = {}
        try:
            with urllib.request.urlopen(f'{root}/props', timeout=3.0) as r:
                props = json.loads(r.read().decode('utf-8'))
            checks['props_reachable'] = True
        except Exception as exc:  # noqa: BLE001
            checks['props_reachable'] = False
            checks['props_error'] = f'{type(exc).__name__}: {exc}'
            return {'ok': False, 'reason': f'无法读取服务 /props：{exc}', 'checks': checks}

        # 1. Window (hard requirement for all services)
        n_ctx = (props.get('default_generation_settings') or {}).get('n_ctx')
        checks['service_n_ctx'] = n_ctx
        checks['expected_n_ctx'] = expected_ctx
        checks['window_matches'] = (type(n_ctx) is int and n_ctx == expected_ctx)
        if not checks['window_matches']:
            return {'ok': False,
                    'reason': f'实际窗口 {n_ctx} 与目标 {expected_ctx} 不符',
                    'checks': checks}

        # 2. Model file (service-reported props.model_path vs managed launch config)
        served_path_txt = props.get('model_path')
        checks['service_model_path'] = served_path_txt
        checks['expected_model_path'] = str(expected_model) if expected_model else None

        if served_path_txt and expected_model:
            try:
                file_ok = Path(served_path_txt).resolve() == expected_model.resolve()
            except OSError:
                file_ok = str(served_path_txt).lower() == str(expected_model).lower()
            checks['model_path_matches'] = file_ok
            checks['file_evidence'] = 'service-props'
        elif expected_model and is_managed:
            launch_model = ((self._launch_evidence or {}).get('model'))
            if launch_model:
                try:
                    file_ok = Path(launch_model).resolve() == expected_model.resolve()
                except OSError:
                    file_ok = str(launch_model).lower() == str(expected_model).lower()
            else:
                file_ok = False
            checks['model_path_matches'] = file_ok
            checks['file_evidence'] = 'launch-config' if launch_model else 'unavailable'
            checks['file_evidence_note'] = (
                '服务未报告 model_path，文件一致性依据本应用的启动配置，'
                '不是服务自述的独立证据。')
        else:
            checks['model_path_matches'] = False
            checks['file_evidence'] = 'unavailable'
            checks['file_evidence_note'] = '服务未自述 model_path 且无受管启动参数记录，无法核验模型文件。'

        if checks['model_path_matches'] is not True:
            return {'ok': False,
                    'reason': ('无法确认实际加载的模型文件就是目标文件'
                               f'（服务 model_path={served_path_txt}，'
                               f'证据={checks.get("file_evidence")}）'),
                    'checks': checks}

        # 3. Serving Alias (/v1/models must be reachable, non-empty, and contain alias)
        try:
            with urllib.request.urlopen(f'{base}/models', timeout=3.0) as r:
                m = json.loads(r.read().decode('utf-8'))
            data_items = m.get('data') or []
            ids = [x.get('id') or x.get('name') for x in data_items if isinstance(x, dict)]
            checks['service_model_ids'] = ids
            checks['models_reachable'] = True
            if not ids:
                checks['alias_matches'] = False
                checks['models_error'] = '服务 /v1/models 返回的模型列表为空'
            elif not expected_alias:
                checks['alias_matches'] = False
                checks['models_error'] = '预期模型别名为空，无法匹配'
            else:
                checks['alias_matches'] = (expected_alias in ids)
        except Exception as exc:  # noqa: BLE001
            checks['service_model_ids'] = None
            checks['models_reachable'] = False
            checks['alias_matches'] = False
            checks['models_error'] = f'{type(exc).__name__}: {exc}'

        if not checks.get('models_reachable', False):
            return {'ok': False,
                    'reason': f'无法连接模型服务 /v1/models 核验别名（{checks.get("models_error")}）',
                    'checks': checks}
        if checks.get('alias_matches') is not True:
            if not checks.get('service_model_ids'):
                return {'ok': False,
                        'reason': f'服务 /v1/models 未提供任何模型别名（{checks.get("models_error") or "空列表"}）',
                        'checks': checks}
            return {'ok': False,
                    'reason': (f"服务别名与目标不符：预期 '{expected_alias}'，"
                               f"服务提供 {checks.get('service_model_ids')}"),
                    'checks': checks}

        # 4. Process identity (strictly required for managed owned processes)
        ident = self.model_service.get('identity')
        if is_managed:
            if not ident:
                checks['identity_verified'] = False
                checks['identity_error'] = 'missing_identity_record'
                checks['identity_note'] = '受管理模型进程缺少身份记录，无法确认进程所有权'
                return {'ok': False,
                        'reason': '受管理模型进程缺少身份记录，无法确认进程所有权',
                        'checks': checks}
            ident_ok = verify_process_identity(ident)
            checks['identity_verified'] = bool(ident_ok)
            if not ident_ok:
                checks['identity_error'] = 'process_identity_invalid'
                return {'ok': False,
                        'reason': '本应用启动的模型进程身份已失效（PID 被复用或进程已退出）',
                        'checks': checks}
            checks['identity_note'] = '受管理进程身份核验通过'
        else:
            checks['identity_verified'] = None
            checks['identity_note'] = '外部复用的服务，本应用不执行受管理进程身份校验与进程生命周期管理'

        return {'ok': True, 'reason': None, 'checks': checks}


    def _get_runtime_n_ctx(self, base_url: Optional[str]) -> Optional[int]:
        if not base_url:
            return None
        root = base_url.removesuffix('/v1')
        try:
            req = urllib.request.Request(f'{root}/props', method='GET')
            with urllib.request.urlopen(req, timeout=2.0) as r:
                if r.status == 200:
                    data = json.loads(r.read().decode('utf-8'))
                    gen_settings = data.get('default_generation_settings') or {}
                    n = gen_settings.get('n_ctx')
                    if isinstance(n, int) and n > 0:
                        return n
        except Exception:
            pass
        return None

    def get_model_status(self) -> dict:
        inst = self.get_runtime_instance()
        svc = self.chat_service.get('service')
        busy_reason = svc.busy_summary() if svc else None

        with self._switch_state_lock:
            cur_pid = self.current_profile_id
            tgt_pid = self.target_profile_id
            prof = dict(self.current_profile) if self.current_profile else {}
            m_status = self.model_status
            act_win = self.actual_window
            tgt_win = self.target_window
            is_rel = self._is_reloading
            last_err = self.last_reload_error
            seq = self._instance_seq
            l_switch = dict(self.last_switch) if self.last_switch else None

        return {
            'ok': True,
            'alias': inst.model_alias,
            'owned': self._is_managed_owned_model(),
            'pid': self.model_service.get('pid'),
            'status': m_status,
            'actual_window': act_win,
            'target_window': tgt_win or act_win,
            # Round 41: presets + limits come from the shared params module.
            'available_windows': list(chatparams.CONTEXT_WINDOW_PRESETS),
            'window_limits': {'min': chatparams.MIN_CONTEXT_WINDOW,
                              'max': chatparams.ABSOLUTE_MAX_CONTEXT_WINDOW,
                              'step': chatparams.CONTEXT_WINDOW_STEP},
            'reloading': is_rel or m_status in ('stopping', 'loading', 'restoring'),
            'busy': bool(busy_reason),
            'busy_reason': busy_reason,
            'error': last_err,
            'instance_seq': seq,
            'base_url': inst.base_url,
            # ---- Round 43: profile identity ------------------------------- #
            # The four ids are reported separately. `current` is set only after a
            # verified load; `display_name` is empty when nothing is loaded, so
            # the page can never show a model name that is not running.
            'profile_id': cur_pid,
            'profile_display_name': prof.get('display_name'),
            'profile_serving_alias': prof.get('serving_alias') or inst.model_alias,
            'profile_target_window': prof.get('target_context_window'),
            'profile_default_max_output': prof.get('default_max_output_tokens'),
            'profile_gpu_layers': prof.get('gpu_layers'),
            'profile_model_path': prof.get('model_path'),
            'profile_file_name': (Path(prof.get('model_path') or '').name
                                  if prof.get('model_path') else None),
            'target_profile_id': tgt_pid,
            'launch_profile_id': self.launch_profile_id,
            'launch_profile_source': self.launch_profile_source,
            'registry_error': self.registry_error,
            'switching': is_rel or m_status in
                         ('stopping', 'loading', 'restoring'),
            'last_switch': l_switch,
        }

    def _check_and_start_model(self, attempt_id: int) -> bool:
        with self._state_lock:
            if self.init_attempt_id == 0:
                self.init_attempt_id = attempt_id
        self._step(1, 'active', '正在检测模型服务状态...')
        # Round 43: the launch config comes from the profile registry (or the
        # deployment file as a legacy fallback) -- never from a Qwen literal.
        dep = self._resolve_launch_profile()
        port = self.model_port or int(dep.get('port', 18085))
        host = dep.get('host', '127.0.0.1')
        base_url = f"http://{host}:{port}/v1"
        self.model_service['port'] = port
        self.model_service['base_url'] = base_url
        launch_profile = (modelprofiles.get_profile(
            modelprofiles.load_registry(), self.launch_profile_id)
            if self.launch_profile_id else None)
        label = (launch_profile or {}).get('display_name') or dep.get('alias') or '未命名模型'

        # Check if already owned by this instance from a previous attempt
        if self.model_service.get('owned') and self.model_service.get('pid'):
            alive, health, err = self._probe_model(base_url, dep)
            if alive:
                self.is_managed_owned = True
                self.model_alias = dep.get('alias') or ''
                self.model_status = 'ready'
                self.actual_window = self._get_runtime_n_ctx(base_url)
                self.target_window = self.actual_window or dep.get('context_size')
                self.last_reload_error = None
                self._commit_current_profile(launch_profile, dep)
                self._sync_runtime_instance()
                self._step(1, 'done', f'自启动模型服务正常 (端口 {port}，PID: {self.model_service["pid"]})')
                self._step(2, 'done', f'模型服务已就绪（{label}）')
                return True

        # 1. Probe if model is already running & healthy
        alive, health, err = self._probe_model(base_url, dep)
        if alive:
            owner_pid = listener_pid(port)
            self.model_service['pid'] = owner_pid
            self.model_service['identity'] = process_identity(owner_pid) if owner_pid else None
            self.model_service['owned'] = False
            self.model_service['status'] = 'reused'
            self.is_managed_owned = False
            self.model_alias = dep.get('alias') or ''
            self.model_status = 'ready'
            self.actual_window = self._get_runtime_n_ctx(base_url)
            self.target_window = self.actual_window or dep.get('context_size')
            self.last_reload_error = None
            # An EXTERNAL service is described by what it reports, not by what we
            # would have launched. Its profile is claimed only if it matches.
            self._commit_current_profile(launch_profile, dep)
            self._sync_runtime_instance()
            self._step(1, 'done', f'检测到运行中的模型服务 (端口 {port}，PID: {owner_pid})')
            self._step(2, 'done', '模型服务已就绪，安全复用已有实例（外部服务只读）')
            return True

        # 2. Check if port is held by an occupier that failed probe
        occupier = listener_pid(port)
        if occupier is not None:
            msg = f'端口 {port} 被外部程序占用 (PID: {occupier})，且未响应正确的模型服务。'
            if err:
                msg += f'\n原因: {err}'
            self._step(1, 'error', f'端口 {port} 占用冲突')
            self._show_error('端口冲突', msg)
            return False

        # 3. Model needs to be started
        self._step(1, 'done', f'未检测到运行中的模型服务，准备由桌面启动 (端口 {port})')
        self._step(2, 'active',
                   f'正在启动 llama-server 并加载「{label}」至显存 (约需 2-5 秒)...')

        ok, err_msg = self._spawn_and_verify_owned_model(attempt_id, dep)
        if not ok:
            self.model_status = 'unavailable'
            self.last_reload_error = err_msg
            self._sync_runtime_instance()
            self._step(2, 'error', f'模型服务启动失败: {err_msg[:200]}')
            self._show_error('模型启动失败', err_msg or '启动超时或核验未通过')
            return False

        self.is_managed_owned = True
        self.model_alias = dep.get('alias') or ''
        self.model_status = 'ready'
        self.actual_window = self._get_runtime_n_ctx(base_url)
        self.target_window = self.actual_window or dep.get('context_size')
        self.last_reload_error = None
        self._commit_current_profile(launch_profile, dep)
        self._sync_runtime_instance()
        self._step(2, 'done',
                   f'模型加载就绪（{label}，PID: {self.model_service["pid"]}，已由本次启动接管）')
        return True

    def _spawn_and_verify_owned_model(self, attempt_id: int,
                                      dep: Optional[dict] = None
                                      ) -> tuple[bool, Optional[str]]:
        if not local_model:
            return False, '缺少 scripts/local_model.py 模块，无法启动模型。'

        # Round 43: the caller passes the EXACT config it intends to load. If it
        # is omitted (older call sites), fall back to the resolved launch config.
        if dep is None:
            dep = self._resolve_launch_profile()
        port = self.model_port or int(dep.get('port', 18085))
        host = dep.get('host', '127.0.0.1')
        base_url = f"http://{host}:{port}/v1"
        self.model_service['port'] = port
        self.model_service['base_url'] = base_url
        # Written per launch so `--config` names THE target, never a shared file
        # that a concurrent edit could change underneath the launcher.
        config_file = self._write_runtime_config(dep, tag='launch')
        self.model_config_path = config_file

        fail_job_create = getattr(self, '_test_hook_fail_job_create', False)
        fail_job_limit = getattr(self, '_test_hook_fail_job_limit', False)
        if fail_job_create:
            self.model_job = None
        elif self.model_job is None:
            self.model_job = create_kill_on_close_job(fail_limit_hook=fail_job_limit)

        if not self.model_job:
            return False, '未能创建 Windows Job Object 退出保护机制，自建模型启动中止。'

        h_auth_event = None
        try:
            python_exe = getattr(sys, '_base_executable', sys.executable)
            launch_token = f"launch_{os.getpid()}_{attempt_id}_{time.time_ns()}"
            self.model_launch_token = launch_token
            handshake_file = LOG_DIR / f'handshake_{launch_token}.json'
            self.model_handshake_file = handshake_file

            auth_event_name = f"Local\\ModelLaunchAuth_{launch_token}"
            h_auth_event = k32.CreateEventW(None, True, False, auth_event_name)
            if not h_auth_event:
                self._cleanup_owned_model()
                return False, '未能创建 Win32 同步授权事件，自建模型启动中止。'

            cmd = [
                python_exe, str(SCRIPTS_DIR / 'local_model.py'), 'start',
                '--port', str(port),
                '--handshake-file', str(handshake_file),
                '--managed',
                '--no-switch-port',
                '--parent-pid', str(os.getpid()),
                '--token', launch_token,
                '--parent-auth-event', auth_event_name
            ]
            if self.model_config_path:
                cmd.extend(['--config', str(self.model_config_path)])

            extra_args = getattr(self, '_test_extra_launcher_args', None)
            if extra_args:
                cmd.extend(extra_args)

            proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT),
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding='utf-8', errors='replace',
                                    creationflags=CREATE_NO_WINDOW)
            self.model_launcher_proc = proc
            self.last_spawned_launcher = {
                'pid': proc.pid,
                'identity': process_identity(proc.pid)
            }

            fail_assign_launcher = getattr(self, '_test_hook_fail_assign_launcher', False)
            assigned_launcher = False if fail_assign_launcher else assign_handle_to_job(self.model_job, proc._handle)
            if not assigned_launcher:
                self.last_abort_stage = 'launcher_assign_protection_failed'
                self._cleanup_owned_model()
                return False, '无法将模型启动器进程加入 Windows Job Object，自建模型启动中止。'

            # Barrier passed: Launcher is safely inside Job Object!
            k32.SetEvent(h_auth_event)

            self.launcher_stdout_lines = []
            self.launcher_stderr_lines = []
            def _drain(stream, sink: list[str]):
                try:
                    for line in iter(stream.readline, ''):
                        sink.append(line)
                        if len(sink) > 300:
                            sink.pop(0)
                except Exception:
                    pass
                finally:
                    try:
                        stream.close()
                    except Exception:
                        pass
            threading.Thread(target=_drain, args=(proc.stdout, self.launcher_stdout_lines), daemon=True).start()
            threading.Thread(target=_drain, args=(proc.stderr, self.launcher_stderr_lines), daemon=True).start()

            ready = False
            handshake_done = False
            last_verify_err = ""
            began = time.time()
            while time.time() - began < 120:
                with self._state_lock:
                    if self.init_cancelled or self.shutting_down:
                        self._cleanup_owned_model()
                        return False, '桌面已取消或正在退出'

                ret = proc.poll()
                if ret is not None and ret != 0:
                    err_lines = self.launcher_stderr_lines or self.launcher_stdout_lines or [f'Exit code {ret}']
                    err_msg = "".join(err_lines[-15:])[-500:]
                    self._cleanup_owned_model()
                    return False, f'模型服务启动失败: {err_msg[:300]}'

                if not handshake_done:
                    h_data = None
                    if handshake_file.is_file():
                        try:
                            raw_json = json.loads(handshake_file.read_text(encoding='utf-8'))
                            valid, payload, err_msg = verify_handshake_payload(
                                raw_json, self.model_launch_token, ('spawned', 'ready', 'reused')
                            )
                            if valid and payload:
                                h_data = payload
                            else:
                                last_verify_err = err_msg
                        except Exception as exc:
                            last_verify_err = f"文件握手解析失败: {exc}"

                    if not h_data:
                        for line in reversed(self.launcher_stdout_lines):
                            if line.startswith('@@HANDSHAKE@@:'):
                                try:
                                    raw_json = json.loads(line.removeprefix('@@HANDSHAKE@@:').strip())
                                    valid, payload, err_msg = verify_handshake_payload(
                                        raw_json, self.model_launch_token, ('spawned', 'ready', 'reused')
                                    )
                                    if valid and payload:
                                        h_data = payload
                                        break
                                    else:
                                        last_verify_err = err_msg
                                except Exception as exc:
                                    last_verify_err = f"标准输出握手解析失败: {exc}"

                    if h_data:
                        evt = h_data.get('event')
                        if evt in ('spawned', 'ready'):
                            child_pid = h_data['pid']
                            fail_assign_model = getattr(self, '_test_hook_fail_assign_model', False)
                            if not fail_assign_model:
                                assign_pid_to_job(self.model_job, child_pid)
                                in_job = is_pid_in_job(self.model_job, child_pid)
                            else:
                                in_job = False

                            if not in_job:
                                self.last_abort_stage = 'model_assign_protection_failed'
                                self._cleanup_owned_model()
                                return False, f'模型进程 (PID: {child_pid}) 未能纳入 Windows Job Object 退出保护，自建模型启动中止。'

                            self.model_service['owned'] = True
                            self.model_service['pid'] = child_pid
                            self.model_service['identity'] = h_data['identity']
                            self.model_service['token'] = self.model_launch_token
                            self.model_service['status'] = 'starting' if evt == 'spawned' else 'started'
                            handshake_done = True
                        elif evt == 'reused':
                            self.model_service['owned'] = False
                            self.model_service['pid'] = h_data['pid']
                            self.model_service['identity'] = h_data['identity']
                            self.model_service['status'] = 'reused'
                            handshake_done = True

                alive, _, probe_err = self._probe_model(base_url, dep)
                if alive:
                    ready = True
                    break
                elif probe_err:
                    last_verify_err = probe_err
                time.sleep(0.3)

            if ready and not handshake_done:
                began_wait_hs = time.time()
                while time.time() - began_wait_hs < 2.0:
                    if handshake_file.is_file():
                        try:
                            raw_json = json.loads(handshake_file.read_text(encoding='utf-8'))
                            valid, payload, err_msg = verify_handshake_payload(
                                raw_json, self.model_launch_token, ('spawned', 'ready', 'reused')
                            )
                            if valid and payload:
                                evt = payload.get('event')
                                if evt in ('spawned', 'ready'):
                                    child_pid = payload['pid']
                                    fail_assign_model = getattr(self, '_test_hook_fail_assign_model', False)
                                    if not fail_assign_model:
                                        assign_pid_to_job(self.model_job, child_pid)
                                        in_job = is_pid_in_job(self.model_job, child_pid)
                                    else:
                                        in_job = False

                                    if not in_job:
                                        self._cleanup_owned_model()
                                        return False, f'模型进程 (PID: {child_pid}) 未能纳入 Windows Job Object 退出保护，自建模型启动中止。'

                                    self.model_service['owned'] = True
                                    self.model_service['pid'] = child_pid
                                    self.model_service['identity'] = payload['identity']
                                    self.model_service['token'] = self.model_launch_token
                                    self.model_service['status'] = 'started'
                                    handshake_done = True
                                    break
                                elif evt == 'reused':
                                    self.model_service['owned'] = False
                                    self.model_service['pid'] = payload['pid']
                                    self.model_service['identity'] = payload['identity']
                                    self.model_service['status'] = 'reused'
                                    handshake_done = True
                                    break
                        except Exception:
                            pass
                    time.sleep(0.1)

            if not ready:
                self._cleanup_owned_model()
                return False, f'模型已启动但在规定时间内未通过核验: {last_verify_err or "超时"}'

            if not handshake_done or not self.model_service.get('pid'):
                self._cleanup_owned_model()
                return False, f'未能安全建立模型服务的所有权凭证: {last_verify_err}'

            if self.model_service.get('owned'):
                self.model_service['status'] = 'started'
            else:
                self.model_service['status'] = 'reused'

            return True, None
        except Exception as exc:
            self._cleanup_owned_model()
            return False, str(exc)
        finally:
            if h_auth_event:
                try:
                    k32.CloseHandle(h_auth_event)
                except Exception:
                    pass
                h_auth_event = None

    # ----------------------------------------------------------------------- #
    # Round 43: the ONE managed switch lifecycle
    #
    # Both "reload at a new window" (round 41) and "switch to another profile"
    # (round 43) are the same operation with a different target, so they share
    # this state machine instead of growing a second one. The order below is
    # the order the requirement lists, and each step is where it is in the code:
    #
    #   1. validate the target profile and its model FILE      _prepare_target
    #   2. snapshot the ORIGINAL running config                _capture_active_model_config
    #   3. enter an explicit switching state                   model_status = stopping/loading
    #   4. chat / search / extraction / summary handled         pause_for_reload + busy check
    #   5. stop only the OLD service this app owns & verified   _cleanup_owned_model
    #   6. launch the target config                             _spawn_and_verify_owned_model
    #   7. verify identity / model / actual window              verify_loaded_profile
    #   8. on success commit profile + capabilities + gen       _commit_current_profile
    #   9. on failure restore the last SUCCESSFUL config        _restore_to
    # ----------------------------------------------------------------------- #
    def _prepare_target(self, *, profile_id: Optional[str], window: Optional[int]
                        ) -> tuple[Optional[dict], dict, Optional[dict]]:
        """Resolve and VALIDATE the target before anything is stopped.

        Returns `(profile, config, error)`. A missing file is refused here, which
        means a bad target never costs the user their running model.
        """
        try:
            reg = modelprofiles.ensure_initialized()
        except modelprofiles.ProfileError as exc:
            return None, {}, exc.to_dict()

        if profile_id:
            try:
                prof = modelprofiles.require_profile(reg, profile_id)
            except modelprofiles.ProfileError as exc:
                return None, {}, exc.to_dict()
        else:
            prof = self.current_profile

        try:
            cfg = modelprofiles.effective_deployment(
                prof.get('profile_id') if prof else None, strict=True)
        except modelprofiles.ProfileError as exc:
            return None, {}, exc.to_dict()

        if window is not None:
            cfg = dict(cfg)
            cfg['context_size'] = window

        # The file must exist and be a GGUF before the old model is touched.
        target_path = cfg.get('model')
        if not target_path:
            return prof, cfg, {'ok': False, 'error': 'model_path_missing',
                               'message': '目标配置缺少模型文件路径。'}
        info = modelprofiles.describe_model_file(Path(target_path))
        if not info['is_file']:
            return prof, cfg, {
                'ok': False, 'error': 'model_file_missing',
                'message': f'目标模型文件不存在：{target_path}',
                'model_path': target_path}
        if not info['looks_like_gguf']:
            return prof, cfg, {
                'ok': False, 'error': 'model_file_invalid',
                'message': f'目标文件不是 GGUF 模型（头部 {info["gguf_magic"]!r}）：{target_path}',
                'model_path': target_path}
        # Port/host stay the deployment's: the profile changes the MODEL, not the
        # endpoint. Keep them explicit so the launcher never has to guess.
        cfg['port'] = self.model_port or int(cfg.get('port', 18085))
        cfg['host'] = cfg.get('host', '127.0.0.1')
        cfg['base_url'] = f"http://{cfg['host']}:{cfg['port']}/v1"
        return prof, cfg, None

    def reload_model(self, target_window: int) -> dict:
        """Round 41 entry point: re-load the CURRENT profile at a new window."""
        if getattr(self, 'shutting_down', False):
            return {'ok': False, 'error': 'shutting_down', 'message': '应用正在退出，无法重新加载模型。'}

        # Round 41: any aligned integer in band, not only 2048/4096.
        try:
            target_window = chatparams.validate_context_window(target_window)
        except chatparams.ParamError as exc:
            return exc.to_dict()

        if not self._is_managed_owned_model():
            return {'ok': False, 'error': 'external_service_read_only', 'message': '当前使用外部模型服务，需在其启动配置中调整。'}

        return self._begin_switch(mode='window',
                                  profile_id=self.current_profile_id,
                                  window=target_window)

    def switch_profile(self, profile_id: str) -> dict:
        """Round 43 entry point: switch to a different (or same) profile."""
        if getattr(self, 'shutting_down', False):
            return {'ok': False, 'error': 'shutting_down',
                    'message': '应用正在退出，无法切换模型。'}
        if not profile_id:
            return {'ok': False, 'error': 'missing_profile_id',
                    'message': '请选择一个模型配置。'}
        # An EXTERNAL service is read-only: this app did not start it, so it must
        # not stop it to make room for its own. Clicking "load" on a profile must
        # never silently kill somebody else's server.
        if not self._is_managed_owned_model():
            return {'ok': False, 'error': 'external_service_read_only',
                    'message': ('当前模型服务不是本应用启动的，本应用不会停止或替换它。'
                                '如需切换模型，请先关闭该外部服务。')}
        return self._begin_switch(mode='profile', profile_id=profile_id, window=None)

    def _begin_switch(self, *, mode: str, profile_id: Optional[str],
                      window: Optional[int]) -> dict:
        if not self._model_reload_lock.acquire(blocking=False):
            return {'ok': False, 'error': 'already_reloading',
                    'message': '模型正在切换中，请勿重复操作。'}

        try:
            with self._switch_state_lock:
                prof, target_cfg, err = self._prepare_target(profile_id=profile_id,
                                                             window=window)
                if err is not None:
                    self.last_switch = {'mode': mode, 'profile_id': profile_id,
                                        'target_window': window, 'result': 'rejected',
                                        'error': err.get('message'),
                                        'at': chatstore.now()}
                    return err

                svc = self.chat_service.get('service')
                if svc:
                    with svc._lock:
                        busy = svc.busy_summary()
                        if busy:
                            return {'ok': False, 'error': 'busy',
                                    'message': f'当前正在执行任务（{busy}），请先等待完成或停止后再切换。'}
                        # Atomic pause: no NEW chat may start while services swap.
                        svc.pause_for_reload()

                self._is_reloading = True
                self.target_window = target_cfg.get('context_size')
                self.target_profile_id = (prof or {}).get('profile_id')
                orig_window = self.actual_window
                orig_config = self._capture_active_model_config()
                orig_profile_id = self.current_profile_id
                orig_profile = dict(self.current_profile) if self.current_profile else None
                self._rollback_profile_id = (orig_profile or {}).get('profile_id') or orig_profile_id
                self._instance_seq += 1
                seq = self._instance_seq

                t = threading.Thread(
                    target=self._execute_switch,
                    args=(mode, prof, target_cfg, orig_config, orig_profile, seq),
                    daemon=True,
                    name=f'model-switch-{seq}',
                )
                t.start()

                label = (prof or {}).get('display_name') or target_cfg.get('alias')
                return {
                    'ok': True,
                    'message': (f'已开始切换到「{label}」' if mode == 'profile'
                                else '已开始重新加载模型'),
                    'mode': mode,
                    'profile_id': (prof or {}).get('profile_id'),
                    'target_profile_id': (prof or {}).get('profile_id'),
                    'from_profile_id': orig_profile_id,
                    'target_window': target_cfg.get('context_size'),
                    'actual_window': orig_window,
                    'status': 'stopping',
                    'instance_seq': seq,
                }
        except Exception as exc:
            with self._switch_state_lock:
                self._is_reloading = False
                self.target_profile_id = None
                self._rollback_profile_id = None
            self.last_reload_error = f"切换初始化异常：{exc}"
            svc = self.chat_service.get('service')
            if svc:
                svc.resume_after_reload()
            return {'ok': False, 'error': 'init_failed', 'message': f'切换初始化失败：{exc}'}
        finally:
            if not self._is_reloading and self._model_reload_lock.locked():
                try:
                    self._model_reload_lock.release()
                except RuntimeError:
                    pass

    def _wait_port_free(self, host: str, port: int, timeout_s: float = 8.0) -> bool:
        began = time.perf_counter()
        pf_func = getattr(self, '_test_port_free', port_free)
        while time.perf_counter() - began < timeout_s:
            if getattr(self, 'shutting_down', False):
                return False
            if pf_func(host, port):
                return True
            time.sleep(0.1)
        return bool(pf_func(host, port))

    def _start_runtime(self, cfg: dict) -> tuple[bool, Optional[str]]:
        """Launch exactly `cfg` and wait for ownership + readiness."""
        self.model_config_path = self._write_runtime_config(cfg, tag='switch')
        self.model_service['port'] = cfg.get('port')
        self.model_service['base_url'] = cfg.get('base_url')
        # Recorded BEFORE the spawn: this is what we asked for, and the only
        # evidence of the model FILE when the service itself does not report it.
        self._launch_evidence = {
            'model': cfg.get('model'),
            'alias': cfg.get('alias'),
            'context_size': cfg.get('context_size'),
            'profile_id': cfg.get('profile_id'),
        }
        self.init_attempt_id += 1
        return self._spawn_and_verify_owned_model(self.init_attempt_id, cfg)


    def _record_switch_diagnostics(self, payload: dict) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / 'reload_diagnostics.json'
            prev = {}
            if path.is_file():
                try:
                    prev = json.loads(path.read_text(encoding='utf-8'))
                except Exception:  # noqa: BLE001
                    prev = {}
            history = prev.get('history') or []
            history.append(payload)
            out = dict(payload)
            out['history'] = history[-20:]
            path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                            encoding='utf-8')
        except Exception:  # noqa: BLE001
            pass

    def _execute_switch(self, mode: str, prof: Optional[dict], target_cfg: dict,
                        orig_config: dict, orig_profile: Optional[dict],
                        seq: int) -> None:
        svc = self.chat_service.get('service')
        port = target_cfg.get('port', 18085)
        host = target_cfg.get('host', '127.0.0.1')
        orig_window = orig_config.get('context_size')
        target_window = target_cfg.get('context_size')
        target_profile_id = (prof or {}).get('profile_id')
        target_label = (prof or {}).get('display_name') or target_cfg.get('alias')
        old_model_stopped = False
        record: dict[str, Any] = {
            'mode': mode,
            'from_profile_id': (orig_profile or {}).get('profile_id'),
            'to_profile_id': target_profile_id,
            'to_display_name': target_label,
            'target_window': target_window,
            'orig_window': orig_window,
            'instance_seq': seq,
            'started_at': chatstore.now(),
        }

        try:
            if getattr(self, 'shutting_down', False):
                return

            # Step B: stop ONLY the old service this app started and verified.
            self.model_status = 'stopping'
            self._sync_runtime_instance()
            self._cleanup_owned_model(preserve_ownership=bool(self._is_managed_owned_model()))
            old_model_stopped = True
            record['old_service_stopped'] = True

            if not self._wait_port_free(host, port):
                occ = listener_pid(port)
                raise RuntimeError(f"旧模型停止后端口 {port} 仍被占用 (PID: {occ})")

            if getattr(self, 'shutting_down', False):
                return

            # Step C: launch the target.
            self.model_status = 'loading'
            self._sync_runtime_instance()
            ok, err_msg = self._start_runtime(target_cfg)
            if not ok:
                raise RuntimeError(err_msg or "目标配置启动失败或核验未通过")

            # Step D: verify identity / model / window independently.
            verdict = self.verify_loaded_profile(target_cfg)
            record['verification'] = verdict
            if not verdict.get('ok'):
                raise RuntimeError(verdict.get('reason') or '目标配置核验未通过')

            # ---- Success: commit identity under lock ----------------------- #
            with self._switch_state_lock:
                self.actual_window = target_window
                self.target_window = target_window
                self._window_override = None
                self.model_status = 'ready'
                self.last_reload_error = None
                self.is_managed_owned = True
                self._commit_current_profile(prof, target_cfg)
                self.target_profile_id = None
                self._rollback_profile_id = None
                record.update({'result': 'ok', 'finished_at': chatstore.now()})
                self.last_switch = record
            self._sync_runtime_instance()
            if svc:
                svc.invalidate_model_caches()

        except Exception as exc:
            target_err = str(exc)
            record['target_error'] = target_err
            record['at'] = chatstore.now()
            self._record_switch_diagnostics(record)
            if getattr(self, 'shutting_down', False):
                return

            if not old_model_stopped:
                # Nothing was stopped: the original model is still serving.
                with self._switch_state_lock:
                    self.model_status = 'ready'
                    self.target_window = orig_window
                    self.target_profile_id = None
                    self._rollback_profile_id = None
                    self.last_reload_error = f"切换准备阶段失败，已保留原运行模型：{target_err}"
                    record.update({'result': 'not_started', 'recovery': 'not_needed'})
                    self.last_switch = record
                self._sync_runtime_instance()
                return

            # ---- Step E: restore the last SUCCESSFUL config ---------------- #
            # The restore target is the SNAPSHOT taken before the switch, not a
            # re-read of the registry: an edit made during the switch must not
            # change what "restore" means.
            self.model_status = 'restoring'
            self._sync_runtime_instance()
            self._cleanup_owned_model(preserve_ownership=True)
            self._wait_port_free(host, port)
            try:
                if getattr(self, 'shutting_down', False):
                    return
                rb_ok, rb_err = self._start_runtime(orig_config)
                if not rb_ok:
                    raise RuntimeError(rb_err or "恢复原配置启动失败")
                rb_verdict = self.verify_loaded_profile(orig_config)
                record['recovery_verification'] = rb_verdict
                if not rb_verdict.get('ok'):
                    raise RuntimeError(rb_verdict.get('reason') or '恢复后核验未通过')

                with self._switch_state_lock:
                    self.actual_window = orig_config.get('context_size')
                    self.target_window = orig_config.get('context_size')
                    self.model_status = 'ready'
                    self.is_managed_owned = True
                    self._commit_current_profile(orig_profile, orig_config)
                    self.target_profile_id = None
                    self._rollback_profile_id = None
                    self.last_reload_error = (
                        f"目标「{target_label}」加载失败（{target_err}），"
                        f"已自动恢复原配置"
                        + (f"「{orig_profile.get('display_name')}」" if orig_profile else '')
                        + f"（{orig_config.get('context_size')} tokens）。")
                    record.update({'result': 'rolled_back', 'recovery': 'ok',
                                   'finished_at': chatstore.now()})
                    self.last_switch = record
                self._sync_runtime_instance()
                if svc:
                    svc.invalidate_model_caches()
            except Exception as rb_exc:
                # Both failed: an explicit, diagnosable unavailable state.
                with self._switch_state_lock:
                    self.actual_window = None
                    self.model_status = 'unavailable'
                    self.target_profile_id = None
                    self._rollback_profile_id = None
                    self.last_reload_error = (
                        f"目标「{target_label}」加载失败（{target_err}），"
                        f"且自动恢复原配置亦失败（{rb_exc}）。模型当前不可用。")
                    record.update({'result': 'unavailable', 'recovery': 'failed',
                                   'recovery_error': str(rb_exc),
                                   'finished_at': chatstore.now()})
                    self.last_switch = record
                self._record_switch_diagnostics(record)
                self._sync_runtime_instance()
                # Process termination and waiting moved OUTSIDE _switch_state_lock
                # while keeping the reload transaction lock (_model_reload_lock) intact.
                try:
                    self._cleanup_owned_model(preserve_ownership=True)
                except Exception as clean_exc:
                    cleanup_err = f"清理残留进程异常：{clean_exc}"
                    with self._switch_state_lock:
                        self.last_reload_error = f"{self.last_reload_error}；{cleanup_err}"
                    record['cleanup_error'] = str(clean_exc)
                    self._record_switch_diagnostics(record)
        finally:
            with self._switch_state_lock:
                self._is_reloading = False
                self.target_profile_id = None
                self._rollback_profile_id = None
            if svc:
                svc.resume_after_reload()
            if self._model_reload_lock.locked():
                try:
                    self._model_reload_lock.release()
                except RuntimeError:
                    pass
            self._sync_runtime_instance()

    def _probe_chat_service(self, port: int) -> tuple[bool, Optional[dict], Optional[str]]:
        url = f'http://127.0.0.1:{port}/api/chat/status'
        req = urllib.request.Request(url, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=1.5) as r:
                if r.status == 200:
                    data = json.loads(r.read().decode('utf-8'))
                    return True, data, None
                return False, None, f"HTTP {r.status}"
        except Exception as exc:  # noqa: BLE001
            return False, None, str(exc)

    def _check_and_start_chat(self, attempt_id: int) -> bool:
        self._step(3, 'active', f'正在检测聊天服务 (端口 {self.port})...')

        # If already owned and running in-process
        if self.chat_service.get('owned') and self.chat_service.get('server'):
            alive, status, _ = self._probe_chat_service(self.port)
            if alive:
                self._step(3, 'done', f'Morgan 服务运行中 (端口 {self.port})')
                return True

        alive, status, err = self._probe_chat_service(self.port)
        if alive and status:
            # Explicitly verify project root and store path
            srv_root = Path(status.get('project_root', '')).resolve()
            my_root = PROJECT_ROOT.resolve()
            srv_store = Path(status.get('store_path', '')).resolve()
            exp_store = self.expected_store_path.resolve()

            if srv_root != my_root:
                self._step(3, 'error', f'端口 {self.port} 属于其他项目')
                self._show_error('服务冲突', f'端口 {self.port} 当前响应聊天接口，但其项目目录为：\n'
                                            f'{status.get("project_root")}\n与本项目不一致，拒绝接管。')
                return False

            if srv_store != exp_store:
                self._step(3, 'error', f'端口 {self.port} 数据目录不匹配')
                self._show_error('数据路径冲突', f'端口 {self.port} 正在使用其他数据库：\n'
                                                f'{status.get("store_path")}\n预期路径：\n{exp_store}\n拒绝接管。')
                return False

            owner_pid = status.get('pid') or listener_pid(self.port)
            self.chat_service['pid'] = owner_pid
            self.chat_service['owned'] = False
            self.chat_service['status'] = 'reused'
            self._step(3, 'done', f'检测到同项目同数据聊天服务 (端口 {self.port}，PID: {owner_pid})，安全复用')
            return True

        # Check if port is held by unrelated program
        occupier = listener_pid(self.port)
        if occupier is not None:
            self._step(3, 'error', f'端口 {self.port} 被外部程序占用 (PID: {occupier})')
            self._show_error('端口冲突', f'端口 {self.port} 当前被外部程序占用 (PID: {occupier})。\n'
                                        '请关闭占用程序后重试。')
            return False

        # Start in-process chat server
        self._step(3, 'active', f'正在启动 Morgan 服务 (端口 {self.port})...')
        try:
            from hermes_lite import chatweb, chatstore
            store = chatstore.ChatStore(self.expected_store_path)
            svc = chatweb.ChatService(store)
            svc.set_desktop_controller(self)
            self._sync_runtime_instance()
            chatweb.SERVICE = svc

            httpd = chatweb.ThreadingHTTPServer(('127.0.0.1', self.port), chatweb.Handler)
            httpd.daemon_threads = True
            server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            server_thread.start()

            self.chat_service['pid'] = os.getpid()
            self.chat_service['identity'] = process_identity(os.getpid())
            self.chat_service['owned'] = True
            self.chat_service['status'] = 'in_process'
            self.chat_service['server'] = httpd
            self.chat_service['service'] = svc

            time.sleep(0.2)
            self._step(3, 'done', f'Morgan 服务已启动 (端口 {self.port}，回环监听，由本窗口管理)')
            return True
        except Exception as exc:  # noqa: BLE001
            self._step(3, 'error', f'聊天服务启动失败: {exc}')
            self._show_error('聊天服务失败', str(exc))
            # Clean up started model if owned
            if self.model_service.get('owned'):
                self._cleanup_owned_model()
            return False

    # ----------------------------------------------------------------------- #
    # Main Init Sequence
    # ----------------------------------------------------------------------- #
    def _run_init(self, attempt_id: int) -> None:
        try:
            time.sleep(0.2)
            with self._state_lock:
                if self.init_attempt_id != attempt_id or self.init_state in ('shutting_down', 'exited'):
                    return

            if not self._check_environment():
                with self._state_lock:
                    if self.init_attempt_id == attempt_id and self.init_state == 'initializing':
                        self.init_state = 'failed'
                return

            with self._state_lock:
                if self.init_attempt_id != attempt_id or self.init_state in ('shutting_down', 'exited'):
                    return

            if not self._check_and_start_model(attempt_id):
                with self._state_lock:
                    if self.init_attempt_id == attempt_id and self.init_state == 'initializing':
                        self.init_state = 'failed'
                return

            with self._state_lock:
                if self.init_attempt_id != attempt_id or self.init_state in ('shutting_down', 'exited'):
                    return

            if not self._check_and_start_chat(attempt_id):
                with self._state_lock:
                    if self.init_attempt_id == attempt_id and self.init_state == 'initializing':
                        self.init_state = 'failed'
                return

            with self._state_lock:
                if self.init_attempt_id != attempt_id or self.init_state in ('shutting_down', 'exited'):
                    return
                self.init_state = 'ready'

            self._step(4, 'done', '全部服务就绪，正在进入聊天页面...')
            time.sleep(0.3)

            # Persist state
            self._save_state()

            # Navigate window to chat page
            chat_url = self.chat_service['url']
            if self.window:
                self.window.load_url(chat_url)
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                if self.init_attempt_id == attempt_id and self.init_state == 'initializing':
                    self.init_state = 'failed'
            self._show_error('初始化异常', str(exc))

    def _save_state(self) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            state = {
                'desktop': {
                    'pid': os.getpid(),
                    'identity': process_identity(os.getpid()),
                    'hwnd': self.hwnd,
                    'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                },
                'model_service': {
                    'pid': self.model_service.get('pid'),
                    'owned': self.model_service.get('owned'),
                    'port': self.model_service.get('port'),
                    'base_url': self.model_service.get('base_url'),
                    'status': self.model_service.get('status'),
                },
                'chat_service': {
                    'pid': self.chat_service.get('pid'),
                    'owned': self.chat_service.get('owned'),
                    'port': self.chat_service.get('port'),
                    'url': self.chat_service.get('url'),
                    'status': self.chat_service.get('status'),
                }
            }
            STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                                  encoding='utf-8')
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------------- #
    # Window Closing & Cleanup
    # ----------------------------------------------------------------------- #
    def on_closing(self) -> None:
        with self._state_lock:
            if self.shutting_down:
                return
            self.shutting_down = True
            self.init_cancelled = True
            self.init_state = 'shutting_down'

        # Round 40: record the real timeline. "Closing the window" and "the
        # process exiting" are different events, and the gap between them is
        # exactly what an in-flight search decides.
        close_t0 = time.monotonic()
        self.close_timeline = {'close_requested_at': chatstore.now()}
        print(f"[close] 关闭动作发出 {self.close_timeline['close_requested_at']}")

        # 1. Clean up owned model launcher and actual model process
        self._cleanup_owned_model()

        # 2. Save window bounds
        if self.window:
            try:
                x = self.window.x
                y = self.window.y
                w = self.window.width
                h = self.window.height
                save_window_config(x, y, w, h, False)
            except Exception:  # noqa: BLE001
                pass

        # 3. Stop chat generation and safely close database
        if self.chat_service.get('owned'):
            svc = self.chat_service.get('service')
            if svc:
                try:
                    drain_res = svc.cancel_and_drain(timeout_s=3.0)
                    # The search pool is a module-level singleton. Close it, and
                    # wait only briefly: `on_closing` runs before the window is
                    # destroyed, so a long wait here freezes the window (measured:
                    # a 12s drain delayed window close by 13.1s). Queued searches
                    # are cancelled; a search already on the wire is reported and
                    # finishes while the process tears down.
                    try:
                        pool_res = svc.shutdown_search_pool(
                            wait=False, timeout_s=SEARCH_DRAIN_TIMEOUT_S,
                            drain=True)
                        self.close_timeline['pool'] = pool_res
                        print(f"[close] 搜索池：取消排队 {pool_res['cancelled_queued']}，"
                              f"在途 {pool_res['still_running']}，"
                              f"等待 {pool_res['waited_s']}s，"
                              f"已排空={pool_res['drained']}")
                    except Exception as pool_err:  # noqa: BLE001
                        print(f"[close] 搜索池关闭异常: {pool_err}")
                    if drain_res.get('drained'):
                        svc.store.close()
                        self.close_timeline['store_closed'] = True
                    else:
                        self.close_timeline['store_closed'] = False
                        print(f"[close] ChatService 排空超时 ({drain_res})；保留数据库安全。")
                except Exception:  # noqa: BLE001
                    pass
            httpd = self.chat_service.get('server')
            if httpd:
                try:
                    httpd.shutdown()
                    httpd.server_close()
                except Exception:  # noqa: BLE001
                    pass
        elif self.chat_service.get('status') == 'reused':
            try:
                stop_req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/api/chat/stop", data=b'{}',
                    headers={'Content-Type': 'application/json'}, method='POST')
                with urllib.request.urlopen(stop_req, timeout=1.0) as _:
                    pass
            except Exception:  # noqa: BLE001
                pass

        # 4. Remove state file
        try:
            if STATE_PATH.is_file():
                STATE_PATH.unlink()
        except Exception:  # noqa: BLE001
            pass

        # 5. Release Mutex cleanly
        if self.mutex_handle:
            try:
                k32.ReleaseMutex(self.mutex_handle)
                k32.CloseHandle(self.mutex_handle)
                self.mutex_handle = None
            except Exception:  # noqa: BLE001
                pass

        self.close_timeline['on_closing_returned_at'] = chatstore.now()
        self.close_timeline['on_closing_elapsed_s'] = round(
            time.monotonic() - close_t0, 3)
        print(f"[close] on_closing 耗时 {self.close_timeline['on_closing_elapsed_s']}s")
        try:
            tl_path = LOG_DIR / 'desktop_close_timeline.json'
            tl_path.write_text(json.dumps(self.close_timeline, ensure_ascii=False,
                                          indent=2), encoding='utf-8')
        except Exception:  # noqa: BLE001
            pass

        with self._state_lock:
            self.init_state = 'exited'

    def on_loaded(self) -> None:
        """Called by webview when a page completes loading."""
        with self._state_lock:
            if not self.hwnd:
                if self.window and hasattr(self.window, 'native') and self.window.native:
                    try:
                        self.hwnd = int(self.window.native.Handle.ToInt64())
                    except Exception:  # noqa: BLE001
                        pass
                if not self.hwnd:
                    self.hwnd = find_window_for_pid(os.getpid()) or u32.FindWindowW(None, 'Morgan') or u32.FindWindowW(None, '本地聊天')

            if self.init_state in ('initializing', 'ready', 'shutting_down', 'exited'):
                # Subsequent page loads (e.g. entering chat page via load_url, or user reload)
                # MUST NOT trigger initialization or change service ownership!
                return

            self.init_state = 'initializing'
            self.init_attempt_id += 1
            attempt_id = self.init_attempt_id

        self.init_cancelled = False
        self._init_thread = threading.Thread(target=self._run_init, args=(attempt_id,), daemon=True)
        self._init_thread.start()


# --------------------------------------------------------------------------- #
# Entry Point
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Morgan 桌面端 (pywebview + WebView2)')
    parser.add_argument('--port', type=int, default=8771, help='聊天服务端口 (默认: 8771)')
    parser.add_argument('--model-port', type=int, default=None, help='模型服务端口')
    parser.add_argument('--model-config', type=str, default=None, help='模型配置文件路径覆盖')
    parser.add_argument('--profile', type=str, default=None, help='启动时优先加载的 profile_id')
    parser.add_argument('--db-path', type=str, default=None, help='指定数据库路径 (默认: 生产数据库)')
    parser.add_argument('--minimized', action='store_true', help='启动时最小化')
    args = parser.parse_args(argv)

    # 1. Single Instance Check via Windows Named Mutex
    expected_db = Path(args.db_path).resolve() if args.db_path else None
    is_primary, mutex_handle = acquire_single_instance_mutex(PROJECT_ROOT)
    if not is_primary:
        # Mutex actively held by another live instance!
        is_running, activated = try_activate_existing_instance()
        if activated:
            print('已有「Morgan」实例运行中，已唤起并置前已有窗口。')
        else:
            print('已有「Morgan」实例运行中（启动中或无法置前），退出第二实例。')
        return 0

    # 2. Window Bounds
    x, y, w, h, maximized = get_safe_initial_bounds()

    # 3. Create Controller & Window
    ctrl = DesktopController(port=args.port, model_port=args.model_port,
                             expected_store_path=expected_db,
                             model_config_path=args.model_config,
                             launch_profile_override=args.profile)
    ctrl.mutex_handle = mutex_handle

    # Ensure pywebview uses Edge Chromium (WebView2)
    os.environ['PYWEBVIEW_GUI'] = 'edgechromium'

    icon_str = str(ICON_PATH) if ICON_PATH.is_file() else None
    js_api = DesktopJsApi(ctrl)
    window = webview.create_window(
        title='Morgan',
        html=LOADING_HTML,
        js_api=js_api,
        width=w,
        height=h,
        x=x,
        y=y,
        min_size=(700, 480),
        resizable=True,
        maximized=maximized
    )
    ctrl.window = window

    # Bind events
    window.events.loaded += ctrl.on_loaded
    window.events.closing += ctrl.on_closing

    try:
        webview.start(debug=False)
    finally:
        ctrl.on_closing()

    return 0


if __name__ == '__main__':
    for attr in ('stdout', 'stderr'):
        st = getattr(sys, attr)
        if hasattr(st, 'reconfigure'):
            try:
                st.reconfigure(encoding='utf-8', errors='replace')
            except Exception:  # noqa: BLE001
                pass
    sys.exit(main())
