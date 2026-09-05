"""
Render-endpoint probe (Windows only). Run as a SUBPROCESS, never in-process.

Reports every ACTIVE render (output) endpoint with:
  - its friendly name,
  - the max peak-meter level observed over a short sampling window
    (IAudioMeterInformation - i.e. is audio ACTUALLY playing there), and
  - whether it is the default Multimedia and/or default Communications device.

Why a subprocess: in-process COM (pycaw/comtypes) alongside live WASAPI capture
and windows-toasts has caused native access-violation crashes in this app
before. This script keeps all COM strictly out-of-process; the app runs it via
subprocess and parses the JSON on stdout.

Why it exists: PortAudio only exposes the Console/Multimedia default output,
but call apps (Teams, Zoom) render to the default COMMUNICATIONS device - a
separate Windows role - or to a device picked inside the app that is neither
default. Following "the default output" therefore captures a silent endpoint
whenever those differ (the 2026-09-01 one-sided-call failure). The peak meter
is the only signal that says where audio is truly playing.

Usage: python render_probe.py [--duration 1.2] [--interval 0.05]
Output: one JSON object on stdout:
  {"ok": true, "duration": 1.2,
   "endpoints": [{"name": "...", "peak": 0.31,
                  "is_default": true, "is_default_communications": false}]}
On failure: {"ok": false, "error": "..."} (exit code still 0 so callers only
parse, never raise).
"""
import argparse
import json
import sys
import time


def _probe(duration: float, interval: float) -> dict:
    import comtypes
    from ctypes import POINTER, cast

    from pycaw.pycaw import AudioUtilities
    from pycaw.api.endpointvolume import IAudioMeterInformation

    CLSCTX_ALL = 23
    E_RENDER = 0            # EDataFlow.eRender
    ROLE_MULTIMEDIA = 1     # ERole.eMultimedia (PortAudio's "default output")
    ROLE_COMMUNICATIONS = 2  # ERole.eCommunications (what call apps use)
    DEVICE_STATE_ACTIVE = 1

    comtypes.CoInitialize()
    enumerator = AudioUtilities.GetDeviceEnumerator()

    def _default_id(role: int) -> str | None:
        try:
            dev = enumerator.GetDefaultAudioEndpoint(E_RENDER, role)
            return dev.GetId()
        except Exception:
            return None

    default_mm_id = _default_id(ROLE_MULTIMEDIA)
    default_comm_id = _default_id(ROLE_COMMUNICATIONS)

    collection = enumerator.EnumAudioEndpoints(E_RENDER, DEVICE_STATE_ACTIVE)
    count = collection.GetCount()

    endpoints = []
    for i in range(count):
        dev = collection.Item(i)
        try:
            dev_id = dev.GetId()
        except Exception:
            continue
        try:
            name = AudioUtilities.CreateDevice(dev).FriendlyName or dev_id
        except Exception:
            name = dev_id
        meter = None
        try:
            iface = dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
            meter = cast(iface, POINTER(IAudioMeterInformation))
        except Exception:
            pass
        endpoints.append({
            "id": dev_id,
            "name": name,
            "peak": 0.0,
            "is_default": dev_id == default_mm_id,
            "is_default_communications": dev_id == default_comm_id,
            "_meter": meter,
        })

    # Sample all meters across the window, keeping the max peak per endpoint,
    # so a brief speech pause inside the window does not read as "silent".
    deadline = time.monotonic() + max(0.1, duration)
    while time.monotonic() < deadline:
        for ep in endpoints:
            m = ep["_meter"]
            if m is None:
                continue
            try:
                ep["peak"] = max(ep["peak"], float(m.GetPeakValue()))
            except Exception:
                ep["_meter"] = None
        time.sleep(max(0.01, interval))

    for ep in endpoints:
        ep.pop("_meter", None)
        ep["peak"] = round(ep["peak"], 5)

    return {"ok": True, "duration": duration, "endpoints": endpoints}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=1.2)
    ap.add_argument("--interval", type=float, default=0.05)
    args = ap.parse_args()
    try:
        result = _probe(args.duration, args.interval)
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
