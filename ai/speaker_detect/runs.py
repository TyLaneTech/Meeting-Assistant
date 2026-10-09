"""Runs: one pass of AI speaker detection over one or more meetings.

A run plans which moments to look at, sends them to the vision model in
parallel waves, resolves the evidence into a change set, and then applies or
suggests each change according to its spec (the user's settings, adjusted by
their own words for this run). Every applied change goes through the app's
own speaker functions and is journaled (core.speaker_journal), so the whole
run can be undone.

Speed comes from overlap: the scouts and the first wave of reads go out
together on full frames; once a scout has found where the participants are,
later waves send only that crop; frames decode on a pool of PyAV workers
while requests are in flight; and later waves ask only about speakers whose
evidence is still thin or mixed.

The meeting page watches a run work: every frame sent goes out as a small
preview (``FrameStore``, in memory) over SSE (speaker_run_frames), then again
with who was read as speaking on it and where.

The app supplies everything app-owned through ``Deps`` (the way the Agent API
gets ``AgentContext``), which is also what lets the evaluation harness and the
tests drive a run with stand-ins and ``dry_run``.
"""
from __future__ import annotations

import contextlib
import io
import queue
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from capture_video import frames
from core import log, speaker_journal

from ai.speaker_detect import observations as obs_mod
from ai.speaker_detect import planner, prompts, resolver
from ai.speaker_detect.names import NameBook
from ai.speaker_detect.vision import Gate, VisionClient, VisionError

OWNER_KEY = "me"
BATCH = 6
SCOUT_WIDTH = 1568
READ_WIDTH = 1280
CROP_MARGIN = 40
MAX_WAVES = 3
MAX_FRAGMENT_LOOKS = 24      # per meeting: one-line speakers each get a look, within reason
HIGH, MEDIUM = 0.85, 0.7

DEFAULT_MODELS = {
    "anthropic": ("claude-haiku-5-5", "claude-sonnet-5-5"),
}


@dataclass
class RunSpec:
    autonomy: str = "apply_confident"          # suggest | apply_confident | act_fully
    library_writes: str = "follow_autonomy"    # follow_autonomy | on_accept | never
    depth: str = "standard"                    # quick | standard | thorough
    recheck_user_labels: bool = False
    trust_screen: bool = False
    targets: list[str] = field(default_factory=list)
    constraints: list[dict] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    intent: str = "identify"                   # identify | question
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Deps:
    segments: Callable[[str], list[dict]]
    labels: Callable[[str], dict]
    owner_name: Callable[[], str | None]
    candidates: Callable[[str], dict]
    live_media: Callable[[], dict]
    client_for: Callable[[str], object]
    setting: Callable[[str, object], object]
    push: Callable[[str, dict], None] = lambda kind, data: None
    # (sid, keys) -> ({key: library match}, {key: mean voice}); see voices.key_voices.
    voices: Callable[[str, list[str]], tuple[dict, dict]] | None = None
    # Voice vectors for single turns, [(turn idx, start, end)] -> {idx: vector},
    # asked for only for keys that look like two people (to split them).
    turn_vectors: Callable[[str, list[tuple[int, float, float]]], dict] | None = None
    apply_name: Callable[..., None] | None = None
    apply_move: Callable[..., None] | None = None
    ensure_key: Callable[[str, str, str], None] | None = None
    # (sid, [(old name, new name)], lines moved, their ids): a meeting's
    # speakers changed.
    after_session: Callable[[str, list, int, list], None] | None = None
    fingerprint_db: object = None
    busy: Callable[[str], bool] = lambda session_id: False
    # True while a recording is running: the turn voices then embed in small
    # steps with pauses, so a run started meanwhile never starves the capture.
    recording: Callable[[], bool] = lambda: False
    # The meeting's speaker-change lock, held while a run applies its changes
    # (accepting and undoing take the same one).
    lock_for: Callable[[str], object] | None = None
    constraints: Callable[[str], list[dict]] | None = None
    # Names the model is told to expect: the calendar invite's attendees.
    # Never the meeting's current speaker names, which may be the voice
    # library's guesses and would steer the model toward them.
    prompt_people: Callable[[str], list[str]] | None = None
    save_run: Callable[[dict], None] | None = None


@dataclass
class Run:
    id: str
    sessions: list[str]
    trigger: str
    instructions: str
    spec: RunSpec
    status: str = "queued"
    progress: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    started: float = 0.0
    chips: list[str] = field(default_factory=list)
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def as_dict(self) -> dict:
        return {"id": self.id, "sessions": self.sessions, "trigger": self.trigger,
                "instructions": self.instructions, "spec": self.spec.as_dict(),
                "status": self.status, "progress": self.progress, "stats": self.stats,
                "report": self.report, "error": self.error, "chips": self.chips,
                "created_at": self.created_at, "started_at": self.started_at,
                "finished_at": self.finished_at}


