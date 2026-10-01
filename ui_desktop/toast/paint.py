"""Painting a toast: one RGBA image, drawn with Pillow, that the window shows.

The whole notification is a picture. A per-pixel-alpha layered window can show
any RGBA bitmap, so the rounded corners, the shadow and the anti-aliasing all
come from here rather than from anything Windows draws, and the result is the
same on Windows 10 and 11, at any scale. Hover and pressed states are just
another picture of the same layout.

Everything is laid out in logical pixels and multiplied by ``scale`` (the
monitor's DPI over 96) at paint time, so a 150 % display gets crisp text and
not a stretched 100 % bitmap.

``layout()`` decides where everything goes and how the text wraps, once per
toast; ``paint()`` turns a layout plus a ``PaintState`` into the image. The
hit regions a layout reports are what the window uses to tell a click on a
button from a click on the body.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ui_desktop.toast import theme
from ui_desktop.toast.spec import ICONS, ToastSpec
from ui_desktop.toast.theme import RGBA, Palette

_ROOT = Path(__file__).resolve().parents[2]
_FA_SOLID = _ROOT / "ui_web" / "static" / "fontAwesome" / "webfonts" / "fa-solid-900.woff2"
_LOGO_PNG = _ROOT / "ui_web" / "static" / "images" / "logo.png"

# ── Geometry (logical px) ─────────────────────────────────────────────────────
CARD_W = 380
MARGIN = 32          # room around the card for its shadow
RADIUS = 12
PAD = 16
RAIL_W = 3
HEADER_Y = 11        # top of the logo / app name row
LOGO = 16
CLOSE = 24           # the close button's hit square
CONTENT_Y = 38
CHIP = 36
CHIP_GAP = 12
TITLE_PX, TITLE_LH, TITLE_LINES = 14, 19, 2
BODY_PX, BODY_LH, BODY_LINES = 13, 18, 4
TEXT_GAP = 3
BUTTON_H, BUTTON_R, BUTTON_PAD, BUTTON_GAP = 30, 7, 14, 8
BUTTON_PX = 12.5
ACTIONS_GAP = 13
HAIRLINE = 2
SHADOW_OFFSET, SHADOW_SIGMA = 8, 11
MIN_CARD_H = CONTENT_Y + CHIP + PAD

Rect = tuple[int, int, int, int]   # x, y, w, h


@dataclass
class PaintState:
    hover: Optional[str] = None      # "body" | "close" | "action:<i>" | None
    pressed: Optional[str] = None
    progress: Optional[float] = None  # fraction of the timeout left; None hides the hairline


@dataclass
class _Button:
    index: int
    label: str
    style: str
    rect: Rect


@dataclass
class Layout:
    spec: ToastSpec
    palette: Palette
    scale: float
    card_w: int
    card_h: int
    title_lines: list[str]
    body_lines: list[str]
    text_x: int
    text_y: int
    text_w: int
    buttons: list[_Button]
    regions: dict[str, Rect] = field(default_factory=dict)   # window coords, logical px

    @property
    def window_size(self) -> tuple[int, int]:
        """The bitmap's size in physical pixels, shadow included."""
        return (_px(self.card_w + 2 * MARGIN, self.scale),
                _px(self.card_h + 2 * MARGIN, self.scale))

    def hit(self, x: float, y: float) -> Optional[str]:
        """What is under a point given in physical window pixels."""
        lx, ly = x / self.scale, y / self.scale
        for name in ("close", *[f"action:{b.index}" for b in self.buttons], "body"):
            r = self.regions.get(name)
            if r and r[0] <= lx < r[0] + r[2] and r[1] <= ly < r[1] + r[3]:
                return name
        return None


# ── Fonts and images ──────────────────────────────────────────────────────────

_FONT_FILES = {
    "regular": ("segoeui.ttf", "SegoeUI.ttf"),
    "semibold": ("seguisb.ttf", "SegoeUI-Semibold.ttf", "segoeuib.ttf"),
}
_font_cache: dict[tuple, ImageFont.FreeTypeFont] = {}
_image_cache: dict[tuple, Image.Image] = {}
_cache_lock = threading.Lock()


