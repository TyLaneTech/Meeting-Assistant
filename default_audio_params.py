"""
Default values for all tunable transcription and diarization parameters.

These are the baseline values shipped with the application.  User overrides
are stored in data/settings.json under the "audio_params" key.  The settings
UI provides per-parameter reset buttons that restore these defaults.
"""

TRANSCRIPTION_DEFAULTS = {
    "silence_threshold": {
        "value": 0.025,
        "label": "Silence Threshold",
        "description": "RMS level below which audio is considered silence.",
        "tooltip": (
            "Controls the <em>volume floor</em> for silence detection. Audio "
            "with an RMS energy below this value is treated as silence, which "
            "triggers the buffer flush to Whisper after the configured duration.<br><br>"
            "<b>Lower values</b> make the detector more sensitive \u2014 even faint "
            "background noise counts as speech, delaying flushes and producing "
            "longer segments.<br>"
            "<b>Higher values</b> treat more audio as silence, flushing sooner "
            "and producing shorter, snappier segments \u2014 but risk cutting off "
            "quiet speakers."
        ),
        "min": 0.001,
        "max": 0.2,
        "step": 0.001,
        "type": "number",
    },
    "silence_duration": {
        "value": 0.3,
        "label": "Silence Duration",
        "unit": "s",
        "description": "Seconds of silence before flushing audio to Whisper.",
        "tooltip": (
            "Once the audio level drops below the <em>Silence Threshold</em>, "
            "this timer starts. When the silence persists for this many seconds, "
            "the buffered audio is sent to Whisper for transcription.<br><br>"
            "<b>Shorter durations</b> give faster response times but may split "
            "natural pauses mid-sentence.<br>"
            "<b>Longer durations</b> allow speakers to pause without triggering "
            "a premature flush, but add visible latency."
        ),
        "min": 0.1,
        "max": 2.0,
        "step": 0.05,
        "type": "number",
    },
    "min_buffer_seconds": {
        "value": 0.5,
        "label": "Min Buffer",
        "unit": "s",
        "description": "Minimum audio before a silence flush is allowed.",
        "tooltip": (
            "Prevents the system from flushing tiny slivers of audio that "
            "would produce garbage Whisper output. No silence-triggered flush "
            "will fire until at least this much audio has been buffered.<br><br>"
            "<b>Lower values</b> allow very short utterances to be transcribed "
            "quickly.<br>"
            "<b>Higher values</b> ensure Whisper always receives enough context "
            "for accurate transcription, at the cost of some latency."
        ),
        "min": 0.1,
        "max": 3.0,
        "step": 0.1,
        "type": "number",
    },
    "max_buffer_seconds": {
        "value": 10.0,
        "label": "Max Buffer",
        "unit": "s",
        "description": "Hard cap \u2014 forces a flush regardless of silence.",
        "tooltip": (
            "A safety valve that forces the audio buffer to be flushed and "
            "transcribed even if no silence pause has been detected. Prevents "
            "runaway buffering during continuous speech.<br><br>"
            "<b>Lower values</b> ensure more frequent transcription updates but "
            "may cut sentences mid-word.<br>"
            "<b>Higher values</b> let Whisper process longer stretches for "
            "better accuracy, but the transcript updates less frequently."
        ),
        "min": 3.0,
        "max": 30.0,
        "step": 0.5,
        "type": "number",
    },
    "beam_size": {
        "value": 2,
        "label": "Beam Size",
        "description": "Whisper beam search width.",
        "tooltip": (
            "Controls how many candidate transcriptions Whisper considers in "
            "parallel during decoding. A wider beam explores more possibilities "
            "before picking the best result.<br><br>"
            "<b>Beam 1</b> (greedy) is fastest but most error-prone.<br>"
            "<b>Beam 2\u20133</b> is the sweet spot for real-time use.<br>"
            "<b>Beam 5+</b> gives marginally better accuracy but significantly "
            "increases latency and VRAM usage."
        ),
        "min": 1,
        "max": 10,
        "step": 1,
        "type": "int",
    },
    "prompt_chars": {
        "value": 800,
        "label": "Context Window",
        "unit": "chars",
        "description": "Prior transcript fed to Whisper as context.",
        "tooltip": (
            "Whisper uses recent transcript text as a <em>conditioning prompt</em> "
            "to maintain coherence across segments \u2014 preserving names, "
            "terminology, and sentence flow.<br><br>"
            "<b>More characters</b> provide richer context but increase the risk "
            "of hallucination loops if the context itself gets corrupted.<br>"
            "<b>Fewer characters</b> (or zero) make each segment independent, "
            "reducing loop risk but losing cross-segment continuity."
        ),
        "min": 0,
        "max": 2000,
        "step": 50,
        "type": "int",
    },
    "vad_min_silence_ms": {
        "value": 300,
        "label": "VAD Min Silence",
        "unit": "ms",
        "description": "Whisper\u2019s internal VAD silence split threshold.",
        "tooltip": (
            "When Whisper\u2019s built-in Voice Activity Detection is active "
            "(non-diarized mode), this controls the minimum duration of silence "
            "that causes the VAD to split the audio into separate speech regions.<br><br>"
            "<b>Lower values</b> split more aggressively at short pauses.<br>"
            "<b>Higher values</b> keep more speech in a single region, which can "
            "improve transcription quality for speakers with frequent pauses."
        ),
        "min": 50,
        "max": 1000,
        "step": 25,
        "type": "int",
    },
    "vad_speech_pad_ms": {
        "value": 150,
        "label": "VAD Speech Padding",
        "unit": "ms",
        "description": "Padding added around detected speech regions.",
        "tooltip": (
            "After the VAD identifies speech boundaries, this much padding is "
            "added to both the start and end of each speech region to avoid "
            "clipping the first or last syllable.<br><br>"
            "<b>More padding</b> reduces the chance of clipped words but may "
            "include extra silence or noise.<br>"
            "<b>Less padding</b> gives tighter segments but risks cutting off "
            "speech at boundaries."
        ),
        "min": 0,
        "max": 500,
        "step": 25,
        "type": "int",
    },
    "compression_ratio_threshold": {
        "value": 2.0,
        "label": "Hallucination Filter",
        "description": "Compression ratio above which output is discarded.",
        "tooltip": (
            "Whisper measures the <em>compression ratio</em> of its output "
            "text. Highly repetitive or hallucinated text compresses extremely "
            "well, producing a high ratio. Segments exceeding this threshold "
            "are automatically discarded and retried without context.<br><br>"
            "<b>Lower values</b> are stricter \u2014 more aggressive at catching "
            "hallucinations but may occasionally reject valid repetitive speech "
            "(e.g. a speaker saying \u201cno no no no\u201d).<br>"
            "<b>Higher values</b> are more permissive, letting more through at "
            "the risk of hallucination loops."
        ),
        "min": 1.0,
        "max": 3.0,
        "step": 0.1,
        "type": "number",
    },
}