def models(setting: Callable[[str, object], object]) -> tuple[str, str, str]:
    """(provider, fast model, strong model) from settings, defaulting to the
    app's own AI provider."""
    provider = (setting("speaker_ai_provider", "") or setting("ai_provider", "anthropic")
                or "anthropic")
    fast_default, strong_default = DEFAULT_MODELS.get(
        provider, (setting("ai_model", "") or "", setting("ai_model", "") or ""))
    fast = setting("speaker_ai_model", "") or fast_default
    strong = setting("speaker_ai_strong_model", "") or strong_default
    return provider, fast, strong


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def _carries(row: dict | None, key: str | None, name: str | None) -> bool:
    """The speaker key is called ``name`` now (its label, or the key itself)."""
    return NameBook().same((row or {}).get("name") or key, name)


def _within_focus(ops: list, focus: set[str], seg_key: dict | None = None) -> list:
    """A run aimed at some speakers changes only those: names for them, and
    lines moving to or from them."""
    out = []
    for op in ops:
        if op.type == "name":
            ks = [k for k in op.keys if k in focus]
            if ks:
                if ks != op.keys:
                    op.keys = ks
                    op.summary = f"Named {', '.join(ks)} {op.name}" + (
                        f" (was {op.replaces})" if op.replaces else "")
                out.append(op)
        elif op.type in ("move", "split"):
            if op.to_key in focus:
                out.append(op)
            elif set(op.keys) & focus:
                if seg_key is not None:
                    op.segment_ids = [i for i in op.segment_ids if seg_key.get(i) in focus]
                    op.keys = [k for k in op.keys if k in focus]
                if op.segment_ids:
                    out.append(op)
        elif not op.keys or set(op.keys) & focus:
            out.append(op)
    return out


def policy(op: resolver.Op, spec: RunSpec) -> tuple[str, bool]:
    """(action, train) for one change: action is apply | suggest | skip.
    A name for speakers nobody has named yet is offered on less evidence
    than one that replaces a name: a suggestion for a blank speaker can only
    help, and leaving it out leaves the user with nothing."""
    if op.type == "finding":
        return "note", False
    c = op.confidence
    blank = op.type == "name" and not op.replaces
    if spec.autonomy == "suggest":
        action = "suggest" if c >= 0.5 else "skip"
    elif spec.autonomy == "act_fully":
        action = "apply" if c >= MEDIUM else ("suggest" if c >= 0.5 else "skip")
    else:
        floor = 0.5 if blank else MEDIUM
        action = "apply" if c >= HIGH else ("suggest" if c >= floor else "skip")
    train = False
    if op.train and action == "apply":
        if spec.library_writes == "follow_autonomy":
            train = c >= HIGH or spec.autonomy == "act_fully"
    return action, train


def _thumbnail(jpeg: bytes, width: int) -> tuple[bytes, int, int]:
    """A small JPEG of a frame (decoded at a reduced scale, which is fast)."""
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg))
    img.draft("RGB", (width, width))
    img = img.convert("RGB")
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))),
                         Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=74)
    return buf.getvalue(), img.width, img.height


def _norm_box(box, crop, vsize) -> list[float] | None:
    """A video-pixel box as fractions of the picture the model was shown
    (the crop, or the whole frame)."""
    if not box or len(box) != 4:
        return None
    if crop:
        ox, oy, w, h = crop[0], crop[1], crop[2] - crop[0], crop[3] - crop[1]
    elif vsize:
        ox, oy, (w, h) = 0.0, 0.0, vsize
    else:
        return None
    if w <= 0 or h <= 0:
        return None
    x0, y0, x1, y1 = (float(v) for v in box)
    b = [(x0 - ox) / w, (y0 - oy) / h, (x1 - ox) / w, (y1 - oy) / h]
    b = [round(max(0.0, min(1.0, v)), 4) for v in b]
    if b[2] - b[0] < 0.005 or b[3] - b[1] < 0.005:
        return None
    return b


class FrameStore:
    """Previews of the frames a run sends, and what was read off each, for
    the meeting page to show while the run works and after. In memory, for
    the last few runs only: the readings are stored (observations), the
    pictures are not."""

    KEEP_RUNS = 4
    WIDTH = 360

    def __init__(self):
        self._runs: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def _entry(self, run_id: str) -> dict:
        r = self._runs.get(run_id)
        if r is None:
            r = self._runs[run_id] = {"frames": {}, "jpegs": {}, "next": 1}
            while len(self._runs) > self.KEEP_RUNS:
                self._runs.popitem(last=False)
        return r

    def begin(self, run_id: str) -> None:
        with self._lock:
            self._entry(run_id)

    def settle(self, run_id: str) -> None:
        """A run ended: frames still out were never read."""
        with self._lock:
            for meta in (self._runs.get(run_id) or {}).get("frames", {}).values():
                if meta["state"] == "looking":
                    meta["state"] = "failed"

    def add(self, run_id: str, jpeg: bytes, *, t: float, kind: str) -> dict:
        thumb, w, h = _thumbnail(jpeg, self.WIDTH)
        with self._lock:
            r = self._entry(run_id)
            fid = r["next"]
            r["next"] += 1
            r["frames"][fid] = {"id": fid, "t": round(float(t), 2), "kind": kind,
                                "state": "looking", "w": w, "h": h}
            r["jpegs"][fid] = thumb
            return dict(r["frames"][fid])

    def update(self, run_id: str, fid: int, **fields) -> dict | None:
        with self._lock:
            meta = (self._runs.get(run_id) or {}).get("frames", {}).get(fid)
            if meta is None:
                return None
            meta.update(fields)
            return dict(meta)

    def jpeg(self, run_id: str, fid: int) -> bytes | None:
        with self._lock:
            return (self._runs.get(run_id) or {}).get("jpegs", {}).get(fid)

    def list(self, run_id: str) -> list[dict] | None:
        """The run's frames in the order they were sent, or None when the run
        is not kept (another run since, or a restart)."""
        with self._lock:
            r = self._runs.get(run_id)
            return None if r is None else [dict(m) for m in r["frames"].values()]


