"""045 team file blobs — images, PDFs and documents in a team's drive

Revision ID: 045_team_file_blobs
Revises: 044_bot_skills

A drive held text only. A person now attaches a photo or a PDF to a message, and the
file it becomes belongs in the team's drive like any other — listed, searchable,
restorable — so `team_files` learns to hold bytes as well as text.

**`team_file_blobs`** stores bytes once, by content: the SHA-256 of the bytes is the
key. A revision of a binary file, a copy, a restore — each refers to the same row
rather than storing the bytes again, so history costs nothing for files nobody edits.

**`team_files.blob_sha`** says a file is binary. Its `content` is then the text that
could be read out of it (a PDF's, a document's), extracted once when it was stored, and
empty for an image; `bytes` is the blob's size and `media_type` what it is. A text file
has no blob, `media_type` `text/plain` and `bytes` its encoded length. The revisions
table keeps `blob_sha` and `media_type` too, so restoring an old revision of a binary
file brings back its bytes, not only its text.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "045_team_file_blobs"
down_revision: str | None = "044_bot_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_file_blobs",
        sa.Column("sha256", sa.Text, primary_key=True),
        sa.Column("data", sa.LargeBinary, nullable=False),
        sa.Column("bytes", sa.Integer, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("bytes = octet_length(data)", name="ck_team_file_blob_bytes"),
    )
    for table in ("team_files", "team_file_revisions"):
        op.add_column(
            table,
            sa.Column("media_type", sa.Text, nullable=False, server_default="text/plain"),
        )
        op.add_column(
            table,
            sa.Column(
                "blob_sha", sa.Text, sa.ForeignKey("team_file_blobs.sha256"), nullable=True
            ),
        )
    op.add_column("team_files", sa.Column("bytes", sa.Integer, nullable=False, server_default="0"))
    op.execute("UPDATE team_files SET bytes = octet_length(content)")


def downgrade() -> None:
    op.drop_column("team_files", "bytes")
    for table in ("team_file_revisions", "team_files"):
        op.drop_column(table, "blob_sha")
        op.drop_column(table, "media_type")
    op.drop_table("team_file_blobs")
