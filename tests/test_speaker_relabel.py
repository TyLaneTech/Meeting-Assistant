"""Tests for the bulk speaker relabel planner, token store, and applier.

Everything runs against fake deps: no database, no network, no server.
Run: .venv/Scripts/python tests/test_speaker_relabel.py
(also collects cleanly under pytest where that is installed)
"""
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai import speaker_relabel  # noqa: E402


@contextlib.contextmanager
def raises(exc_type):
    """Minimal pytest.raises stand-in: this repo's tests run without pytest."""
    caught = []
    try:
        yield caught
    except exc_type as e:
        caught.append(e)
        return
    raise AssertionError(f"expected {exc_type.__name__}, nothing was raised")


class FakeDeps:
    """In-memory speaker_labels plus recorders for every write primitive."""

    def __init__(self, rows, profiles=None, stats=None, overrides=None,
                 linked=None, session_infos=None, me_id=None,
                 me_key='me', ready=True, patch_fails_on=None):
        # rows: list of dicts with session_id, speaker_key, name, [is_noise],
        # [global_id], [title], [started_at]
        self.rows = [dict(r) for r in rows]
        self.profiles = list(profiles or [])
        self.stats = stats or {}
        self.overrides = overrides or {}
        self.patched = []
        self.bulk_linked = []
        self.merged = []
        self.created = []
        self.summaries = []
        # global_id -> speaker_labels rows currently linked to it
        self.linked = linked or {}
        self.session_infos = session_infos
        self.me_id = me_id
        self.me_key_value = me_key
        self.ready = ready
        self.patch_fails_on = patch_fails_on

    # -- reads ---------------------------------------------------------------

    def find_labels(self, name, match, session_ids):
        target = speaker_relabel.normalize(name)
        out = []
        for row in self.rows:
            row_name = speaker_relabel.normalize(row.get("name") or "")
            hit = target in row_name if match == "contains" else row_name == target
            if not hit:
                continue
            if session_ids is not None and row["session_id"] not in session_ids:
                continue
            out.append({
                "session_id": row["session_id"],
                "speaker_key": row["speaker_key"],
                "name": row.get("name"),
                "color": row.get("color"),
                "global_id": row.get("global_id"),
                "is_noise": row.get("is_noise", 0),
                "title": row.get("title", "Meeting " + row["session_id"]),
                "started_at": row.get("started_at", "2026-09-01T10:00:00"),
            })
        return out

    def speaker_time_stats(self, session_id):
        return self.stats.get(session_id, [])

    def count_label_overrides(self, name, match, session_ids):
        target = speaker_relabel.normalize(name)
        out = {}
        for (sid, override_name), count in self.overrides.items():
            row_name = speaker_relabel.normalize(override_name)
            hit = target in row_name if match == "contains" else row_name == target
            if not hit:
                continue
            if session_ids is not None and sid not in session_ids:
                continue
            out[sid] = out.get(sid, 0) + count
        return out

    def find_profile_by_name(self, name):
        target = speaker_relabel.normalize(name)
        for p in self.profiles:
            if speaker_relabel.normalize(p["name"]) == target:
                return dict(p)
        return None

    def linked_labels(self, global_id):
        return [dict(r) for r in self.linked.get(global_id, [])]

    def session_info(self, session_ids):
        if self.session_infos is not None:
            return {sid: self.session_infos[sid] for sid in session_ids
                    if sid in self.session_infos}
        known = {r['session_id'] for r in self.rows}
        return {sid: {'title': 'Meeting ' + sid,
                      'started_at': '2026-09-01T10:00:00'}
                for sid in session_ids if sid in known}

    # -- writes --------------------------------------------------------------

    def create_profile(self, name):
        gid = f"gid-new-{len(self.created)}"
        self.created.append((name, gid))
        self.profiles.append({"id": gid, "name": name, "emb_count": 0})
        return gid

    def bulk_link(self, name, global_id):
        self.bulk_linked.append((name, global_id))
        return {"linked_count": 0, "global_id": global_id}

    def merge_profiles(self, keep_id, merge_id):
        self.merged.append((keep_id, merge_id))
        return {"name": "merged"}

    def patch_session(self, session_id, speaker_keys, name):
        if session_id == self.patch_fails_on:
            raise RuntimeError('database is locked')
        self.patched.append((session_id, list(speaker_keys), name))
        return []

    def queue_summaries(self, session_ids, from_name, to_name):
        self.summaries.append((list(session_ids), from_name, to_name))
        return len(session_ids)

    def bundle(self):
        return speaker_relabel.RelabelDeps(
            find_labels=self.find_labels,
            speaker_time_stats=self.speaker_time_stats,
            count_label_overrides=self.count_label_overrides,
            find_profile_by_name=self.find_profile_by_name,
            create_profile=self.create_profile,
            bulk_link=self.bulk_link,
            merge_profiles=self.merge_profiles,
            patch_session=self.patch_session,
            linked_labels=self.linked_labels,
            session_info=self.session_info,
            me_profile_id=lambda: self.me_id,
            me_key=self.me_key_value,
            library_ready=lambda: self.ready,
            queue_summaries=self.queue_summaries,
        )