class Detector:
    """Starts runs, keeps their state, and runs a meeting's pass."""

    def __init__(self, deps: Deps):
        self.deps = deps
        self.runs: dict[str, Run] = {}
        self.frames = FrameStore()
        self._lock = threading.Lock()
        self._busy_sessions: set[str] = set()

    # ── run lifecycle ───────────────────────────────────────────────────────

    def start(self, sessions: list[str], *, trigger: str, instructions: str = "",
              spec: RunSpec | None = None, wait: float = 0.0,
              chips: list[str] | None = None) -> Run:
        run = Run(uuid.uuid4().hex[:12], list(sessions), trigger, instructions, spec or RunSpec(),
                  chips=list(chips or []), created_at=_utcnow())
        with self._lock:
            # One run per meeting at a time: a second would skip the meeting
            # and report nothing, which reads as "no changes needed".
            for other in self.runs.values():
                if other.status in ("queued", "running") and set(other.sessions) & set(sessions):
                    raise RuntimeError("Speakers are already being identified in this meeting.")
            self.runs[run.id] = run
        self._save(run)
        t = threading.Thread(target=self._run, args=(run,), daemon=True,
                             name=f"speaker-ai-{run.id}")
        t.start()
        if wait:
            t.join(wait)
        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self.runs.get(run_id)

    def cancel(self, run_id: str) -> bool:
        run = self.get(run_id)
        if not run or run.status not in ("queued", "running"):
            return False
        run.cancel.set()
        return True

    def _progress(self, run: Run, **fields) -> None:
        run.progress.update(fields)
        self.deps.push("speaker_run_progress", {"run_id": run.id, **run.progress})

    def _save(self, run: Run) -> None:
        if self.deps.save_run is None or run.spec.dry_run:
            return
        try:
            self.deps.save_run(run.as_dict())
        except Exception as e:  # noqa: BLE001 - the record is a convenience
            log.warn("speakers", f"Could not save run {run.id}: {e}")

    def _run(self, run: Run) -> None:
        run.status, run.started, run.started_at = "running", time.perf_counter(), _utcnow()
        if not run.spec.dry_run:
            self.frames.begin(run.id)
        self._save(run)
        self.deps.push("speaker_run_start", {"run_id": run.id, "sessions": run.sessions,
                                             "trigger": run.trigger, "spec": run.spec.as_dict(),
                                             "chips": run.chips})
        totals = {"applied": 0, "suggested": 0, "findings": 0, "frames": 0}
        sessions_out = []
        try:
            for sid in run.sessions:
                if run.cancel.is_set():
                    break
                with self._lock:
                    if sid in self._busy_sessions:
                        log.info("speakers", f"{sid[:8]} already has a detection running; skipped")
                        continue
                    self._busy_sessions.add(sid)
                try:
                    res = self.run_session(run, sid)
                finally:
                    with self._lock:
                        self._busy_sessions.discard(sid)
                sessions_out.append(res)
                for k in ("applied", "suggested", "findings"):
                    totals[k] += len(res.get(k, []))
                totals["frames"] += res.get("frames", 0)
            run.status = "cancelled" if run.cancel.is_set() else "done"
        except Exception as e:  # noqa: BLE001 - a run must always end with a status
            log.error("speakers", f"Speaker detection run {run.id} failed: {e}")
            import traceback; traceback.print_exc()
            run.status, run.error = "failed", str(e)
        run.stats["seconds"] = round(time.perf_counter() - run.started, 1)
        run.report = {"totals": totals, "sessions": [
            {k: v for k, v in s.items() if k != "observations"} for s in sessions_out]}
        run.finished_at = _utcnow()
        self.frames.settle(run.id)
        self._save(run)
        run.done.set()
        self.deps.push("speaker_run_done", {"run_id": run.id, "status": run.status,
                                            "error": run.error, "report": run.report,
                                            "stats": run.stats, "sessions": run.sessions})

    # ── one meeting ─────────────────────────────────────────────────────────

    def run_session(self, run: Run, sid: str) -> dict:
        """Detect, resolve and act for one meeting. Returns the meeting's report."""
        spec, deps = run.spec, self.deps
        t0 = time.perf_counter()
        segs = deps.segments(sid)
        labels = deps.labels(sid)
        live = deps.live_media() or {}
        out = {"session_id": sid, "applied": [], "suggested": [], "findings": [],
               "frames": 0, "status": "done"}
        if not frames.available(sid, live):
            out["status"] = "no_video"
            out["findings"].append({"kind": "no_video", "summary":
                                    "This meeting has no screen recording to read."})
            return out

        noise = {k for k, r in labels.items() if r and r.get("is_noise")}
        tl = planner.build(segs, owner_key=OWNER_KEY, skip_keys=noise)
        talk: dict[str, float] = {}
        for tr in tl.turns:
            talk[tr.key] = talk.get(tr.key, 0.0) + tr.length
        keys = {}
        for k in talk:
            r = labels.get(k) or {}
            keys[k] = resolver.KeyState(k, r.get("name") or k, r.get("global_id"),
                                        r.get("set_by"), bool(r.get("is_noise")), talk[k])
        if spec.targets:
            wanted = {str(t).strip().casefold() for t in spec.targets}
            focus = {k for k in keys
                     if k.casefold() in wanted or keys[k].name.casefold() in wanted}
        else:
            focus = set(keys)

        # The voice library's view of each key, worked out while frames are read.
        voice_box: dict = {}
        voice_thread = None
        if deps.voices and keys:
            def _voice():
                try:
                    voice_box["v"], voice_box["c"] = deps.voices(sid, sorted(keys))
                except Exception as e:  # noqa: BLE001 - the screen alone still works
                    log.warn("speakers", f"Voice evidence unavailable for {sid[:8]}: {e}")
            voice_thread = threading.Thread(target=_voice, daemon=True)
            voice_thread.start()

        book = NameBook(deps.candidates(sid) or {})
        owner = deps.owner_name()
        provider, fast, strong = models(deps.setting)
        try:
            concurrency = int(deps.setting("speaker_ai_concurrency", 6) or 6)
        except (TypeError, ValueError):
            concurrency = 6
        client = VisionClient(provider, deps.client_for(provider),
                              gate=Gate(start=max(1, min(16, concurrency))))
        expected = deps.prompt_people(sid) if deps.prompt_people else []
        for name in expected:
            book.add(name, expected=True)
        context = prompts.context_block(owner=owner, candidates=expected, roster=[],
                                        hints=spec.hints)

        cached = [o for o in obs_mod.load(sid)] if not spec.dry_run else []
        seen_t = [o.t for o in cached]
        observed: list[obs_mod.Observation] = list(cached)
        new_frames = [0]
        lock = threading.Lock()

        seen_turns = {tr.idx for tr in (tl.turn_at(u) for u in seen_t) if tr is not None}

        def known(t: float) -> bool:
            """Already read: this moment, or (from an earlier run, whose turns
            may have been cut differently) a moment inside the same turn."""
            if any(abs(t - u) < 0.3 for u in seen_t):
                return True
            tr = tl.turn_at(t)
            return tr is not None and tr.idx in seen_turns and tr.length < 20.0

        cands = planner.candidates(tl, spec.depth)
        cands = {k: v for k, v in cands.items() if k in focus}
        scout_moments = [] if any(o.kind == "scout" for o in cached) else planner.scouts(tl)
        wave = [m for m in planner.first_wave(cands, talk, spec.depth) if not known(m.t)]
        # Speakers too short for an anchor still get a look, nameless ones first.
        short = sorted(focus - set(cands), key=lambda k: (not resolver.is_default_name(
            keys[k].name, k), -talk.get(k, 0.0)))
        wave += [m for m in planner.fragments(tl, short) if not known(m.t)][:MAX_FRAGMENT_LOOKS]
        used: set[float] = set(seen_t)
        region: dict = {"crop": None}
        vsize = _video_size(sid, live)
        for o in cached:
            if o.kind == "scout":
                self._learn_region(region, o, vsize)

        # Each looked-at turn's own voice, to check what the screen says.
        voices = (_TurnVoices(deps.turn_vectors, sid, tl, deps.recording)
                  if deps.turn_vectors else None)

        def want_voices(times) -> None:
            if voices is not None:
                voices.want([tr.idx for tr in (tl.turn_at(t) for t in times) if tr is not None])

        def want_suspects(tal: resolver.Tally) -> None:
            """Every turn of the keys the screen casts doubt on so far, longest
            turns first, so they are heard while later waves are read."""
            if voices is None:
                return
            for k, v in (voice_box.get("v") or {}).items():
                if k in keys:
                    keys[k].voice = v
            doubt = resolver.suspect_keys(tal, keys, book) & focus
            voices.want([t.idx for t in sorted(tl.turns, key=lambda t: -t.length)
                         if t.key in doubt])

        want_voices(seen_t)
        pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix=f"speaker-ai-{sid[:6]}")
        sent = [0]
        sent_lock = threading.Lock()

        def show(metas: list[dict]) -> None:
            metas = [m for m in metas if m]
            if metas:
                deps.push("speaker_run_frames", {"run_id": run.id, "session_id": sid,
                                                 "frames": metas})

        def look(kind: str, shots: list[tuple[float, bytes]]) -> list[int | None]:
            """Previews of the frames going out, for the meeting page."""
            if spec.dry_run:
                return [None] * len(shots)
            metas = []
            for t, jpeg in shots:
                try:
                    metas.append(self.frames.add(run.id, jpeg, t=t, kind=kind))
                except Exception:  # noqa: BLE001 - a preview is never worth a read
                    metas.append(None)
            with sent_lock:
                sent[0] += len(shots)
                run.progress["frames_sent"] = sent[0]
            show(metas)
            return [m["id"] if m else None for m in metas]

        def missed(refs: list) -> None:
            show([self.frames.update(run.id, r, state="failed") for r in refs if r is not None])

        def submit(moments: list[planner.Moment], kind: str):
            futs = []
            if not moments:
                return futs
            want_voices([m.t for m in moments])
            if kind == "scout":
                batches, width, model, crop = [moments], SCOUT_WIDTH, strong, None
            else:
                crop = region["crop"]
                width = READ_WIDTH if crop is None else min(READ_WIDTH, int(crop[2] - crop[0]))
                batches = [moments[i:i + BATCH] for i in range(0, len(moments), BATCH)]
                model = fast
            for b in batches:
                futs.append(pool.submit(self._ask, client, sid, live, b, kind, model, width,
                                        crop, context, vsize, look, missed))
            for m in moments:
                used.add(m.t)
            return futs

        def reading(o: obs_mod.Observation) -> dict:
            """What one frame was read as, for its preview."""
            who = []
            for s in o.speaking:
                person = book.resolve(s.label)[0] if s.label else None
                who.append({"name": person or s.label, "self": s.self_view, "cue": s.cue,
                            "confidence": round(s.confidence, 2),
                            "box": _norm_box(s.box, o.crop, vsize)})
            return {"state": "read", "visible": o.meeting_visible, "speaking": who, "obs": o.id}

        def collect(futs) -> int:
            got = 0
            for f in as_completed(futs):
                if run.cancel.is_set():
                    break
                try:
                    batch_obs = f.result()
                except VisionError as e:
                    out.setdefault("errors", []).append(str(e))
                    continue
                readings = []
                with lock:
                    for o in batch_obs:
                        if not spec.dry_run:
                            obs_mod.store(sid, o, run.id, None)
                        observed.append(o)
                        got += 1
                        new_frames[0] += 1
                        if o.kind == "scout":
                            self._learn_region(region, o, vsize)
                        if o.ref is not None:
                            readings.append(self.frames.update(run.id, o.ref, **reading(o)))
                show(readings)
                out["frames"] = new_frames[0]
                self._progress(run, session_id=sid, stage="reading",
                               frames_done=out["frames"], label=f"Reading the screen · "
                               f"{out['frames']} frames")
            return got

        try:
            self._progress(run, session_id=sid, stage="reading", frames_done=0, frames_sent=0,
                           frames_cached=len(cached), label="Looking at the screen recording")
            futs = submit(scout_moments, "scout") + submit(wave, "read")
            collect(futs)
            for _w in range(MAX_WAVES - 1):
                if run.cancel.is_set():
                    break
                tal = resolver.tally(tl, observed, owner=owner, book=book)
                votes, turns = tal.votes, tal.turns
                want_suspects(tal)
                need: list[planner.Moment] = []
                for k in sorted(focus, key=lambda k: -talk.get(k, 0.0)):
                    pv = votes.get(k, {})
                    tot = sum(pv.values())
                    best = max(pv.values()) if pv else 0.0
                    n_best = max((len(v) for v in turns.get(k, {}).values()), default=0)
                    settled = tot and best / tot >= 0.85 and (n_best >= 3 or
                                                               (talk[k] < 10 and n_best >= 1))
                    # Two people in one key: a split by voice needs several
                    # turns the screen settled for each of them.
                    second = sorted(pv.values(), reverse=True)[1] if len(pv) > 1 else 0.0
                    mixed = tot and second / tot >= 0.15
                    if not settled:
                        need += planner.more_for(k, cands, used, 4 if mixed else 2, spec.depth,
                                                 known=known)
                if _w == 0:
                    need += [m for m in planner.audits(tl, used, spec.depth) if not known(m.t)]
                if not need:
                    break
                collect(submit(need, "read"))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        if run.cancel.is_set():
            # Stopped: what was read stays cached for next time; nothing is
            # decided or changed on partial evidence.
            if voices is not None:
                voices.finish(timeout=0.5)
            out["status"] = "cancelled"
            out["usage"] = client.usage.as_dict()
            out["seconds"] = round(time.perf_counter() - t0, 1)
            return out

        if voice_thread:
            voice_thread.join(timeout=30)
        for k, v in (voice_box.get("v") or {}).items():
            if k in keys:
                keys[k].voice = v
        centroids = voice_box.get("c") or None
        constraints = list(spec.constraints)
        if deps.constraints:
            constraints += deps.constraints(sid)
        turn_vectors = None
        if voices is not None:
            # Every turn of a key the screen casts doubt on, to sort it by voice.
            want_suspects(resolver.tally(tl, observed, owner=owner, book=book))
            self._progress(run, session_id=sid, stage="voices",
                           label="Checking the screen against the voices")
            turn_vectors = voices.finish(timeout=5.0 if run.cancel.is_set() else 120.0)
            out["voice_seconds"] = round(voices.seconds, 1)

        self._progress(run, session_id=sid, stage="deciding", label="Deciding who is who")
        res = resolver.resolve(tl, observed, keys, book, owner=owner, constraints=constraints,
                               recheck_user_labels=spec.recheck_user_labels,
                               trust_screen=spec.trust_screen, centroids=centroids,
                               turn_vectors=turn_vectors,
                               taken=set(labels) | {s["key"] for s in segs})
        out["decisions"] = {k: {"person": d.person, "confidence": round(d.confidence, 3),
                                "turns": d.turns, "share": round(d.share, 3), "reason": d.reason,
                                "tentative": d.tentative, "evidence": list(d.evidence)[:6],
                                "votes": {p: round(v, 2) for p, v in sorted(
                                    d.votes.items(), key=lambda x: -x[1])[:3]}}
                            for k, d in res.decisions.items()}
        out["non_specific"] = sorted(res.non_specific)
        out["seen"] = res.seen
        out["misreads"] = res.misreads
        out["voiced_turns"] = len(turn_vectors or {})
        if spec.dry_run:
            out["observations"] = [
                {"t": o.t, "kind": o.kind, "visible": o.meeting_visible, "layout": o.layout,
                 "speaking": [(s.label, s.person, s.cue, round(s.confidence, 2))
                              for s in o.speaking], "pinned": o.pinned, "crop": o.crop,
                 "roster": [r.get("label") for r in o.roster][:12],
                 "participants_box": o.participants_box}
                for o in observed]
            out["region"] = region.get("crop")
            out["voices"] = {k: ks.voice for k, ks in keys.items() if ks.voice}
            if centroids:
                import numpy as np
                ck = sorted(centroids)
                out["voice_sims"] = {
                    f"{a}|{b}": round(float(np.dot(centroids[a], centroids[b])), 3)
                    for i, a in enumerate(ck) for b in ck[i + 1:]}
        out["usage"] = client.usage.as_dict()
        out["seconds"] = round(time.perf_counter() - t0, 1)
        ops = (_within_focus(res.ops, focus, {s["id"]: s["key"] for s in segs})
               if spec.targets else res.ops)
        renames = []
        moved: list[int] = []
        # What the run read at the start, updated by its own changes as they
        # land: a row or line that differs from it was edited by someone else
        # while the frames were read, and is left as they set it.
        expected_rows = {k: (dict(v) if v else v) for k, v in labels.items()}
        expected_seg = {s["id"]: s["key"] for s in segs}

        def edited_meanwhile(op: resolver.Op) -> str | None:
            rows = deps.labels(sid)
            for k in list(op.keys) + [k for k in (op.to_key,) if k]:
                a, b = expected_rows.get(k) or {}, rows.get(k) or {}
                if any(a.get(f) != b.get(f) for f in ("name", "set_by", "global_id", "is_noise")):
                    return k
            if op.segment_ids:
                now = {s["id"]: s["key"] for s in deps.segments(sid)}
                if any(now.get(i) != expected_seg.get(i) for i in op.segment_ids):
                    return "Some of " + ", ".join(op.keys) + "'s lines"
            return None

        def settle(op: resolver.Op) -> None:
            rows = deps.labels(sid)
            for k in list(op.keys) + [k for k in (op.to_key, op.new_key) if k]:
                expected_rows[k] = rows.get(k)
            if op.segment_ids:
                now = {s["id"]: s["key"] for s in deps.segments(sid)}
                for i in op.segment_ids:
                    expected_seg[i] = now.get(i)

        def suggest(seq: int, op: resolver.Op, entry: dict) -> None:
            cid = speaker_journal.record(session_id=sid, actor="ai", op=op.as_dict(),
                                         state="suggested", run_id=run.id, seq=seq,
                                         risk=op.risk, confidence=op.confidence,
                                         evidence=op.evidence, summary=op.summary)
            out["suggested"].append({**entry, "change_id": cid})

        if not spec.dry_run:
            # This run's view replaces what earlier runs left waiting.
            speaker_journal.supersede_suggestions(sid, run.id,
                                                  keys=focus if spec.targets else None)
        lock = deps.lock_for(sid) if deps.lock_for else contextlib.nullcontext()
        with lock:
            for seq, op in enumerate(ops):
                if run.cancel.is_set():
                    break
                action, train = policy(op, spec)
                entry = {"op": op.as_dict(), "summary": op.summary,
                         "confidence": round(op.confidence, 3)}
                if action == "note":
                    out["findings"].append({"kind": op.kind, "summary": op.summary,
                                            "keys": op.keys})
                    continue
                if action == "skip":
                    continue
                if spec.dry_run:
                    out["applied" if action == "apply" else "suggested"].append(entry)
                    continue
                gone = edited_meanwhile(op)
                if gone:
                    out["findings"].append({
                        "kind": "edited_meanwhile", "keys": op.keys,
                        "summary": f"{gone} changed while this run was reading the screen, so "
                                   f"it was left as it is now ({op.summary})"})
                    continue
                if action == "apply" and op.type == "move" and not \
                        _carries(deps.labels(sid).get(op.to_key), op.to_key, op.name):
                    # Its new speaker isn't called that (yet): the lines would
                    # read one name and the speaker another.
                    action = "suggest"
                if action == "suggest" or spec.intent == "question":
                    suggest(seq, op, entry)
                    continue
                try:
                    cid = self.apply(sid, op, train=train, run_id=run.id, seq=seq)
                    out["applied"].append({**entry, "change_id": cid})
                    settle(op)
                    if op.type == "name":
                        renames += [(keys[k].name, op.name) for k in op.keys if k in keys]
                    else:
                        moved += op.segment_ids
                except Exception as e:  # noqa: BLE001 - one bad change must not stop the rest
                    log.warn("speakers", f"Could not apply {op.summary!r}: {e}")
                    out.setdefault("errors", []).append(f"{op.summary}: {e}")
        if run.cancel.is_set():
            out["status"] = "cancelled"
        if not spec.dry_run and deps.after_session and out["applied"]:
            deps.after_session(sid, renames, len(moved), moved)
        log.info("speakers", f"{sid[:8]}: {len(out['applied'])} applied, "
                             f"{len(out['suggested'])} suggested, {out['frames']} frames, "
                             f"{out['usage']['input_tokens']} tokens in, {out['seconds']} s")
        return out

    # ── one request ─────────────────────────────────────────────────────────

    def _ask(self, client: VisionClient, sid: str, live: dict, moments, kind: str, model: str,
             width: int, crop, context: str, vsize, look=None,
             missed=None) -> list[obs_mod.Observation]:
        """Read one batch. ``look(kind, [(t, jpeg)])`` hears about the frames
        before they go out (and returns their preview ids, put on each
        observation's ``ref``); ``missed(ids)`` about those that came back
        unread."""
        times = [m.t for m in moments]
        jpegs = frames.grab_many(sid, times, width=width, crop=crop, live=live,
                                 workers=min(4, len(times)))
        keep = [(m, j) for m, j in zip(moments, jpegs) if j]
        if not keep:
            return []
        from PIL import Image
        refs = look(kind, [(m.t, j) for m, j in keep]) if look else [None] * len(keep)
        sizes = [Image.open(io.BytesIO(j)).size for _m, j in keep]
        labels = [f"Image {i} ({_fmt(m.t)})" for i, (m, _j) in enumerate(keep)]
        stamps = [_fmt(m.t) for m, _j in keep]
        task = prompts.scout_task(stamps) if kind == "scout" else \
            prompts.read_task(stamps, cropped=crop is not None)
        try:
            answer = client.analyze(model, [j for _m, j in keep], labels, task, context)
        except VisionError:
            if missed:
                missed(refs)
            raise
        by_i = {f.get("i"): f for f in answer.get("frames") or [] if isinstance(f, dict)}
        result, unread = [], []
        for i, (m, _j) in enumerate(keep):
            f = by_i.get(i)
            if f is None:
                unread.append(refs[i])
                continue
            o = obs_mod.parse(f, t=m.t, kind=kind, crop=crop, img_size=sizes[i],
                              video_size=vsize, model=model)
            o.ref = refs[i]
            result.append(o)
        if missed and unread:
            missed(unread)
        return result

    @staticmethod
    def _learn_region(region: dict, o: obs_mod.Observation, vsize) -> None:
        """Crop later reads to the participant area a scout found, when it is
        small enough to be worth it (under 60% of the frame)."""
        box = o.participants_box
        if not (o.meeting_visible and box) or vsize is None:
            return
        vw, vh = vsize
        x0, y0, x1, y1 = box
        x0, y0 = max(0, x0 - CROP_MARGIN), max(0, y0 - CROP_MARGIN)
        x1, y1 = min(vw, x1 + CROP_MARGIN), min(vh, y1 + CROP_MARGIN)
        if (x1 - x0) * (y1 - y0) > 0.6 * vw * vh or x1 - x0 < 120 or y1 - y0 < 120:
            return
        cur = region.get("crop")
        if cur:
            x0, y0 = min(x0, cur[0]), min(y0, cur[1])
            x1, y1 = max(x1, cur[2]), max(y1, cur[3])
            if (x1 - x0) * (y1 - y0) > 0.6 * vw * vh:
                return
        region["crop"] = [round(x0), round(y0), round(x1), round(y1)]

    # ── applying a change ───────────────────────────────────────────────────

    def apply(self, sid: str, op: resolver.Op | dict, *, train: bool, run_id: str | None,
              seq: int = 0, actor: str = "ai", change_id: int | None = None) -> int:
        """Apply one change through the app's own speaker functions and journal
        it (or mark an existing suggestion applied). Returns the change id."""
        d = op.as_dict() if isinstance(op, resolver.Op) else dict(op)
        deps = self.deps
        kind = d["type"]
        keys = list(d.get("keys") or [])
        seg_ids = list(d.get("segment_ids") or [])
        new_key = d.get("new_key")
        if kind == "split":
            # A suggestion accepted later: the key it planned may be in use now.
            taken = set(deps.labels(sid)) | {s["key"] for s in deps.segments(sid)}
            if not new_key or new_key in taken:
                new_key = d["new_key"] = resolver.next_key((), taken)
        if kind == "move" and not _carries(deps.labels(sid).get(d.get("to_key")),
                                           d.get("to_key"), d.get("name")):
            raise ValueError(f"Name {d.get('to_key')} {d.get('name')} first: moved there, these "
                             f"lines would read {d.get('name')} under a speaker called "
                             f"something else.")
        touched = keys + [k for k in (new_key, d.get("to_key")) if k]
        before = speaker_journal.snapshot(sid, touched, seg_ids)
        effects: dict = {"origin": actor}
        if d.get("train_segments"):
            effects["train_segment_ids"] = list(d["train_segments"])
        if kind == "name":
            deps.apply_name(sid, keys, d["name"], d.get("global_id"), bool(d.get("link", True)),
                            train, effects)
        elif kind == "split":
            deps.ensure_key(sid, new_key, d["name"])
            for seg in seg_ids:
                deps.apply_move(seg, d["name"], new_key)
            deps.apply_name(sid, [new_key], d["name"], d.get("global_id"),
                            bool(d.get("link", True)), False, effects)
        elif kind == "move":
            for seg in seg_ids:
                deps.apply_move(seg, d["name"], d["to_key"])
        else:
            raise ValueError(f"Unknown change type {kind!r}")
        after = speaker_journal.snapshot(sid, touched, seg_ids)
        journal_effects = {
            "embedding_ids": effects.get("embedding_ids") or [],
            "created_profiles": [effects["created_profile"]] if effects.get("created_profile")
            else [],
            "global_id": effects.get("global_id"),
        }
        if change_id is not None:
            speaker_journal.mark_applied(change_id, before=before, after=after,
                                         effects=journal_effects)
            return change_id
        return speaker_journal.record(session_id=sid, actor=actor, op=d, state="applied",
                                      run_id=run_id, seq=seq, risk=d.get("risk"),
                                      confidence=d.get("confidence"),
                                      evidence=d.get("evidence"), summary=d.get("summary", ""),
                                      before=before, after=after, effects=journal_effects)


