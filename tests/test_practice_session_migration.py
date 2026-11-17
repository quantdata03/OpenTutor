"""SQLite upgrade coverage for trustworthy practice-session history."""

import sqlite3
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from database import Base
from services.sqlite_migrations import REVISION, current_revision, upgrade_sqlite


@pytest.mark.asyncio
async def test_previous_revision_adds_session_tables_with_backup_and_preserves_results(
    tmp_path,
):
    path = tmp_path / "previous.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    user_id = str(uuid.uuid4())
    course_id = str(uuid.uuid4())
    problem_id = str(uuid.uuid4())
    result_id = str(uuid.uuid4())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql("DROP TABLE practice_session_items")
        await connection.exec_driver_sql("DROP TABLE practice_sessions")
        await connection.exec_driver_sql(
            "CREATE TABLE opentutor_schema_version (revision TEXT NOT NULL PRIMARY KEY)"
        )
        await connection.exec_driver_sql(
            "INSERT INTO opentutor_schema_version VALUES ('sqlite_20261029_01')"
        )
        await connection.execute(
            Base.metadata.tables["users"]
            .insert()
            .values(id=user_id, name="Preserved learner")
        )
        await connection.execute(
            Base.metadata.tables["courses"]
            .insert()
            .values(id=course_id, user_id=user_id, name="Preserved course")
        )
        await connection.execute(
            Base.metadata.tables["practice_problems"]
            .insert()
            .values(
                id=problem_id,
                course_id=course_id,
                question_type="short_answer",
                question="1+1?",
                correct_answer="2",
            )
        )
        await connection.execute(
            Base.metadata.tables["practice_results"]
            .insert()
            .values(
                id=result_id,
                problem_id=problem_id,
                user_id=user_id,
                user_answer="2",
                is_correct=True,
            )
        )

    backup = await upgrade_sqlite(engine)
    assert backup
    with sqlite3.connect(backup) as old:
        names = {
            row[0]
            for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "practice_sessions" not in names
        assert old.execute("SELECT count(*) FROM practice_results").fetchone()[0] == 1
    async with engine.connect() as connection:
        names = {
            row[0]
            for row in (
                await connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ).all()
        }
        assert {"practice_sessions", "practice_session_items"} <= names
        assert (
            await connection.exec_driver_sql("SELECT count(*) FROM practice_results")
        ).scalar() == 1
        assert await connection.run_sync(current_revision) == REVISION
    await engine.dispose()