def _rows():
    return [
        {"session_id": "s1", "speaker_key": "SPEAKER_00", "name": "Justin",
         "title": "Renewal Call", "started_at": "2026-08-01T09:00:00"},
        {"session_id": "s1", "speaker_key": "SPEAKER_02", "name": "Justin R",
         "title": "Renewal Call", "started_at": "2026-08-01T09:00:00"},
        {"session_id": "s2", "speaker_key": "SPEAKER_01", "name": "justin",
         "title": "Standup", "started_at": "2026-08-05T09:00:00"},
        {"session_id": "s3", "speaker_key": "SPEAKER_00", "name": "Dana",
         "title": "Other", "started_at": "2026-08-09T09:00:00"},
    ]


def _noise_row():
    """A diarizer phantom flagged as noise, sharing the target name."""
    return {"session_id": "s2", "speaker_key": "SPEAKER_07",
            "name": "Justin", "is_noise": 1, "title": "Standup",
            "started_at": "2026-08-05T09:00:00"}


def _stats():
    return {
        "s1": [
            {"speaker_key": "SPEAKER_00", "segment_count": 12, "talk_seconds": 300.0},
            {"speaker_key": "SPEAKER_02", "segment_count": 3, "talk_seconds": 45.0},
        ],
        "s2": [
            {"speaker_key": "SPEAKER_01", "segment_count": 7, "talk_seconds": 120.0},
        ],
    }


_DEFAULT_TTL = speaker_relabel.TOKEN_TTL_SEC


def _reset():
    """Fresh token store and default TTL before each test."""
    speaker_relabel._TOKENS.clear()
    speaker_relabel.TOKEN_TTL_SEC = _DEFAULT_TTL


def _plan(deps, **kw):
    args = {
        "from_name": "Justin",
        "to_name": "Jennifer Davis",
        "scope": "library",
        "session_ids": None,
        "match": "exact",
    }
    args.update(kw)
    return speaker_relabel.build_plan(
        args["from_name"], args["to_name"], args["scope"],
        args["session_ids"], args["match"], deps=deps.bundle(),
    )


# -- planning -----------------------------------------------------------------

def test_plan_matches_exact_names_only_by_default():
    _reset()
    deps = FakeDeps(_rows() + [_noise_row()], stats=_stats())
    plan = _plan(deps)
    keys = {(s["session_id"], k["speaker_key"])
            for s in plan["sessions"] for k in s["keys"]}
    # "Justin R" is a different speaker; the is_noise row is excluded.
    assert keys == {("s1", "SPEAKER_00"), ("s2", "SPEAKER_01")}
    assert plan["key_count"] == 2
    assert plan["session_count"] == 2
    assert plan["segment_total"] == 19
    assert plan["talk_seconds"] == 420.0


def test_plan_contains_mode_widens_the_match():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    plan = _plan(deps, match="contains")
    keys = {k["speaker_key"] for s in plan["sessions"] for k in s["keys"]}
    assert keys == {"SPEAKER_00", "SPEAKER_02", "SPEAKER_01"}
    assert plan["key_count"] == 3


def test_plan_session_scope_stays_in_one_meeting():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    plan = _plan(deps, scope="session", session_ids=["s1"])
    assert plan["session_count"] == 1
    assert plan["sessions"][0]["session_id"] == "s1"
    assert plan["strategy"] == "session_patch"


