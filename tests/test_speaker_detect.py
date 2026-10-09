"""AI speaker detection (ai/speaker_detect): name labels, which moments get
read, the resolver (screen readings checked against the turns' own voices),
the run policy, a whole run against a stub model, the journal's undo, the
vision request, reading the user's words, and the routes.

Synthetic data only: no model calls, no audio, no video. A voice is a unit
vector per person and a turn's voice is that vector plus noise, which is how
the real embeddings behave (one person's turns about 0.8 from their mean,
different people near 0).

Run: .venv/Scripts/python -m pytest tests/test_speaker_detect.py
"""
from __future__ import annotations

import io
import json
import re
from types import SimpleNamespace

import numpy as np
import pytest
from flask import Flask
from PIL import Image

from ai.speaker_detect import instructions, planner, prompts, resolver, routes, runs, vision
from ai.speaker_detect.names import NameBook, normalize
from ai.speaker_detect.observations import Observation, Sighting
from core import paths, speaker_journal, storage
from ml.speaker_db import SpeakerFingerprintDB

# ── helpers ─────────────────────────────────────────────────────────────────


def seg(i, key, start, end):
    return {"id": i, "key": key, "start_time": float(start), "end_time": float(end)}


def look(t, label, *, cue="border", conf=0.9, oid=None, pinned=False):
    return Observation(t=float(t), kind="read", meeting_visible=True, app="teams",
                       layout="gallery", speaking=[Sighting(label, cue, conf, None, False)],
                       pinned=pinned, id=oid)


def unit(v):
    return (v / np.linalg.norm(v)).astype(np.float32)


class Voices:
    """A voice per person; ``turn(person)`` is one turn's noisy vector."""

    def __init__(self, seed=7):
        self.rng = np.random.default_rng(seed)
        self.base: dict[str, np.ndarray] = {}

    def of(self, person):
        if person not in self.base:
            self.base[person] = unit(self.rng.normal(size=256))
        return self.base[person]

    def turn(self, person, noise=0.03):
        return unit(self.of(person) + noise * self.rng.normal(size=256))


def keys_for(tl, names=None, set_by=None):
    talk: dict[str, float] = {}
    for t in tl.turns:
        talk[t.key] = talk.get(t.key, 0.0) + t.length
    names = names or {}
    return {k: resolver.KeyState(k, names.get(k, k), set_by=(set_by or {}).get(k), talk=v)
            for k, v in talk.items()}


def ops_by_type(res, kind):
    return [op for op in res.ops if op.type == kind]


# ── names ───────────────────────────────────────────────────────────────────

def test_name_labels_lose_their_decoration():
    assert normalize("Tom McDonnell (2)") == "Tom McDonnell"
    assert normalize("Priya Nibert (She/Her)") == "Priya Nibert"
    assert normalize("Lane, Ty") == "Ty Lane"
    assert normalize("Collin Murdock - Nationwide") == "Collin Murdock"
    assert normalize("Ty Lane | Higginbotham") == "Ty Lane"
    assert normalize(None) == ""


def test_a_label_matches_a_known_person_conservatively():
    book = NameBook({"Caleb Smith": "g1", "Chris Johnson": "g2", "Chris Johnston": "g3"})
    assert book.resolve("Caleb") == ("Caleb Smith", "g1", True)          # the only Caleb
    assert book.resolve("Chris")[2] is False                              # two Chrises
    book.add("Chris Johnson", expected=True)
    assert book.resolve("Chris")[0] == "Chris Johnson"                    # the invite settles it
    assert book.resolve("Caleb Smit")[0] == "Caleb Smith"                 # cut off by the tile edge
    assert book.resolve("Christopher Johnson")[2] is False                # never on spelling alone
    assert book.same("Lane, Ty", "Ty Lane")


# ── planning ────────────────────────────────────────────────────────────────

def test_turns_merge_close_lines_and_the_owner_is_set_aside():
    tl = planner.build([seg(1, "Speaker 1", 0, 4), seg(2, "Speaker 1", 4.5, 8),
                        seg(3, "me", 8, 9), seg(4, "Speaker 2", 9, 15),
                        seg(5, "[noise]", 15, 16)])
    assert [(t.key, t.start, t.end, t.seg_ids) for t in tl.turns] == [
        ("Speaker 1", 0.0, 8.0, (1, 2)), ("Speaker 2", 9.0, 15.0, (4,))]
    assert tl.owner == [(8.0, 9.0)]


def test_no_moment_is_read_while_someone_else_is_still_talking():
    # Speaker 1 starts before Speaker 2 has finished: its only moment would
    # show Speaker 2's highlight, so it is never chosen.
    tl = planner.build([seg(1, "Speaker 2", 20, 30), seg(2, "Speaker 1", 29, 40)])
    cands = planner.candidates(tl)
    assert "Speaker 1" not in cands
    assert cands["Speaker 2"]


def test_the_first_wave_looks_at_every_speaker_before_any_twice():
    segs = []
    for i, start in enumerate(range(0, 200, 20)):
        segs.append(seg(i, "Speaker 1" if i % 3 else "Speaker 2", start, start + 15))
    segs.append(seg(99, "Speaker 3", 210, 220))
    tl = planner.build(segs)
    cands = planner.candidates(tl)
    talk = {k: sum(t.length for t in tl.turns if t.key == k) for k in cands}
    wave = planner.first_wave(cands, talk)
    assert {m.key for m in wave[:3]} == {"Speaker 1", "Speaker 2", "Speaker 3"}
    # An earlier run's moment inside the same turn counts as read.
    used = {wave[0].t}
    more = planner.more_for("Speaker 1", cands, used, 2, known=lambda t: True)
    assert more == []