def _font(style: str, size_px: float, scale: float) -> ImageFont.FreeTypeFont:
    size = max(6, int(round(size_px * scale)))
    key = (style, size)
    with _cache_lock:
        f = _font_cache.get(key)
    if f is not None:
        return f
    f = None
    if style == "icons":
        try:
            f = ImageFont.truetype(str(_FA_SOLID), size)
        except OSError:
            f = None
    else:
        for name in _FONT_FILES[style]:
            try:
                f = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
    if f is None:
        # Not Windows, or a stripped install: Pillow's own scalable fallback.
        try:
            f = ImageFont.load_default(size)
        except TypeError:  # Pillow < 10.1
            f = ImageFont.load_default()
    with _cache_lock:
        _font_cache[key] = f
    return f


def _logo(size: int) -> Optional[Image.Image]:
    """The active icon set's app image, so a custom set shows up here too."""
    key = ("logo", size)
    with _cache_lock:
        img = _image_cache.get(key)
    if img is not None:
        return img
    img = None
    try:
        from core import icons
        img = icons.resolve_image("app").convert("RGBA")
    except Exception:
        try:
            img = Image.open(_LOGO_PNG).convert("RGBA")
        except Exception:
            img = None
    if img is not None:
        img = img.resize((size, size), Image.LANCZOS)
        with _cache_lock:
            _image_cache[key] = img
    return img


def forget_images() -> None:
    """Drop the cached logo (the icon set changed)."""
    with _cache_lock:
        _image_cache.clear()


def _px(v: float, scale: float) -> int:
    return int(round(v * scale))


def _rounded_mask(w: int, h: int, r: int) -> Image.Image:
    """An anti-aliased rounded rectangle, drawn at 4x and brought down."""
    key = ("mask", w, h, r)
    with _cache_lock:
        m = _image_cache.get(key)
    if m is not None:
        return m
    if w <= 0 or h <= 0:
        return Image.new("L", (max(w, 1), max(h, 1)), 0)
    big = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(big).rounded_rectangle([0, 0, w * 4 - 1, h * 4 - 1], radius=r * 4, fill=255)
    m = big.resize((w, h), Image.LANCZOS)
    with _cache_lock:
        _image_cache[key] = m
    return m


