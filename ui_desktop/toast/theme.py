"""The toast's colours, taken from the app's own stylesheet.

A notification is the app stepping outside its window, and it should look like
the app did: the same surface, the same text colour, the same accent the user
picked in Settings > System. Rather than keep a second copy of the palette
here, the theme blocks in ``ui_web/static/style.css`` are read directly, so a
change to the stylesheet reaches the toast with nothing to update.

Three inputs decide the palette, all read at show time:

  theme_mode    "system" | "light" | "dark"   (system asks Windows)
  theme_accent  "blue" | "ocean" | ... | "mono" | "custom"
  theme_custom  {"accent": "#rrggbb", "strength": 0..100}, for "custom"

The custom accent is derived the way ``_deriveCustomPalette`` in app.js does it,
with the same blend amounts, so a custom theme matches on both sides.
"""
from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
STYLE_CSS = _ROOT / "ui_web" / "static" / "style.css"

MODES = ("system", "light", "dark")
ACCENTS = ("blue", "ocean", "forest", "sunset", "rose", "violet", "amber", "crimson", "mono", "custom")

# The variables a toast paints with. Everything else in the blocks is ignored.
_WANTED = (
    "bg", "surface", "surface2", "surface3", "border", "border-sub",
    "fg", "fg-muted", "fg-subtle", "accent", "accent-dim", "accent-dim2",
    "green", "red", "yellow", "purple",
)

# Mirrors THEME_BASE and the blend amounts in app.js (_deriveCustomPalette).
_CUSTOM_BASE = {
    "dark": {"bg": "#0d1117", "surface": "#161b22", "surface2": "#21262d",
             "surface3": "#2d333b", "border": "#484f58"},
    "light": {"bg": "#ffffff", "surface": "#f6f8fa", "surface2": "#eaeef2",
              "surface3": "#d8dee4", "border": "#d0d7de"},
}
CUSTOM_DEFAULT = {"accent": "#58a6ff", "strength": 30}

RGBA = tuple[int, int, int, int]


@dataclass(frozen=True)
class Palette:
    """One resolved theme, as RGBA tuples ready for Pillow."""
    mode: str                      # "dark" | "light", never "system"
    accent_name: str
    colors: dict[str, RGBA] = field(default_factory=dict)

    def __getitem__(self, name: str) -> RGBA:
        return self.colors[name]

    @property
    def is_dark(self) -> bool:
        return self.mode == "dark"

    def kind_color(self, kind: str) -> RGBA:
        """The colour a notification kind is painted in: the accent for anything
        neutral, green for a success, yellow for a warning, red for an error."""
        return self.colors[KIND_TOKENS.get(kind, "accent")]


# What each notification kind borrows from the palette. Same mapping as the
# in-page toasts (.ui-toast-success/-warn/-error in style.css).
KIND_TOKENS = {
    "info": "accent",
    "prompt": "accent",
    "recording": "red",
    "success": "green",
    "warning": "yellow",
    "error": "red",
}


# ── Colour maths ──────────────────────────────────────────────────────────────

def parse_color(value: str) -> RGBA | None:
    """``#rgb``, ``#rrggbb``, ``#rrggbbaa`` or ``rgba(r, g, b, a)`` to RGBA."""
    s = (value or "").strip()
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            h += "ff"
        if len(h) != 8 or not re.fullmatch(r"[0-9a-fA-F]{8}", h):
            return None
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4, 6))  # type: ignore[return-value]
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", s)
    if m:
        r, g, b = (int(round(float(m.group(i)))) for i in (1, 2, 3))
        a = int(round(float(m.group(4)) * 255)) if m.group(4) is not None else 255
        return (r, g, b, max(0, min(255, a)))
    return None


def to_hex(c: RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(*c[:3])


def blend(a: RGBA, b: RGBA, t: float) -> RGBA:
    """``a`` moved ``t`` of the way to ``b`` (alpha included)."""
    t = max(0.0, min(1.0, t))
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))  # type: ignore[return-value]


def with_alpha(c: RGBA, alpha: float) -> RGBA:
    return (c[0], c[1], c[2], max(0, min(255, int(round(alpha * 255)))))


def over(fg: RGBA, bg: RGBA) -> RGBA:
    """``fg`` composited over an opaque ``bg``; the solid colour the eye sees."""
    a = fg[3] / 255.0
    return tuple(int(round(f * a + b * (1 - a))) for f, b in zip(fg[:3], bg[:3])) + (255,)  # type: ignore[return-value]


def luminance(c: RGBA) -> float:
    def lin(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])


def contrast(a: RGBA, b: RGBA) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ── Reading the stylesheet ────────────────────────────────────────────────────

_BLOCK_RE = re.compile(r"(:root[^{}]*)\{([^{}]*)\}", re.S)
_VAR_RE = re.compile(r"--([a-zA-Z0-9-]+)\s*:\s*([^;]+);")

_css_cache: dict[str, object] = {"key": None, "blocks": None}
_css_lock = threading.Lock()