def test_a_one_line_speaker_still_gets_a_look():
    # Speaker 3 says one 1.3 s line: too short for an anchor, so it used to
    # be never looked at and left unnamed. It gets a look just after the line
    # ends, once the app's highlight has caught up; a 0.5 s blip, one said
    # over someone else, and a speaker with real turns get none.
    tl = planner.build([seg(1, "Speaker 1", 0, 20), seg(2, "Speaker 3", 22.0, 23.3),
                        seg(3, "Speaker 1", 26, 40), seg(4, "Speaker 4", 41.0, 41.5),
                        seg(5, "Speaker 5", 45, 46.2), seg(6, "Speaker 1", 46.0, 60)])
    assert "Speaker 3" not in planner.candidates(tl)
    looks = planner.fragments(tl, ["Speaker 3", "Speaker 4", "Speaker 5", "Speaker 1"])
    assert [(m.key, m.kind) for m in looks] == [("Speaker 3", "fragment")]
    t = looks[0].t
    assert 23.3 <= t <= 23.3 + planner.FRAGMENT_AFTER and tl.turn_at(t).key == "Speaker 3"
    # A "yeah" right after someone else stops would catch their highlight.
    tl = planner.build([seg(1, "Speaker 1", 0, 20), seg(2, "Speaker 3", 20.3, 21.6)])
    assert planner.fragments(tl, ["Speaker 3"]) == []
    # The owner answering at once rules out the later moments, not the line:
    # the look moves back into it (the dbt Labs meeting's "Sick").
    tl = planner.build([seg(1, "me", 10.0, 11.2), seg(2, "Speaker 3", 11.3, 12.6),
                        seg(3, "me", 12.7, 13.6), seg(4, "Speaker 1", 20, 40)])
    looks = planner.fragments(tl, ["Speaker 3"])
    assert len(looks) == 1 and 12.3 <= looks[0].t <= 12.4


# ── the resolver ────────────────────────────────────────────────────────────

def _two_speakers(turns=3):
    """Speaker 1 is Dana Lee (known), Speaker 2 is Sam Park (not in the
    library); ``turns`` turns each, read once each."""
    segs, obs = [], []
    for i in range(2 * turns):
        key = "Speaker 1" if i % 2 == 0 else "Speaker 2"
        segs.append(seg(i, key, i * 10, i * 10 + 9))
        obs.append(look(i * 10 + 3, "Dana Lee" if key == "Speaker 1" else "Sam Park", oid=i))
    return planner.build(segs), obs


def test_a_speaker_is_named_after_who_the_screen_showed():
    tl, obs = _two_speakers()
    res = resolver.resolve(tl, obs, keys_for(tl), NameBook({"Dana Lee": "g1"}), owner="Ty Lane")
    names = {op.keys[0]: op for op in ops_by_type(res, "name")}
    assert names["Speaker 1"].name == "Dana Lee" and names["Speaker 1"].global_id == "g1"
    assert names["Speaker 2"].name == "Sam Park" and names["Speaker 2"].create_profile
    # A known profile is surer than a name only the screen knows.
    assert names["Speaker 1"].confidence >= runs.HIGH > names["Speaker 2"].confidence
    assert set(names["Speaker 1"].evidence) == {0, 2, 4}


def test_a_name_the_user_typed_is_questioned_not_changed():
    # Four turns: overruling any existing name takes more than naming a blank one.
    tl, obs = _two_speakers(turns=4)
    keys = keys_for(tl, names={"Speaker 1": "Dana Smith"}, set_by={"Speaker 1": "user"})
    res = resolver.resolve(tl, obs, keys, NameBook({"Dana Lee": "g1"}))
    assert not [op for op in ops_by_type(res, "name") if "Speaker 1" in op.keys]
    finding = next(op for op in res.ops if op.kind == "disagrees_with_you")
    assert finding.keys == ["Speaker 1"] and finding.name == "Dana Lee"
    res = resolver.resolve(tl, obs, keys_for(tl, names={"Speaker 1": "Dana Smith"},
                                             set_by={"Speaker 1": "user"}),
                           NameBook({"Dana Lee": "g1"}), recheck_user_labels=True)
    assert [op for op in ops_by_type(res, "name") if "Speaker 1" in op.keys]


def test_the_owner_on_a_desktop_speaker_is_an_echo_never_a_name():
    tl, obs = _two_speakers()
    obs = [look(o.t, "Ty Lane", oid=o.id) if o.speaking[0].label == "Dana Lee" else o
           for o in obs]
    res = resolver.resolve(tl, obs, keys_for(tl), NameBook({"Ty Lane": "me"}), owner="Ty Lane")
    assert not [op for op in ops_by_type(res, "name") if "Speaker 1" in op.keys]
    assert not res.decisions.get("Speaker 1")


def test_constraints_force_forbid_and_protect():
    tl, obs = _two_speakers()
    book = NameBook({"Dana Lee": "g1"})
    res = resolver.resolve(tl, obs, keys_for(tl), book, constraints=[
        {"kind": "is_not", "subject": {"key": "Speaker 1"}, "value": "Dana Lee"},
        {"kind": "is", "subject": {"key": "Speaker 2"}, "value": "Pat Gordon"},
    ])
    assert not (res.decisions.get("Speaker 1") and res.decisions["Speaker 1"].person)
    assert res.decisions["Speaker 2"].person == "Pat Gordon"
    res = resolver.resolve(tl, obs, keys_for(tl), book, constraints=[
        {"kind": "protect", "subject": {"key": "Speaker 1"}}])
    assert not [op for op in res.ops if "Speaker 1" in op.keys and op.type != "finding"]


def _mixed_meeting(v: Voices, *, seen_b_in_key1=True):
    """The diarizer put Bea's last three turns into Speaker 1 with Al's.
    Speaker 2 is Bea's own key. Every turn has a voice."""
    segs, obs, tv = [], [], {}
    plan = [("Speaker 1", "Al", 12), ("Speaker 2", "Bea", 9), ("Speaker 1", "Al", 12),
            ("Speaker 2", "Bea", 9), ("Speaker 1", "Al", 12), ("Speaker 2", "Bea", 9),
            ("Speaker 1", "Bea", 6), ("Speaker 3", "Cy", 9), ("Speaker 1", "Bea", 6),
            ("Speaker 3", "Cy", 9), ("Speaker 1", "Bea", 6)]
    t = 0.0
    for i, (key, person, dur) in enumerate(plan):
        segs.append(seg(i, key, t, t + dur))
        t += dur + 1
    tl = planner.build(segs)
    for tr, (key, person, dur) in zip(tl.turns, plan):
        tv[tr.idx] = v.turn(person)
        shown = person in ("Al", "Bea") and (key != "Speaker 1" or person == "Al"
                                             or (seen_b_in_key1 and tr.idx == 6))
        if shown:
            obs.append(look(tr.start + 2.5, {"Al": "Al Moss", "Bea": "Bea Kim"}[person],
                            oid=tr.idx))
    return tl, obs, tv


