"""Name labels read off a meeting window, to people.

Meeting apps decorate names: "Tom McDonnell (2)" for a second join, "(He/Him)",
"(Guest)", "Collin Murdock - Nationwide", "Ty Lane | Higginbotham", "Lane, Ty".
``normalize`` strips the decoration; ``NameBook`` maps what is left to a known
person (a voice-library profile, a calendar attendee, a name the user gave)
exactly, or fuzzily only within those candidates and only when the first
names agree, so "Chris Johnson" never becomes "Chris Johnston" on spelling
alone.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

_PAREN = re.compile(
    r"\s*\((?:he|she|they|him|her|them|his|hers|theirs|[a-z]+/[a-z]+(?:/[a-z]+)?|guest|host|"
    r"co-host|cohost|me|you|external|presenter|organizer|organiser|\d+)\)\s*", re.I)
# " | Company", " - Company", and the same with a bullet, middle dot or en dash.
_TAIL = re.compile("\\s+(?:[|\u2022\u00b7]|-|\u2013)\\s+.*$")
# A participant chooses their own display name, and it becomes a speaker and
# a voice profile name: no markup or control characters survive, nor length.
_UNSAFE = re.compile(r"[<>\"`{}\\\x00-\x1f\x7f]")
MAX_NAME = 80


def normalize(label: str | None) -> str:
    """A label as a plain name: decoration removed, "Last, First" reordered."""
    if not label:
        return ""
    s = unicodedata.normalize("NFKC", str(label)).replace("’", "'")
    s = _UNSAFE.sub("", s).strip()[:MAX_NAME * 2]
    prev = None
    while prev != s:
        prev = s
        s = _PAREN.sub(" ", s).strip()
    s = _TAIL.sub("", s).strip()
    if s.count(",") == 1:
        last, first = (p.strip() for p in s.split(","))
        if last and first and " " not in last and len(first.split()) <= 2:
            s = f"{first} {last}"
    return re.sub(r"\s+", " ", s).strip(" .-|")[:MAX_NAME].strip()


def key(name: str) -> str:
    """Comparison form: lower case, accents and punctuation gone."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _first(k: str) -> str:
    return k.split(" ", 1)[0] if k else ""


class NameBook:
    """Known people and how raw labels map to them.

    ``people`` maps display name -> voice profile id (or None for a person
    known only from the calendar or the user's words)."""

    def __init__(self, people: dict[str, str | None] | None = None,
                 aliases: dict[str, str] | None = None):
        self.people: dict[str, str | None] = {}
        self._by_key: dict[str, str] = {}
        self._expected: set[str] = set()
        for name, gid in (people or {}).items():
            self.add(name, gid)
        for alias, name in (aliases or {}).items():
            k = key(normalize(alias))
            if k and name in self.people:
                self._by_key.setdefault(k, name)

    def add(self, name: str, gid: str | None = None, *, expected: bool = False) -> None:
        name = (name or "").strip()
        if not name:
            return
        if name not in self.people or (gid and not self.people[name]):
            self.people[name] = gid
        self._by_key.setdefault(key(normalize(name)), name)
        if expected:
            self.expected.add(name)

    @property
    def expected(self) -> set[str]:
        """People the meeting's invite lists."""
        return self._expected

    def _first_name_only(self, k: str) -> str | None:
        """A one-word label ("Caleb") is the one known person with that first
        name, or, when several share it, the one the invite expects; else
        nobody."""
        hits = [n for ck, n in self._by_key.items() if " " in ck and _first(ck) == k]
        hits = sorted(set(hits))
        if len(hits) == 1:
            return hits[0]
        expected = [n for n in hits if n in self.expected]
        return expected[0] if len(expected) == 1 else None

    def resolve(self, label: str | None) -> tuple[str | None, str | None, bool]:
        """(person name, profile id, known) for a label read on screen. An
        unknown label comes back normalized, with known False."""
        n = normalize(label)
        k = key(n)
        if not k:
            return None, None, False
        if k in self._by_key:
            name = self._by_key[k]
            return name, self.people.get(name), True
        if " " not in k:
            name = self._first_name_only(k)
            if name:
                return name, self.people.get(name), True
        first = _first(k)
        best, best_score = None, 0.0
        for ck, name in self._by_key.items():
            if _first(ck) != first:
                continue
            if len(k) >= 6 and (ck.startswith(k) or k.startswith(ck)):
                score = 0.95       # a label cut off by the tile edge
            else:
                score = difflib.SequenceMatcher(None, k, ck).ratio()
            if score > best_score:
                best, best_score = name, score
        if best is not None and best_score >= 0.88:
            return best, self.people.get(best), True
        return n, None, False

    def same(self, a: str | None, b: str | None) -> bool:
        return bool(a and b and key(normalize(a)) == key(normalize(b)))
