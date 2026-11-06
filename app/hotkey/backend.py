"""Win32 ctypes backend and protocol abstraction for global hotkey operations."""

from __future__ import annotations

import logging
import sys
from typing import Any, Protocol, runtime_checkable

from app.hotkey.models import WM_HOTKEY, WM_QUIT

logger = logging.getLogger(__name__)


@runtime_checkable
class HotkeyBackend(Protocol):
    """Protocol declaring OS-level hotkey and message queue primitives."""

    def is_available(self) -> bool:
        """Return True if this backend is supported and available on the current platform."""
        ...

    def initialize_message_queue(self) -> None:
        """Force the creation of the calling thread's Windows message queue."""
        ...

    def get_current_thread_id(self) -> int:
        """Return the OS thread ID of the calling thread."""
        ...

    def register_hotkey(self, hotkey_id: int, fs_modifiers: int, vk_code: int) -> bool:
        """Register a global hotkey associated with the calling thread's message queue."""
        ...

    def unregister_hotkey(self, hotkey_id: int) -> bool:
        """Unregister a global hotkey associated with the calling thread's message queue."""
        ...

    def get_last_error(self) -> int:
        """Return the last Win32 error code."""
        ...

    def create_msg(self) -> Any:
        """Create a native MSG structure instance."""
        ...

    def get_message(self, msg: Any) -> int:
        """Retrieve a message from the thread's message queue (blocks until a message arrives).

        Returns:
            Positive integer if a message was retrieved.
            0 if WM_QUIT was retrieved.
            -1 on error.
        """
        ...

    def post_thread_quit(self, thread_id: int) -> bool:
        """Post a WM_QUIT message to the specified thread's message queue."""
        ...

    def extract_msg_details(self, msg: Any) -> tuple[int, int]:
        """Extract (message_id, wParam_hotkey_id) from an MSG structure."""
        ...