DIARIZATION_DEFAULTS = {
    "step_seconds": {
        "value": 0.25,
        "label": "Step Size",
        "unit": "s",
        "description": "How often speaker labels are updated.",
        "tooltip": (
            "The diarization pipeline advances by this many seconds each "
            "cycle. Each step produces a fresh speaker label decision for "
            "that slice of audio.<br><br>"
            "<b>Smaller steps</b> detect speaker changes faster (less lag at "
            "transitions) but double the compute load per second.<br>"
            "<b>Larger steps</b> are more efficient but speaker changes "
            "may lag by up to one full step duration.<br><br>"
            "<em>Requires session restart to take effect.</em>"
        ),
        "min": 0.1,
        "max": 1.0,
        "step": 0.05,
        "type": "number",
    },
    "duration_seconds": {
        "value": 5.0,
        "label": "Context Window",
        "unit": "s",
        "description": "Audio window fed to the segmentation model.",
        "tooltip": (
            "The segmentation model receives this much audio as context for "
            "each step. The pyannote segmentation-3.0 model was trained on "
            "5-second windows \u2014 deviating significantly may reduce accuracy.<br><br>"
            "<b>Shorter windows</b> process faster but give the model less "
            "context to distinguish speakers.<br>"
            "<b>Longer windows</b> provide more context but increase memory "
            "usage and latency.<br><br>"
            "<em>Requires session restart to take effect.</em>"
        ),
        "min": 2.0,
        "max": 10.0,
        "step": 0.5,
        "type": "number",
    },
    "tau_active": {
        "value": 0.5,
        "label": "Activity Threshold",
        "description": "Voice-activity sensitivity for speaker detection.",
        "tooltip": (
            "Controls how confident the model must be that a speaker is "
            "actively talking before assigning them a label. This is the "
            "diarizer\u2019s own VAD, separate from Whisper\u2019s.<br><br>"
            "<b>Lower values</b> are more sensitive \u2014 picks up quiet speech "
            "and distant speakers, but may also pick up background noise.<br>"
            "<b>Higher values</b> require stronger voice activity, reducing "
            "false positives but potentially missing soft-spoken participants.<br><br>"
            "<em>Requires session restart to take effect.</em>"
        ),
        "min": 0.1,
        "max": 0.9,
        "step": 0.05,
        "type": "number",
    },
    "rho_update": {
        "value": 0.422,
        "label": "Centroid Update Rate",
        "description": "Speaker embedding adaptation speed.",
        "tooltip": (
            "Speaker embeddings (voice fingerprints) are stored as centroids "
            "that get updated as new audio arrives. This controls how much "
            "weight new audio gets versus the existing centroid.<br><br>"
            "<b>Higher values</b> adapt faster to changes in a speaker\u2019s voice "
            "(useful for varying mic distance or tone), but may cause speaker "
            "identities to drift and merge.<br>"
            "<b>Lower values</b> keep centroids stable, which is better for "
            "long recordings with consistent audio quality.<br><br>"
            "<em>Requires session restart to take effect.</em>"
        ),
        "min": 0.1,
        "max": 1.0,
        "step": 0.01,
        "type": "number",
    },
    "delta_new": {
        "value": 0.5,
        "label": "New Speaker Threshold",
        "description": "Distance required to create a new speaker.",
        "tooltip": (
            "When a voice segment doesn\u2019t match any known speaker centroid "
            "within this distance, a new speaker is created. Think of it as "
            "how \u201cdifferent\u201d a voice must sound to be recognized as someone new.<br><br>"
            "<b>Lower values</b> create new speakers more readily \u2014 good for "
            "meetings with many participants who sound similar.<br>"
            "<b>Higher values</b> are more conservative, merging similar voices "
            "into existing speakers \u2014 better when there are fewer participants "
            "to avoid over-segmentation.<br><br>"
            "<em>Requires session restart to take effect.</em>"
        ),
        "min": 0.1,
        "max": 2.0,
        "step": 0.05,
        "type": "number",
    },
    "merge_gap_seconds": {
        "value": 0.1,
        "label": "Merge Gap",
        "unit": "s",
        "description": "Max gap before same-speaker segments merge.",
        "tooltip": (
            "When the same speaker is detected in two consecutive segments "
            "with a gap shorter than this, they are merged into a single "
            "continuous segment.<br><br>"
            "<b>Higher values</b> merge more aggressively, producing fewer, "
            "longer segments \u2014 cleaner output but may merge across genuine "
            "pauses.<br>"
            "<b>Lower values</b> (or zero) preserve every segment boundary, "
            "giving a more granular timeline but potentially fragmenting "
            "continuous speech."
        ),
        "min": 0.0,
        "max": 1.0,
        "step": 0.05,
        "type": "number",
    },
}

