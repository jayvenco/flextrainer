"""Exam, question, practice-session and progress logic."""

from __future__ import annotations

import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Attempt, Exam, PracticeSession, Question, Source, now
from . import gpt, store
from .parser import fingerprint


class ExamError(Exception):
    pass


# ---------- questions ----------


def add_questions(db: Session, exam: Exam, parsed: list[dict], source: Source | None) -> tuple[int, int]:
    existing = {fp for (fp,) in db.query(Question.fingerprint).filter(Question.exam_id == exam.id)}
    pos = db.query(func.coalesce(func.max(Question.position), 0)).filter(Question.exam_id == exam.id).scalar() or 0
    added = dupes = 0
    for item in parsed:
        fp = fingerprint(item["text"], item["options"])
        if fp in existing:
            dupes += 1
            continue
        existing.add(fp)
        pos += 1
        db.add(
            Question(
                exam_id=exam.id,
                source_id=source.id if source else None,
                position=pos,
                qtype=item["qtype"],
                text=item["text"],
                options_json=json.dumps(item["options"]),
                correct_json=json.dumps(item["correct"]),
                answer_text=item.get("answer_text", ""),
                explanation=item.get("explanation", ""),
                fingerprint=fp,
            )
        )
        added += 1
    db.commit()
    return added, dupes


def question_json(q: Question, full: bool = True) -> dict:
    out = {
        "id": q.id,
        "qtype": q.qtype,
        "text": q.text,
        "options": q.options,
        "source_id": q.source_id,
        "verify_status": q.verify_status,
    }
    if full:
        out.update(
            correct=q.correct,
            explanation=q.explanation,
            answer_text=q.answer_text,
            verify_note=q.verify_note,
            verify_link=q.verify_link,
            ai_correct=q.ai_correct,
            ai_answer=q.ai_answer,
            verified_at=q.verified_at.isoformat() if q.verified_at else None,
        )
    return out


def apply_verification(q: Question, res: dict) -> None:
    q.verify_status = res["status"]
    q.verify_note = res["note"]
    q.verify_link = res["link"]
    q.ai_correct_json = json.dumps(res["ai_correct"])
    q.ai_answer = res["ai_answer"]
    q.verified_at = now()


def verify_one(db: Session, q: Question) -> dict:
    cfg = store.openai_config(db)
    res = gpt.verify_question(
        cfg,
        {"text": q.text, "options": q.options, "correct": q.correct, "answer_text": q.answer_text, "qtype": q.qtype},
    )
    apply_verification(q, res)
    db.commit()
    return res


# ---------- bulk ChatGPT check (background) ----------

_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()


def verify_status(exam_id: int) -> dict:
    with _jobs_lock:
        return dict(_jobs.get(exam_id, {"running": False, "total": 0, "done": 0, "errors": 0, "message": ""}))


def start_verify_all(exam_id: int, only_unchecked: bool = True) -> dict:
    with SessionLocal() as db:
        cfg = store.openai_config(db)
        if not cfg["configured"]:
            raise ExamError("Add your ChatGPT API key in Settings first.")
        qs = db.query(Question.id).filter(Question.exam_id == exam_id)
        if only_unchecked:
            qs = qs.filter(Question.verify_status.in_(("unchecked", "error")))
        ids = [i for (i,) in qs.order_by(Question.position)]
    with _jobs_lock:
        if _jobs.get(exam_id, {}).get("running"):
            raise ExamError("A check is already running for this exam.")
        _jobs[exam_id] = {"running": True, "total": len(ids), "done": 0, "errors": 0, "message": ""}
    if not ids:
        with _jobs_lock:
            _jobs[exam_id]["running"] = False
            _jobs[exam_id]["message"] = "Nothing to check."
        return verify_status(exam_id)

    def work(qid: int) -> None:
        try:
            with SessionLocal() as db:
                q = db.get(Question, qid)
                if q:
                    verify_one(db, q)
        except gpt.GptError as exc:
            with _jobs_lock:
                _jobs[exam_id]["errors"] += 1
                _jobs[exam_id]["message"] = str(exc)
        except Exception:  # noqa: BLE001
            with _jobs_lock:
                _jobs[exam_id]["errors"] += 1
        finally:
            with _jobs_lock:
                _jobs[exam_id]["done"] += 1

    def runner() -> None:
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(work, ids))
        with _jobs_lock:
            _jobs[exam_id]["running"] = False

    threading.Thread(target=runner, daemon=True, name=f"verify-{exam_id}").start()
    return verify_status(exam_id)


# ---------- progress ----------


