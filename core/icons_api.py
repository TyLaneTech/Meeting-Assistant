"""HTTP surface for icon sets (core/icons.py): the images every consumer
loads, the manifest that carries them, and the Settings > Icons actions.

Mounted on the Flask app as a blueprint. app.py registers a change hook so a
new set or image also repaints the tray and re-points the launcher shortcut.
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

from core import icons as icons

bp = Blueprint("icons", __name__)

_STATIC_MANIFEST = Path(__file__).parent.parent / "ui_web" / "static" / "manifest.webmanifest"
_change_hooks: list = []


def on_change(fn) -> None:
    """Call *fn* after any change to the active set or its images."""
    _change_hooks.append(fn)


def _changed() -> None:
    for fn in list(_change_hooks):
        try:
            fn()
        except Exception:
            pass


@bp.app_context_processor
def _inject_icon_version():
    """``icon_version`` for the icon links in the page head (see
    core.icons.page_version)."""
    try:
        return {"icon_version": icons.page_version()}
    except Exception:
        return {"icon_version": "default"}


def _set_arg() -> str | None:
    """``?set=`` names a set to preview; the active set otherwise."""
    value = (request.args.get("set") or "").strip()
    return value or None


@bp.route("/api/icons", methods=["GET"])
def api_icons_state():
    """Every set, the active one, and the active set's slots."""
    return jsonify(icons.state())


@bp.route("/api/icons/<slot>", methods=["GET"])
def api_icon_get(slot: str):
    """The resolved PNG for a slot. ``?size=N`` squares it to N px (the
    installed-app manifest asks for 192 and 512); ``?set=`` previews another
    set. ``<slot>.ico`` is the favicon: a multi-frame ICO with the 16 to 256 px
    frames rendered here, so the tab and the window icon Chrome derives from
    it start crisp."""
    set_id = _set_arg()
    try:
        if slot.endswith(".ico"):
            data, version = icons.ico_bytes(slot[:-4], set_id)
            mimetype = "image/x-icon"
        else:
            size = request.args.get("size", type=int)
            if size is not None and not (16 <= size <= 1024):
                return jsonify({"error": "size must be between 16 and 1024"}), 400
            data, version = icons.png_bytes(slot, size, set_id)
            mimetype = "image/png"
    except KeyError as e:
        return jsonify({"error": str(e).strip("'")}), 404
    resp = Response(data, mimetype=mimetype)
    # Revalidate every time: the browser tab and the sidebar logo must pick up
    # a replacement without a hard reload. The ETag makes that cheap.
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["ETag"] = f'"{slot}-{version}-{request.args.get("size") or 0}"'
    return resp


@bp.route("/api/icons/<slot>", methods=["POST"])
def api_icon_upload(slot: str):
    """Replace a slot's image in the active custom set (multipart field ``file``)."""
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        icons.save_custom(slot, f.read(), _set_arg())
    except KeyError as e:
        return jsonify({"error": str(e).strip("'")}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _changed()
    return jsonify(icons.state())


@bp.route("/api/icons/<slot>", methods=["DELETE"])
def api_icon_reset(slot: str):
    """Put the set's base image back for a slot."""
    try:
        icons.reset(slot, _set_arg())
    except KeyError as e:
        return jsonify({"error": str(e).strip("'")}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _changed()
    return jsonify(icons.state())


@bp.route("/api/icons/sets", methods=["POST"])
def api_icon_set_create():
    """A new custom set copied from ``base`` (default: the active set),
    activated straight away so its slots are what Settings shows next."""
    body = request.get_json(silent=True) or {}
    base = (body.get("base") or icons.active_set())
    try:
        set_id = icons.create_set(body.get("name", ""), base)
        icons.activate(set_id)
    except KeyError as e:
        return jsonify({"error": str(e).strip("'")}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _changed()
    return jsonify(icons.state())


@bp.route("/api/icons/sets/<set_id>/activate", methods=["POST"])
def api_icon_set_activate(set_id: str):
    try:
        icons.activate(set_id)
    except KeyError as e:
        return jsonify({"error": str(e).strip("'")}), 404
    _changed()
    return jsonify(icons.state())


@bp.route("/api/icons/sets/<set_id>", methods=["PATCH"])
def api_icon_set_rename(set_id: str):
    body = request.get_json(silent=True) or {}
    try:
        icons.rename_set(set_id, body.get("name", ""))
    except KeyError as e:
        return jsonify({"error": str(e).strip("'")}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(icons.state())


@bp.route("/api/icons/sets/<set_id>", methods=["DELETE"])
def api_icon_set_delete(set_id: str):
    try:
        icons.delete_set(set_id)
    except KeyError as e:
        return jsonify({"error": str(e).strip("'")}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _changed()
    return jsonify(icons.state())


@bp.route("/manifest.webmanifest", methods=["GET"])
def manifest():
    """The installed-app manifest, with the icon URLs versioned to the active
    set. Chrome re-reads a launched app's manifest about once a day and
    updates the app's icon when the manifest's icons change; a URL that
    changes with the set is what makes that comparison notice."""
    try:
        data = json.loads(_STATIC_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return jsonify({"error": "manifest unavailable"}), 500
    ver = icons.page_version()
    for icon in data.get("icons", []):
        try:
            size = int(str(icon.get("sizes", "512x512")).split("x")[0])
        except ValueError:
            size = 512
        icon["src"] = f"/api/icons/app_idle?size={size}&v={ver}"
    resp = Response(json.dumps(data, indent=2), mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp
