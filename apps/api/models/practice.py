"""Practice problem and result models."""

import uuid
from typing import Optional
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from models.compat import CompatUUID, CompatJSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class PracticeProblem(Base):
    """Practice problems extracted from course content."""

    __tablename__ = "practice_problems"
    __table_args__ = (
        Index("ix_practice_problem_course", "course_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(CompatUUID, primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID] = mapped_column(CompatUUID, ForeignKey("courses.id", ondelete="CASCADE"))
    content_node_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        CompatUUID, ForeignKey("course_content_tree.id", ondelete="SET NULL"), nullable=True
    )

    # Problem data
    question_type: Mapped[str] = mapped_column(String(20))  # mc, tf, short_answer, fill_blank, matching, select_all, free_response
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[Optional[dict]] = mapped_column(CompatJSONB, nullable=True)  # For MC/matching
    correct_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    # v3: Knowledge point tagging + source tracking
    knowledge_points: Mapped[Optional[list]] = mapped_column(CompatJSONB, nullable=True, default=list)
    source: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Sources: extracted | ai_generated | derived

    # v4: VCE-inspired diagnostic fields
    difficulty_layer: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 1=basic concept recall, 2=standard application, 3=trap/edge case
    problem_metadata: Mapped[Optional[dict]] = mapped_column(CompatJSONB, nullable=True)
    # AI-generated structured annotation: {potential_traps, core_concept, bloom_level, ...}
    parent_problem_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        CompatUUID, ForeignKey("practice_problems.id", ondelete="SET NULL"), nullable=True
    )
    is_diagnostic: Mapped[bool] = mapped_column(Boolean, default=False)
    # True for simplified "clean" versions generated for diagnostic pairs
    source_batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(CompatUUID, nullable=True)
    source_version: Mapped[int] = mapped_column(Integer, default=1)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)

    # Content ownership tracking (for agent vs user content)
    source_owner: Mapped[str] = mapped_column(String(20), default="ai")
    # Values: "ai" | "user" | "ai+user_edited"
    locked: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    course = relationship("Course", back_populates="practice_problems")
    results = relationship("PracticeResult", back_populates="problem", cascade="all, delete-orphan")


class PracticeResult(Base):
    """User answers to practice problems."""

    __tablename__ = "practice_results"
    __table_args__ = (
        # Per-user answer history for a problem (mastery/wrong-answer queries)
        Index("ix_practice_result_user_problem", "user_id", "problem_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(CompatUUID, primary_key=True, default=uuid.uuid4)
    problem_id: Mapped[uuid.UUID] = mapped_column(CompatUUID, ForeignKey("practice_problems.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(CompatUUID, ForeignKey("users.id", ondelete="CASCADE"))

    user_answer: Mapped[str] = mapped_column(Text)
    is_correct: Mapped[bool] = mapped_column(Boolean)
    ai_explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # v4: Error tracking for cross-type triangulation
    error_category: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # conceptual | procedural | computational | reading | careless
    difficulty_layer: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    answer_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    problem = relationship("PracticeProblem", back_populates="results")


class PracticeSession(Base):
    """A server-verified, immutable whole-practice attempt."""

    __tablename__ = "practice_sessions"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'finalized')", name="ck_practice_session_status"),
        UniqueConstraint("user_id", "finalize_key", name="uq_practice_session_user_finalize_key"),
        Index("ix_practice_session_user_finalized", "user_id", "finalized_at", "id"),
        Index(
            "ix_practice_session_user_course_finalized",
            "user_id",
            "course_id",
            "finalized_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(CompatUUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        CompatUUID, ForeignKey("users.id", ondelete="CASCADE")
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        CompatUUID, ForeignKey("courses.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(12), default="open")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finalized_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    correct_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    blank_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    finalize_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    items = relationship(
        "PracticeSessionItem",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="PracticeSessionItem.ordinal",
    )


class PracticeSessionItem(Base):
    """An ordered practice item linked to at most one persisted result."""

    __tablename__ = "practice_session_items"
    __table_args__ = (
        UniqueConstraint("session_id", "ordinal", name="uq_practice_session_item_ordinal"),
        UniqueConstraint("session_id", "problem_id", name="uq_practice_session_item_problem"),
        UniqueConstraint(
            "session_id", "submission_key", name="uq_practice_session_item_submission_key"
        ),
        UniqueConstraint("result_id", name="uq_practice_session_item_result"),
        Index("ix_practice_session_item_session", "session_id", "ordinal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(CompatUUID, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        CompatUUID, ForeignKey("practice_sessions.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    problem_id: Mapped[uuid.UUID] = mapped_column(
        CompatUUID, ForeignKey("practice_problems.id", ondelete="RESTRICT")
    )
    result_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        CompatUUID,
        ForeignKey("practice_results.id", ondelete="RESTRICT"),
        nullable=True,
    )
    submission_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_blank: Mapped[bool] = mapped_column(Boolean, default=False)
    user_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_correct: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    review_snapshot: Mapped[dict] = mapped_column(CompatJSONB)

    session = relationship("PracticeSession", back_populates="items")
