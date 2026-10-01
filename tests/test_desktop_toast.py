"""The desktop notifications: the toast widget (ui_desktop/toast) and the
notifications module that sends them.

Windows' own toasts were dropped silently under Focus Assist during the very
meetings the app records, so the app draws its own. The widget is three
platform-neutral parts (theme, paint, manager) that these tests run for
real, and one Win32 part (the window host) that they do not: the manager is
driven through a fake host whose UI thread is the test itself.
"""
from __future__ import annotations

import io
import re
import sys
import wave
from pathlib import Path

import pytest
from PIL import Image

from core import settings
from ui_desktop import notifications
from ui_desktop import toast
from ui_desktop.toast import manager as mgr, paint, sounds, spec, theme
from ui_desktop.toast.manager import Prefs, ToastManager
from ui_desktop.toast.spec import Action, ToastSpec

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")
SETTINGS_HTML = (ROOT / "ui_web/templates/_settings.html").read_text(encoding="utf-8")
NOTIF_PY = (ROOT / "ui_desktop/notifications.py").read_text(encoding="utf-8")
WIN32_PY = (ROOT / "ui_desktop/toast/win32.py").read_text(encoding="utf-8")

WIN = sys.platform == "win32"


# ── A host whose UI thread is the test ────────────────────────────────────────

class FakeHost:
    def __init__(self, manager, *, scale=1.0, work=(0, 0, 1920, 1040), animations=False):
        self.manager = manager
        self._scale, self.work, self._anim = scale, work, animations
        self.windows: dict[int, dict] = {}
        self.destroyed: list[int] = []
        self.timer = 0
        self._next = 100

    def start(self): return True
    def post(self, fn): fn()
    def scale(self): return self._scale
    def work_area(self): return self.work
    def animations(self): return self._anim

    def create(self):
        self._next += 1
        self.windows[self._next] = {"image": None, "updates": 0}
        return self._next

    def update(self, h, image, x, y, alpha):
        w = self.windows[h]
        w.update(image=image, x=x, y=y, alpha=alpha)
        w["updates"] += 1

    def move(self, h, x, y, alpha):
        self.windows[h].update(x=x, y=y, alpha=alpha)

    def destroy(self, h):
        self.destroyed.append(h)
        self.windows.pop(h, None)

    def set_timer(self, ms): self.timer = ms
    def stop_timer(self): self.timer = 0


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def make(*, animations=False, prefs=None, scale=1.0, played=None, recording=False):
    clock = Clock()
    hosts = {}

    def factory(m):
        hosts["h"] = FakeHost(m, animations=animations, scale=scale)
        return hosts["h"]

    def run_cb(fn, *args):      # inline, so the test sees the call at once
        fn(*args)

    def play(set_id, motif, gain):
        if played is not None:
            played.append((set_id, motif, round(gain, 3)))
        return True

    m = ToastManager(factory, prefs_reader=lambda: prefs or Prefs(),
                     palette_reader=lambda: theme.resolve("dark", "blue"),
                     clock=clock, run_callback=run_cb, play_sound=play)
    m.is_recording = lambda: recording
    assert m.supported()
    return m, hosts["h"], clock


def centre(lay, name):
    x, y, w, h = lay.regions[name]
    return (x + w / 2) * lay.scale, (y + h / 2) * lay.scale


def click(m, live, name):
    x, y = centre(live.lay, name)
    m.on_mouse(live.hwnd, "move", x, y)
    m.on_mouse(live.hwnd, "down", x, y)
    m.on_mouse(live.hwnd, "up", x, y)


# ── Theme: the stylesheet is the palette ──────────────────────────────────────

def test_every_mode_and_accent_resolves_from_the_stylesheet():
    for mode in ("dark", "light"):
        for accent in theme.ACCENTS:
            pal = theme.resolve(mode, accent, {"accent": "#e36aa6", "strength": 60})
            assert pal.mode == mode and pal.accent_name == accent
            for name in ("bg", "surface", "border", "fg", "fg-muted", "accent", "red", "green", "yellow"):
                assert len(pal[name]) == 4, (mode, accent, name)


