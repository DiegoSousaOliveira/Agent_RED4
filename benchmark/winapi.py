"""Thin ctypes wrappers around the Win32 calls the benchmark needs.

Private ``WinDLL`` instances are used on purpose: setting ``argtypes`` on the shared
``ctypes.windll.user32`` would leak into the libraries being benchmarked.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass

import cv2
import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HWND = wintypes.HWND
HDC = wintypes.HDC
HGDIOBJ = wintypes.HGDIOBJ

user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = HWND
user32.IsIconic.argtypes = [HWND]
user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(wintypes.POINT)]
user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.GetDC.argtypes = [HWND]
user32.GetDC.restype = HDC
user32.ReleaseDC.argtypes = [HWND, HDC]
user32.PrintWindow.argtypes = [HWND, HDC, wintypes.UINT]
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = HWND
user32.DestroyWindow.argtypes = [HWND]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.UpdateWindow.argtypes = [HWND]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteDC.argtypes = [HDC]
gdi32.GetDeviceCaps.argtypes = [HDC, ctypes.c_int]
gdi32.GetDIBits.argtypes = [HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.LPVOID, wintypes.UINT]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = wintypes.HANDLE(-4)
HWND_TOPMOST = HWND(-1)
HWND_NOTOPMOST = HWND(-2)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000008, 0x00000080, 0x08000000
SW_SHOWNOACTIVATE = 4
PM_REMOVE = 0x0001
PW_CLIENTONLY, PW_RENDERFULLCONTENT = 0x1, 0x2
VREFRESH = 116
SM_CXSCREEN, SM_CYSCREEN = 0, 1
SPI_GETWORKAREA = 0x0030
ERROR_CLASS_ALREADY_EXISTS = 1410

# Color of the window used to cover the game in the occlusion test (BGR, as frames are)
OVERLAY_BGR = (255, 0, 255)


class WindowNotFoundError(RuntimeError):
    pass


def enable_dpi_awareness() -> None:
    """Make every coordinate and capture use physical pixels instead of scaled ones."""
    if not user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
        user32.SetProcessDPIAware()  # Already set by someone else, or an older Windows


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def intersect(self, other: Rect) -> Rect:
        return Rect(max(self.left, other.left), max(self.top, other.top),
                    min(self.right, other.right), min(self.bottom, other.bottom))

    def relative_to(self, origin: Rect) -> Rect:
        """This rect in the coordinate system whose (0, 0) is ``origin``'s top-left corner."""
        return Rect(self.left - origin.left, self.top - origin.top,
                    self.right - origin.left, self.bottom - origin.top)

    def shrink(self, fraction: float) -> Rect:
        """A centered rect covering ``fraction`` of this one's width and height."""
        dx = round(self.width * (1 - fraction) / 2)
        dy = round(self.height * (1 - fraction) / 2)
        return Rect(self.left + dx, self.top + dy, self.right - dx, self.bottom - dy)

    def slices(self) -> tuple[slice, slice]:
        """Row and column slices to cut this rect out of a numpy image."""
        return slice(self.top, self.bottom), slice(self.left, self.right)


@dataclass(frozen=True)
class GameWindow:
    hwnd: int
    title: str
    client: Rect  # Game image area, in screen coordinates
    screen: Rect  # Primary monitor
    work_area: Rect  # Primary monitor minus the taskbar

    @property
    def visible_client(self) -> Rect:
        """Part of the game image that is on the monitor and not under the taskbar."""
        return self.client.intersect(self.work_area)


def find_window(title: str) -> GameWindow:
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        raise WindowNotFoundError(f"Janela '{title}' não encontrada. O jogo está aberto?")
    if user32.IsIconic(hwnd):
        raise WindowNotFoundError(f"Janela '{title}' está minimizada; nenhuma API consegue capturá-la assim.")

    client_size = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client_size))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    work = wintypes.RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0)

    return GameWindow(
        hwnd=hwnd,
        title=title,
        client=Rect(origin.x, origin.y, origin.x + client_size.right, origin.y + client_size.bottom),
        screen=Rect(0, 0, user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)),
        work_area=Rect(work.left, work.top, work.right, work.bottom),
    )


