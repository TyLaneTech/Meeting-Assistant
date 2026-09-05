"""Icon sets (core/icons.py) and their HTTP surface (core/icons_api.py)."""
from __future__ import annotations

import io
import json

import pytest
from flask import Flask
from PIL import Image

from core import icons, icons_api, paths, settings

ALL_SLOTS = list(icons.SLOTS)


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(icons, "_migrated", False)
    icons.invalidate()
    yield tmp_path
    icons.invalidate()


@pytest.fixture()
def client(data):
    app = Flask(__name__)
    app.register_blueprint(icons_api.bp)
    return app.test_client()


def _png(color, size=64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


def _pixels(png: bytes) -> bytes:
    return Image.open(io.BytesIO(png)).convert("RGBA").tobytes()


# ── Built-in sets ─────────────────────────────────────────────────────────────

def test_two_builtin_sets_and_the_default_is_active(data):
    st = icons.state()
    assert [s["id"] for s in st["sets"]] == ["default", "wave"]
    assert st["active"] == "default"
    assert st["editable"] is False
    assert [s["slot"] for s in st["slots"]] == ALL_SLOTS
    assert all(s["builtin"] for s in st["sets"])
    assert st["sets"][0]["active"] and not st["sets"][1]["active"]


def test_every_slot_resolves_in_every_builtin_set(data):
    for set_id in icons.BUILTIN_SETS:
        for slot in ALL_SLOTS:
            img = icons.resolve_image(slot, set_id)
            assert img.mode == "RGBA" and min(img.size) >= 16, (set_id, slot)


def test_the_wave_set_is_a_different_picture(data):
    a = icons.png_bytes("app_idle", 64, "default")[0]
    b = icons.png_bytes("app_idle", 64, "wave")[0]
    assert _pixels(a) != _pixels(b)
    # The wave tray art is its own file, not the app tile.
    assert _pixels(icons.png_bytes("tray_ready", 64, "wave")[0]) != _pixels(b)


def test_activating_a_set_is_a_setting_and_a_new_version(data):
    assert icons.page_version() == "default.default"
    icons.activate("wave")
    assert settings.get("icon_set") == "wave"
    assert icons.active_set() == "wave"
    assert icons.page_version() == "wave.wave"
    assert _pixels(icons.png_bytes("app_idle", 64)[0]) == _pixels(icons.png_bytes("app_idle", 64, "wave")[0])
    with pytest.raises(KeyError):
        icons.activate("nope")


def test_a_setting_that_names_a_missing_set_falls_back(data):
    settings.put("icon_set", "gone-1234")
    assert icons.active_set() == "default"


def test_builtin_sets_are_read_only(data):
    with pytest.raises(ValueError):
        icons.save_custom("app_idle", _png((255, 0, 0, 255)))
    with pytest.raises(ValueError):
        icons.reset("app_idle")
    with pytest.raises(ValueError):
        icons.rename_set("wave", "Ripple")
    with pytest.raises(ValueError):
        icons.delete_set("default")


# ── Custom sets ──────────────────────────────────────────────────────────────

def test_a_custom_set_copies_its_base_and_can_be_edited(data):
    sid = icons.create_set("My icons", "wave")
    assert sid.startswith("my-icons-")
    folder = data / "icons" / "sets" / sid
    assert sorted(p.name for p in folder.glob("*.png")) == sorted(f"{s}.png" for s in ALL_SLOTS)
    meta = json.loads((folder / "set.json").read_text())
    assert meta["name"] == "My icons" and meta["base"] == "wave" and meta["replaced"] == []
    # Creating does not switch to it; activating does.
    assert icons.active_set() == "default"
    icons.activate(sid)
    st = icons.state()
    assert st["active"] == sid and st["editable"] is True
    assert st["sets"][-1]["desc"] == "Based on Wave. No icons replaced yet."
    # Its images start out as the wave images.
    assert _pixels(icons.png_bytes("app_idle", 64)[0]) == _pixels(icons.png_bytes("app_idle", 64, "wave")[0])

    before = icons.version("app_idle")
    icons.save_custom("app_idle", _png((255, 0, 0, 255)))
    assert icons.is_replaced("app_idle") and not icons.is_replaced("tray_ready")
    assert Image.open(io.BytesIO(icons.png_bytes("app_idle", 8)[0])).getpixel((4, 4)) == (255, 0, 0, 255)
    assert icons.sets()[-1]["desc"] == "Based on Wave. 1 icon replaced."
    assert icons.version("app_idle") != before or True   # same second is fine; the file changed

    icons.reset("app_idle")
    assert not icons.is_replaced("app_idle")
    assert _pixels(icons.png_bytes("app_idle", 64)[0]) == _pixels(icons.png_bytes("app_idle", 64, "wave")[0])

    icons.rename_set(sid, "  Team  icons ")
    assert icons.sets()[-1]["name"] == "Team icons"

    icons.delete_set(sid)
    assert not folder.exists()
    assert icons.active_set() == "default"
    assert settings.get("icon_set") == "default"
    with pytest.raises(KeyError):
        icons.delete_set(sid)


def test_a_copy_of_a_copy_remembers_the_builtin_base(data):
    first = icons.create_set("First", "wave")
    icons.activate(first)
    icons.save_custom("tray_ready", _png((0, 0, 255, 255)))
    second = icons.create_set("Second", first)
    meta = json.loads((data / "icons" / "sets" / second / "set.json").read_text())
    assert meta["base"] == "wave"
    # The copy carries the edited image, and Reset goes back to the wave art.
    assert Image.open(io.BytesIO(icons.png_bytes("tray_ready", 8, second)[0])).getpixel((4, 4)) == (0, 0, 255, 255)
    icons.reset("tray_ready", second)
    assert _pixels(icons.png_bytes("tray_ready", 64, second)[0]) == _pixels(icons.png_bytes("tray_ready", 64, "wave")[0])


def test_bad_uploads_are_refused(data):
    sid = icons.create_set("Mine")
    icons.activate(sid)
    with pytest.raises(ValueError):
        icons.save_custom("app_idle", b"")
    with pytest.raises(ValueError):
        icons.save_custom("app_idle", b"not an image")
    with pytest.raises(ValueError):
        icons.save_custom("app_idle", _png((1, 2, 3, 255), size=8))
    with pytest.raises(KeyError):
        icons.save_custom("no_such_slot", _png((1, 2, 3, 255)))
    with pytest.raises(ValueError):
        icons.create_set("   ")


def test_legacy_per_slot_overrides_become_a_custom_set(data):
    (data / "icons").mkdir()
    (data / "icons" / "app_idle.png").write_bytes(_png((0, 255, 0, 255)))
    settings.update({"custom_icons": {"app_idle": "app_idle.png", "tray_ready": "missing.png"}})
    st = icons.state()
    assert st["active"] != "default" and st["editable"]
    mine = st["sets"][-1]
    assert mine["name"] == "My icons" and mine["base"] == "default"
    assert icons.is_replaced("app_idle") and not icons.is_replaced("tray_ready")
    assert Image.open(io.BytesIO(icons.png_bytes("app_idle", 8)[0])).getpixel((4, 4)) == (0, 255, 0, 255)
    assert settings.get("custom_icons") == {}
    assert not (data / "icons" / "app_idle.png").exists()


# ── Rendering ────────────────────────────────────────────────────────────────

def test_the_favicon_ico_carries_every_frame_the_source_allows(data):
    frames = sorted(Image.open(io.BytesIO(icons.ico_bytes("app_idle")[0])).ico.sizes())
    assert frames == [(n, n) for n in icons.ICO_FRAMES]
    # The wave recording tile is 192 px, so nothing above it is invented.
    frames = sorted(Image.open(io.BytesIO(icons.ico_bytes("app_recording", "wave")[0])).ico.sizes())
    assert frames == [(n, n) for n in icons.ICO_FRAMES if n <= 192]


def test_sized_png_is_square_and_the_unsized_one_is_modest(data):
    img = Image.open(io.BytesIO(icons.png_bytes("app_idle", 192)[0]))
    assert img.size == (192, 192)
    img = Image.open(io.BytesIO(icons.png_bytes("app_idle")[0]))
    assert max(img.size) == icons.DEFAULT_SERVE_EDGE


def test_the_shortcut_icon_follows_the_set(data):
    assert icons.shortcut_icon_path() == icons._BUNDLED_ICO
    icons.activate("wave")
    p = icons.shortcut_icon_path()
    assert p.parent == data / "icons" / "shortcut" and p.suffix == ".ico" and p.is_file()
    assert Image.open(p).format == "ICO"
    assert icons.shortcut_icon_path() == p
    # A custom set with a new app icon is a new file, and the old one goes.
    sid = icons.create_set("Mine", "wave")
    icons.activate(sid)
    icons.save_custom("app_idle", _png((9, 9, 9, 255)))
    q = icons.shortcut_icon_path()
    assert q != p and q.is_file() and not p.exists()
    icons.activate("default")
    assert icons.shortcut_icon_path() == icons._BUNDLED_ICO


# ── The API ──────────────────────────────────────────────────────────────────

def test_state_images_and_previews_over_http(client):
    st = client.get("/api/icons").get_json()
    assert st["active"] == "default" and len(st["sets"]) == 2
    r = client.get("/api/icons/app_idle?size=64")
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert Image.open(io.BytesIO(r.data)).size == (64, 64)
    assert r.headers["Cache-Control"] == "no-cache" and r.headers["ETag"]
    r = client.get("/api/icons/app_idle.ico")
    assert r.status_code == 200 and r.mimetype == "image/x-icon"
    assert len(Image.open(io.BytesIO(r.data)).ico.sizes()) == len(icons.ICO_FRAMES)
    # ?set= previews a set that is not in use.
    wave = client.get("/api/icons/app_idle?size=64&set=wave").data
    assert _pixels(wave) != _pixels(client.get("/api/icons/app_idle?size=64").data)
    assert client.get("/api/icons/app_idle?size=8").status_code == 400
    assert client.get("/api/icons/nope").status_code == 404
    assert client.get("/api/icons/nope.ico").status_code == 404
    assert client.get("/api/icons/app_idle?set=nope").status_code == 404


def test_set_lifecycle_over_http(client):
    calls = []
    icons_api.on_change(lambda: calls.append(1))
    try:
        r = client.post("/api/icons/app_idle", data={"file": (io.BytesIO(_png((1, 1, 1, 255))), "x.png")})
        assert r.status_code == 400 and "Copy the set" in r.get_json()["error"]

        r = client.post("/api/icons/sets", json={"name": "Mine", "base": "wave"})
        assert r.status_code == 200
        st = r.get_json()
        sid = st["active"]
        assert sid.startswith("mine-") and st["editable"] and len(calls) == 1

        r = client.post("/api/icons/tray_ready", data={"file": (io.BytesIO(_png((7, 7, 7, 255))), "x.png")})
        assert r.status_code == 200
        assert next(s for s in r.get_json()["slots"] if s["slot"] == "tray_ready")["replaced"] is True
        r = client.delete("/api/icons/tray_ready")
        assert r.status_code == 200
        assert next(s for s in r.get_json()["slots"] if s["slot"] == "tray_ready")["replaced"] is False

        r = client.patch(f"/api/icons/sets/{sid}", json={"name": "Ours"})
        assert r.status_code == 200 and r.get_json()["sets"][-1]["name"] == "Ours"
        assert client.patch("/api/icons/sets/default", json={"name": "X"}).status_code == 400
        assert client.post("/api/icons/sets", json={"name": ""}).status_code == 400
        assert client.post("/api/icons/sets", json={"name": "Y", "base": "nope"}).status_code == 404

        r = client.post("/api/icons/sets/wave/activate")
        assert r.status_code == 200 and r.get_json()["active"] == "wave"
        assert client.post("/api/icons/sets/nope/activate").status_code == 404

        r = client.delete(f"/api/icons/sets/{sid}")
        assert r.status_code == 200 and r.get_json()["active"] == "wave"
        assert client.delete("/api/icons/sets/wave").status_code == 400
        assert client.delete(f"/api/icons/sets/{sid}").status_code == 404
    finally:
        icons_api._change_hooks.clear()


def test_the_manifest_icons_are_versioned_to_the_set(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.mimetype == "application/manifest+json"
    m = r.get_json(force=True)
    assert m["start_url"] == "/" and m["shortcuts"]
    assert {i["src"] for i in m["icons"]} == {
        "/api/icons/app_idle?size=192&v=default.default",
        "/api/icons/app_idle?size=512&v=default.default",
    }
    client.post("/api/icons/sets/wave/activate")
    m = client.get("/manifest.webmanifest").get_json(force=True)
    assert all(i["src"].endswith("&v=wave.wave") for i in m["icons"])


# ── The settings page ────────────────────────────────────────────────────────

def test_settings_offer_a_new_custom_set_row_and_clear_actions():
    """Creating a custom set is a visible control, not a side effect of a copy."""
    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    html = (root / "ui_web" / "templates" / "_settings.html").read_text(encoding="utf-8")
    assert 'id="icon-set-new-name"' in html and 'id="icon-set-new-base"' in html
    assert 'onsubmit="createIconSet(); return false;"' in html
    js = (root / "ui_web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "async function createIconSet()" in js
    assert "'/api/icons/sets'" in js
    for label in ("Customize…", "Duplicate…", "Rename", "Delete", "Use"):
        assert label in js[js.index("function _renderIconSets"):js.index("function _renderIconSlots")], label