def test_the_colours_are_the_ones_the_app_uses():
    assert theme.resolve("dark", "blue")["surface"] == (0x16, 0x1B, 0x22, 255)
    assert theme.resolve("light", "blue")["bg"] == (255, 255, 255, 255)
    assert theme.resolve("dark", "ocean")["accent"] == (0x39, 0xC5, 0xCF, 255)
    # Light + accent is the light accent block, not the dark one with light text.
    assert theme.resolve("light", "ocean")["bg"] == (0xFB, 0xFD, 0xFE, 255)
    assert theme.resolve("light", "ocean")["accent"] == (0x05, 0x98, 0xA8, 255)
    # The dark border carries alpha (#484f585c) and survives as such.
    assert theme.resolve("dark", "blue")["border"][3] == 0x5C


def test_the_custom_accent_is_derived_like_the_page_derives_it():
    pal = theme.resolve("dark", "custom", {"accent": "#e36aa6", "strength": 60})
    assert pal["accent"] == (0xE3, 0x6A, 0xA6, 255)
    # mix = 0.6 * 0.13 of the way from the base surface to the accent.
    assert pal["bg"] == (30, 24, 34, 255)
    assert pal["accent-dim"] == theme.blend((0xE3, 0x6A, 0xA6, 255), (0, 0, 0, 255), 0.28)
    assert pal["border"][3] == 0x5C
    light = theme.resolve("light", "custom", {"accent": "#e36aa6", "strength": 60})
    assert light["border"][3] == 255


def test_selectors_match_the_way_the_cascade_would():
    m = theme._selector_matches
    assert m(':root', "dark", "blue")
    assert m(':root[data-theme-mode="dark"]', "dark", "blue")
    assert not m(':root[data-theme-mode="light"]', "dark", "blue")
    assert not m(':root[data-accent="ocean"]', "dark", "blue")       # blue sets no attribute
    assert m(':root[data-accent="ocean"]', "dark", "ocean")
    assert not m(':root[data-theme-mode="light"][data-accent="ocean"]', "dark", "ocean")
    assert m(':root[data-theme-mode="light"][data-accent="ocean"]', "light", "ocean")
    assert not m(':root[data-theme-mode="light"] .note-file', "light", "blue")


def test_system_mode_resolves_to_one_of_the_two():
    assert theme.effective_mode("system") in ("dark", "light")
    assert theme.effective_mode("light") == "light"
    assert theme.effective_mode(None) in ("dark", "light")
    assert set(theme.KIND_TOKENS) == set(spec.KINDS)


# ── Spec ──────────────────────────────────────────────────────────────────────

def test_a_spec_fills_in_what_the_kind_implies():
    s = ToastSpec("Hello", kind="nonsense")
    assert s.kind == "info" and s.icon == "circle-info" and s.timeout == 8.0 and s.sound_motif == "info"
    e = ToastSpec("Bad", kind="error")
    assert e.sticky and e.timeout == 0.0 and e.sound_motif == "error"
    assert ToastSpec("Q", kind="prompt").sticky
    assert ToastSpec("Q", kind="prompt", sound=False).sound_motif is None
    assert ToastSpec("Q", kind="info", sound="ask").sound_motif == "ask"
    assert ToastSpec("Q", icon="video").icon == "video"
    assert ToastSpec("Q", icon="no-such-icon").icon == "circle-info"


def test_buttons_are_capped_and_the_first_one_leads():
    s = ToastSpec("T", actions=[{"label": "A"}, {"label": "B"}, {"label": "C"}, {"label": "D"}])
    assert [a.label for a in s.actions] == ["A", "B", "C"]
    assert [a.style for a in s.actions] == ["primary", "secondary", "secondary"]
    d = ToastSpec("T", actions=[Action("Stop", style="danger"), Action("Keep")])
    assert [a.style for a in d.actions] == ["danger", "secondary"]
    assert Action("Keep").arg == "Keep" and Action("Keep", arg="k").arg == "k"
    assert ToastSpec("T", actions=[{"label": "  "}]).actions == []


def test_every_icon_has_a_glyph_in_the_bundled_font():
    font = paint._font("icons", 16, 1.0)
    for name, cp in spec.ICONS.items():
        assert font.getmask(chr(cp)).getbbox(), name


# ── Paint ─────────────────────────────────────────────────────────────────────

PAL = theme.resolve("dark", "blue")
LONG = ("A very long title that goes on and on to see how the wrapping and the ellipsis "
        "behave at two lines in total and then some more words")


