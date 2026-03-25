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
        "description": "Remove speaker echo from the microphone signal.",
        "tooltip": (
            "Uses WebRTC AEC (Acoustic Echo Cancellation) to remove desktop "
            "speaker audio that bleeds into the microphone. This is the same "
            "echo canceller used in Chrome and other browsers.<br><br>"
            "<b>Leave disabled</b> if you use headphones or a headset \u2014 echo "
            "cancellation is unnecessary.<br>"
            "<b>Enable</b> if you hear duplicated transcriptions caused by the mic "
            "picking up speaker output."
        ),
        "min": 0,
        "max": 1,
        "step": 1,
        "type": "toggle",
    },
}


SCREEN_RECORDING_DEFAULTS = {
    "screen_record_enabled": {
        "value": 0,
        "label": "Enable Screen Recording",
        "description": "Record the selected display during meetings.",
        "tooltip": (
            "Captures your screen using FFmpeg and saves it as an MP4 file "
            "alongside the audio recording. The video is encoded with H.264 "
            "for broad compatibility.<br><br>"
            "<b>Enable</b> to record your screen during meetings.<br>"
            "<b>Leave disabled</b> to save system resources when video isn't needed."
        ),
        "min": 0,
        "max": 1,
        "step": 1,
        "type": "toggle",
    },
    "screen_framerate": {
        "value": 10,
        "label": "Framerate",
        "unit": "fps",
        "description": "Capture frames per second.",
        "tooltip": (
            "How many frames per second to capture from the display. Screen "
            "content is mostly static, so low framerates work well.<br><br>"
            "<b>5–10 fps</b> is ideal for presentations and documents — minimal "
            "CPU usage and small files.<br>"
            "<b>15–24 fps</b> is smooth enough for video playback and demos.<br>"
            "<b>30 fps</b> produces very smooth video but significantly larger files."
        ),
        "min": 1,
        "max": 60,
        "step": 1,
        "type": "int",
    },
    "screen_crf": {
        "value": 32,
        "label": "Quality (CRF)",
        "description": "Constant Rate Factor — lower is better quality.",
        "tooltip": (
            "Controls the quality-vs-size tradeoff for H.264 encoding. CRF "
            "uses a perceptual quality model — the encoder adjusts bitrate "
            "automatically to maintain constant visual quality.<br><br>"
            "<b>18–22</b>: Visually lossless — excellent quality, large files.<br>"
            "<b>23–28</b>: Good quality — text is sharp, moderate file size.<br>"
            "<b>29–35</b>: Acceptable quality — some softness, small files.<br>"
            "<b>36+</b>: Low quality — blurry details, very small files.<br><br>"
            "Each +6 roughly halves the file size."
        ),
        "min": 0,
        "max": 51,
        "step": 1,
        "type": "int",
    },
    "screen_h264_preset": {
        "value": 2,
        "label": "Encoder Speed",
        "description": "H.264 preset — faster encoding uses more disk, less CPU.",
        "tooltip": (
            "The H.264 preset controls the trade-off between encoding speed "
            "and compression efficiency. All presets produce the same visual "
            "quality at a given CRF — faster presets just use more bits.<br><br>"
            "<b>0 (ultrafast)</b>: Minimal CPU, ~2× file size vs medium.<br>"
            "<b>2 (veryfast)</b>: Low CPU, good compression. Recommended.<br>"
            "<b>4 (fast)</b>: Moderate CPU, efficient compression.<br>"
            "<b>5 (medium)</b>: FFmpeg default — balanced but heavier.<br>"
            "<b>7+ (slow–veryslow)</b>: Maximum compression, high CPU."
        ),
        "min": 0,
        "max": 8,
        "step": 1,
        "type": "int",
    },
    "screen_scale_width": {
        "value": 0,
        "label": "Downscale Width",
        "unit": "px",
        "description": "Scale video width (0 = native resolution).",
        "tooltip": (
            "Downscale the captured video to this width (height adjusts "
            "automatically to maintain aspect ratio). Useful on high-DPI "
            "displays to reduce file size.<br><br>"
            "<b>0</b>: Native resolution (no scaling).<br>"
            "<b>1920</b>: Full HD — good for 4K displays.<br>"
            "<b>1280</b>: 720p — small files, still readable text.<br><br>"
            "Values below 960 may make small text difficult to read."
        ),
        "min": 0,
        "max": 7680,
        "step": 160,
        "type": "int",
    },
}

# ── Transcription Presets ─────────────────────────────────────────────────────

