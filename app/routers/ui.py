"""Cookie-only JSON endpoints used by the web UI. FlexTrainer has no public or token API."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import FONT_SIZES, FONTS, THEMES
from ..database import get_db
from ..deps import throttled, ui_user
from ..models import Attempt, Exam, Image, PracticeSession, Question, Source, User, now
from ..services import backup, exams, extract, gpt, images, parser, scraper, store

log = logging.getLogger("flextrainer.ui")
router = APIRouter(prefix="/ui", include_in_schema=False)


def _exam(db: Session, exam_id: int) -> Exam:
    e = db.get(Exam, exam_id)
    if not e:
        raise HTTPException(404, "Exam not found")
    return e


def _question(db: Session, qid: int) -> Question:
    q = db.get(Question, qid)
    if not q:
        raise HTTPException(404, "Question not found")
    return q


def _session(db: Session, sid: int) -> PracticeSession:
    s = db.get(PracticeSession, sid)
    if not s:
        raise HTTPException(404, "Practice session not found")
    return s


# ---------- appearance ----------


class AppearanceIn(BaseModel):
    theme: str | None = None
    font: str | None = None
    size: int | None = None


@router.post("/appearance")
def appearance(body: AppearanceIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    if body.theme is not None:
        if body.theme not in THEMES:
            raise HTTPException(422, "Unknown theme")
        user.theme = body.theme
    if body.font is not None:
        if body.font not in FONTS:
            raise HTTPException(422, "Unknown font")
        user.font = body.font
    if body.size is not None:
        if body.size not in FONT_SIZES:
            raise HTTPException(422, "Unknown text size")
        user.font_size = body.size
    db.commit()
    return {"theme": user.theme, "font": user.font, "size": user.font_size}


# ---------- exams ----------


class ExamIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


@router.post("/exams")
def create_exam(body: ExamIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    e = Exam(title=body.title.strip(), description=body.description.strip())
    db.add(e)
    db.commit()
    return {"id": e.id}


@router.patch("/exams/{exam_id}")
def rename_exam(exam_id: int, body: ExamIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    e = _exam(db, exam_id)
    e.title, e.description = body.title.strip(), body.description.strip()
    db.commit()
    return {"ok": True}


@router.delete("/exams/{exam_id}")
def delete_exam(exam_id: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    db.delete(_exam(db, exam_id))
    db.commit()
    return {"ok": True}


@router.post("/exams/{exam_id}/reset")
def reset_progress(exam_id: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    e = _exam(db, exam_id)
    for s in list(e.sessions):
        db.delete(s)
    db.commit()
    return {"ok": True}


@router.post("/exams/{exam_id}/duplicate")
def duplicate_exam(exam_id: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    """Copy an exam with its questions (no progress, ChatGPT checks and sources are not copied)."""
    e = _exam(db, exam_id)
    copy = Exam(title=f"{e.title} (copy)"[:200], description=e.description)
    db.add(copy)
    db.flush()
    copied: dict[int, int] = {}

    def copy_image(iid: int) -> int | None:
        if iid not in copied:
            src = db.get(Image, iid)
            if not src:
                return None
            row = Image(
                exam_id=copy.id, sha1=src.sha1, mime=src.mime, width=src.width, height=src.height, data=src.data
            )
            db.add(row)
            db.flush()
            copied[iid] = row.id
        return copied[iid]

    parsed = [
        {
            "text": q.text,
            "options": q.options,
            "correct": q.correct,
            "qtype": q.qtype,
            "explanation": q.explanation,
            "answer_text": q.answer_text,
            "images": [n for n in (copy_image(i) for i in q.images) if n],
        }
        for q in sorted(e.questions, key=lambda x: x.position)
    ]
    exams.add_questions(db, copy, parsed, None)
    return {"id": copy.id}


# ---------- import ----------


MAX_REMOTE_IMAGES = 40


def _resolve_images(db: Session, exam: Exam, items: list[dict], sources: dict) -> int:
    """Turn the picture keys of parsed questions into stored picture ids. Returns the number of pictures kept."""
    used = {k for it in items for k in it.get("images", [])}
    if not used:
        return 0
    prepared: dict[str, tuple[bytes, str, int, int]] = {}
    fetched = 0
    for key in sorted(used):
        src = sources.get(key)
        if isinstance(src, str):  # remote picture: fetch it (public hosts only)
            if fetched >= MAX_REMOTE_IMAGES:
                continue
            fetched += 1
            src = scraper.fetch_image(src)
        norm = images.normalise(src) if src else None
        if norm:
            prepared[key] = norm
    # a picture that sits on many questions is a logo or page decoration, not part of a question
    users: dict[str, int] = {}
    for it in items:
        for sha in {hashlib.sha1(prepared[k][0]).hexdigest() for k in it.get("images", []) if k in prepared}:
            users[sha] = users.get(sha, 0) + 1
    decor = {sha for sha, n in users.items() if n >= 4 and len(items) >= 6 and n > len(items) // 2}
    ids: dict[str, int] = {}
    for it in items:
        out: list[int] = []
        for k in it.get("images", []):
            if k not in prepared or hashlib.sha1(prepared[k][0]).hexdigest() in decor:
                continue
            if k not in ids:
                ids[k] = images.save(db, exam.id, prepared[k]).id
            if ids[k] not in out:
                out.append(ids[k])
        it["images"] = out[: images.MAX_PER_QUESTION]
    return len(set(ids.values()))


def _ingest(
    db: Session,
    exam: Exam,
    doc: extract.Doc,
    name: str,
    kind: str,
    url: str,
    use_ai: bool,
    generate: bool = False,
) -> dict:
    text = doc.text
    parsed = parser.parse_questions(text)
    note = ""
    cfg = store.openai_config(db)
    has_mc = any(p["qtype"] == "mc" and len(p["options"]) >= 2 for p in parsed)
    if cfg["configured"] and (generate or use_ai or not has_mc):
        try:
            if generate or not has_mc:
                # Build multiple choice from examples/material; keep questions the parser already read as MC.
                ai = gpt.generate_questions(cfg, text)
                if ai:
                    parsed = [p for p in parsed if p["qtype"] == "mc" and len(p["options"]) >= 2]
                    if not generate and not has_mc:
                        note = "No multiple choice found; ChatGPT generated it from the examples."
            else:
                ai = gpt.extract_questions(cfg, text)
            seen = {parser.fingerprint(p["text"], p["options"]) for p in parsed}
            parsed += [a for a in ai if parser.fingerprint(a["text"], a["options"]) not in seen]
        except gpt.GptError as exc:
            note = f"AI-assisted import failed: {exc}"
    elif not has_mc and not parsed:
        note = "No questions recognised. Add a ChatGPT key in Settings to enable AI-assisted import."
    if not parsed:
        return {
            "name": name,
            "found": 0,
            "added": 0,
            "duplicates": 0,
            "images": 0,
            "error": note or "No questions found.",
        }
    n_images = _resolve_images(db, exam, parsed, doc.images)
    src = Source(exam_id=exam.id, kind=kind, name=name[:300], url=url[:2000])
    db.add(src)
    db.flush()
    added, dupes = exams.add_questions(db, exam, parsed, src)
    if added == 0:
        db.delete(src)
    db.commit()
    images.prune(db, exam.id)
    log.info("Imported %s: %d found, %d added, %d duplicates, %d pictures", name, len(parsed), added, dupes, n_images)
    return {"name": name, "found": len(parsed), "added": added, "duplicates": dupes, "images": n_images, "error": note}


@router.post("/exams/{exam_id}/upload")
def upload(
    exam_id: int,
    files: Annotated[list[UploadFile], File()],
    ai: Annotated[bool, Form()] = False,
    generate: Annotated[bool, Form()] = False,
    user: User = Depends(throttled),
    db: Session = Depends(get_db),
):
    e = _exam(db, exam_id)
    if len(files) > 20:
        raise HTTPException(422, "Upload at most 20 files at once.")
    results = []
    for f in files:
        name = (f.filename or "file").rsplit("/", 1)[-1]
        try:
            data = f.file.read(extract.MAX_BYTES + 1)
            doc = extract.extract_document(name, data)
            results.append(_ingest(db, e, doc, name, "file", "", ai, generate))
        except extract.ExtractError as exc:
            results.append({"name": name, "found": 0, "added": 0, "duplicates": 0, "error": str(exc)})
    return {"results": results}


class ScrapeIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10)
    ai: bool = False
    generate: bool = False


@router.post("/exams/{exam_id}/scrape")
def scrape(exam_id: int, body: ScrapeIn, user: User = Depends(throttled), db: Session = Depends(get_db)):
    e = _exam(db, exam_id)
    results = []
    for url in body.urls:
        url = url.strip()
        if not url:
            continue
        try:
            doc, title = scraper.fetch(url)
            results.append(_ingest(db, e, doc, title or url, "url", url, body.ai, body.generate))
        except scraper.ScrapeError as exc:
            results.append({"name": url, "found": 0, "added": 0, "duplicates": 0, "error": str(exc)})
    return {"results": results}


class PasteIn(BaseModel):
    name: str = Field(default="Pasted text", max_length=300)
    text: str = Field(min_length=3, max_length=500_000)
    ai: bool = False
    generate: bool = False


@router.post("/exams/{exam_id}/paste")
def paste(exam_id: int, body: PasteIn, user: User = Depends(throttled), db: Session = Depends(get_db)):
    e = _exam(db, exam_id)
    name = body.name.strip() or "Pasted text"
    return {"results": [_ingest(db, e, extract.markdown_doc(body.text), name, "file", "", body.ai, body.generate)]}


class SourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)


def _source(db: Session, source_id: int) -> Source:
    s = db.get(Source, source_id)
    if not s:
        raise HTTPException(404, "Source not found")
    return s


@router.patch("/sources/{source_id}")
def rename_source(source_id: int, body: SourceIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    _source(db, source_id).name = body.name.strip()
    db.commit()
    return {"ok": True}


@router.delete("/sources/{source_id}")
def delete_source(
    source_id: int, keep_questions: bool = False, user: User = Depends(ui_user), db: Session = Depends(get_db)
):
    s = _source(db, source_id)
    rows = db.query(Question).filter(Question.source_id == s.id)
    if keep_questions:
        rows.update({Question.source_id: None})
    else:
        rows.delete()
    exam_id = s.exam_id
    db.delete(s)
    db.commit()
    images.prune(db, exam_id)
    return {"ok": True}


# ---------- questions ----------


class QuestionIn(BaseModel):
    text: str = Field(min_length=3, max_length=5000)
    options: list[str] = Field(default_factory=list, max_length=8)
    correct: list[int] = Field(default_factory=list)
    explanation: str = Field(default="", max_length=4000)
    answer_text: str = Field(default="", max_length=4000)


def _validated(body: QuestionIn) -> tuple[list[str], list[int]]:
    options = [o.strip() for o in body.options if o.strip()]
    if options and len(options) < 2:
        raise HTTPException(422, "A multiple-choice question needs at least 2 options.")
    if any(c < 0 or c >= len(options) for c in body.correct):
        raise HTTPException(422, "A correct answer points to a missing option.")
    return options, sorted(set(body.correct))


@router.post("/exams/{exam_id}/questions")
def add_question(exam_id: int, body: QuestionIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    e = _exam(db, exam_id)
    options, correct = _validated(body)
    item = {
        "text": body.text.strip(),
        "options": options,
        "correct": correct,
        "qtype": "mc" if options else "open",
        "explanation": body.explanation.strip(),
        "answer_text": body.answer_text.strip(),
    }
    added, _ = exams.add_questions(db, e, [item], None)
    if not added:
        raise HTTPException(409, "An identical question already exists in this exam.")
    return {"ok": True}


class IdsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)


@router.post("/exams/{exam_id}/questions/delete")
def delete_questions(exam_id: int, body: IdsIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    e = _exam(db, exam_id)
    n = 0
    for q in db.query(Question).filter(Question.exam_id == e.id, Question.id.in_(body.ids)).all():
        db.delete(q)
        n += 1
    db.commit()
    images.prune(db, e.id)
    return {"deleted": n}


@router.post("/questions/{qid}/reset")
def reset_question(qid: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    """Forget the answers given to one question (it counts as unseen again)."""
    q = _question(db, qid)
    db.query(Attempt).filter(Attempt.question_id == q.id).delete()
    db.commit()
    return {"ok": True}


@router.get("/questions/{qid}")
def get_question(qid: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    return exams.question_json(_question(db, qid))


@router.patch("/questions/{qid}")
def edit_question(qid: int, body: QuestionIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    q = _question(db, qid)
    options, _ = _validated(body)
    q.qtype = "mc" if options else "open"
    q.text, q.options_json = body.text.strip(), json.dumps(options)
    q.correct_json, q.explanation = json.dumps(sorted(set(body.correct))), body.explanation.strip()
    q.answer_text = body.answer_text.strip()
    q.fingerprint = parser.fingerprint(q.text, options, images.shas(db, q.images))
    q.verify_status, q.verify_note, q.verify_link = "unchecked", "", ""
    q.ai_correct_json, q.ai_answer, q.verified_at = "[]", "", None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Another question in this exam is identical.") from None
    return exams.question_json(q)


@router.delete("/questions/{qid}")
def delete_question(qid: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    q = _question(db, qid)
    exam_id = q.exam_id
    db.delete(q)
    db.commit()
    images.prune(db, exam_id)
    return {"ok": True}


# ---------- question pictures ----------


@router.get("/images/{image_id}")
def get_image(image_id: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if not img:
        raise HTTPException(404, "Picture not found")
    return Response(
        img.data,
        media_type=img.mime,
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


def _set_images(db: Session, q: Question, ids: list[int]) -> None:
    q.images_json = json.dumps(ids)
    q.fingerprint = parser.fingerprint(q.text, q.options, images.shas(db, ids))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Another question in this exam is identical.") from None
    images.prune(db, q.exam_id)


@router.post("/questions/{qid}/images")
def add_question_image(
    qid: int, file: Annotated[UploadFile, File()], user: User = Depends(throttled), db: Session = Depends(get_db)
):
    q = _question(db, qid)
    if len(q.images) >= images.MAX_PER_QUESTION:
        raise HTTPException(422, f"A question can have at most {images.MAX_PER_QUESTION} pictures.")
    data = file.file.read(images.MAX_INPUT_BYTES + 1)
    img = images.store_image(db, q.exam_id, data, keep_small=True)
    if not img:
        raise HTTPException(422, "Use a PNG, JPEG, GIF or WebP picture of at most 8 MB.")
    if img.id not in q.images:
        _set_images(db, q, [*q.images, img.id])
    return exams.question_json(q)


@router.delete("/questions/{qid}/images/{image_id}")
def remove_question_image(qid: int, image_id: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    q = _question(db, qid)
    _set_images(db, q, [i for i in q.images if i != image_id])
    return exams.question_json(q)


@router.post("/questions/{qid}/verify")
def verify_question(qid: int, user: User = Depends(throttled), db: Session = Depends(get_db)):
    q = _question(db, qid)
    try:
        exams.verify_one(db, q)
    except gpt.GptError as exc:
        raise HTTPException(502, str(exc)) from exc
    return exams.question_json(q)


@router.post("/questions/{qid}/accept-ai")
def accept_ai(qid: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    q = _question(db, qid)
    if q.qtype == "mc" and q.ai_correct:
        q.correct_json = json.dumps(q.ai_correct)
    elif q.qtype == "open" and q.ai_answer:
        q.answer_text = q.ai_answer
    else:
        raise HTTPException(409, "ChatGPT has no answer for this question yet.")
    q.verify_status = "confirmed"
    db.commit()
    return exams.question_json(q)


@router.post("/exams/{exam_id}/verify")
def verify_all(exam_id: int, redo: bool = False, user: User = Depends(throttled)):
    try:
        return exams.start_verify_all(exam_id, only_unchecked=not redo)
    except exams.ExamError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/exams/{exam_id}/verify/status")
def verify_progress(exam_id: int, user: User = Depends(ui_user)):
    return exams.verify_status(exam_id)


# ---------- practice ----------


class SessionIn(BaseModel):
    mode: str = Field(default="smart", pattern="^(smart|unseen|wrong|all)$")
    count: int | None = Field(default=20, ge=1, le=1000)
    shuffle: bool = True


@router.post("/exams/{exam_id}/sessions")
def start_session(exam_id: int, body: SessionIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    try:
        s = exams.create_session(db, _exam(db, exam_id), body.mode, body.count, body.shuffle)
    except exams.ExamError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": s.id}


@router.delete("/sessions/{sid}")
def end_session(sid: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    s = _session(db, sid)
    s.finished_at = s.finished_at or now()
    db.commit()
    return {"ok": True}


def _feedback(q: Question, correct: bool | None, chosen: list[int]) -> dict:
    return {
        "correct": correct,
        "chosen": chosen,
        "correct_indexes": q.effective_correct,
        "key_source": "file" if q.correct else ("chatgpt" if q.ai_correct else None),
        "explanation": q.explanation,
        "answer": q.reference_answer,
        "ai": {
            "status": q.verify_status,
            "note": q.verify_note,
            "link": q.verify_link,
            "correct": q.ai_correct,
            "answer": q.ai_answer,
        },
        "disputed": q.verify_status == "disputed",
    }


@router.get("/sessions/{sid}")
def session_state(sid: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    s = _session(db, sid)
    prog = exams.session_progress(db, s)
    if prog["remaining"] == 0 and not s.finished_at:
        s.finished_at = now()
        db.commit()
    return {
        "id": s.id,
        "exam_id": s.exam_id,
        "exam_title": s.exam.title,
        "mode": s.mode,
        **prog,
        "finished": prog["remaining"] == 0,
        "gpt": store.openai_config(db)["configured"],
    }


@router.get("/sessions/{sid}/question")
def current_question(sid: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    s = _session(db, sid)
    prog = exams.session_progress(db, s)
    if not prog["next_id"]:
        return {"done": True}
    q = _question(db, prog["next_id"])
    return {
        "done": False,
        "index": prog["answered"] + 1,
        "total": prog["total"],
        "question": {
            **exams.question_json(q, full=False),
            "multi": len(q.effective_correct) > 1,
            "has_key": bool(q.effective_correct),
            "checked": q.verify_status != "unchecked",
        },
    }


@router.get("/sessions/{sid}/history/{n}")
def history(sid: int, n: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    """Read-only look at the n-th (0-based) question already answered in this session, in session order."""
    s = _session(db, sid)
    latest: dict[int, Attempt] = {}
    for a in db.query(Attempt).filter(Attempt.session_id == s.id).order_by(Attempt.id):
        latest[a.question_id] = a
    live = {i for (i,) in db.query(Question.id).filter(Question.id.in_(s.order or [0]))}
    seen = [i for i in s.order if i in live and i in latest]
    if n < 0 or n >= len(seen):
        raise HTTPException(404, "No such question in the history.")
    q, a = _question(db, seen[n]), latest[seen[n]]
    chosen = json.loads(a.chosen_json or "[]")
    return {
        "index": n + 1,
        "count": len(seen),
        "question": {**exams.question_json(q, full=False), "multi": len(q.effective_correct) > 1},
        "skipped": a.skipped,
        "feedback": _feedback(q, a.correct, chosen),
    }


@router.post("/questions/{qid}/reveal")
def reveal(qid: int, user: User = Depends(throttled), db: Session = Depends(get_db)):
    """Open questions: show the reference answer (asking ChatGPT when there is none yet)."""
    q = _question(db, qid)
    if not q.reference_answer and store.openai_config(db)["configured"]:
        try:
            exams.verify_one(db, q)
        except gpt.GptError as exc:
            log.warning("Reveal check failed: %s", exc)
    return _feedback(q, None, [])


class AnswerIn(BaseModel):
    question_id: int
    chosen: list[int] = Field(default_factory=list, max_length=8)
    self_correct: bool | None = None
    skip: bool = False


@router.post("/sessions/{sid}/answer")
def answer(sid: int, body: AnswerIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    s = _session(db, sid)
    q = _question(db, body.question_id)
    prog = exams.session_progress(db, s)
    if prog["next_id"] != q.id:
        raise HTTPException(409, "This question is not the current one in the session.")
    if body.skip:
        db.add(Attempt(session_id=s.id, question_id=q.id, chosen_json="[]", correct=None, skipped=True))
        db.commit()
        return {"skipped": True, **{k: v for k, v in exams.session_progress(db, s).items() if k != "next_id"}}
    if q.qtype == "mc":
        if not body.chosen or any(c < 0 or c >= len(q.options) for c in body.chosen):
            raise HTTPException(422, "Pick at least one valid option.")
        if not q.effective_correct and store.openai_config(db)["configured"]:
            try:
                exams.verify_one(db, q)  # no answer key in the source: ask ChatGPT first
            except gpt.GptError as exc:
                log.warning("Answer check failed: %s", exc)
    elif body.self_correct is None:
        raise HTTPException(422, "Say whether your answer was right.")
    ok = exams.grade(q, body.chosen, body.self_correct)
    db.add(Attempt(session_id=s.id, question_id=q.id, chosen_json=json.dumps(sorted(set(body.chosen))), correct=ok))
    db.commit()
    prog = exams.session_progress(db, s)
    if prog["remaining"] == 0:
        s.finished_at = now()
        db.commit()
    return {
        **_feedback(q, ok, sorted(set(body.chosen))),
        "needs_check": q.verify_status == "unchecked",
        "progress": {k: v for k, v in prog.items() if k != "next_id"},
    }


@router.get("/sessions/{sid}/summary")
def summary(sid: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    s = _session(db, sid)
    prog = exams.session_progress(db, s)
    rows = (
        db.query(Attempt, Question)
        .join(Question, Attempt.question_id == Question.id)
        .filter(Attempt.session_id == s.id, Attempt.correct.is_(False))
        .order_by(Attempt.id)
        .all()
    )
    graded = prog["correct"] + prog["wrong"]
    return {
        "exam_id": s.exam_id,
        "exam_title": s.exam.title,
        **{k: v for k, v in prog.items() if k != "next_id"},
        "accuracy": round(100 * prog["correct"] / graded) if graded else None,
        "wrong_items": [{"id": q.id, "text": q.text} for _, q in rows],
    }


# ---------- settings: ChatGPT ----------


class OpenAIIn(BaseModel):
    key: str | None = Field(default=None, max_length=300)  # None = keep, "" = remove
    model: str = Field(default="", max_length=80)


@router.post("/settings/openai")
def save_openai(body: OpenAIIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    if body.model and not re.fullmatch(r"[\w.\-:/]+", body.model):
        raise HTTPException(422, "Model name contains unsupported characters.")
    if body.key is not None:
        store.set_openai_key(db, body.key.strip())
    store.put(db, "openai_model", body.model.strip())
    return {"configured": store.openai_config(db)["configured"]}


@router.post("/settings/openai/test")
def test_openai(user: User = Depends(throttled), db: Session = Depends(get_db)):
    try:
        return {"ok": True, "model": gpt.test_connection(store.openai_config(db))}
    except gpt.GptError as exc:
        raise HTTPException(502, str(exc)) from exc


# ---------- settings: backups ----------


class BackupCfgIn(BaseModel):
    enabled: bool
    keep: int = Field(ge=1, le=60)


@router.post("/settings/backup")
def save_backup_cfg(body: BackupCfgIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    store.put(db, "backup_enabled", "1" if body.enabled else "0")
    store.put(db, "backup_keep", str(body.keep))
    backup.prune(body.keep)
    return store.backup_config(db)


@router.get("/backups")
def backups(user: User = Depends(ui_user)):
    return backup.list_backups()


@router.post("/backups")
def make_backup(user: User = Depends(ui_user), db: Session = Depends(get_db)):
    path = backup.create_backup("manual")
    backup.prune(store.backup_config(db)["keep"])
    return {"name": path.name}


@router.get("/backups/export")
def export_backup(user: User = Depends(ui_user)):
    path = backup.create_backup("export")
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.get("/backups/{name}")
def download_backup(name: str, user: User = Depends(ui_user)):
    try:
        path = backup.safe_path(name)
    except backup.BackupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.delete("/backups/{name}")
def delete_backup(name: str, user: User = Depends(ui_user)):
    try:
        backup.safe_path(name).unlink()
    except backup.BackupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@router.post("/backups/import")
def import_backup(file: Annotated[UploadFile, File()], user: User = Depends(ui_user)):
    data = file.file.read(300 * 1024 * 1024 + 1)
    if len(data) > 300 * 1024 * 1024:
        raise HTTPException(413, "Backup is larger than 300 MB.")
    try:
        backup.restore(data)
    except backup.BackupError as exc:
        raise HTTPException(422, str(exc)) from exc
    log.warning("Database restored from an uploaded backup")
    return Response(json.dumps({"ok": True}), media_type="application/json")