def test_lines_in_another_persons_voice_move_to_that_person():
    v = Voices()
    tl, obs, tv = _mixed_meeting(v)
    book = NameBook({"Al Moss": "ga", "Bea Kim": "gb"})
    res = resolver.resolve(tl, obs, keys_for(tl), book, turn_vectors=tv)
    assert res.decisions["Speaker 1"].person == "Al Moss"
    assert res.decisions["Speaker 2"].person == "Bea Kim"
    move = next(op for op in ops_by_type(res, "move") if op.name == "Bea Kim")
    assert move.keys == ["Speaker 1"] and move.to_key == "Speaker 2"
    bea_turns = [t for t in tl.turns if t.key == "Speaker 1" and t.idx >= 6]
    assert sorted(move.segment_ids) == sorted(s for t in bea_turns for s in t.seg_ids)
    assert move.confidence >= runs.HIGH
    # Speaker 1's profile learns only from Al's own turns.
    name = next(op for op in ops_by_type(res, "name") if "Speaker 1" in op.keys)
    al_segs = {s for t in tl.turns if t.key == "Speaker 1" and t.idx < 6 for s in t.seg_ids}
    assert name.train and set(name.train_segments) <= al_segs


def test_a_voice_the_screen_never_showed_is_not_given_to_anyone():
    v = Voices()
    tl, obs, tv = _mixed_meeting(v)
    res = resolver.resolve(tl, obs, keys_for(tl), NameBook({"Al Moss": "ga", "Bea Kim": "gb"}),
                           turn_vectors=tv)
    # Cy was never on screen: Speaker 3 stays unnamed and keeps its lines.
    assert not (res.decisions.get("Speaker 3") and res.decisions["Speaker 3"].person)
    assert not [op for op in res.ops if "Speaker 3" in op.keys and op.type != "finding"]


def test_a_misread_is_set_aside_by_the_turns_voice():
    v = Voices()
    tl, obs, tv = _mixed_meeting(v)
    # One read of an Al turn says Bea: Bea's other turns don't sound like it.
    al_turn = next(t for t in tl.turns if t.key == "Speaker 1" and t.idx == 4)
    obs.append(look(al_turn.start + 6, "Bea Kim", oid=99))
    res = resolver.resolve(tl, obs, keys_for(tl), NameBook({"Al Moss": "ga", "Bea Kim": "gb"}),
                           turn_vectors=tv)
    assert res.misreads >= 1
    assert res.decisions["Speaker 1"].person == "Al Moss"


def test_a_tile_shown_for_several_voices_is_not_believed():
    v = Voices()
    segs, obs, tv = [], [], {}
    people = ["Al", "Bea", "Cy", "Al", "Bea", "Cy"]
    for i, p in enumerate(people):
        segs.append(seg(i, f"Speaker {i % 3 + 1}", i * 10, i * 10 + 9))
    tl = planner.build(segs)
    for tr, p in zip(tl.turns, people):
        tv[tr.idx] = v.turn(p)
        obs.append(look(tr.start + 3, "Presenter Pat", oid=tr.idx, pinned=True))
    res = resolver.resolve(tl, obs, keys_for(tl), NameBook({"Presenter Pat": "gp"}),
                           turn_vectors=tv)
    assert "Presenter Pat" in res.non_specific
    assert not ops_by_type(res, "name")
    # What the screen showed is still reported, for the page to say so.
    assert res.seen["Speaker 1"] == {"Presenter Pat": 2}


def test_without_voices_a_two_person_key_is_only_reported():
    segs, obs = [], []
    for i in range(10):
        # Two seconds apart, so each line is a turn of its own.
        segs.append(seg(i, "Speaker 1", i * 11, i * 11 + 9))
        obs.append(look(i * 11 + 3, "Al Moss" if i < 5 else "Bea Kim", oid=i))
    tl = planner.build(segs)
    res = resolver.resolve(tl, obs, keys_for(tl), NameBook({"Al Moss": "ga", "Bea Kim": "gb"}))
    finding = next(op for op in res.ops if op.kind == "two_people")
    assert finding.keys == ["Speaker 1"]
    assert not ops_by_type(res, "move") and not ops_by_type(res, "split")


def test_an_unnamed_speaker_seen_once_gets_a_guess_never_an_applied_name():
    # Speaker 1 is Dana, settled over three turns; Speaker 3 said one line and
    # one look showed Dana: not enough to decide, but worth offering. The
    # guess is its own change (it never drags Dana's sure name down to a
    # suggestion) and teaches the voice library nothing.
    segs = [seg(i, "Speaker 1", i * 10, i * 10 + 8) for i in range(3)] + [seg(9, "Speaker 3", 31, 32.3)]
    obs = [look(i * 10 + 3, "Dana Lee", oid=i) for i in range(3)] + [look(32.7, "Dana Lee", oid=9)]
    tl = planner.build(segs)
    res = resolver.resolve(tl, obs, keys_for(tl), NameBook({"Dana Lee": "g1"}))
    names = {tuple(op.keys): op for op in ops_by_type(res, "name")}
    sure, guess = names[("Speaker 1",)], names[("Speaker 3",)]
    assert sure.confidence >= runs.HIGH
    assert guess.name == "Dana Lee" and guess.confidence <= resolver.GUESS_CEILING
    assert not guess.train and "too short to check by voice" in guess.reason
    assert res.decisions["Speaker 3"].tentative
    for autonomy in ("suggest", "apply_confident", "act_fully"):
        assert runs.policy(guess, runs.RunSpec(autonomy=autonomy))[0] == "suggest"
    # A speaker that already has a name gets no guess.
    keys = keys_for(tl, names={"Speaker 3": "Sam Park"}, set_by={"Speaker 3": "user"})
    res = resolver.resolve(tl, obs, keys, NameBook({"Dana Lee": "g1"}))
    assert not [op for op in ops_by_type(res, "name") if "Speaker 3" in op.keys]


def test_a_guess_the_turns_own_voice_contradicts_is_not_made():
    v = Voices()
    tl, obs, tv = _mixed_meeting(v)
    # Cy's Speaker 3 was never shown, but one stray look at a Cy turn says Al.
    cy = next(t for t in tl.turns if t.key == "Speaker 3")
    obs.append(look(cy.start + 3, "Al Moss", oid=77))
    res = resolver.resolve(tl, obs, keys_for(tl), NameBook({"Al Moss": "ga", "Bea Kim": "gb"}),
                           turn_vectors=tv)
    assert not [op for op in ops_by_type(res, "name") if "Speaker 3" in op.keys]


def test_every_change_says_why():
    v = Voices()
    tl, obs, tv = _mixed_meeting(v)
    res = resolver.resolve(tl, obs, keys_for(tl), NameBook({"Al Moss": "ga", "Bea Kim": "gb"}),
                           turn_vectors=tv)
    move = next(op for op in ops_by_type(res, "move"))
    assert "sound like Bea Kim" in move.reason and "voice match" in move.reason
    assert move.as_dict()["reason"] == move.reason
    name = next(op for op in ops_by_type(res, "name") if op.keys == ["Speaker 1"])
    assert name.reason.startswith("the screen showed Al Moss")