def test_plan_session_scope_requires_exactly_one_id():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    with raises(ValueError):
        _plan(deps, scope="session", session_ids=["s1", "s2"])
    with raises(ValueError):
        _plan(deps, scope="session", session_ids=None)


def test_plan_reports_label_override_warning():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats(),
                    overrides={("s1", "Justin"): 4})
    plan = _plan(deps)
    assert any("4 transcript line(s)" in w for w in plan["warnings"])


def test_plan_warns_when_no_speaker_matches():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    plan = _plan(deps, from_name="Nobody")
    assert plan["sessions"] == []
    assert plan["key_count"] == 0
    assert any("list_speakers" in w for w in plan["warnings"])


# -- strategy selection -------------------------------------------------------

def test_strategy_bulk_link_when_target_profile_is_new():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    plan = _plan(deps)
    assert plan["strategy"] == "bulk_link"
    assert plan["creates_profile"] is True


def test_strategy_merge_profiles_when_both_exist_with_embeddings():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats(), profiles=[
        {"id": "gid-justin", "name": "Justin", "emb_count": 9},
        {"id": "gid-jen", "name": "Jennifer Davis", "emb_count": 4},
    ])
    plan = _plan(deps)
    assert plan["strategy"] == "merge_profiles"
    assert plan["from_profile_id"] == "gid-justin"
    assert plan["to_profile_id"] == "gid-jen"
    assert any("merged" in w for w in plan["warnings"])


def test_strategy_bulk_link_when_source_profile_has_no_embeddings():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats(), profiles=[
        {"id": "gid-justin", "name": "Justin", "emb_count": 0},
        {"id": "gid-jen", "name": "Jennifer Davis", "emb_count": 4},
    ])
    assert _plan(deps)["strategy"] == "bulk_link"


def test_strategy_falls_back_to_session_patch_on_partial_scope():
    _reset()
    # A filtered library plan must not reach for the library-wide primitives:
    # they would rewrite the rows the filter deliberately excluded.
    deps = FakeDeps(_rows(), stats=_stats())
    plan = _plan(deps, session_ids=["s1"])
    assert plan["strategy"] == "session_patch"
    assert any("outside the current filter" in w for w in plan["warnings"])


def test_strategy_falls_back_to_session_patch_for_contains_mode():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    assert _plan(deps, match="contains")["strategy"] == "session_patch"


# -- tokens -------------------------------------------------------------------

def test_token_is_single_use():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    speaker_relabel.apply_plan(token, current_request_id="req-2", deps=deps.bundle())
    with raises(ValueError):
        speaker_relabel.apply_plan(token, current_request_id="req-3", deps=deps.bundle())


def test_token_expires():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    speaker_relabel.TOKEN_TTL_SEC = -1.0
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    assert speaker_relabel.get_plan(token) is None
    with raises(ValueError):
        speaker_relabel.apply_plan(token, current_request_id="req-2", deps=deps.bundle())


def test_cancel_drops_the_token():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    assert speaker_relabel.cancel(token) is True
    assert speaker_relabel.cancel(token) is False
    with raises(ValueError):
        speaker_relabel.apply_plan(token, current_request_id=None, deps=deps.bundle())


# -- applying -----------------------------------------------------------------

def test_apply_refuses_the_minting_request_id():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    with raises(ValueError) as excinfo:
        speaker_relabel.apply_plan(token, current_request_id="req-1", deps=deps.bundle())
    assert "current turn" in str(excinfo[0])
    # Refusing must not burn the token: the next turn can still confirm.
    assert speaker_relabel.get_plan(token) is not None
    assert deps.bulk_linked == []


def test_apply_refuses_an_unknown_token():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    with raises(ValueError):
        speaker_relabel.apply_plan("not-a-token", current_request_id=None,
                                   deps=deps.bundle())


def test_ui_path_may_apply_with_no_request_id():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    result = speaker_relabel.apply_plan(token, current_request_id=None,
                                        confirmed_by="ui", deps=deps.bundle())
    assert result["applied"] is True
    assert result["confirmed_by"] == "ui"


