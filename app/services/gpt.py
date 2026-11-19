"""ChatGPT (OpenAI-compatible chat completions) checks and optional AI-assisted question extraction.

The API key is never logged. Reference links are only *suggestions* from the model and are labelled as such.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

import httpx

from .parser import LETTERS, fingerprint, letters_to_idx

log = logging.getLogger("flextrainer.gpt")

VERIFY_SYSTEM = (
    "You check exam questions. Reply ONLY with a JSON object with these keys: "
    '"correct" (array of the correct option letters, e.g. ["B"]; empty array for open questions), '
    '"answer" (short model answer for open questions, empty string for multiple choice), '
    '"agrees" (true if the provided answer key matches your answer, false if it does not, null if no key was given), '
    '"explanation" (at most 3 sentences saying why the answer is correct), '
    '"reference_url" (a stable public URL such as official documentation or an encyclopedia article that supports '
    "the answer; an empty string if you are not sure one exists; never invent URLs), "
    '"confidence" ("high", "medium" or "low").'
)

EXTRACT_SYSTEM = (
    "You extract exam questions from text. Reply ONLY with JSON: "
    '{"questions":[{"text":"...","options":["...","..."],"correct":["B"],"explanation":"...","answer":"..."}]}. '
    "Include only questions that actually appear in the text. Leave options empty for open questions. "
    'Use option letters in "correct" only when the text states the answer; otherwise use an empty array.'
)


class GptError(Exception):
    pass


def _chat(cfg: dict, system: str, user: str, timeout: float = 90) -> dict:
    if not cfg.get("key"):
        raise GptError("No ChatGPT API key is configured (Settings -> ChatGPT).")
    body = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    try:
        resp = httpx.post(
            cfg["base_url"] + "/chat/completions",
            json=body,
            timeout=timeout,
            headers={"Authorization": "Bearer " + cfg["key"]},
        )
    except httpx.HTTPError as exc:
        raise GptError(f"Could not reach the ChatGPT API ({exc.__class__.__name__}).") from exc
    if resp.status_code == 401:
        raise GptError("The ChatGPT API rejected the API key.")
    if resp.status_code == 429:
        raise GptError("ChatGPT rate limit or quota reached. Try again later.")
    if resp.status_code >= 400:
        raise GptError(f"ChatGPT API error (HTTP {resp.status_code}).")
    try:
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise GptError("ChatGPT returned an unreadable answer.") from exc


def test_connection(cfg: dict) -> str:
    data = _chat(cfg, 'Reply with the JSON object {"ok": true}.', "ping", timeout=30)
    if not data.get("ok"):
        raise GptError("Unexpected answer from the API.")
    return cfg["model"]


def _safe_link(url: object) -> str:
    if not isinstance(url, str):
        return ""
    url = url.strip()
    p = urlparse(url)
    return url[:2000] if p.scheme in {"http", "https"} and p.netloc and " " not in url else ""


def verify_question(cfg: dict, q: dict) -> dict:
    """q: {text, options, correct (idx list), answer_text, qtype}. Returns a normalised result."""
    lines = ["Question: " + q["text"]]
    if q["qtype"] == "mc":
        lines += [f"{LETTERS[i]}. {o}" for i, o in enumerate(q["options"])]
        key = ", ".join(LETTERS[i] for i in q["correct"])
        lines.append("Provided answer key: " + (key or "none"))
    else:
        lines.append("This is an open question.")
        lines.append("Provided reference answer: " + (q["answer_text"] or "none"))
    data = _chat(cfg, VERIFY_SYSTEM, "\n".join(lines))
    correct_raw = data.get("correct") or []
    if isinstance(correct_raw, str):
        correct_raw = [correct_raw]
    ai_idx = letters_to_idx(" ".join(str(c) for c in correct_raw), len(q["options"])) if q["qtype"] == "mc" else []
    answer = str(data.get("answer") or "").strip()
    explanation = re.sub(r"\s+", " ", str(data.get("explanation") or "")).strip()[:1200]
    link = _safe_link(data.get("reference_url"))
    if q["qtype"] == "mc":
        if not ai_idx:
            return {
                "status": "error",
                "note": "ChatGPT did not return an answer.",
                "link": "",
                "ai_correct": [],
                "ai_answer": "",
            }
        if q["correct"]:
            status = "confirmed" if sorted(q["correct"]) == ai_idx else "disputed"
        else:
            status = "answered"
    else:
        agrees = data.get("agrees")
        status = "answered" if not q["answer_text"] else ("confirmed" if agrees is True else "disputed")
        if not answer:
            return {
                "status": "error",
                "note": "ChatGPT did not return an answer.",
                "link": "",
                "ai_correct": [],
                "ai_answer": "",
            }
    return {"status": status, "note": explanation, "link": link, "ai_correct": ai_idx, "ai_answer": answer}


def extract_questions(cfg: dict, text: str, max_chunks: int = 8) -> list[dict]:
    """AI-assisted extraction for material the heuristic parser cannot read."""
    chunks, buf = [], ""
    for para in text.split("\n"):
        if len(buf) + len(para) > 9000:
            chunks.append(buf)
            buf = ""
        buf += para + "\n"
    if buf.strip():
        chunks.append(buf)
    out: list[dict] = []
    for chunk in chunks[:max_chunks]:
        data = _chat(cfg, EXTRACT_SYSTEM, chunk, timeout=120)
        for item in data.get("questions") or []:
            try:
                qtext = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()
                options = [re.sub(r"\s+", " ", str(o)).strip() for o in (item.get("options") or []) if str(o).strip()]
                if len(qtext) < 3:
                    continue
                if len(options) >= 2:
                    correct = letters_to_idx(" ".join(str(c) for c in (item.get("correct") or [])), len(options))
                    out.append(
                        {
                            "text": qtext,
                            "options": options,
                            "correct": correct,
                            "qtype": "mc",
                            "explanation": str(item.get("explanation") or "").strip(),
                            "answer_text": "",
                        }
                    )
                else:
                    out.append(
                        {
                            "text": qtext,
                            "options": [],
                            "correct": [],
                            "qtype": "open",
                            "explanation": str(item.get("explanation") or "").strip(),
                            "answer_text": str(item.get("answer") or "").strip(),
                        }
                    )
            except (AttributeError, TypeError):
                continue
    return out


__all__ = ["GptError", "verify_question", "extract_questions", "test_connection", "fingerprint"]
