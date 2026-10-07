"""
Desktop (WASAPI loopback) capture helper (Windows only). Run as a SUBPROCESS.

Why a subprocess: PortAudio scans the audio devices once, when it is first
initialised in a process, and Pa_Initialize is reference counted, so a "fresh"
PyAudio() made while any other instance is alive returns the same cached list
(measured: 0.01 ms, no rescan). The app can never terminate its PortAudio once
a loopback stream has been opened (closing a WASAPI loopback stream calls
ExitProcess), so an in-process device list froze at the first recording and an
output that appeared later (headphones plugged into the jack) could not be
found or opened until the app restarted. 2026-10-06: a Teams call played on
"Headphones (Realtek(R) Audio)"; the render probe saw it, but the app's list
only held "Speakers (Realtek(R) Audio)", the switch found no match, and the
whole call was recorded as Pat alone. A new process initialises PortAudio from
scratch, so every recording and every device switch sees the devices Windows
has right now. Killing this process is also the only safe way to retire a
loopback stream.

Protocol (stdout carries JSON lines, then raw PCM; stdin carries one command):
  1. On start: one JSON line describing every PortAudio device:
       {"ok": true, "wasapi": {"index", "defaultOutputDevice",
        "defaultInputDevice"}, "devices": [<PyAudio device info dicts>]}
  2. The parent writes JSON lines on stdin. {"scan": true} re-initialises
     PortAudio and answers with a new step 1 line (a spare helper is rescanned
     this way when it is put to use). Then:
       {"open": <device index>, "channels": c, "rate": r,
        "frames_per_buffer": f, "chunk": n}
     or closes stdin (or sends {"exit": true}) to quit.
  3. Reply line: {"ok": true, "opened": {...}} or {"ok": false, "error": "..."}.
  4. Raw little-endian int16 PCM, interleaved, `chunk` frames per write,
     until stdin closes, stdout breaks, or the device fails.

Usage: python loopback_child.py           (the protocol above)
       python loopback_child.py --list    (step 1 only, then exit)
"""
import json
import os
import queue
import sys
import threading
import time


def _device_report(pa, pyaudio) -> dict:
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    devices = [pa.get_device_info_by_index(i) for i in range(pa.get_device_count())]
    return {
        "ok": True,
        "wasapi": {
            "index": wasapi.get("index"),
            "defaultOutputDevice": wasapi.get("defaultOutputDevice"),
            "defaultInputDevice": wasapi.get("defaultInputDevice"),
        },
        "devices": devices,
    }


def _emit(out, obj: dict) -> None:
    out.write((json.dumps(obj) + "\n").encode("utf-8"))
    out.flush()


def main() -> None:
    out = sys.stdout.buffer
    try:
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()
        report = _device_report(pa, pyaudio)
    except Exception as e:
        _emit(out, {"ok": False, "error": f"device scan failed: {e}"})
        os._exit(2)

    _emit(out, report)
    if "--list" in sys.argv[1:]:
        # No stream was opened, so terminating PortAudio is safe here.
        try:
            pa.terminate()
        except Exception:
            pass
        os._exit(0)

    # Commands until "open". A spare helper waits here for its turn and is told
    # to "scan" first: with no stream open, PortAudio can be terminated and
    # re-initialised, a fresh device scan in ~0.2 s instead of a new process.
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            os._exit(0)
        try:
            cmd = json.loads(line)
        except Exception as e:
            _emit(out, {"ok": False, "error": f"bad command: {e}"})
            os._exit(2)
        if cmd.get("scan"):
            try:
                pa.terminate()
                pa = pyaudio.PyAudio()
                report = _device_report(pa, pyaudio)
            except Exception as e:
                _emit(out, {"ok": False, "error": f"device scan failed: {e}"})
                os._exit(2)
            _emit(out, report)
            continue
        if cmd.get("exit") or "open" not in cmd:
            os._exit(0)
        break

    chunk = int(cmd.get("chunk") or 512)
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=int(cmd["channels"]),
            rate=int(cmd["rate"]),
            input=True,
            input_device_index=int(cmd["open"]),
            frames_per_buffer=int(cmd["frames_per_buffer"]),
        )
    except Exception as e:
        _emit(out, {"ok": False, "error": str(e)})
        os._exit(3)
    _emit(out, {"ok": True, "opened": {"index": int(cmd["open"]),
                                       "channels": int(cmd["channels"]),
                                       "rate": int(cmd["rate"])}})

    # The parent closing stdin (or dying) ends the capture.
    def _watch_stdin() -> None:
        try:
            while sys.stdin.buffer.readline():
                pass
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_watch_stdin, daemon=True).start()

    # PortAudio reads and pipe writes run on separate threads, so a parent that
    # is briefly slow to drain the pipe never stalls the device read (a stalled
    # read overflows PortAudio's ring buffer and drops audio with no error, the
    # 2026-09-21 popping). ~20 s of backlog, then the oldest chunk goes.
    pending: queue.Queue = queue.Queue(maxsize=2000)

    def _write() -> None:
        try:
            while True:
                parts = [pending.get()]
                # Behind (the parent was briefly slow to read): send what has
                # queued up as one write. The pipe holds one pending write at a
                # time, so with chunk-sized writes an 8-channel 96 kHz output
                # drained at barely over real time: after a one second stall it
                # was still 0.6 s behind the mic eight seconds later (2026-10-07).
                # Batched, it is level again within a second. Up to date, this
                # is one chunk per write as before.
                while len(parts) < 64:
                    try:
                        parts.append(pending.get_nowait())
                    except queue.Empty:
                        break
                out.write(parts[0] if len(parts) == 1 else b"".join(parts))
                out.flush()
        except Exception:
            os._exit(0)   # parent gone or pipe closed

    threading.Thread(target=_write, daemon=True).start()

    errors = 0
    while True:
        try:
            data = stream.read(chunk, exception_on_overflow=False)
            errors = 0
        except Exception as e:
            # The device went away (unplugged, disabled, format change). Exit so
            # the parent sees the pipe close and reopens on a live device.
            errors += 1
            if errors >= 20:
                sys.stderr.write(f"loopback read failed: {e}\n")
                sys.stderr.flush()
                os._exit(4)
            time.sleep(0.01)
            continue
        try:
            pending.put_nowait(data)
        except queue.Full:
            try:
                pending.get_nowait()
            except queue.Empty:
                pass
            pending.put_nowait(data)
    # Never close the stream: closing a WASAPI loopback stream calls ExitProcess.


if __name__ == "__main__":
    main()
