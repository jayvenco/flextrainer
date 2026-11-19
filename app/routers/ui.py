"""Cookie-only JSON endpoints used by the web UI. FlexTrainer has no public or token API."""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import FONTS, THEMES
from ..database import get_db
from ..deps import throttled, ui_user
from ..models import Attempt, Exam, PracticeSession, Question, Source, User, now
from ..services import backup, exams, extract, gpt, parser, scraper, store

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
    db.commit()
    return {"theme": user.theme, "font": user.font}


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


# ---------- import ----------


def _ingest(db: Session, exam: Exam, text: str, name: str, kind: str, url: str, use_ai: bool) -> dict:
    parsed = parser.parse_questions(text)
    note = ""
    cfg = store.openai_config(db)
    if cfg["configured"] and (use_ai or not parsed):
        try:
            ai = gpt.extract_questions(cfg, text)
            seen = {parser.fingerprint(p["text"], p["options"]) for p in parsed}
            parsed += [a for a in ai if parser.fingerprint(a["text"], a["options"]) not in seen]
        except gpt.GptError as exc:
            note = f"AI-assisted import failed: {exc}"
    elif not parsed:
        note = "No questions recognised. Add a ChatGPT key in Settings to enable AI-assisted import."
    if not parsed:
        return {"name": name, "found": 0, "added": 0, "duplicates": 0, "error": note or "No questions found."}
    src = Source(exam_id=exam.id, kind=kind, name=name[:300], url=url[:2000])
    db.add(src)
    db.flush()
    added, dupes = exams.add_questions(db, exam, parsed, src)
    if added == 0:
        db.delete(src)
        db.commit()
    log.info("Imported %s: %d found, %d added, %d duplicates", name, len(parsed), added, dupes)
    return {"name": name, "found": len(parsed), "added": added, "duplicates": dupes, "error": note}


@router.post("/exams/{exam_id}/upload")
def upload(
    exam_id: int,
    files: Annotated[list[UploadFile], File()],
    ai: Annotated[bool, Form()] = False,
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
            text = extract.extract_text(name, data)
            results.append(_ingest(db, e, text, name, "file", "", ai))
        except extract.ExtractError as exc:
            results.append({"name": name, "found": 0, "added": 0, "duplicates": 0, "error": str(exc)})
    return {"results": results}


class ScrapeIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10)
    ai: bool = False


@router.post("/exams/{exam_id}/scrape")
def scrape(exam_id: int, body: ScrapeIn, user: User = Depends(throttled), db: Session = Depends(get_db)):
    e = _exam(db, exam_id)
    results = []
    for url in body.urls:
        url = url.strip()
        if not url:
            continue
        try:
            text, title = scraper.fetch(url)
            results.append(_ingest(db, e, text, title or url, "url", url, body.ai))
        except scraper.ScrapeError as exc:
            results.append({"name": url, "found": 0, "added": 0, "duplicates": 0, "error": str(exc)})
    return {"results": results}


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    s = db.get(Source, source_id)
    if not s:
        raise HTTPException(404, "Source not found")
    db.query(Question).filter(Question.source_id == s.id).delete()
    db.delete(s)
    db.commit()
    return {"ok": True}


# ---------- questions ----------


class QuestionIn(BaseModel):
    text: str = Field(min_length=3, max_length=5000)
    options: list[str] = Field(default_factory=list, max_length=8)
    correct: list[int] = Field(default_factory=list)
    explanation: str = Field(default="", max_length=4000)
    answer_text: str = Field(default="", max_length=4000)


@router.get("/questions/{qid}")
def get_question(qid: int, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    return exams.question_json(_question(db, qid))


@router.patch("/questions/{qid}")
def edit_question(qid: int, body: QuestionIn, user: User = Depends(ui_user), db: Session = Depends(get_db)):
    q = _question(db, qid)
    options = [o.strip() for o in body.options if o.strip()]
    if options and len(options) < 2:
        raise HTTPException(422, "A multiple-choice question needs at least 2 options.")
    if any(c < 0 or c >= len(options) for c in body.correct):
        raise HTTPException(422, "A correct answer points to a missing option.")
    q.qtype = "mc" if options else "open"
    q.text, q.options_json = body.text.strip(), json.dumps(options)
    q.correct_json, q.explanation = json.dumps(sorted(set(body.correct))), body.explanation.strip()
    q.answer_text = body.answer_text.strip()
    q.fingerprint = parser.fingerprint(q.text, options)
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
    db.delete(_question(db, qid))
    db.commit()
    return {"ok": True}


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
