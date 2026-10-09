"""Evidence to decisions: who each speaker key is, and what should change.

Deterministic and fast (well under a second for a long meeting), so a
correction re-resolves from cached evidence instantly. Nothing is assumed
about the keys or the voice library: an observation counts for the turn that
was speaking when it was taken, a key is named after the person the screen
showed across several of its turns, and the voice library's name for a key is
a claim the screen can overrule.

The main way a screen misleads is a tile that stays put while different
people talk (a pinned or spotlighted video, a stuck highlight). Its tell is
one name shown as speaking for several different voices; such a person's
tile-based sightings are dropped for the meeting (``non_specific``).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from ai.speaker_detect.names import NameBook
from ai.speaker_detect.observations import CUE_WEIGHT, Observation
from ai.speaker_detect.planner import Timeline

STRONG_CUES = ("banner", "caption")
TILE_CUES = ("main_tile", "border")
# Two turns' voice vectors this similar are one voice (one speaker's turns
# measure about 0.6 to 0.9 against each other, different people under 0.4).
SAME_VOICE = 0.45
# Two people's voices this similar cannot be told apart turn by turn.
CONFUSABLE = 0.75
# Turn-by-turn voice attribution is used only when the people's own turns
# (each left out in turn) are put back with them at least this often.
MIN_RELIABILITY = 0.9
# A key's lines move to another person only with this much of their speech.
MIN_MOVE_SEC = 4.0
# A turn is someone's when its voice is this close to theirs and this much
# closer than to anyone else's.
ATTRIBUTE_FLOOR = 0.65
ATTRIBUTE_MARGIN = 0.15
# A whole key is named by voice only when this much of the speech that has a
# voice could be told (the rest may be someone the screen never showed).
VOICE_COVERAGE = 0.7
# A guess at a speaker nobody has named stays under every autonomy's apply
# threshold (0.7), so it is only ever a suggestion.
GUESS_CEILING = 0.69
_DEFAULT_NAME = re.compile(r"^speaker \d+$", re.I)


def is_default_name(name: str | None, key: str) -> bool:
    n = (name or "").strip()
    return not n or n == key or bool(_DEFAULT_NAME.match(n))


@dataclass
class KeyState:
    """A meeting speaker key as it stands."""
    key: str
    name: str
    global_id: str | None = None
    set_by: str | None = None
    is_noise: bool = False
    talk: float = 0.0
    # The voice library's best match for this key, as the Agent API review
    # pack reports it: {"name", "global_id", "verdict", "similarity"}.
    voice: dict | None = None


@dataclass
class Decision:
    key: str
    person: str | None
    profile_id: str | None
    confidence: float
    turns: int
    share: float
    votes: dict[str, float]
    evidence: list[int]
    reason: str
    # A best guess for a speaker nobody has named, from too little to decide
    # on: only ever offered as a suggestion.
    tentative: bool = False


@dataclass
class Op:
    type: str                        # name | split | move | finding
    keys: list[str]
    name: str | None = None
    global_id: str | None = None
    confidence: float = 0.0
    link: bool = False
    create_profile: bool = False
    train: bool = False
    segment_ids: list[int] = field(default_factory=list)
    new_key: str | None = None
    to_key: str | None = None
    # Lines to teach the voice profile from, when the key's own lines cannot
    # all be trusted to be this person (only the ones whose voice matched).
    train_segments: list[int] = field(default_factory=list)
    replaces: str | None = None
    kind: str | None = None          # for findings
    summary: str = ""
    # Why, in a sentence the meeting page shows under the change.
    reason: str | None = None
    evidence: list[int] = field(default_factory=list)

    @property
    def risk(self) -> str:
        if self.type == "finding":
            return "none"
        if self.train:
            return "train"
        if self.create_profile:
            return "profile"
        if self.link:
            return "link"
        return "session"

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], "", False)} | {
            "type": self.type, "keys": self.keys, "confidence": round(self.confidence, 3),
            "risk": self.risk, "link": self.link, "train": self.train}


@dataclass
class Resolution:
    decisions: dict[str, Decision]
    ops: list[Op]
    non_specific: set[str]
    looked: int                     # observations that counted
    meeting_seen: bool
    misreads: int = 0               # sightings set aside because the turn's voice disagreed
    # key -> person -> looks, before misreads and untrusted tiles were set
    # aside: what the screen showed, for the page to say even when nothing
    # was decided from it.
    seen: dict[str, dict[str, int]] = field(default_factory=dict)


def _calibrated(conf: float) -> float:
    # The model's own confidence is a weak signal; compress it toward the middle.
    return 0.5 + 0.5 * max(0.0, min(1.0, conf))


@dataclass
class Tally:
    votes: dict        # key -> person -> weighted mass
    turns: dict        # key -> person -> set of turn idx
    looks: dict        # key -> person -> number of sightings
    evidence: dict     # key -> person -> [observation ids]
    strong: dict       # key -> person -> mass from banners and captions
    counted: int       # observations that counted


def tally(tl: Timeline, observations: list[Observation], *, owner: str | None,
          book: NameBook, skip: dict[str, set[int]] | None = None) -> Tally:
    """Weighted sightings per key: each counts for the turn that was
    speaking when it was taken. ``skip`` (person -> turn idxs) drops the
    sightings the turn's own voice contradicts (see ``vet_by_voice``)."""
    skip = skip or {}
    votes: dict = defaultdict(lambda: defaultdict(float))
    turns: dict = defaultdict(lambda: defaultdict(set))
    looks: dict = defaultdict(lambda: defaultdict(int))
    evidence: dict = defaultdict(lambda: defaultdict(list))
    strong: dict = defaultdict(lambda: defaultdict(float))
    counted = 0
    prev_end: dict[int, float] = {}
    ordered = sorted(tl.turns, key=lambda t: t.start)
    for i, tr in enumerate(ordered):
        prev_end[tr.idx] = ordered[i - 1].end if i and ordered[i - 1].key != tr.key else -1e9
    for o in observations:
        if not o.meeting_visible or not o.speaking:
            continue
        tr = tl.turn_at(o.t)
        if tr is None or tl.crowded(o.t, tr):
            continue
        people = []
        for s in o.speaking:
            if s.self_view:
                continue
            person = s.person
            if person is None:
                person, gid, known = book.resolve(s.label)
                s.person, s.profile_id, s.known = person, gid, known
            if not person or (owner and book.same(person, owner)):
                continue
            if tr.idx in skip.get(person, ()):
                continue
            people.append(s)
        if not people:
            continue
        counted += 1
        lag = 0.3 if (o.t - tr.start < 1.0 and tr.start - prev_end.get(tr.idx, -1e9) < 1.0) else 1.0
        for s in people:
            w = _calibrated(s.confidence) * CUE_WEIGHT.get(s.cue, 0.3) / len(people) * lag
            if o.pinned and s.cue in TILE_CUES:
                w *= 0.4
            if o.legibility == "poor":
                w *= 0.6
            votes[tr.key][s.person] += w
            turns[tr.key][s.person].add(tr.idx)
            looks[tr.key][s.person] += 1
            if o.id is not None:
                evidence[tr.key][s.person].append(o.id)
            if s.cue in STRONG_CUES:
                strong[tr.key][s.person] += w
    return Tally(votes, turns, looks, evidence, strong, counted)


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    return v / (float(np.linalg.norm(v)) or 1.0)