def test_mixed_and_doubted_keys_are_the_ones_heard_turn_by_turn():
    tl, obs = _two_speakers()
    keys = keys_for(tl)
    keys["Speaker 2"].voice = {"name": "Someone Else", "verdict": "strong"}
    tal = resolver.tally(tl, obs, owner=None, book=NameBook({"Dana Lee": "g1"}))
    assert resolver.suspect_keys(tal, keys, NameBook({"Dana Lee": "g1"})) == {"Speaker 2"}


# ── policy ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("autonomy,library,conf,replaces,want", [
    ("suggest", "follow_autonomy", 0.95, None, ("suggest", False)),
    ("apply_confident", "follow_autonomy", 0.90, None, ("apply", True)),
    ("apply_confident", "follow_autonomy", 0.80, None, ("suggest", False)),
    # A guess at a speaker nobody has named is still offered; one that
    # would replace a name needs more.
    ("apply_confident", "follow_autonomy", 0.60, None, ("suggest", False)),
    ("apply_confident", "follow_autonomy", 0.60, "Dana Smith", ("skip", False)),
    ("apply_confident", "follow_autonomy", 0.45, None, ("skip", False)),
    ("act_fully", "follow_autonomy", 0.75, None, ("apply", True)),
    ("act_fully", "never", 0.95, None, ("apply", False)),
    ("apply_confident", "on_accept", 0.95, None, ("apply", False)),
])
def test_what_a_run_may_do_on_its_own(autonomy, library, conf, replaces, want):
    op = resolver.Op("name", ["Speaker 1"], name="Dana Lee", confidence=conf, link=True,
                     train=True, replaces=replaces)
    assert runs.policy(op, runs.RunSpec(autonomy=autonomy, library_writes=library)) == want
    assert runs.policy(resolver.Op("finding", ["Speaker 1"], kind="echo"),
                       runs.RunSpec()) == ("note", False)


# ── reading the user's words ────────────────────────────────────────────────

def test_the_users_words_set_the_run():
    spec, chips = instructions.compile_spec(
        "Fix everything and update the voice profiles, don't ask me", runs.RunSpec())
    assert spec.autonomy == "act_fully" and spec.library_writes == "follow_autonomy"
    assert "Act fully" in chips
    spec, _ = instructions.compile_spec("Who is Speaker 4?", runs.RunSpec())
    assert spec.intent == "question" and spec.autonomy == "suggest"
    assert spec.targets == ["Speaker 4"]
    spec, _ = instructions.compile_spec("don't touch the voice profiles", runs.RunSpec())
    assert spec.library_writes == "never"
    spec, _ = instructions.compile_spec("", runs.RunSpec(autonomy="suggest"))
    assert spec.autonomy == "suggest"
    # A model's reading wins over the keywords when there is one.
    spec, chips = instructions.compile_spec(
        "Bob never turns his camera on", runs.RunSpec(),
        complete=lambda *a: {"intent": "identify", "autonomy": None, "library_writes": None,
                             "depth": "thorough", "recheck_user_labels": False,
                             "trust_screen": False, "targets": [], "protect": ["Tracey"],
                             "camera_off": ["Bob"], "hints": []})
    assert spec.depth == "thorough"
    assert any("Bob" in h for h in spec.hints)
    assert {"kind": "protect", "subject": {"name": "Tracey"}} in spec.constraints


def test_an_agent_never_gets_more_than_settings_allow():
    spec, _ = instructions.compile_spec("just do it, recheck my names, train the profiles",
                                        runs.RunSpec())
    capped = instructions.cap(spec, runs.RunSpec(autonomy="suggest", library_writes="never"))
    assert capped.autonomy == "suggest" and capped.library_writes == "never"
    assert capped.recheck_user_labels is False
    lower = instructions.cap(runs.RunSpec(autonomy="suggest"), runs.RunSpec(autonomy="act_fully"))
    assert lower.autonomy == "suggest"


# ── the vision request ──────────────────────────────────────────────────────

class _Messages:
    def __init__(self, answers):
        self.calls, self.answers = [], list(answers)

    def create(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def _reply(payload, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80),
        stop_reason=stop)


class _Http400(Exception):
    status_code = 400


def test_a_frame_batch_is_one_cached_structured_request():
    msgs = _Messages([_reply({"frames": []})])
    vc = vision.VisionClient("anthropic", SimpleNamespace(messages=msgs))
    vc.analyze("claude-haiku-5-5", [b"a", b"b"], ["Image 0 (0:01)", "Image 1 (0:09)"],
               "the task", "the meeting context")
    kw = msgs.calls[0]
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": prompts.SCHEMA}
    assert kw["output_config"]["effort"] == "low"
    assert kw["thinking"] == {"type": "disabled"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    content = kw["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "the meeting context",
                          "cache_control": {"type": "ephemeral"}}
    assert [c["type"] for c in content[1:]] == ["text", "image", "text", "image", "text"]
    assert vc.usage.cache_read_tokens == 80


def test_a_model_without_structured_outputs_falls_back_to_a_tool_once():
    msgs = _Messages([_Http400("output_config.format is not supported"),
                      SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"frames": []})],
                                      usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                                      stop_reason="tool_use")])
    vc = vision.VisionClient("anthropic", SimpleNamespace(messages=msgs))
    assert vc.analyze("claude-sonnet-5-5", [b"a"], ["Image 0"], "t", "c") == {"frames": []}
    assert msgs.calls[1]["tool_choice"] == {"type": "tool", "name": prompts.TOOL_NAME}
    assert msgs.calls[0]["thinking"] == {"type": "between_tools"}


def test_a_cut_off_answer_is_not_retried():
    msgs = _Messages([_reply({}, stop="max_tokens")])
    vc = vision.VisionClient("anthropic", SimpleNamespace(messages=msgs))
    with pytest.raises(vision.VisionError):
        vc.analyze("claude-haiku-5-5", [b"a"], ["Image 0"], "t", "c")
    assert len(msgs.calls) == 1


def test_the_gate_halves_on_a_rate_limit_and_grows_back():
    g = vision.Gate(start=8, low=2, high=10, grow_after=2)
    g.throttled()
    assert g.limit == 4
    g.ok(); g.ok()
    assert g.limit == 5


# ── a whole run against a stub model ────────────────────────────────────────

