"""Small ctypes wrapper for the Windows APIs used by the tracker."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from datetime import datetime

from tracker_core import ActivitySample


if os.name != "nt":
    raise RuntimeError("The foreground activity tracker only runs on Windows.")


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2
ERROR_ALREADY_EXISTS = 183

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetTickCount64.argtypes = []
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.GetCurrentProcessId.argtypes = []
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
kernel32.ReleaseMutex.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
user32.GetUserObjectInformationW.restype = wintypes.BOOL
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int


def foreground_executable() -> str | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    process_id = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)) or not process_id.value:
        return None
    if process_id.value == os.getpid():
        return None

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value).name or None
    finally:
        kernel32.CloseHandle(handle)


def last_input_monotonic(reference_monotonic: float | None = None) -> float | None:
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    tick64_ms = int(kernel32.GetTickCount64())
    tick32_ms = tick64_ms & 0xFFFFFFFF
    idle_ms = (tick32_ms - int(info.dwTime)) & 0xFFFFFFFF
    # The modulo subtraction handles the 32-bit tick wrap. Reject impossible
    # or suspiciously stale results rather than creating a future timestamp.
    idle_seconds = idle_ms / 1000.0
    now = (int(kernel32.GetTickCount64()) / 1000.0) if reference_monotonic is None else reference_monotonic
    if idle_seconds > (0xFFFFFFFF / 1000.0):
        return None
    return now - idle_seconds


def desktop_locked() -> bool | None:
    """Return True on a non-Default input desktop, None if detection failed."""
    handle = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not handle:
        return None
    try:
        required = wintypes.DWORD()
        user32.GetUserObjectInformationW(handle, UOI_NAME, None, 0, ctypes.byref(required))
        if required.value <= 0:
            return None
        buffer = ctypes.create_unicode_buffer(required.value // ctypes.sizeof(ctypes.c_wchar) + 1)
        if not user32.GetUserObjectInformationW(
            handle,
            UOI_NAME,
            buffer,
            ctypes.sizeof(buffer),
            ctypes.byref(required),
        ):
            return None
        return buffer.value.casefold() != "default"
    finally:
        user32.CloseDesktop(handle)


def windows_monotonic_seconds() -> float:
    """Return the same monotonic clock domain used for activity samples."""
    return int(kernel32.GetTickCount64()) / 1000.0


_LOCAL_TIMEZONE = None


_WINDOWS_TZ_TO_IANA: dict[str, str] = {
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "GMT Standard Time": "Europe/London",
    "Central European Standard Time": "Europe/Berlin",
}


def local_timezone():
    """Return the Windows IANA timezone when available (DST-aware)."""
    global _LOCAL_TIMEZONE
    if _LOCAL_TIMEZONE is not None:
        return _LOCAL_TIMEZONE
    try:
        from zoneinfo import ZoneInfo
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation",
        ) as key:
            name, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
        iana_name = _WINDOWS_TZ_TO_IANA.get(str(name), str(name))
        _LOCAL_TIMEZONE = ZoneInfo(iana_name)
    except Exception:
        _LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo
    return _LOCAL_TIMEZONE


def local_wall_time() -> datetime:
    return datetime.now(local_timezone())


def take_sample() -> ActivitySample:
    """Collect one best-effort sample; failed fields result in no credit."""
    mono = windows_monotonic_seconds()
    wall = local_wall_time()
    try:
        executable = foreground_executable()
    except Exception:
        executable = None
    try:
        input_time = last_input_monotonic(mono)
    except Exception:
        input_time = None
    try:
        locked = desktop_locked()
    except Exception:
        locked = None
    return ActivitySample(mono, wall, executable, input_time, locked)


def acquire_single_instance_mutex() -> tuple[int | None, bool]:
    """Create a per-session mutex. Return (handle, is_already_running)."""
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, True, "Local\\WindowsForegroundActivityTracker")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    return int(handle), already_running


def release_single_instance_mutex(handle: int | None) -> None:
    if handle:
        kernel32.ReleaseMutex(wintypes.HANDLE(handle))
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def show_windows_error(title: str, message: str) -> None:
    user32.MessageBoxW(None, message, title, 0x10)
