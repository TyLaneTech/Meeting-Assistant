"""Icon sets: the images behind every app and tray state.

Every state the app shows an icon for is a named *slot* (the sidebar logo, the
tab and installed-app icon, and the six tray states). A *set* supplies an image
for every slot. Two sets are built in:

  * ``default``  the owner's H-and-microphone logo (ui_web/static/images)
  * ``wave``     the sound-bars tile from Pat Gordon's build (images/sets/wave)

A custom set is a copy of another set that the user can edit slot by slot. It
lives with the user's data, under <data>/icons/sets/<id>/, as one PNG per slot
plus a set.json with its name and the built-in set it started from ("base"),
so an app update never touches it and it never enters git. Built-in sets are
read-only: to change an icon, copy the set first.

The active set is the ``icon_set`` setting. Changing it re-resolves every
consumer:

  * /api/icons/<slot>          the sidebar brand and the installed-app manifest
  * /api/icons/<slot>.ico      the favicon, a multi-frame ICO rendered here so
                               the tab and the window icon Chrome derives from
                               it start from crisp small frames
  * ui_desktop.tray            reloads the six tray_* slots
  * the Start Menu shortcut    re-pointed at an ICO of the set's app icon
                               (sync_shortcut_icon, Windows)
"""
from __future__ import annotations

import io
import json
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from core import paths as paths
from core import settings as settings

_IMAGES_DIR = Path(__file__).parent.parent / "ui_web" / "static" / "images"
_BUNDLED_ICO = _IMAGES_DIR / "logo.ico"

DEFAULT_SET = "default"

# Largest edge kept for a custom upload. Tray icons render at 64 px and the
# installed-app manifest asks for 512, so anything bigger is wasted bytes.
MAX_EDGE = 512
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
# Largest edge served when no ?size= is asked for.
DEFAULT_SERVE_EDGE = 256
# Frames written into the favicon ICO. Windows and Chrome pick the exact frame
# for the tab (16), the title bar (16, 32) and the taskbar (32, 48), so each
# one is rendered straight from the source rather than scaled from a neighbour.
ICO_FRAMES = (16, 24, 32, 48, 64, 128, 256)

# Tints applied to a set's base images for the derived tray states (RGB).
# These match the colours the tray has always used for those states.
_GRAY   = (110, 118, 129)
_AMBER  = (210, 153,  34)
_ORANGE = (255, 140,   0)
_BLUE   = ( 88, 166, 255)

# A built-in set supplies an image per *role*. "tray" and "tray_recording"
# fall back to "app" and "app_recording" when a set has no separate tray art.
BUILTIN_SETS: dict[str, dict] = {
    "default": {
        "name": "Meeting Assistant",
        "desc": "The H and microphone logo the app ships with.",
        "files": {
            "app":           "logo.png",
            "app_recording": "logo_recording.png",
        },
    },
    "wave": {
        "name": "Wave",
        "desc": "Sound bars on a dark tile, from Pat Gordon's build.",
        "files": {
            "app":            "sets/wave/app.png",
            "app_recording":  "sets/wave/app_recording.png",
            "tray":           "sets/wave/tray.png",
            "tray_recording": "sets/wave/tray_recording.png",
        },
    },
}

# slot -> group, label, description, the role it is drawn from, tint or None
SLOTS: dict[str, dict] = {
    "app_idle": {
        "group": "App",
        "label": "App icon",
        "desc":  "The sidebar logo, the browser tab, the installed app, and the Start Menu shortcut.",
        "role":  "app",
        "tint":  None,
    },
    "app_recording": {
        "group": "App",
        "label": "App icon while recording",
        "desc":  "Swapped in for the tab and sidebar logo while a recording is running.",
        "role":  "app_recording",
        "tint":  None,
    },
    "tray_ready": {
        "group": "System tray",
        "label": "Ready",
        "desc":  "Models are loaded and nothing is recording.",
        "role":  "tray",
        "tint":  None,
    },
    "tray_recording": {
        "group": "System tray",
        "label": "Recording",
        "desc":  "A recording is running and audio is flowing.",
        "role":  "tray_recording",
        "tint":  None,
    },
    "tray_recording_silent": {
        "group": "System tray",
        "label": "Recording, no audio",
        "desc":  "A recording is running but the desktop capture has gone quiet.",
        "role":  "tray_recording",
        "tint":  _ORANGE,
    },
    "tray_loading": {
        "group": "System tray",
        "label": "Loading",
        "desc":  "Models are still loading after launch.",
        "role":  "tray",
        "tint":  _GRAY,
    },
    "tray_setup": {
        "group": "System tray",
        "label": "Setup required",
        "desc":  "An API key is missing, so the app is waiting on Settings.",
        "role":  "tray",
        "tint":  _AMBER,
    },
    "tray_reanalyzing": {
        "group": "System tray",
        "label": "Reanalyzing",
        "desc":  "A meeting is being reanalyzed, which blocks recording until it finishes.",
        "role":  "tray",
        "tint":  _BLUE,
    },
}