@dataclass
class VoiceModel:
    """What the turns' own voices say about the screen's sightings."""
    voices: dict[str, np.ndarray]       # person -> their voice, from turns the screen showed them in
    kept: dict[str, list[int]]          # person -> those turns
    skip: dict[str, set[int]]           # person -> sighted turns in another voice (misreads)
    lying: set[str]                     # shown for several voices, none of them dominant
    vetted: set[str]                    # everyone the check could run for
    reliability: float | None = None    # leave-one-out: kept turns whose voice picks the person seen

    @property
    def usable(self) -> bool:
        """Turns can be told apart by voice in this meeting."""
        return len(self.voices) >= 2 and (self.reliability or 0.0) >= MIN_RELIABILITY


def voice_model(tal: Tally, turn_vectors: dict, *, min_turns: int = 3,
                dominant: float = 0.7) -> VoiceModel:
    """Each person's sightings against the voices of the turns they were seen in.

    Per person with ``min_turns`` or more sighted turns that carry a voice:
    the turn most like the others stands for theirs, and a turn is the same
    voice at SAME_VOICE or above. When that voice holds ``dominant`` of the
    turns, the rest are misreads (skipped) and the person's voice is the mean
    of the rest; when no voice does, the person's tile is shown for several
    people and is not to be believed (``lying``). Two people who sound alike
    (CONFUSABLE) give no voice to tell apart."""
    by_person: dict[str, set[int]] = defaultdict(set)
    for _k, pt in tal.turns.items():
        for p, idxs in pt.items():
            by_person[p] |= idxs
    vm = VoiceModel({}, {}, {}, set(), set())
    for p, idxs in by_person.items():
        have = sorted(i for i in idxs if turn_vectors.get(i) is not None)
        if len(have) < min_turns:
            continue
        vm.vetted.add(p)
        vs = np.stack([_unit(turn_vectors[i]) for i in have])
        sims = vs @ vs.T
        medoid = int(np.argmax(sims.sum(axis=1)))
        same = sims[medoid] >= SAME_VOICE
        if same.sum() / len(have) < dominant:
            vm.lying.add(p)
            continue
        out = {have[j] for j in range(len(have)) if not same[j]}
        if out:
            vm.skip[p] = out
        vm.kept[p] = [have[j] for j in range(len(have)) if same[j]]
        vm.voices[p] = _unit(vs[same].mean(axis=0))
    names = sorted(vm.voices)
    alike: set[str] = set()
    for i, p in enumerate(names):
        for q in names[i + 1:]:
            if float(vm.voices[p] @ vm.voices[q]) >= CONFUSABLE:
                alike |= {p, q}
    for p in alike:
        vm.voices.pop(p, None)
    vm.reliability = _leave_one_out(vm, turn_vectors)
    return vm


