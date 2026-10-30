"""
smart_search.py — understand what a teacher types into the search box.

The same algorithm lives in LogicShare Android (SmartSearch.kt), and both are
held to one set of cases in LogicShare/shared/smart_search_cases.json, so a
query means the same thing on a laptop and on a phone.

What a teacher actually types for "the board in 11-A, room 12":

    11a   11-A   11 a   class 11 A   XI A   11th A   eleventh a
    std 11 sec a   clas 11 a   room 12   rm12   r 12   12

None of those is a substring of "Class 11-A · Room 12", which is why a plain
text filter fails. So the query is PARSED into constraints first:

    class numbers   from "class 11", "11th", "XI", "eleven", or a number
                    written straight against a section letter ("11a")
    sections        single letters ("a"), or after "sec"/"section"/"div"
    rooms           after "room"/"rm"/"r" ("rm12", "r 12")
    ambiguous nums  a bare number: class OR room ("12")
    ip fragment     anything with dots between digits ("29.127")
    words           everything else — free text, typo-tolerant

and a board matches only if it satisfies ALL of them. Keywords tolerate one
typo ("clas", "romm", "secton"), because they are typed on phones.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

__all__ = ["Query", "parse", "match_board", "match_history", "normalize"]

_CLASS_WORDS = {"class", "cls", "std", "standard", "grade", "gr", "klass"}
_ROOM_WORDS = {"room", "rm", "roomno"}
_SECTION_WORDS = {"section", "sec", "div", "division"}
_STOP = {"board", "boards", "smart", "the", "of", "in", "for", "no", "number",
         "and", "to", "a_"}
# Keywords long enough that one typo is still unambiguous.
_FUZZY_KEYWORDS = {"class": "class", "standard": "class", "grade": "class",
                   "room": "room", "section": "section", "division": "section"}
_ORDINAL_SUFFIX = {"st", "nd", "rd", "th"}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
          "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12,
}

_IP_RE = re.compile(r"\d{1,3}(?:\.\d{1,3})+\.?|\.\d{1,3}(?:\.\d{1,3})*\.?")


def normalize(text: str) -> str:
    """Lower-case and strip accents ("Émile" -> "emile"), keeping every
    script intact — a Hindi announcement must stay searchable in Hindi."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text.lower()