def test_text_wraps_inside_the_column_and_ends_with_an_ellipsis():
    s = ToastSpec(LONG, " ".join(["body"] * 120))
    lay = paint.layout(s, PAL)
    assert len(lay.title_lines) == paint.TITLE_LINES and lay.title_lines[-1].endswith(paint.ELLIPSIS)
    assert len(lay.body_lines) == paint.BODY_LINES and lay.body_lines[-1].endswith(paint.ELLIPSIS)
    font = paint._font("semibold", paint.TITLE_PX, 1.0)
    assert all(font.getlength(line) <= lay.text_w for line in lay.title_lines)
    short = paint.layout(ToastSpec("Hi"), PAL)
    assert short.card_h == paint.MIN_CARD_H
    assert short.text_y > paint.CONTENT_Y      # one line sits level with the chip


def test_buttons_sit_in_a_row_inside_the_card_and_share_the_width_when_too_wide():
    s = ToastSpec("T", actions=[Action("Start recording"), Action("Not now")])
    lay = paint.layout(s, PAL)
    assert [b.label for b in lay.buttons] == ["Start recording", "Not now"]
    right = max(b.rect[0] + b.rect[2] for b in lay.buttons)
    assert right == paint.CARD_W - paint.PAD
    assert lay.buttons[0].rect[2] > lay.buttons[1].rect[2]     # natural widths
    wide = paint.layout(ToastSpec("T", actions=[Action("Confirm this very long label"),
                                                Action("Another long one too"), Action("And a third")]), PAL)
    widths = [b.rect[2] for b in wide.buttons]
    assert max(widths) - min(widths) <= 1                      # shared, to the pixel
    assert wide.buttons[0].rect[0] == paint.PAD
    assert wide.buttons[-1].rect[0] + wide.buttons[-1].rect[2] == paint.CARD_W - paint.PAD


def test_hit_regions_map_physical_pixels_at_any_scale():
    s = ToastSpec("T", "b", actions=[Action("Go")])
    lay = paint.layout(s, PAL, scale=1.5)
    assert lay.hit(*centre(lay, "close")) == "close"
    assert lay.hit(*centre(lay, "action:0")) == "action:0"
    m = paint.MARGIN * 1.5
    assert lay.hit(m + 10, m + lay.card_h * 1.5 / 2) == "body"
    assert lay.hit(2, 2) is None                      # the shadow margin
    assert lay.window_size == (int(round((paint.CARD_W + 2 * paint.MARGIN) * 1.5)),
                               int(round((lay.card_h + 2 * paint.MARGIN) * 1.5)))


