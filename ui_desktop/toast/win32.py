"""The Windows side of the toast: layered windows on their own thread.

Each notification is a ``WS_POPUP`` window with ``WS_EX_LAYERED``, shown with
``UpdateLayeredWindow`` from the bitmap ``paint.py`` produced, so Windows
composites the rounded corners and the shadow from the bitmap's alpha and
draws no frame of its own. ``WS_EX_TOPMOST`` keeps it above everything,
``WS_EX_TOOLWINDOW`` keeps it out of the taskbar and Alt-Tab, and
``WS_EX_NOACTIVATE`` means clicking a button never takes focus away from the
meeting the user is in.

All windows belong to one daemon thread that runs its own message loop,
separate from the tray's loop on the main thread. Other threads reach it
through ``post()``, which queues a callable and wakes the loop with a
message. The thread is per-monitor DPI aware (V2) regardless of how the rest
of the process was started, so the work area and the mouse come back in
physical pixels and the bitmap is painted at the monitor's real scale.
"""
from __future__ import annotations

import collections
import ctypes
import sys
import threading
from ctypes import wintypes as wt
from typing import Callable, Optional

CLASS_NAME = "MeetingAssistantToast"

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
ULW_ALPHA = 0x00000002
AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
HWND_MESSAGE = -3
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
SPI_GETWORKAREA = 0x0030
SPI_SETWORKAREA = 0x002F
SPI_GETCLIENTAREAANIMATION = 0x1042
MONITOR_DEFAULTTOPRIMARY = 1
MDT_EFFECTIVE_DPI = 0
IDC_ARROW, IDC_HAND = 32512, 32649
TME_LEAVE = 0x00000002
MA_NOACTIVATE = 3
DIB_RGB_COLORS, BI_RGB = 0, 0
TIMER_ID = 1

WM_DESTROY = 0x0002
WM_SETTINGCHANGE = 0x001A
WM_SETCURSOR = 0x0020
WM_MOUSEACTIVATE = 0x0021
WM_DISPLAYCHANGE = 0x007E
WM_TIMER = 0x0113
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_MOUSELEAVE = 0x02A3
WM_DPICHANGED = 0x02E0
WM_APP = 0x8000
WM_APP_POST = WM_APP + 1
WM_APP_QUIT = WM_APP + 2

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", wt.BYTE), ("BlendFlags", wt.BYTE),
                ("SourceConstantAlpha", wt.BYTE), ("AlphaFormat", wt.BYTE)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD),
                ("hwndTrack", wt.HWND), ("dwHoverTime", wt.DWORD)]


def _signed_lo(v: int) -> int:
    return ctypes.c_short(v & 0xFFFF).value


def _signed_hi(v: int) -> int:
    return ctypes.c_short((v >> 16) & 0xFFFF).value


def _bgra_premultiplied(image) -> bytes:
    """A Pillow RGBA image as the top-down premultiplied BGRA that a 32-bit
    DIB section holds for UpdateLayeredWindow."""
    from PIL import Image
    r, g, b, a = image.convert("RGBa").split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


class _Surface:
    """A window's DIB section and the memory DC it is selected into."""

    def __init__(self, gdi32, user32) -> None:
        self._gdi32, self._user32 = gdi32, user32
        self.size = (0, 0)
        self.hdc = None
        self.hbmp = None
        self.old = None
        self.bits = ctypes.c_void_p()
        self.shown = False

    def ensure(self, w: int, h: int) -> None:
        if self.size == (w, h) and self.hdc:
            return
        self.release()
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        screen = self._user32.GetDC(None)
        try:
            self.hdc = self._gdi32.CreateCompatibleDC(screen)
            self.bits = ctypes.c_void_p()
            self.hbmp = self._gdi32.CreateDIBSection(screen, ctypes.byref(bmi), DIB_RGB_COLORS,
                                                     ctypes.byref(self.bits), None, 0)
        finally:
            self._user32.ReleaseDC(None, screen)
        if not self.hbmp or not self.hdc:
            self.release()
            raise OSError("CreateDIBSection failed")
        self.old = self._gdi32.SelectObject(self.hdc, self.hbmp)
        self.size = (w, h)

    def write(self, data: bytes) -> None:
        ctypes.memmove(self.bits, data, min(len(data), self.size[0] * self.size[1] * 4))

    def release(self) -> None:
        if self.hdc:
            if self.old:
                self._gdi32.SelectObject(self.hdc, self.old)
            self._gdi32.DeleteDC(self.hdc)
        if self.hbmp:
            self._gdi32.DeleteObject(self.hbmp)
        self.hdc = self.hbmp = self.old = None
        self.size = (0, 0)


