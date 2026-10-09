"""Moving one transcript line to another speaker trains and links that speaker.

Regression 2026-10-07: the training pass behind PATCH /api/segments/<id>/label
linked the line's ORIGINAL speaker key to the new person's voice profile, so
moving one of "Lisa"'s lines to "Jennifer" pointed every remaining Lisa line at
Jennifer's profile. It now links the key the line moved to, keeps an existing
link on that key, and links nothing for a one-off name.

app.py is never imported (that loads the models): _relabel_segment is lifted out
of the source and run against stand-ins.

Run: .venv/Scripts/python -m pytest tests/test_segment_move_training.py
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")


class _FP:
    ready = True
    MIN_DURATION_SEC = 2.5

    def __init__(self, links=None, profiles=None):
        self.links = dict(links or {})
        self.profiles = dict(profiles or {})      # name -> id
        self.embeddings = []
        self.created = []

    def get_link(self, sid, key):
        return self.links.get((sid, key))

    def link_session_speaker(self, sid, key, gid):
        self.links[(sid, key)] = gid

    def find_by_name(self, name):
        return {"id": self.profiles[name]} if name in self.profiles else None

    def create_global_speaker(self, name):
        gid = f"new-{name}"
        self.profiles[name] = gid
        self.created.append(name)
        return gid

    def find_or_create(self, name, color=None, *, avoid=()):
        found = self.find_by_name(name)
        if found and found["id"] not in avoid:
            return found["id"], False
        return self.create_global_speaker(name), True

    def extract_embedding_from_wav(self, path, start, end):
        return [0.1, 0.2]

    def add_embedding(self, gid, sid, key, emb, dur):
        self.embeddings.append((gid, key))


class _Storage:
    def __init__(self, seg):
        self.seg = dict(seg)

    def save_segment_label_override(self, seg_id, label):
        self.seg["label_override"] = label

    def save_segment_source_override(self, seg_id, key):
        self.seg["source_override"] = key

    def get_segment(self, seg_id):
        return dict(self.seg)


class _Now:
    def submit(self, fn, *a):
        fn(*a)


def _relabel(fp, seg, label, target):
    tree = ast.parse(APP_PY)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_relabel_segment")
    pushed = []
    scope = {
        "storage": _Storage(seg), "fingerprint_db": fp, "_fp_executor": _Now(),
        "_NOISE_LABEL": "[Noise]", "_maybe_update_live_redirect": lambda *a: None,
        "media": type("M", (), {"pcm_wav_path": staticmethod(lambda sid: "x.wav")}),
        "_push": lambda kind, data: pushed.append((kind, data)),
        "obsidian": type("O", (), {"queue_export": staticmethod(lambda sid: None)}),
        "log": type("L", (), {"info": staticmethod(lambda *a: None)}),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<app._relabel_segment>", "exec"),
         scope)
    scope["_relabel_segment"](seg["id"], label, target)
    return pushed


SEG = {"id": 7, "session_id": "s1", "source": "Speaker 1", "start_time": 10.0,
       "end_time": 16.0}


def test_moving_a_line_links_the_speaker_it_moved_to_not_the_one_it_left():
    fp = _FP(links={("s1", "Speaker 1"): "lisa"}, profiles={"Lisa": "lisa", "Jenny": "jenny"})
    pushed = _relabel(fp, SEG, "Jenny", "Speaker 7")
    assert fp.links[("s1", "Speaker 1")] == "lisa", "the old speaker must keep its profile"
    assert fp.links[("s1", "Speaker 7")] == "jenny"
    assert fp.embeddings == [("jenny", "Speaker 7")]
    assert pushed == [("speaker_linked", {"session_id": "s1", "speaker_key": "Speaker 7",
                                          "global_id": "jenny", "name": "Jenny"})]


def test_a_target_that_already_has_a_profile_trains_that_profile():
    # Two profiles share the name; the key's own link decides, not the name.
    fp = _FP(links={("s1", "Speaker 3"): "bettie-2"}, profiles={"Bettie": "bettie-1"})
    _relabel(fp, SEG, "Bettie", "Speaker 3")
    assert fp.embeddings == [("bettie-2", "Speaker 3")]
    assert fp.links[("s1", "Speaker 3")] == "bettie-2"


def test_a_one_off_name_trains_the_profile_but_links_no_speaker():
    fp = _FP(links={("s1", "Speaker 1"): "lisa"})
    pushed = _relabel(fp, SEG, "Visitor", None)
    assert fp.created == ["Visitor"]
    assert fp.links == {("s1", "Speaker 1"): "lisa"}
    assert pushed == []


def test_noise_and_short_lines_never_train():
    fp = _FP()
    _relabel(fp, SEG, "[Noise]", "[Noise]")
    _relabel(fp, {**SEG, "end_time": 11.0}, "Jenny", "Speaker 7")
    assert fp.embeddings == [] and fp.links == {}
