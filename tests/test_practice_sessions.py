"""Focused tests for server-verified whole-practice sessions."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database import Base
from libs.exceptions import ConflictError, NotFoundError, ValidationError
from models.course import Course
from models.practice import (
    PracticeProblem,
    PracticeResult,
    PracticeSession,
    PracticeSessionItem,
)
from models.user import User
from routers.practice_sessions import (
    finalize_practice_session,
    get_practice_session,
    list_practice_sessions,
    start_practice_session,
    submit_practice_session_answer,
)
from routers.quiz_submission import submit_answer
from schemas.practice_session import (
    PracticeSessionAnswerRequest,
    PracticeSessionFinalizeRequest,
    PracticeSessionStartRequest,
)
from schemas.quiz import SubmitAnswerRequest


@pytest_asyncio.fixture
async def session_env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        user = User(id=uuid.uuid4(), name="Session learner")
        other = User(id=uuid.uuid4(), name="Other learner")
        course = Course(id=uuid.uuid4(), user_id=user.id, name="Math")
        other_course = Course(id=uuid.uuid4(), user_id=other.id, name="Private")
        db.add_all([user, other, course, other_course])
        problems = []
        for index in range(3):
            problem = PracticeProblem(
                id=uuid.uuid4(),
                course_id=course.id,
                question_type="short_answer",
                question=f"Question {index + 1}",
                correct_answer=str(index + 1),
                explanation=f"Explanation {index + 1}",
                knowledge_points=[f"point-{index + 1}"],
                difficulty_layer=2,
                problem_metadata={
                    "core_concept": f"Core {index + 1}",
                    "reasoning": f"Reason {index + 1}",
                    "solution_steps": ["Step 1", "Step 2"],
                    "common_mistake": "Sign error",
                    "source_page": index + 10,
                    "review_presentation_version": "keira.review-presentation.v1",
                    "option_analysis": [
                        {"option": "A", "judgment": "incorrect", "reason": "Not equal"}
                    ],
                    "conclusion": f"Conclusion {index + 1}",
                },
            )
            problems.append(problem)
        foreign_problem = PracticeProblem(
            id=uuid.uuid4(),
            course_id=other_course.id,
            question_type="short_answer",
            question="Private",
            correct_answer="x",
        )
        db.add_all([*problems, foreign_problem])
        await db.commit()
        yield engine, factory, db, user, other, course, problems, foreign_problem
    await engine.dispose()


async def _start(db, user, course, problems):
    return await start_practice_session(
        PracticeSessionStartRequest(
            course_id=course.id, problem_ids=[problem.id for problem in problems]
        ),
        user=user,
        db=db,
    )


async def _answer(db, user, session_id, problem, answer, key):
    return await submit_practice_session_answer(
        session_id,
        PracticeSessionAnswerRequest(
            problem_id=problem.id,
            user_answer=answer,
            answer_time_ms=25,
            submission_key=key,
        ),
        BackgroundTasks(),
        user=user,
        db=db,
    )


@pytest.mark.asyncio
async def test_whole_session_idempotency_blank_finalize_and_immutable_detail(
    session_env,
):
    _, _, db, user, _, course, problems, _ = session_env
    started = await _start(db, user, course, problems)
    assert [item.ordinal for item in started.items] == [1, 2, 3]

    first = await _answer(db, user, started.id, problems[0], "1", "answer-1")
    replay = await _answer(db, user, started.id, problems[0], "1", "answer-1")
    assert replay.idempotent is True and replay.result_id == first.result_id
    with pytest.raises(ConflictError):
        await _answer(db, user, started.id, problems[0], "changed", "answer-1")
    with pytest.raises(ConflictError):
        await _answer(db, user, started.id, problems[0], "wrong", "answer-other")

    wrong = await _answer(db, user, started.id, problems[1], "wrong", "answer-2")
    blank = await _answer(db, user, started.id, problems[2], "", "answer-3")
    result_ids = [first.result_id, wrong.result_id, blank.result_id]
    summary = await finalize_practice_session(
        started.id,
        PracticeSessionFinalizeRequest(finalize_key="final-1", result_ids=result_ids),
        user=user,
        db=db,
    )
    assert (
        summary.total_count,
        summary.correct_count,
        summary.blank_count,
        summary.wrong_count,
    ) == (3, 1, 1, 1)
    replay_summary = await finalize_practice_session(
        started.id,
        PracticeSessionFinalizeRequest(
            finalize_key="final-1", result_ids=list(reversed(result_ids))
        ),
        user=user,
        db=db,
    )
    assert replay_summary.result_ids == summary.result_ids

    problems[0].explanation = "MUTATED"
    problems[0].problem_metadata = {"core_concept": "MUTATED"}
    await db.commit()
    detail = await get_practice_session(started.id, user=user, db=db)
    assert detail.items[0].review_snapshot["explanation"] == "Explanation 1"
    assert detail.items[0].review_snapshot["core_concept"] == "Core 1"
    assert (
        detail.items[0].review_snapshot["review_presentation_version"]
        == "keira.review-presentation.v1"
    )
    assert await db.scalar(select(func.count()).select_from(PracticeResult)) == 3


@pytest.mark.asyncio
async def test_finalize_rejects_missing_foreign_and_mismatched_results(session_env):
    _, _, db, user, other, course, problems, foreign_problem = session_env
    started = await _start(db, user, course, problems[:2])
    first = await _answer(db, user, started.id, problems[0], "1", "a1")
    with pytest.raises(ConflictError):
        await finalize_practice_session(
            started.id,
            PracticeSessionFinalizeRequest(
                finalize_key="early", result_ids=[first.result_id]
            ),
            user=user,
            db=db,
        )
    with pytest.raises(ConflictError):
        await _answer(db, user, started.id, problems[1], "2", "a1")
    for value in (user, other, problems[1], foreign_problem):
        await db.refresh(value)
    second = await _answer(db, user, started.id, problems[1], "2", "a2")
    foreign_result = PracticeResult(
        problem_id=foreign_problem.id,
        user_id=other.id,
        user_answer="x",
        is_correct=True,
    )
    db.add(foreign_result)
    await db.commit()
    with pytest.raises(ConflictError):
        await finalize_practice_session(
            started.id,
            PracticeSessionFinalizeRequest(
                finalize_key="foreign", result_ids=[first.result_id, foreign_result.id]
            ),
            user=user,
            db=db,
        )
    # The failed finalize did not consume the session.
    summary = await finalize_practice_session(
        started.id,
        PracticeSessionFinalizeRequest(
            finalize_key="good", result_ids=[first.result_id, second.result_id]
        ),
        user=user,
        db=db,
    )
    assert summary.status == "finalized"


@pytest.mark.asyncio
async def test_owner_boundaries_and_open_session_history_visibility(session_env):
    _, _, db, user, other, course, problems, foreign_problem = session_env
    with pytest.raises(NotFoundError):
        await start_practice_session(
            PracticeSessionStartRequest(
                course_id=course.id, problem_ids=[foreign_problem.id]
            ),
            user=user,
            db=db,
        )
    started = await _start(db, user, course, problems[:1])
    with pytest.raises(NotFoundError):
        await get_practice_session(started.id, user=user, db=db)
    with pytest.raises(NotFoundError):
        await get_practice_session(started.id, user=other, db=db)


@pytest.mark.asyncio
async def test_bounded_keyset_history_and_cursor_validation(session_env):
    _, _, db, user, _, course, problems, _ = session_env
    finalized = []
    for index in range(3):
        started = await _start(db, user, course, problems[:1])
        answer = await _answer(db, user, started.id, problems[0], "1", f"a-{index}")
        finalized.append(
            await finalize_practice_session(
                started.id,
                PracticeSessionFinalizeRequest(
                    finalize_key=f"f-{index}", result_ids=[answer.result_id]
                ),
                user=user,
                db=db,
            )
        )
    now = datetime.now(timezone.utc)
    first_page = await list_practice_sessions(
        course_id=course.id,
        start_utc=now - timedelta(days=1),
        end_utc=now + timedelta(days=1),
        limit=2,
        cursor=None,
        user=user,
        db=db,
    )
    assert len(first_page.sessions) == 2 and first_page.next_cursor
    second_page = await list_practice_sessions(
        course_id=course.id,
        start_utc=now - timedelta(days=1),
        end_utc=now + timedelta(days=1),
        limit=2,
        cursor=first_page.next_cursor,
        user=user,
        db=db,
    )
    assert len(second_page.sessions) == 1
    assert {x.id for x in first_page.sessions}.isdisjoint(
        {x.id for x in second_page.sessions}
    )
    with pytest.raises(ValidationError):
        await list_practice_sessions(
            course_id=course.id,
            start_utc=now - timedelta(days=32),
            end_utc=now,
            limit=20,
            cursor=None,
            user=user,
            db=db,
        )
    with pytest.raises(ValidationError):
        await list_practice_sessions(
            course_id=course.id,
            start_utc=now - timedelta(days=1),
            end_utc=now,
            limit=20,
            cursor="%%%invalid%%%",
            user=user,
            db=db,
        )


@pytest.mark.asyncio
async def test_legacy_submit_does_not_create_a_session(session_env):
    _, _, db, user, _, _, problems, _ = session_env
    response = await submit_answer(
        SubmitAnswerRequest(problem_id=problems[0].id, user_answer="1"),
        BackgroundTasks(),
        user=user,
        db=db,
    )
    assert response.is_correct is True
    assert await db.scalar(select(func.count()).select_from(PracticeSession)) == 0
    assert await db.scalar(select(func.count()).select_from(PracticeSessionItem)) == 0


@pytest.mark.asyncio
async def test_concurrent_same_key_creates_one_result(session_env):
    _, factory, db, user, _, course, problems, _ = session_env
    started = await _start(db, user, course, problems[:1])

    async def submit_once():
        async with factory() as isolated:
            return await _answer(
                isolated, user, started.id, problems[0], "1", "same-key"
            )

    outcomes = await asyncio.gather(
        submit_once(), submit_once(), return_exceptions=True
    )
    successes = [value for value in outcomes if not isinstance(value, BaseException)]
    # A simultaneous retry may receive the committed idempotent response or a
    # bounded conflict while the winner owns the reservation. It may never
    # create a second result.
    assert successes
    async with factory() as verify:
        assert (
            await verify.scalar(select(func.count()).select_from(PracticeResult)) == 1
        )
        item = (await verify.execute(select(PracticeSessionItem))).scalar_one()
        assert item.result_id == successes[0].result_id
