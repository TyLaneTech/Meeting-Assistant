"""Which moments of the screen recording to look at.

Diarized turns say where people talked; they are a starting point for where
to look, never a statement of who talked. Three kinds of moments:

- scouts: a few full frames spread over the meeting, to learn the app, the
  layout, where the participants are and who is on screen;
- anchors: moments inside each speaker key's turns, breadth first (every key
  gets a look before any gets a second) and spread over the meeting, so names
  appear early and a key that is really two people is seen at both ends;
- audits: moments chosen without regard to keys (one per stretch of speech,
  and inside long turns nobody has looked at), which is how a voice the
  diarizer merged or misassigned gets caught;
- fragment looks: for a key whose every turn is too short for an anchor, a
  moment just after its longest turns, so a one-line speaker still gets a
  look (weaker evidence, which the resolver only ever suggests from).

A moment sits well inside a turn: the meeting app's highlight trails the
voice by up to about two seconds, and a moment while someone else (or the
owner, on the microphone) is also talking proves nothing, so those are never
chosen. Pure functions; the run decides how many waves to ask for.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LAG = 1.6           # seconds into a turn before the app's highlight is trusted
TAIL = 0.7          # and this far from its end
MIN_TURN = 2.5      # shorter turns get no anchor
FRAGMENT_MIN = 0.8  # shortest turn a fragment look is tried in
FRAGMENT_AFTER = 0.45   # a fragment look is at most this far past the turn's end
FRAGMENT_EARLIEST = 1.0     # and at least this far into it
FRAGMENT_STEP = 0.3
# A "yeah" right after someone else catches the highlight still on them: a
# fragment is looked at only when nobody else talked this long before the look.
FRAGMENT_QUIET = 2.6
OVERLAP_BEFORE = 1.5
OWNER_BEFORE = 1.0
MERGE_GAP = 1.0     # lines of one key this close form one turn

DEPTHS = {
    #            first wave, cap per key, long-turn step, audit every (s of speech)
    "quick":    {"first": 2, "cap": 6, "step": 30.0, "audit": None},
    "standard": {"first": 3, "cap": 12, "step": 20.0, "audit": 60.0},
    "thorough": {"first": 5, "cap": 30, "step": 10.0, "audit": 25.0},
}


@dataclass(frozen=True)
class Turn:
    idx: int
    key: str
    start: float
    end: float
    seg_ids: tuple[int, ...]

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Moment:
    t: float
    kind: str                  # scout | anchor | audit | fragment
    key: str | None = None
    turn: int | None = None


@dataclass
class Timeline:
    """A meeting's speech, as the planner and the resolver see it."""
    turns: list[Turn]
    owner: list[tuple[float, float]] = field(default_factory=list)

    def turn_at(self, t: float, lag: float = 0.8, tail: float = 0.5) -> Turn | None:
        best = None
        for tr in self.turns:
            if tr.start + lag <= t <= tr.end + tail:
                if best is None or tr.start > best.start:
                    best = tr
        return best

    def crowded(self, t: float, turn: Turn) -> bool:
        """Someone besides ``turn``'s speaker talks just before ``t``."""
        lo = t - OVERLAP_BEFORE
        for tr in self.turns:
            if tr.idx != turn.idx and tr.key != turn.key and tr.start < t + 0.3 and tr.end > lo:
                return True
        olo = t - OWNER_BEFORE
        return any(a < t + 0.3 and b > olo for a, b in self.owner)


def build(segments: list[dict], *, owner_key: str = "me",
          skip_keys: set[str] | None = None) -> Timeline:
    """Turns from transcript lines ({id, key, start_time, end_time}); the
    owner's microphone lines become owner intervals, not turns."""
    skip = skip_keys or set()
    lines = sorted((s for s in segments if s["end_time"] > s["start_time"]),
                   key=lambda s: s["start_time"])
    turns: list[Turn] = []
    owner: list[tuple[float, float]] = []
    cur = None
    for s in lines:
        k = s["key"]
        if k == owner_key:
            owner.append((s["start_time"], s["end_time"]))
            continue
        if k in skip or k.startswith("["):
            continue
        if cur and cur["key"] == k and s["start_time"] - cur["end"] <= MERGE_GAP:
            cur["end"] = max(cur["end"], s["end_time"])
            cur["ids"].append(s["id"])
        else:
            if cur:
                turns.append(Turn(len(turns), cur["key"], cur["start"], cur["end"],
                                  tuple(cur["ids"])))
            cur = {"key": k, "start": s["start_time"], "end": s["end_time"], "ids": [s["id"]]}
    if cur:
        turns.append(Turn(len(turns), cur["key"], cur["start"], cur["end"], tuple(cur["ids"])))
    return Timeline(turns, owner)


def _turn_moments(tr: Turn, step: float) -> list[float]:
    if tr.length < MIN_TURN:
        return []
    t = tr.start + min(LAG, max(0.6, tr.length * 0.4))
    out = []
    while t <= tr.end - min(TAIL, tr.length * 0.2):
        out.append(round(t, 2))
        t += step
    return out