def _leave_one_out(vm: VoiceModel, turn_vectors: dict) -> float | None:
    """Share of the people's own turns that their voices, each rebuilt
    without the turn, assign back to them."""
    if len(vm.voices) < 2:
        return None
    sums = {p: np.sum([_unit(turn_vectors[i]) for i in vm.kept[p]], axis=0) for p in vm.voices}
    right = total = 0
    for p in vm.voices:
        n = len(vm.kept[p])
        if n < 2:
            continue
        for i in vm.kept[p]:
            v = _unit(turn_vectors[i])
            best, best_sim = None, -2.0
            for q in vm.voices:
                c = _unit(sums[q] - v) if q == p else _unit(sums[q])
                s = float(v @ c)
                if s > best_sim:
                    best, best_sim = q, s
            right += best == p
            total += 1
    return right / total if total else None


def attribute(turn_ids, turn_vectors: dict, voices: dict[str, np.ndarray], *,
              floor: float = ATTRIBUTE_FLOOR,
              margin: float = ATTRIBUTE_MARGIN) -> dict[int, tuple[str, float, float]]:
    """Turn idx -> (person, similarity, lead over the next person) for every
    turn whose voice is clearly one of ``voices``. The floor is well above
    where different people meet: someone the screen never showed has no voice
    here, and their turns must stay unattributed rather than go to whoever
    sounds least unlike them."""
    names = list(voices)
    if len(names) < 2:
        return {}
    mat = np.stack([voices[n] for n in names])
    out = {}
    for i in turn_ids:
        v = turn_vectors.get(i)
        if v is None:
            continue
        s = mat @ _unit(v)
        order = np.argsort(-s)
        best, second = float(s[order[0]]), float(s[order[1]])
        if best >= floor and best - second >= margin:
            out[i] = (names[order[0]], best, best - second)
    return out


def _distinct_voices(a: KeyState, b: KeyState, centroids: dict | None) -> bool | None:
    """True when two keys are clearly different people by voice, False when
    clearly the same, None when we cannot tell."""
    if centroids and a.key in centroids and b.key in centroids:
        va, vb = centroids[a.key], centroids[b.key]
        if va is not None and vb is not None:
            sim = float(np.dot(va, vb) / ((np.linalg.norm(va) * np.linalg.norm(vb)) or 1.0))
            if sim < 0.45:
                return True
            if sim >= 0.70:
                return False
    va, vb = a.voice or {}, b.voice or {}
    if (va.get("verdict") == "strong" and vb.get("verdict") == "strong"
            and va.get("name") and vb.get("name") and va["name"] != vb["name"]):
        return True
    return None