def bring_to_top(hwnd: int) -> None:
    """Put the window above the others without giving it keyboard focus."""
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


def primary_display() -> tuple[str, int]:
    """Name of the GPU driving the primary monitor and the monitor refresh rate in Hz."""
    device = DISPLAY_DEVICEW(cb=ctypes.sizeof(DISPLAY_DEVICEW))
    user32.EnumDisplayDevicesW(None, 0, ctypes.byref(device), 0)
    screen_dc = user32.GetDC(None)
    try:
        refresh_hz = gdi32.GetDeviceCaps(screen_dc, VREFRESH)
    finally:
        user32.ReleaseDC(None, screen_dc)
    return device.DeviceString, refresh_hz


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class WindowPrinter:
    """Copies a window's client area with PrintWindow, which works even when it is covered.

    The GDI objects and the pixel buffer are created once and reused on every call.
    """

    def __init__(self, hwnd: int, width: int, height: int) -> None:
        self._hwnd = hwnd
        self._window_dc = user32.GetDC(hwnd)
        self._memory_dc = gdi32.CreateCompatibleDC(self._window_dc)
        self._bitmap = gdi32.CreateCompatibleBitmap(self._window_dc, width, height)
        gdi32.SelectObject(self._memory_dc, self._bitmap)
        # Negative height = rows top-down, 32 bits per pixel = BGRA
        self._header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        self._bgra = np.empty((height, width, 4), dtype=np.uint8)

    def grab(self) -> np.ndarray:
        if not user32.PrintWindow(self._hwnd, self._memory_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT):
            raise ctypes.WinError(ctypes.get_last_error())
        height = self._bgra.shape[0]
        if gdi32.GetDIBits(self._memory_dc, self._bitmap, 0, height, self._bgra.ctypes.data,
                           ctypes.byref(self._header), 0) != height:
            raise ctypes.WinError(ctypes.get_last_error())
        return cv2.cvtColor(self._bgra, cv2.COLOR_BGRA2BGR)

    def close(self) -> None:
        gdi32.DeleteObject(self._bitmap)
        gdi32.DeleteDC(self._memory_dc)
        user32.ReleaseDC(self._hwnd, self._window_dc)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", wintypes.LPVOID),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM

_OVERLAY_CLASS = "AgentRED4BenchmarkOverlay"
_overlay_class_registered = False


def _register_overlay_class() -> None:
    global _overlay_class_registered
    if _overlay_class_registered:
        return
    blue, green, red = OVERLAY_BGR
    window_class = WNDCLASSW(
        # DefWindowProc paints the class brush, so no Python callback is needed
        lpfnWndProc=ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value,
        hInstance=kernel32.GetModuleHandleW(None),
        hbrBackground=gdi32.CreateSolidBrush(red | green << 8 | blue << 16),
        lpszClassName=_OVERLAY_CLASS,
    )
    if not user32.RegisterClassW(ctypes.byref(window_class)) and ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
        raise ctypes.WinError(ctypes.get_last_error())
    _overlay_class_registered = True


class Overlay:
    """Solid-color, always-on-top window that never takes focus. Used to cover the game."""

    def __init__(self, rect: Rect) -> None:
        self.rect = rect
        self._hwnd: int | None = None

    def __enter__(self) -> Overlay:
        _register_overlay_class()
        self._hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, _OVERLAY_CLASS, None, WS_POPUP,
            self.rect.left, self.rect.top, self.rect.width, self.rect.height,
            None, None, kernel32.GetModuleHandleW(None), None,
        )
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
        user32.UpdateWindow(self._hwnd)
        return self

    def pump(self, seconds: float = 0.0) -> None:
        """Process window messages (keeps the overlay painted and responsive) for a while."""
        message = wintypes.MSG()
        deadline = time.perf_counter() + seconds
        while True:
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            if time.perf_counter() >= deadline:
                return
            time.sleep(0.005)

    def __exit__(self, *exc_info: object) -> None:
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
            self.pump()
