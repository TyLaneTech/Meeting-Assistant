"""Show the notifications on this desktop, to look at them.

    python -m ui_desktop.toast            every kind, one after another
    python -m ui_desktop.toast prompt     one kind
"""
from __future__ import annotations

import sys
import time

from ui_desktop import toast

DEMOS = {
    "prompt": dict(title="Zoom meeting detected", body="Want to record and transcribe it?",
                   icon="video", tag="meeting",
                   actions=[toast.Action("Start recording"), toast.Action("Not now")]),
    "recording": dict(title="Recording Teams meeting",
                      body="Auto-started recording and transcription. Click to open.",
                      actions=[toast.Action("Open"), toast.Action("Stop recording", style="danger")]),
    "warning": dict(title="Call audio not captured",
                    body="Call/desktop audio is NOT being captured. Check that the call is playing "
                         "to your current Windows output device (the one you hear it on)."),
    "error": dict(title="Meeting Assistant is NOT recording",
                  body="Automatic start did not go through. Open the app and press Record.",
                  actions=[toast.Action("Open the app")]),
    "success": dict(title="Transcript ready",
                    body="Weekly sync with the platform team (42 min) is summarised."),
    "info": dict(title="Recording stopped", icon="circle-stop"),
}


def main(argv: list[str]) -> int:
    kinds = argv or list(DEMOS)
    handles = []
    for kind in kinds:
        demo = dict(DEMOS[kind])
        demo["on_click"] = lambda arg, k=kind: print(f"[{k}] body clicked")
        for a in demo.get("actions", []):
            a.on_click = lambda arg, k=kind: print(f"[{k}] action: {arg}")
        demo["on_dismiss"] = lambda reason, k=kind: print(f"[{k}] dismissed: {reason}")
        handles.append(toast.show(kind=kind, **demo))
        time.sleep(0.9)
    if not toast.is_supported():
        print("Toasts are not supported here.")
        return 1
    toast.flush(120)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