def _circle_mask(d: int) -> Image.Image:
    return _rounded_mask(d, d, d // 2)


def _fill(img: Image.Image, rect: Rect, color: RGBA, mask: Optional[Image.Image] = None) -> None:
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return
    layer = Image.new("RGBA", (w, h), color)
    img.paste(layer, (x, y), mask if mask is not None else layer)


def _rounded_fill(img: Image.Image, rect: Rect, r: int, color: RGBA,
                  border: Optional[RGBA] = None) -> None:
    x, y, w, h = rect
    if border is not None:
        _fill(img, (x, y, w, h), border, _rounded_mask(w, h, r))
        _fill(img, (x + 1, y + 1, w - 2, h - 2), color, _rounded_mask(w - 2, h - 2, max(r - 1, 1)))
    else:
        _fill(img, (x, y, w, h), color, _rounded_mask(w, h, r))


def _glyph(draw: ImageDraw.ImageDraw, cx: float, cy: float, name: str,
           font: ImageFont.FreeTypeFont, color: RGBA) -> None:
    """A Font Awesome glyph centred on (cx, cy) by its ink box, not its em box."""
    ch = chr(ICONS[name])
    l, t, r, b = font.getbbox(ch)
    draw.text((cx - (l + r) / 2.0, cy - (t + b) / 2.0), ch, font=font, fill=color)


# ── Text ──────────────────────────────────────────────────────────────────────

ELLIPSIS = "…"


def _trim(text: str, font: ImageFont.FreeTypeFont, max_w: float) -> str:
    """``text`` cut to fit ``max_w`` with an ellipsis."""
    if font.getlength(text) <= max_w:
        return text
    t = text
    while t and font.getlength(t.rstrip() + ELLIPSIS) > max_w:
        t = t[:-1]
    return t.rstrip() + ELLIPSIS if t else ELLIPSIS


def wrap(text: str, font: ImageFont.FreeTypeFont, max_w: float, max_lines: int) -> list[str]:
    """Greedy word wrap; a word wider than the column is broken, and anything
    past ``max_lines`` is folded into the last line with an ellipsis."""
    if not text or max_lines <= 0:
        return []
    lines: list[str] = []
    cur = ""
    for word in text.split():
        candidate = f"{cur} {word}".strip()
        if font.getlength(candidate) <= max_w:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
            cur = ""
        while font.getlength(word) > max_w and len(word) > 1:
            cut = len(word)
            while cut > 1 and font.getlength(word[:cut]) > max_w:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        rest = " ".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [_trim(rest, font, max_w)]
    return lines


# ── Layout ────────────────────────────────────────────────────────────────────

def layout(spec: ToastSpec, palette: Palette, scale: float = 1.0) -> Layout:
    scale = max(0.5, float(scale))
    title_font = _font("semibold", TITLE_PX, scale)
    body_font = _font("regular", BODY_PX, scale)
    button_font = _font("semibold", BUTTON_PX, scale)

    text_x = PAD + CHIP + CHIP_GAP
    text_w = CARD_W - text_x - PAD - 2
    title_lines = wrap(spec.title, title_font, text_w * scale, TITLE_LINES)
    body_lines = wrap(spec.body, body_font, text_w * scale, BODY_LINES)
    text_h = len(title_lines) * TITLE_LH
    if body_lines:
        text_h += TEXT_GAP + len(body_lines) * BODY_LH
    # A short text block sits level with the chip rather than hanging off its top.
    text_y = CONTENT_Y + max(0, (CHIP - text_h) // 2) if text_h < CHIP else CONTENT_Y
    y = max(CONTENT_Y + CHIP, text_y + text_h)

    buttons: list[_Button] = []
    if spec.actions:
        y += ACTIONS_GAP
        avail = CARD_W - 2 * PAD
        widths = [int(round(button_font.getlength(a.label) / scale)) + 2 * BUTTON_PAD
                  for a in spec.actions]
        total = sum(widths) + BUTTON_GAP * (len(widths) - 1)
        if total > avail:
            # Too wide to sit in a row at their natural size: share the width,
            # the odd pixels going to the first buttons so the row fills it.
            n = len(widths)
            room = avail - BUTTON_GAP * (n - 1)
            widths = [room // n + (1 if i < room % n else 0) for i in range(n)]
            total = avail
        x = CARD_W - PAD - total
        for i, (a, w) in enumerate(zip(spec.actions, widths)):
            buttons.append(_Button(i, a.label, a.style, (x, y, w, BUTTON_H)))
            x += w + BUTTON_GAP
        y += BUTTON_H
    card_h = max(MIN_CARD_H, y + PAD)

    lay = Layout(spec, palette, scale, CARD_W, card_h, title_lines, body_lines,
                 text_x, text_y, text_w, buttons)
    m = MARGIN
    lay.regions["card"] = (m, m, CARD_W, card_h)
    lay.regions["close"] = (m + CARD_W - 12 - CLOSE, m + 7, CLOSE, CLOSE)
    for b in buttons:
        bx, by, bw, bh = b.rect
        lay.regions[f"action:{b.index}"] = (m + bx, m + by, bw, bh)
    lay.regions["body"] = (m, m, CARD_W, card_h)
    return lay


# ── Painting ──────────────────────────────────────────────────────────────────

def _button_colors(pal: Palette, style: str, hover: bool, pressed: bool) -> tuple[RGBA, RGBA, Optional[RGBA]]:
    """(fill, text, border) for a button in one state."""
    black = (0, 0, 0, 255)
    if style == "primary":
        fill = pal["accent-dim"] if hover else pal["accent"]
        if pressed:
            fill = theme.blend(fill, black, 0.12)
        return fill, pal["bg"], None
    if style == "danger":
        fill = theme.blend(pal["red"], black, 0.15) if hover else pal["red"]
        if pressed:
            fill = theme.blend(fill, black, 0.12)
        return fill, pal["bg"], None
    fill = pal["surface3"] if hover else pal["surface2"]
    if pressed:
        fill = theme.blend(fill, pal["fg"], 0.06)
    return fill, pal["fg"], theme.over(pal["border"], fill)


def paint(lay: Layout, state: Optional[PaintState] = None) -> Image.Image:
    """The toast as an RGBA image of ``lay.window_size`` physical pixels."""
    state = state or PaintState()
    pal, s, spec = lay.palette, lay.scale, lay.spec
    W, H = _px(lay.card_w, s), _px(lay.card_h, s)
    M = _px(MARGIN, s)
    cw, ch = lay.window_size
    kind = pal.kind_color(spec.kind)
    hovering = state.hover is not None

    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))

    # Shadow: the stylesheet's --shadow-lg, as a blurred copy of the card's shape.
    radius = _px(RADIUS, s)
    sh = Image.new("L", (cw, ch), 0)
    sh.paste(_rounded_mask(W, H, radius), (M, M + _px(SHADOW_OFFSET, s)))
    sh = sh.filter(ImageFilter.GaussianBlur(SHADOW_SIGMA * s))
    strength = 0.5 if pal.is_dark else 0.16
    shadow = Image.new("RGBA", (cw, ch), (0, 0, 0, 0) if pal.is_dark else (31, 35, 40, 0))
    shadow.putalpha(sh.point(lambda a: int(a * strength)))
    canvas = Image.alpha_composite(canvas, shadow)

    # The card: a border-coloured plate with the surface inset one pixel.
    surface = pal["surface"]
    border = theme.over(pal["border"], surface)
    if hovering:
        border = theme.blend(border, pal["fg"], 0.16)
    card = Image.new("RGBA", (W, H), border)
    inset = max(1, _px(1, s))
    _fill(card, (inset, inset, W - 2 * inset, H - 2 * inset), surface,
          _rounded_mask(W - 2 * inset, H - 2 * inset, max(radius - inset, 1)))
    draw = ImageDraw.Draw(card)
    # The kind's rail down the left edge, like the in-page toasts' left border.
    draw.rectangle([0, 0, _px(RAIL_W, s) - 1, H], fill=kind)
    if pal.is_dark:
        # A hairline of light along the top edge lifts the card off the desktop.
        # Composited here: ImageDraw writes alpha literally rather than blending,
        # and the card's alpha is replaced by its mask below, so a translucent
        # fill would come out solid white.
        sheen = theme.over((255, 255, 255, 18), surface)
        draw.line([(radius, inset), (W - radius - 1, inset)], fill=sheen, width=1)

    # Header: logo, app name, close.
    logo = _logo(_px(LOGO, s))
    hx, hy = _px(PAD, s), _px(HEADER_Y, s)
    if logo is not None:
        card.paste(logo, (hx, hy), logo)
        hx += logo.width + _px(7, s)
    name_font = _font("semibold", 11, s)
    asc, desc = name_font.getmetrics()
    draw.text((hx, hy + (_px(LOGO, s) + asc - desc) // 2), spec.app_name,
              font=name_font, fill=pal["fg-muted"], anchor="ls")
    cx0, cy0, cwid, chgt = lay.regions["close"]
    ccx, ccy = _px(cx0 - MARGIN + cwid / 2, s), _px(cy0 - MARGIN + chgt / 2, s)
    close_hot = state.hover == "close"
    if close_hot:
        d = _px(CLOSE - 2, s)
        _fill(card, (ccx - d // 2, ccy - d // 2, d, d),
              theme.blend(pal["surface3"], pal["fg"], 0.08 if state.pressed == "close" else 0.0),
              _circle_mask(d))
    _glyph(draw, ccx, ccy, "xmark", _font("icons", 11, s),
           pal["fg"] if close_hot else pal["fg-muted"])

    # The icon chip: a tinted disc with the kind's glyph.
    chip = _px(CHIP, s)
    chip_x, chip_y = _px(PAD, s), _px(CONTENT_Y, s)
    tint = theme.over(theme.with_alpha(kind, 0.16 if pal.is_dark else 0.12), surface)
    _fill(card, (chip_x, chip_y, chip, chip), tint, _circle_mask(chip))
    _glyph(draw, chip_x + chip / 2, chip_y + chip / 2, spec.icon, _font("icons", 16, s), kind)

    # Title and body.
    tx = _px(lay.text_x, s)
    y = _px(lay.text_y, s)
    title_font = _font("semibold", TITLE_PX, s)
    asc, desc = title_font.getmetrics()
    lh = _px(TITLE_LH, s)
    for line in lay.title_lines:
        draw.text((tx, y + (lh + asc - desc) // 2), line, font=title_font, fill=pal["fg"], anchor="ls")
        y += lh
    if lay.body_lines:
        y += _px(TEXT_GAP, s)
        body_font = _font("regular", BODY_PX, s)
        asc, desc = body_font.getmetrics()
        lh = _px(BODY_LH, s)
        for line in lay.body_lines:
            draw.text((tx, y + (lh + asc - desc) // 2), line, font=body_font,
                      fill=pal["fg-muted"], anchor="ls")
            y += lh

    # Buttons.
    button_font = _font("semibold", BUTTON_PX, s)
    asc, desc = button_font.getmetrics()
    for b in lay.buttons:
        name = f"action:{b.index}"
        fill, text, edge = _button_colors(pal, b.style, state.hover == name, state.pressed == name)
        bx, by, bw, bh = (_px(v, s) for v in b.rect)
        _rounded_fill(card, (bx, by, bw, bh), _px(BUTTON_R, s), fill, edge)
        label = _trim(b.label, button_font, bw - _px(BUTTON_PAD, s))
        draw.text((bx + bw / 2, by + (bh + asc - desc) // 2), label, font=button_font,
                  fill=text, anchor="ms")

    # The time left, as a hairline along the bottom edge.
    if state.progress is not None and not spec.sticky:
        frac = max(0.0, min(1.0, float(state.progress)))
        hw = int(round((W - _px(RAIL_W, s)) * frac))
        if hw > 0:
            hy = H - inset - _px(HAIRLINE, s)
            draw.rectangle([_px(RAIL_W, s), hy, _px(RAIL_W, s) + hw, H - inset],
                           fill=theme.over(theme.with_alpha(kind, 0.55), surface))

    card.putalpha(_rounded_mask(W, H, radius))
    plate = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    plate.paste(card, (M, M))
    return Image.alpha_composite(canvas, plate)


def preview(spec: ToastSpec, palette: Palette, scale: float = 1.0,
            state: Optional[PaintState] = None, backdrop: Optional[RGBA] = None) -> Image.Image:
    """The toast on a flat background, for looking at it outside a window."""
    lay = layout(spec, palette, scale)
    img = paint(lay, state)
    if backdrop is None:
        backdrop = (24, 26, 32, 255) if palette.is_dark else (229, 232, 236, 255)
    out = Image.new("RGBA", img.size, backdrop)
    return Image.alpha_composite(out, img)


if __name__ == "__main__":  # pragma: no cover - a hand check
    from ui_desktop.toast.spec import Action
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    demo = ToastSpec("Zoom meeting detected", "Want to record and transcribe it?", kind="prompt",
                     icon="video", actions=[Action("Start recording"), Action("Not now")])
    for mode in ("dark", "light"):
        for accent in theme.ACCENTS[:-1]:
            img = preview(demo, theme.resolve(mode, accent), 1.0, PaintState(progress=0.6))
            img.save(out_dir / f"toast-{mode}-{accent}.png")
