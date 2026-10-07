"""Short-reply speaker folding and the source-aware speaker count.

Short replies ("mm-hmm", "yeah") carry too little voice for the diarizer, so
it files them as extra people; absorb_short_replies folds them back into the
closest real speaker. desktop_speaker_params takes Me out of the count for the
desktop track, since Me is only ever on the mic track.
"""
import numpy as np

from ml.batch_transcriber import (
    absorb_short_replies,
    desktop_speaker_params,
    renumber_speakers,
)


def _turns(speaker, count, length, start=0.0, gap=10.0):
    return [(speaker, start + i * gap, start + i * gap + length) for i in range(count)]


def test_backchannel_cluster_folds_into_nearest_centroid():
    segs = (_turns("Speaker 1", 60, 12.0)           # 720 s, the far-side talker
            + _turns("Speaker 2", 30, 9.0, start=3)  # 270 s, a second real person
            + _turns("Speaker 3", 40, 0.6, start=5))  # 24 s of "mm-hmm"
    centroids = {
        "Speaker 1": np.array([1.0, 0.0]),
        "Speaker 2": np.array([0.0, 1.0]),
        "Speaker 3": np.array([0.1, 0.9]),  # sounds like Speaker 2
    }
    out, mapping = absorb_short_replies(segs, centroids)
    assert mapping == {"Speaker 3": "Speaker 2"}
    assert {s for s, _a, _b in out} == {"Speaker 1", "Speaker 2"}


def test_one_on_one_collapses_to_one_far_side_speaker():
    # The 2026-10-01 meeting: one far-side voice plus four reply fragments.
    segs = (_turns("Speaker 1", 400, 4.0)
            + _turns("Speaker 2", 40, 0.8, start=1)
            + _turns("Speaker 3", 116, 0.7, start=2)
            + _turns("Speaker 4", 17, 1.0, start=3)
            + _turns("Speaker 5", 12, 0.8, start=4))
    out, mapping = absorb_short_replies(segs)  # no centroids: fold to the main talker
    assert set(mapping.values()) == {"Speaker 1"}
    assert {s for s, _a, _b in out} == {"Speaker 1"}


def test_speaker_with_a_real_turn_is_kept():
    # Mostly short replies, but one 30 s answer: a real participant.
    segs = (_turns("Speaker 1", 100, 11.0)
            + _turns("Speaker 2", 12, 1.0, start=2)
            + [("Speaker 2", 2000.0, 2030.0)])
    _out, mapping = absorb_short_replies(segs)
    assert mapping == {}


def test_large_share_is_kept_even_with_short_turns():
    segs = _turns("Speaker 1", 100, 16.0) + _turns("Speaker 2", 450, 0.9, start=1)
    _out, mapping = absorb_short_replies(segs)
    assert mapping == {}  # 405 s is a quarter of the main speaker: real


def test_tiny_total_folds_regardless_of_turn_length():
    segs = _turns("Speaker 1", 50, 10.0) + [("Speaker 2", 3.0, 7.0)]
    _out, mapping = absorb_short_replies(segs)
    assert mapping == {"Speaker 2": "Speaker 1"}


def test_main_speaker_and_single_speaker_never_fold():
    assert absorb_short_replies(_turns("Speaker 1", 5, 0.5)) == (_turns("Speaker 1", 5, 0.5), {})
    segs = _turns("Speaker 1", 3, 0.5) + _turns("Speaker 2", 2, 0.4, start=1)
    _out, mapping = absorb_short_replies(segs)
    assert "Speaker 1" not in mapping


def test_zero_centroid_falls_back_to_main_speaker():
    segs = (_turns("Speaker 1", 60, 12.0) + _turns("Speaker 2", 30, 9.0, start=3)
            + _turns("Speaker 3", 40, 0.6, start=5))
    centroids = {"Speaker 1": np.array([1.0, 0.0]), "Speaker 2": np.array([0.0, 1.0]),
                 "Speaker 3": np.zeros(2)}
    _out, mapping = absorb_short_replies(segs, centroids)
    assert mapping == {"Speaker 3": "Speaker 1"}


def test_a_voice_that_matches_nobody_is_kept():
    # Someone else who said one sentence: under 5 s in total, but their voice
    # is unlike every real speaker, so it is a person, not a short reply
    # (review of PR 1086, 2026-10-07).
    segs = _turns("Speaker 1", 50, 10.0) + [("Speaker 2", 300.0, 304.0)]
    centroids = {"Speaker 1": np.array([1.0, 0.0]), "Speaker 2": np.array([-0.2, 1.0])}
    _out, mapping = absorb_short_replies(segs, centroids)
    assert mapping == {}


def test_folding_never_goes_below_min_speakers():
    segs = (_turns("Speaker 1", 400, 4.0)
            + _turns("Speaker 2", 40, 0.8, start=1)
            + _turns("Speaker 3", 116, 0.7, start=2)
            + _turns("Speaker 4", 17, 1.0, start=3))
    _out, mapping = absorb_short_replies(segs, min_speakers=3)
    # One fold allowed (4 -> 3), and it takes the smallest talker.
    assert mapping == {"Speaker 4": "Speaker 1"}
    _out, mapping = absorb_short_replies(segs, min_speakers=4)
    assert mapping == {}


def test_folding_is_opt_in():
    from pathlib import Path

    from capture_audio.params import REANALYSIS_DEFAULTS
    assert REANALYSIS_DEFAULTS["reanalysis_absorb_short_replies"]["value"] == 0
    src = (Path(__file__).parents[1] / "ml/batch_transcriber.py").read_text(encoding="utf-8")
    assert 'params.get("reanalysis_absorb_short_replies", 0)' in src


def test_renumber_closes_gaps_by_first_appearance():
    segs = [("Speaker 4", 5.0, 6.0), ("Speaker 1", 0.0, 1.0), ("Speaker 4", 9.0, 9.5)]
    assert renumber_speakers(segs) == [
        ("Speaker 1", 0.0, 1.0), ("Speaker 2", 5.0, 6.0), ("Speaker 2", 9.0, 9.5)]


def test_desktop_count_excludes_me():
    params = {"reanalysis_num_speakers": 0, "reanalysis_min_speakers": 0,
              "reanalysis_max_speakers": 2, "other": "x"}
    out = desktop_speaker_params(params)
    assert out["reanalysis_max_speakers"] == 1  # a 1:1: only the other person
    assert out["reanalysis_num_speakers"] == 0 and out["reanalysis_min_speakers"] == 0
    assert out["other"] == "x"
    assert params["reanalysis_max_speakers"] == 2  # caller's dict untouched


def test_desktop_count_never_below_one():
    out = desktop_speaker_params({"reanalysis_num_speakers": 1})
    assert out["reanalysis_num_speakers"] == 1
