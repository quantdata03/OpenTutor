"""Server-verified whole-practice sessions and immutable History reads."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from libs.exceptions import ConflictError, NotFoundError, ValidationError
from models.practice import (
    PracticeProblem,
    PracticeResult,
    PracticeSession,
    PracticeSessionItem,
)
from models.user import User
from routers.quiz_submission import persist_problem_answer
from schemas.practice_session import (
    PracticeSessionAnswerRequest,
    PracticeSessionAnswerResponse,
    PracticeSessionDetailItem,
    PracticeSessionDetailResponse,
    PracticeSessionFinalizeRequest,
    PracticeSessionListResponse,
    PracticeSessionStartItem,
    PracticeSessionStartRequest,
    PracticeSessionStartResponse,
    PracticeSessionSummary,
)
from schemas.quiz import SubmitAnswerRequest
from services.auth.dependency import get_current_user
from services.course_access import get_course_or_404

router = APIRouter()


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _metadata_value(metadata: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = metadata.get(name)
        if value not in (None, "", []):
            return value
    return None


def _review_snapshot(problem: PracticeProblem) -> dict[str, Any]:
    """Freeze only the persisted fields needed to grade and reopen review."""
    metadata = (
        problem.problem_metadata if isinstance(problem.problem_metadata, dict) else {}
    )
    source_pages = metadata.get("source_pages")
    if not isinstance(source_pages, list):
        page = metadata.get("source_page")
        source_pages = (
            [page] if isinstance(page, int) and not isinstance(page, bool) else []
        )
    return {
        "question": problem.question,
        "question_type": problem.question_type,
        "options": problem.options,
        "correct_answer": problem.correct_answer,
        "explanation": problem.explanation
        or _metadata_value(metadata, "detailed_explanation"),
        "reasoning": _metadata_value(
            metadata, "reasoning", "solution_reasoning", "layer_justification"
        ),
        "solution_steps": _metadata_value(metadata, "solution_steps", "steps"),
        "knowledge_point_analysis": _metadata_value(
            metadata, "knowledge_point_analysis", "knowledge_analysis"
        ),
        "core_concept": _metadata_value(metadata, "core_concept", "core"),
        "review_presentation_version": metadata.get("review_presentation_version"),
        "option_analysis": metadata.get("option_analysis"),
        "term_data_analysis": metadata.get("term_data_analysis"),
        "detailed_reasoning": metadata.get("detailed_reasoning"),
        "formula_chain": metadata.get("formula_chain"),
        "conclusion": metadata.get("conclusion"),
        "misconception": metadata.get("misconception"),
        "known_information": _metadata_value(
            metadata, "known_information", "given_information", "known"
        ),
        "common_mistake": _metadata_value(
            metadata, "common_mistake", "common_errors", "potential_traps"
        ),
        "knowledge_points": problem.knowledge_points or [],
        "difficulty_layer": problem.difficulty_layer,
        "problem_metadata": {"answer_type": metadata.get("answer_type")}
        if metadata.get("answer_type")
        else {},
        "chapter": _metadata_value(metadata, "chapter", "unit"),
        "topic": _metadata_value(metadata, "topic", "source_section"),
        "source_pages": source_pages,
        "visual_spec": metadata.get("visual_spec"),
        "review_visual_spec": metadata.get("review_visual_spec"),
        "source_version": problem.source_version,
    }


def _answer_response(
    session_id: uuid.UUID,
    result: PracticeResult,
    snapshot: dict[str, Any],
    *,
    idempotent: bool,
    prerequisite_gaps: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> PracticeSessionAnswerResponse:
    return PracticeSessionAnswerResponse(
        result_id=result.id,
        answered_at=_utc(result.answered_at),
        practice_session_id=session_id,
        idempotent=idempotent,
        is_correct=bool(result.is_correct),
        correct_answer=snapshot.get("correct_answer"),
        explanation=snapshot.get("explanation"),
        reasoning=snapshot.get("reasoning"),
        solution_steps=snapshot.get("solution_steps"),
        knowledge_point_analysis=snapshot.get("knowledge_point_analysis")
        or snapshot.get("knowledge_points"),
        review_visual_spec=snapshot.get("review_visual_spec"),
        prerequisite_gaps=prerequisite_gaps,
        warnings=warnings or [],
    )


async def _session_items(
    db: AsyncSession, session_id: uuid.UUID
) -> list[PracticeSessionItem]:
    result = await db.execute(
        select(PracticeSessionItem)
        .where(PracticeSessionItem.session_id == session_id)
        .order_by(PracticeSessionItem.ordinal)
    )
    return list(result.scalars().all())


def _summary(
    session: PracticeSession, items: list[PracticeSessionItem]
) -> PracticeSessionSummary:
    if session.status != "finalized" or session.finalized_at is None:
        raise ConflictError("Practice session is not finalized")
    result_ids = [item.result_id for item in items if item.result_id is not None]
    return PracticeSessionSummary(
        id=session.id,
        status=session.status,
        course_id=session.course_id,
        started_at=_utc(session.started_at),
        finalized_at=_utc(session.finalized_at),
        total_count=int(session.total_count or 0),
        correct_count=int(session.correct_count or 0),
        blank_count=int(session.blank_count or 0),
        wrong_count=int(session.total_count or 0)
        - int(session.correct_count or 0)
        - int(session.blank_count or 0),
        result_ids=result_ids,
    )


def _encode_cursor(session: PracticeSession) -> str:
    payload = json.dumps(
        {"finalized_at": _utc(session.finalized_at).isoformat(), "id": str(session.id)},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        timestamp = datetime.fromisoformat(payload["finalized_at"])
        identifier = uuid.UUID(payload["id"])
    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ValidationError("Invalid practice-session cursor") from exc
    if timestamp.tzinfo is None:
        raise ValidationError("Invalid practice-session cursor")
    return timestamp.astimezone(timezone.utc), identifier


@router.post("", response_model=PracticeSessionStartResponse, status_code=201)
async def start_practice_session(
    body: PracticeSessionStartRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await get_course_or_404(db, body.course_id, user_id=user.id)
    rows = await db.execute(
        select(PracticeProblem).where(
            PracticeProblem.id.in_(body.problem_ids),
            PracticeProblem.course_id == body.course_id,
            PracticeProblem.is_archived == False,  # noqa: E712
            PracticeProblem.is_diagnostic == False,  # noqa: E712
        )
    )
    by_id = {problem.id: problem for problem in rows.scalars().all()}
    if len(by_id) != len(body.problem_ids):
        raise NotFoundError("Active practice problem")

    session = PracticeSession(user_id=user.id, course_id=body.course_id, status="open")
    db.add(session)
    await db.flush()
    items = []
    for ordinal, problem_id in enumerate(body.problem_ids, start=1):
        item = PracticeSessionItem(
            session_id=session.id,
            ordinal=ordinal,
            problem_id=problem_id,
            review_snapshot=_review_snapshot(by_id[problem_id]),
            is_blank=False,
        )
        db.add(item)
        items.append(item)
    await db.commit()
    await db.refresh(session)
    return PracticeSessionStartResponse(
        id=session.id,
        status=session.status,
        course_id=session.course_id,
        started_at=_utc(session.started_at),
        total_count=len(items),
        items=[
            PracticeSessionStartItem(ordinal=item.ordinal, problem_id=item.problem_id)
            for item in items
        ],
    )


@router.post("/{session_id}/answers", response_model=PracticeSessionAnswerResponse)
async def submit_practice_session_answer(
    session_id: uuid.UUID,
    body: PracticeSessionAnswerRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = (
        await db.execute(
            select(PracticeSession).where(
                PracticeSession.id == session_id,
                PracticeSession.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if not session:
        raise NotFoundError("Practice session", session_id)
    if session.status != "open":
        raise ConflictError("Practice session is already finalized")
    item = (
        await db.execute(
            select(PracticeSessionItem).where(
                PracticeSessionItem.session_id == session_id,
                PracticeSessionItem.problem_id == body.problem_id,
            )
        )
    ).scalar_one_or_none()
    if not item:
        raise NotFoundError("Practice session item", body.problem_id)
    snapshot = dict(item.review_snapshot or {})
    if item.result_id is not None:
        if item.submission_key != body.submission_key:
            raise ConflictError("Practice item already has a different submission")
        if item.user_answer != body.user_answer:
            raise ConflictError(
                "Submission key was already used with a different answer"
            )
        saved_result = await db.get(PracticeResult, item.result_id)
        if not saved_result or saved_result.user_id != user.id:
            raise ConflictError("Practice item result link is invalid")
        return _answer_response(session_id, saved_result, snapshot, idempotent=True)

    try:
        reservation = await db.execute(
            update(PracticeSessionItem)
            .where(
                PracticeSessionItem.id == item.id,
                PracticeSessionItem.result_id.is_(None),
                PracticeSessionItem.submission_key.is_(None),
            )
            .values(submission_key=body.submission_key)
        )
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("Submission key is already in use") from exc
    if reservation.rowcount != 1:
        await db.rollback()
        retry_item = (
            await db.execute(
                select(PracticeSessionItem).where(PracticeSessionItem.id == item.id)
            )
        ).scalar_one()
        if retry_item.submission_key == body.submission_key and retry_item.result_id:
            saved_result = await db.get(PracticeResult, retry_item.result_id)
            if saved_result and saved_result.user_id == user.id:
                return _answer_response(
                    session_id, saved_result, snapshot, idempotent=True
                )
        raise ConflictError(
            "Practice item submission is already in progress or completed"
        )

    item.submission_key = body.submission_key
    problem = await db.get(PracticeProblem, body.problem_id)
    if not problem or problem.course_id != session.course_id:
        await db.rollback()
        raise NotFoundError("Practice problem", body.problem_id)
    try:
        saved = await persist_problem_answer(
            SubmitAnswerRequest(
                problem_id=body.problem_id,
                user_answer=body.user_answer,
                answer_time_ms=body.answer_time_ms,
            ),
            background_tasks,
            user,
            db,
            problem=problem,
            review_snapshot=snapshot,
            session_item=item,
        )
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("Submission key is already in use") from exc
    return _answer_response(
        session_id,
        saved["result"],
        snapshot,
        idempotent=False,
        prerequisite_gaps=saved["prerequisite_gaps"],
        warnings=saved["warnings"],
    )


@router.post("/{session_id}/finalize", response_model=PracticeSessionSummary)
async def finalize_practice_session(
    session_id: uuid.UUID,
    body: PracticeSessionFinalizeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = (
        await db.execute(
            select(PracticeSession).where(
                PracticeSession.id == session_id,
                PracticeSession.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if not session:
        raise NotFoundError("Practice session", session_id)
    items = await _session_items(db, session_id)
    expected_ids = [item.result_id for item in items]

    if session.status == "finalized":
        if session.finalize_key == body.finalize_key and set(expected_ids) == set(
            body.result_ids
        ):
            return _summary(session, items)
        raise ConflictError("Practice session was finalized with a different request")
    if len(items) != len(body.result_ids) or any(
        result_id is None for result_id in expected_ids
    ):
        raise ConflictError(
            "Every practice item must have exactly one persisted result"
        )
    if set(expected_ids) != set(body.result_ids):
        raise ConflictError("Finalized result IDs do not match this practice session")

    results = await db.execute(
        select(PracticeResult).where(
            PracticeResult.id.in_(body.result_ids),
            PracticeResult.user_id == user.id,
        )
    )
    results_by_id = {result.id: result for result in results.scalars().all()}
    if len(results_by_id) != len(body.result_ids):
        raise ConflictError("One or more results do not belong to this user")
    for item in items:
        result = results_by_id.get(item.result_id)
        if not result or result.problem_id != item.problem_id:
            raise ConflictError("Result ownership does not match the practice item")

    try:
        reservation = await db.execute(
            update(PracticeSession)
            .where(
                PracticeSession.id == session_id,
                PracticeSession.user_id == user.id,
                PracticeSession.status == "open",
                PracticeSession.finalize_key.is_(None),
            )
            .values(finalize_key=body.finalize_key)
        )
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("Finalize key is already in use") from exc
    if reservation.rowcount != 1:
        await db.rollback()
        current = (
            await db.execute(
                select(PracticeSession).where(
                    PracticeSession.id == session_id,
                    PracticeSession.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        current_items = await _session_items(db, session_id) if current else []
        if (
            current
            and current.status == "finalized"
            and current.finalize_key == body.finalize_key
            and {item.result_id for item in current_items} == set(body.result_ids)
        ):
            return _summary(current, current_items)
        raise ConflictError(
            "Practice session finalization is already in progress or completed"
        )

    correct_count = 0
    blank_count = 0
    for item in items:
        result = results_by_id[item.result_id]
        item.user_answer = result.user_answer
        item.is_correct = bool(result.is_correct)
        item.is_blank = not bool(result.user_answer.strip())
        correct_count += int(bool(result.is_correct))
        blank_count += int(item.is_blank)
    session.finalize_key = body.finalize_key
    session.status = "finalized"
    session.finalized_at = datetime.now(timezone.utc)
    session.total_count = len(items)
    session.correct_count = correct_count
    session.blank_count = blank_count
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("Finalize key is already in use") from exc
    await db.refresh(session)
    return _summary(session, items)


@router.get("", response_model=PracticeSessionListResponse)
async def list_practice_sessions(
    course_id: uuid.UUID,
    start_utc: datetime,
    end_utc: datetime,
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await get_course_or_404(db, course_id, user_id=user.id)
    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise ValidationError("History bounds must include UTC offsets")
    start = start_utc.astimezone(timezone.utc)
    end = end_utc.astimezone(timezone.utc)
    if start >= end or end - start > timedelta(days=31):
        raise ValidationError("History range must be positive and no more than 31 days")

    query = select(PracticeSession).where(
        PracticeSession.user_id == user.id,
        PracticeSession.course_id == course_id,
        PracticeSession.status == "finalized",
        PracticeSession.finalized_at >= start,
        PracticeSession.finalized_at < end,
    )
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                PracticeSession.finalized_at < cursor_time,
                and_(
                    PracticeSession.finalized_at == cursor_time,
                    PracticeSession.id < cursor_id,
                ),
            )
        )
    rows = await db.execute(
        query.order_by(
            PracticeSession.finalized_at.desc(), PracticeSession.id.desc()
        ).limit(limit + 1)
    )
    sessions = list(rows.scalars().all())
    has_more = len(sessions) > limit
    sessions = sessions[:limit]

    item_map: dict[uuid.UUID, list[PracticeSessionItem]] = {
        session.id: [] for session in sessions
    }
    if sessions:
        item_rows = await db.execute(
            select(PracticeSessionItem)
            .where(PracticeSessionItem.session_id.in_(item_map))
            .order_by(PracticeSessionItem.session_id, PracticeSessionItem.ordinal)
        )
        for item in item_rows.scalars().all():
            item_map[item.session_id].append(item)
    return PracticeSessionListResponse(
        sessions=[_summary(session, item_map[session.id]) for session in sessions],
        next_cursor=_encode_cursor(sessions[-1]) if has_more and sessions else None,
    )


@router.get("/{session_id}", response_model=PracticeSessionDetailResponse)
async def get_practice_session(
    session_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = (
        await db.execute(
            select(PracticeSession).where(
                PracticeSession.id == session_id,
                PracticeSession.user_id == user.id,
                PracticeSession.status == "finalized",
            )
        )
    ).scalar_one_or_none()
    if not session:
        raise NotFoundError("Finalized practice session", session_id)
    items = await _session_items(db, session_id)
    return PracticeSessionDetailResponse(
        session=_summary(session, items),
        items=[
            PracticeSessionDetailItem(
                ordinal=item.ordinal,
                problem_id=item.problem_id,
                result_id=item.result_id,
                user_answer=item.user_answer or "",
                is_correct=bool(item.is_correct),
                is_blank=bool(item.is_blank),
                review_snapshot=dict(item.review_snapshot or {}),
            )
            for item in items
        ],
    )
