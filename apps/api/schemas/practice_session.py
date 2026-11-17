"""Schemas for trustworthy whole-practice sessions."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class PracticeSessionStartRequest(BaseModel):
    course_id: uuid.UUID
    problem_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_problem_ids(self):
        if len(set(self.problem_ids)) != len(self.problem_ids):
            raise ValueError("problem_ids must be unique")
        return self


class PracticeSessionAnswerRequest(BaseModel):
    problem_id: uuid.UUID
    user_answer: str = Field(..., max_length=5000)
    answer_time_ms: int | None = Field(default=None, ge=0)
    submission_key: str = Field(min_length=1, max_length=128)


class PracticeSessionFinalizeRequest(BaseModel):
    finalize_key: str = Field(min_length=1, max_length=128)
    result_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_result_ids(self):
        if len(set(self.result_ids)) != len(self.result_ids):
            raise ValueError("result_ids must be unique")
        return self


class PracticeSessionStartItem(BaseModel):
    ordinal: int
    problem_id: uuid.UUID


class PracticeSessionStartResponse(BaseModel):
    id: uuid.UUID
    status: str
    course_id: uuid.UUID
    started_at: datetime
    total_count: int
    items: list[PracticeSessionStartItem]


class PracticeSessionAnswerResponse(BaseModel):
    result_id: uuid.UUID
    answered_at: datetime
    practice_session_id: uuid.UUID
    idempotent: bool
    is_correct: bool
    correct_answer: str | None
    explanation: str | None
    reasoning: str | list[Any] | None = None
    solution_steps: list[Any] | str | None = None
    knowledge_point_analysis: str | list[Any] | dict[str, Any] | None = None
    review_visual_spec: dict[str, Any] | None = None
    prerequisite_gaps: list[dict[str, Any]] | None = None
    warnings: list[str] = Field(default_factory=list)


class PracticeSessionSummary(BaseModel):
    id: uuid.UUID
    status: str
    course_id: uuid.UUID
    started_at: datetime
    finalized_at: datetime
    total_count: int
    correct_count: int
    blank_count: int
    wrong_count: int
    result_ids: list[uuid.UUID]


class PracticeSessionListResponse(BaseModel):
    sessions: list[PracticeSessionSummary]
    next_cursor: str | None = None


class PracticeSessionDetailItem(BaseModel):
    ordinal: int
    problem_id: uuid.UUID
    result_id: uuid.UUID
    user_answer: str
    is_correct: bool
    is_blank: bool
    review_snapshot: dict[str, Any]


class PracticeSessionDetailResponse(BaseModel):
    session: PracticeSessionSummary
    items: list[PracticeSessionDetailItem]
