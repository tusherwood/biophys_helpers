"""
desktop_notify.py

Tiny, dependency-free attention helpers for the Tk GUIs: a system
notification sound and a taskbar flash for a window that isn't in front.
Windows-only effects (winsound / user32 via ctypes, both standard library);
elsewhere they fall back to Tk's bell or do nothing. Never raise.
"""
from __future__ import annotations

import sys
import tkinter as tk


def beep(widget: tk.Misc | None = None):
    """The system 'notification' sound (Tk's bell off Windows)."""
    try:
        if sys.platform == 'win32':
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        elif widget is not None:
            widget.bell()
    except Exception:
        pass


def _hwnd(widget: tk.Misc) -> int | None:
    try:
        return int(widget.winfo_toplevel().wm_frame(), 16)
    except (tk.TclError, ValueError):
        return None


def is_foreground(widget: tk.Misc) -> bool:
    """Whether `widget`'s window is the active one (True when unknowable)."""
    if sys.platform != 'win32':
        try:
            return widget.focus_displayof() is not None
        except tk.TclError:
            return True
    try:
        import ctypes
        hwnd = _hwnd(widget)
        return hwnd is None or ctypes.windll.user32.GetForegroundWindow() == hwnd
    except Exception:
        return True


def flash(widget: tk.Misc, count: int = 5):
    """Flash the window's taskbar button until it's brought to the front
    (Windows), unless it already is."""
    if sys.platform != 'win32' or is_foreground(widget):
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.UINT), ('hwnd', wintypes.HWND),
                        ('dwFlags', wintypes.DWORD), ('uCount', wintypes.UINT),
                        ('dwTimeout', wintypes.DWORD)]

        hwnd = _hwnd(widget)
        if hwnd is None:
            return
        FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG,
                          count, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def on_screen(x: int, y: int, root: tk.Misc | None = None) -> bool:
    """Whether screen point (x, y) lies on some monitor (Windows: any
    monitor; elsewhere: the primary screen)."""
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes
            MONITOR_DEFAULTTONULL = 0
            return bool(ctypes.windll.user32.MonitorFromPoint(
                wintypes.POINT(int(x), int(y)), MONITOR_DEFAULTTONULL))
        except Exception:
            pass
    if root is None:
        return True
    return 0 <= x < root.winfo_screenwidth() and 0 <= y < root.winfo_screenheight()
