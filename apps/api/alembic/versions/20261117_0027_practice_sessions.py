"""Add trustworthy whole-practice sessions and immutable review items.

Revision ID: 20261117_0027
Revises: 20261028_0026
"""

from alembic import op
import sqlalchemy as sa


revision = "20261117_0027"
down_revision = "20261028_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "practice_sessions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("course_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("correct_count", sa.Integer(), nullable=True),
        sa.Column("blank_count", sa.Integer(), nullable=True),
        sa.Column("finalize_key", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'finalized')", name="ck_practice_session_status"
        ),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "finalize_key", name="uq_practice_session_user_finalize_key"
        ),
    )
    op.create_index(
        "ix_practice_session_user_finalized",
        "practice_sessions",
        ["user_id", "finalized_at", "id"],
    )
    op.create_index(
        "ix_practice_session_user_course_finalized",
        "practice_sessions",
        ["user_id", "course_id", "finalized_at", "id"],
    )
    op.create_table(
        "practice_session_items",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("problem_id", sa.String(36), nullable=False),
        sa.Column("result_id", sa.String(36), nullable=True),
        sa.Column("submission_key", sa.String(128), nullable=True),
        sa.Column("is_blank", sa.Boolean(), nullable=False),
        sa.Column("user_answer", sa.Text(), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.Column("review_snapshot", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["problem_id"], ["practice_problems.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["result_id"], ["practice_results.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["practice_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("result_id", name="uq_practice_session_item_result"),
        sa.UniqueConstraint(
            "session_id", "ordinal", name="uq_practice_session_item_ordinal"
        ),
        sa.UniqueConstraint(
            "session_id", "problem_id", name="uq_practice_session_item_problem"
        ),
        sa.UniqueConstraint(
            "session_id",
            "submission_key",
            name="uq_practice_session_item_submission_key",
        ),
    )
    op.create_index(
        "ix_practice_session_item_session",
        "practice_session_items",
        ["session_id", "ordinal"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_practice_session_item_session", table_name="practice_session_items"
    )
    op.drop_table("practice_session_items")
    op.drop_index(
        "ix_practice_session_user_course_finalized", table_name="practice_sessions"
    )
    op.drop_index("ix_practice_session_user_finalized", table_name="practice_sessions")
    op.drop_table("practice_sessions")