_lock = threading.Lock()
# (set, slot, size) -> (version, bytes). Cleared by invalidate(). The ICO
# variant is keyed with size "ico".
_png_cache: dict[tuple[str, str, int | str | None], tuple[str, bytes]] = {}
_migrated = False


# ── Paths and set registry ───────────────────────────────────────────────────

def icons_dir() -> Path:
    return paths.data_dir() / "icons"


def sets_dir() -> Path:
    return icons_dir() / "sets"


def _set_dir(set_id: str) -> Path:
    return sets_dir() / set_id


def _check_slot(slot: str) -> dict:
    try:
        return SLOTS[slot]
    except KeyError:
        raise KeyError(f"Unknown icon slot: {slot}") from None


def is_builtin(set_id: str) -> bool:
    return set_id in BUILTIN_SETS


def _read_meta(set_id: str) -> dict | None:
    p = _set_dir(set_id) / "set.json"
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    base = meta.get("base")
    meta["base"] = base if base in BUILTIN_SETS else DEFAULT_SET
    meta["name"] = str(meta.get("name") or set_id)
    replaced = meta.get("replaced")
    meta["replaced"] = [s for s in replaced if s in SLOTS] if isinstance(replaced, list) else []
    return meta


def _write_meta(set_id: str, meta: dict) -> None:
    d = _set_dir(set_id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "set.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    tmp.replace(d / "set.json")


def _custom_sets() -> dict[str, dict]:
    """Every custom set on disk, oldest first."""
    out: dict[str, dict] = {}
    root = sets_dir()
    if not root.is_dir():
        return out
    for d in root.iterdir():
        if not d.is_dir() or d.name in BUILTIN_SETS:
            continue
        meta = _read_meta(d.name)
        if meta is not None:
            out[d.name] = meta
    return dict(sorted(out.items(), key=lambda kv: (kv[1].get("created") or "", kv[0])))


def _check_set(set_id: str) -> None:
    if set_id in BUILTIN_SETS:
        return
    if _read_meta(set_id) is None:
        raise KeyError(f"Unknown icon set: {set_id}")


def active_set() -> str:
    """The id of the set in use. Falls back to the default when the setting
    names a set that no longer exists."""
    _migrate_legacy()
    set_id = settings.get("icon_set")
    set_id = str(set_id) if set_id else DEFAULT_SET
    if set_id in BUILTIN_SETS or _read_meta(set_id) is not None:
        return set_id
    return DEFAULT_SET


def activate(set_id: str) -> None:
    _check_set(set_id)
    settings.put("icon_set", set_id)
    invalidate()


def sets() -> list[dict]:
    """Every set with its display metadata, built-in ones first."""
    active = active_set()
    out = []
    for set_id, spec in BUILTIN_SETS.items():
        out.append({
            "id": set_id, "name": spec["name"], "desc": spec["desc"],
            "builtin": True, "base": None, "active": set_id == active,
            "version": version("app_idle", set_id),
        })
    for set_id, meta in _custom_sets().items():
        base = BUILTIN_SETS[meta["base"]]["name"]
        n = len(meta["replaced"])
        out.append({
            "id": set_id, "name": meta["name"],
            "desc": f"Based on {base}. " + (f"{n} icon{'s' if n != 1 else ''} replaced." if n else "No icons replaced yet."),
            "builtin": False, "base": meta["base"], "active": set_id == active,
            "version": version("app_idle", set_id),
        })
    return out


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24]
    return s or "set"


def _clean_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("Give the set a name.")
    if len(name) > 40:
        raise ValueError("Keep the name under 40 characters.")
    return name