def test_apply_session_patch_calls_the_route_helper_per_session():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps, session_ids=["s1"]), "req-1")
    result = speaker_relabel.apply_plan(token, current_request_id="req-2",
                                        deps=deps.bundle())
    assert deps.patched == [("s1", ["SPEAKER_00"], "Jennifer Davis")]
    assert deps.bulk_linked == []
    assert result["strategy"] == "session_patch"
    assert result["key_count"] == 1
    assert result["summaries_queued"] == 1


def test_apply_bulk_link_creates_the_target_profile_once():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    result = speaker_relabel.apply_plan(token, current_request_id="req-2",
                                        deps=deps.bundle())
    assert deps.created == [("Jennifer Davis", "gid-new-0")]
    assert deps.bulk_linked == [("Justin", "gid-new-0")]
    assert deps.patched == []
    assert result["strategy"] == "bulk_link"


def test_apply_merge_profiles_merges_then_relinks_stragglers():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats(), profiles=[
        {"id": "gid-justin", "name": "Justin", "emb_count": 9},
        {"id": "gid-jen", "name": "Jennifer Davis", "emb_count": 4},
    ])
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    result = speaker_relabel.apply_plan(token, current_request_id="req-2",
                                        deps=deps.bundle())
    assert deps.merged == [("gid-jen", "gid-justin")]
    # Labels never linked to the old profile still have to be renamed.
    assert deps.bulk_linked == [("Justin", "gid-jen")]
    assert result["strategy"] == "merge_profiles"


def test_apply_uses_the_pinned_rows_not_the_current_data():
    _reset()
    rows = _rows()
    # A Justin outside the filter keeps the plan on the session_patch path,
    # which is the one that carries per-session keys.
    rows.append({"session_id": "s4", "speaker_key": "SPEAKER_00",
                 "name": "Justin", "title": "Elsewhere",
                 "started_at": "2026-08-20T09:00:00"})
    deps = FakeDeps(rows, stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps, session_ids=["s1", "s2"]), "req-1")
    # The library changes after planning: a new Justin appears, an old one goes.
    deps.rows = [r for r in deps.rows if r["speaker_key"] != "SPEAKER_00"]
    deps.rows.append({"session_id": "s9", "speaker_key": "SPEAKER_04",
                      "name": "Justin", "title": "Late", "started_at": "2026-09-02T09:00:00"})
    result = speaker_relabel.apply_plan(token, current_request_id="req-2",
                                        deps=deps.bundle())
    assert deps.patched == [
        ("s1", ["SPEAKER_00"], "Jennifer Davis"),
        ("s2", ["SPEAKER_01"], "Jennifer Davis"),
    ]
    assert [s["session_id"] for s in result["sessions"]] == ["s1", "s2"]


def test_apply_refuses_an_empty_plan():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps, from_name="Nobody"), "req-1")
    with raises(ValueError):
        speaker_relabel.apply_plan(token, current_request_id="req-2", deps=deps.bundle())


# -- plan card ----------------------------------------------------------------

def test_plan_card_carries_the_token_and_per_session_lines():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    plan = _plan(deps)
    card = speaker_relabel.plan_card(plan, "tok-123")
    assert card["token"] == "tok-123"
    assert card["key_count"] == 2
    assert [s["session_id"] for s in card["sessions"]] == ["s1", "s2"]
    assert all("keys" not in s for s in card["sessions"])


# -- P0-1: blast radius of the library-wide primitives ------------------------

def test_noise_row_forces_session_patch_unfiltered():
    _reset()
    # bulk_link_by_name has no noise predicate, so a noise "Justin" anywhere in
    # the library would be renamed and its audio trained into the profile.
    deps = FakeDeps(_rows() + [_noise_row()], stats=_stats())
    plan = _plan(deps)
    assert plan["strategy"] == "session_patch"
    assert any("marked as noise" in w for w in plan["warnings"])
    assert any("left alone" in w for w in plan["warnings"])


def test_noise_row_forces_session_patch_filtered():
    _reset()
    deps = FakeDeps(_rows() + [_noise_row()], stats=_stats())
    plan = _plan(deps, session_ids=["s1", "s2"])
    assert plan["strategy"] == "session_patch"
    warning = next(w for w in plan["warnings"] if "left alone" in w)
    assert "1 marked as noise" in warning