def _edit1(a: str, b: str) -> bool:
    """True if a and b are within one insert, delete, substitute or
    transposition of each other."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return (len(diff) == 2 and diff[1] == diff[0] + 1
                and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    if la > lb:
        a, b = b, a
    i = j = 0
    skipped = False
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            if skipped:
                return False
            skipped = True
            j += 1
        else:
            i += 1
            j += 1
    return True


def _keyword(tok: str) -> str:
    if tok in _CLASS_WORDS:
        return "class"
    if tok in _ROOM_WORDS:
        return "room"
    if tok in _SECTION_WORDS:
        return "section"
    if len(tok) >= 4:
        for kw, kind in _FUZZY_KEYWORDS.items():
            if _edit1(tok, kw):
                return kind
    return ""


def _number(tok: str):
    if tok.isdigit():
        return str(int(tok))
    if tok in _WORD_NUMBERS:
        return str(_WORD_NUMBERS[tok])
    if len(tok) >= 2 and tok in _ROMAN:
        return str(_ROMAN[tok])
    return None


def _norm_value(v: str) -> str:
    """A board field compared on equal terms: "011" -> "11", "XI" -> "11",
    "A" -> "a"."""
    v = normalize(str(v or "")).strip()
    n = _number(v)
    return n if n is not None else v


@dataclass
class Query:
    classes: list = field(default_factory=list)
    sections: list = field(default_factory=list)
    rooms: list = field(default_factory=list)
    numbers: list = field(default_factory=list)     # class OR room
    words: list = field(default_factory=list)
    ip: str = ""

    @property
    def empty(self) -> bool:
        return not (self.classes or self.sections or self.rooms
                    or self.numbers or self.words or self.ip)

    def to_dict(self) -> dict:
        return {"classes": self.classes, "sections": self.sections,
                "rooms": self.rooms, "numbers": self.numbers,
                "words": self.words, "ip": self.ip}


def parse(query: str) -> Query:
    q = Query()
    s = normalize(query)

    ip = _IP_RE.search(s)
    if ip:
        q.ip = ip.group(0).strip(".")
        s = s[:ip.start()] + " " + s[ip.end():]

    s = re.sub(r"[-_/,;:·|()#.'\"]+", " ", s)
    # "11a" -> "11 a", "rm12" -> "rm 12"; remember which pairs were glued,
    # because "11a" is far stronger evidence of class+section than "11 a".
    glued = set()
    for m in re.finditer(r"(\d+)([a-z]+)|([a-z]+)(\d+)", s):
        glued.add(m.group(0))
    s = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", s)
    toks = s.split()

    pending = ""            # a keyword waiting for its value
    last_num_idx = None     # index in q.numbers of a bare number just seen
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""

        # Ordinal suffix straight after a number: "11th" -> 11, and a number
        # written as an ordinal is a class ("11th a", "eleventh").
        if t in _ORDINAL_SUFFIX and i > 0 and _number(toks[i - 1]) is not None:
            if last_num_idx is not None:
                q.classes.append(q.numbers.pop(last_num_idx))
                last_num_idx = None
            i += 1
            continue

        # A lone "r" is the room keyword only when a number follows it;
        # otherwise it is section R.
        kw = _keyword(t)
        if t == "r" and _number(nxt) is not None:
            kw = "room"
        if kw:
            pending = kw
            last_num_idx = None
            i += 1
            continue

        num = _number(t)
        if num is not None:
            if pending == "class":
                q.classes.append(num); last_num_idx = None
            elif pending == "room":
                q.rooms.append(num); last_num_idx = None
            elif pending == "section":
                q.sections.append(t); last_num_idx = None
            elif t in _WORD_NUMBERS and t.endswith(("th", "st", "nd", "rd")) \
                    and t not in ("one", "two", "three"):
                q.classes.append(num); last_num_idx = None     # "eleventh"
            elif t in _ROMAN:
                q.classes.append(num); last_num_idx = None     # "XI"
            else:
                q.numbers.append(num); last_num_idx = len(q.numbers) - 1
            pending = ""
            i += 1
            continue

        if len(t) == 1 and t.isalpha() and t.isascii():
            q.sections.append(t)
            # A number right before a section letter is a class: "11 a".
            if last_num_idx is not None:
                q.classes.append(q.numbers.pop(last_num_idx))
            last_num_idx = None
            pending = ""
            i += 1
            continue

        if pending == "section":
            q.sections.append(t)
        elif t not in _STOP:
            q.words.append(t)
        pending = ""
        last_num_idx = None
        i += 1

    del glued   # informative only; kept for symmetry with the Kotlin port
    return q


def _room_matches(want: str, room: str) -> bool:
    r = _norm_value(room)
    if r == want:
        return True
    m = re.match(r"\d+", r)
    return bool(m and str(int(m.group(0))) == want)


def _text_has(word: str, haystack: str) -> bool:
    if word in haystack:
        return True
    if len(word) >= 4:
        return any(_edit1(word, w) for w in re.split(r"[^\w]+", haystack) if w)
    return False


def match_board(q: Query, cls: str, section: str, room: str, ip: str) -> bool:
    """Does one board satisfy every constraint in the query?"""
    if q.empty:
        return True
    c, sec = _norm_value(cls), _norm_value(section)
    if q.ip and q.ip not in (ip or ""):
        return False
    if any(c != v for v in q.classes):
        return False
    if any(sec != v for v in q.sections):
        return False
    if any(not _room_matches(v, room) for v in q.rooms):
        return False
    if any(c != v and not _room_matches(v, room) for v in q.numbers):
        return False
    hay = normalize(f"class {cls} {section} {cls}{section} room {room} {ip}")
    return all(_text_has(w, hay) for w in q.words)


def match_history(q: Query, title: str, message: str, recipients) -> bool:
    """One history entry. ``recipients`` is an iterable of
    (class, section, room) tuples.

    Board constraints must be met by at least one recipient. A bare number
    may instead appear in the text ("Exam on 12"), and every free word must
    be in the title, the message, or a recipient's label."""
    if q.empty:
        return True
    recips = list(recipients or [])
    text = normalize(f"{title} {message}")

    def recip_ok(r):
        rc, rs, rr = r
        c, s = _norm_value(rc), _norm_value(rs)
        return (all(c == v for v in q.classes)
                and all(s == v for v in q.sections)
                and all(_room_matches(v, rr) for v in q.rooms))

    if q.classes or q.sections or q.rooms:
        if not any(recip_ok(r) for r in recips):
            return False
    for n in q.numbers:
        in_recips = any(_norm_value(r[0]) == n or _room_matches(n, r[2]) for r in recips)
        if not in_recips and not re.search(rf"(?<!\d){re.escape(n)}(?!\d)", text):
            return False
    labels = normalize(" ".join(f"class {r[0]} {r[1]} {r[0]}{r[1]} room {r[2]}" for r in recips))
    return all(_text_has(w, text) or _text_has(w, labels) for w in q.words)
