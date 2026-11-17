"""Quiz submission endpoints: submit answers, list problems, mastery history."""

import asyncio
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session, get_db
from models.course import Course
from models.practice import PracticeProblem, PracticeResult
from models.user import User
from schemas.quiz import (
    AnswerResponse,
    MasterySnapshotResponse,
    ProblemResponse,
    SubmitAnswerRequest,
)
from services.practice.answers import grade_select_all_answer, grade_text_answer
from services.practice.annotation import normalize_question_options
from services.auth.dependency import get_current_user
from services.course_access import get_course_or_404
from sqlalchemy.exc import SQLAlchemyError

from libs.exceptions import NotFoundError, ValidationError

router = APIRouter()
_post_submission_tasks: set[asyncio.Task[None]] = set()


@router.get("/{course_id}", response_model=list[ProblemResponse], summary="List practice problems", description="Return paginated practice problems for a course, excluding diagnostics.")
async def list_problems(
    course_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List user-facing practice problems for a course."""
    await get_course_or_404(db, course_id, user_id=user.id)

    result = await db.execute(
        select(PracticeProblem)
        .where(PracticeProblem.course_id == course_id)
        .where(PracticeProblem.is_diagnostic == False)
        .where(PracticeProblem.is_archived == False)
        .order_by(PracticeProblem.order_index)
        .offset(offset)
        .limit(limit)
    )
    return result.scalars().all()


async def _auto_derive_diagnostic(wrong_answer_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Background task: auto-generate a diagnostic pair for a wrong answer."""
    try:
        async with async_session() as db:
            from models.ingestion import WrongAnswer
            from models.practice import PracticeProblem as PP
            result = await db.execute(
                select(WrongAnswer, PP)
                .join(PP, WrongAnswer.problem_id == PP.id)
                .where(WrongAnswer.id == wrong_answer_id, WrongAnswer.user_id == user_id)
            )
            row = result.one_or_none()
            if not row:
                return
            wa, problem = row
            # Skip if diagnostic pair already exists
            existing = await db.execute(
                select(PP.id).where(PP.parent_problem_id == problem.id, PP.is_diagnostic == True)
            )
            if existing.scalar_one_or_none():
                return
            if problem.is_diagnostic or (problem.difficulty_layer and problem.difficulty_layer < 2):
                return

            from services.diagnosis.derive import derive_diagnostic
            await derive_diagnostic(db, wa, problem)
            await db.commit()
            logger.info("Auto-generated diagnostic pair for wrong answer %s", wrong_answer_id)
    except (SQLAlchemyError, ValueError, KeyError, TypeError):
        logger.exception("Auto-derive diagnostic failed (best-effort)")


async def _check_effective_review(
    problem_id: uuid.UUID, course_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Background: if user previously got this problem wrong, record effective_review signal."""
    try:
        async with async_session() as db:
            prev_wrong = await db.execute(
                select(PracticeResult.id).where(
                    PracticeResult.problem_id == problem_id,
                    PracticeResult.user_id == user_id,
                    PracticeResult.is_correct == False,  # noqa: E712
                ).limit(1)
            )
            if prev_wrong.scalar_one_or_none() is None:
                return  # No prior wrong answer — not an improvement
            from services.block_decision.preference import record_block_event
            await record_block_event(db, user_id, course_id, "review", "effective_review")
            await db.commit()
            logger.info("Recorded effective_review signal for problem %s", problem_id)
    except (SQLAlchemyError, ValueError, ImportError):
        logger.exception("Effective review check failed (best-effort)")


async def _auto_detect_confusion(course_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Background task: detect confusion pairs from accumulated wrong answers."""
    try:
        async with async_session() as db:
            from services.loom_confusion import detect_confusion_pairs
            await detect_confusion_pairs(db, course_id, user_id, min_occurrences=1)
            await db.commit()
    except (SQLAlchemyError, ValueError, KeyError):
        logger.exception("Auto confusion detection failed (best-effort)")


async def _classify_wrong_answer(
    practice_result_id: uuid.UUID,
    wrong_answer_id: uuid.UUID,
    problem_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Background task: classify an already-persisted wrong answer."""
    try:
        async with async_session() as db:
            from models.ingestion import WrongAnswer
            result = await db.execute(
                select(PracticeResult, WrongAnswer, PracticeProblem)
                .join(WrongAnswer, WrongAnswer.problem_id == PracticeResult.problem_id)
                .join(PracticeProblem, PracticeProblem.id == PracticeResult.problem_id)
                .where(
                    PracticeResult.id == practice_result_id,
                    WrongAnswer.id == wrong_answer_id,
                    PracticeProblem.id == problem_id,
                    PracticeResult.user_id == user_id,
                    WrongAnswer.user_id == user_id,
                )
            )
            row = result.one_or_none()
            if not row:
                return
            practice_result, wrong_answer, problem = row
            from services.diagnosis.classifier import classify_error
            classification = await classify_error(
                question=problem.question,
                correct_answer=problem.correct_answer or "",
                user_answer=practice_result.user_answer,
                problem_metadata=problem.problem_metadata,
            )
            error_category = classification["category"]
            practice_result.error_category = error_category
            wrong_answer.error_category = error_category
            wrong_answer.error_detail = classification
            await db.commit()
    except (SQLAlchemyError, ValueError, KeyError, TypeError, OSError):
        logger.exception("Background error classification failed (best-effort)")


async def _process_wrong_answer_after_response(
    practice_result_id: uuid.UUID,
    wrong_answer_id: uuid.UUID,
    problem_id: uuid.UUID,
    course_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Run the existing best-effort diagnosis chain outside request completion."""
    await _classify_wrong_answer(
        practice_result_id, wrong_answer_id, problem_id, user_id
    )
    await _auto_derive_diagnostic(wrong_answer_id, user_id)
    await _auto_detect_confusion(course_id, user_id)


async def _schedule_wrong_answer_postprocessing(
    practice_result_id: uuid.UUID,
    wrong_answer_id: uuid.UUID,
    problem_id: uuid.UUID,
    course_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Detach post-submit diagnosis after the response body has been sent."""
    task = asyncio.create_task(
        _process_wrong_answer_after_response(
            practice_result_id, wrong_answer_id, problem_id, course_id, user_id
        )
    )
    _post_submission_tasks.add(task)
    task.add_done_callback(_post_submission_tasks.discard)


async def persist_problem_answer(
    body: SubmitAnswerRequest,
    background_tasks: BackgroundTasks,
    user: User,
    db: AsyncSession,
    *,
    problem: PracticeProblem | None = None,
    review_snapshot: dict[str, Any] | None = None,
    session_item: Any | None = None,
) -> dict[str, Any]:
    """Grade and persist one answer through the canonical quiz write path.

    Session-aware callers reserve their item before entering this function and
    pass it as ``session_item``. The result link is then committed in the same
    transaction as PracticeResult, WrongAnswer and the existing progress rows.
    """
    if problem is None:
        result = await db.execute(
            select(PracticeProblem)
            .join(Course, PracticeProblem.course_id == Course.id)
            .where(
                PracticeProblem.id == body.problem_id,
                Course.user_id == user.id,
            )
        )
        problem = result.scalar_one_or_none()
    if not problem:
        raise NotFoundError("Problem", body.problem_id)

    snapshot = review_snapshot or {}
    question_type = str(snapshot.get("question_type") or problem.question_type)
    question = str(snapshot.get("question") or problem.question)
    correct_answer = snapshot.get("correct_answer", problem.correct_answer)
    explanation = snapshot.get("explanation", problem.explanation)
    options = snapshot.get("options", problem.options)
    problem_metadata = snapshot.get("problem_metadata", problem.problem_metadata)
    difficulty_layer = snapshot.get("difficulty_layer", problem.difficulty_layer)
    knowledge_points = snapshot.get("knowledge_points", problem.knowledge_points)

    # A missing answer key is a question defect, not evidence of learner error.
    # Coding has its own grading contract and may not require a reference.
    if question_type != "coding" and not str(correct_answer or "").strip():
        raise ValidationError("This question has no reference answer and cannot be graded.")

    warnings: list[str] = []
    is_correct = False
    if question_type == "coding":
        try:
            from services.diagnosis.coding_grader import grade_coding_answer
            grading_result = await grade_coding_answer(
                question=question,
                reference_answer=str(correct_answer or ""),
                user_code=body.user_answer,
            )
            is_correct = grading_result.get("is_correct", False)
        except (ValueError, KeyError, TypeError, OSError):
            logger.exception("Coding grading failed (best-effort)")
            warnings.append("coding_grading_failed")
    elif question_type == "select_all":
        is_correct = grade_select_all_answer(
            body.user_answer,
            str(correct_answer or ""),
            normalize_question_options(options),
        )
    elif correct_answer:
        is_correct = grade_text_answer(
            body.user_answer,
            str(correct_answer),
            numeric=(
                question_type in {"short_answer", "fill_blank"}
                and isinstance(problem_metadata, dict)
                and problem_metadata.get("answer_type") == "number"
            ),
        )

    pr = PracticeResult(
        problem_id=problem.id,
        user_id=user.id,
        user_answer=body.user_answer,
        is_correct=is_correct,
        ai_explanation=explanation,
        difficulty_layer=difficulty_layer,
        answer_time_ms=body.answer_time_ms,
    )
    db.add(pr)

    error_category = None
    wa = None
    if not is_correct:
        from models.ingestion import WrongAnswer
        wa = WrongAnswer(
            user_id=user.id,
            problem_id=problem.id,
            course_id=problem.course_id,
            user_answer=body.user_answer,
            correct_answer=correct_answer,
            explanation=explanation,
            error_category=error_category,
            error_detail=None,
            knowledge_points=knowledge_points,
        )
        db.add(wa)

    try:
        from services.progress.tracker import update_quiz_result
        await update_quiz_result(
            db, user.id, problem.course_id, problem.content_node_id,
            is_correct=is_correct,
            error_category=error_category,
        )
    except (SQLAlchemyError, ValueError, TypeError):
        logger.exception("Progress update failed (best-effort)")
        warnings.append("progress_update_failed")

    kp_list: list[str] = (
        [str(x) for x in knowledge_points] if isinstance(knowledge_points, list)
        else [knowledge_points] if isinstance(knowledge_points, str)
        else []
    )
    if kp_list:
        from services.loom_mastery import update_concept_mastery
        for kp in kp_list:
            try:
                await update_concept_mastery(
                    db,
                    user.id,
                    str(kp),
                    problem.course_id,
                    correct=is_correct,
                    question_type=question_type,
                )
            except (SQLAlchemyError, ValueError, KeyError):
                logger.exception("Concept mastery update failed for '%s'", kp)
                warnings.append("concept_mastery_update_failed")

    try:
        from services.analytics.events import emit_quiz_answered
        await emit_quiz_answered(
            db,
            user_id=user.id,
            course_id=problem.course_id,
            quiz_id=str(problem.id),
            score=1.0 if is_correct else 0.0,
            correct=is_correct,
            agent_name="quiz_router",
            answers={"user_answer": body.user_answer, "error_category": error_category},
        )
    except (SQLAlchemyError, ValueError, TypeError):
        logger.exception("Learning event emission failed (best-effort)")
        warnings.append("analytics_event_failed")

    await db.flush()
    if session_item is not None:
        session_item.result_id = pr.id
        session_item.user_answer = body.user_answer
        session_item.is_correct = is_correct
        session_item.is_blank = not bool(body.user_answer.strip())
    await db.commit()
    await db.refresh(pr)

    if not is_correct and wa:
        background_tasks.add_task(
            _schedule_wrong_answer_postprocessing,
            pr.id,
            wa.id,
            problem.id,
            problem.course_id,
            user.id,
        )
    if is_correct:
        background_tasks.add_task(
            _check_effective_review, problem.id, problem.course_id, user.id
        )

    prerequisite_gaps = None
    if not is_correct and kp_list:
        try:
            from services.loom_graph import check_prerequisite_gaps
            gaps = await check_prerequisite_gaps(
                db, user.id, problem.course_id,
                failed_concept_names=kp_list,
            )
            if gaps:
                prerequisite_gaps = gaps[:3]
        except (SQLAlchemyError, ValueError, KeyError):
            logger.exception("Prerequisite gap check failed (best-effort)")
            warnings.append("prerequisite_gap_check_failed")

    return {
        "result": pr,
        "is_correct": is_correct,
        "correct_answer": correct_answer,
        "explanation": explanation,
        "prerequisite_gaps": prerequisite_gaps,
        "warnings": warnings,
    }


@router.post("/submit", response_model=AnswerResponse, summary="Submit a quiz answer", description="Grade a practice answer, classify errors, and update mastery tracking.")
async def submit_answer(
    body: SubmitAnswerRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit an answer to a practice problem."""
    saved = await persist_problem_answer(body, background_tasks, user, db)
    return AnswerResponse(
        is_correct=saved["is_correct"],
        correct_answer=saved["correct_answer"],
        explanation=saved["explanation"],
        prerequisite_gaps=saved["prerequisite_gaps"],
        warnings=saved["warnings"],
    )


# -- Mastery history time-series endpoint --


@router.get("/{course_id}/mastery-history", response_model=list[MasterySnapshotResponse], summary="Get mastery history", description="Return mastery score time-series for analytics charts.")
async def mastery_history(
    course_id: uuid.UUID,
    content_node_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return mastery score time-series for analytics charts."""
    from models.mastery_snapshot import MasterySnapshot

    await get_course_or_404(db, course_id, user_id=user.id)

    query = (
        select(MasterySnapshot)
        .where(
            MasterySnapshot.user_id == user.id,
            MasterySnapshot.course_id == course_id,
        )
        .order_by(MasterySnapshot.recorded_at.desc())
        .limit(limit)
    )
    if content_node_id:
        query = query.where(MasterySnapshot.content_node_id == content_node_id)

    result = await db.execute(query)
    snapshots = result.scalars().all()

    return [
        MasterySnapshotResponse(
            mastery_score=s.mastery_score,
            gap_type=s.gap_type,
            content_node_id=str(s.content_node_id) if s.content_node_id else None,
            recorded_at=s.recorded_at,
        )
        for s in reversed(snapshots)
    ]