def test_session_patch_never_calls_the_library_primitives():
    _reset()
    deps = FakeDeps(_rows() + [_noise_row()], stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    result = speaker_relabel.apply_plan(token, current_request_id="req-2",
                                        deps=deps.bundle())
    assert result["strategy"] == "session_patch"
    assert deps.bulk_linked == []
    assert deps.merged == []
    # One profile is resolved up front for the fan-out (see P2-B), never the
    # library-wide primitives.
    assert deps.created == [("Jennifer Davis", "gid-new-0")]
    assert [p[0] for p in deps.patched] == ["s1", "s2"]


def test_extra_row_outside_the_filter_is_counted_separately():
    _reset()
    rows = _rows() + [_noise_row(), {
        "session_id": "s4", "speaker_key": "SPEAKER_00", "name": "Justin",
        "title": "Elsewhere", "started_at": "2026-08-20T09:00:00"}]
    deps = FakeDeps(rows, stats=_stats())
    plan = _plan(deps, session_ids=["s1", "s2"])
    warning = next(w for w in plan["warnings"] if "left alone" in w)
    assert "2 other speaker label(s)" in warning
    assert "1 outside the current filter" in warning
    assert "1 marked as noise" in warning


# -- P0-2: labels the merge renames without matching the name -----------------

def _merge_profiles_fixture(**kw):
    return FakeDeps(
        _rows(), stats=_stats(),
        profiles=[{"id": "gid-justin", "name": "Justin", "emb_count": 9},
                  {"id": "gid-jen", "name": "Jennifer Davis", "emb_count": 4}],
        linked={"gid-justin": [
            {"session_id": "s1", "speaker_key": "SPEAKER_00", "name": "Justin"},
            {"session_id": "s5", "speaker_key": "SPEAKER_09", "name": "Speaker 3"},
        ]},
        **kw,
    )


_S5_INFO = {"s5": {"title": "Board Sync", "started_at": "2026-08-11T09:00:00"}}


def test_merge_plan_shows_labels_linked_under_other_names():
    _reset()
    deps = _merge_profiles_fixture(session_infos=_S5_INFO)
    plan = _plan(deps)
    assert plan["strategy"] == "merge_profiles"
    pairs = {(s["session_id"], k["speaker_key"], k["current_name"],
              k["linked_via_profile"])
             for s in plan["sessions"] for k in s["keys"]}
    assert ("s5", "SPEAKER_09", "Speaker 3", True) in pairs
    # The already-matching label is not duplicated by the linked lookup.
    assert sum(1 for p in pairs if p[:2] == ("s1", "SPEAKER_00")) == 1


def test_merge_plan_key_count_and_summary_include_linked_labels():
    _reset()
    deps = _merge_profiles_fixture(session_infos=_S5_INFO)
    plan = _plan(deps)
    assert plan["key_count"] == 3          # 2 matched by name, 1 via the profile
    assert plan["session_count"] == 3
    assert plan["linked_via_profile_count"] == 1
    note = 'linked to the "Justin" or "Jennifer Davis" voice profile'
    assert note in plan["summary"]
    assert any(note in w for w in plan["warnings"])
    assert "Board Sync" in plan["summary"]


def test_merge_downgrades_when_a_linked_session_cannot_be_described():
    _reset()
    deps = _merge_profiles_fixture(session_infos={})
    plan = _plan(deps)
    assert plan["strategy"] == "session_patch"
    assert plan["linked_via_profile_count"] == 0
    assert any("could not be read" in w for w in plan["warnings"])


# -- P0-3: the owner's own identity -------------------------------------------

def test_refuses_when_the_source_is_the_me_profile():
    _reset()
    deps = FakeDeps(
        _rows(), stats=_stats(),
        profiles=[{"id": "gid-justin", "name": "Justin", "emb_count": 3}],
        me_id="gid-justin",
    )
    with raises(ValueError) as caught:
        _plan(deps)
    assert "Settings" in str(caught[0])


def test_refuses_when_a_matched_row_is_the_me_key():
    _reset()
    rows = _rows() + [{"session_id": "s6", "speaker_key": "me",
                       "name": "Justin", "title": "Solo",
                       "started_at": "2026-08-15T09:00:00"}]
    deps = FakeDeps(rows, stats=_stats())
    with raises(ValueError) as caught:
        _plan(deps)
    assert "Settings" in str(caught[0])


def test_target_being_the_me_profile_stays_on_session_patch():
    _reset()
    # Renaming a mislabelled speaker to the owner is legitimate, but bulk_link
    # would train their voice profile on that audio, so it is not used.
    deps = FakeDeps(
        _rows(), stats=_stats(),
        profiles=[{"id": "gid-me", "name": "Jennifer Davis", "emb_count": 12}],
        me_id="gid-me",
    )
    plan = _plan(deps)
    assert plan["strategy"] == "session_patch"
    # The warning has to admit that a per-meeting rename still trains the
    # owner's profile, exactly as a manual rename does.
    warning = next(w for w in plan["warnings"] if "own voice profile" in w)
    assert "added to your own voice profile" in warning
    assert "Voice Library cleanup" in warning


def test_me_key_linked_to_the_source_profile_downgrades_instead_of_refusing():
    _reset()
    # The user asked to rename Justin, not to touch their own voice, so the
    # merge is dropped rather than the whole request.
    deps = _merge_profiles_fixture(session_infos=_S5_INFO)
    deps.linked["gid-justin"].append(
        {"session_id": "s7", "speaker_key": "me", "name": "Speaker 1"})
    plan = _plan(deps)
    assert plan["strategy"] == "session_patch"
    assert plan["linked_via_profile_count"] == 0
    assert any("is your own voice; it is left alone" in w
               for w in plan["warnings"])
    # The rows that did match by name are still renamed.
    assert [s["session_id"] for s in plan["sessions"]] == ["s1", "s2"]


# -- P1-1: partial failure in the session_patch loop --------------------------

def test_session_patch_partial_failure_reports_how_far_it_got():
    _reset()
    deps = FakeDeps(_rows() + [_noise_row()], stats=_stats(),
                    patch_fails_on="s2")
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    with raises(speaker_relabel.RelabelRefused) as caught:
        speaker_relabel.apply_plan(token, current_request_id="req-2",
                                   deps=deps.bundle())
    message = str(caught[0])
    assert "Applied 1 of 2 meetings" in message
    assert "Standup" in message
    assert "database is locked" in message
    assert "remaining meetings were not changed" in message
    assert caught[0].applied_session_ids == ["s1"]
    assert [p[0] for p in deps.patched] == ["s1"]


# -- P2-2: the target profile is re-resolved at apply time --------------------

def test_apply_reuses_a_profile_created_during_the_confirmation_window():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    plan = _plan(deps)
    assert plan["strategy"] == "bulk_link" and plan["to_profile_id"] is None
    token = speaker_relabel.mint_token(plan, "req-1")
    # A background auto-link creates the profile while the user is deciding.
    deps.profiles.append({"id": "gid-bg", "name": "Jennifer Davis", "emb_count": 1})
    speaker_relabel.apply_plan(token, current_request_id="req-2",
                               deps=deps.bundle())
    assert deps.created == []
    assert deps.bulk_linked == [("Justin", "gid-bg")]


def test_apply_merge_reuses_the_current_target_profile():
    _reset()
    deps = _merge_profiles_fixture(session_infos=_S5_INFO)
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    speaker_relabel.apply_plan(token, current_request_id="req-2",
                               deps=deps.bundle())
    assert deps.merged == [("gid-jen", "gid-justin")]
    assert deps.bulk_linked == [("Justin", "gid-jen")]


# -- P3-b: Voice Library readiness --------------------------------------------

def test_library_not_ready_forces_session_patch():
    _reset()
    deps = FakeDeps(
        _rows(), stats=_stats(), ready=False,
        profiles=[{"id": "gid-justin", "name": "Justin", "emb_count": 9},
                  {"id": "gid-jen", "name": "Jennifer Davis", "emb_count": 4}],
    )
    plan = _plan(deps)
    assert plan["strategy"] == "session_patch"
    assert plan["to_profile_id"] is None
    assert plan["from_profile_id"] is None
    assert any("Voice Library is not loaded" in w for w in plan["warnings"])


def test_library_not_ready_apply_touches_no_profile():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats(), ready=False)
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    speaker_relabel.apply_plan(token, current_request_id="req-2",
                               deps=deps.bundle())
    assert deps.created == [] and deps.bulk_linked == [] and deps.merged == []
    assert [p[0] for p in deps.patched] == ["s1", "s2"]


