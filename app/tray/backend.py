"""Win32 ctypes backend and protocol abstraction for system tray operations."""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Protocol, runtime_checkable

from app.tray.models import (
    NIF_ICON,
    NIF_MESSAGE,
    NIF_TIP,
    NIM_ADD,
    NIM_DELETE,
    NIM_MODIFY,
    NIM_SETVERSION,
    NOTIFYICON_VERSION_4,
    WM_TRAY_CALLBACK,
    sanitize_and_truncate_tooltip,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class TrayBackend(Protocol):
    """Protocol declaring OS-level system tray and window primitives."""

    def is_available(self) -> bool:
        """Return True if this backend is supported and available on the current platform."""
        ...

    def get_current_thread_id(self) -> int:
        """Return the OS thread ID of the calling thread."""
        ...

    def create_tray_window(self, class_name: str, window_title: str, wndproc: Callable[..., int]) -> int:
        """Register a window class and create an invisible window. Returns native HWND integer."""
        ...

    def destroy_tray_window(self, hwnd: int) -> bool:
        """Destroy the window and unregister its class."""
        ...

    def def_window_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        """Call default window procedure."""
        ...

    def register_taskbar_created_message(self) -> int:
        """Register and return the 'TaskbarCreated' message ID."""
        ...

    def add_icon(self, hwnd: int, icon_id: int, hicon: int | None, tooltip: str) -> bool:
        """Add the notification icon to the taskbar area via Shell_NotifyIconW(NIM_ADD)."""
        ...

    def set_version(self, hwnd: int, icon_id: int, version: int = NOTIFYICON_VERSION_4) -> bool:
        """Set notification icon version via Shell_NotifyIconW(NIM_SETVERSION)."""
        ...

    def modify_icon(self, hwnd: int, icon_id: int, hicon: int | None = None, tooltip: str | None = None) -> bool:
        """Modify existing tray icon via Shell_NotifyIconW(NIM_MODIFY)."""
        ...

    def delete_icon(self, hwnd: int, icon_id: int) -> bool:
        """Delete tray icon via Shell_NotifyIconW(NIM_DELETE)."""
        ...

    def load_default_icon(self) -> int:
        """Return a handle to the system default application icon."""
        ...

    def destroy_icon(self, hicon: int) -> bool:
        """Destroy custom icon handle."""
        ...

    def post_message(self, hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
        """Post a message to the specified window's message queue."""
        ...

    def post_thread_quit(self, thread_id: int) -> bool:
        """Post WM_QUIT to the specified thread's queue."""
        ...

    def create_msg(self) -> Any:
        """Create a native MSG structure instance."""
        ...

    def get_message(self, msg: Any) -> int:
        """Retrieve message from thread queue (blocks until arrival). Returns >0 for message, 0 for WM_QUIT, -1 on error."""
        ...

    def translate_message(self, msg: Any) -> None:
        """Translate virtual-key messages."""
        ...

    def dispatch_message(self, msg: Any) -> int:
        """Dispatch message to the target window procedure."""
        ...

    def create_popup_menu(self) -> int:
        """Create a native popup menu handle (HMENU)."""
        ...

    def destroy_menu(self, hmenu: int) -> bool:
        """Destroy a native popup menu handle."""
        ...

    def append_menu_item(self, hmenu: int, flags: int, item_id: int, text: str) -> bool:
        """Append an item to a native menu."""
        ...

    def append_submenu(self, hmenu: int, flags: int, submenu_handle: int, text: str) -> bool:
        """Append a submenu to a native menu."""
        ...

    def append_separator(self, hmenu: int) -> bool:
        """Append a separator to a native menu."""
        ...

    def set_foreground_window(self, hwnd: int) -> bool:
        """Bring window to foreground for popup menu focus handling."""
        ...

    def track_popup_menu(self, hmenu: int, x: int, y: int, hwnd: int) -> bool:
        """Display native context menu at specified coordinates."""
        ...

    def get_cursor_pos(self) -> tuple[int, int]:
        """Return current cursor position (x, y)."""
        ...

    def get_last_error(self) -> int:
        """Return the last Win32 error code."""
        ...


class Win32TrayBackend:
    """Production Win32 system tray backend using standard-library ctypes.

    Guarantees:
        - Lazy DLL loading: user32, kernel32, and shell32 loaded on demand.
        - Pointer-sized WPARAM (c_size_t) and LPARAM (c_ssize_t) compatibility on 32-bit and 64-bit Windows.
        - Strict NOTIFYICONDATAW structure layout matching Windows 10 & 11 SDK.
        - Non-Windows import safety: reports is_available() == False on Linux/macOS.
    """

    def __init__(self) -> None:
        self._loaded = False
        self._user32: Any = None
        self._kernel32: Any = None
        self._shell32: Any = None
        self._msg_class: Any = None
        self._point_class: Any = None
        self._nid_class: Any = None
        self._wndclass_class: Any = None
        self._wndproc_factory: Any = None
        self._registered_classes: dict[str, Any] = {}

    def is_available(self) -> bool:
        """Return True if running on Windows and Win32 DLLs load successfully."""
        if sys.platform != "win32":
            return False
        try:
            self._ensure_loaded()
            return True
        except Exception as exc:
            logger.debug("Win32TrayBackend unavailable (%s): %s", type(exc).__name__, exc)
            return False

    def _ensure_loaded(self) -> None:
        """Lazily load Windows DLLs and declare Win32 structures and function signatures."""
        if self._loaded:
            return

        if sys.platform != "win32":
            raise RuntimeError(f"Win32TrayBackend is not supported on platform '{sys.platform}'.")

        import ctypes
        from ctypes import (
            POINTER,
            Structure,
            c_byte,
            c_int,
            c_long,
            c_size_t,
            c_ssize_t,
            c_uint,
            c_ulong,
            c_ushort,
            c_void_p,
            c_wchar,
            c_wchar_p,
        )

        class POINT(Structure):
            _fields_ = [
                ("x", c_long),
                ("y", c_long),
            ]

        class MSG(Structure):
            _fields_ = [
                ("hwnd", c_void_p),
                ("message", c_uint),
                ("wParam", c_size_t),
                ("lParam", c_ssize_t),
                ("time", c_ulong),
                ("pt", POINT),
            ]

        class GUID(Structure):
            _fields_ = [
                ("Data1", c_ulong),
                ("Data2", c_ushort),
                ("Data3", c_ushort),
                ("Data4", c_byte * 8),
            ]

        class NOTIFYICONDATAW(Structure):
            _fields_ = [
                ("cbSize", c_ulong),
                ("hWnd", c_void_p),
                ("uID", c_uint),
                ("uFlags", c_uint),
                ("uCallbackMessage", c_uint),
                ("hIcon", c_void_p),
                ("szTip", c_wchar * 128),
                ("dwState", c_ulong),
                ("dwStateMask", c_ulong),
                ("szInfo", c_wchar * 256),
                ("uVersion", c_uint),
                ("szInfoTitle", c_wchar * 64),
                ("dwInfoFlags", c_ulong),
                ("guidItem", GUID),
                ("hBalloonIcon", c_void_p),
            ]

        WNDPROC = ctypes.WINFUNCTYPE(c_ssize_t, c_void_p, c_uint, c_size_t, c_ssize_t)

        class WNDCLASSEXW(Structure):
            _fields_ = [
                ("cbSize", c_uint),
                ("style", c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", c_int),
                ("cbWndExtra", c_int),
                ("hInstance", c_void_p),
                ("hIcon", c_void_p),
                ("hCursor", c_void_p),
                ("hbrBackground", c_void_p),
                ("lpszMenuName", c_wchar_p),
                ("lpszClassName", c_wchar_p),
                ("hIconSm", c_void_p),
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        # user32 signatures
        user32.RegisterClassExW.argtypes = [POINTER(WNDCLASSEXW)]
        user32.RegisterClassExW.restype = c_ushort

        user32.UnregisterClassW.argtypes = [c_wchar_p, c_void_p]
        user32.UnregisterClassW.restype = c_int

        user32.CreateWindowExW.argtypes = [
            c_ulong,
            c_wchar_p,
            c_wchar_p,
            c_ulong,
            c_int,
            c_int,
            c_int,
            c_int,
            c_void_p,
            c_void_p,
            c_void_p,
            c_void_p,
        ]
        user32.CreateWindowExW.restype = c_void_p

        user32.DestroyWindow.argtypes = [c_void_p]
        user32.DestroyWindow.restype = c_int

        user32.DefWindowProcW.argtypes = [c_void_p, c_uint, c_size_t, c_ssize_t]
        user32.DefWindowProcW.restype = c_ssize_t

        user32.GetMessageW.argtypes = [POINTER(MSG), c_void_p, c_uint, c_uint]
        user32.GetMessageW.restype = c_int

        user32.TranslateMessage.argtypes = [POINTER(MSG)]
        user32.TranslateMessage.restype = c_int

        user32.DispatchMessageW.argtypes = [POINTER(MSG)]
        user32.DispatchMessageW.restype = c_ssize_t

        user32.PostMessageW.argtypes = [c_void_p, c_uint, c_size_t, c_ssize_t]
        user32.PostMessageW.restype = c_int

        user32.PostThreadMessageW.argtypes = [c_ulong, c_uint, c_size_t, c_ssize_t]
        user32.PostThreadMessageW.restype = c_int

        user32.PostQuitMessage.argtypes = [c_int]
        user32.PostQuitMessage.restype = None

        user32.RegisterWindowMessageW.argtypes = [c_wchar_p]
        user32.RegisterWindowMessageW.restype = c_uint

        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = c_void_p

        user32.DestroyMenu.argtypes = [c_void_p]
        user32.DestroyMenu.restype = c_int

        user32.AppendMenuW.argtypes = [c_void_p, c_uint, c_size_t, c_wchar_p]
        user32.AppendMenuW.restype = c_int

        user32.SetForegroundWindow.argtypes = [c_void_p]
        user32.SetForegroundWindow.restype = c_int

        user32.TrackPopupMenuEx.argtypes = [c_void_p, c_uint, c_int, c_int, c_void_p, c_void_p]
        user32.TrackPopupMenuEx.restype = c_int

        user32.GetCursorPos.argtypes = [POINTER(POINT)]
        user32.GetCursorPos.restype = c_int

        user32.LoadIconW.argtypes = [c_void_p, c_void_p]
        user32.LoadIconW.restype = c_void_p

        user32.DestroyIcon.argtypes = [c_void_p]
        user32.DestroyIcon.restype = c_int

        # kernel32 signatures
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = c_ulong

        kernel32.GetModuleHandleW.argtypes = [c_wchar_p]
        kernel32.GetModuleHandleW.restype = c_void_p

        # shell32 signatures
        shell32.Shell_NotifyIconW.argtypes = [c_ulong, POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = c_int

        self._user32 = user32
        self._kernel32 = kernel32
        self._shell32 = shell32
        self._msg_class = MSG
        self._point_class = POINT
        self._nid_class = NOTIFYICONDATAW
        self._wndclass_class = WNDCLASSEXW
        self._wndproc_factory = WNDPROC
        self._loaded = True

    def get_current_thread_id(self) -> int:
        self._ensure_loaded()
        return int(self._kernel32.GetCurrentThreadId())

    def create_tray_window(self, class_name: str, window_title: str, wndproc: Callable[..., int]) -> int:
        self._ensure_loaded()
        import ctypes

        h_instance = self._kernel32.GetModuleHandleW(None)
        c_wndproc = self._wndproc_factory(wndproc)

        wndclass = self._wndclass_class()
        wndclass.cbSize = ctypes.sizeof(self._wndclass_class)
        wndclass.style = 0
        wndclass.lpfnWndProc = c_wndproc
        wndclass.cbClsExtra = 0
        wndclass.cbWndExtra = 0
        wndclass.hInstance = h_instance
        wndclass.hIcon = None
        wndclass.hCursor = None
        wndclass.hbrBackground = None
        wndclass.lpszMenuName = None
        wndclass.lpszClassName = class_name
        wndclass.hIconSm = None

        atom = self._user32.RegisterClassExW(ctypes.byref(wndclass))
        if atom == 0:
            err = self.get_last_error()
            # 1410 = ERROR_CLASS_ALREADY_EXISTS
            if err != 1410:
                raise RuntimeError(f"Failed to register tray window class '{class_name}' (Win32 error {err}).")

        # Retain reference to c_wndproc so GC does not collect callback
        self._registered_classes[class_name] = (wndclass, c_wndproc)

        hwnd = self._user32.CreateWindowExW(
            0,
            class_name,
            window_title,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            h_instance,
            None,
        )
        if not hwnd:
            raise RuntimeError(f"Failed to create tray window '{window_title}' (Win32 error {self.get_last_error()}).")

        return int(hwnd)

    def destroy_tray_window(self, hwnd: int) -> bool:
        self._ensure_loaded()
        ret = self._user32.DestroyWindow(ctypes.c_void_p(hwnd))
        return ret != 0

    def def_window_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        self._ensure_loaded()
        import ctypes
        return int(self._user32.DefWindowProcW(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(msg),
            ctypes.c_size_t(wparam),
            ctypes.c_ssize_t(lparam),
        ))

    def register_taskbar_created_message(self) -> int:
        self._ensure_loaded()
        return int(self._user32.RegisterWindowMessageW("TaskbarCreated"))

    def add_icon(self, hwnd: int, icon_id: int, hicon: int | None, tooltip: str) -> bool:
        self._ensure_loaded()
        import ctypes

        nid = self._nid_class()
        nid.cbSize = ctypes.sizeof(self._nid_class)
        nid.hWnd = ctypes.c_void_p(hwnd)
        nid.uID = ctypes.c_uint(icon_id)
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY_CALLBACK
        nid.hIcon = ctypes.c_void_p(hicon or self.load_default_icon())
        nid.szTip = sanitize_and_truncate_tooltip(tooltip, max_code_units=127)

        ret = self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        return ret != 0

    def set_version(self, hwnd: int, icon_id: int, version: int = NOTIFYICON_VERSION_4) -> bool:
        self._ensure_loaded()
        import ctypes

        nid = self._nid_class()
        nid.cbSize = ctypes.sizeof(self._nid_class)
        nid.hWnd = ctypes.c_void_p(hwnd)
        nid.uID = ctypes.c_uint(icon_id)
        nid.uVersion = ctypes.c_uint(version)

        ret = self._shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
        return ret != 0

    def modify_icon(self, hwnd: int, icon_id: int, hicon: int | None = None, tooltip: str | None = None) -> bool:
        self._ensure_loaded()
        import ctypes

        nid = self._nid_class()
        nid.cbSize = ctypes.sizeof(self._nid_class)
        nid.hWnd = ctypes.c_void_p(hwnd)
        nid.uID = ctypes.c_uint(icon_id)
        flags = 0

        if hicon is not None:
            flags |= NIF_ICON
            nid.hIcon = ctypes.c_void_p(hicon)
        if tooltip is not None:
            flags |= NIF_TIP
            nid.szTip = sanitize_and_truncate_tooltip(tooltip, max_code_units=127)

        nid.uFlags = flags
        ret = self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        return ret != 0

    def delete_icon(self, hwnd: int, icon_id: int) -> bool:
        self._ensure_loaded()
        import ctypes

        nid = self._nid_class()
        nid.cbSize = ctypes.sizeof(self._nid_class)
        nid.hWnd = ctypes.c_void_p(hwnd)
        nid.uID = ctypes.c_uint(icon_id)

        ret = self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        return ret != 0

    def load_default_icon(self) -> int:
        self._ensure_loaded()
        import ctypes
        # IDI_APPLICATION = 32512
        hicon = self._user32.LoadIconW(None, ctypes.c_void_p(32512))
        return int(hicon) if hicon else 0

    def destroy_icon(self, hicon: int) -> bool:
        self._ensure_loaded()
        import ctypes
        ret = self._user32.DestroyIcon(ctypes.c_void_p(hicon))
        return ret != 0

    def post_message(self, hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
        self._ensure_loaded()
        import ctypes
        ret = self._user32.PostMessageW(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(msg),
            ctypes.c_size_t(wparam),
            ctypes.c_ssize_t(lparam),
        )
        return ret != 0

    def post_thread_quit(self, thread_id: int) -> bool:
        self._ensure_loaded()
        from app.tray.models import WM_QUIT
        ret = self._user32.PostThreadMessageW(int(thread_id), WM_QUIT, 0, 0)
        return ret != 0

    def create_msg(self) -> Any:
        self._ensure_loaded()
        return self._msg_class()

    def get_message(self, msg: Any) -> int:
        self._ensure_loaded()
        import ctypes
        return int(self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0))

    def translate_message(self, msg: Any) -> None:
        self._ensure_loaded()
        import ctypes
        self._user32.TranslateMessage(ctypes.byref(msg))

    def dispatch_message(self, msg: Any) -> int:
        self._ensure_loaded()
        import ctypes
        return int(self._user32.DispatchMessageW(ctypes.byref(msg)))

    def create_popup_menu(self) -> int:
        self._ensure_loaded()
        return int(self._user32.CreatePopupMenu())

    def destroy_menu(self, hmenu: int) -> bool:
        self._ensure_loaded()
        import ctypes
        ret = self._user32.DestroyMenu(ctypes.c_void_p(hmenu))
        return ret != 0

    def append_menu_item(self, hmenu: int, flags: int, item_id: int, text: str) -> bool:
        self._ensure_loaded()
        import ctypes
        ret = self._user32.AppendMenuW(
            ctypes.c_void_p(hmenu),
            ctypes.c_uint(flags),
            ctypes.c_size_t(item_id),
            ctypes.c_wchar_p(text),
        )
        return ret != 0

    def append_submenu(self, hmenu: int, flags: int, submenu_handle: int, text: str) -> bool:
        self._ensure_loaded()
        import ctypes
        from app.tray.models import MF_POPUP
        ret = self._user32.AppendMenuW(
            ctypes.c_void_p(hmenu),
            ctypes.c_uint(flags | MF_POPUP),
            ctypes.c_size_t(submenu_handle),
            ctypes.c_wchar_p(text),
        )
        return ret != 0

    def append_separator(self, hmenu: int) -> bool:
        self._ensure_loaded()
        import ctypes
        from app.tray.models import MF_SEPARATOR
        ret = self._user32.AppendMenuW(
            ctypes.c_void_p(hmenu),
            ctypes.c_uint(MF_SEPARATOR),
            0,
            None,
        )
        return ret != 0

    def set_foreground_window(self, hwnd: int) -> bool:
        self._ensure_loaded()
        import ctypes
        ret = self._user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        return ret != 0

    def track_popup_menu(self, hmenu: int, x: int, y: int, hwnd: int) -> bool:
        self._ensure_loaded()
        import ctypes
        from app.tray.models import TPM_BOTTOMALIGN, TPM_RIGHTBUTTON
        ret = self._user32.TrackPopupMenuEx(
            ctypes.c_void_p(hmenu),
            ctypes.c_uint(TPM_RIGHTBUTTON | TPM_BOTTOMALIGN),
            ctypes.c_int(x),
            ctypes.c_int(y),
            ctypes.c_void_p(hwnd),
            None,
        )
        return ret != 0

    def get_cursor_pos(self) -> tuple[int, int]:
        self._ensure_loaded()
        import ctypes
        pt = self._point_class()
        self._user32.GetCursorPos(ctypes.byref(pt))
        return int(pt.x), int(pt.y)

    def get_last_error(self) -> int:
        import ctypes
        return ctypes.get_last_error()