def _read_blocks(css_path: Path = STYLE_CSS) -> list[tuple[str, dict[str, str]]]:
    """Every ``:root[...] { --x: y; }`` block, in file order, as (selector, vars).

    Cached on the file's size and mtime, so a stylesheet edit is picked up by
    the next toast without a restart.
    """
    try:
        st = css_path.stat()
        key = (str(css_path), st.st_size, st.st_mtime_ns)
    except OSError:
        key = (str(css_path), 0, 0)
    with _css_lock:
        if _css_cache["key"] == key:
            return _css_cache["blocks"]  # type: ignore[return-value]
    text = css_path.read_text(encoding="utf-8")
    blocks: list[tuple[str, dict[str, str]]] = []
    for sel, body in _BLOCK_RE.findall(text):
        for selector in sel.split(","):
            selector = selector.strip()
            if not selector.startswith(":root"):
                continue
            variables = {k: v.strip() for k, v in _VAR_RE.findall(body)}
            if variables:
                blocks.append((selector, variables))
    with _css_lock:
        _css_cache["key"] = key
        _css_cache["blocks"] = blocks
    return blocks


def _selector_matches(selector: str, mode: str, accent: str) -> bool:
    """Whether a ``:root[...]`` selector applies to the document the app would
    build for this mode and accent: ``data-theme-mode`` is always set, and
    ``data-accent`` only when the accent is not the default blue."""
    attrs = dict(re.findall(r"\[data-([a-z-]+)=\"([^\"]+)\"\]", selector))
    if re.sub(r"\[[^\]]*\]", "", selector).strip() != ":root":
        return False
    for name, value in attrs.items():
        if name == "theme-mode" and value != mode:
            return False
        if name == "accent" and (accent == "blue" or value != accent):
            return False
        if name not in ("theme-mode", "accent"):
            return False
    return True


def css_palette(mode: str, accent: str, css_path: Path = STYLE_CSS) -> dict[str, RGBA]:
    """The variables the stylesheet resolves to for ``mode`` and ``accent``,
    applied in file order like the cascade would (later blocks win)."""
    raw: dict[str, str] = {}
    for selector, variables in _read_blocks(css_path):
        if _selector_matches(selector, mode, accent):
            raw.update(variables)
    out: dict[str, RGBA] = {}
    for name in _WANTED:
        c = parse_color(raw.get(name, ""))
        if c is not None:
            out[name] = c
    return out


def custom_palette(mode: str, cfg: dict | None) -> dict[str, RGBA]:
    """The ``custom`` accent: app.js's _deriveCustomPalette, in Python."""
    cfg = {**CUSTOM_DEFAULT, **(cfg or {})}
    accent = parse_color(str(cfg.get("accent", ""))) or parse_color(CUSTOM_DEFAULT["accent"])
    try:
        strength = float(cfg.get("strength", 30))
    except (TypeError, ValueError):
        strength = 30.0
    t = max(0.0, min(1.0, strength / 100.0))
    is_dark = mode != "light"
    base = {k: parse_color(v) for k, v in _CUSTOM_BASE["dark" if is_dark else "light"].items()}
    mix = t * 0.13
    black, white = (0, 0, 0, 255), (255, 255, 255, 255)
    out = {
        "accent": accent,
        "accent-dim": blend(accent, black, 0.28),
        "accent-dim2": blend(accent, black, 0.82) if is_dark else blend(accent, white, 0.88),
        "bg": blend(base["bg"], accent, mix),
        "surface": blend(base["surface"], accent, mix),
        "surface2": blend(base["surface2"], accent, mix),
        "surface3": blend(base["surface3"], accent, mix),
        "border-sub": blend(base["surface2"], accent, mix),
    }
    border = blend(base["border"], accent, mix)
    out["border"] = with_alpha(border, 0x5c / 255) if is_dark else border
    return out


# ── What the app has chosen ───────────────────────────────────────────────────

def system_prefers_dark() -> bool:
    """Windows' app theme (Settings > Personalization > Colors). Dark when it
    cannot be read: that is the app's own default too."""
    if sys.platform != "win32":
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            value, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        return int(value) == 0
    except OSError:
        return True


def effective_mode(mode: str | None) -> str:
    if mode == "light":
        return "light"
    if mode == "dark":
        return "dark"
    return "dark" if system_prefers_dark() else "light"


def resolve(mode: str | None = None, accent: str | None = None,
            custom: dict | None = None, css_path: Path = STYLE_CSS) -> Palette:
    """The palette for an explicit choice. ``mode`` may be "system"."""
    eff = effective_mode(mode)
    accent = accent if accent in ACCENTS else "blue"
    colors = css_palette(eff, "blue", css_path)       # the base of every accent
    if accent == "custom":
        colors.update(custom_palette(eff, custom))
    elif accent != "blue":
        colors.update(css_palette(eff, accent, css_path))
    missing = [n for n in _WANTED if n not in colors]
    if missing:
        raise ValueError(f"style.css is missing theme variables: {', '.join(missing)}")
    return Palette(mode=eff, accent_name=accent, colors=colors)


def current() -> Palette:
    """The palette for the theme the user has saved, read from settings."""
    mode, accent, custom = "system", "blue", None
    try:
        from core import settings
        prefs = settings.load()
        mode = prefs.get("theme_mode") or "system"
        accent = prefs.get("theme_accent") or "blue"
        custom = prefs.get("theme_custom") or None
    except Exception:
        pass
    try:
        return resolve(mode, accent, custom)
    except Exception:
        return resolve("dark", "blue", None)