# -- P1-A: the apply result names the plan it applied -------------------------

def test_apply_result_carries_the_token():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    result = speaker_relabel.apply_plan(token, current_request_id="req-2",
                                        deps=deps.bundle())
    # The chat card is keyed by token; without it the card never resolves.
    assert result["token"] == token


# -- P1-B: labels already on the target profile under another name ------------

def test_merge_plan_includes_target_linked_labels_under_other_names():
    _reset()
    deps = _merge_profiles_fixture(session_infos={
        "s5": {"title": "Board Sync", "started_at": "2026-08-11T09:00:00"},
        "s8": {"title": "Client Review", "started_at": "2026-08-14T09:00:00"},
    })
    deps.linked["gid-jen"] = [
        # Linked to the target but never renamed: the merge rewrites it.
        {"session_id": "s8", "speaker_key": "SPEAKER_05", "name": "Speaker 2"},
        # Already showing the target name: nothing changes for this one.
        {"session_id": "s8", "speaker_key": "SPEAKER_06", "name": "Jennifer Davis"},
    ]
    plan = _plan(deps)
    assert plan["strategy"] == "merge_profiles"
    entries = {(s["session_id"], k["speaker_key"], k["current_name"],
                k["linked_via_profile"])
               for s in plan["sessions"] for k in s["keys"]}
    assert ("s8", "SPEAKER_05", "Speaker 2", True) in entries
    assert not any(e[:2] == ("s8", "SPEAKER_06") for e in entries)
    assert plan["linked_via_profile_count"] == 2   # s5 source + s8 target
    assert plan["key_count"] == 4
    assert "Client Review" in plan["summary"]