def candidates(tl: Timeline, depth: str = "standard") -> dict[str, list[Moment]]:
    """Every usable anchor moment per key, best first: longer turns first,
    then spread out so consecutive picks are far apart in time."""
    cfg = DEPTHS.get(depth, DEPTHS["standard"])
    by_key: dict[str, list[tuple[float, Moment]]] = {}
    for tr in tl.turns:
        for t in _turn_moments(tr, cfg["step"]):
            if tl.crowded(t, tr):
                continue
            by_key.setdefault(tr.key, []).append((tr.length, Moment(t, "anchor", tr.key, tr.idx)))
    out: dict[str, list[Moment]] = {}
    for k, items in by_key.items():
        items.sort(key=lambda x: -x[0])
        chosen: list[Moment] = []
        pool = [m for _l, m in items]
        while pool:
            if not chosen:
                chosen.append(pool.pop(0))
                continue
            # Farthest from everything chosen so far, ties to the longer turn.
            far = max(range(len(pool)), key=lambda i: min(abs(pool[i].t - c.t) for c in chosen))
            chosen.append(pool.pop(far))
        out[k] = chosen
    return out


def first_wave(cands: dict[str, list[Moment]], talk: dict[str, float],
               depth: str = "standard") -> list[Moment]:
    """Breadth first, most talk time first: every key's best moment, then
    every key's second, up to the depth's first-wave size (fragments under
    10 s of talk get fewer)."""
    cfg = DEPTHS.get(depth, DEPTHS["standard"])
    order = sorted(cands, key=lambda k: -talk.get(k, 0.0))
    want = {k: (cfg["first"] if talk.get(k, 0.0) >= 10.0 else max(1, cfg["first"] - 1))
            for k in order}
    out: list[Moment] = []
    for rnd in range(max(want.values(), default=0)):
        for k in order:
            if rnd < want[k] and rnd < len(cands[k]):
                out.append(cands[k][rnd])
    return out


def more_for(key: str, cands: dict[str, list[Moment]], used: set[float], n: int,
             depth: str = "standard", known=None) -> list[Moment]:
    """The key's next ``n`` unread moments, within the depth's cap per key.
    ``known(t)`` marks moments an earlier run already read."""
    cfg = DEPTHS.get(depth, DEPTHS["standard"])
    have = sum(1 for m in cands.get(key, []) if m.t in used)
    room = max(0, cfg["cap"] - have)
    fresh = [m for m in cands.get(key, []) if m.t not in used and not (known and known(m.t))]
    return fresh[:min(n, room)]


def audits(tl: Timeline, used: set[float], depth: str = "standard") -> list[Moment]:
    """Key-agnostic moments: one per ``audit`` seconds of speech, plus one in
    every turn of 8 s or more that has no moment yet."""
    cfg = DEPTHS.get(depth, DEPTHS["standard"])
    every = cfg["audit"]
    if not every:
        return []
    out: list[Moment] = []
    acc = 0.0
    for tr in tl.turns:
        moments = [t for t in _turn_moments(tr, cfg["step"]) if not tl.crowded(t, tr)]
        if not moments:
            continue
        looked = any(tr.start <= u <= tr.end for u in used)
        acc += tr.length
        if acc >= every or (tr.length >= 8.0 and not looked):
            mid = moments[len(moments) // 2]
            if mid not in used:
                out.append(Moment(mid, "audit", tr.key, tr.idx))
            acc = 0.0
    return out


def _quiet_before(tl: Timeline, t: float, turn: Turn, span: float) -> bool:
    """Nobody but ``turn``'s speaker talked in the ``span`` seconds before ``t``."""
    lo = t - span
    return not any(tr.key != turn.key and tr.start < t and tr.end > lo for tr in tl.turns)


def fragments(tl: Timeline, keys, per_key: int = 2) -> list[Moment]:
    """A look for each of ``keys`` that has no anchor at all (every turn
    under MIN_TURN): just after its longest turns, once the app's highlight
    has caught up and before anyone else starts, and only when nobody else
    spoke just before (their highlight would still be up). Still weaker than
    an anchor, so one look only ever backs a suggestion."""
    by_key: dict[str, list[Turn]] = {}
    for tr in tl.turns:
        if tr.key in keys:
            by_key.setdefault(tr.key, []).append(tr)
    out: list[Moment] = []
    for k in keys:
        trs = by_key.get(k) or []
        if not trs or any(tr.length >= MIN_TURN for tr in trs):
            continue
        n = 0
        for tr in sorted(trs, key=lambda tr: -tr.length):
            if tr.length < FRAGMENT_MIN:
                break
            # The latest moment that works, the highlight being likelier to
            # have caught up: from just past the line back to a second into
            # it (someone answering at once, the owner included, rules out
            # the later moments).
            t = min(tr.start + LAG, tr.end + FRAGMENT_AFTER)
            while t >= tr.start + FRAGMENT_EARLIEST - 1e-6:
                if not tl.crowded(t, tr) and tl.turn_at(t) == tr and \
                        _quiet_before(tl, t, tr, FRAGMENT_QUIET):
                    out.append(Moment(round(t, 2), "fragment", k, tr.idx))
                    n += 1
                    break
                t -= FRAGMENT_STEP
            if n >= per_key:
                break
    return out


def scouts(tl: Timeline, n: int = 3) -> list[Moment]:
    """Full-frame looks spread over the meeting, each inside a clean turn."""
    clean = []
    for tr in tl.turns:
        for t in _turn_moments(tr, 1e9):
            if not tl.crowded(t, tr):
                clean.append((t, tr))
    if not clean:
        return []
    clean.sort(key=lambda x: x[0])
    span0, span1 = clean[0][0], clean[-1][0]
    out, seen = [], set()
    for k in range(n):
        target = span0 + (span1 - span0) * ((k + 0.5) / n)
        t, tr = min(clean, key=lambda x: abs(x[0] - target))
        if t not in seen:
            seen.add(t)
            out.append(Moment(t, "scout", tr.key, tr.idx))
    return out
