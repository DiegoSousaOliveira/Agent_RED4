"""One adapter per capture library, all honoring the same frame contract.

Every ``grab()`` returns a caller-owned ``numpy.ndarray`` of shape (H, W, 3), dtype uint8,
in BGR order (what OpenCV expects), so the measured cost always includes getting the
pixels into a format the agent can use directly.

Each adapter is written the way the library is meant to be used for repeated captures:
handles, device contexts and buffers are created once in ``open()`` and reused.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import ClassVar, Literal, TypeVar

import cv2
import numpy as np

from .winapi import GameWindow, Rect, WindowPrinter

Source = Literal["tela", "janela"]
T = TypeVar("T")

FIRST_FRAME_TIMEOUT_S = 3.0


def _wait_for(produce: Callable[[], T | None], timeout: float = FIRST_FRAME_TIMEOUT_S) -> T:
    """Call ``produce`` until it returns something other than None."""
    deadline = time.perf_counter() + timeout
    while (value := produce()) is None:
        if time.perf_counter() > deadline:
            raise TimeoutError(f"Nenhum quadro em {timeout:.0f} s")
        time.sleep(0.001)
    return value


class Capturer(ABC):
    key: ClassVar[str]
    label: ClassVar[str]
    distribution: ClassVar[str | None]  # pip package that provides it (None = only ctypes)
    modules: ClassVar[tuple[str, ...]]  # imported (and timed) before open()
    source: ClassVar[Source]  # "tela" copies the monitor, "janela" copies the game window itself
    api: ClassVar[str]  # Windows API doing the actual work
    mode: ClassVar[str]  # How the adapter uses it, in plain words

    def __init__(self, game: GameWindow) -> None:
        self.game = game

    @property
    def region(self) -> Rect:
        """Area covered by the returned frames, in screen coordinates."""
        return self.game.screen if self.source == "tela" else self.game.client

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def grab(self) -> np.ndarray: ...

    def close(self) -> None:
        pass


CAPTURERS: dict[str, type[Capturer]] = {}


def register(cls: type[Capturer]) -> type[Capturer]:
    CAPTURERS[cls.key] = cls
    return cls


@register
class MssCapturer(Capturer):
    key = "mss"
    label = "mss"
    distribution = "mss"
    modules = ("mss",)
    source = "tela"
    api = "GDI BitBlt"
    mode = "Cópia do monitor a cada chamada"

    def open(self) -> None:
        import mss

        self._sct = mss.MSS()
        screen = self.region
        self._monitor = {"left": screen.left, "top": screen.top, "width": screen.width, "height": screen.height}

    def grab(self) -> np.ndarray:
        shot = self._sct.grab(self._monitor)
        bgra = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        return cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)

    def close(self) -> None:
        self._sct.close()


@register
class PillowCapturer(Capturer):
    key = "pillow"
    label = "Pillow ImageGrab"
    distribution = "pillow"
    modules = ("PIL.ImageGrab",)
    source = "tela"
    api = "GDI BitBlt"
    mode = "Cópia do monitor a cada chamada"

    def open(self) -> None:
        from PIL import ImageGrab

        self._grab = ImageGrab.grab
        screen = self.region
        self._bbox = (screen.left, screen.top, screen.right, screen.bottom)

    def grab(self) -> np.ndarray:
        return cv2.cvtColor(np.asarray(self._grab(bbox=self._bbox)), cv2.COLOR_RGB2BGR)


@register
class PyAutoGuiCapturer(Capturer):
    key = "pyautogui"
    label = "pyautogui"
    distribution = "pyautogui"
    modules = ("pyautogui",)
    source = "tela"
    api = "GDI BitBlt (via Pillow)"
    mode = "Cópia do monitor a cada chamada"

    def open(self) -> None:
        import pyautogui

        self._screenshot = pyautogui.screenshot
        screen = self.region
        self._region = (screen.left, screen.top, screen.width, screen.height)

    def grab(self) -> np.ndarray:
        return cv2.cvtColor(np.asarray(self._screenshot(region=self._region)), cv2.COLOR_RGB2BGR)


@register
class PyWin32BitBltCapturer(Capturer):
    key = "pywin32"
    label = "pywin32 BitBlt"
    distribution = "pywin32"
    modules = ("win32gui", "win32ui", "win32con")
    source = "tela"
    api = "GDI BitBlt"
    mode = "Cópia do monitor a cada chamada"

    def open(self) -> None:
        import win32con
        import win32gui
        import win32ui

        screen = self.region
        self._srccopy = win32con.SRCCOPY
        self._desktop = win32gui.GetDesktopWindow()
        self._desktop_dc_handle = win32gui.GetWindowDC(self._desktop)
        self._desktop_dc = win32ui.CreateDCFromHandle(self._desktop_dc_handle)
        self._memory_dc = self._desktop_dc.CreateCompatibleDC()
        self._bitmap = win32ui.CreateBitmap()
        self._bitmap.CreateCompatibleBitmap(self._desktop_dc, screen.width, screen.height)
        self._memory_dc.SelectObject(self._bitmap)
        self._size = (screen.width, screen.height)
        self._origin = (screen.left, screen.top)

    def grab(self) -> np.ndarray:
        self._memory_dc.BitBlt((0, 0), self._size, self._desktop_dc, self._origin, self._srccopy)
        width, height = self._size
        bgra = np.frombuffer(self._bitmap.GetBitmapBits(True), dtype=np.uint8).reshape(height, width, 4)
        return cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)

    def close(self) -> None:
        import win32gui

        win32gui.DeleteObject(self._bitmap.GetHandle())
        self._memory_dc.DeleteDC()
        self._desktop_dc.DeleteDC()
        win32gui.ReleaseDC(self._desktop, self._desktop_dc_handle)


@register
class DxcamCapturer(Capturer):
    key = "dxcam"
    label = "dxcam"
    distribution = "dxcam"
    modules = ("dxcam",)
    source = "tela"
    api = "DXGI Desktop Duplication"
    mode = "grab() sob demanda; repete o último quadro se a tela não mudou"

    def open(self) -> None:
        import dxcam

        self._camera = dxcam.create(output_color="BGR")

    def grab(self) -> np.ndarray:
        # new_frame_only=False: return the current screen even when nothing changed since the last call
        return _wait_for(lambda: self._camera.grab(new_frame_only=False))

    def close(self) -> None:
        self._camera.release()


@register
class DxcamStreamCapturer(Capturer):
    key = "dxcam-stream"
    label = "dxcam (stream)"
    distribution = "dxcam"
    modules = ("dxcam",)
    source = "tela"
    api = "DXGI Desktop Duplication"
    mode = "Thread interna a 60 FPS; grab() lê o quadro mais recente"

    def open(self) -> None:
        import dxcam

        self._camera = dxcam.create(output_color="BGR")
        self._camera.start(target_fps=60)

    def grab(self) -> np.ndarray:
        return _wait_for(self._camera.grab)

    def close(self) -> None:
        self._camera.stop()
        self._camera.release()


@register
class BettercamCapturer(Capturer):
    key = "bettercam"
    label = "bettercam"
    distribution = "bettercam"
    modules = ("bettercam",)
    source = "tela"
    api = "DXGI Desktop Duplication"
    mode = "grab() sob demanda; repete o último quadro se a tela não mudou"

    def open(self) -> None:
        import bettercam

        self._camera = bettercam.create(output_color="BGR")
        self._last: np.ndarray | None = None

    def grab(self) -> np.ndarray:
        # bettercam returns None when the screen has not changed; fall back to the last frame
        if self._last is None:
            self._last = _wait_for(self._camera.grab)
            return self._last
        frame = self._camera.grab()
        if frame is None:
            return self._last.copy()
        self._last = frame
        return frame

    def close(self) -> None:
        self._camera.release()


# windows-capture rules, learned the hard way:
# - frame.frame_buffer points to GPU-mapped memory that is only valid inside on_frame_arrived,
#   so the pixels must be copied there.
# - Copy with numpy, never OpenCV: calling cv2 on the library's native thread starts OpenCV's
#   thread pool there, and the next capture session crashes the process (access violation).


def _game_area(frame, client: Rect) -> np.ndarray | None:
    """The game image inside a windows-capture frame, or None if the frame is smaller than the game.

    Frames normally hold the whole window (equal borders left, right and bottom, title bar on top),
    but right after the game starts some arrive with no border at all, so the offsets are computed
    from each frame instead of once.
    """
    border = (frame.width - client.width) // 2
    top = frame.height - client.height - border
    if border < 0 or top < 0:  # Window being resized
        return None
    return frame.frame_buffer[top:top + client.height, border:border + client.width]


@register
class WindowsCaptureStreamCapturer(Capturer):
    key = "windows-capture-stream"
    label = "windows-capture (stream)"
    distribution = "windows-capture"
    modules = ("windows_capture",)
    source = "janela"
    api = "Windows Graphics Capture"
    mode = "Sessão contínua; grab() lê o quadro mais recente"

    def open(self) -> None:
        from windows_capture import WindowsCapture

        client = self.game.client
        # Allocated once: a fresh 8 MB array per frame (60/s) costs more CPU than the copy itself
        self._bgra = np.empty((client.height, client.width, 4), dtype=np.uint8)
        self._lock = threading.Lock()
        self._has_frame = threading.Event()
        capture = WindowsCapture(cursor_capture=False, draw_border=None, window_hwnd=self.game.hwnd)

        @capture.event
        def on_frame_arrived(frame, capture_control) -> None:
            game_area = _game_area(frame, client)
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
        if not self._has_frame.wait(FIRST_FRAME_TIMEOUT_S):
            raise TimeoutError(f"Nenhum quadro em {FIRST_FRAME_TIMEOUT_S:.0f} s")
        with self._lock:  # Never convert a half-written frame
            return cv2.cvtColor(self._bgra, cv2.COLOR_BGRA2BGR)

    def close(self) -> None:
        self._control.stop()
        self._control.wait()


@register
class WindowsCaptureOneShotCapturer(Capturer):
    key = "windows-capture-oneshot"
    label = "windows-capture (1 sessão por foto)"
    distribution = "windows-capture"
    modules = ("windows_capture",)
    source = "janela"
    api = "Windows Graphics Capture"
    mode = "Abre uma sessão, espera 1 quadro e fecha (como o main.py)"

    def open(self) -> None:
        from windows_capture import WindowsCapture

        self._windows_capture = WindowsCapture

    def grab(self) -> np.ndarray:
        result: list[np.ndarray] = []
        done = threading.Event()
        capture = self._windows_capture(cursor_capture=False, draw_border=None, window_hwnd=self.game.hwnd)

        @capture.event
        def on_frame_arrived(frame, capture_control) -> None:
            game_area = _game_area(frame, self.game.client)
            if game_area is None:
                return  # Wait for the next frame
            result.append(game_area.copy())
            capture_control.stop()
            done.set()

        @capture.event
        def on_closed() -> None:
            done.set()

        control = capture.start_free_threaded()
        finished = done.wait(FIRST_FRAME_TIMEOUT_S)
        control.stop()
        control.wait()
        if not finished or not result:
            raise TimeoutError("A sessão não entregou nenhum quadro (janela minimizada?)")
        return cv2.cvtColor(result[0], cv2.COLOR_BGRA2BGR)


@register
class PrintWindowCapturer(Capturer):
    key = "printwindow"
    label = "PrintWindow (ctypes)"
    distribution = None
    modules = ()
    source = "janela"
    api = "PrintWindow (PW_RENDERFULLCONTENT)"
    mode = "Pede ao Windows o conteúdo atual da janela"

    def open(self) -> None:
        client = self.game.client
        self._printer = WindowPrinter(self.game.hwnd, client.width, client.height)

    def grab(self) -> np.ndarray:
        return self._printer.grab()

    def close(self) -> None:
        self._printer.close()