class _TurnVoices:
    """Voice vectors for turns, worked out on one background thread while
    frames are read (embedding is CPU work, the reads are network waits), so
    the voices are usually ready when the reads are."""

    CHUNK = 16
    QUIET_CHUNK, QUIET_PAUSE = 4, 0.75      # while a recording runs

    def __init__(self, fn: Callable, sid: str, tl: planner.Timeline,
                 recording: Callable[[], bool] = lambda: False):
        self.fn, self.sid, self.recording = fn, sid, recording
        self.by_idx = {t.idx: t for t in tl.turns}
        self.asked: set[int] = set()
        self.vectors: dict = {}
        self.seconds = 0.0
        self._stop = threading.Event()
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"speaker-ai-voices-{sid[:6]}")
        self._thread.start()

    def want(self, idxs) -> None:
        new = [i for i in idxs if i is not None and i in self.by_idx and i not in self.asked]
        if new:
            self.asked.update(new)
            self._q.put(new)

    def _loop(self) -> None:
        while True:
            batch = self._q.get()
            if batch is None:
                return
            spans = [(i, self.by_idx[i].start, self.by_idx[i].end) for i in batch]
            j = 0
            while j < len(spans):
                if self._stop.is_set():
                    return
                quiet = self.recording()
                step = self.QUIET_CHUNK if quiet else self.CHUNK
                t0 = time.perf_counter()
                try:
                    self.vectors.update(self.fn(self.sid, spans[j:j + step]) or {})
                except Exception as e:  # noqa: BLE001 - the screen alone still works
                    log.warn("speakers", f"Turn voices unavailable for {self.sid[:8]}: {e}")
                    return
                finally:
                    self.seconds += time.perf_counter() - t0
                j += step
                if quiet:
                    time.sleep(self.QUIET_PAUSE)

    def finish(self, timeout: float) -> dict:
        """The vectors so far, once the queue is done or ``timeout`` passes;
        either way the thread embeds nothing more."""
        self._q.put(None)
        self._thread.join(timeout)
        self._stop.set()
        return dict(self.vectors)


def _video_size(sid: str, live: dict) -> tuple[int, int] | None:
    img = frames.image(sid, 1.0, width=None, live=live, allow_screenshot=False)
    return img.size if img is not None else None