class Win32HotkeyBackend:
    """Production Win32 global hotkey backend using standard-library ctypes.

    Guarantees:
        - Lazy DLL loading: user32 and kernel32 are never loaded at module import time.
        - Pointer-sized WPARAM (c_size_t) and LPARAM (c_ssize_t) compatibility on 32-bit and 64-bit Windows.
        - WinDLL(..., use_last_error=True) with ctypes.get_last_error() for reliable error code retrieval.
        - Non-Windows import safety: instantiating or importing on Linux/macOS safely reports is_available() == False.
    """

    def __init__(self) -> None:
        self._loaded = False
        self._user32: Any = None
        self._kernel32: Any = None
        self._msg_class: Any = None
        self._point_class: Any = None

    def is_available(self) -> bool:
        """Return True if running on Windows and Win32 DLLs load successfully."""
        if sys.platform != "win32":
            return False
        try:
            self._ensure_loaded()
            return True
        except Exception as exc:
            logger.debug("Win32HotkeyBackend unavailable (%s): %s", type(exc).__name__, exc)
            return False

    def _ensure_loaded(self) -> None:
        """Lazily load Windows DLLs and declare Win32 structures and function signatures."""
        if self._loaded:
            return

        if sys.platform != "win32":
            raise RuntimeError(f"Win32HotkeyBackend is not supported on platform '{sys.platform}'.")

        import ctypes
        from ctypes import Structure, c_int, c_long, c_size_t, c_ssize_t, c_uint, c_ulong, c_void_p, POINTER

        class POINT(Structure):
            _fields_ = [
                ("x", c_long),
                ("y", c_long),
            ]

        class MSG(Structure):
            _fields_ = [
                ("hwnd", c_void_p),
                ("message", c_uint),
                ("wParam", c_size_t),   # UINT_PTR (pointer-sized unsigned)
                ("lParam", c_ssize_t),  # LONG_PTR (pointer-sized signed)
                ("time", c_ulong),
                ("pt", POINT),
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # 1. RegisterHotKey: BOOL RegisterHotKey(HWND hWnd, int id, UINT fsModifiers, UINT vk)
        user32.RegisterHotKey.argtypes = [c_void_p, c_int, c_uint, c_uint]
        user32.RegisterHotKey.restype = c_int

        # 2. UnregisterHotKey: BOOL UnregisterHotKey(HWND hWnd, int id)
        user32.UnregisterHotKey.argtypes = [c_void_p, c_int]
        user32.UnregisterHotKey.restype = c_int

        # 3. PeekMessageW: BOOL PeekMessageW(LPMSG lpMsg, HWND hWnd, UINT wMsgFilterMin, UINT wMsgFilterMax, UINT wRemoveMsg)
        user32.PeekMessageW.argtypes = [POINTER(MSG), c_void_p, c_uint, c_uint, c_uint]
        user32.PeekMessageW.restype = c_int

        # 4. GetMessageW: BOOL GetMessageW(LPMSG lpMsg, HWND hWnd, UINT wMsgFilterMin, UINT wMsgFilterMax)
        user32.GetMessageW.argtypes = [POINTER(MSG), c_void_p, c_uint, c_uint]
        user32.GetMessageW.restype = c_int

        # 5. PostThreadMessageW: BOOL PostThreadMessageW(DWORD idThread, UINT Msg, WPARAM wParam, LPARAM lParam)
        user32.PostThreadMessageW.argtypes = [c_ulong, c_uint, c_size_t, c_ssize_t]
        user32.PostThreadMessageW.restype = c_int

        # 6. GetCurrentThreadId: DWORD GetCurrentThreadId(void)
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = c_ulong

        self._user32 = user32
        self._kernel32 = kernel32
        self._msg_class = MSG
        self._point_class = POINT
        self._loaded = True

    def initialize_message_queue(self) -> None:
        """Force the Windows Shell subsystem to initialize a message queue for the calling thread."""
        self._ensure_loaded()
        import ctypes
        msg = self._msg_class()
        # PeekMessage with PM_NOREMOVE (0) forces creation of thread message queue without removing messages
        self._user32.PeekMessageW(ctypes.byref(msg), None, 0x0400, 0x0400, 0)

    def get_current_thread_id(self) -> int:
        """Return the OS thread ID of the calling thread."""
        self._ensure_loaded()
        return int(self._kernel32.GetCurrentThreadId())

    def register_hotkey(self, hotkey_id: int, fs_modifiers: int, vk_code: int) -> bool:
        """Register global hotkey associated with thread message queue (hWnd=None)."""
        self._ensure_loaded()
        ret = self._user32.RegisterHotKey(None, int(hotkey_id), int(fs_modifiers), int(vk_code))
        return ret != 0

    def unregister_hotkey(self, hotkey_id: int) -> bool:
        """Unregister global hotkey associated with thread message queue (hWnd=None)."""
        self._ensure_loaded()
        ret = self._user32.UnregisterHotKey(None, int(hotkey_id))
        return ret != 0

    def get_last_error(self) -> int:
        """Return Win32 error code captured immediately by ctypes.get_last_error()."""
        import ctypes
        return ctypes.get_last_error()

    def create_msg(self) -> Any:
        """Create a native MSG structure instance."""
        self._ensure_loaded()
        return self._msg_class()

    def get_message(self, msg: Any) -> int:
        """Retrieve message from thread queue; blocks until a message arrives."""
        self._ensure_loaded()
        import ctypes
        return int(self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0))

    def post_thread_quit(self, thread_id: int) -> bool:
        """Post WM_QUIT (0x0012) to the thread message queue."""
        self._ensure_loaded()
        ret = self._user32.PostThreadMessageW(int(thread_id), WM_QUIT, 0, 0)
        return ret != 0

    def extract_msg_details(self, msg: Any) -> tuple[int, int]:
        """Return (message_id, wParam_hotkey_id)."""
        return int(msg.message), int(msg.wParam)