def non_specific_people(votes, turns, keys: dict[str, KeyState], centroids: dict | None,
                        vetted: set[str] | frozenset = frozenset()) -> tuple[set[str], set[str]]:
    """(people shown speaking for clearly different voices, people spread
    over several keys where voices cannot be told apart).

    Only keys the screen puts on one person count: a key that is two people
    has a mean voice that may be either of them, so comparing it says nothing
    about a tile (it is split instead). A pinned or stuck tile still shows:
    it puts one person on every key it covers."""
    by_person: dict[str, list[str]] = defaultdict(list)
    for k, pv in votes.items():
        tot = sum(pv.values()) or 1.0
        for p, v in pv.items():
            if len(turns[k][p]) >= 2 and v / tot >= 0.85:
                by_person[p].append(k)
    lying, unsure = set(), set()
    for p, ks in by_person.items():
        if len(ks) < 2 or p in vetted:
            continue
        verdicts = [_distinct_voices(keys[a], keys[b], centroids)
                    for i, a in enumerate(ks) for b in ks[i + 1:] if a in keys and b in keys]
        if any(v is True for v in verdicts):
            lying.add(p)
        elif len(ks) >= 3 and not any(v is False for v in verdicts):
            unsure.add(p)
    return lying, unsure


def mixed_pair(person_votes: dict, person_turns: dict,
               lying: set[str] | frozenset = frozenset()) -> tuple[str, str] | None:
    """The two people one key's sightings divide between, when both were
    shown in at least 4 of its turns and each holds a fifth of the votes."""
    tot = sum(person_votes.values()) or 1.0
    pair = [p for p, v in sorted(person_votes.items(), key=lambda x: -x[1])
            if len(person_turns.get(p, ())) >= 4 and v / tot >= 0.2 and p not in lying]
    return (pair[0], pair[1]) if len(pair) >= 2 else None


def suspect_keys(tal: Tally, keys: dict[str, KeyState], book: NameBook) -> set[str]:
    """Keys worth hearing turn by turn: the screen showed a second person in
    two or more of their turns, or showed someone other than the name the key
    carries or the voice library's match for it, or never looked at them."""
    out = set()
    for k, ks in keys.items():
        pv = tal.votes.get(k) or {}
        if not pv:
            out.add(k)
            continue
        tot = sum(pv.values()) or 1.0
        ranked = sorted(pv, key=pv.get, reverse=True)
        top = ranked[0]
        if any(len(tal.turns[k][p]) >= 2 and pv[p] / tot >= 0.1 for p in ranked[1:]):
            out.add(k)
            continue
        voice = ks.voice or {}
        if voice.get("name") and not book.same(voice["name"], top):
            out.add(k)
        elif not is_default_name(ks.name, k) and not book.same(ks.name, top):
            out.add(k)
    return out


def next_key(keys, taken=()) -> str:
    """A fresh ``Speaker N`` past every key in use (the meeting's keys plus
    any label rows without lines)."""
    n = 0
    for k in list(keys) + list(taken):
        m = re.match(r"^Speaker (\d+)$", k)
        if m:
            n = max(n, int(m.group(1)))
    return f"Speaker {n + 1}"


def _move_confidence(items: list[tuple[int, float, float]], reliability: float | None) -> float:
    """How sure a move of these turns is: more turns and clearer leads over
    the next voice are surer, and a meeting whose voices separate less
    cleanly costs every move."""
    n = len(items)
    lead = sum(m for _i, _s, m in items) / n
    conf = 0.55 + 0.25 * (1.0 - math.exp(-n / 2.0)) + 0.2 * min(1.0, lead / 0.3)
    conf -= max(0.0, 0.97 - (reliability or 0.0))
    return max(0.0, min(0.95, conf))