class WindowHost:
    """``manager.Host`` for Windows. One instance per process."""

    def __init__(self, manager) -> None:
        self._manager = manager
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._failed: Optional[str] = None
        self._queue: collections.deque = collections.deque()
        self._queue_lock = threading.Lock()
        self._msg_hwnd = None
        self._surfaces: dict[int, _Surface] = {}
        self._tracking: set[int] = set()
        self._timer_ms = 0
        self._proc = None
        self._user32 = self._gdi32 = self._kernel32 = None
        self._hand = None
        self._scale_cache: Optional[float] = None

    # ── Host protocol ─────────────────────────────────────────────────────────

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        if self._thread is not None:
            return self._failed is None
        self._thread = threading.Thread(target=self._run, name="toast-ui", daemon=True)
        self._thread.start()
        self._ready.wait(10.0)
        return self._failed is None and self._msg_hwnd is not None

    def post(self, fn: Callable[[], None]) -> None:
        with self._queue_lock:
            self._queue.append(fn)
        if self._msg_hwnd is not None:
            self._user32.PostMessageW(self._msg_hwnd, WM_APP_POST, 0, 0)

    def scale(self) -> float:
        if self._scale_cache is not None:
            return self._scale_cache
        dpi = 96.0
        try:
            shcore = ctypes.WinDLL("shcore")
            pt = wt.POINT(0, 0)
            mon = self._user32.MonitorFromPoint(pt, MONITOR_DEFAULTTOPRIMARY)
            dx, dy = wt.UINT(96), wt.UINT(96)
            if shcore.GetDpiForMonitor(mon, MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                dpi = float(dx.value)
        except Exception:
            try:
                dpi = float(self._user32.GetDpiForSystem())
            except Exception:
                dpi = 96.0
        self._scale_cache = max(0.5, dpi / 96.0)
        return self._scale_cache

    def work_area(self) -> tuple[int, int, int, int]:
        rect = wt.RECT()
        if self._user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
        return 0, 0, self._user32.GetSystemMetrics(0), self._user32.GetSystemMetrics(1)

    def animations(self) -> bool:
        flag = wt.BOOL(1)
        try:
            if self._user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(flag), 0):
                return bool(flag.value)
        except Exception:
            pass
        return True

    def create(self) -> int:
        hwnd = self._user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            CLASS_NAME, "Meeting Assistant notification", WS_POPUP,
            0, 0, 1, 1, None, None, self._kernel32.GetModuleHandleW(None), None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._surfaces[hwnd] = _Surface(self._gdi32, self._user32)
        return hwnd

    def update(self, handle: int, image, x: int, y: int, alpha: float) -> None:
        surf = self._surfaces.get(handle)
        if surf is None:
            return
        w, h = image.size
        surf.ensure(w, h)
        surf.write(_bgra_premultiplied(image))
        self._blit(handle, surf, x, y, alpha)

    def move(self, handle: int, x: int, y: int, alpha: float) -> None:
        surf = self._surfaces.get(handle)
        if surf is None or not surf.hdc:
            return
        self._blit(handle, surf, x, y, alpha)

    def destroy(self, handle: int) -> None:
        surf = self._surfaces.pop(handle, None)
        self._tracking.discard(handle)
        self._user32.DestroyWindow(handle)
        if surf is not None:
            surf.release()

    def set_timer(self, ms: int) -> None:
        if self._msg_hwnd is None:
            return
        self._timer_ms = ms
        self._user32.SetTimer(self._msg_hwnd, TIMER_ID, max(1, int(ms)), None)

    def stop_timer(self) -> None:
        if self._msg_hwnd is None:
            return
        self._timer_ms = 0
        self._user32.KillTimer(self._msg_hwnd, TIMER_ID)

    # ── The thread ────────────────────────────────────────────────────────────

    def _bind(self) -> None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        u.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        u.RegisterClassExW.restype = wt.ATOM
        u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
        u.CreateWindowExW.restype = wt.HWND
        u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        u.GetMessageW.restype = ctypes.c_int
        u.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        u.DispatchMessageW.restype = LRESULT
        u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.PostMessageW.restype = wt.BOOL
        u.DestroyWindow.argtypes = [wt.HWND]
        u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.UINT]
        u.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                          ctypes.POINTER(wt.SIZE), wt.HDC, ctypes.POINTER(wt.POINT),
                                          wt.COLORREF, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
        u.UpdateLayeredWindow.restype = wt.BOOL
        u.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
        u.SetTimer.restype = ctypes.c_size_t
        u.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
        u.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]
        u.SystemParametersInfoW.restype = wt.BOOL
        u.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
        u.SetCapture.argtypes = [wt.HWND]
        u.SetCapture.restype = wt.HWND
        u.SetCursor.argtypes = [wt.HANDLE]
        u.SetCursor.restype = wt.HANDLE
        u.LoadCursorW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
        u.LoadCursorW.restype = wt.HANDLE
        u.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
        u.MonitorFromPoint.restype = wt.HMONITOR
        u.GetDC.argtypes = [wt.HWND]
        u.GetDC.restype = wt.HDC
        u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        g.CreateCompatibleDC.argtypes = [wt.HDC]
        g.CreateCompatibleDC.restype = wt.HDC
        g.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
        g.CreateDIBSection.restype = wt.HBITMAP
        g.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
        g.SelectObject.restype = wt.HGDIOBJ
        g.DeleteObject.argtypes = [wt.HGDIOBJ]
        g.DeleteDC.argtypes = [wt.HDC]
        k.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        k.GetModuleHandleW.restype = wt.HMODULE
        self._user32, self._gdi32, self._kernel32 = u, g, k

    def _run(self) -> None:
        try:
            self._bind()
            u = self._user32
            try:
                # Per-monitor DPI awareness for this thread alone, whatever
                # the process was launched with.
                u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
                u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
            self._proc = WNDPROC(self._wndproc)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._proc
            wc.hInstance = self._kernel32.GetModuleHandleW(None)
            wc.hCursor = u.LoadCursorW(None, ctypes.cast(IDC_ARROW, wt.LPCWSTR))
            wc.lpszClassName = CLASS_NAME
            if not u.RegisterClassExW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                    raise ctypes.WinError(err)
            self._hand = u.LoadCursorW(None, ctypes.cast(IDC_HAND, wt.LPCWSTR))
            self._msg_hwnd = u.CreateWindowExW(0, CLASS_NAME, "Meeting Assistant toasts", 0,
                                               0, 0, 0, 0, wt.HWND(HWND_MESSAGE), None,
                                               wc.hInstance, None)
            if not self._msg_hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception as e:
            self._failed = str(e)
            self._ready.set()
            return
        self._ready.set()
        msg = wt.MSG()
        u = self._user32
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def _drain(self) -> None:
        while True:
            with self._queue_lock:
                if not self._queue:
                    return
                fn = self._queue.popleft()
            try:
                fn()
            except Exception as e:
                self._log(f"Notification task failed: {e}")

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_APP_POST:
                self._drain()
                return 0
            if msg == WM_TIMER:
                self._manager.on_tick()
                return 0
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_MOUSEMOVE:
                if hwnd not in self._tracking:
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                    self._user32.TrackMouseEvent(ctypes.byref(tme))
                    self._tracking.add(hwnd)
                self._manager.on_mouse(hwnd, "move", _signed_lo(lparam), _signed_hi(lparam))
                return 0
            if msg == WM_MOUSELEAVE:
                self._tracking.discard(hwnd)
                self._manager.on_mouse(hwnd, "leave", -1, -1)
                return 0
            if msg == WM_LBUTTONDOWN:
                self._user32.SetCapture(hwnd)
                self._manager.on_mouse(hwnd, "down", _signed_lo(lparam), _signed_hi(lparam))
                return 0
            if msg == WM_LBUTTONUP:
                self._user32.ReleaseCapture()
                self._manager.on_mouse(hwnd, "up", _signed_lo(lparam), _signed_hi(lparam))
                return 0
            if msg == WM_RBUTTONUP:
                self._manager.on_mouse(hwnd, "secondary", _signed_lo(lparam), _signed_hi(lparam))
                return 0
            if msg == WM_SETCURSOR:
                if self._manager.wants_hand(hwnd) and self._hand:
                    self._user32.SetCursor(self._hand)
                    return 1
            if msg in (WM_DISPLAYCHANGE, WM_DPICHANGED) or \
                    (msg == WM_SETTINGCHANGE and wparam == SPI_SETWORKAREA):
                self._scale_cache = None
                self._manager.on_display_change()
                return 0
        except Exception as e:
            self._log(f"Notification window error: {e}")
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _blit(self, hwnd: int, surf: _Surface, x: int, y: int, alpha: float) -> None:
        w, h = surf.size
        pos = wt.POINT(int(x), int(y))
        size = wt.SIZE(w, h)
        src = wt.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, int(round(alpha * 255)))), AC_SRC_ALPHA)
        self._user32.UpdateLayeredWindow(hwnd, None, ctypes.byref(pos), ctypes.byref(size),
                                         surf.hdc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        if not surf.shown:
            self._user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            self._user32.SetWindowPos(hwnd, wt.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                                      SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            surf.shown = True

    @staticmethod
    def _log(message: str) -> None:
        try:
            from core import log
            log.warn("notify", message)
        except Exception:
            pass