@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    storage.init_db()
    yield tmp_path


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), (20, 30, 40)).save(buf, "JPEG")
    return buf.getvalue()


class _Screen:
    """A stub vision model: reads the image times out of the request and
    answers who the meeting app showed speaking then."""

    def __init__(self, who_at):
        self.who_at = who_at
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        content = kw["messages"][0]["content"]
        frames = []
        i = 0
        for block in content:
            m = re.match(r"Image (\d+) \((\d+):(\d+)\)", block.get("text", "")) \
                if block["type"] == "text" else None
            if m:
                t = int(m.group(2)) * 60 + int(m.group(3))
                who = self.who_at(t)
                frames.append({"i": i, "meeting_visible": True, "app": "teams",
                               "layout": "gallery",
                               "speaking": [{"label": who, "cue": "border", "confidence": 0.9,
                                             "box": None, "self_view": False}] if who else [],
                               "roster": [], "participants_box": None,
                               "pinned_or_spotlight": False, "legibility": "good"})
                i += 1
        return _reply({"frames": frames})


def test_a_run_reads_the_screen_names_speakers_and_undoes_cleanly(data, monkeypatch):
    sid = storage.create_session("Brillian Integration Intro")
    for i in range(8):
        key = "Speaker 1" if i % 2 == 0 else "Speaker 2"
        storage.save_segment(sid, f"line {i}", key, float(i * 10), float(i * 10 + 9))
    who = lambda t: "Dana Lee" if (t // 10) % 2 == 0 else "Sam Park"  # noqa: E731
    jpeg = _jpeg()
    monkeypatch.setattr(runs.frames, "available", lambda sid, live=None: True)
    monkeypatch.setattr(runs.frames, "grab_many", lambda sid, times, **kw: [jpeg] * len(times))
    monkeypatch.setattr(runs, "_video_size", lambda sid, live: (1920, 1080))
    client = SimpleNamespace(messages=_Screen(who))
    pushed = []

    def apply_name(s, keys, name, gid, link, train, effects):
        for k in keys:
            storage.save_speaker_label(s, k, name=name, set_by="ai")

    deps = runs.Deps(
        segments=storage.get_speaker_segments,
        labels=lambda s: storage.get_speaker_label_rows(s),
        owner_name=lambda: "Ty Lane", candidates=lambda s: {"Dana Lee": None},
        live_media=lambda: {}, client_for=lambda p: client,
        setting=lambda k, d=None: {"speaker_ai_provider": "anthropic"}.get(k, d),
        push=lambda kind, payload: pushed.append(kind),
        apply_name=apply_name, save_run=storage.save_speaker_ai_run)
    det = runs.Detector(deps)
    run = det.start([sid], trigger="test", spec=runs.RunSpec(), wait=60)
    assert run.done.is_set() and run.status == "done", run.error
    assert pushed[0] == "speaker_run_start" and pushed[-1] == "speaker_run_done"
    rows = storage.get_speaker_label_rows(sid)
    assert rows["Speaker 1"]["name"] == "Dana Lee" and rows["Speaker 1"]["set_by"] == "ai"
    # Sam Park is not in the library, but four turns all showing him is enough.
    assert rows["Speaker 2"]["name"] == "Sam Park"
    changes = speaker_journal.list_changes(session_id=sid)
    assert [c["state"] for c in changes] == ["applied", "applied"]
    saved = storage.get_speaker_ai_run(run.id)
    assert saved["status"] == "done" and saved["report"]["totals"]["applied"] == 2
    # The screen was read once per moment and the readings were kept against
    # meeting time, so a second run reads nothing new.
    calls = client.messages.calls
    det.start([sid], trigger="test", spec=runs.RunSpec(), wait=60)
    assert client.messages.calls == calls
    # Undoing the run puts the labels back exactly (the rows did not exist).
    res = speaker_journal.undo_run(run.id)
    assert len(res["undone"]) == 2 and not res["conflicts"]
    assert storage.get_speaker_label_rows(sid, ["Speaker 1", "Speaker 2"]) == {
        "Speaker 1": None, "Speaker 2": None}


def _stub_run(data, monkeypatch, *, labels=None, apply_name=None):
    """A meeting where Speaker 1 is Dana Lee and Speaker 2 Sam Park, four
    turns each, a stub screen, and a Detector over them."""
    sid = storage.create_session("Brillian Integration Intro")
    for i in range(8):
        key = "Speaker 1" if i % 2 == 0 else "Speaker 2"
        storage.save_segment(sid, f"line {i}", key, float(i * 10), float(i * 10 + 9))
    who = lambda t: "Dana Lee" if (t // 10) % 2 == 0 else "Sam Park"  # noqa: E731
    jpeg = _jpeg()
    monkeypatch.setattr(runs.frames, "available", lambda s, live=None: True)
    monkeypatch.setattr(runs.frames, "grab_many", lambda s, times, **kw: [jpeg] * len(times))
    monkeypatch.setattr(runs, "_video_size", lambda s, live: (1920, 1080))
    client = SimpleNamespace(messages=_Screen(who))

    def default_apply(s, keys, name, gid, link, train, effects):
        for k in keys:
            storage.save_speaker_label(s, k, name=name, set_by="ai")

    deps = runs.Deps(
        segments=storage.get_speaker_segments,
        labels=labels or (lambda s: storage.get_speaker_label_rows(s)),
        owner_name=lambda: "Ty Lane", candidates=lambda s: {"Dana Lee": None},
        live_media=lambda: {}, client_for=lambda p: client,
        setting=lambda k, d=None: {"speaker_ai_provider": "anthropic",
                                   "speaker_ai_concurrency": -4}.get(k, d),
        apply_name=apply_name or default_apply, save_run=storage.save_speaker_ai_run)
    return sid, runs.Detector(deps)


def test_the_page_watches_each_frame_go_out_and_come_back(data, monkeypatch):
    """Every frame sent is pushed as a preview first ("looking"), then again
    with who was read as speaking and where, as fractions of the picture."""
    sid = storage.create_session("m")
    for i in range(4):
        storage.save_segment(sid, f"line {i}", "Speaker 1" if i % 2 == 0 else "Speaker 2",
                             float(i * 10), float(i * 10 + 9))
    jpeg = _jpeg()                                      # 64 x 36
    monkeypatch.setattr(runs.frames, "available", lambda s, live=None: True)
    monkeypatch.setattr(runs.frames, "grab_many", lambda s, times, **kw: [jpeg] * len(times))
    monkeypatch.setattr(runs, "_video_size", lambda s, live: (1920, 1080))

    class Boxed(_Screen):
        def create(self, **kw):
            reply = super().create(**kw)
            payload = json.loads(reply.content[0].text)
            for f in payload["frames"]:
                for s in f["speaking"]:
                    s["box"] = [16, 9, 32, 18]          # the middle quarter of the image
            reply.content[0].text = json.dumps(payload)
            return reply

    who = lambda t: "Dana Lee" if (t // 10) % 2 == 0 else "Sam Park"  # noqa: E731
    client = SimpleNamespace(messages=Boxed(who))
    pushed = []
    deps = runs.Deps(
        segments=storage.get_speaker_segments, labels=lambda s: storage.get_speaker_label_rows(s),
        owner_name=lambda: "Ty Lane", candidates=lambda s: {"Dana Lee": None},
        live_media=lambda: {}, client_for=lambda p: client,
        setting=lambda k, d=None: {"speaker_ai_provider": "anthropic"}.get(k, d),
        push=lambda kind, payload: pushed.append((kind, payload)),
        apply_name=lambda *a: None, save_run=storage.save_speaker_ai_run)
    det = runs.Detector(deps)
    run = det.start([sid], trigger="test", spec=runs.RunSpec(autonomy="suggest"), wait=60)
    assert run.status == "done", run.error
    events = [p for k, p in pushed if k == "speaker_run_frames"]
    sent = [f for e in events for f in e["frames"] if f["state"] == "looking"]
    read = [f for e in events for f in e["frames"] if f["state"] == "read"]
    assert sent and len(read) == len(sent)
    assert all(e["run_id"] == run.id and e["session_id"] == sid for e in events)
    first = read[0]
    assert first["speaking"][0]["name"] in ("Dana Lee", "Sam Park")
    assert first["speaking"][0]["box"] == [0.25, 0.25, 0.5, 0.5]
    assert first["obs"] is not None                     # the stored reading, for the full frame
    # The previews stay for the page to show after the run, until later runs push them out.
    kept = det.frames.list(run.id)
    assert {f["id"] for f in kept} == {f["id"] for f in sent} and all(f["state"] == "read" for f in kept)
    assert det.frames.jpeg(run.id, kept[0]["id"])[:2] == b"\xff\xd8"
    # Stages the steps on the page follow, in order.
    stages = [p.get("stage") for k, p in pushed if k == "speaker_run_progress"]
    assert stages[0] == "reading" and "deciding" in stages


def test_the_frame_store_keeps_the_last_few_runs():
    store = runs.FrameStore()
    jpeg = _jpeg()
    for n in range(runs.FrameStore.KEEP_RUNS + 1):
        store.begin(f"r{n}")
        meta = store.add(f"r{n}", jpeg, t=12.3456, kind="read")
    assert store.list("r0") is None                     # the oldest run is gone
    assert meta == {"id": 1, "t": 12.35, "kind": "read", "state": "looking", "w": 64, "h": 36}
    store.settle(f"r{runs.FrameStore.KEEP_RUNS}")
    assert store.list(f"r{runs.FrameStore.KEEP_RUNS}")[0]["state"] == "failed"
    assert store.update("nope", 1, state="read") is None
    # Boxes come back as fractions of the picture shown: the crop, or the frame.
    assert runs._norm_box([110, 60, 210, 110], [100, 50, 300, 150], None) == [0.05, 0.1, 0.55, 0.6]
    assert runs._norm_box([0, 0, 960, 540], None, (1920, 1080)) == [0.0, 0.0, 0.5, 0.5]
    assert runs._norm_box([5, 5, 6, 6], None, (1920, 1080)) is None   # too small to draw
    assert runs._norm_box(None, None, (1920, 1080)) is None


def test_a_stopped_run_changes_nothing(data, monkeypatch):
    sid, det = _stub_run(data, monkeypatch)
    run = runs.Run("r-stop", [sid], "test", "", runs.RunSpec())
    run.cancel.set()
    out = det.run_session(run, sid)
    assert out["status"] == "cancelled" and not out["applied"] and not out["suggested"]
    assert storage.get_speaker_label_rows(sid) == {}


def test_a_name_set_while_the_run_read_is_left_alone(data, monkeypatch):
    calls = {"n": 0}

    def labels(s):
        # The first read is the run's start; by the time it applies, the user
        # has named Speaker 1 herself.
        calls["n"] += 1
        if calls["n"] > 1:
            storage.save_speaker_label(s, "Speaker 1", name="Dana Whitfield", set_by="user")
        return storage.get_speaker_label_rows(s)

    sid, det = _stub_run(data, monkeypatch, labels=labels)
    run = det.start([sid], trigger="test", spec=runs.RunSpec(), wait=60)
    assert run.status == "done", run.error
    assert storage.get_speaker_label_rows(sid)["Speaker 1"]["name"] == "Dana Whitfield"
    findings = run.report["sessions"][0]["findings"]
    assert any(f["kind"] == "edited_meanwhile" and "Speaker 1" in f["keys"] for f in findings)


def test_a_second_run_on_the_same_meeting_waits_its_turn(data, monkeypatch):
    sid, det = _stub_run(data, monkeypatch)
    first = runs.Run("busy", [sid], "test", "", runs.RunSpec(), status="running")
    det.runs[first.id] = first
    with pytest.raises(RuntimeError):
        det.start([sid], trigger="test", spec=runs.RunSpec())


def test_a_move_waits_until_its_new_speaker_carries_the_name(data, monkeypatch):
    sid, det = _stub_run(data, monkeypatch)
    op = {"type": "move", "keys": ["Speaker 2"], "to_key": "Speaker 1", "name": "Dana Lee",
          "segment_ids": [2]}
    with pytest.raises(ValueError, match="first"):
        det.apply(sid, op, train=False, run_id=None)
    assert runs._carries({"name": "Dana Lee"}, "Speaker 1", "dana lee")
    assert runs._carries(None, "Speaker 1", "Speaker 1")


def test_a_run_aimed_at_one_speaker_changes_only_that_one():
    ops = [resolver.Op("name", ["Speaker 1", "Speaker 4"], name="Dana Lee", confidence=0.9),
           resolver.Op("name", ["Speaker 2"], name="Sam Park", confidence=0.9),
           resolver.Op("move", ["Speaker 4", "Speaker 7"], name="Bob Ray", to_key="Speaker 9",
                       segment_ids=[40, 70]),
           resolver.Op("finding", [], kind="voice_only")]
    kept = runs._within_focus(ops, {"Speaker 4"}, {40: "Speaker 4", 70: "Speaker 7"})
    assert [(o.type, o.keys) for o in kept] == [("name", ["Speaker 4"]),
                                                ("move", ["Speaker 4"]), ("finding", [])]
    assert kept[0].summary == "Named Speaker 4 Dana Lee" and kept[1].segment_ids == [40]


def test_names_off_the_screen_carry_no_markup():
    name = normalize('<img src=x onerror="alert(1)">Bob Ray')
    assert not set('<>"`{}') & set(name) and name.endswith("Bob Ray")
    assert "<" not in normalize("<script>") and len(normalize("A" * 500)) <= 80


def test_this_is_x_names_a_speaker_that_already_has_a_name():
    tl, obs = _two_speakers()
    keys = keys_for(tl, names={"Speaker 1": "Dana Smith"}, set_by={"Speaker 1": "user"})
    res = resolver.resolve(tl, obs, keys, NameBook({"Dana Lee": "g1"}), constraints=[
        {"kind": "is", "subject": {"key": "Speaker 1"}, "value": "Pat Gordon"}])
    op = next(o for o in ops_by_type(res, "name") if o.keys == ["Speaker 1"])
    assert op.name == "Pat Gordon" and op.confidence == 1.0


def test_concurrency_never_drops_below_one():
    g = vision.Gate(start=-3)
    assert g.limit == 1
    g.throttled()
    assert g.limit == 1
    assert instructions.compile_spec("look at speaker 4", runs.RunSpec())[0].targets == ["Speaker 4"]


def test_an_agent_cannot_trust_the_screen_past_settings():
    spec, _ = instructions.compile_spec("trust the screen", runs.RunSpec())
    assert spec.trust_screen
    assert instructions.cap(spec, runs.RunSpec()).trust_screen is False


# ── the journal ─────────────────────────────────────────────────────────────


def test_a_suggestion_is_accepted_once_and_a_rerun_replaces_old_ones(data):
    sid = storage.create_session("m")
    a = speaker_journal.record(session_id=sid, actor="ai", op={"type": "name", "keys": ["Speaker 1"]},
                               state="suggested", run_id="r1")
    b = speaker_journal.record(session_id=sid, actor="ai", op={"type": "name", "keys": ["Speaker 2"]},
                               state="suggested", run_id="r1")
    assert speaker_journal.claim(a) is True
    assert speaker_journal.claim(a) is False                 # the second accept finds it taken
    assert speaker_journal.supersede_suggestions(sid, "r2", keys={"Speaker 2"}) == 1
    assert speaker_journal.get(b)["state"] == "superseded"
    assert speaker_journal.get(a)["state"] == "applying"


def test_naming_a_speaker_answers_the_name_suggestions_waiting_for_it(data):
    sid = storage.create_session("m")
    guess = speaker_journal.record(session_id=sid, actor="ai", state="suggested", run_id="r1",
                                   op={"type": "name", "keys": ["Speaker 3"], "name": "Jorge Remirez"})
    other = speaker_journal.record(session_id=sid, actor="ai", state="suggested", run_id="r1",
                                   op={"type": "name", "keys": ["Speaker 4"], "name": "Dana Lee"})
    move = speaker_journal.record(session_id=sid, actor="ai", state="suggested", run_id="r1",
                                  op={"type": "move", "keys": ["Speaker 3"], "to_key": "Speaker 1",
                                      "name": "Dana Lee", "segment_ids": [1]})
    assert speaker_journal.retire_name_suggestions(sid, ["Speaker 3"]) == 1
    states = {c["id"]: c["state"] for c in speaker_journal.list_changes(session_id=sid)}
    assert states == {guess: "superseded", other: "suggested", move: "suggested"}
    assert speaker_journal.retire_name_suggestions(sid, []) == 0


def test_a_reanalysis_expires_changes_and_a_trim_moves_the_readings(data):
    sid = storage.create_session("m")
    storage.save_segment(sid, "hi", "Speaker 1", 0.0, 5.0)
    cid = speaker_journal.record(session_id=sid, actor="ai", op={"type": "name"}, state="applied")
    storage.add_speaker_constraint(sid, "is_not", {"key": "Speaker 1"}, "Bob")
    storage.add_speaker_constraint(sid, "protect", {"name": "Tracey"}, None)
    for t in (2.0, 12.0, 40.0):
        obs = look(t, "Dana Lee")
        from ai.speaker_detect import observations as obs_mod
        obs_mod.store(sid, obs, "r1", None)
    storage.trim_session_segments(sid, 10.0, 30.0)
    assert sorted(o.t for o in obs_mod.load(sid)) == [2.0]       # 12 s is now 2 s; 2 and 40 cut
    assert speaker_journal.get(cid)["state"] == "expired"
    storage.reset_session_transcript(sid)
    kinds = [c["kind"] for c in storage.list_speaker_constraints(sid)]
    assert kinds == ["protect"]                                   # key hints void, name hints kept
    storage.delete_session(sid)
    assert obs_mod.load(sid) == [] and speaker_journal.list_changes(session_id=sid) == []

@pytest.fixture()
def fp(data):
    lib = SpeakerFingerprintDB.__new__(SpeakerFingerprintDB)
    lib._db_path = paths.db_path()
    lib._ready = False
    lib._inference = None
    lib._me_id = None
    return lib


def test_undo_restores_lines_and_voice_samples_and_refuses_after_a_later_edit(data, fp):
    sid = storage.create_session("m")
    seg_id = storage.save_segment(sid, "hello", "Speaker 1", 0.0, 5.0)
    storage.save_speaker_label(sid, "Speaker 1", name="Speaker 1")
    gid = fp.create_global_speaker("Dana Lee")
    before = speaker_journal.snapshot(sid, ["Speaker 1", "Speaker 2"], [seg_id])
    storage.save_speaker_label(sid, "Speaker 1", name="Dana Lee", set_by="ai")
    storage.save_speaker_label(sid, "Speaker 2", name="Sam Park", set_by="ai")
    storage.save_segment_source_override(seg_id, "Speaker 2")
    emb = fp.add_embedding(gid, sid, "Speaker 1", np.ones(256, dtype=np.float32) / 16.0, 3.0)
    after = speaker_journal.snapshot(sid, ["Speaker 1", "Speaker 2"], [seg_id])
    cid = speaker_journal.record(session_id=sid, actor="ai", op={"type": "name"}, before=before,
                                 after=after, effects={"embedding_ids": [emb],
                                                       "created_profiles": [gid]})
    # Edited again since: undo refuses unless forced.
    storage.save_speaker_label(sid, "Speaker 1", name="Dana Lee-Smith", set_by="user")
    with pytest.raises(speaker_journal.Conflict):
        speaker_journal.undo(cid, fp)
    storage.save_speaker_label(sid, "Speaker 1", name="Dana Lee", set_by="ai")
    res = speaker_journal.undo(cid, fp)
    rows = storage.get_speaker_label_rows(sid, ["Speaker 1", "Speaker 2"])
    assert rows["Speaker 1"]["name"] == "Speaker 1" and rows["Speaker 2"] is None
    assert storage.get_segment(seg_id)["source_override"] is None
    assert fp.get_global_speaker(gid) is None            # it made the profile, nothing else uses it
    assert res["session_id"] == sid
    with pytest.raises(ValueError):
        speaker_journal.undo(cid, fp)                     # already undone


# ── routes ──────────────────────────────────────────────────────────────────

def test_the_routes_answer_with_the_right_codes():
    def undo_change(cid, force):
        if not force:
            raise speaker_journal.Conflict(cid, "Those speakers were changed again.")
        return {"session_id": "s1"}

    def start(sessions, trigger, instructions_text, overrides):
        if overrides.get("autonomy") == "boom":
            raise RuntimeError("off")
        return SimpleNamespace(as_dict=lambda: {"id": "r1", "trigger": trigger,
                                                "overrides": overrides})

    hooks = routes.Hooks(
        detector=SimpleNamespace(get=lambda rid: None, cancel=lambda rid: rid == "r1"),
        enabled=lambda: True, session_exists=lambda sid: sid in ("s1", "s2"),
        has_video=lambda sid: sid == "s1", start=start,
        insights=lambda sid: {"session": sid}, undo_change=undo_change,
        undo_run=lambda rid: {"undone": [1]}, apply_change=lambda cid: {"id": cid},
        dismiss_change=lambda cid: {"id": cid},
        add_constraint=lambda sid, kind, subject, value: {"kind": kind, "value": value},
        evidence_jpeg=lambda sid, oid, full: None)
    app = Flask(__name__)
    routes.register(app, hooks)
    c = app.test_client()
    assert c.post("/api/sessions/nope/speakers/identify").status_code == 404
    assert c.post("/api/sessions/s2/speakers/identify").status_code == 409     # no video
    r = c.post("/api/sessions/s1/speakers/identify", json={"autonomy": "suggest", "x": 1})
    assert r.status_code == 200 and r.get_json()["run"]["overrides"] == {"autonomy": "suggest"}
    assert c.post("/api/sessions/s1/speakers/identify",
                  json={"autonomy": "boom"}).status_code == 409
    assert c.get("/api/sessions/s1/speakers/insights").get_json() == {"session": "s1"}
    assert c.post("/api/sessions/s1/speakers/constraints", json={"kind": "bad"}).status_code == 400
    assert c.post("/api/sessions/s1/speakers/constraints",
                  json={"kind": "is_not", "speaker_key": "Speaker 1"}).status_code == 400
    assert c.post("/api/sessions/s1/speakers/constraints",
                  json={"kind": "protect", "speaker_key": "Speaker 1"}).status_code == 200
    r = c.post("/api/speaker-changes/5/undo")
    assert r.status_code == 409 and r.get_json()["conflict"] is True
    assert c.post("/api/speaker-changes/5/undo", json={"force": True}).status_code == 200
    assert c.get("/api/sessions/s1/speakers/evidence/3.jpg").status_code == 404
    assert c.post("/api/speaker-runs/r1/cancel").get_json() == {"ok": True}


def test_the_frame_routes_serve_a_runs_previews():
    store = runs.FrameStore()
    store.begin("r1")
    meta = store.add("r1", _jpeg(), t=3.0, kind="scout")
    hooks = routes.Hooks(
        detector=SimpleNamespace(get=lambda rid: None, cancel=lambda rid: False, frames=store),
        enabled=lambda: True, session_exists=lambda sid: True, has_video=lambda sid: True,
        start=None, insights=None, undo_change=None, undo_run=None, apply_change=None,
        dismiss_change=None, add_constraint=None, evidence_jpeg=None)
    app = Flask(__name__)
    routes.register(app, hooks)
    c = app.test_client()
    assert c.get("/api/speaker-runs/r1/frames").get_json()["frames"][0]["id"] == meta["id"]
    assert c.get("/api/speaker-runs/gone/frames").get_json() == {"frames": None}
    r = c.get(f"/api/speaker-runs/r1/frames/{meta['id']}.jpg")
    assert r.status_code == 200 and r.mimetype == "image/jpeg"
    assert c.get("/api/speaker-runs/r1/frames/99.jpg").status_code == 404


def test_the_roster_lists_every_speaker_and_moved_lines_report_where_they_are(data):
    sid = storage.create_session("m")
    a = storage.save_segment(sid, "hi", "Speaker 1", 0.0, 4.0)
    storage.save_segment(sid, "more", "Speaker 1", 5.0, 9.5)
    b = storage.save_segment(sid, "yes", "Speaker 2", 10.0, 11.3)
    storage.save_segment(sid, "*cough*", "[Noise]", 12.0, 12.4)
    storage.save_segment(sid, "um", "Speaker 4", 13.0, 13.5)
    c = storage.save_segment(sid, "me", "me", 14.0, 15.0)
    storage.save_speaker_label(sid, "Speaker 1", name="Dana Lee", set_by="ai")
    storage.save_segment_label_override(storage.get_speaker_segments(sid)[-2]["id"], "[Noise]")
    storage.save_segment_source_override(b, "Speaker 1")
    storage.save_segment_label_override(b, "Dana Lee")
    rows = {r["key"]: r for r in storage.speaker_roster(sid)}
    assert rows["Speaker 1"]["lines"] == 3 and rows["Speaker 1"]["name"] == "Dana Lee"
    assert rows["Speaker 1"]["set_by"] == "ai" and rows["Speaker 1"]["seconds"] == 9.8
    assert "Speaker 2" not in rows                      # its only line moved away
    assert rows["[Noise]"]["is_noise"] and rows["Speaker 4"]["is_noise"]   # every line marked noise
    assert not rows["me"]["is_noise"] and rows["me"]["name"] == "me"
    moved = storage.get_segment_speakers([b, a, 999])
    assert moved == [{"id": a, "key": "Speaker 1", "label": None, "source": "Speaker 1"},
                     {"id": b, "key": "Speaker 1", "label": "Dana Lee", "source": "Speaker 2"}]
    assert storage.get_segment_speakers([]) == [] and c