def _latest_graded(db: Session, exam_id: int) -> tuple[dict[int, bool], int, int]:
    rows = (
        db.query(Attempt.question_id, Attempt.correct)
        .join(PracticeSession, Attempt.session_id == PracticeSession.id)
        .filter(PracticeSession.exam_id == exam_id, Attempt.correct.isnot(None))
        .order_by(Attempt.id)
        .all()
    )
    latest: dict[int, bool] = {}
    right = 0
    for qid, ok in rows:
        latest[qid] = bool(ok)
        right += 1 if ok else 0
    return latest, len(rows), right


def open_session(db: Session, exam_id: int) -> dict | None:
    s = (
        db.query(PracticeSession)
        .filter(PracticeSession.exam_id == exam_id, PracticeSession.finished_at.is_(None))
        .order_by(PracticeSession.id.desc())
        .first()
    )
    if not s:
        return None
    prog = session_progress(db, s)
    if prog["remaining"] == 0:
        s.finished_at = now()
        db.commit()
        return None
    return {"id": s.id, "answered": prog["answered"], "total": prog["total"], "mode": s.mode}


def exam_stats(db: Session, exam: Exam) -> dict:
    total = db.query(func.count(Question.id)).filter(Question.exam_id == exam.id).scalar() or 0
    latest, graded, right = _latest_graded(db, exam.id)
    live = {i for (i,) in db.query(Question.id).filter(Question.exam_id == exam.id)}
    latest = {k: v for k, v in latest.items() if k in live}
    verified = (
        db.query(func.count(Question.id))
        .filter(Question.exam_id == exam.id, Question.verify_status.in_(("confirmed", "answered", "disputed")))
        .scalar()
        or 0
    )
    disputed = (
        db.query(func.count(Question.id))
        .filter(Question.exam_id == exam.id, Question.verify_status == "disputed")
        .scalar()
        or 0
    )
    return {
        "id": exam.id,
        "title": exam.title,
        "description": exam.description,
        "total": total,
        "attempted": len(latest),
        "mastered": sum(1 for v in latest.values() if v),
        "wrong": sum(1 for v in latest.values() if not v),
        "accuracy": round(100 * right / graded) if graded else None,
        "verified": verified,
        "disputed": disputed,
        "session": open_session(db, exam.id),
    }


def create_session(db: Session, exam: Exam, mode: str, count: int | None, shuffle: bool) -> PracticeSession:
    qs = list(exam.questions)
    latest, _, _ = _latest_graded(db, exam.id)
    seen_ids = {
        i
        for (i,) in db.query(Attempt.question_id)
        .join(PracticeSession)
        .filter(PracticeSession.exam_id == exam.id)
        .distinct()
    }
    unseen = [q.id for q in qs if q.id not in seen_ids]
    wrong = [q.id for q in qs if latest.get(q.id) is False]
    rest = [q.id for q in qs if q.id not in set(unseen) | set(wrong)]
    if mode == "unseen":
        order = unseen
    elif mode == "wrong":
        order = wrong
    elif mode == "all":
        order = [q.id for q in qs]
    else:
        mode = "smart"
        for grp in (unseen, wrong, rest):
            random.shuffle(grp)
        order = unseen + wrong + rest
    if shuffle and mode != "smart":
        random.shuffle(order)
    if count:
        order = order[:count]
    if not order:
        raise ExamError(
            {"unseen": "There are no unseen questions left.", "wrong": "There are no wrongly answered questions."}.get(
                mode, "This exam has no questions yet."
            )
        )
    s = PracticeSession(exam_id=exam.id, mode=mode, order_json=json.dumps(order))
    # Only one open session per exam: finish older ones so "resume" is unambiguous.
    for old in db.query(PracticeSession).filter(
        PracticeSession.exam_id == exam.id, PracticeSession.finished_at.is_(None)
    ):
        old.finished_at = now()
    db.add(s)
    db.commit()
    return s


def session_progress(db: Session, s: PracticeSession) -> dict:
    live = {i for (i,) in db.query(Question.id).filter(Question.id.in_(s.order or [0]))}
    done = {
        qid: (ok, sk)
        for qid, ok, sk in db.query(Attempt.question_id, Attempt.correct, Attempt.skipped).filter(
            Attempt.session_id == s.id
        )
    }
    done = {k: v for k, v in done.items() if k in live}
    remaining = [i for i in s.order if i in live and i not in done]
    return {
        "answered": len(done),
        "total": len(done) + len(remaining),
        "remaining": len(remaining),
        "correct": sum(1 for ok, _ in done.values() if ok),
        "wrong": sum(1 for ok, _ in done.values() if ok is False),
        "skipped": sum(1 for _, sk in done.values() if sk),
        "next_id": remaining[0] if remaining else None,
    }


def grade(q: Question, chosen: list[int], self_correct: bool | None) -> bool | None:
    if q.qtype == "open":
        return self_correct
    key = q.effective_correct
    return (sorted(set(chosen)) == sorted(key)) if key else None