TRANSCRIPTION_PRESETS = {
    "responsive": {
        "label": "Responsive",
        "description": "Fast updates, shorter segments — ideal for live captioning",
        "values": {
            "silence_threshold": 0.03,
            "silence_duration": 0.2,
            "min_buffer_seconds": 0.3,
            "max_buffer_seconds": 6.0,
            "beam_size": 1,
            "prompt_chars": 400,
            "vad_min_silence_ms": 200,
            "vad_speech_pad_ms": 100,
            "compression_ratio_threshold": 2.0,
        },
    },
    "balanced": {
        "label": "Balanced (Default)",
        "description": "Good accuracy with reasonable latency — recommended for most meetings",
        "values": {
            "silence_threshold": 0.025,
            "silence_duration": 0.3,
            "min_buffer_seconds": 0.5,
            "max_buffer_seconds": 10.0,
            "beam_size": 2,
            "prompt_chars": 800,
            "vad_min_silence_ms": 300,
            "vad_speech_pad_ms": 150,
            "compression_ratio_threshold": 2.0,
        },
    },
    "accurate": {
        "label": "Accurate",
        "description": "Higher accuracy with longer context — more latency, better results",
        "values": {
            "silence_threshold": 0.02,
            "silence_duration": 0.5,
            "min_buffer_seconds": 1.0,
            "max_buffer_seconds": 15.0,
            "beam_size": 4,
            "prompt_chars": 1200,
            "vad_min_silence_ms": 400,
            "vad_speech_pad_ms": 200,
            "compression_ratio_threshold": 2.2,
        },
    },
    "quality": {
        "label": "Quality",
        "description": "Maximum accuracy — significant latency, best for post-processing",
        "values": {
            "silence_threshold": 0.015,
            "silence_duration": 0.7,
            "min_buffer_seconds": 1.5,
            "max_buffer_seconds": 20.0,
            "beam_size": 5,
            "prompt_chars": 1600,
            "vad_min_silence_ms": 500,
            "vad_speech_pad_ms": 250,
            "compression_ratio_threshold": 2.4,
        },
    },
    "custom": {
        "label": "Custom",
        "description": "Manually configure all parameters",
        "values": {},
    },
}

TRANSCRIPTION_DEFAULT_PRESET = "balanced"


# ── Diarization Presets ──────────────────────────────────────────────────────

DIARIZATION_PRESETS = {
    "responsive": {
        "label": "Responsive",
        "description": "Fast speaker detection — may over-segment in noisy environments",
        "values": {
            "step_seconds": 0.15,
            "duration_seconds": 4.0,
            "tau_active": 0.4,
            "rho_update": 0.5,
            "delta_new": 0.4,
            "merge_gap_seconds": 0.05,
        },
    },
    "balanced": {
        "label": "Balanced (Default)",
        "description": "Good speaker tracking for typical meetings — recommended",
        "values": {
            "step_seconds": 0.25,
            "duration_seconds": 5.0,
            "tau_active": 0.5,
            "rho_update": 0.422,
            "delta_new": 0.5,
            "merge_gap_seconds": 0.1,
        },
    },
    "conservative": {
        "label": "Conservative",
        "description": "Fewer false speaker changes — better for small groups",
        "values": {
            "step_seconds": 0.35,
            "duration_seconds": 5.0,
            "tau_active": 0.6,
            "rho_update": 0.35,
            "delta_new": 0.7,
            "merge_gap_seconds": 0.2,
        },
    },
    "large_meeting": {
        "label": "Large Meeting",
        "description": "Tuned for 5+ speakers — sensitive detection, stable centroids",
        "values": {
            "step_seconds": 0.2,
            "duration_seconds": 5.0,
            "tau_active": 0.45,
            "rho_update": 0.3,
            "delta_new": 0.35,
            "merge_gap_seconds": 0.15,
        },
    },
    "custom": {
        "label": "Custom",
        "description": "Manually configure all parameters",
        "values": {},
    },
}

DIARIZATION_DEFAULT_PRESET = "balanced"


_ALL_DEFAULTS_DICTS = (
    TRANSCRIPTION_DEFAULTS, DIARIZATION_DEFAULTS,
    ECHO_CANCELLATION_DEFAULTS, SCREEN_RECORDING_DEFAULTS,
)


def get_all_defaults() -> dict:
    """Return a flat dict of param_name -> default_value for all parameters."""
    flat = {}
    for d in _ALL_DEFAULTS_DICTS:
        for key, spec in d.items():
            flat[key] = spec["value"]
    return flat


def get_default(key: str):
    """Return the default value for a single parameter, or None."""
    for d in _ALL_DEFAULTS_DICTS:
        if key in d:
            return d[key]["value"]
    return None