ECHO_CANCELLATION_DEFAULTS = {
    "echo_cancel_enabled": {
        "value": 0,
        "label": "Enable Echo Cancellation",
        "description": "Master switch for speaker/mic echo suppression.",
        "tooltip": (
            "Enables advanced echo cancellation for setups where the microphone "
            "picks up audio from desktop speakers (e.g. webcam mic + external "
            "speakers). When disabled, only basic source-gated mixing is used.<br><br>"
            "<b>Leave disabled</b> if you use headphones or a headset \u2014 echo "
            "cancellation is unnecessary and may slightly reduce audio quality.<br>"
            "<b>Enable</b> if you hear duplicated transcriptions caused by the mic "
            "picking up speaker output."
        ),
        "min": 0,
        "max": 1,
        "step": 1,
        "type": "toggle",
    },
    "echo_gate_ratio": {
        "value": 2.0,
        "label": "Gate Ratio",
        "description": "How much louder one source must be to gate the other.",
        "tooltip": (
            "Controls the source-gating threshold. When one audio source (loopback "
            "or mic) is louder than the other by this ratio, the quieter source is "
            "suppressed entirely.<br><br>"
            "<b>Lower values</b> (e.g. 1.5) gate more aggressively \u2014 good for "
            "strong echo but may cut off overlapping speech.<br>"
            "<b>Higher values</b> (e.g. 4.0) are more permissive, allowing both "
            "sources through when levels are closer."
        ),
        "min": 1.2,
        "max": 6.0,
        "step": 0.1,
        "type": "number",
    },
    "echo_silence_floor": {
        "value": 0.005,
        "label": "Silence Floor",
        "description": "RMS level below which a source is treated as silent.",
        "tooltip": (
            "Audio sources with an RMS energy below this threshold are considered "
            "silent and ignored during source gating. Raising this helps reject "
            "low-level echo bleed that sits just above the noise floor.<br><br>"
            "<b>Lower values</b> are more sensitive \u2014 very quiet signals still "
            "count as active.<br>"
            "<b>Higher values</b> reject more background noise and faint echo, "
            "but may suppress quiet speakers."
        ),
        "min": 0.001,
        "max": 0.05,
        "step": 0.001,
        "type": "number",
    },
    "echo_spectral_sub": {
        "value": 0.6,
        "label": "Spectral Subtraction",
        "description": "Strength of frequency-domain echo removal from mic.",
        "tooltip": (
            "When the loopback (speaker) audio is active, its frequency spectrum "
            "is subtracted from the microphone signal to remove echo. This targets "
            "the specific frequencies being played through the speakers.<br><br>"
            "<b>Higher values</b> remove more echo but may introduce artifacts or "
            "thin out the local speaker's voice.<br>"
            "<b>Lower values</b> are gentler, preserving voice quality but leaving "
            "more residual echo.<br>"
            "<b>0</b> disables spectral subtraction entirely (gating only)."
        ),
        "min": 0.0,
        "max": 1.5,
        "step": 0.05,
        "type": "number",
    },
    "echo_hold_ms": {
        "value": 150,
        "label": "Gate Hold Time",
        "unit": "ms",
        "description": "How long gating persists after the dominant source drops.",
        "tooltip": (
            "After the loopback audio drops below the gating threshold, the mic "
            "remains suppressed for this many milliseconds. This catches the tail "
            "end of echo that lingers after the speaker stops.<br><br>"
            "<b>Longer hold times</b> catch more echo tail but add latency before "
            "the local speaker's mic reopens.<br>"
            "<b>Shorter hold times</b> reopen the mic faster, reducing perceived "
            "delay but risking echo bleed on the trailing edge."
        ),
        "min": 0,
        "max": 500,
        "step": 10,
        "type": "int",
    },
    "echo_crossfade_ms": {
        "value": 30,
        "label": "Crossfade Duration",
        "unit": "ms",
        "description": "Smooth transition time when switching between sources.",
        "tooltip": (
            "When the gate switches from one source to another, a short crossfade "
            "prevents audible clicks and pops. Longer crossfades sound smoother "
            "but may briefly mix both sources.<br><br>"
            "<b>10\u201330 ms</b> is typical for clean transitions.<br>"
            "<b>0 ms</b> gives instant hard switching (may cause clicks)."
        ),
        "min": 0,
        "max": 100,
        "step": 5,
        "type": "int",
    },
    "echo_mic_suppress_db": {
        "value": -18,
        "label": "Mic Suppression",
        "unit": "dB",
        "description": "Attenuation applied to mic when loopback is dominant.",
        "tooltip": (
            "Instead of fully muting the mic during loopback-dominant periods, "
            "this controls how much the mic signal is attenuated. Allows some "
            "local speech to bleed through even during active echo suppression.<br><br>"
            "<b>\u221224 dB or lower</b> is near-total suppression (strongest echo "
            "rejection).<br>"
            "<b>\u22126 dB</b> allows significant mic passthrough (minimal echo "
            "rejection).<br>"
            "<b>0 dB</b> disables suppression entirely (mic always at full level)."
        ),
        "min": -30,
        "max": 0,
        "step": 1,
        "type": "int",
    },
}


def get_all_defaults() -> dict:
    """Return a flat dict of param_name -> default_value for all parameters."""
    flat = {}
    for d in (TRANSCRIPTION_DEFAULTS, DIARIZATION_DEFAULTS,
              ECHO_CANCELLATION_DEFAULTS):
        for key, spec in d.items():
            flat[key] = spec["value"]
    return flat


def get_default(key: str):
    """Return the default value for a single parameter, or None."""
    for d in (TRANSCRIPTION_DEFAULTS, DIARIZATION_DEFAULTS,
              ECHO_CANCELLATION_DEFAULTS):
        if key in d:
            return d[key]["value"]
    return None
