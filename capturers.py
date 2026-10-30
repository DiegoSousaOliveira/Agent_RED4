"""Game capture for the Agent RED4: Resident Evil 4 Remake window via windows-capture.

Chosen by the benchmark (benchmark/RELATORIO.md): it keeps seeing the game when other windows
cover it, returns the latest frame in ~1-2 ms without blocking the agent, receives every frame
the game draws (60/s) and, at 60 FPS, uses less CPU than PrintWindow.

    import cv2
    from capturers import WindowsCaptureStreamCapturer

    cv2.setNumThreads(1)  # OpenCV's idle thread pool spins and nearly doubles the CPU use
    with WindowsCaptureStreamCapturer() as capturer:
        frame = capturer.grab()  # numpy (1080, 1920, 3), uint8, BGR

The game can be behind other windows, but never minimized: Windows stops drawing it.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

import cv2
import numpy as np
from windows_capture import WindowsCapture

GAME_TITLE = "Resident Evil 4"
FIRST_FRAME_TIMEOUT_S = 3.0

# Private WinDLL instance: setting argtypes on the shared ctypes.windll.user32 would leak into other libraries
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
_user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = wintypes.HANDLE(-4)


class WindowNotFoundError(RuntimeError):
    pass


def _enable_dpi_awareness() -> None:
    """Make window sizes come in physical pixels, the same unit as the captured frames."""
    if not _user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
        _user32.SetProcessDPIAware()  # Already set by someone else, or an older Windows


def _find_game(title: str) -> tuple[int, int, int]:
    """Handle, width and height of the game image (the window without title bar and borders)."""
    hwnd = _user32.FindWindowW(None, title)
    if not hwnd:
        raise WindowNotFoundError(f"Janela '{title}' não encontrada. O jogo está aberto?")
    if _user32.IsIconic(hwnd):
        raise WindowNotFoundError(f"Janela '{title}' está minimizada; não dá para capturá-la assim.")
    client = wintypes.RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(client))
    return hwnd, client.right, client.bottom


# windows-capture rules, learned the hard way:
# - frame.frame_buffer points to GPU-mapped memory that is only valid inside on_frame_arrived,
#   so the pixels must be copied there.
# - Copy with numpy, never OpenCV: calling cv2 on the library's native thread starts OpenCV's
#   thread pool there, and the next capture session crashes the process (access violation).


def _game_area(frame, width: int, height: int) -> np.ndarray | None:
    """The game image inside a windows-capture frame, or None if the frame is smaller than the game.

    Frames normally hold the whole window (equal borders left, right and bottom, title bar on top),
    but right after the game starts some arrive with no border at all, so the offsets are computed
    from each frame instead of once.
    """
    border = (frame.width - width) // 2
    top = frame.height - height - border
    if border < 0 or top < 0:  # Window being resized
        return None
    return frame.frame_buffer[top:top + height, border:border + width]


class WindowsCaptureStreamCapturer:
    """Keeps one capture session open on the game window and returns its latest frame on demand."""

    def __init__(self, title: str = GAME_TITLE) -> None:
        self.title = title
        self._control = None

    def open(self) -> None:
        _enable_dpi_awareness()
        hwnd, width, height = _find_game(self.title)
        # Allocated once: a fresh 8 MB array per frame (60/s) costs more CPU than the copy itself
        self._bgra = np.empty((height, width, 4), dtype=np.uint8)
        self._lock = threading.Lock()
        self._has_frame = threading.Event()
        capture = WindowsCapture(cursor_capture=False, draw_border=None, window_hwnd=hwnd)

        @capture.event
        def on_frame_arrived(frame, capture_control) -> None:
            game_area = _game_area(frame, width, height)
            if game_area is None:
                return
            with self._lock:
                np.copyto(self._bgra, game_area)
            self._has_frame.set()

        @capture.event
        def on_closed() -> None:
            pass

        self._control = capture.start_free_threaded()

    def grab(self) -> np.ndarray:
        """Latest game frame as a new (height, width, 3) uint8 BGR array owned by the caller."""
        if not self._has_frame.wait(FIRST_FRAME_TIMEOUT_S):
            raise TimeoutError(f"Nenhum quadro em {FIRST_FRAME_TIMEOUT_S:.0f} s")
        with self._lock:  # Never convert a half-written frame
            return cv2.cvtColor(self._bgra, cv2.COLOR_BGRA2BGR)

    def close(self) -> None:
        if self._control is not None:
            self._control.stop()
            self._control.wait()
            self._control = None

    def __enter__(self) -> WindowsCaptureStreamCapturer:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
