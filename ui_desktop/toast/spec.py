"""What a notification is made of, before it is painted or shown.

``ToastSpec`` is the caller's side of the widget: text, a kind, optional
buttons and callbacks. Everything about how it looks and how long it stays
is derived from the kind unless the caller says otherwise, so a new kind of
notification is one call with a title and a body.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

APP_NAME = "Meeting Assistant"

# The kinds a notification can be. Colour comes from theme.KIND_TOKENS; the
# rest of what a kind implies is here. timeout 0 means the toast stays until
# someone deals with it, which is right for a question or a failure and
# wrong for "recording started".
KIND_DEFAULTS: dict[str, dict] = {
    "info":      {"icon": "circle-info",          "timeout": 8.0,  "sound": "info"},
    "success":   {"icon": "circle-check",         "timeout": 8.0,  "sound": "success"},
    "warning":   {"icon": "triangle-exclamation", "timeout": 15.0, "sound": "warning"},
    "error":     {"icon": "circle-exclamation",   "timeout": 0.0,  "sound": "error"},
    "prompt":    {"icon": "circle-question",      "timeout": 0.0,  "sound": "ask"},
    "recording": {"icon": "microphone",           "timeout": 10.0, "sound": "started"},
}
KINDS = tuple(KIND_DEFAULTS)

# Font Awesome solid glyphs the toast can show, by their FA name. Rendered
# from the bundled fa-solid-900.woff2, so they are the icons the app uses.
ICONS: dict[str, int] = {
    "microphone": 0xF130,
    "microphone-slash": 0xF131,
    "video": 0xF03D,
    "bell": 0xF0F3,
    "triangle-exclamation": 0xF071,
    "circle-exclamation": 0xF06A,
    "circle-check": 0xF058,
    "circle-info": 0xF05A,
    "circle-question": 0xF059,
    "circle-xmark": 0xF057,
    "circle-stop": 0xF28D,
    "circle-pause": 0xF28B,
    "circle-dot": 0xF192,
    "xmark": 0xF00D,
    "check": 0xF00C,
    "play": 0xF04B,
    "stop": 0xF04D,
    "record-vinyl": 0xF8D9,
    "wand-magic-sparkles": 0xE2CA,
    "sparkles": 0xF890,
    "calendar-days": 0xF073,
    "headset": 0xF590,
    "volume-high": 0xF028,
    "volume-xmark": 0xF6A9,
    "moon": 0xF186,
    "bolt": 0xF0E7,
    "gear": 0xF013,
    "arrow-up-right-from-square": 0xF08E,
    "comment-dots": 0xF4AD,
    "clock": 0xF017,
    "hourglass-half": 0xF252,
    "user-group": 0xF500,
    "people-group": 0xE533,
    "display": 0xF390,
    "rotate-right": 0xF2F9,
    "heart-pulse": 0xF21E,
    "file-lines": 0xF15C,
    "waveform-lines": 0xF8F2,
    "phone": 0xF095,
    "eye-slash": 0xF070,
}

BUTTON_STYLES = ("primary", "secondary", "danger")


@dataclass
class Action:
    """A button on the toast.

    ``on_click`` runs off the UI thread with ``arg`` (the label when no arg
    was given). ``close`` is whether the toast goes away once it is pressed,
    which is nearly always: a button that keeps the toast open is for things
    like "snooze".
    """
    label: str
    on_click: Optional[Callable[[str], None]] = None
    style: str = "secondary"
    arg: str = ""
    close: bool = True

    def __post_init__(self) -> None:
        self.label = str(self.label).strip()
        if self.style not in BUTTON_STYLES:
            self.style = "secondary"
        if not self.arg:
            self.arg = self.label


@dataclass
class ToastSpec:
    title: str
    body: str = ""
    kind: str = "info"
    icon: Optional[str] = None             # an ICONS name; None picks the kind's
    actions: list[Action] = field(default_factory=list)
    on_click: Optional[Callable[[str], None]] = None     # the body; arg is ""
    on_dismiss: Optional[Callable[[str], None]] = None   # receives the reason
    timeout: Optional[float] = None        # seconds; None = kind's; 0 = sticky
    tag: Optional[str] = None              # a new toast with the same tag replaces the old
    sound: object = None                   # None = kind's motif; False = silent; str = a motif
    app_name: str = APP_NAME

    def __post_init__(self) -> None:
        if self.kind not in KIND_DEFAULTS:
            self.kind = "info"
        self.title = " ".join(str(self.title or "").split())
        self.body = str(self.body or "").strip()
        if self.icon not in ICONS:
            self.icon = KIND_DEFAULTS[self.kind]["icon"]
        self.actions = [a if isinstance(a, Action) else Action(**a) for a in (self.actions or [])]
        self.actions = [a for a in self.actions if a.label][:3]
        if self.actions and all(a.style == "secondary" for a in self.actions):
            self.actions[0].style = "primary"
        if self.timeout is None:
            self.timeout = float(KIND_DEFAULTS[self.kind]["timeout"])
        self.timeout = max(0.0, float(self.timeout))

    @property
    def sticky(self) -> bool:
        return self.timeout <= 0

    @property
    def sound_motif(self) -> Optional[str]:
        """The sound to play, or None for silence."""
        if self.sound is False:
            return None
        if isinstance(self.sound, str) and self.sound:
            return self.sound
        return KIND_DEFAULTS[self.kind]["sound"]
