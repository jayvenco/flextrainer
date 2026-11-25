from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    theme: Mapped[str] = mapped_column(String(32), default="midnight")
    font: Mapped[str] = mapped_column(String(32), default="inter")
    font_size: Mapped[int] = mapped_column(Integer, default=100, server_default="100")  # percent, see FONT_SIZES


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Exam(Base):
    __tablename__ = "exams"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    questions: Mapped[list["Question"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan", order_by="Question.position, Question.id"
    )
    sources: Mapped[list["Source"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan", order_by="Source.id"
    )
    sessions: Mapped[list["PracticeSession"]] = relationship(back_populates="exam", cascade="all, delete-orphan")


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # file | url
    name: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(2000), default="")
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    exam: Mapped[Exam] = relationship(back_populates="sources")


class Image(Base):
    """Picture that belongs to questions of one exam (stored in the database so backups include it)."""

    __tablename__ = "images"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    sha1: Mapped[str] = mapped_column(String(40))
    mime: Mapped[str] = mapped_column(String(32))
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary)


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (UniqueConstraint("exam_id", "fingerprint", name="uq_question_fp"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    qtype: Mapped[str] = mapped_column(String(8), default="mc")  # mc | open
    text: Mapped[str] = mapped_column(Text)
    options_json: Mapped[str] = mapped_column(Text, default="[]")
    correct_json: Mapped[str] = mapped_column(Text, default="[]")  # answer key from the source
    answer_text: Mapped[str] = mapped_column(Text, default="")  # reference answer for open questions
    explanation: Mapped[str] = mapped_column(Text, default="")
    fingerprint: Mapped[str] = mapped_column(String(40))
    images_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")  # ids in the images table
    # ChatGPT check
    verify_status: Mapped[str] = mapped_column(String(16), default="unchecked")
    # unchecked | confirmed | disputed | answered | error
    verify_note: Mapped[str] = mapped_column(Text, default="")
    verify_link: Mapped[str] = mapped_column(String(2000), default="")
    ai_correct_json: Mapped[str] = mapped_column(Text, default="[]")
    ai_answer: Mapped[str] = mapped_column(Text, default="")
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    exam: Mapped[Exam] = relationship(back_populates="questions")
    attempts: Mapped[list["Attempt"]] = relationship(back_populates="question", cascade="all, delete-orphan")

    @property
    def options(self) -> list[str]:
        return json.loads(self.options_json or "[]")

    @property
    def images(self) -> list[int]:
        return json.loads(self.images_json or "[]")

    @property
    def correct(self) -> list[int]:
        return json.loads(self.correct_json or "[]")

    @property
    def ai_correct(self) -> list[int]:
        return json.loads(self.ai_correct_json or "[]")

    @property
    def effective_correct(self) -> list[int]:
        return self.correct or self.ai_correct

    @property
    def reference_answer(self) -> str:
        return self.answer_text or self.ai_answer


class PracticeSession(Base):
    __tablename__ = "practice_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="smart")
    order_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    exam: Mapped[Exam] = relationship(back_populates="sessions")
    attempts: Mapped[list["Attempt"]] = relationship(back_populates="session", cascade="all, delete-orphan")

    @property
    def order(self) -> list[int]:
        return json.loads(self.order_json or "[]")


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("practice_sessions.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    chosen_json: Mapped[str] = mapped_column(Text, default="[]")
    correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # None = skipped / no key
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    session: Mapped[PracticeSession] = relationship(back_populates="attempts")
    question: Mapped[Question] = relationship(back_populates="attempts")
