"""newsletter ingestion: mail accounts, mail feeds, article intros

Revision ID: 0012_newsletter_mail
Revises: 0011_chat_message
Create Date: 2026-09-06

Per-user IMAP accounts (mail_account) polled for newsletters; senders become
mail Feeds (feed.kind = 'mail', feed.sender_email); each extracted link becomes
an article whose human-written intro is kept on article.newsletter_intro so a
NEW story created from it shows the newsletter text instead of the LLM summary.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_newsletter_mail"
down_revision: str | None = "0011_chat_message"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mail_account",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("host", sa.String(256), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("username", sa.String(256), nullable=False),
        sa.Column("password", sa.String(512), nullable=False),
        sa.Column("folder", sa.String(256), nullable=False),
        sa.Column("use_ssl", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_uid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mail_account_user_id", "mail_account", ["user_id"])
    op.add_column(
        "feed",
        sa.Column("kind", sa.String(16), nullable=False, server_default="rss"),
    )
    op.add_column("feed", sa.Column("sender_email", sa.String(320), nullable=True))
    op.add_column("article", sa.Column("newsletter_intro", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("article", "newsletter_intro")
    op.drop_column("feed", "sender_email")
    op.drop_column("feed", "kind")
    op.drop_index("ix_mail_account_user_id", table_name="mail_account")
    op.drop_table("mail_account")