def test_target_linked_labels_are_covered_by_the_summary_refresh():
    _reset()
    deps = _merge_profiles_fixture(session_infos={
        "s5": {"title": "Board Sync", "started_at": "2026-08-11T09:00:00"},
        "s8": {"title": "Client Review", "started_at": "2026-08-14T09:00:00"},
    })
    deps.linked["gid-jen"] = [
        {"session_id": "s8", "speaker_key": "SPEAKER_05", "name": "Speaker 2"},
    ]
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    result = speaker_relabel.apply_plan(token, current_request_id="req-2",
                                        deps=deps.bundle())
    queued_ids, _, _ = deps.summaries[0]
    assert set(queued_ids) == {"s1", "s2", "s5", "s8"}
    assert result["session_count"] == 4


def test_target_linked_label_in_an_unreadable_session_downgrades():
    _reset()
    deps = _merge_profiles_fixture(session_infos=_S5_INFO)
    deps.linked["gid-jen"] = [
        {"session_id": "s8", "speaker_key": "SPEAKER_05", "name": "Speaker 2"},
    ]
    plan = _plan(deps)
    assert plan["strategy"] == "session_patch"
    assert any("could not be read" in w for w in plan["warnings"])


# -- P2-A: coverage is re-verified at apply time ------------------------------

def test_bulk_link_refuses_when_a_new_matching_label_appeared():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    # A meeting finishes and diarizes a new "Justin" while the user decides.
    deps.rows.append({"session_id": "s9", "speaker_key": "SPEAKER_04",
                      "name": "Justin", "title": "Late",
                      "started_at": "2026-09-02T09:00:00"})
    with raises(speaker_relabel.RelabelRefused) as caught:
        speaker_relabel.apply_plan(token, current_request_id="req-2",
                                   deps=deps.bundle())
    assert "appeared since this plan was made" in str(caught[0])
    assert deps.bulk_linked == [] and deps.created == [] and deps.merged == []


def test_merge_refuses_when_a_new_matching_label_appeared():
    _reset()
    deps = _merge_profiles_fixture(session_infos=_S5_INFO)
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    deps.rows.append({"session_id": "s9", "speaker_key": "SPEAKER_04",
                      "name": "Justin", "title": "Late",
                      "started_at": "2026-09-02T09:00:00"})
    with raises(speaker_relabel.RelabelRefused) as caught:
        speaker_relabel.apply_plan(token, current_request_id="req-2",
                                   deps=deps.bundle())
    assert "Nothing was changed" in str(caught[0])
    assert deps.merged == [] and deps.bulk_linked == []


