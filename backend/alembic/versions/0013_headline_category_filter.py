"""headline classification and category filtering metadata

Revision ID: 0013_headline_category_filter
Revises: 0012_newsletter_mail
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_headline_category_filter"
down_revision: str | None = "0012_newsletter_mail"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_columns = {column["name"] for column in inspector.get_columns("user")}
    article_columns = {column["name"] for column in inspector.get_columns("article")}
    if "category_interests" not in user_columns:
        op.add_column(
            "user",
            sa.Column("category_interests", sa.JSON(), nullable=False, server_default="[]"),
        )
    if "headline_category" not in article_columns:
        op.add_column(
            "article", sa.Column("headline_category", sa.String(128), nullable=True)
        )
    if "filter_reason" not in article_columns:
        op.add_column("article", sa.Column("filter_reason", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("article", "filter_reason")
    op.drop_column("article", "headline_category")
    op.drop_column("user", "category_interests")
