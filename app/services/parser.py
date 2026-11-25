"""Heuristic multiple-choice / open question extraction from plain text.

Handles numbered questions ("1.", "Q1)", "Question 3", "Vraag 4"), lettered options ("A.", "b)", "(C)"),
markdown checkboxes, "Answer:"/"Antwoord:" lines, "Explanation:"/"Uitleg:" lines and a trailing answer key.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .extract import clean_markdown

LETTERS = "ABCDEFGH"
IMG_LINE_RE = re.compile(r"^\s*\u27e6img:([A-Za-z0-9_]+)\u27e7\s*$")

Q_RE = re.compile(r"^\s*(?:(?:Q|Question|Vraag)\s*\.?\s*)?(?P<n>\d{1,4})\s*[\).:\-]\s+(?P<t>\S.*)$", re.I)
Q_HEAD_RE = re.compile(r"^\s*(?:Question|Vraag|Q)\s*\.?\s*(?P<n>\d{1,4})\s*[:.\-)]?\s*$", re.I)
OPT_RE = re.compile(r"^\s*(?:[-*+]\s+)?\(?(?P<l>[A-Ha-h])\s*[\).:]\s*(?P<t>\S.*)$")
CHK_RE = re.compile(r"^\s*[-*+]\s*\[(?P<c>[ xX])\]\s*(?P<t>\S.*)$")
ANS_RE = re.compile(
    r"^\s*(?:the\s+)?(?:correct\s+|juiste\s+)?(?:answers?|antwoord(?:en)?|ans|key)\s*(?:is|are)?\s*[:=\-–]\s*(?P<a>.+)$",
    re.I,
)
EXPL_RE = re.compile(r"^\s*(?:explanation|rationale|reason|uitleg|toelichting|why)\s*[:\-–]\s*(?P<t>.*)$", re.I)
KEY_HEAD_RE = re.compile(
    r"^\W*(?:answer\s*key|answers|correct\s+answers|solutions|antwoorden(?:lijst|sleutel)?)\W*$", re.I
)
KEY_LINE_RE = re.compile(
    r"^\s*(?:Q\.?\s*)?(?P<n>\d{1,4})\s*[\).:\-–]?\s*(?P<a>[A-Ha-h](?:\s*[,;&/+]\s*[A-Ha-h])*|[A-H]{2,8})\b.*$"
)
KEY_PAIR_RE = re.compile(r"(\d{1,4})\s*[-–.:)]\s*([A-Ha-h](?:\s*,\s*[A-Ha-h])*)\b")
LETTER_LIST_RE = re.compile(
    r"^\(?(?P<l>[A-Ha-h](?:\s*(?:,|;|&|/|\+|and|en)\s*[A-Ha-h])*|[A-H]{1,8})\)?(?=[\s.\):\-,;]|$)"
)


@dataclass
class ParsedQuestion:
    text: str = ""
    options: list[str] = field(default_factory=list)
    correct: list[int] = field(default_factory=list)
    explanation: str = ""
    answer_text: str = ""
    number: int | None = None
    qtype: str = "mc"
    checked: list[bool] = field(default_factory=list)
    images: list[str] = field(default_factory=list)  # picture keys (see extract.Doc)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "options": self.options,
            "correct": self.correct,
            "explanation": self.explanation,
            "answer_text": self.answer_text,
            "qtype": self.qtype,
            "images": self.images,
        }


def fingerprint(text: str, options: list[str], image_shas: list[str] | None = None) -> str:
    norm = (
        re.sub(r"\W+", " ", text.lower()).strip()
        + "|"
        + "|".join(sorted(re.sub(r"\W+", " ", o.lower()).strip() for o in options))
    )
    if image_shas:  # the same wording with a different picture is a different question
        norm += "|img:" + ",".join(sorted(image_shas))
    return hashlib.sha1(norm.encode()).hexdigest()


def letters_to_idx(raw: str, n_options: int) -> list[int]:
    idx = sorted({LETTERS.index(c.upper()) for c in re.findall(r"[A-Ha-h]", raw)})
    return [i for i in idx if i < n_options] if n_options else idx


def _split_key(lines: list[str]) -> tuple[list[str], dict[int, str]]:
    heads = [i for i, ln in enumerate(lines) if KEY_HEAD_RE.match(ln)]
    for h in reversed(heads):
        key: dict[int, str] = {}
        tail = [ln for ln in lines[h + 1 :] if ln.strip()]
        for ln in tail:
            m = KEY_LINE_RE.match(ln)
            if m:
                key[int(m["n"])] = m["a"]
                continue
            pairs = KEY_PAIR_RE.findall(ln)
            if len(pairs) >= 2:
                for n, a in pairs:
                    key[int(n)] = a
        if len(key) >= 2 or (key and len(key) == len(tail)):
            return lines[:h], key
    return lines, {}


INLINE_ANS_RE = re.compile(
    r"\s+(?=(?:correct\s+|juiste\s+)?(?:answers?|antwoord(?:en)?)\s*(?:is|are)?\s*[:=]\s*\S)", re.I
)
INLINE_NEXT_RE = re.compile(
    r"((?:answers?|antwoord(?:en)?)\s*[:=]\s*[A-Ha-h](?:\s*[,;&/]\s*[A-Ha-h])*)\s+(?=(?:Q|Question|Vraag)?\s*\d{1,4}\s*[\).:]\s+\S)",
    re.I,
)
INLINE_OPT_RE = re.compile(r"(?:(?<=\s)|^)\(?([A-Ha-h])[\).]\s+(?=\S)")


def _explode_inline(line: str) -> list[str]:
    """Split lines where PDF/Word extraction glued options, answers or the next question together."""
    if len(line) < 40:
        return [line]
    line = INLINE_NEXT_RE.sub(lambda m: m.group(1) + "\n", line)
    parts: list[str] = []
    for chunk in line.split("\n"):
        chunk = INLINE_ANS_RE.sub("\n", chunk) if not ANS_RE.match(chunk.strip()) else chunk
        for piece in chunk.split("\n"):
            marks = [m for m in INLINE_OPT_RE.finditer(piece)]
            # accept only a run that starts at A and continues B, C, ... (avoids splitting normal prose)
            run: list[re.Match] = []
            for m in marks:
                want = LETTERS[len(run)] if len(run) < len(LETTERS) else ""
                if m.group(1).upper() == want:
                    run.append(m)
            if len(run) >= 2:
                cuts = [m.start() for m in run]
                if cuts[0] > 0:
                    parts.append(piece[: cuts[0]].rstrip())
                for a, b in zip(cuts, cuts[1:] + [len(piece)], strict=True):
                    parts.append(piece[a:b].strip())
            else:
                parts.append(piece)
    return [p for p in parts if p.strip()]


def _finish(q: ParsedQuestion | None, out: list[ParsedQuestion]) -> None:
    if not q:
        return
    q.text = re.sub(r"\s+", " ", q.text).strip()
    q.options = [re.sub(r"\s+", " ", o).strip() for o in q.options]
    q.explanation = re.sub(r"[ \t]+", " ", q.explanation).strip()
    q.answer_text = q.answer_text.strip()
    if len(q.options) >= 2:
        q.qtype = "mc"
        if q.checked and any(q.checked) and not q.correct:
            q.correct = [i for i, c in enumerate(q.checked) if c]
        if q.answer_text and not q.correct:
            letters = LETTER_LIST_RE.match(q.answer_text)
            if letters:
                q.correct = letters_to_idx(letters["l"], len(q.options))
            else:
                for i, o in enumerate(q.options):
                    if o.lower().strip(" .") == q.answer_text.lower().strip(" ."):
                        q.correct = [i]
        if q.correct:
            q.answer_text = ""
    elif q.answer_text or (q.text.endswith("?") and len(q.text) > 15):
        q.qtype, q.options, q.correct = "open", [], []
    else:
        return
    if len(q.text) < 3 and q.images:
        q.text = "Look at the picture."
    if len(q.text) < 3:
        return
    out.append(q)


def parse_questions(raw: str) -> list[dict]:
    lines = [piece for ln in clean_markdown(raw.replace("\r", "")).split("\n") for piece in _explode_inline(ln)]
    lines, key = _split_key(lines)
    out: list[ParsedQuestion] = []
    cur: ParsedQuestion | None = None
    pending: list[str] = []
    pending_imgs: list[str] = []
    mode = ""  # q | opt | expl | ans

    def start(text: str, number: int | None) -> ParsedQuestion:
        nonlocal cur, mode, pending_imgs
        _finish(cur, out)
        cur = ParsedQuestion(text=text, number=number, images=pending_imgs)
        pending_imgs = []
        mode = "q"
        return cur

    def new_question_ahead(i: int) -> int | None:
        """Index of the "A." option that follows the (up to 3) text lines starting at i, if they look like a question."""
        seen = 0
        for k in range(i, min(len(lines), i + 4)):
            t = lines[k].strip()
            if not t:
                continue
            if IMG_LINE_RE.match(t):
                continue
            m = OPT_RE.match(t)
            if (m and m["l"].upper() == "A") or CHK_RE.match(t):
                return k if seen else None
            if ANS_RE.match(t) or EXPL_RE.match(t) or Q_RE.match(t) or Q_HEAD_RE.match(t) or m:
                return None
            seen += 1
        return None

    skip_to = -1
    for idx, line in enumerate(lines):
        if idx < skip_to:
            continue
        s = line.strip()
        if not s:
            continue
        m = IMG_LINE_RE.match(s)
        if m:
            # a picture belongs to the question being read; after its answer it belongs to the next one
            if cur is not None and mode in {"q", "opt"}:
                cur.images.append(m[1])
            else:
                pending_imgs.append(m[1])
            continue
        m = ANS_RE.match(s)
        if m and cur is not None:
            cur.answer_text = m["a"].strip()
            letters = LETTER_LIST_RE.match(cur.answer_text)
            if letters and cur.options:
                cur.correct = letters_to_idx(letters["l"], len(cur.options))
            mode = "ans"
            continue
        m = EXPL_RE.match(s)
        if m and cur is not None:
            cur.explanation = m["t"]
            mode = "expl"
            continue
        m = CHK_RE.match(s)
        if m and cur is not None:
            cur.options.append(m["t"])
            cur.checked.append(m["c"].lower() == "x")
            mode = "opt"
            continue
        m = OPT_RE.match(s)
        if m:
            letter = m["l"].upper()
            expected = LETTERS[len(cur.options)] if cur is not None and len(cur.options) < len(LETTERS) else ""
            if cur is not None and letter == expected and mode in {"q", "opt"}:
                cur.options.append(m["t"])
                cur.checked.append(False)
                mode = "opt"
                continue
            if letter == "A" and (cur is None or cur.options or mode in {"ans", "expl"}):
                text = " ".join(pending).strip()
                if text:
                    start(text, None)
                    pending = []
                    cur.options.append(m["t"])  # type: ignore[union-attr]
                    cur.checked.append(False)  # type: ignore[union-attr]
                    mode = "opt"
                    continue
        m = Q_RE.match(s)
        if m and (cur is None or mode != "opt" or len(m["t"]) > 3):
            start(m["t"], int(m["n"]))
            pending = []
            continue
        m = Q_HEAD_RE.match(s)
        if m:
            start("", int(m["n"]))
            pending = []
            continue
        # an unnumbered question directly after the previous one (its text would otherwise be glued to it)
        if cur is not None and mode in {"opt", "ans", "expl"} and (mode != "opt" or len(cur.options) >= 2):
            k = new_question_ahead(idx)
            if k is not None:
                chunk = [t.strip() for t in lines[idx:k] if t.strip()]
                pending_imgs += [im[1] for t in chunk if (im := IMG_LINE_RE.match(t))]
                start(" ".join(t for t in chunk if not IMG_LINE_RE.match(t)), None)
                pending = []
                skip_to = k
                continue
        # continuation line
        if cur is None:
            pending.append(s)
            if len(pending) > 6:
                pending = pending[-6:]
        elif mode == "q":
            cur.text += " " + s
        elif mode == "opt" and cur.options:
            cur.options[-1] += " " + s
        elif mode == "expl":
            cur.explanation += " " + s
        elif mode == "ans":
            cur.answer_text += " " + s
        else:
            pending.append(s)
    _finish(cur, out)

    for i, q in enumerate(out):
        if key and not q.correct and q.qtype == "mc":
            letters = key.get(q.number if q.number is not None else i + 1)
            if letters:
                q.correct = letters_to_idx(letters, len(q.options))
    return [q.to_dict() for q in out]