def test_reverify_passes_when_the_library_is_unchanged():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    speaker_relabel.apply_plan(token, current_request_id="req-2",
                               deps=deps.bundle())
    assert deps.bulk_linked == [("Justin", "gid-new-0")]


# -- P2-B: one target profile for the whole session_patch fan-out -------------

def test_session_patch_resolves_the_target_profile_once():
    _reset()
    # Every patched session hands its rename to a background worker that would
    # otherwise create the profile itself, twice if two workers race.
    deps = FakeDeps(_rows() + [_noise_row()], stats=_stats())
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    speaker_relabel.apply_plan(token, current_request_id="req-2",
                               deps=deps.bundle())
    assert len(deps.created) == 1
    assert deps.find_profile_by_name("Jennifer Davis")["id"] == "gid-new-0"


def test_session_patch_reuses_an_existing_target_profile():
    _reset()
    deps = FakeDeps(_rows() + [_noise_row()], stats=_stats(),
                    profiles=[{"id": "gid-jen", "name": "Jennifer Davis",
                               "emb_count": 2}])
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    speaker_relabel.apply_plan(token, current_request_id="req-2",
                               deps=deps.bundle())
    assert deps.created == []


def test_session_patch_creates_no_profile_when_the_target_is_the_me_profile():
    _reset()
    deps = FakeDeps(
        _rows(), stats=_stats(),
        profiles=[{"id": "gid-me", "name": "Jennifer Davis", "emb_count": 12}],
        me_id="gid-me",
    )
    token = speaker_relabel.mint_token(_plan(deps), "req-1")
    speaker_relabel.apply_plan(token, current_request_id="req-2",
                               deps=deps.bundle())
    assert deps.created == []
    assert [p[0] for p in deps.patched] == ["s1", "s2"]


# -- P3-C: colour note on the library-wide rename -----------------------------

def test_bulk_link_warns_about_the_profile_colour():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    plan = _plan(deps)
    assert plan["strategy"] == "bulk_link"
    assert "Renamed labels take the profile's colour." in plan["warnings"]


def test_session_patch_does_not_warn_about_colour():
    _reset()
    deps = FakeDeps(_rows(), stats=_stats())
    plan = _plan(deps, scope="session", session_ids=["s1"])
    assert not any("colour" in w for w in plan["warnings"])


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"OK test_speaker_relabel ({len(tests)} tests)")


def test_profile_only_merge_when_no_label_carries_the_name():
    """An orphan Voice Library profile (samples, no labels) still merges."""
    _reset()
    deps = FakeDeps(
        [r for r in _rows() if speaker_relabel.normalize(r["name"]) != "justin"],
        stats=_stats(),
        profiles=[{"id": "gid-justin", "name": "Justin", "emb_count": 6},
                  {"id": "gid-jen", "name": "Jennifer Davis", "emb_count": 39}],
    )
    plan = _plan(deps)
    assert plan["sessions"] == []
    assert plan["profile_only"] is True
    assert plan["strategy"] == "merge_profiles"
    assert "6 voice sample" in " ".join(plan["warnings"])
    assert "No transcript speaker is named" in plan["summary"]
    token = speaker_relabel.mint_token(plan, "req-1")
    result = speaker_relabel.apply_plan(token, current_request_id="req-2",
                                        deps=deps.bundle())
    assert deps.merged == [("gid-jen", "gid-justin")]
    assert result["applied"] is True
    assert result["session_count"] == 0


def test_profile_only_merge_needs_both_profiles_and_samples():
    _reset()
    base = [r for r in _rows() if speaker_relabel.normalize(r["name"]) != "justin"]
    # No target profile: nothing to merge into, so the plan stays empty.
    deps = FakeDeps(base, stats=_stats(),
                    profiles=[{"id": "gid-justin", "name": "Justin", "emb_count": 6}])
    plan = _plan(deps)
    assert plan["profile_only"] is False and plan["sessions"] == []
    # Source profile without samples: nothing worth merging.
    deps = FakeDeps(base, stats=_stats(),
                    profiles=[{"id": "gid-justin", "name": "Justin", "emb_count": 0},
                              {"id": "gid-jen", "name": "Jennifer Davis", "emb_count": 4}])
    assert _plan(deps)["profile_only"] is False