def resolve(tl: Timeline, observations: list[Observation], keys: dict[str, KeyState],
            book: NameBook, *, owner: str | None = None, constraints: list[dict] | None = None,
            recheck_user_labels: bool = False, trust_screen: bool = False,
            centroids: dict | None = None, turn_vectors: dict | None = None,
            taken: set[str] | None = None) -> Resolution:
    constraints = constraints or []
    tal = tally(tl, observations, owner=owner, book=book)
    sightings = {k: dict(pv) for k, pv in tal.looks.items() if pv}
    vm: VoiceModel | None = None
    misreads = 0
    if turn_vectors:
        vm = voice_model(tal, turn_vectors)
        if vm.skip:
            misreads = sum(len(v) for v in vm.skip.values())
            tal = tally(tl, observations, owner=owner, book=book, skip=vm.skip)
    votes, turns, looks, evidence, strong = tal.votes, tal.turns, tal.looks, tal.evidence, \
        tal.strong
    counted = tal.counted
    lying, unsure = non_specific_people(votes, turns, keys, centroids,
                                        vm.vetted if vm else frozenset())
    if vm:
        lying |= vm.lying
    # A lying tile's sightings stop counting; banners and captions name the
    # talker in text and still stand.
    for k in list(votes):
        for p in list(votes[k]):
            if p in lying:
                votes[k][p] = strong[k].get(p, 0.0)
                if votes[k][p] <= 0:
                    del votes[k][p]

    protected = {c["subject"].get("key") for c in constraints
                 if c.get("kind") == "protect" and c.get("subject", {}).get("key")}
    protected_names = {c["subject"].get("name") for c in constraints
                       if c.get("kind") == "protect" and c.get("subject", {}).get("name")}
    forbidden: dict[str, set[str]] = defaultdict(set)
    forced: dict[str, str] = {}
    for c in constraints:
        k = c.get("subject", {}).get("key")
        if not k:
            continue
        if c.get("kind") == "is_not" and c.get("value"):
            forbidden[k].add(book.resolve(c["value"])[0] or c["value"])
        elif c.get("kind") == "is" and c.get("value"):
            forced[k] = book.resolve(c["value"])[0] or c["value"]

    # Who each key's turns sound like, once the meeting's voices (learned from
    # the turns the screen showed each person in) tell people apart cleanly.
    # This is what catches the diarizer putting two people in one key, and
    # names keys the screen never looked at.
    length = {t.idx: t.length for t in tl.turns}
    by_idx = {t.idx: t for t in tl.turns}
    key_turns: dict[str, list[int]] = defaultdict(list)
    for t in tl.turns:
        key_turns[t.key].append(t.idx)
    heard: dict[str, dict[int, tuple[str, float, float]]] = {}
    if vm and vm.usable:
        for k in keys:
            if k in forced or k in protected:
                continue
            att = {i: a for i, a in attribute(key_turns[k], turn_vectors, vm.voices).items()
                   if a[0] not in forbidden[k] and a[0] not in lying}
            if att:
                heard[k] = att
    seen_as: dict[str, list[int]] = defaultdict(list)
    for k in evidence:
        for p, ids in evidence[k].items():
            seen_as[p] += ids

    decisions: dict[str, Decision] = {}
    for k, ks in keys.items():
        if k in forced:
            p = forced[k]
            decisions[k] = Decision(k, p, book.people.get(p), 1.0, 0, 1.0, {}, [], "you said so")
            continue
        pv = {p: v for p, v in votes.get(k, {}).items() if p not in forbidden[k]}
        att = heard.get(k, {})
        vt: dict[str, float] = defaultdict(float)
        for i, (p, _s, _m) in att.items():
            vt[p] += length[i]
        p_v = max(vt, key=vt.get) if vt else None
        told = sum(vt.values())
        voiced = sum(length[i] for i in key_turns[k] if turn_vectors and i in turn_vectors)
        coverage = told / voiced if voiced else 0.0
        s_v = vt[p_v] / told if p_v else 0.0
        # Judged on what stays: other people's turns leave with them.
        leaving_talk = sum(s for q, s in vt.items() if q != p_v and s >= MIN_MOVE_SEC)
        s_after = vt[p_v] / (told - leaving_talk) if p_v and told > leaving_talk else 0.0
        n_v = sum(1 for a in att.values() if a[0] == p_v)
        p_s = max(pv, key=pv.get) if pv else None
        if p_v and p_v != p_s and s_v >= 0.5 and s_after >= 0.8 and n_v >= 2 and \
                coverage >= VOICE_COVERAGE and told >= 0.3 * ks.talk:
            # Most of the key sounds like someone the screen showed elsewhere;
            # whoever the screen showed in it leaves with their own turns.
            _name, gid, known = book.resolve(p_v)
            conf = 0.5 + 0.45 * s_after * min(1.0, n_v / 4) + (0.03 if known else -0.05)
            conf -= max(0.0, 0.97 - (vm.reliability or 0.0))
            decisions[k] = Decision(
                k, p_v, gid, max(0.0, min(0.95, conf)), n_v, s_v, dict(vt), seen_as[p_v][:12],
                f"{n_v} of its turns sound like {p_v} ({s_v:.0%} of what could be told by "
                f"voice), whom the screen showed in {len(vm.kept.get(p_v, ()))} turns of the "
                f"meeting")
            continue
        if not pv:
            continue
        tot = sum(pv.values())
        p, mass = p_s, pv[p_s]
        share = mass / tot if tot else 0.0
        n = len(turns[k][p])
        voice = ks.voice or {}
        voice_agrees = bool(voice.get("name")) and book.same(voice["name"], p) and \
            voice.get("verdict") in ("strong", "clear")
        voice_against = voice.get("verdict") == "strong" and voice.get("name") and \
            not book.same(voice["name"], p)
        if att:
            # The turns' own voices beat the key's mean voice, which is a
            # blend when the key holds two people.
            voice_agrees, voice_against = p_v == p, False
            if p_v == p and coverage >= 0.5:
                # Whoever else the screen saw in it leaves with their turns.
                share = max(share, s_after)
        has_strong = strong[k].get(p, 0.0) > 0
        seen = looks[k].get(p, 0)
        enough = (n >= 3 or (n >= 2 and (seen >= 4 or voice_agrees or has_strong))
                  or (n >= 1 and ks.talk < 10.0 and share >= 0.99 and (voice_agrees or has_strong)))
        if share < 0.6 or not enough:
            decisions[k] = Decision(k, None, None, 0.0, n, share, dict(pv),
                                    evidence[k][p], "not enough agreement yet")
            continue
        _name, gid, known = book.resolve(p)
        # Strength of evidence (distinct turns count most; extra looks inside
        # a turn a little) and agreement among the looks, each bounded.
        strength = 1.0 - math.exp(-(n + 0.3 * max(seen - n, 0)) / 2.5)
        agreement = (share - 0.5) / 0.5
        conf = 0.45 + 0.35 * strength + 0.2 * agreement
        conf += 0.08 if voice_agrees else 0.0
        conf -= 0.0 if trust_screen else (0.15 if voice_against else 0.0)
        conf += 0.03 if known else -0.05
        if p in unsure:
            conf = min(conf, 0.75)
        conf = max(0.0, min(0.99, conf))
        why = f"the screen showed {p} in {n} of its turns"
        if att and p_v == p:
            why += f", and {s_after:.0%} of what stays in it sounds like {p}"
        elif voice_agrees:
            why += f" ({share:.0%} of looks), and the voice library agrees"
        else:
            why += f" ({share:.0%} of looks)"
        decisions[k] = Decision(k, p, gid, conf, n, share, dict(pv), evidence[k][p], why)

    # Fragments: a short key whose every look shows someone already settled
    # elsewhere is a piece of that person (the diarizer splits one voice into
    # many keys far more often than it merges two). Weaker than a key that
    # earned its own name, so it waits for a look unless voice agrees.
    settled = {d.person for d in decisions.values() if d.person and d.confidence >= 0.85}
    for k, d in list(decisions.items()):
        if d.person or not d.votes:
            continue
        p, mass = max(d.votes.items(), key=lambda x: x[1])
        if p not in settled or mass / (sum(d.votes.values()) or 1.0) < 0.99 or d.turns < 1:
            continue
        ks = keys[k]
        voice = ks.voice or {}
        agrees = bool(voice.get("name")) and book.same(voice["name"], p)
        if looks[k].get(p, 0) < 2 and not agrees:
            continue          # one look at a fragment is a coin toss on timing
        conf = min(0.9, 0.72 + 0.06 * d.turns + (0.08 if agrees else 0.0))
        _name, gid, _known = book.resolve(p)
        decisions[k] = Decision(k, p, gid, conf, d.turns, 1.0, d.votes, d.evidence,
                                f"every look at this fragment showed {p}, who is settled "
                                f"elsewhere in the meeting")

    # Speakers nobody has named yet, with too little to decide on (a look or
    # two, or lines too short to hear): the screen's best guess is still worth
    # offering, but only as a suggestion that teaches the voice library nothing.
    voiced = {t.key for t in tl.turns if turn_vectors and t.idx in turn_vectors}
    for k, d in list(decisions.items()):
        ks = keys[k]
        if d.person or not d.votes or k in forced or k in protected or ks.is_noise \
                or not is_default_name(ks.name, k):
            continue
        p, mass = max(d.votes.items(), key=lambda x: x[1])
        share = mass / (sum(d.votes.values()) or 1.0)
        if share < 0.75 or (owner and book.same(p, owner)):
            continue
        att = heard.get(k) or {}
        if att and not any(book.same(a[0], p) for a in att.values()):
            continue          # its own voice says someone else
        voice = ks.voice or {}
        if voice.get("verdict") == "strong" and voice.get("name") and \
                not book.same(voice["name"], p):
            continue
        agrees = bool(voice.get("name")) and book.same(voice["name"], p)
        n_looks = looks[k].get(p, 0)
        conf = 0.55 + 0.04 * min(d.turns, 3) + (0.04 if p in settled else 0.0) + \
            (0.04 if agrees else 0.0)
        why = (f"the screen showed {p} speaking in {n_looks} look{'' if n_looks == 1 else 's'} "
               f"at it, too few to be sure")
        if k not in voiced:
            why += ", and its lines are too short to check by voice"
        elif agrees:
            why += ", and the voice library leans the same way"
        _name, gid, _known = book.resolve(p)
        decisions[k] = Decision(k, p, gid, min(conf, GUESS_CEILING), d.turns, share, d.votes,
                                d.evidence, why, tentative=True)

    ops: list[Op] = []
    # Name ops, grouped by person so fragments of one voice become one change
    # (a guess apart from the rest, so it never drags a sure name down to one).
    grouped: dict[tuple[str, bool], list[Decision]] = defaultdict(list)
    for k, d in decisions.items():
        ks = keys[k]
        if not d.person or k in protected or ks.name in protected_names:
            continue
        if owner and book.same(d.person, owner):
            ops.append(Op("finding", [k], kind="echo", confidence=d.confidence,
                          summary=f"{k} sounds like your own voice coming back through the call "
                                  f"audio", evidence=d.evidence))
            continue
        if book.same(ks.name, d.person):
            if ks.global_id is None and d.profile_id:
                grouped[(d.person, d.tentative)].append(d)   # named but unlinked: link it
            continue
        default = is_default_name(ks.name, k)
        if k in forced:
            # The user said who this is: a change of its own, at full
            # confidence, whatever the key is called now.
            ops.append(Op("name", [k], name=d.person, global_id=d.profile_id, confidence=1.0,
                          link=True, create_profile=d.profile_id is None,
                          summary=f"Named {k} {d.person} (you said so)"))
            continue
        if not default and ks.set_by == "user" and not recheck_user_labels:
            ops.append(Op("finding", [k], name=d.person, kind="disagrees_with_you",
                          confidence=d.confidence, evidence=d.evidence,
                          summary=f"The screen suggests {ks.name} ({k}) is {d.person}"))
            continue
        if not default:
            # Overruling an existing name needs more than naming a blank one.
            if d.turns < 4 and not strong[k].get(d.person):
                continue
            if ks.set_by is None and d.confidence < 0.9:
                d.confidence = min(d.confidence, 0.85)
        if ks.is_noise and d.turns < 4:
            continue
        grouped[(d.person, d.tentative)].append(d)

    for (person, tentative), ds in grouped.items():
        conf = min(d.confidence for d in ds) if len(ds) > 1 else ds[0].confidence
        gid = next((d.profile_id for d in ds if d.profile_id), None)
        replaced = sorted({keys[d.key].name for d in ds
                           if not is_default_name(keys[d.key].name, d.key)
                           and not book.same(keys[d.key].name, person)})
        ks_names = ", ".join(d.key for d in ds)
        summary = f"Named {ks_names} {person}"
        if replaced:
            summary += f" (was {', '.join(replaced)})"
        # What teaches the profile: in a key heard turn by turn, only the
        # turns in this person's voice; a key the screen saw two people in but
        # the voices could not sort out teaches nothing, and nor does a guess.
        train, train_segs = True, []
        if tentative:
            train = False
        elif any(d.key in heard for d in ds):
            mine = sorted((i for d in ds for i, a in heard.get(d.key, {}).items()
                           if book.same(a[0], person)), key=lambda i: -length[i])
            train_segs = [s for i in mine for s in by_idx[i].seg_ids][:12]
            train = bool(train_segs)
        elif any(mixed_pair(votes.get(d.key, {}), turns[d.key], lying) for d in ds):
            train = False
        lead = max(ds, key=lambda d: d.confidence)
        reason = lead.reason if len(ds) == 1 else f"{lead.key}: {lead.reason}"
        ops.append(Op("name", [d.key for d in ds], name=person, global_id=gid, confidence=conf,
                      link=True, create_profile=gid is None, train=train,
                      train_segments=train_segs, replaces=", ".join(replaced) or None,
                      summary=summary, reason=reason,
                      evidence=[e for d in ds for e in d.evidence][:24]))

    # Lines in another person's voice move to that person: to the key that is
    # them in this meeting, or to a new key when none is. One change per
    # person, however many keys their lines are scattered over.
    leaving: dict[str, list[tuple[str, int, float, float]]] = defaultdict(list)
    for k, att in heard.items():
        d = decisions.get(k)
        if not d or not d.person or keys[k].name in protected_names:
            continue
        for i, (q, s, m) in att.items():
            if q != d.person:
                leaving[q].append((k, i, s, m))
    for q, items in leaving.items():
        if sum(length[i] for _k, i, _s, _m in items) < MIN_MOVE_SEC:
            continue
        if owner and book.same(q, owner):
            continue
        sources = sorted({k for k, _i, _s, _m in items})
        homes = [k2 for k2, d2 in decisions.items()
                 if d2.person and book.same(d2.person, q) and k2 not in sources]
        homes += [k2 for k2, ks2 in keys.items() if k2 not in decisions and k2 not in sources
                  and book.same(ks2.name, q)]
        home = max(homes, key=lambda k2: keys[k2].talk) if homes else None
        turn_ids = sorted(i for _k, i, _s, _m in items)
        seg_ids = [s for i in turn_ids for s in by_idx[i].seg_ids]
        conf = _move_confidence([(i, s, m) for _k, i, s, m in items], vm.reliability)
        if any(keys[k].set_by == "user" and not recheck_user_labels
               and not is_default_name(keys[k].name, k) for k in sources):
            conf = min(conf, 0.84)          # lines you labelled move only if you agree
        _n, gid_q, _known = book.resolve(q)
        secs = sum(length[i] for i in turn_ids)
        where = ", ".join(sources)
        ev = seen_as[q][:12]
        match = sum(s for _k, _i, s, _m in items) / len(items)
        turns_sound = "one turn sounds" if len(turn_ids) == 1 else f"{len(turn_ids)} turns sound"
        why = (f"{turns_sound} like {q} ({match:.0%} voice match), whom the screen showed in "
               f"{len(vm.kept.get(q, ()))} turns of the meeting")
        if home:
            ops.append(Op("move", sources, name=q, global_id=gid_q, to_key=home,
                          segment_ids=seg_ids, confidence=conf, evidence=ev, reason=why,
                          summary=f"Moved {len(seg_ids)} lines ({secs:.0f} s) in {q}'s voice "
                                  f"from {where} to {q}"))
        else:
            new_key = next_key(keys, taken or ())
            keys[new_key] = KeyState(new_key, q)
            ops.append(Op("split", sources, name=q, global_id=gid_q, new_key=new_key,
                          segment_ids=seg_ids, confidence=conf, link=True,
                          create_profile=gid_q is None, evidence=ev, reason=why,
                          summary=f"Split {len(seg_ids)} lines ({secs:.0f} s) in {q}'s voice "
                                  f"off {where} as {q}"))

    # Two people the screen saw in one key that the voices could not sort out.
    for k, pv in votes.items():
        if k not in keys or k in protected or k in heard:
            continue
        pair = mixed_pair(pv, turns[k], lying)
        if pair:
            a, b = pair
            ops.append(Op("finding", [k], kind="two_people", name=f"{a} and {b}",
                          confidence=0.6, evidence=evidence[k][a][:6] + evidence[k][b][:6],
                          summary=f"{k} looks like two people: {a} and {b}"))

    visible = any(o.meeting_visible for o in observations)
    if observations and not visible:
        ops.append(Op("finding", [], kind="voice_only", confidence=1.0,
                      summary="The meeting window wasn't on the recorded screen, so speakers "
                              "were named from their voices only"))
    return Resolution(decisions, ops, lying, counted, visible, misreads, sightings)
