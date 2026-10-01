"""What the toasts do: stacking, timing, hover, clicks and animation.

The manager owns every live notification and is the only thing that touches
them. It knows nothing about Win32: a ``Host`` gives it windows to put images
on, a work area to stack them in, a timer for animation, and mouse events
back. ``win32.WindowHost`` is the real one; the tests drive a fake.

Everything below ``show()`` runs on the host's UI thread, where ``post()``
puts it. Callers' callbacks never run there: a button's handler may POST to
the server and wait, and nothing on the UI thread may wait.

Positions and sizes are in physical pixels (the host's ``scale`` has already
been applied), except the handful of logical constants at the top.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from ui_desktop.toast import paint, sounds, theme
from ui_desktop.toast.paint import Layout, PaintState
from ui_desktop.toast.spec import ToastSpec

POSITIONS = ("bottom-right", "top-right", "bottom-left", "top-left")
DEFAULT_POSITION = "bottom-right"

EDGE = 16              # logical px between the stack and the work area's edge
GAP = 10               # logical px between toasts
MAX_VISIBLE = 4
SLIDE = 28             # logical px a toast travels while it fades in or out
ENTER_SEC, LEAVE_SEC, REFLOW_SEC = 0.24, 0.16, 0.20
HOVER_GRACE_SEC = 1.5  # the least a toast stays after the mouse leaves it
PROGRESS_STEPS = 48    # how finely the hairline is redrawn
TICK_ANIM_MS, TICK_IDLE_MS = 16, 80
SOUND_FATIGUE_SEC = 60 # the same motif again within this plays softer
SOUND_FATIGUE_GAIN = 0.65
RECORDING_GAIN = 0.6   # "quieter while recording": the sound lands in the recording

DISMISS_REASONS = ("clicked", "action", "closed", "timeout", "replaced", "dismissed", "shutdown")


class Host(Protocol):
    """The platform side. Every method except ``start`` and ``post`` is
    called on the UI thread."""
    def start(self) -> bool: ...
    def post(self, fn: Callable[[], None]) -> None: ...
    def scale(self) -> float: ...
    def work_area(self) -> tuple[int, int, int, int]: ...
    def animations(self) -> bool: ...
    def create(self) -> int: ...
    def update(self, handle: int, image, x: int, y: int, alpha: float) -> None: ...
    def move(self, handle: int, x: int, y: int, alpha: float) -> None: ...
    def destroy(self, handle: int) -> None: ...
    def set_timer(self, ms: int) -> None: ...
    def stop_timer(self) -> None: ...


@dataclass
class Prefs:
    position: str = DEFAULT_POSITION
    sticky: bool = False
    play_sounds: bool = True
    volume: float = 70.0
    sound_set: str = sounds.DEFAULT_SET
    quieter_while_recording: bool = True

    @classmethod
    def from_settings(cls, values: dict) -> "Prefs":
        p = cls()
        pos = str(values.get("notify_position") or "")
        p.position = pos if pos in POSITIONS else DEFAULT_POSITION
        p.sticky = bool(values.get("notify_sticky", False))
        p.play_sounds = values.get("notify_sounds", True) is not False
        try:
            p.volume = float(values.get("notify_volume", 70))
        except (TypeError, ValueError):
            p.volume = 70.0
        s = str(values.get("notify_sound_set") or "")
        p.sound_set = s if s in sounds.SETS else sounds.DEFAULT_SET
        p.quieter_while_recording = values.get("notify_quieter_while_recording", True) is not False
        return p


def read_prefs() -> Prefs:
    try:
        from core import settings
        return Prefs.from_settings(settings.load())
    except Exception:
        return Prefs()


class ToastHandle:
    """What ``show()`` returns: a way to take the toast down or change its
    text, and an event for anyone waiting on it."""

    def __init__(self, toast_id: int, manager: "ToastManager") -> None:
        self.id = toast_id
        self._manager = manager
        self.closed = threading.Event()
        self.reason: Optional[str] = None

    def dismiss(self) -> None:
        self._manager.dismiss(self.id)

    def update(self, title: Optional[str] = None, body: Optional[str] = None) -> None:
        self._manager.update(self.id, title, body)

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self.closed.wait(timeout)

    def _finish(self, reason: str) -> None:
        if not self.closed.is_set():
            self.reason = reason
            self.closed.set()


@dataclass
class _Live:
    id: int
    spec: ToastSpec
    palette: theme.Palette
    lay: Layout
    handle: ToastHandle
    hwnd: int = 0
    state: PaintState = field(default_factory=PaintState)
    phase: str = "new"                 # new | in | shown | out
    phase_t0: float = 0.0
    x: float = 0.0
    y: float = 0.0
    tx: float = 0.0
    ty: float = 0.0
    move_from: Optional[tuple[float, float]] = None
    move_t0: float = 0.0
    alpha: float = 0.0
    deadline: Optional[float] = None   # monotonic; None while sticky, hovered or not yet shown
    remaining: float = 0.0
    hovered: bool = False
    image: object = None
    image_key: object = None
    reason: Optional[str] = None

    @property
    def card_px(self) -> tuple[int, int]:
        s = self.lay.scale
        return int(round(self.lay.card_w * s)), int(round(self.lay.card_h * s))


def _ease_out(p: float) -> float:
    return 1.0 - (1.0 - p) ** 3


def _ease_in(p: float) -> float:
    return p * p


def _run_callback(fn: Callable, *args) -> None:
    """A caller's handler, on its own thread, never able to take the UI down."""
    def _go() -> None:
        try:
            fn(*args)
        except Exception as e:
            try:
                from core import log
                log.warn("notify", f"Notification callback raised: {e}")
            except Exception:
                pass
    threading.Thread(target=_go, name="toast-callback", daemon=True).start()


