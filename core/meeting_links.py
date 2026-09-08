"""Join links: find one in a calendar event, then hand it to the OS.

A published ICS feed carries the join link in whichever field the organizer's
client happened to use, so all three are read: the Exchange-only
``X-MICROSOFT-SKYPETEAMSMEETINGURL`` property, ``LOCATION`` (some Teams and
most Zoom invites put it there), and the ``DESCRIPTION`` body (everything
else). First hit wins in that order, most reliable first.

Two rules shape everything here:

* **A join link is a credential.** Anyone holding it can walk into the
  meeting, exactly like the feed URL itself. It therefore never reaches the
  browser: ``/api/calendar/events`` returns only a provider slug, and
  ``POST /api/calendar/join`` resolves the URL from the cache server-side.
  ``open_link`` is only ever given a URL this module parsed out of the feed,
  never a string a client supplied.
* **The path has to look like a join, not just the host.** An Outlook invite
  is full of same-host links that are not the meeting: "Meeting options",
  ``aka.ms`` help pages, Zoom's download and SIP lines. Every pattern below
  matches a host *and* a join path, so those never get opened.

The desktop app's own URL scheme is tried before the https link, so Join lands
in Teams or Zoom rather than bouncing through a browser tab. A scheme with no
handler registered on this machine is skipped, so the https link is always
there as the fallback and Join never dead-ends.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import webbrowser
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

from core import log as log

# (slug, label, matcher). Order is presentation order only; a description
# holding two providers' links is not a thing Outlook produces.
_PROVIDERS: tuple[tuple[str, str, re.Pattern], ...] = (
    ("teams", "Teams", re.compile(
        r"https://(?:[\w.-]+\.)?(?:teams\.microsoft\.(?:com|us)|teams\.live\.com)"
        r"/(?:l/meetup-join/|meet/)\S+", re.I)),
    ("zoom", "Zoom", re.compile(
        r"https://(?:[\w.-]+\.)?zoom\.(?:us|com)"
        r"/(?:j/|s/|w/|my/)\S+", re.I)),
    ("meet", "Google Meet", re.compile(
        r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}\S*", re.I)),
    ("webex", "Webex", re.compile(
        r"https://(?:[\w.-]+\.)?webex\.com"
        r"/(?:[\w.-]+/)?(?:meet/|join/|j\.php)\S+", re.I)),
)

_LABELS = {slug: label for slug, label, _ in _PROVIDERS}

# Outlook wraps URLs in angle brackets and drops them mid-sentence, so the
# match runs on past the link itself. Closing brackets are only trimmed when
# nothing opened them inside the URL.
_TRAILING = ">\"'.,;:!"
_PAIRS = {")": "(", "]": "["}


def label_for(provider: str) -> str:
    """The provider's display name, or a capitalized fallback."""
    return _LABELS.get((provider or "").lower(), (provider or "").title())


def _clean(url: str) -> str:
    """Trim the sentence punctuation an ICS body leaves on the end of a URL."""
    text = (url or "").strip()
    while text:
        tail = text[-1]
        if tail in _TRAILING:
            text = text[:-1]
            continue
        opener = _PAIRS.get(tail)
        if opener and text.count(opener) < text.count(tail):
            text = text[:-1]
            continue
        break
    return text


def _fields(instance) -> list[str]:
    """The event's join-link sources, most reliable first."""
    return [
        str(getattr(instance, "join_url", "") or ""),
        str(getattr(instance, "location", "") or ""),
        str(getattr(instance, "description", "") or ""),
    ]


def find_link(instance) -> dict | None:
    """Return ``{provider, label, url, deep_link}`` for an event, or None.

    ``deep_link`` is the desktop app's own scheme when one can be built for
    this provider, and ``""`` otherwise.
    """
    for text in _fields(instance):
        if not text:
            continue
        for slug, label, pattern in _PROVIDERS:
            found = pattern.search(text)
            if not found:
                continue
            url = _clean(found.group(0))
            if not url:
                continue
            return {
                "provider": slug,
                "label": label,
                "url": url,
                "deep_link": deep_link(slug, url),
            }
    return None


def provider_for(instance) -> str:
    """Just the provider slug, for the redacted event payload."""
    link = find_link(instance)
    return link["provider"] if link else ""


def deep_link(provider: str, url: str) -> str:
    """The desktop client's own URL for a join link, or "" if there isn't one.

    Only the two schemes Microsoft and Zoom document are built. Google Meet
    has no desktop client and Webex has no stable published scheme, so both
    stay on https, which is what their own invites link anyway.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = (parts.netloc or "").lower()
    if provider == "teams":
        # Documented as the https path carried over onto the msteams scheme,
        # which covers the classic /l/meetup-join/ link. The newer short form
        # (teams.microsoft.com/meet/<id>) and personal Teams (teams.live.com)
        # have no published equivalent, so they stay on https and hand off
        # through Teams' own join page. Roughly half a real feed is each.
        if host.endswith(("teams.microsoft.com", "teams.microsoft.us")) \
                and parts.path.startswith("/l/"):
            return urlunsplit(("msteams", "", parts.path, parts.query, parts.fragment))
        return ""
    if provider == "zoom":
        found = re.match(r"^/(?:j|s|w)/(\d+)", parts.path)
        if not found:
            return ""
        args = [f"confno={found.group(1)}"]
        password = (parse_qs(parts.query).get("pwd") or [""])[0]
        if password:
            args.append(f"pwd={quote(password, safe='')}")
        return f"zoommtg://{host}/join?{'&'.join(args)}"
    return ""


def _scheme_registered(scheme: str) -> bool:
    """True when this machine can actually handle ``scheme:`` URLs.

    Windows declares a protocol handler with a ``URL Protocol`` value under
    ``Software\\Classes\\<scheme>``, so that is the check. Elsewhere the launch
    itself reports failure, so assume yes and let the fallback handle it.
    """
    if sys.platform != "win32":
        return True
    try:
        import winreg
    except ImportError:
        return False
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"Software\Classes\{scheme}") as key:
                winreg.QueryValueEx(key, "URL Protocol")
                return True
        except OSError:
            continue
    return False


def _launch(target: str) -> bool:
    """Open one URL with the OS handler. False means nothing handled it."""
    scheme = (urlsplit(target).scheme or "").lower()
    if scheme not in ("http", "https") and not _scheme_registered(scheme):
        return False
    try:
        if sys.platform == "win32":
            os.startfile(target)  # noqa: S606 - a URL parsed out of the feed
            return True
        if sys.platform == "darwin":
            done = subprocess.run(["open", target], capture_output=True, timeout=15)
            return done.returncode == 0
        return bool(webbrowser.open(target))
    except Exception as exc:  # noqa: BLE001 - any launch failure is a fallback
        log.warn("calendar", f"Could not open a {scheme}: link ({type(exc).__name__})")
        return False


def open_link(link: dict) -> dict:
    """Open a link from :func:`find_link`. The app's scheme first, then https.

    Returns ``{ok, opened}`` where ``opened`` is "app" when the desktop client
    took it and "browser" when the https link was used instead.
    """
    if not isinstance(link, dict) or not link.get("url"):
        return {"ok": False, "opened": ""}
    for candidate, kind in ((link.get("deep_link"), "app"), (link.get("url"), "browser")):
        if candidate and _launch(candidate):
            log.info("calendar", f"Opened a {link.get('provider')} join link ({kind})")
            return {"ok": True, "opened": kind}
    return {"ok": False, "opened": ""}