def create_set(name: str, base: str | None = None) -> str:
    """A new custom set copied from *base* (a built-in set, or a custom set,
    whose current images are copied). Returns its id. Does not activate it."""
    name = _clean_name(name)
    base = base or DEFAULT_SET
    _check_set(base)
    # Reset points back at a built-in set, so a copy of a copy remembers the
    # built-in ancestor rather than a set that may be deleted later.
    root_base = base if base in BUILTIN_SETS else _read_meta(base)["base"]
    set_id = f"{_slug(name)}-{secrets.token_hex(2)}"
    while _set_dir(set_id).exists() or set_id in BUILTIN_SETS:
        set_id = f"{_slug(name)}-{secrets.token_hex(2)}"
    d = _set_dir(set_id)
    d.mkdir(parents=True, exist_ok=True)
    for slot in SLOTS:
        _save_png(resolve_image(slot, base), d / f"{slot}.png")
    _write_meta(set_id, {
        "name": name, "base": root_base, "replaced": [],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    invalidate()
    return set_id


def rename_set(set_id: str, name: str) -> None:
    if set_id in BUILTIN_SETS:
        raise ValueError("Built-in sets can't be renamed.")
    meta = _read_meta(set_id)
    if meta is None:
        raise KeyError(f"Unknown icon set: {set_id}")
    meta["name"] = _clean_name(name)
    _write_meta(set_id, meta)


def delete_set(set_id: str) -> None:
    """Remove a custom set. The default set takes over if it was in use."""
    if set_id in BUILTIN_SETS:
        raise ValueError("Built-in sets can't be deleted.")
    if _read_meta(set_id) is None:
        raise KeyError(f"Unknown icon set: {set_id}")
    was_active = active_set() == set_id
    shutil.rmtree(_set_dir(set_id), ignore_errors=True)
    if was_active:
        settings.put("icon_set", DEFAULT_SET)
    invalidate()


# ── Images ───────────────────────────────────────────────────────────────────

def _tint(img, color: tuple[int, int, int]):
    """Recolour *img* with *color*, keeping its alpha channel."""
    from PIL import ImageOps
    alpha = img.split()[3]
    gray = ImageOps.grayscale(img)
    out = ImageOps.colorize(gray, black=(0, 0, 0), white=color).convert("RGBA")
    out.putalpha(alpha)
    return out


def _role_file(set_id: str, role: str) -> Path:
    files = BUILTIN_SETS[set_id]["files"]
    name = files.get(role)
    if name is None:
        name = files["app_recording" if role == "tray_recording" else "app"]
    return _IMAGES_DIR / name


def _builtin_image(slot: str, set_id: str):
    from PIL import Image
    spec = _check_slot(slot)
    img = Image.open(_role_file(set_id, spec["role"])).convert("RGBA")
    if spec["tint"]:
        img = _tint(img, spec["tint"])
    return img


def resolve_image(slot: str, set_id: str | None = None):
    """The RGBA image for *slot* in *set_id* (the active set by default).

    A custom set's slot is its PNG on disk; a missing or unreadable file falls
    back to the set's base so the tray never breaks over one bad file."""
    from PIL import Image
    _check_slot(slot)
    set_id = set_id or active_set()
    if set_id in BUILTIN_SETS:
        return _builtin_image(slot, set_id)
    meta = _read_meta(set_id)
    if meta is None:
        raise KeyError(f"Unknown icon set: {set_id}")
    p = _set_dir(set_id) / f"{slot}.png"
    if p.is_file():
        try:
            return Image.open(p).convert("RGBA")
        except Exception:
            pass
    return _builtin_image(slot, meta["base"])


def is_replaced(slot: str, set_id: str | None = None) -> bool:
    """True when the user uploaded their own image for *slot* in a custom set."""
    _check_slot(slot)
    set_id = set_id or active_set()
    if set_id in BUILTIN_SETS:
        return False
    meta = _read_meta(set_id)
    return bool(meta and slot in meta["replaced"])


def version(slot: str, set_id: str | None = None) -> str:
    """A token that changes whenever the resolved image changes. Used for
    ETags and cache-busting query strings."""
    _check_slot(slot)
    set_id = set_id or active_set()
    if set_id in BUILTIN_SETS:
        return set_id
    try:
        return f"{set_id}-{int((_set_dir(set_id) / f'{slot}.png').stat().st_mtime)}"
    except OSError:
        return f"{set_id}-0"


def page_version() -> str:
    """One token covering the app slots, for the icon links in the page head
    and the manifest. Chrome keys its favicon cache on the URL and re-downloads
    days later, so the URL must change whenever either image does."""
    return f"{version('app_idle')}.{version('app_recording')}"


def _fit_square(img, size: int):
    """Scale *img* to fit inside size x size and centre it on a transparent square."""
    from PIL import Image
    img = img.copy()
    img.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
    return canvas


def _save_png(img, path: Path) -> None:
    from PIL import Image
    img = img.copy()
    if max(img.size) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".png.tmp")
    img.save(tmp, format="PNG", optimize=True)
    tmp.replace(path)


def png_bytes(slot: str, size: int | None = None, set_id: str | None = None) -> tuple[bytes, str]:
    """PNG bytes for *slot*, optionally squared to *size* px, plus a version token."""
    from PIL import Image
    _check_slot(slot)
    set_id = set_id or active_set()
    ver = version(slot, set_id)
    key = (set_id, slot, size)
    with _lock:
        hit = _png_cache.get(key)
        if hit and hit[0] == ver:
            return hit[1], ver
    img = resolve_image(slot, set_id)
    if size:
        img = _fit_square(img, size)
    elif max(img.size) > DEFAULT_SERVE_EDGE:
        # The bundled logos are 2048 px. A browser tab or a 28 px sidebar
        # brand does not need that, so the unsized route serves a modest copy.
        img = img.copy()
        img.thumbnail((DEFAULT_SERVE_EDGE, DEFAULT_SERVE_EDGE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    with _lock:
        _png_cache[key] = (ver, data)
    return data, ver


def ico_bytes(slot: str, set_id: str | None = None) -> tuple[bytes, str]:
    """A multi-frame ICO for *slot* (see ICO_FRAMES), plus a version token.

    Each frame is one LANCZOS resample from the full-size image. The frames at
    48 px and under get a light unsharp mask: at those sizes a straight
    downscale of a 2048 px logo goes soft, and the small frames are the ones a
    browser tab and a taskbar button actually show.
    """
    from PIL import ImageFilter
    _check_slot(slot)
    set_id = set_id or active_set()
    ver = version(slot, set_id)
    key = (set_id, slot, "ico")
    with _lock:
        hit = _png_cache.get(key)
        if hit and hit[0] == ver:
            return hit[1], ver
    src = resolve_image(slot, set_id)
    frames = []
    for edge in ICO_FRAMES:
        if edge > max(src.size) and edge != ICO_FRAMES[0]:
            continue   # never upscale a small image
        f = _fit_square(src, edge)
        if edge <= 48:
            f = f.filter(ImageFilter.UnsharpMask(radius=0.8, percent=70, threshold=0))
        frames.append(f)
    buf = io.BytesIO()
    # Pillow drops any listed size larger than the base image and only scales
    # the base for sizes nobody supplied, so the largest frame is the base and
    # the smaller ones ride along as rendered above.
    frames.sort(key=lambda f: f.size[0])
    frames[-1].save(buf, format="ICO", sizes=[f.size for f in frames],
                    append_images=frames[:-1])
    data = buf.getvalue()
    with _lock:
        _png_cache[key] = (ver, data)
    return data, ver


def invalidate() -> None:
    with _lock:
        _png_cache.clear()


# ── Editing a custom set ─────────────────────────────────────────────────────

def _editable(set_id: str | None) -> tuple[str, dict]:
    set_id = set_id or active_set()
    if set_id in BUILTIN_SETS:
        raise ValueError("Built-in sets can't be changed. Copy the set first, then replace its icons.")
    meta = _read_meta(set_id)
    if meta is None:
        raise KeyError(f"Unknown icon set: {set_id}")
    return set_id, meta


def save_custom(slot: str, data: bytes, set_id: str | None = None) -> None:
    """Validate *data* as an image, normalise it to PNG, store it for *slot*
    in a custom set (the active one by default).

    Accepts anything PIL can decode (PNG, ICO, JPEG, WebP, GIF, BMP). ICO files
    contribute their largest frame. Raises ValueError for bad input or a
    built-in set.
    """
    from PIL import Image, UnidentifiedImageError
    _check_slot(slot)
    set_id, meta = _editable(set_id)
    if not data:
        raise ValueError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("That image is over 4 MB. Use something smaller.")
    try:
        img = Image.open(io.BytesIO(data))
        if getattr(img, "format", "") == "ICO" and hasattr(img, "ico"):
            # Pick the largest embedded frame rather than the first.
            sizes = sorted(img.ico.sizes())
            if sizes:
                img.size = sizes[-1]
        img.load()
        img = img.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise ValueError("That file is not an image the app can read.") from e
    if img.width < 16 or img.height < 16:
        raise ValueError("That image is too small. Use at least 16 x 16 pixels.")
    _save_png(img, _set_dir(set_id) / f"{slot}.png")
    if slot not in meta["replaced"]:
        meta["replaced"].append(slot)
        _write_meta(set_id, meta)
    invalidate()


def reset(slot: str, set_id: str | None = None) -> None:
    """Put the set's base image back for *slot*."""
    _check_slot(slot)
    set_id, meta = _editable(set_id)
    _save_png(_builtin_image(slot, meta["base"]), _set_dir(set_id) / f"{slot}.png")
    if slot in meta["replaced"]:
        meta["replaced"].remove(slot)
        _write_meta(set_id, meta)
    invalidate()


# ── Listings ─────────────────────────────────────────────────────────────────

def listing(set_id: str | None = None) -> list[dict]:
    """Every slot with its display metadata and state, in UI order."""
    set_id = set_id or active_set()
    _check_set(set_id)
    return [{
        "slot":     slot,
        "group":    spec["group"],
        "label":    spec["label"],
        "desc":     spec["desc"],
        "replaced": is_replaced(slot, set_id),
        "version":  version(slot, set_id),
    } for slot, spec in SLOTS.items()]


def state() -> dict:
    """What Settings > Icons shows: every set, the active one, and its slots."""
    active = active_set()
    return {
        "active":   active,
        "editable": not is_builtin(active),
        "sets":     sets(),
        "slots":    listing(active),
    }


# ── Legacy per-slot overrides ────────────────────────────────────────────────

def _migrate_legacy() -> None:
    """Turn the pre-sets ``custom_icons`` mapping (one file per replaced slot
    under <data>/icons/) into a custom set called "My icons" and activate it.
    Runs once per process; a no-op when there is nothing to migrate."""
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        mapping = settings.get("custom_icons") or {}
        if not isinstance(mapping, dict) or not mapping:
            return
        files = {slot: icons_dir() / Path(name).name
                 for slot, name in mapping.items() if slot in SLOTS and name}
        files = {slot: p for slot, p in files.items() if p.is_file()}
        settings.put("custom_icons", {})
        if not files:
            return
        set_id = create_set("My icons", DEFAULT_SET)
        meta = _read_meta(set_id)
        for slot, p in files.items():
            shutil.copyfile(p, _set_dir(set_id) / f"{slot}.png")
            meta["replaced"].append(slot)
            try:
                p.unlink()
            except OSError:
                pass
        _write_meta(set_id, meta)
        settings.put("icon_set", set_id)
        invalidate()
    except Exception:
        pass


# ── The Start Menu shortcut icon (Windows) ───────────────────────────────────

def shortcut_icon_path(generate: bool = True) -> Path:
    """The .ico the launcher shortcut should carry for the active set.

    The default set keeps the bundled logo.ico. Any other set gets an ICO of
    its app icon under <data>/icons/shortcut/, named after the set and its
    version so a changed image is a new file (Explorer caches shortcut icons
    by path). Older files are removed once the new one is written.
    """
    set_id = active_set()
    if set_id == DEFAULT_SET:
        return _BUNDLED_ICO
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", version("app_idle", set_id))
    folder = icons_dir() / "shortcut"
    path = folder / f"{token}.ico"
    if generate and not path.is_file():
        try:
            data, _ = ico_bytes("app_idle", set_id)
            folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".ico.tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            for old in folder.glob("*.ico"):
                if old != path:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        except Exception:
            return _BUNDLED_ICO
    return path if path.is_file() else _BUNDLED_ICO


def sync_shortcut_icon() -> list[Path]:
    """Point every shortcut that launches this install (Start Menu, taskbar
    pins) at the active set's icon. Returns the shortcuts that were rewritten.
    Windows only; costs a PowerShell call per shortcut, so callers run it off
    the request thread."""
    import sys
    if sys.platform != "win32":
        return []
    from core import shortcut
    bat = _IMAGES_DIR.parent.parent.parent / "launch.bat"
    if not bat.exists():
        return []
    icon = shortcut_icon_path()
    updated: list[Path] = []
    for lnk in shortcut.our_shortcuts(bat):
        info = shortcut.read(lnk)
        if info and shortcut.same_path(info.get("icon_file", ""), icon) and Path(info["icon_file"]).exists():
            continue
        if shortcut.set_icon(lnk, icon):
            updated.append(lnk)
    return updated