class ToastManager:
    def __init__(self, host_factory: Callable[["ToastManager"], Host], *,
                 prefs_reader: Callable[[], Prefs] = read_prefs,
                 palette_reader: Callable[[], theme.Palette] = theme.current,
                 clock: Callable[[], float] = time.monotonic,
                 run_callback: Callable = _run_callback,
                 play_sound: Callable[[str, str, float], object] = sounds.play) -> None:
        self._host_factory = host_factory
        self._host: Optional[Host] = None
        self._host_failed = False
        self._lock = threading.Lock()
        self._next_id = 1
        self._live: list[_Live] = []        # oldest first; UI thread only
        self._idle = threading.Condition()
        self._clock = clock
        self._prefs_reader = prefs_reader
        self._palette_reader = palette_reader
        self._run_callback = run_callback
        self._play_sound = play_sound
        self._prefs = Prefs()
        self._timer_ms = 0
        self._last_sound: dict[str, float] = {}
        self.is_recording: Callable[[], bool] = lambda: False

    # ── Public, any thread ────────────────────────────────────────────────────

    def supported(self) -> bool:
        return self._ensure_host() is not None

    def show(self, spec: ToastSpec) -> ToastHandle:
        with self._lock:
            toast_id = self._next_id
            self._next_id += 1
        handle = ToastHandle(toast_id, self)
        host = self._ensure_host()
        if host is None:
            handle._finish("unsupported")
            return handle
        prefs = self._prefs_reader()
        palette = self._palette_reader()
        if prefs.sticky:
            spec.timeout = 0.0
        self._sound_for(spec, prefs)
        host.post(lambda: self._add(toast_id, spec, palette, prefs, handle))
        return handle

    def dismiss(self, target, reason: str = "dismissed") -> None:
        """Take down a toast by id or by tag."""
        host = self._host
        if host is None:
            return
        host.post(lambda: self._dismiss(target, reason))

    def dismiss_all(self, reason: str = "dismissed") -> None:
        host = self._host
        if host is None:
            return
        host.post(lambda: [self._remove(t, reason, animate=reason != "shutdown")
                           for t in list(self._live)])

    def update(self, toast_id: int, title: Optional[str], body: Optional[str]) -> None:
        host = self._host
        if host is None:
            return
        host.post(lambda: self._update(toast_id, title, body))

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Wait until nothing is on screen. True when that happened in time."""
        if self._host is None:
            return True
        with self._idle:
            return self._idle.wait_for(lambda: not self._live, timeout)

    def play_preview(self, motif: str, sound_set: Optional[str] = None,
                     volume: Optional[float] = None) -> bool:
        """A motif at the given set and volume, or the saved ones, for the
        Settings picker. Never softened for a recording: it is a preview."""
        prefs = self._prefs_reader()
        gain = sounds.amplitude(prefs.volume if volume is None else volume)
        return bool(self._play_sound(sound_set or prefs.sound_set, motif, gain))

    # ── Host callbacks, UI thread ─────────────────────────────────────────────

    def on_tick(self) -> None:
        now = self._clock()
        animating = False
        for live in list(self._live):
            if live.phase == "in":
                p = min(1.0, (now - live.phase_t0) / ENTER_SEC)
                e = _ease_out(p)
                live.alpha = e
                live.x = live.move_from[0] + (live.tx - live.move_from[0]) * e if live.move_from else live.tx
                live.y = live.ty
                if p >= 1.0:
                    live.phase, live.move_from = "shown", None
                    live.x, live.y, live.alpha = live.tx, live.ty, 1.0
                    self._arm(live, now)
                else:
                    animating = True
            elif live.phase == "out":
                p = min(1.0, (now - live.phase_t0) / LEAVE_SEC)
                e = _ease_in(p)
                live.alpha = 1.0 - e
                live.x = live.tx + self._slide_px() * e
                if p >= 1.0:
                    self._destroy(live)
                    continue
                animating = True
            elif live.move_from is not None:
                p = min(1.0, (now - live.move_t0) / REFLOW_SEC)
                e = _ease_out(p)
                live.x = live.move_from[0] + (live.tx - live.move_from[0]) * e
                live.y = live.move_from[1] + (live.ty - live.move_from[1]) * e
                if p >= 1.0:
                    live.move_from = None
                    live.x, live.y = live.tx, live.ty
                else:
                    animating = True
            if live.phase == "shown" and live.deadline is not None:
                if now >= live.deadline:
                    self._remove(live, "timeout")
                    continue
                self._refresh_progress(live, now)
            self._blit(live)
        self._schedule(animating)

    def on_mouse(self, hwnd: int, event: str, x: float, y: float) -> None:
        live = self._by_hwnd(hwnd)
        if live is None or live.phase == "out":
            return
        now = self._clock()
        before = (live.state.hover, live.state.pressed)
        region = live.lay.hit(x, y) if event != "leave" else None
        if event == "move":
            if not live.hovered:
                live.hovered = True
                if live.deadline is not None:
                    live.remaining = max(0.0, live.deadline - now)
                    live.deadline = None
            live.state.hover = region
        elif event == "leave":
            live.hovered = False
            live.state.hover = None
            live.state.pressed = None
            if live.phase == "shown" and not live.spec.sticky:
                live.deadline = now + max(live.remaining, HOVER_GRACE_SEC)
        elif event == "down":
            live.state.pressed = region
        elif event == "up":
            pressed, live.state.pressed = live.state.pressed, None
            if pressed is not None and pressed == region:
                self._activate(live, region)
                return
        elif event == "secondary":
            self._remove(live, "closed")
            return
        if (live.state.hover, live.state.pressed) != before:
            self._paint(live)
            self._blit(live)
        self._schedule()

    def wants_hand(self, hwnd: int) -> bool:
        live = self._by_hwnd(hwnd)
        if live is None:
            return False
        h = live.state.hover
        if h is None:
            return False
        if h == "body":
            return live.spec.on_click is not None
        return True

    def on_display_change(self) -> None:
        """The work area or the DPI changed: lay everything out again."""
        host = self._host
        if host is None:
            return
        scale = host.scale()
        for live in self._live:
            if abs(live.lay.scale - scale) > 1e-6:
                live.lay = paint.layout(live.spec, live.palette, scale)
                live.image_key = None
                self._paint(live)
        self._reflow(animate=False)
        for live in self._live:
            self._blit(live)

    # ── Internals, UI thread ──────────────────────────────────────────────────

    def _ensure_host(self) -> Optional[Host]:
        if self._host is not None:
            return self._host
        if self._host_failed:
            return None
        with self._lock:
            if self._host is not None:
                return self._host
            if self._host_failed:
                return None
            try:
                host = self._host_factory(self)
                ok = host.start()
            except Exception as e:
                ok = False
                try:
                    from core import log
                    log.warn("notify", f"Notification window host failed to start: {e}")
                except Exception:
                    pass
            if not ok:
                self._host_failed = True
                return None
            self._host = host
            return host

    def _add(self, toast_id: int, spec: ToastSpec, palette: theme.Palette,
             prefs: Prefs, handle: ToastHandle) -> None:
        host = self._host
        assert host is not None
        self._prefs = prefs
        if spec.tag:
            for old in [t for t in self._live if t.spec.tag == spec.tag]:
                self._remove(old, "replaced", animate=False)
        while len([t for t in self._live if t.phase != "out"]) >= MAX_VISIBLE:
            victims = [t for t in self._live if t.phase != "out"]
            loose = [t for t in victims if not t.spec.sticky]
            self._remove((loose or victims)[0], "replaced")
        lay = paint.layout(spec, palette, host.scale())
        live = _Live(toast_id, spec, palette, lay, handle)
        live.hwnd = host.create()
        self._live.append(live)
        now = self._clock()
        self._reflow(animate=True)
        if host.animations():
            live.phase, live.phase_t0 = "in", now
            live.move_from = (live.tx + self._slide_px(), live.ty)
            live.x, live.y, live.alpha = live.move_from[0], live.ty, 0.0
        else:
            live.phase = "shown"
            live.x, live.y, live.alpha = live.tx, live.ty, 1.0
            self._arm(live, now)
        self._paint(live)
        self._blit(live)
        for other in self._live:
            if other is not live:
                self._blit(other)
        self._schedule(True)

    def _arm(self, live: _Live, now: float) -> None:
        if live.spec.sticky or live.hovered:
            live.deadline = None
            live.remaining = live.spec.timeout
        else:
            live.deadline = now + live.spec.timeout

    def _dismiss(self, target, reason: str) -> None:
        for live in list(self._live):
            if live.id == target or (live.spec.tag is not None and live.spec.tag == target):
                self._remove(live, reason)

    def _update(self, toast_id: int, title: Optional[str], body: Optional[str]) -> None:
        live = next((t for t in self._live if t.id == toast_id), None)
        if live is None or live.phase == "out":
            return
        if title is not None:
            live.spec.title = " ".join(str(title).split())
        if body is not None:
            live.spec.body = str(body).strip()
        live.lay = paint.layout(live.spec, live.palette, live.lay.scale)
        live.image_key = None
        self._reflow(animate=True)
        self._paint(live)
        for t in self._live:
            self._blit(t)
        self._schedule(True)

    def _activate(self, live: _Live, region: str) -> None:
        spec = live.spec
        if region == "close":
            self._remove(live, "closed")
        elif region.startswith("action:"):
            action = spec.actions[int(region.split(":", 1)[1])]
            if action.on_click is not None:
                self._run_callback(action.on_click, action.arg)
            if action.close:
                self._remove(live, "action")
            else:
                live.state.hover = region
                self._paint(live)
                self._blit(live)
        elif region == "body":
            if spec.on_click is not None:
                self._run_callback(spec.on_click, "")
            self._remove(live, "clicked")

    def _remove(self, live: _Live, reason: str, animate: bool = True) -> None:
        if live.phase == "out":
            return
        live.reason = reason
        live.deadline = None
        live.handle._finish(reason)
        if live.spec.on_dismiss is not None:
            self._run_callback(live.spec.on_dismiss, reason)
        host = self._host
        if animate and host is not None and host.animations():
            live.phase, live.phase_t0 = "out", self._clock()
            live.tx, live.ty = live.x, live.y
            live.move_from = None
        else:
            self._destroy(live)
        self._reflow(animate=True)
        self._schedule(True)

    def _destroy(self, live: _Live) -> None:
        host = self._host
        if live in self._live:
            self._live.remove(live)
        if host is not None and live.hwnd:
            try:
                host.destroy(live.hwnd)
            except Exception:
                pass
        live.hwnd = 0
        if not self._live:
            with self._idle:
                self._idle.notify_all()

    def _slide_px(self) -> float:
        s = self._host.scale() if self._host else 1.0
        return SLIDE * s * (1.0 if self._prefs.position.endswith("right") else -1.0)

    def _reflow(self, animate: bool) -> None:
        """Where every toast belongs: the newest at the chosen corner, the
        rest stacked away from it."""
        host = self._host
        if host is None:
            return
        s = host.scale()
        left, top, right, bottom = host.work_area()
        edge, gap, margin = EDGE * s, GAP * s, paint.MARGIN * s
        at_right = self._prefs.position.endswith("right")
        at_bottom = self._prefs.position.startswith("bottom")
        cursor = (bottom - edge) if at_bottom else (top + edge)
        now = self._clock()
        active = [t for t in self._live if t.phase != "out"]
        for live in reversed(active):
            cw, ch = live.card_px
            card_x = (right - edge - cw) if at_right else (left + edge)
            card_y = (cursor - ch) if at_bottom else cursor
            tx, ty = card_x - margin, card_y - margin
            cursor = (card_y - gap) if at_bottom else (card_y + ch + gap)
            if live.phase == "new":
                live.tx, live.ty = tx, ty
            elif (tx, ty) != (live.tx, live.ty):
                live.tx, live.ty = tx, ty
                if animate and host.animations() and live.phase == "shown":
                    live.move_from, live.move_t0 = (live.x, live.y), now
                elif live.phase == "shown":
                    live.move_from = None
                    live.x, live.y = tx, ty
                elif live.phase == "in":
                    live.move_from = (tx + self._slide_px(), ty)

    def _refresh_progress(self, live: _Live, now: float) -> None:
        if live.spec.sticky or live.spec.timeout <= 0:
            return
        frac = max(0.0, (live.deadline - now) / live.spec.timeout) if live.deadline else \
            max(0.0, live.remaining / live.spec.timeout)
        step = math.ceil(frac * PROGRESS_STEPS) / PROGRESS_STEPS
        if live.state.progress != step:
            live.state.progress = step
            self._paint(live)

    def _paint(self, live: _Live) -> None:
        st = live.state
        key = (st.hover, st.pressed, st.progress, live.spec.title, live.spec.body, live.lay.scale)
        if key == live.image_key and live.image is not None:
            return
        live.image = paint.paint(live.lay, st)
        live.image_key = key
        live.image_dirty = True  # type: ignore[attr-defined]

    def _blit(self, live: _Live) -> None:
        host = self._host
        if host is None or not live.hwnd or live.image is None:
            return
        x, y = int(round(live.x)), int(round(live.y))
        if getattr(live, "image_dirty", False):
            host.update(live.hwnd, live.image, x, y, live.alpha)
            live.image_dirty = False  # type: ignore[attr-defined]
        else:
            host.move(live.hwnd, x, y, live.alpha)

    def _schedule(self, animating: bool = False) -> None:
        host = self._host
        if host is None:
            return
        if not animating:
            animating = any(t.phase in ("in", "out") or t.move_from is not None for t in self._live)
        if animating:
            ms = TICK_ANIM_MS
        elif any(t.phase == "shown" and t.deadline is not None for t in self._live):
            ms = TICK_IDLE_MS
        else:
            ms = 0
        if ms == self._timer_ms:
            return
        self._timer_ms = ms
        if ms:
            host.set_timer(ms)
        else:
            host.stop_timer()

    def _by_hwnd(self, hwnd: int) -> Optional[_Live]:
        return next((t for t in self._live if t.hwnd == hwnd), None)

    def _sound_for(self, spec: ToastSpec, prefs: Prefs) -> None:
        motif = spec.sound_motif
        if motif is None or not prefs.play_sounds:
            return
        gain = sounds.amplitude(prefs.volume)
        now = self._clock()
        last = self._last_sound.get(motif)
        if last is not None and now - last < SOUND_FATIGUE_SEC:
            gain *= SOUND_FATIGUE_GAIN
        self._last_sound[motif] = now
        if prefs.quieter_while_recording:
            try:
                if self.is_recording():
                    gain *= RECORDING_GAIN
            except Exception:
                pass
        try:
            self._play_sound(prefs.sound_set, motif, gain)
        except Exception:
            pass