def test_the_picture_has_rounded_corners_a_rail_and_no_white_line_along_the_top():
    s = ToastSpec("Meeting Assistant is NOT recording", "Open the app.", kind="error")
    lay = paint.layout(s, PAL)
    img = paint.paint(lay, paint.PaintState(progress=0.5))
    assert img.mode == "RGBA" and img.size == lay.window_size
    M, W, H = paint.MARGIN, lay.card_w, lay.card_h
    assert img.getpixel((0, 0))[3] == 0                          # outside: clear
    assert img.getpixel((M, M))[3] < 128                         # the corner is cut round
    assert img.getpixel((M + 20, M + 20))[3] == 255              # the card is solid
    assert img.getpixel((M + 1, M + H // 2))[:3] == PAL["red"][:3]   # the kind's rail
    # An error stays until dealt with, so it has no hairline to show.
    assert img.getpixel((M + paint.RAIL_W + 8, M + H - 2)) == PAL["surface"]
    # A timed toast's hairline is the theme's accent, not the kind's colour.
    warn = paint.layout(ToastSpec("Call audio not captured", "Check the device.", kind="warning"), PAL)
    timed = paint.paint(warn, paint.PaintState(progress=0.5))
    line = timed.getpixel((M + paint.RAIL_W + 8, M + warn.card_h - 2))
    assert line == theme.over(theme.with_alpha(PAL["accent"], 0.6), PAL["surface"])
    assert line[:3] != PAL["yellow"][:3]
    # The top edge is the surface, not the solid white line the first build drew:
    # ImageDraw writes alpha literally and the mask then made it opaque.
    top = img.getpixel((M + W // 2, M + 1))
    assert all(abs(top[i] - PAL["surface"][i]) < 40 for i in range(3)), top
    assert top[:3] != (255, 255, 255)


def test_hover_and_progress_change_the_picture_and_scale_changes_its_size():
    s = ToastSpec("T", "b", actions=[Action("Go")])
    lay = paint.layout(s, PAL)
    rest = paint.paint(lay, paint.PaintState()).tobytes()
    assert paint.paint(lay, paint.PaintState(hover="close")).tobytes() != rest
    assert paint.paint(lay, paint.PaintState(hover="action:0")).tobytes() != rest
    assert paint.paint(lay, paint.PaintState(progress=0.3)).tobytes() != rest
    big = paint.paint(paint.layout(s, PAL, scale=2.0))
    assert big.size == (lay.window_size[0] * 2, lay.window_size[1] * 2)


# ── Sounds ────────────────────────────────────────────────────────────────────

def test_every_set_renders_every_motif_cleanly():
    import numpy as np
    for set_id in sounds.SETS:
        seen = []
        for motif in sounds.MOTIF_ORDER:
            x = sounds.render(set_id, motif)
            assert x.dtype == np.float32 and x.ndim == 2 and x.shape[1] == 2
            assert not np.isnan(x).any()
            assert abs(float(np.abs(x).max()) - 0.5) < 1e-3          # normalised to -6 dBFS
            assert 1.0 <= len(x) / sounds.SR <= 2.5
            assert all(y.shape != x.shape or not np.array_equal(y, x) for y in seen)
            seen.append(x)


def test_the_wav_image_is_a_real_wav_and_the_volume_curve_is_sane():
    data = sounds.wav_bytes("glass", "ask", 0.5)
    with wave.open(io.BytesIO(data), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (2, 2, sounds.SR)
        assert w.getnframes() == len(sounds.render("glass", "ask"))
    assert sounds.amplitude(0) == 0.0 and sounds.amplitude(100) == 1.0
    curve = [sounds.amplitude(v) for v in range(0, 101, 10)]
    assert curve == sorted(curve) and 0.2 < sounds.amplitude(50) < 0.5
    assert sounds.amplitude("junk") == sounds.amplitude(100)


def test_export_writes_every_cue_and_a_tour_per_set(tmp_path):
    files = sounds.export(tmp_path)
    assert len(files) == len(sounds.SETS) * (len(sounds.MOTIF_ORDER) + 1)
    tour = tmp_path / "tour-glass.wav"
    assert tour.exists()
    with wave.open(str(tour), "rb") as w:
        assert w.getnframes() > sum(len(sounds.render("glass", m)) for m in sounds.MOTIF_ORDER)


# ── Manager: where they go, how long they stay ────────────────────────────────

def test_a_toast_lands_in_the_bottom_right_corner_of_the_work_area():
    m, host, clock = make()
    h = m.show(ToastSpec("Hello", "there"))
    live = m._live[0]
    cw, ch = live.card_px
    assert live.x + paint.MARGIN + cw == 1920 - mgr.EDGE
    assert live.y + paint.MARGIN + ch == 1040 - mgr.EDGE
    w = host.windows[live.hwnd]
    assert w["x"] == live.x and w["y"] == live.y and w["alpha"] == 1.0
    assert w["image"].size == live.lay.window_size
    assert not h.closed.is_set()


def test_the_newest_toast_takes_the_corner_and_the_older_one_moves_up():
    m, host, clock = make()
    m.show(ToastSpec("First", "one"))
    first = m._live[0]
    y_before = first.y
    m.show(ToastSpec("Second", "two"))
    second = m._live[1]
    assert second.y + paint.MARGIN + second.card_px[1] == 1040 - mgr.EDGE
    assert first.y < y_before
    assert first.y + paint.MARGIN + first.card_px[1] == second.y + paint.MARGIN - mgr.GAP


def test_other_corners_are_honoured():
    m, host, clock = make(prefs=Prefs(position="top-left"))
    m.show(ToastSpec("Hello"))
    live = m._live[0]
    assert live.x + paint.MARGIN == mgr.EDGE and live.y + paint.MARGIN == mgr.EDGE


def test_a_toast_times_out_and_says_so():
    m, host, clock = make()
    reasons = []
    h = m.show(ToastSpec("Hello", on_dismiss=reasons.append))
    hwnd = m._live[0].hwnd
    clock.advance(7.9)
    m.on_tick()
    assert m._live and not h.closed.is_set()
    clock.advance(0.2)
    m.on_tick()
    assert h.closed.is_set() and h.reason == "timeout" and reasons == ["timeout"]
    assert host.destroyed == [hwnd] and not m._live
    assert host.timer == 0


def test_hovering_holds_a_toast_and_leaving_gives_it_a_moment():
    m, host, clock = make()
    h = m.show(ToastSpec("Hello", "body"))
    live = m._live[0]
    m.on_mouse(live.hwnd, "move", *centre(live.lay, "body"))
    assert live.hovered and live.state.hover == "body"
    clock.advance(30)
    m.on_tick()
    assert not h.closed.is_set()
    m.on_mouse(live.hwnd, "leave", -1, -1)
    assert live.state.hover is None
    clock.advance(mgr.HOVER_GRACE_SEC - 0.1)
    m.on_tick()
    assert not h.closed.is_set()
    clock.advance(8)       # the full timeout was still owed
    m.on_tick()
    assert h.reason == "timeout"


def test_sticky_kinds_and_the_sticky_preference_never_time_out():
    m, host, clock = make()
    h = m.show(ToastSpec("Q", kind="prompt"))
    clock.advance(3600)
    m.on_tick()
    assert not h.closed.is_set() and m._live[0].deadline is None
    m2, _, clock2 = make(prefs=Prefs(sticky=True))
    h2 = m2.show(ToastSpec("Info", kind="info"))
    clock2.advance(3600)
    m2.on_tick()
    assert not h2.closed.is_set()


def test_the_timer_runs_only_while_there_is_something_to_do():
    m, host, clock = make()
    m.show(ToastSpec("Q", kind="prompt"))
    m.on_tick()
    assert host.timer == 0                       # sticky and still: nothing to tick for
    m.show(ToastSpec("Timed"))
    m.on_tick()
    assert host.timer == mgr.TICK_IDLE_MS         # a hairline to redraw


# ── Manager: clicks ───────────────────────────────────────────────────────────

def test_a_button_runs_its_handler_and_closes_the_toast():
    m, host, clock = make()
    calls, reasons = [], []
    s = ToastSpec("Zoom meeting detected", "Record it?", kind="prompt", on_dismiss=reasons.append,
                  actions=[Action("Start recording", on_click=calls.append, arg="start"),
                           Action("Not now", on_click=calls.append)])
    h = m.show(s)
    live = m._live[0]
    click(m, live, "action:1")
    assert calls == ["Not now"] and h.reason == "action" and reasons == ["action"]
    assert not m._live


def test_a_press_that_slips_off_the_button_does_nothing():
    m, host, clock = make()
    calls = []
    m.show(ToastSpec("T", actions=[Action("Go", on_click=calls.append)]))
    live = m._live[0]
    x, y = centre(live.lay, "action:0")
    m.on_mouse(live.hwnd, "down", x, y)
    assert live.state.pressed == "action:0"
    m.on_mouse(live.hwnd, "up", *centre(live.lay, "close"))
    assert calls == [] and m._live and live.state.pressed is None


def test_a_button_may_keep_the_toast_open():
    m, host, clock = make()
    calls = []
    m.show(ToastSpec("T", actions=[Action("Snooze", on_click=calls.append, close=False)]))
    live = m._live[0]
    click(m, live, "action:0")
    assert calls == ["Snooze"] and m._live


def test_the_close_button_a_right_click_and_the_body_each_dismiss():
    m, host, clock = make()
    h = m.show(ToastSpec("T"))
    click(m, m._live[0], "close")
    assert h.reason == "closed"
    h = m.show(ToastSpec("T"))
    m.on_mouse(m._live[0].hwnd, "secondary", 10, 10)
    assert h.reason == "closed"
    opened = []
    h = m.show(ToastSpec("T", on_click=opened.append))
    click(m, m._live[0], "body")
    assert opened == [""] and h.reason == "clicked"


def test_the_hand_cursor_shows_only_where_a_click_does_something():
    m, host, clock = make()
    m.show(ToastSpec("Plain"))
    plain = m._live[0]
    m.on_mouse(plain.hwnd, "move", *centre(plain.lay, "body"))
    assert not m.wants_hand(plain.hwnd)
    m.on_mouse(plain.hwnd, "move", *centre(plain.lay, "close"))
    assert m.wants_hand(plain.hwnd)
    m.show(ToastSpec("Clickable", on_click=lambda a: None, actions=[Action("Go")]))
    live = m._live[1]
    m.on_mouse(live.hwnd, "move", *centre(live.lay, "body"))
    assert m.wants_hand(live.hwnd)
    m.on_mouse(live.hwnd, "move", *centre(live.lay, "action:0"))
    assert m.wants_hand(live.hwnd) and live.state.hover == "action:0"


# ── Manager: tags, limits, updates ────────────────────────────────────────────

def test_a_tag_replaces_and_dismisses():
    m, host, clock = make()
    first = m.show(ToastSpec("Zoom meeting detected", tag="meeting"))
    second = m.show(ToastSpec("Teams meeting detected", tag="meeting"))
    assert first.reason == "replaced" and len(m._live) == 1
    m.dismiss("meeting")
    assert second.reason == "dismissed" and not m._live
    third = m.show(ToastSpec("X", tag="t"))
    m.dismiss(third.id)
    assert third.reason == "dismissed"
    m.show(ToastSpec("A"))
    m.show(ToastSpec("B"))
    m.dismiss_all("shutdown")
    assert not m._live


def test_only_so_many_show_at_once_and_the_oldest_loose_one_goes_first():
    m, host, clock = make()
    sticky = m.show(ToastSpec("Keep", kind="error"))
    handles = [m.show(ToastSpec(f"T{i}")) for i in range(mgr.MAX_VISIBLE)]
    assert handles[0].reason == "replaced"
    assert not sticky.closed.is_set() and len(m._live) == mgr.MAX_VISIBLE


def test_updating_the_text_repaints_in_place():
    m, host, clock = make()
    h = m.show(ToastSpec("Starting", "please wait"))
    live = m._live[0]
    before = host.windows[live.hwnd]["image"].tobytes()
    h.update(title="Recording", body="all good")
    assert live.spec.title == "Recording" and live.spec.body == "all good"
    assert host.windows[live.hwnd]["image"].tobytes() != before
    assert not h.closed.is_set()


def test_a_dpi_change_repaints_at_the_new_scale():
    m, host, clock = make()
    m.show(ToastSpec("Hello", "there"))
    live = m._live[0]
    size1 = host.windows[live.hwnd]["image"].size
    host._scale = 2.0
    m.on_display_change()
    assert live.lay.scale == 2.0
    assert host.windows[live.hwnd]["image"].size == (size1[0] * 2, size1[1] * 2)
    assert live.x + paint.MARGIN * 2 + live.card_px[0] == 1920 - mgr.EDGE * 2


def test_flush_waits_for_an_empty_screen():
    m, host, clock = make()
    assert m.flush(0.01)
    h = m.show(ToastSpec("Q", kind="prompt"))
    assert not m.flush(0.01)
    h.dismiss()
    assert m.flush(0.01)


# ── Manager: animation ────────────────────────────────────────────────────────

def test_a_toast_slides_in_and_fades_out_when_animations_are_on():
    m, host, clock = make(animations=True)
    h = m.show(ToastSpec("Hello"))
    live = m._live[0]
    assert live.phase == "in" and live.alpha == 0.0 and live.x > live.tx
    assert host.timer == mgr.TICK_ANIM_MS
    clock.advance(mgr.ENTER_SEC / 2)
    m.on_tick()
    assert 0.0 < live.alpha < 1.0 and live.tx < live.x
    clock.advance(mgr.ENTER_SEC)
    m.on_tick()
    assert live.phase == "shown" and live.alpha == 1.0 and live.x == live.tx
    assert live.deadline is not None and host.timer == mgr.TICK_IDLE_MS
    hwnd = live.hwnd
    h.dismiss()
    assert live.phase == "out" and h.reason == "dismissed" and hwnd in host.windows
    clock.advance(mgr.LEAVE_SEC / 2)
    m.on_tick()
    assert 0.0 < live.alpha < 1.0
    clock.advance(mgr.LEAVE_SEC)
    m.on_tick()
    assert not m._live and host.destroyed == [hwnd] and host.timer == 0


def test_the_others_glide_into_place_when_one_leaves():
    m, host, clock = make(animations=True)
    m.show(ToastSpec("First"))
    clock.advance(1)
    m.on_tick()
    m.show(ToastSpec("Second"))
    first = m._live[0]
    assert first.move_from is not None and first.ty < first.y
    clock.advance(mgr.REFLOW_SEC + 0.05)
    m.on_tick()
    assert first.move_from is None and first.y == first.ty


# ── Manager: sound ────────────────────────────────────────────────────────────

def test_the_kind_picks_the_cue_and_the_settings_pick_the_set_and_gain():
    played = []
    m, host, clock = make(prefs=Prefs(sound_set="wood", volume=50), played=played)
    m.show(ToastSpec("Zoom meeting detected", kind="prompt"))
    assert played == [("wood", "ask", round(sounds.amplitude(50), 3))]
    m.show(ToastSpec("Quiet", kind="info", sound=False))
    assert len(played) == 1
    m.show(ToastSpec("Stopped", kind="info", sound="stopped"))
    assert played[-1][1] == "stopped"


def test_the_volume_means_what_it_says_except_during_a_recording():
    played = []
    m, host, clock = make(played=played, recording=True)
    m.show(ToastSpec("A"))
    base = sounds.amplitude(100) * mgr.RECORDING_GAIN
    assert played[0] == (sounds.DEFAULT_SET, "info", round(base, 3))
    clock.advance(10)
    m.show(ToastSpec("B"))          # the same cue again is not scaled down
    assert played[1] == played[0]
    loud = []
    m2, _, _ = make(played=loud)
    m2.show(ToastSpec("A"))
    m2.show(ToastSpec("B"))
    assert loud == [(sounds.DEFAULT_SET, "info", 1.0)] * 2
    quiet = []
    m3, _, _ = make(prefs=Prefs(play_sounds=False), played=quiet)
    m3.show(ToastSpec("Silent"))
    assert quiet == []


def test_the_preference_defaults_round_trip():
    p = Prefs.from_settings(settings.DEFAULTS)
    assert p == Prefs()
    assert settings.DEFAULTS["notify_position"] == "bottom-right"
    assert settings.DEFAULTS["notify_sound_set"] == "felt" == sounds.DEFAULT_SET
    assert next(iter(sounds.SETS)) == "felt"            # listed first in the picker
    assert settings.DEFAULTS["notify_volume"] == 100
    assert settings.DEFAULTS["notify_sticky"] is False
    assert Prefs.from_settings({"notify_position": "nowhere", "notify_sound_set": "kazoo",
                                "notify_volume": "loud"}) == Prefs()


# ── notifications.py: what the app sends ──────────────────────────────────────

@pytest.fixture
def shown(monkeypatch):
    """Capture what notify() hands the widget, without a window."""
    calls = []

    class _Handle:
        reason = None

    monkeypatch.setattr(toast, "show", lambda title, body="", **kw: (calls.append((title, body, kw)), _Handle())[1])
    monkeypatch.setattr(notifications.recording_request, "request_start_async", lambda *a, **k: None)
    return calls


@pytest.mark.skipif(not WIN, reason="the widget is Windows-only; macOS keeps Notification Center")
def test_notify_keeps_the_old_vocabulary_and_passes_on_the_new(shown):
    assert notifications.notify("T", "b", duration="long") is True
    assert shown[-1][2]["timeout"] == 20.0
    notifications.notify("T", scenario="reminder")
    assert shown[-1][2]["timeout"] == 0.0
    notifications.notify("T", kind="warning", tag="x", timeout=5,
                         actions=[{"label": "Go", "arg": "g", "style": "danger"}, {"label": ""}])
    kw = shown[-1][2]
    assert kw["kind"] == "warning" and kw["tag"] == "x" and kw["timeout"] == 5
    assert [(a.label, a.arg, a.style) for a in kw["actions"]] == [("Go", "g", "danger")]


@pytest.mark.skipif(not WIN, reason="Windows-only")
def test_each_notification_the_app_sends_is_tagged_and_kinded(shown):
    notifications.send_meeting_detected_toast("Zoom", "http://x")
    title, body, kw = shown[-1]
    assert title == "Zoom meeting detected" and kw["kind"] == "prompt" and kw["icon"] == "video"
    assert kw["tag"] == notifications.TAG_MEETING and kw["timeout"] is None
    assert [a.style for a in kw["actions"]] == ["primary", "secondary"]
    notifications.send_quiet_recording_toast("sid", "http://x")
    _, _, kw = shown[-1]
    assert kw["tag"] == notifications.TAG_QUIET and kw["timeout"] == 30
    assert [a.style for a in kw["actions"]] == ["danger", "secondary"]
    assert notifications.send_meeting_autostarted_toast("Teams", "http://x") is True
    _, _, kw = shown[-1]
    assert kw["kind"] == "recording" and kw["tag"] == notifications.TAG_RECORDING
    notifications.send_test_toast("http://x")
    assert len(shown[-1][2]["actions"]) == 2
    notifications.send_test_toast()
    assert len(shown[-1][2]["actions"]) == 1


@pytest.mark.skipif(not WIN, reason="Windows-only")
def test_the_app_takes_notifications_down_when_they_stop_applying(monkeypatch):
    gone = []
    monkeypatch.setattr(toast, "dismiss", lambda tag, reason="dismissed": gone.append(tag))
    notifications.recording_started()
    assert gone == [notifications.TAG_MEETING, notifications.TAG_START_FAILED]
    gone.clear()
    notifications.recording_stopped()
    assert gone == [notifications.TAG_QUIET, notifications.TAG_CAPTURE, notifications.TAG_RECORDING]
    gone.clear()
    notifications.meeting_ended()
    notifications.capture_recovered()
    assert gone == [notifications.TAG_MEETING, notifications.TAG_CAPTURE]


def test_app_py_hooks_every_moment_a_notification_stops_applying():
    start = APP_PY[APP_PY.index("def start_recording("):APP_PY.index("def _concat_video_parts(")]
    assert start.index('"recording": True') < start.index("notifications.recording_started()")
    stop = APP_PY[APP_PY.index("def stop_recording("):]
    stop = stop[:stop.index("_recording_cleanup_done.set()")]
    assert "notifications.recording_stopped()" in stop
    detect = APP_PY[APP_PY.index("def _meeting_detect_loop("):APP_PY.index("def _heartbeat_loop(")]
    assert "notifications.meeting_ended()" in detect
    recovered = APP_PY[APP_PY.index("def _alert_loopback_recovered("):APP_PY.index("def _recording_prereqs_locked(")]
    assert "notifications.capture_recovered()" in recovered
    alarm = APP_PY[APP_PY.index("def _alert_loopback_silent("):APP_PY.index("def _alert_loopback_recovered(")]
    assert "tag=notifications.TAG_CAPTURE" in alarm and "timeout=0" in alarm
    failed = APP_PY[APP_PY.index("def _notify_start_failed("):APP_PY.index("_start_coordinator = ")]
    assert 'kind="error"' in failed and "tag=notifications.TAG_START_FAILED" in failed
    assert "notifications.configure(is_recording=" in APP_PY
    for route in ("/api/notifications/sounds", "/api/notifications/sound", "/api/notifications/test"):
        assert f'@app.route("{route}"' in APP_PY


def test_nothing_imports_a_windows_toast_library_any_more():
    for path in ROOT.rglob("*.py"):
        if ".venv" in path.parts or "storage" in path.parts:
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        assert not re.search(r"^\s*(from|import)\s+(windows_toasts|winotify)\b", src, re.M), path
    for req in ("requirements.txt", "requirements-macos.txt"):
        text = (ROOT / req).read_text(encoding="utf-8")
        assert "windows-toasts" not in text and "winotify" not in text
    assert "from ui_desktop import toast" in (ROOT / "watchdog.py").read_text(encoding="utf-8")
    assert '"Test Notification"' in (ROOT / "ui_desktop/tray.py").read_text(encoding="utf-8")


def test_the_window_never_takes_focus_and_the_callbacks_never_run_on_its_thread():
    assert "WS_EX_NOACTIVATE" in WIN32_PY and "WS_EX_TOPMOST" in WIN32_PY and "WS_EX_TOOLWINDOW" in WIN32_PY
    assert "return MA_NOACTIVATE" in WIN32_PY
    assert "SetThreadDpiAwarenessContext" in WIN32_PY
    src = (ROOT / "ui_desktop/toast/manager.py").read_text(encoding="utf-8")
    run = src[src.index("def _run_callback("):src.index("class ToastManager")]
    assert "threading.Thread(" in run and "daemon=True" in run


def test_the_settings_page_has_the_controls_and_the_client_saves_them():
    for element_id in ("notify-position", "notify-sticky", "notify-sounds", "notify-sound-set",
                       "notify-sound-play", "notify-volume", "notify-quieter", "notify-test-btn"):
        assert f'id="{element_id}"' in SETTINGS_HTML, element_id
    assert "_renderNotifySettings();" in APP_JS
    save = APP_JS[APP_JS.index("function saveNotifySettings("):APP_JS.index("function previewNotifySound(")]
    for key in ("notify_position", "notify_sticky", "notify_sounds", "notify_volume",
                "notify_sound_set", "notify_quieter_while_recording"):
        assert key in save, key
    assert "'/api/preferences'" in save and "JSON.stringify(updates)" in save
    preview = APP_JS[APP_JS.index("function previewNotifySound("):APP_JS.index("async function sendTestNotification(")]
    assert "'/api/notifications/sound'" in preview
    assert "'/api/notifications/test'" in APP_JS
